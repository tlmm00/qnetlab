"""Fachada Lab e CLI para os recursos de experimento (faults/capture/bench), sem GNS3 real."""
import json

import pytest

from qnetlab import cli
from qnetlab.errors import QNetLabError
from qnetlab.lab import Lab

from test_capture import _eth_ip, write_pcap
from test_faults import FaultyGns3


@pytest.fixture()
def lab(tmp_path, ring4_path, monkeypatch):
    lab = Lab(ring4_path, tmp_path)
    fake = FaultyGns3()
    lab._client = fake
    return lab, fake


def _deploy(lab, fake):
    from qnetlab.infra.deployer import TopologyDeployer
    TopologyDeployer(fake, lab.topo, lab.plan, lab.ws).deploy()


def test_up_times_each_stage_and_writes_report(lab, monkeypatch):
    lab, _ = lab
    seen = []
    for name in ("pki", "deploy", "bootstrap", "upgrade", "verify"):
        monkeypatch.setattr(Lab, name, lambda self, *a, _n=name, **k: seen.append(_n) or ({"_ok": True} if _n == "verify" else None))
    monkeypatch.setattr(Lab, "configure", lambda self, upgrade=True: seen.append(f"configure(upgrade={upgrade})"))
    monkeypatch.setattr("qnetlab.lab.ImageBuilder", lambda *a, **k: type("B", (), {"build_all": lambda s: seen.append("images")})())
    rep = lab.up(recreate=True)
    assert rep == {"_ok": True}
    assert seen == ["pki", "images", "deploy", "bootstrap", "upgrade", "configure(upgrade=False)", "verify"]
    out = json.loads(next(lab.ws.bench_dir.glob("up-*.json")).read_text())
    assert [s["stage"] for s in out["stages"]] == ["pki", "images", "deploy", "bootstrap", "upgrade", "configure", "verify"]
    assert out["verify_ok"] is True and all(s["ok"] for s in out["stages"])


def test_up_report_is_written_even_when_a_stage_fails(lab, monkeypatch):
    lab, _ = lab
    monkeypatch.setattr(Lab, "pki", lambda self, force=False: None)
    monkeypatch.setattr(Lab, "deploy", lambda self, recreate=False: (_ for _ in ()).throw(QNetLabError("GNS3 fora do ar")))
    with pytest.raises(QNetLabError):
        lab.up(build_images=False)
    out = json.loads(next(lab.ws.bench_dir.glob("up-*.json")).read_text())
    assert [(s["stage"], s["ok"]) for s in out["stages"]] == [("pki", True), ("deploy", False)] and out["verify_ok"] is None


def test_faults_requires_deployed_lab(lab):
    lab, _ = lab
    with pytest.raises(QNetLabError, match="não implantado"):
        lab.faults()


def test_links_rebuilt_from_gns3_and_cached_when_state_lacks_ids(lab):
    lab, fake = lab
    _deploy(lab, fake)
    st = lab.ws.load_state()
    st.pop("links")                                         # laboratório implantado antes de existirem os IDs
    lab.ws.save_state(st)
    assert len(lab.links().links) == 36
    assert len(lab.ws.load_state()["links"]) == 36            # reconstruído e gravado
    fake.link_state.clear()                                   # agora vem do estado, sem consultar o GNS3
    assert lab.links().one("MKT12", "MKT21").kind == "ring"


def test_faults_marks_nodes_only_with_docker_access(lab, monkeypatch):
    lab, fake = lab
    _deploy(lab, fake)
    assert lab.faults(mark_nodes=False).docker is None
    inj = lab.faults()
    assert inj.docker is not None and inj.marker_nodes == ["NODE1", "NODE2", "NODE3", "NODE4"] and inj.marker_port == 9100


def test_cli_fault_link_and_clear(lab, monkeypatch, capsys, ring4_path):
    lab, fake = lab
    _deploy(lab, fake)
    monkeypatch.setattr(cli, "Lab", lambda *a, **k: lab)
    monkeypatch.setattr(Lab, "docker", lambda self, required=False: None)
    base = ["-t", str(ring4_path)]
    assert cli.main(["fault", "link", "MKT12", "MKT21", "--delay", "30", "--jitter", "3", "--loss", "2", *base]) == 0
    assert json.loads(capsys.readouterr().out) == {"delay": [30, 3], "packet_loss": [2]}
    assert cli.main(["fault", "down", "KME1", "KME2", *base]) == 0
    assert cli.main(["fault", "list", *base]) == 0
    out = capsys.readouterr().out
    assert "SUSPENSO" in out and '"delay":[30,3]' in out
    assert cli.main(["fault", "clear", "--all", *base]) == 0
    assert "2 enlace(s) restaurado(s)" in capsys.readouterr().out
    assert cli.main(["fault", "node", "KME1", "stop", *base]) == 0
    assert json.loads(capsys.readouterr().out) == {"node": "KME1", "action": "stop", "state": "stopped"}


