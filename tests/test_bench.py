"""Bench: cronômetro de etapas, leitores de /proc (diretório falso), docker stats e regra de saturação."""
import json
import os

import pytest

from qnetlab import bench
from qnetlab.bench import ResourceSampler, StageTimer, read_docker, read_host, read_qemu, saturation


def make_proc(root, cpu=(100, 0, 50, 800, 50, 0, 0, 0), mem=(8_000_000, 5_000_000), load="0.50 0.4 0.3 1/200 1",
              qemu=None):
    """Cria um /proc mínimo. qemu: {pid: (nome, projeto, utime, stime, rss_pages)}."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "stat").write_text("cpu  " + " ".join(map(str, cpu)) + " 0 0\ncpu0 1 1 1 1 1 1 1 1\n")
    (root / "meminfo").write_text(f"MemTotal:       {mem[0]} kB\nMemFree: 1 kB\nMemAvailable:   {mem[1]} kB\n")
    (root / "loadavg").write_text(load)
    for pid, (name, proj, ut, st, rss) in (qemu or {}).items():
        d = root / str(pid)
        d.mkdir(exist_ok=True)
        cmd = ["qemu-system-x86_64", "-name", f"{name},process=x", "-drive", f"file=/gns3/projects/{proj}/disk.qcow2"]
        (d / "cmdline").write_bytes(b"\0".join(c.encode() for c in cmd) + b"\0")
        fields = ["S"] + ["0"] * 10 + [str(ut), str(st)] + ["0"] * 30
        (d / "stat").write_text(f"{pid} (qemu-system-x86) " + " ".join(fields))
        (d / "statm").write_text(f"1000 {rss} 0 0 0 0 0")
    return root


def test_stage_timer_records_duration_and_failures():
    t = iter([0.0, 1.0, 2.0, 3.0, 6.5, 9.0])          # init, pki(início, fim), deploy(início, fim), report
    timer = StageTimer(clock=lambda: next(t), wall=lambda: 1700000000.0)
    with timer.stage("pki"):
        pass
    with pytest.raises(RuntimeError):
        with timer.stage("deploy"):
            raise RuntimeError("boom")
    rep = timer.report()
    assert [(s["stage"], s["seconds"], s["ok"]) for s in rep["stages"]] == [("pki", 1.0, True), ("deploy", 3.5, False)]
    assert rep["total_seconds"] == 4.5 and rep["elapsed_seconds"] == 9.0
    assert rep["stages"][0]["start_ts"] == 1700000000.0


def test_read_host(tmp_path):
    h = read_host(make_proc(tmp_path / "proc", cpu=(100, 10, 50, 800, 40, 0, 0, 7)))
    assert h["cpu_total"] == 1007 and h["cpu_idle"] == 840 and h["cpu_steal"] == 7
    assert h["mem_total_kb"] == 8_000_000 and h["mem_avail_kb"] == 5_000_000 and h["load1"] == 0.5


def test_read_qemu_filters_by_project_and_parses_name(tmp_path):
    proc = make_proc(tmp_path / "proc", qemu={11: ("MKT11", "PID-A", 300, 100, 2560), 12: ("OTHER", "PID-B", 5, 5, 10)})
    (proc / "999").mkdir()
    (proc / "999" / "cmdline").write_bytes(b"/bin/bash\0")
    got = read_qemu(proc, "PID-A", page_size=4096)
    assert list(got) == ["MKT11"] and got["MKT11"] == {"pid": 11, "rss_kb": 10240, "ticks": 400}
    assert set(read_qemu(proc, None, page_size=4096)) == {"MKT11", "OTHER"}


def test_read_qemu_survives_process_vanishing(tmp_path):
    proc = make_proc(tmp_path / "proc", qemu={11: ("MKT11", "P", 1, 1, 10)})
    (proc / "11" / "stat").unlink()
    assert read_qemu(proc, "P") == {}


DOCKER_OUT = "\n".join(json.dumps(r) for r in [
    {"Name": "GNS3.KME1.PID-A", "CPUPerc": "1.50%", "MemUsage": "40.5MiB / 7.6GiB"},
    {"Name": "GNS3.NODE1.PID-A", "CPUPerc": "0.20%", "MemUsage": "2GiB / 7.6GiB"},
    {"Name": "GNS3.KME1.PID-B", "CPUPerc": "9%", "MemUsage": "1MiB / 1GiB"},
    {"Name": "unrelated", "CPUPerc": "50%", "MemUsage": "1GiB / 2GiB"}]) + "\nnot json\n"


def test_read_docker_filters_project_and_converts_units():
    got = read_docker("PID-A", runner=lambda cmd: DOCKER_OUT)
    assert got == {"KME1": {"cpu_pct": 1.5, "mem_mb": 40.5}, "NODE1": {"cpu_pct": 0.2, "mem_mb": 2048.0}}


def test_sampler_computes_rates_between_samples(tmp_path):
    proc = tmp_path / "proc"
    make_proc(proc, cpu=(100, 0, 0, 900, 0, 0, 0, 0), qemu={11: ("MKT11", "PID-A", 100, 0, 1000)})
    now = [1000.0]
    s = ResourceSampler("PID-A", proc=proc, use_docker=True, docker_runner=lambda c: DOCKER_OUT,
                        clock=lambda: now[0], ticks_per_s=100)
    first = s.sample()
    assert "cpu_pct" not in first["host"] and "cpu_pct" not in first["chr"]["MKT11"]
    # 10 s depois: +200 jiffies de CPU em um total de +1000 (20 % ocupado), 4 s de CPU do qemu em 10 s = 40 %
    make_proc(proc, cpu=(300, 0, 0, 1700, 0, 0, 0, 20), qemu={11: ("MKT11", "PID-A", 400, 0, 1000)})
    now[0] = 1010.0
    second = s.sample()
    assert second["host"]["cpu_pct"] == pytest.approx(100 * (1 - 800 / 1020), abs=0.01)
    assert second["host"]["steal_pct"] == pytest.approx(100 * 20 / 1020, abs=0.01)
    assert second["chr"]["MKT11"]["cpu_pct"] == pytest.approx(300 / 100 / 10 * 100)    # 3 s em 10 s = 30 %
    assert second["containers"]["KME1"]["mem_mb"] == 40.5


def test_sampler_without_project_samples_only_host(tmp_path):
    proc = make_proc(tmp_path / "proc", qemu={11: ("MKT11", "PID-A", 1, 1, 10)})
    row = ResourceSampler(None, proc=proc, docker_runner=lambda c: DOCKER_OUT).sample()
    assert row["chr"] == {} and row["containers"] == {} and "mem_used_mb" in row["host"]


def test_sampler_project_id_resolver_is_lazy(tmp_path):
    proc = make_proc(tmp_path / "proc", qemu={11: ("MKT11", "PID-A", 1, 1, 10)})
    box = {"pid": None}
    s = ResourceSampler(lambda: box["pid"], proc=proc, use_docker=False)
    assert s.sample()["chr"] == {}
    box["pid"] = "PID-A"
    assert list(s.sample()["chr"]) == ["MKT11"]


def test_summary_aggregates_and_percentiles(tmp_path):
    s = ResourceSampler("PID-A")
    for i, cpu in enumerate([10, 20, 30, 40, 90]):
        s.samples.append({"t": i, "ts": i, "host": {"cpu_pct": cpu, "mem_used_mb": 1000.0},
                          "chr": {"A": {"rss_mb": 300.0, "cpu_pct": 5.0}, "B": {"rss_mb": 200.0, "cpu_pct": 7.0}},
                          "containers": {"KME1": {"cpu_pct": 1.0, "mem_mb": 50.0}}})
    sm = s.summary()
    assert sm["host.cpu_pct"]["max"] == 90 and sm["host.cpu_pct"]["mean"] == 38.0 and sm["host.cpu_pct"]["p50"] == 30
    assert sm["host.cpu_pct"]["p95"] == 90 and sm["host.cpu_pct"]["n"] == 5
    assert sm["chr_total.rss_mb"]["mean"] == 500.0 and sm["chr_total.cpu_pct"]["mean"] == 12.0
    assert sm["containers_total.mem_mb"]["mean"] == 50.0 and "chr.A.rss_mb" in sm


def test_saturation_rule():
    ok = saturation({"host.cpu_pct": {"p95": 60.0}, "host.steal_pct": {"p95": 1.0}})
    assert ok["valid"] and ok["host_steal_p95"] == 1.0
    assert not saturation({"host.cpu_pct": {"p95": 85.0}})["valid"]
    none = saturation({})
    assert not none["valid"] and none["note"]


def test_environment_and_report(tmp_path, ring4_path, monkeypatch):
    monkeypatch.setattr(bench, "_cmd", lambda argv: "x")
    proc = make_proc(tmp_path / "proc")
    (proc / "cpuinfo").write_text("processor : 0\nmodel name : Test CPU @ 3GHz\n")
    env = bench.environment(ring4_path, {"lab": "x"}, proc=proc)
    assert env["cpu_model"] == "Test CPU @ 3GHz" and env["mem_total_mb"] == round(8_000_000 / 1024)
    assert len(env["topology_sha256"]) == 64 and env["lab"] == "x" and env["docker"] == "x"
    from qnetlab.workspace import Workspace
    ws = Workspace(tmp_path, "lab").ensure()
    path = bench.write_report(ws, "my label/1", {"a": 1})
    assert path.parent == ws.bench_dir and path.name.startswith("my_label_1-") and json.loads(path.read_text()) == {"a": 1}
