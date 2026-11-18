"""Injeção de falhas: índice de enlaces, filtros do GNS3 e ações de nós, com um cliente GNS3 falso."""
import json

import pytest

from qnetlab.addressing import build_plan
from qnetlab.config import load_topology
from qnetlab.errors import Gns3Error, QNetLabError
from qnetlab.faults import FaultInjector, LinkIndex, LinkRef, build_filters
from qnetlab.infra.deployer import TopologyDeployer
from qnetlab.workspace import Workspace

from test_deployer import FakeGns3


class FaultyGns3(FakeGns3):
    """FakeGns3 com a API de enlaces/nós usada por faults/capture."""

    def __init__(self):
        super().__init__()
        self.link_state, self.actions, self.status = {}, [], {}

    def create_link(self, pid, a, b):
        res = super().create_link(pid, a, b)
        self.link_state[res["link_id"]] = {"link_id": res["link_id"], "filters": {}, "suspend": False,
                                           "capturing": False, "nodes": [{"node_id": e["node_id"], "adapter_number": e["adapter_number"]} for e in (a, b)]}
        return res

    project_status = "opened"

    def list_projects(self):
        return [{"project_id": "pid", "name": "qkd-ring4", "status": self.project_status}]

    def list_links(self, pid): return list(self.link_state.values())

    def update_link(self, pid, lid, payload):
        self.link_state[lid].update(payload)
        return self.link_state[lid]

    def node_action(self, pid, nid, action):
        self.actions.append((nid, action))
        self.status[nid] = {"stop": "stopped", "start": "started", "suspend": "suspended", "reload": "started"}[action]

    def wait_node_status(self, pid, nid, status, timeout=0):
        if self.status.get(nid) != status:
            raise Gns3Error(f"nó {nid} não chegou a {status}")


@pytest.fixture()
def lab(tmp_path, ring4_path):
    topo = load_topology(ring4_path)
    plan = build_plan(topo)
    ws = Workspace(tmp_path, topo.name).ensure()
    fake = FaultyGns3()
    TopologyDeployer(fake, topo, plan, ws).deploy()
    st = ws.load_state()
    inj = FaultInjector(fake, st["project_id"], st["nodes"], LinkIndex.from_state(st), ws, clock=lambda: 1000.0)
    return fake, inj, ws, plan, st


def test_deploy_saves_link_ids_in_state(lab):
    fake, _inj, _ws, plan, st = lab
    assert len(st["links"]) == len(plan.links) == 36
    assert {l["id"] for l in st["links"]} == set(fake.link_state)
    assert {"id", "kind", "a", "a_if", "b", "b_if"} == set(st["links"][0])


def test_index_resolves_by_nodes_iface_and_kind(lab):
    _f, inj, *_ = lab
    idx = inj.links
    ring = idx.one("MKT12", "MKT21")                      # único enlace de anel entre os dois
    assert ring.kind == "ring" and {ring.a, ring.b} == {"MKT12", "MKT21"}
    assert idx.one("mkt21", "mkt12").id == ring.id        # ordem e caixa não importam
    assert idx.one("KME1", "KME2").kind == "kma"
    assert idx.one("MKT11:ether2").kind == "ring"         # a interface desambigua
    assert len(idx.find("MKT11")) == 5                    # kme, ring, sibling, node, nat


def test_index_ambiguous_and_missing_give_actionable_errors(lab):
    _f, inj, *_ = lab
    with pytest.raises(QNetLabError, match="casa com 5 enlaces"):
        inj.links.one("MKT11")
    with pytest.raises(QNetLabError, match="Nenhum enlace"):
        inj.links.one("MKT11", "MKT33")
    with pytest.raises(QNetLabError, match="Nenhum enlace"):
        inj.links.one("MKT12", "MKT21", kind="kma")


def test_index_rebuilt_from_gns3_for_old_deployments(lab):
    fake, _inj, _ws, plan, st = lab
    rebuilt = LinkIndex.from_gns3(fake.list_links("pid"), st["nodes"], plan)
    assert len(rebuilt.links) == 36
    assert {l.id for l in rebuilt.links} == {l["id"] for l in st["links"]}
    orig = LinkIndex.from_state(st)
    assert rebuilt.one("MKT12", "MKT21").id == orig.one("MKT12", "MKT21").id


