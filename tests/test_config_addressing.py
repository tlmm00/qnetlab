import copy
import ipaddress

import pytest
import yaml

from qnetlab.addressing import build_plan
from qnetlab.config import load_topology, parse_topology
from qnetlab.errors import AddressingError, ConfigError


def _raw(path):
    return yaml.safe_load(path.read_text())


@pytest.mark.parametrize("fixture,n", [("ring4_path", 4), ("ring6_path", 6)])
def test_rings_load_and_plan(request, fixture, n):
    topo = load_topology(request.getfixturevalue(fixture))
    plan = build_plan(topo)
    assert len(topo.sites) == n and len(plan.chrs()) == 2 * n
    assert len(plan.tunnels) == n and all(t.protected for t in plan.tunnels)
    # por site: 2 kme-access + 2 node + 1 sibling + NAT x2; entre sites: 1 ring + 1 kma
    kinds = {}
    for l in plan.links:
        kinds[l.kind] = kinds.get(l.kind, 0) + 1
    assert kinds == {"kme-access": 2 * n, "node": 2 * n, "sibling": n, "ring": n, "kma": n, "nat": 2 * n}
    # sem NAT duplicado, cada /30 é único
    nets = [str(l.network) for l in plan.links if l.network]
    assert len(nets) == len(set(nets)) and all(l.network.prefixlen == 30 for l in plan.links if l.network)
    mg = [c.mgmt_ip for c in plan.chrs()]
    assert len(set(mg)) == 2 * n and all(ip in plan.mgmt_network for ip in mg)
    # cada CHR tem exatamente 1 túnel (um objeto 'key qkd' por roteador)
    for c in plan.chrs():
        assert plan.tunnel_of(c.name) is not None


def test_ring6_matches_mitacs_table(ring6_path):
    plan = build_plan(load_topology(ring6_path))
    c = plan.chr("MKT11")
    assert {k: i.cidr for k, i in c.ifaces.items()} == {
        "kme": "10.0.0.2/30", "ring": "10.0.1.1/30", "sibling": "10.0.2.1/30", "node": "10.0.3.1/30"}
    assert str(c.mgmt_ip) == "192.168.122.10"
    assert str(plan.chr("MKT62").ifaces["ring"].ip) == "10.0.1.2"
    assert str(plan.chr("MKT62").mgmt_ip) == "192.168.122.120"
    node1 = plan.sites[1].node
    assert node1.routes == (("10.0.3.44/30", "10.0.3.1", "eth0"), ("10.0.3.8/30", "10.0.3.5", "eth1"))
    kme1 = plan.sites[1].kme
    assert str(kme1.ifaces["chr1"].ip) == "10.0.0.1"


def test_link_endpoints_use_distinct_adapters(ring6_path):
    plan = build_plan(load_topology(ring6_path))
    seen = set()
    for l in plan.links:
        for end in ((l.a, l.a_if), (l.b, l.b_if)):
            assert end not in seen, end
            seen.add(end)


def test_unprotected_links_flag(ring4_path):
    raw = _raw(ring4_path)
    raw["ipsec"]["unprotected_links"] = [2]
    plan = build_plan(parse_topology(raw))
    assert [t.protected for t in plan.tunnels] == [True, False, True, True]


def test_ipsec_disabled_means_no_protected_tunnels(ring4_path):
    raw = _raw(ring4_path)
    raw["ipsec"]["enabled"] = False
    assert not any(t.protected for t in build_plan(parse_topology(raw)).tunnels)


@pytest.mark.parametrize("size", [3, 8, 12])
def test_expansion_to_other_sizes(ring4_path, size):
    raw = _raw(ring4_path)
    raw["ring"]["size"] = size
    plan = build_plan(parse_topology(raw))
    assert len(plan.sites) == size and len(plan.tunnels) == size


@pytest.mark.parametrize("mutate,msg", [
    (lambda r: r.update(bogus=1), "desconhecida"),
    (lambda r: r["ipsec"].update(ppk_mode="magic"), "ppk_mode|schema|Violação"),
    (lambda r: r["ring"].update(size=2), "ring.size|schema|Violação"),
    (lambda r: r["ring"].update(size=25), "ring.size|schema|Violação"),
    (lambda r: r["addressing"].update(mgmt_subnet="10.0.0.0/8"), "/24|sobrepostos"),
    (lambda r: r["addressing"].update(ring_pool="10.0.0.0/24"), "sobrepostos"),
    (lambda r: r["router"].update(if_ring="ether1"), "distinta"),
    (lambda r: r["ipsec"].update(unprotected_links=[9]), "fora de"),
    (lambda r: r.pop("ring"), "ring|schema|Violação"),
    (lambda r: r.update(sites=[1, 2]), "schema|Violação|desconhecida"),
])
def test_invalid_configs_are_rejected(ring4_path, mutate, msg):
    raw = copy.deepcopy(_raw(ring4_path))
    mutate(raw)
    with pytest.raises((ConfigError, AddressingError), match=msg):
        build_plan(parse_topology(raw))


def test_env_interpolation(ring4_path, monkeypatch):
    monkeypatch.setenv("QNETLAB_ADMIN_PASSWORD", "s3cret-pass")
    assert load_topology(ring4_path).router.admin_password == "s3cret-pass"
    monkeypatch.delenv("QNETLAB_ADMIN_PASSWORD")
    assert load_topology(ring4_path).router.admin_password == "qnetlab-lab"


def test_required_env_missing(ring4_path, monkeypatch):
    raw = _raw(ring4_path)
    raw["gns3"]["password"] = "${DEFINITELY_NOT_SET_XYZ}"
    monkeypatch.delenv("DEFINITELY_NOT_SET_XYZ", raising=False)
    with pytest.raises(ConfigError, match="DEFINITELY_NOT_SET_XYZ"):
        parse_topology(raw)


def test_pool_exhaustion(ring4_path):
    raw = _raw(ring4_path)
    raw["ring"]["size"] = 24
    raw["addressing"].update(ring_pool="10.0.1.0/26", mgmt_step=5)
    with pytest.raises(AddressingError, match="sem espaço"):
        build_plan(parse_topology(raw))


def test_mgmt_capacity_overflow(ring4_path):
    raw = _raw(ring4_path)
    raw["ring"]["size"] = 24
    with pytest.raises(AddressingError, match="gerenciamento"):
        build_plan(parse_topology(raw))
    raw["addressing"]["mgmt_step"] = 5
    assert len(build_plan(parse_topology(raw)).chrs()) == 48


def test_mgmt_none_has_no_nat_links(ring4_path):
    raw = _raw(ring4_path)
    raw["addressing"]["mgmt_attach"] = "none"
    plan = build_plan(parse_topology(raw))
    assert not [l for l in plan.links if l.kind == "nat"]
    assert all(c.mgmt_ip is None for c in plan.chrs())


def test_control_topology_has_exactly_one_unprotected_ring_link():
    """topologies/ring4-control.yaml (controle positivo do SEG-01): só o enlace 2 em claro; o 1 continua protegido."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    topo = load_topology(root / "topologies" / "ring4-control.yaml")
    plan = build_plan(topo)
    assert topo.name == "qkd-ring4-control"                       # não colide com o build/projeto do ring4
    assert [t.protected for t in plan.tunnels] == [True, False, True, True]
    assert plan.tunnels[1].name == "MKT22-MKT31"
