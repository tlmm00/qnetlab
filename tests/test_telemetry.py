import json
import random
import tarfile

import pytest

from qnetlab.addressing import build_plan
from qnetlab.config import load_topology
from qnetlab.telemetry.collector import TelemetryCollector
from qnetlab.telemetry.plot_metrics import consolidate, percentile, read_events
from qnetlab.errors import TelemetryError
from qnetlab.workspace import Workspace


def _write_metrics(d, loss_protected=0.1):
    random.seed(7)
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for seq in range(1, 201):
        for peer, prot, base in (("NODE2", True, 1.4), ("NODE4", False, 0.6)):
            lost = prot and random.random() < loss_protected
            lines.append({"type": "probe", "node": "NODE1", "peer": peer, "protected": prot, "seq": seq,
                          "t": 1000 + seq * 0.1, "rtt_ms": None if lost else base + random.random() * 0.2})
    for i in range(5):
        lines.append({"type": "key_request", "node": "NODE1", "peer": "NODE2", "ok": i != 4, "t": 1000 + i,
                      "latency_ms": 12.0 + i})
    for r in range(1, 4):
        lines.append({"type": "accuracy", "node": "NODE1", "round": r, "value": 0.5 + r / 10, "t": 1000 + r})
    (d / "NODE1.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot-json\n")


def test_percentile():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([5], 99) == 5
    assert percentile([], 50) != percentile([], 50)       # NaN


def test_consolidate_summary_and_plots(tmp_path):
    _write_metrics(tmp_path / "m")
    s = consolidate(tmp_path / "m", tmp_path / "plots")
    prot = next(r for r in s["probes"] if r["protected"])
    clear = next(r for r in s["probes"] if not r["protected"])
    assert prot["sent"] == 200 and 0 < prot["loss_pct"] < 30 and clear["loss_pct"] == 0
    assert prot["rtt_p50_ms"] > clear["rtt_p50_ms"]
    k = s["keys"][0]
    assert k["requests"] == 5 and k["ok"] == 4 and k["failure_pct"] == 20
    assert s["accuracy"][0]["final"] == pytest.approx(0.8)
    assert (tmp_path / "plots" / "summary.json").exists()
    assert (tmp_path / "plots" / "summary_probes.csv").exists()
    assert {"rtt_time.png", "rtt_cdf.png", "loss.png", "key_latency.png", "accuracy.png"} <= set(s["plots"])


def test_read_events_empty_dir(tmp_path):
    with pytest.raises(TelemetryError):
        read_events(tmp_path)


def test_package_masks_secrets(tmp_path, ring4_path):
    topo = load_topology(ring4_path)
    plan = build_plan(topo)
    ws = Workspace(tmp_path, topo.name).ensure()
    _write_metrics(ws.metrics_dir)
    (ws.rsc_dir / "MKT11.rsc").write_text('/ip ipsec identity add secret="TOPSECRET" ppk-secret="ALSO"\n')
    (ws.logs_dir / "MKT11-ipsec.log").write_text("ipsec,debug qkd: add to cache key abc\n")
    res = TelemetryCollector(topo, plan, ws).run(collect_live=False)
    with tarfile.open(res["package"]) as tar:
        names = tar.getnames()
        assert "manifest.json" in names and "metrics/NODE1.jsonl" in names and "plots/summary.json" in names
        rsc = tar.extractfile("rsc/MKT11.rsc").read().decode()
        assert "TOPSECRET" not in rsc and "ALSO" not in rsc and '"***"' in rsc
        manifest = json.loads(tar.extractfile("manifest.json").read())
        assert "logs/MKT11-ipsec.log" in manifest["files"]


# ---------------------------------------------------------------- docker exec (runner falso)
import subprocess as _sp


class FakeDocker:
    """Simula `docker ps/exec/cp` para o projeto 'pid'."""
    def __init__(self, plan, kme_rows="[]"):
        self.calls = []
        self.names = [f"GNS3.{n}.pid" for sp in plan.sites.values() for n in (sp.node.name, sp.kme.name)]
        self.kme_rows = kme_rows

    def __call__(self, cmd, timeout):
        self.calls.append(list(cmd))
        args = cmd[1:]
        out = ""
        if args[0] == "ps":
            out = "\n".join(self.names)
        elif args[0] == "exec":
            c = args[1:]
            if "curl" in c and any(x.endswith("/control/status") for x in c):
                out = json.dumps({"running": False})
            elif "curl" in c and any(x.endswith("/control/start") for x in c):
                out = json.dumps({"status": "started", "peers": ["NODE2"]})
            elif "curl" in c and any(x.endswith("/metrics.jsonl") for x in c):
                out = json.dumps({"type": "probe", "node": "x", "peer": "y", "protected": True, "seq": 1,
                                  "t": 1.0, "rtt_ms": 1.0}) + "\n"
            elif "python3" in c:
                out = self.kme_rows
        elif args[0] == "cp":
            from pathlib import Path
            Path(args[2]).write_text("kme log\n")
        return _sp.CompletedProcess(cmd, 0, out, "")


def test_docker_exec_resolves_gns3_container_names(ring4_path):
    from qnetlab.containers import DockerExec
    plan = build_plan(load_topology(ring4_path))
    dx = DockerExec("pid", runner=FakeDocker(plan))
    assert dx.container("NODE3") == "GNS3.NODE3.pid"
    with pytest.raises(TelemetryError, match="não encontrado"):
        dx.container("NODE9")


def test_loadrunner_drives_all_nodes_via_docker(ring4_path):
    from qnetlab.containers import DockerExec
    from qnetlab.telemetry.loadgen import LoadRunner
    import dataclasses
    topo = load_topology(ring4_path)
    topo = dataclasses.replace(topo, load=dataclasses.replace(topo.load, warmup_s=0, duration_s=0))
    plan = build_plan(topo)
    fake = FakeDocker(plan)
    started = LoadRunner(topo, plan, DockerExec("pid", runner=fake)).run()
    assert sorted(started) == ["NODE1", "NODE2", "NODE3", "NODE4"]
    starts = [c for c in fake.calls if any(a.endswith("/control/start") for a in c)]
    assert len(starts) == 4 and all("127.0.0.1:9100" in " ".join(c) for c in starts)


def test_collector_gathers_nodes_and_kmes_via_docker(tmp_path, ring4_path):
    from qnetlab.containers import DockerExec
    topo = load_topology(ring4_path)
    plan = build_plan(topo)
    ws = Workspace(tmp_path, topo.name).ensure()
    fake = FakeDocker(plan, kme_rows='[["kme-sim-2","synchronized",100],["kme-sim-2","used",3]]')
    col = TelemetryCollector(topo, plan, ws, DockerExec("pid", runner=fake))
    col.collect_nodes()
    col.collect_kmes()
    assert (ws.metrics_dir / "NODE1.jsonl").read_text().strip().startswith("{")
    assert (ws.logs_dir / "KME1.log").read_text() == "kme log\n"
    pools = json.loads((ws.logs_dir / "KME1-keys.json").read_text())
    assert pools["kme_id"] == "kme-sim-1" and pools["pools"]["kme-sim-2"] == {"synchronized": 100, "used": 3}
    assert not col.warnings
