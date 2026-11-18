"""Testa o deployer com um cliente GNS3 falso (sem servidor GNS3)."""
import itertools

import pytest

from qnetlab.addressing import build_plan
from qnetlab.config import load_topology
from qnetlab.errors import Gns3Error
from qnetlab.infra.deployer import TopologyDeployer
from qnetlab.infra.gns3_client import Gns3Client
from qnetlab.workspace import Workspace


class FakeGns3:
    def __init__(self, existing=False):
        self._id = itertools.count(1)
        self.nodes, self.links, self.updates = {}, [], {}
        self.projects = [{"name": "qkd-ring4", "project_id": "old"}] if existing else []
        self.deleted, self.started = [], False

    def version(self): return {"version": "2.2.99"}
    nat_template = {"template_id": "tpl-nat", "name": "NAT", "template_type": "nat", "symbol": ":/symbols/cloud.svg"}

    def find_template(self, name): return {"template_id": "tpl-chr", "name": name}
    def find_nat_template(self): return self.nat_template
    def get_project_by_name(self, name): return next((p for p in self.projects if p["name"] == name), None)
    def create_project(self, name): return {"project_id": "pid", "name": name}
    def stop_all(self, pid): pass
    def close_project(self, pid): pass
    def delete_project(self, pid): self.deleted.append(pid); self.projects.clear()
    def start_all(self, pid): self.started = True
    def wait_started(self, pid): pass

    def _node(self, name, ntype, **extra):
        nid = f"n{next(self._id)}"
        node = {"node_id": nid, "name": name, "node_type": ntype, "console": 5000 + len(self.nodes), **extra}
        self.nodes[nid] = node
        return node

    def create_node_from_template(self, pid, tid, x, y):
        if tid == "tpl-nat":
            return self._node("NAT1", "nat", symbol=":/symbols/cloud.svg", from_template=tid)
        return self._node("tmp", "qemu")
    def create_node(self, pid, payload):
        extra = {k: v for k, v in payload.items() if k not in ("name", "node_type")}
        return self._node(payload["name"], payload["node_type"], **extra)

    def update_node(self, pid, nid, payload):
        self.nodes[nid].update({k: v for k, v in payload.items() if k != "properties"})
        self.nodes[nid]["properties"] = payload.get("properties", self.nodes[nid].get("properties", {}))
        return self.nodes[nid]

    def get_node(self, pid, nid): return {**self.nodes[nid], "console_host": "127.0.0.1"}
    def create_link(self, pid, a, b):
        self.links.append((a, b))
        return {"link_id": f"L{len(self.links)}", "nodes": [a, b]}


@pytest.fixture()
def setup(tmp_path, ring4_path):
    topo = load_topology(ring4_path)
    plan = build_plan(topo)
    ws = Workspace(tmp_path, topo.name).ensure()
    return topo, plan, ws


def _by_name(fake):
    return {n["name"]: n for n in fake.nodes.values()}


def test_deploy_counts_and_adapters(setup):
    topo, plan, ws = setup
    fake = FakeGns3()
    dep = TopologyDeployer(fake, topo, plan, ws)
    nodes = dep.deploy()
    # por site: 2 CHR + 2 NAT + KME + NODE = 6
    assert len(nodes) == 4 * 6 and fake.started
    assert len(fake.links) == len(plan.links) == 36
    by = _by_name(fake)
    assert by["MKT11"]["properties"]["adapters"] == 8           # ether1..ether8
    assert by["KME3"]["properties"]["adapters"] == 4 and by["NODE2"]["properties"]["adapters"] == 2
    assert by["KME3"]["properties"]["image"] == "qnetlab/qkd-ring4-kme3:latest"
    assert "KME_ID=kme-sim-3" in by["KME3"]["properties"]["environment"]
    assert "IFACES=eth0=10.0.3.2/30,eth1=10.0.3.6/30" in by["NODE1"]["properties"]["environment"]
    assert by["NAT4"]["node_type"] == "nat"
    st = ws.load_state()
    assert st["project_id"] == "pid" and st["nodes"]["MKT11"]["console"] is not None


def test_no_port_or_adapter_used_twice(setup):
    topo, plan, ws = setup
    fake = FakeGns3()
    TopologyDeployer(fake, topo, plan, ws).deploy()
    used = set()
    for a, b in fake.links:
        for ep in (a, b):
            key = (ep["node_id"], ep["adapter_number"], ep["port_number"])
            assert key not in used, f"terminal duplicado: {key}"
            used.add(key)
    by_id = {n["node_id"]: n["name"] for n in fake.nodes.values()}
    ends = {}
    for a, b in fake.links:
        for ep in (a, b):
            ends.setdefault(by_id[ep["node_id"]], []).append(ep["adapter_number"])
    # CHR: ether1,2,3,7,8 -> adapters 0,1,2,6,7 ; KME: eth0..eth3 ; NODE: eth0,eth1 ; NAT: 0
    assert sorted(ends["MKT11"]) == [0, 1, 2, 6, 7]
    assert sorted(ends["KME1"]) == [0, 1, 2, 3] and sorted(ends["NODE1"]) == [0, 1]
    assert ends["NAT1"] == [0]