def test_build_filters_maps_to_gns3_keys():
    assert build_filters(delay_ms=50, jitter_ms=5, loss_pct=2, corrupt_pct=1, drop_every=100) == {
        "delay": [50, 5], "packet_loss": [2], "corrupt": [1], "frequency_drop": [100]}
    assert build_filters(bpf=["ip proto 50", "udp port 500"]) == {"bpf": ["ip proto 50\nudp port 500"]}
    assert build_filters() == {}


@pytest.mark.parametrize("kw", [dict(delay_ms=40000), dict(loss_pct=101), dict(corrupt_pct=-1),
                                dict(drop_every=-2), dict(jitter_ms=5)])
def test_build_filters_rejects_out_of_range(kw):
    with pytest.raises(QNetLabError):
        build_filters(**kw)


def test_set_down_up_clear_link(lab):
    fake, inj, ws, *_ = lab
    ref = inj.links.one("MKT12", "MKT21")
    inj.set_link(ref, delay_ms=20, jitter_ms=2, loss_pct=1)
    assert fake.link_state[ref.id]["filters"] == {"delay": [20, 2], "packet_loss": [1]}
    inj.down(ref)
    assert fake.link_state[ref.id]["suspend"] is True
    inj.up(ref)
    assert fake.link_state[ref.id]["suspend"] is False
    inj.set_link(ref, loss_pct=5)                         # substitui: o atraso anterior some
    assert fake.link_state[ref.id]["filters"] == {"packet_loss": [5]}
    inj.clear(ref)
    assert fake.link_state[ref.id]["filters"] == {} and fake.link_state[ref.id]["suspend"] is False
    events = [json.loads(l) for l in (ws.logs_dir / "fault-events.jsonl").read_text().splitlines()]
    assert [e["action"] for e in events] == ["link-filters", "link-down", "link-up", "link-filters", "link-clear"]
    assert events[0]["ts"] == 1000.0 and events[0]["target"] == ref.label
    assert events[0]["params"]["filters"] == {"delay": [20, 2], "packet_loss": [1]}


def test_clear_all_restores_only_changed_links(lab):
    fake, inj, *_ = lab
    a, b = inj.links.one("MKT12", "MKT21"), inj.links.one("KME1", "KME2")
    inj.set_link(a, delay_ms=10)
    inj.down(b)
    assert inj.clear_all() == 2
    assert all(not l["filters"] and not l["suspend"] for l in fake.link_state.values())
    assert inj.clear_all() == 0


def test_link_state_reports_live_flags(lab):
    fake, inj, *_ = lab
    ref = inj.links.one("MKT12", "MKT21")
    inj.down(ref)
    fake.link_state[ref.id]["capturing"] = True
    row = next(r for r in inj.link_state() if r["id"] == ref.id)
    assert row["suspended"] and row["capturing"] and row["kind"] == "ring"


@pytest.mark.parametrize("action,gns3,state", [("stop", "stop", "stopped"), ("start", "start", "started"),
                                               ("restart", "reload", "started"), ("pause", "suspend", "suspended"),
                                               ("resume", "start", "started")])
def test_node_actions_map_to_gns3(lab, action, gns3, state):
    fake, inj, _ws, _plan, st = lab
    out = inj.node("KME1", action)
    assert fake.actions[-1] == (st["nodes"]["KME1"]["id"], gns3) and out["state"] == state


def test_node_unknown_or_invalid(lab):
    _f, inj, *_ = lab
    with pytest.raises(QNetLabError, match="não existe"):
        inj.node("KME9", "stop")
    with pytest.raises(QNetLabError, match="Ação inválida"):
        inj.node("KME1", "explode")


def test_node_wait_failure_becomes_qnetlab_error(lab):
    fake, inj, *_ = lab
    fake.wait_node_status = lambda *a, **k: (_ for _ in ()).throw(Gns3Error("timeout"))
    with pytest.raises(QNetLabError, match="timeout"):
        inj.node("KME1", "stop")
    assert inj.node("KME1", "stop", wait=False)["state"] == "requested"


