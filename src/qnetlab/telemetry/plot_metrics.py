"""Consolidação de métricas e geração de gráficos (matplotlib opcional).

Entrada: diretório com ``*.jsonl`` (um por APP) contendo eventos:

* ``probe``       {peer, protected, seq, rtt_ms|null}   -> latência e perda
* ``key_request`` {peer, ok, latency_ms}                -> obtenção de chave ao KME
* ``accuracy``    {round, value}                        -> opcional (aprendizado distribuído)

Saída: ``summary.json``, ``summary.csv`` e PNGs (RTT no tempo, CDF, perda, latência de chave,
acurácia) em ``--output``. Uso: ``python -m qnetlab.telemetry.plot_metrics -i metrics -o plots``.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List

from ..errors import TelemetryError

log = logging.getLogger(__name__)


def percentile(values: List[float], q: float) -> float:
    """Percentil por interpolação linear (q em [0, 100])."""
    if not values:
        return math.nan
    xs = sorted(values)
    k = (len(xs) - 1) * q / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return xs[int(k)] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def read_events(metrics_dir: Path) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    files = sorted(Path(metrics_dir).glob("*.jsonl"))
    if not files:
        raise TelemetryError(f"Nenhum arquivo .jsonl em {metrics_dir}")
    for f in files:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                log.warning("%s:%d ignorada (JSON inválido)", f.name, n)
                continue
            ev.setdefault("node", f.stem)
            events.append(ev)
    return events


def summarize(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    probes: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    keys: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    acc: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for ev in events:
        t = ev.get("type")
        if t == "probe":
            probes[(ev["node"], ev.get("peer", "?"), bool(ev.get("protected")))].append(ev)
        elif t == "key_request":
            keys[(ev["node"], ev.get("peer", "?"))].append(ev)
        elif t == "accuracy":
            acc[ev["node"]].append(ev)

    out: Dict[str, Any] = {"probes": [], "keys": [], "accuracy": []}
    for (node, peer, prot), evs in sorted(probes.items()):
        rtts = [e["rtt_ms"] for e in evs if e.get("rtt_ms") is not None]
        sent = len(evs)
        out["probes"].append({
            "src": node, "dst": peer, "protected": prot, "sent": sent, "received": len(rtts),
            "loss_pct": round(100.0 * (sent - len(rtts)) / sent, 3) if sent else math.nan,
            "rtt_mean_ms": round(sum(rtts) / len(rtts), 3) if rtts else math.nan,
            "rtt_p50_ms": round(percentile(rtts, 50), 3), "rtt_p95_ms": round(percentile(rtts, 95), 3),
            "rtt_p99_ms": round(percentile(rtts, 99), 3),
            "rtt_max_ms": round(max(rtts), 3) if rtts else math.nan,
        })
    for (node, peer), evs in sorted(keys.items()):
        ok = [e["latency_ms"] for e in evs if e.get("ok")]
        out["keys"].append({
            "node": node, "peer": peer, "requests": len(evs), "ok": len(ok),
            "failure_pct": round(100.0 * (len(evs) - len(ok)) / len(evs), 3),
            "latency_mean_ms": round(sum(ok) / len(ok), 3) if ok else math.nan,
            "latency_p95_ms": round(percentile(ok, 95), 3),
        })
    for node, evs in sorted(acc.items()):
        vals = [float(e["value"]) for e in evs if "value" in e]
        if vals:
            out["accuracy"].append({"node": node, "rounds": len(vals), "final": vals[-1], "best": max(vals)})
    return out


def write_tables(summary: Dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=True), encoding="utf-8")
    for name in ("probes", "keys", "accuracy"):
        rows = summary[name]
        if not rows:
            continue
        with open(out_dir / f"summary_{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)


def make_plots(events: List[Dict[str, Any]], summary: Dict[str, Any], out_dir: Path) -> List[Path]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        log.warning("matplotlib ausente: gráficos não gerados (pip install matplotlib).")
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    made: List[Path] = []

    def save(fig: Any, name: str) -> None:
        p = out_dir / name
        fig.tight_layout()
        fig.savefig(p, dpi=140)
        plt.close(fig)
        made.append(p)

    probes = [e for e in events if e.get("type") == "probe"]
    if probes:
        t0 = min(e["t"] for e in probes)
        # RTT no tempo (protegido x claro)
        fig, ax = plt.subplots(figsize=(9, 4))
        for prot, color, label in ((True, "tab:red", "protegido (IPsec+PPK)"), (False, "tab:blue", "em claro")):
            pts = [(e["t"] - t0, e["rtt_ms"]) for e in probes if bool(e.get("protected")) == prot and e.get("rtt_ms") is not None]
            if pts:
                ax.scatter(*zip(*pts), s=4, alpha=0.5, color=color, label=label)
        ax.set(xlabel="tempo (s)", ylabel="RTT (ms)", title="RTT das sondas UDP")
        ax.legend()
        save(fig, "rtt_time.png")
        # CDF
        fig, ax = plt.subplots(figsize=(6, 4))
        for prot, color, label in ((True, "tab:red", "protegido"), (False, "tab:blue", "em claro")):
            xs = sorted(e["rtt_ms"] for e in probes if bool(e.get("protected")) == prot and e.get("rtt_ms") is not None)
            if xs:
                ax.plot(xs, [(i + 1) / len(xs) for i in range(len(xs))], color=color, label=label)
        ax.set(xlabel="RTT (ms)", ylabel="CDF", title="Distribuição de RTT")
        ax.legend()
        save(fig, "rtt_cdf.png")
        # perda por par
        rows = summary["probes"]
        fig, ax = plt.subplots(figsize=(max(6, 0.7 * len(rows) + 2), 4))
        ax.bar([f"{r['src']}→{r['dst']}" for r in rows], [r["loss_pct"] for r in rows],
               color=["tab:red" if r["protected"] else "tab:blue" for r in rows])
        ax.set(ylabel="perda (%)", title="Perda de pacotes por par")
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
        save(fig, "loss.png")

    keyrows = [e for e in events if e.get("type") == "key_request" and e.get("ok")]
    if keyrows:
        t0 = min(e["t"] for e in keyrows)
        fig, ax = plt.subplots(figsize=(8, 4))
        for node in sorted({e["node"] for e in keyrows}):
            pts = [(e["t"] - t0, e["latency_ms"]) for e in keyrows if e["node"] == node]
            ax.plot(*zip(*pts), marker="o", ms=3, label=node)
        ax.set(xlabel="tempo (s)", ylabel="latência (ms)", title="Latência de obtenção de chave (KME)")
        ax.legend(fontsize=7)
        save(fig, "key_latency.png")

    accrows = [e for e in events if e.get("type") == "accuracy" and "value" in e]
    if accrows:
        fig, ax = plt.subplots(figsize=(7, 4))
        for node in sorted({e["node"] for e in accrows}):
            pts = sorted((e.get("round", i), e["value"]) for i, e in enumerate(x for x in accrows if x["node"] == node))
            ax.plot(*zip(*pts), label=node)
        ax.set(xlabel="rodada", ylabel="acurácia", title="Acurácia do aprendizado distribuído")
        ax.legend(fontsize=7)
        save(fig, "accuracy.png")
    return made


def consolidate(metrics_dir: Path, out_dir: Path) -> Dict[str, Any]:
    events = read_events(Path(metrics_dir))
    summary = summarize(events)
    write_tables(summary, Path(out_dir))
    summary["plots"] = [p.name for p in make_plots(events, summary, Path(out_dir))]
    return summary


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Consolida métricas do QNetLab e gera gráficos.")
    ap.add_argument("-i", "--input", required=True, type=Path, help="diretório com *.jsonl")
    ap.add_argument("-o", "--output", required=True, type=Path, help="diretório de saída")
    args = ap.parse_args(argv)
    try:
        summary = consolidate(args.input, args.output)
    except TelemetryError as exc:
        print(f"erro: {exc}")
        return 2
    print(json.dumps({k: len(v) if isinstance(v, list) else v for k, v in summary.items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