def test_link_topology_matches_plan(setup):
    topo, plan, ws = setup
    fake = FakeGns3()
    TopologyDeployer(fake, topo, plan, ws).deploy()
    by_id = {n["node_id"]: n["name"] for n in fake.nodes.values()}
    pairs = {frozenset((by_id[a["node_id"]], by_id[b["node_id"]])) for a, b in fake.links}
    assert frozenset(("MKT12", "MKT21")) in pairs and frozenset(("MKT42", "MKT11")) in pairs   # anel (ether2)
    assert frozenset(("KME1", "KME2")) in pairs and frozenset(("KME4", "KME1")) in pairs       # anel KMA
    assert frozenset(("MKT11", "NAT1")) in pairs and frozenset(("NODE3", "MKT32")) in pairs


def test_existing_project_requires_recreate(setup):
    topo, plan, ws = setup
    fake = FakeGns3(existing=True)
    with pytest.raises(Gns3Error, match="--recreate"):
        TopologyDeployer(fake, topo, plan, ws).deploy()
    fake = FakeGns3(existing=True)
    TopologyDeployer(fake, topo, plan, ws).deploy(recreate=True)
    assert fake.deleted == ["old"]


def test_destroy_clears_state(setup):
    topo, plan, ws = setup
    fake = FakeGns3()
    dep = TopologyDeployer(fake, topo, plan, ws)
    dep.deploy()
    fake.projects.append({"name": "qkd-ring4", "project_id": "pid"})
    assert dep.destroy() is True and "nodes" not in ws.load_state()
    assert dep.destroy() is False


def test_real_client_wraps_http_errors(monkeypatch, setup):
    topo, *_ = setup
    client = Gns3Client(topo.gns3)

    class Resp:
        status_code, text, content = 409, "conflict", b"x"

    monkeypatch.setattr(client.session, "request", lambda *a, **k: Resp())
    with pytest.raises(Gns3Error) as ei:
        client.create_project("x")
    assert ei.value.status == 409


def test_only_mikrotik_docker_and_nat_nodes(setup):
    """Só QEMU (CHR), Docker e NAT: nada que dependa de Dynamips/Cisco (ethernet_switch etc.)."""
    topo, plan, ws = setup
    fake = FakeGns3()
    TopologyDeployer(fake, topo, plan, ws).deploy()
    types = {n["node_type"] for n in fake.nodes.values()}
    assert types == {"qemu", "docker", "nat"}, types
    assert not [n for n in fake.nodes.values() if n["name"].startswith(("SW", "MGMT"))]
    by_id = {n["node_id"]: n for n in fake.nodes.values()}
    for a, b in fake.links:
        for ep in (a, b):
            assert ep["port_number"] == 0


def test_no_nat_nodes_when_mgmt_none(tmp_path, ring4_path):
    import yaml
    from qnetlab.config import parse_topology
    raw = yaml.safe_load(ring4_path.read_text())
    raw["addressing"]["mgmt_attach"] = "none"
    topo = parse_topology(raw)
    fake = FakeGns3()
    TopologyDeployer(fake, topo, build_plan(topo), Workspace(tmp_path, topo.name).ensure()).deploy()
    assert not [n for n in fake.nodes.values() if n["node_type"] == "nat"]
    assert len(fake.links) == 28




def test_nat_nodes_are_template_based_cloud_nodes_named_like_the_lab(setup):
    """NAT1..NAT8 (como no ring4 do laboratório): nós NAT embutidos do GNS3, ícone de nuvem, porta nat0 -> ether8."""
    topo, plan, ws = setup
    fake = FakeGns3()
    TopologyDeployer(fake, topo, plan, ws).deploy()
    nats = {n["name"]: n for n in fake.nodes.values() if n["node_type"] == "nat"}
    assert sorted(nats) == [f"NAT{k}" for k in range(1, 9)]
    assert all(n["symbol"] == ":/symbols/cloud.svg" and n.get("from_template") == "tpl-nat" for n in nats.values())
    by_id = {n["node_id"]: n["name"] for n in fake.nodes.values()}
    pairs = {(by_id[a["node_id"]], by_id[b["node_id"]]) for a, b in fake.links}
    # NATk -> CHR k na ordem MKT11, MKT12, MKT21, MKT22, ...
    expected = {(f"NAT{k}", chr_) for k, chr_ in enumerate(
        [c.name for c in plan.chrs()], 1)}
    assert expected <= pairs or expected <= {(b, a) for a, b in pairs}
    assert [c.nat_name for c in plan.chrs()] == [f"NAT{k}" for k in range(1, 9)]


def test_nat_falls_back_to_cloud_symbol_without_template(setup):
    topo, plan, ws = setup
    fake = FakeGns3()
    fake.find_nat_template = lambda: None
    TopologyDeployer(fake, topo, plan, ws).deploy()
    nats = [n for n in fake.nodes.values() if n["node_type"] == "nat"]
    assert len(nats) == 8 and all(n["symbol"] == ":/symbols/cloud.svg" for n in nats)
