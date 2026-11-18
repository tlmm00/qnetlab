"""Medição de custo do emulador: tempo por etapa e consumo de recursos do host (CPU, RAM, CHRs, contêineres).

* :class:`StageTimer`: cronometra etapas (``pki``, ``images``, ``deploy`` ...); usado por ``Lab.up``.
* :class:`ResourceSampler`: amostra periodicamente, em uma *thread*, o host (``/proc``), cada CHR (processo
  ``qemu-system-*``, pelo ``-name``) e cada contêiner (``docker stats``). Só enxerga o host onde o ``qnetlab`` roda:
  vale quando o servidor GNS3 é local (``gns3.url`` em ``localhost``).
* :func:`saturation`: aplica a regra "CPU > 80 % invalida a medição" do plano de validação (DES-05).
* :func:`environment`: registro do ambiente (CPU, RAM, kernel, versões, hash da topologia) para a evidência.

Stdlib apenas; o leitor de ``/proc`` aceita um diretório alternativo para testes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from . import __version__
from .workspace import Workspace

log = logging.getLogger(__name__)

CPU_SATURATION_PCT = 80.0


# ---------------------------------------------------------------------------- tempo
class StageTimer:
    """Cronômetro de etapas. ``with timer.stage("deploy"): ...`` registra duração e sucesso, mesmo se houver exceção."""

    def __init__(self, clock: Callable[[], float] = time.perf_counter, wall: Callable[[], float] = time.time):
        self._clock, self._wall = clock, wall
        self.stages: List[Dict[str, Any]] = []
        self._t0 = clock()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start, wall = self._clock(), self._wall()
        ok = False
        try:
            yield
            ok = True
        finally:
            self.stages.append({"stage": name, "start_ts": round(wall, 3),
                                "seconds": round(self._clock() - start, 3), "ok": ok})

    def report(self) -> Dict[str, Any]:
        return {"stages": list(self.stages),
                "total_seconds": round(sum(s["seconds"] for s in self.stages), 3),
                "elapsed_seconds": round(self._clock() - self._t0, 3)}


# ------------------------------------------------------------------------ leitores
def read_host(proc: Path = Path("/proc")) -> Dict[str, Any]:
    """Contadores do host: jiffies de CPU (total/ocioso/roubado), memória e carga."""
    out: Dict[str, Any] = {}
    stat = (proc / "stat").read_text().splitlines()[0].split()
    vals = [int(x) for x in stat[1:9]]                                   # user nice system idle iowait irq softirq steal
    out["cpu_total"], out["cpu_idle"], out["cpu_steal"] = sum(vals), vals[3] + vals[4], vals[7]
    mem: Dict[str, int] = {}
    for line in (proc / "meminfo").read_text().splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = int(v.split()[0])
    out["mem_total_kb"], out["mem_avail_kb"] = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    try:
        out["load1"] = float((proc / "loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        out["load1"] = None
    return out


def read_qemu(proc: Path = Path("/proc"), project_id: Optional[str] = None,
              page_size: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    """CHRs (processos qemu) por nome do nó: ``{nome: {pid, rss_kb, ticks}}``."""
    page = page_size or os.sysconf("SC_PAGE_SIZE")
    out: Dict[str, Dict[str, Any]] = {}
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
            if not argv or not os.path.basename(argv[0].decode(errors="replace")).startswith("qemu-system"):
                continue
            args = [a.decode(errors="replace") for a in argv if a]
            if project_id and not any(project_id in a for a in args):
                continue
            name = args[args.index("-name") + 1].split(",")[0] if "-name" in args else f"pid{d.name}"
            raw = (d / "stat").read_text()
            rest = raw[raw.rindex(")") + 2:].split()
            ticks = int(rest[11]) + int(rest[12])                         # utime + stime
            rss = int((d / "statm").read_text().split()[1]) * page // 1024
            out[name] = {"pid": int(d.name), "rss_kb": rss, "ticks": ticks}
        except (OSError, ValueError, IndexError):
            continue                                                      # processo terminou durante a leitura
    return out


_UNITS = {"b": 1 / 1048576, "kb": 1 / 1024, "kib": 1 / 1024, "mb": 1.0, "mib": 1.0, "gb": 1024.0, "gib": 1024.0}


def _mb(text: str) -> float:
    m = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", text or "")
    return round(float(m.group(1)) * _UNITS.get(m.group(2).lower(), 0.0), 2) if m else 0.0


def read_docker(project_id: str, runner: Optional[Callable[[Sequence[str]], str]] = None) -> Dict[str, Dict[str, float]]:
    """Contêineres do projeto (``GNS3.<nó>.<projeto>``): ``{nó: {cpu_pct, mem_mb}}`` via ``docker stats``."""
    def run(cmd: Sequence[str]) -> str:
        res = subprocess.run(list(cmd), capture_output=True, text=True, timeout=30)
        return res.stdout if res.returncode == 0 else ""

    text = (runner or run)(["docker", "stats", "--no-stream", "--format", "{{json .}}"])
    out: Dict[str, Dict[str, float]] = {}
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        m = re.match(rf"^GNS3\.(.+?)\.{re.escape(project_id)}$", row.get("Name", ""), re.IGNORECASE)
        if not m:
            continue
        out[m.group(1)] = {"cpu_pct": float(row.get("CPUPerc", "0").strip("% ") or 0),
                           "mem_mb": _mb(row.get("MemUsage", "").split("/")[0])}
    return out


# --------------------------------------------------------------------------- amostrador
def _stats(values: Sequence[float]) -> Dict[str, float]:
    v = sorted(values)
    p95 = v[min(len(v) - 1, max(0, int(round(0.95 * len(v) + 0.5)) - 1))]
    return {"n": len(v), "min": round(v[0], 3), "mean": round(sum(v) / len(v), 3),
            "p50": round(v[len(v) // 2], 3), "p95": round(p95, 3), "max": round(v[-1], 3)}


class ResourceSampler:
    """``project_id`` pode ser um texto ou uma função (lida a cada amostra: o ID só existe após o ``deploy``).
    Sem ID, só o host é amostrado (CHRs e contêineres exigem o projeto, para não contar laboratórios alheios)."""

    def __init__(self, project_id: "str | Callable[[], Optional[str]] | None" = None, interval: float = 2.0,
                 proc: Path = Path("/proc"),
                 use_docker: bool = True, docker_runner: Optional[Callable[[Sequence[str]], str]] = None,
                 clock: Callable[[], float] = time.time, ticks_per_s: Optional[int] = None):
        self.project_id, self.interval, self.proc = project_id, interval, proc
        self.use_docker, self._docker_runner, self._clock = use_docker, docker_runner, clock
        self._hz = ticks_per_s or os.sysconf("SC_CLK_TCK")
        self.samples: List[Dict[str, Any]] = []
        self._prev: Optional[Dict[str, Any]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._t0 = clock()

    def sample(self) -> Dict[str, Any]:
        """Uma amostra. Taxas (CPU %) precisam da amostra anterior; a primeira só traz valores absolutos."""
        now = self._clock()
        pid = self.project_id() if callable(self.project_id) else self.project_id
        host = read_host(self.proc)
        qemu = read_qemu(self.proc, pid) if pid else {}
        row: Dict[str, Any] = {"t": round(now - self._t0, 2), "ts": round(now, 3), "host": {}, "chr": {}, "containers": {}}
        row["host"]["mem_used_mb"] = round((host["mem_total_kb"] - host["mem_avail_kb"]) / 1024, 1)
        row["host"]["mem_avail_mb"] = round(host["mem_avail_kb"] / 1024, 1)
        row["host"]["load1"] = host["load1"]
        prev = self._prev
        if prev:
            dt = max(now - prev["ts"], 1e-6)
            dtot = host["cpu_total"] - prev["host"]["cpu_total"]
            if dtot > 0:
                row["host"]["cpu_pct"] = round(100.0 * (1 - (host["cpu_idle"] - prev["host"]["cpu_idle"]) / dtot), 2)
                row["host"]["steal_pct"] = round(100.0 * (host["cpu_steal"] - prev["host"]["cpu_steal"]) / dtot, 2)
        for name, q in qemu.items():
            r = {"rss_mb": round(q["rss_kb"] / 1024, 1)}
            if prev and name in prev["qemu"]:
                r["cpu_pct"] = round(100.0 * (q["ticks"] - prev["qemu"][name]["ticks"]) / self._hz / dt, 2)
            row["chr"][name] = r
        if self.use_docker and pid:
            try:
                row["containers"] = read_docker(pid, self._docker_runner)
            except (OSError, subprocess.SubprocessError) as exc:
                log.debug("docker stats indisponível: %s", exc)
        self._prev = {"ts": now, "host": host, "qemu": qemu}
        self.samples.append(row)
        return row

    # thread ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception as exc:                                       # a medição nunca derruba o experimento
                log.debug("amostra falhou: %s", exc)
            self._stop.wait(self.interval)

    def start(self) -> "ResourceSampler":
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="resource-sampler", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 35)

    def __enter__(self) -> "ResourceSampler":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # resumo --------------------------------------------------------------------
    def summary(self) -> Dict[str, Dict[str, float]]:
        series: Dict[str, List[float]] = {}

        def add(key: str, val: Optional[float]) -> None:
            if val is not None:
                series.setdefault(key, []).append(float(val))

        for s in self.samples:
            for k, v in s["host"].items():
                add(f"host.{k}", v)
            for n, r in s["chr"].items():
                for k, v in r.items():
                    add(f"chr.{n}.{k}", v)
            for n, r in s["containers"].items():
                for k, v in r.items():
                    add(f"containers.{n}.{k}", v)
            if s["chr"]:
                add("chr_total.rss_mb", sum(r["rss_mb"] for r in s["chr"].values()))
                cpu = [r["cpu_pct"] for r in s["chr"].values() if "cpu_pct" in r]
                if cpu:
                    add("chr_total.cpu_pct", sum(cpu))
            if s["containers"]:
                add("containers_total.mem_mb", sum(r["mem_mb"] for r in s["containers"].values()))
                add("containers_total.cpu_pct", sum(r["cpu_pct"] for r in s["containers"].values()))
        return {k: _stats(v) for k, v in sorted(series.items()) if v}


def saturation(summary: Dict[str, Dict[str, float]], threshold: float = CPU_SATURATION_PCT) -> Dict[str, Any]:
    """Regra do plano (DES-05): CPU do host acima do limite em p95 invalida a medição de desempenho."""
    host = summary.get("host.cpu_pct", {}).get("p95")
    steal = summary.get("host.steal_pct", {}).get("p95")
    return {"threshold_pct": threshold, "host_cpu_p95": host, "host_steal_p95": steal,
            "valid": host is not None and host <= threshold,
            "note": None if host is not None else "sem amostras de CPU (intervalo curto demais?)"}


# ----------------------------------------------------------------------- ambiente
def _cmd(argv: Sequence[str]) -> Optional[str]:
    try:
        res = subprocess.run(list(argv), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else None


def environment(topology_path: "str | Path | None" = None, extra: Optional[Dict[str, Any]] = None,
                proc: Path = Path("/proc")) -> Dict[str, Any]:
    cpu_model = None
    try:
        for line in (proc / "cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    try:
        mem_total_mb = round(read_host(proc)["mem_total_kb"] / 1024)
    except (OSError, ValueError, IndexError):
        mem_total_mb = None
    env: Dict[str, Any] = {
        "qnetlab": __version__, "python": platform.python_version(), "kernel": platform.release(),
        "cpu_model": cpu_model, "cpus": os.cpu_count(), "mem_total_mb": mem_total_mb,
        "docker": _cmd(["docker", "version", "--format", "{{.Server.Version}}"]),
        "git_commit": _cmd(["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"]),
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if topology_path:
        env["topology_sha256"] = hashlib.sha256(Path(topology_path).read_bytes()).hexdigest()
    env.update(extra or {})
    return env


def write_report(ws: Workspace, label: str, data: Dict[str, Any]) -> Path:
    ws.bench_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label) or "bench"
    path = ws.bench_dir / f"{safe}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    ws.write_text(path, json.dumps(data, indent=2, default=str) + "\n")
    return path