def test_cli_fault_errors_are_clean(lab, monkeypatch, capsys, ring4_path):
    lab, fake = lab
    _deploy(lab, fake)
    monkeypatch.setattr(cli, "Lab", lambda *a, **k: lab)
    monkeypatch.setattr(Lab, "docker", lambda self, required=False: None)
    base = ["-t", str(ring4_path)]
    assert cli.main(["fault", "link", "MKT12", "MKT21", *base]) == 1                  # sem nenhum filtro
    assert cli.main(["fault", "down", "MKT11", *base]) == 1                           # ambíguo
    assert cli.main(["fault", "node", "KME9", "stop", *base]) == 1                    # nó inexistente


def test_cli_capture_summary_is_offline(tmp_path, capsys):
    pcap = write_pcap(tmp_path / "c.pcap", [_eth_ip(50, b"\0\0\0\1" + b"SECRET")] * 3)
    assert cli.main(["capture", "summary", str(pcap), "--needle", "SECRET"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["esp_packets"] == 3 and out["needle_hits"] == 3 and out["cleartext_data_packets"] == 0


def test_cli_parser_exposes_new_commands():
    p = cli.build_parser()
    for argv in (["fault", "list", "-t", "x"], ["fault", "node", "KME1", "pause", "-t", "x"],
                 ["capture", "run", "MKT12", "MKT21", "--duration", "5", "-t", "x"],
                 ["bench", "sample", "--duration", "3", "-t", "x"], ["bench", "up", "-t", "x"]):
        p.parse_args(argv)
    with pytest.raises(SystemExit):
        p.parse_args(["fault", "node", "KME1", "explode", "-t", "x"])


def test_bench_sample_writes_report_with_saturation(lab, monkeypatch, capsys, ring4_path, tmp_path):
    lab, _ = lab
    monkeypatch.setattr(cli, "Lab", lambda *a, **k: lab)
    monkeypatch.setattr("qnetlab.bench._cmd", lambda argv: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["bench", "sample", "--duration", "0", "--interval", "0.01", "--no-docker", "--label", "idle",
                     "-t", str(ring4_path)]) == 0
    brief = json.loads(capsys.readouterr().out)
    rep = json.loads(open(brief["report"]).read())
    assert rep["label"] == "idle" and rep["environment"]["lab"] == lab.topo.name
    assert "saturation" in rep and "resources" in rep and rep["samples"]


def test_check_deployed_explains_stale_state_and_closed_projects(lab):
    lab, fake = lab
    _deploy(lab, fake)
    assert lab.check_deployed()["project_id"] == "pid"
    fake.list_projects = lambda: [{"project_id": "outro", "name": lab.topo.project_name, "status": "opened"}]
    with pytest.raises(QNetLabError, match=r"não conhece o projeto pid.*outro ID \(outro\).*desatualizado"):
        lab.faults()
    fake.list_projects = lambda: []
    with pytest.raises(QNetLabError, match="make up"):
        lab.capture()
    fake.list_projects = lambda: [{"project_id": "pid", "name": "qkd-ring4", "status": "closed"}]
    with pytest.raises(QNetLabError, match="closed"):
        lab.faults()


def _run_lab_suite(env_extra, *args):
    import os, subprocess, sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "QNETLAB_TOPOLOGY": str(root / "does-not-exist.yaml"), **env_extra}
    env.pop("QNETLAB_DISRUPTIVE", None) if "QNETLAB_DISRUPTIVE" not in env_extra else None
    out = subprocess.run([sys.executable, "-m", "pytest", "tests/lab", "-q", "-rs", "-p", "no:cacheprovider", *args],
                         cwd=root, env=env, capture_output=True, text=True, timeout=120)
    return out.stdout + out.stderr


def test_disruptive_lab_tests_never_run_unless_explicitly_requested():
    """`make test` com QNETLAB_TOPOLOGY definido não pode derrubar enlaces/nós do laboratório do usuário."""
    default = _run_lab_suite({})
    assert "disruptivo (derruba enlaces/nós)" in default
    assert default.count("disruptivo (derruba") == 2                     # link down/up e KME1 stop/start
    by_marker = _run_lab_suite({}, "-m", "disruptive")
    assert "disruptivo (derruba" not in by_marker
    by_env = _run_lab_suite({"QNETLAB_DISRUPTIVE": "1"})
    assert "disruptivo (derruba" not in by_env
    excluded = _run_lab_suite({}, "-m", "lab and not disruptive")
    assert "disruptivo (derruba" not in excluded                          # deselecionados, nem rodam