def test_node_wait_failure_includes_gns3_and_docker_diagnosis(lab):
    """GNS3 responde 204 ao stop mesmo se o compute falhar: o erro deve dizer o que GNS3 e Docker enxergam."""
    fake, inj, *_ = lab
    fake.wait_node_status = lambda *a, **k: (_ for _ in ()).throw(Gns3Error("não chegou a stopped (atual: started)"))
    fake.get_node = lambda pid, nid: {"status": "started", "node_type": "docker", "compute_id": "local"}
    inj.docker = type("D", (), {"state": staticmethod(lambda n: f"Up 5 minutes ({n})")})()
    calls = []
    fake.compute_node_action = lambda pid, nid, typ, act: (calls.append((typ, act)), "Could not fix permissions for /etc/network: "
                                                           "[Errno 2] No such file or directory: 'docker'")[1]
    with pytest.raises(QNetLabError) as e:
        inj.node("KME1", "stop")
    msg = str(e.value)
    assert "GNS3: status=started tipo=docker" in msg and "Docker: Up 5 minutes (KME1)" in msg
    assert calls == [("docker", "stop")] and "Compute do GNS3 (stop direto): Could not fix permissions" in msg
    assert "CAUSA: o processo do servidor GNS3 não encontra o binário 'docker'" in msg
    del fake.compute_node_action
    fake.get_node = lambda pid, nid: {"status": "started", "node_type": "qemu"}     # CHR: sem consulta ao compute docker
    with pytest.raises(QNetLabError, match="log do servidor GNS3"):
        inj.node("MKT11", "stop")
    fake.get_node = lambda pid, nid: (_ for _ in ()).throw(Gns3Error("boom"))      # o diagnóstico não mascara o erro original
    inj.docker = type("D", (), {"state": staticmethod(lambda n: (_ for _ in ()).throw(RuntimeError("sem docker")))})()
    with pytest.raises(QNetLabError, match="não chegou a stopped") as e2:
        inj.node("KME1", "stop")
    assert "não foi possível consultar" in str(e2.value)


def test_fault_markers_are_posted_to_node_agents(lab):
    fake, _inj, ws, _plan, st = lab
    calls = []

    class Docker:
        def http(self, node, method, port, path, body=None, timeout=0):
            calls.append((node, method, port, path, body))
            if node == "NODE2":
                raise QNetLabError("parado")              # um NODE fora do ar não pode quebrar a injeção

    inj = FaultInjector(fake, "pid", st["nodes"], LinkIndex.from_state(st), ws, Docker(), ["NODE1", "NODE2"])
    inj.down(inj.links.one("MKT12", "MKT21"))
    assert [c[0] for c in calls] == ["NODE1", "NODE2"]
    node, method, port, path, body = calls[0]
    assert (method, port, path) == ("POST", 9100, "/event")
    assert body["type"] == "fault" and body["action"] == "link-down"


def test_restore_undoes_links_and_restarts_only_nodes_it_stopped(lab):
    fake, inj, *_ = lab
    inj.set_link(inj.links.one("MKT12", "MKT21"), loss_pct=5)
    inj.down(inj.links.one("KME1", "KME2"))
    inj.node("KME1", "stop")
    inj.node("KME2", "pause")
    inj.node("KME3", "stop")
    inj.node("KME3", "start")                              # já voltou sozinho: não deve ser tocado de novo
    n_before = len(fake.actions)
    assert inj.restore() == {"links": 2, "nodes": ["KME1", "KME2"]}
    assert [a for _n, a in fake.actions[n_before:]] == ["start", "start"]
    assert all(not l["filters"] and not l["suspend"] for l in fake.link_state.values())
    assert inj.restore() == {"links": 0, "nodes": []}      # idempotente


def test_docker_state_reads_stopped_containers_too():
    import subprocess
    from qnetlab.containers import DockerExec

    def runner(cmd, timeout):
        assert cmd[1:4] == ["ps", "-a", "--format"]
        out = "GNS3.KME1.pid|Exited (137) 3 seconds ago\nGNS3.KME2.pid|Up 2 hours\nGNS3.KME1.other|Up 1 hour\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    dx = DockerExec("pid", runner=runner)
    assert dx.state("KME1") == "Exited (137) 3 seconds ago" and dx.state("kme2") == "Up 2 hours"
    assert dx.state("KME9") == "inexistente"
