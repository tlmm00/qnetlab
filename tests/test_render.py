import re

import pytest
import yaml

from qnetlab.addressing import build_plan
from qnetlab.config import load_topology, parse_topology
from qnetlab.containers.env import kme_environment, node_environment
from qnetlab.dataplane.renderer import RouterConfigRenderer
from qnetlab.errors import ConfigError
from qnetlab.workspace import Workspace


@pytest.fixture()
def ctx(tmp_path, ring4_path):
    topo = load_topology(ring4_path)
    return topo, build_plan(topo), Workspace(tmp_path, topo.name).ensure()


def test_all_routers_render_without_leftovers(ctx):
    topo, plan, ws = ctx
    files = RouterConfigRenderer(topo, plan, ws).render_all()
    assert len(files) == 2 * len(plan.chrs())              # .rsc + .bootstrap.rsc por CHR
    for name, path in files.items():
        text = path.read_text()
        assert "{{" not in text and "{%" not in text, name
        assert oct(path.stat().st_mode & 0o777) == "0o600"        # contém segredos


def test_mkt11_script_matches_lab(tmp_path, ring6_path):
    topo = load_topology(ring6_path)
    plan = build_plan(topo)
    r = RouterConfigRenderer(topo, plan, Workspace(tmp_path, topo.name).ensure()).render_router("MKT11")
    for line in (
        "/ip address add address=10.0.0.2/30 interface=ether1",
        "/ip address add address=10.0.1.1/30 interface=ether2",
        "/ip address add address=10.0.2.1/30 interface=ether3",
        "/ip address add address=10.0.3.1/30 interface=ether7",
        "/ip route add dst-address=10.0.3.44/30 gateway=10.0.1.2",
        "/ip ipsec key qkd set enabled=yes address=10.0.0.1:8443 certificate=sae-chr11 kme-id=kme-sim-1 "
        "peer-sae-id=sae-chr62 key-size=256 cache-size=10",
        "/ip ipsec peer add name=to-MKT62 address=10.0.1.2/32 local-address=10.0.1.1 exchange-mode=ike2 profile=qkd-prof",
        "/ip ipsec policy add peer=to-MKT62 tunnel=yes src-address=10.0.3.0/30 dst-address=10.0.3.44/30 proposal=qkd-prop action=encrypt level=require",
        "router-id=10.255.0.11",
    ):
        assert line in r, line
    assert "dh-group=ecp384" in r and "ppk=qkd" in r and "pfs-group=modp2048" in r
    assert "interface=ether8" not in r                         # mgmt/NAT é do bootstrap, não do .rsc
    assert r.index("action=none") < r.index("action=encrypt")  # bypass antes do túnel


def test_one_qkd_key_object_per_router_and_pairing(ctx):
    topo, plan, ws = ctx
    rr = RouterConfigRenderer(topo, plan, ws)
    for c in plan.chrs():
        txt = rr.render_router(c.name)
        assert txt.count("/ip ipsec key qkd set") == 1
        peer = plan.tunnel_of(c.name).peer_of(c.name)
        assert f"peer-sae-id={plan.chr(peer).sae}" in txt


def test_psk_is_stable_across_renders_and_matches_peer(ctx):
    topo, plan, ws = ctx
    rr = RouterConfigRenderer(topo, plan, ws)
    psk = lambda t: re.search(r'secret="([0-9a-f]{64})"', t).group(1)
    t = plan.tunnels[0]
    a, b = psk(rr.render_router(t.a)), psk(rr.render_router(t.b))
    assert a == b == psk(rr.render_router(t.a))                # persistido em state.json
    assert psk(rr.render_router(plan.tunnels[1].a)) != a


def test_explicit_psk_is_used(tmp_path, ring4_path):
    raw = yaml.safe_load(ring4_path.read_text())
    raw["ipsec"]["psk"] = "lab-shared-psk"
    topo = parse_topology(raw)
    r = RouterConfigRenderer(topo, build_plan(topo), Workspace(tmp_path, topo.name).ensure()).render_router("MKT12")
    assert 'secret="lab-shared-psk"' in r


def test_no_ipsec_when_disabled(tmp_path, ring4_path):
    raw = yaml.safe_load(ring4_path.read_text())
    raw["ipsec"]["enabled"] = False
    topo = parse_topology(raw)
    r = RouterConfigRenderer(topo, build_plan(topo), Workspace(tmp_path, topo.name).ensure()).render_router("MKT11")
    assert "/ip ipsec key" not in r and "action=encrypt" not in r and "to-MKT" not in r
    assert "OSPFv2" in r and "/system logging add" in r


def test_unprotected_link_has_no_tunnel_on_either_side(tmp_path, ring4_path):
    raw = yaml.safe_load(ring4_path.read_text())
    raw["ipsec"]["unprotected_links"] = [1]            # MKT12 <-> MKT21
    topo = parse_topology(raw)
    plan = build_plan(topo)
    rr = RouterConfigRenderer(topo, plan, Workspace(tmp_path, topo.name).ensure())
    for n in ("MKT12", "MKT21"):
        t = rr.render_router(n)
        assert "action=encrypt" not in t and "/ip ipsec key qkd set" not in t
        assert "drop-clear" not in t                      # tráfego em claro permitido neste enlace
    assert "action=encrypt" in rr.render_router("MKT22")


def test_static_ppk_variant(tmp_path, ring4_path):
    raw = yaml.safe_load(ring4_path.read_text())
    raw["ipsec"].update(ppk_mode="static")
    topo = parse_topology(raw)
    r = RouterConfigRenderer(topo, build_plan(topo), Workspace(tmp_path, topo.name).ensure()).render_router("MKT12")
    assert "/ip ipsec key qkd set" not in r and "ppk=required" in r


def test_bootstrap_uses_nat_interface(ctx):
    topo, plan, ws = ctx
    b = RouterConfigRenderer(topo, plan, ws).render_bootstrap("MKT22")
    assert "192.168.122.40/24" in b and "ether8" in b


def test_unsafe_password_rejected(tmp_path, ring4_path):
    raw = yaml.safe_load(ring4_path.read_text())
    raw["router"]["admin_password"] = 'a"b'
    topo = parse_topology(raw)
    ws = Workspace(tmp_path, topo.name).ensure()
    with pytest.raises(ConfigError, match="aspas"):
        RouterConfigRenderer(topo, build_plan(topo), ws).render_bootstrap("MKT11")


def test_container_environment(ctx):
    topo, plan, ws = ctx
    k = kme_environment(topo, plan, ws, 1)
    assert k["KME_ID"] == "kme-sim-1" and k["ROLE"] == "kme" and k["KME_CONFIG"] == "/etc/qnetlab/kme_config.yaml"
    assert k["IFACES"] == "eth0=172.16.100.1/30,eth1=172.16.100.14/30,eth2=10.0.0.1/30,eth3=10.0.0.5/30"
    n = node_environment(topo, plan, ws, 1)
    assert n["IFACES"] == "eth0=10.0.3.2/30,eth1=10.0.3.6/30"
    assert n["ROUTES"] == "10.0.3.28/30 via 10.0.3.1 dev eth0;10.0.3.8/30 via 10.0.3.5 dev eth1"
    assert n["PEERS"] == "NODE4=10.0.3.30=1=,NODE2=10.0.3.10=1="


def test_peers_flag_unprotected(tmp_path, ring4_path):
    raw = yaml.safe_load(ring4_path.read_text())
    raw["ipsec"]["unprotected_links"] = [1]             # NODE1 <-> NODE2 em claro
    topo = parse_topology(raw)
    n = node_environment(topo, build_plan(topo), Workspace(tmp_path, topo.name).ensure(), 1)
    assert "NODE2=10.0.3.10=0=" in n["PEERS"] and "NODE4=10.0.3.30=1=" in n["PEERS"]


def test_kme_config_matches_user_schema(ctx):
    from qnetlab.containers.kme_config import render_kme_config
    topo, plan, ws = ctx
    c1 = yaml.safe_load(render_kme_config(topo, plan, ws, 1))
    c2 = yaml.safe_load(render_kme_config(topo, plan, ws, 2))
    c3 = yaml.safe_load(render_kme_config(topo, plan, ws, 3))
    assert c1["local_kme_id"] == "kme-sim-1" and c1["use_mTLS"] is True and c1["storage"]["type"] == "sqlite"
    assert c1["listen"] == "0.0.0.0:8443"
    assert set(c1["peer_sync"]["peers"]) == {"kme-sim-2", "kme-sim-4"}          # anel KMA
    assert c1["peer_sync"]["peers"]["kme-sim-2"]["base_url"] == "https://172.16.100.2:8443"
    assert c1["peer_sync"]["peers"]["kme-sim-4"]["base_url"] == "https://172.16.100.13:8443"
    # mesma seed nas duas pontas de cada enlace, seeds distintas entre enlaces
    assert c1["peer_sync"]["peers"]["kme-sim-2"]["seed"] == c2["peer_sync"]["peers"]["kme-sim-1"]["seed"]
    assert c1["peer_sync"]["peers"]["kme-sim-2"]["seed"] != c1["peer_sync"]["peers"]["kme-sim-4"]["seed"]
    assert len(c1["peer_sync"]["peers"]["kme-sim-2"]["seed"]) == 64
    assert "kme-sim-1" not in c3["peer_sync"]["peers"]
    assert c1["kme_topology"]["kme-sim-3"]["connected_saes"] == ["sae-chr31", "sae-chr32"]


def test_compose_renders_kme_only(tmp_path, ring4_path):
    from qnetlab.containers import ImageBuilder
    from qnetlab.pki import CertificateAuthority
    topo = load_topology(ring4_path)
    plan = build_plan(topo)
    ws = Workspace(tmp_path, topo.name).ensure()
    p = ImageBuilder(topo, plan, ws, CertificateAuthority(ws.pki_dir, topo.pki)).render_compose()
    doc = yaml.safe_load(p.read_text())
    assert sorted(doc["services"]) == [f"kme-sim-{i}" for i in range(1, 5)]
    assert doc["services"]["kme-sim-1"]["environment"]["SKIP_NET"] == "1"


def test_ipsec_commands_match_the_lab_syntax(tmp_path, ring6_path):
    """profile/proposal/peer/identity sem 'comment=' (o RouterOS 7.22 rejeitou 'comment' no profile) e política como no PDF."""
    topo = load_topology(ring6_path)
    r = RouterConfigRenderer(topo, build_plan(topo), Workspace(tmp_path, topo.name).ensure()).render_router("MKT11")
    for menu in ("profile", "proposal", "peer", "identity"):
        lines = [l for l in r.splitlines() if l.startswith(f"/ip ipsec {menu} add")]
        assert len(lines) == 1 and "comment" not in lines[0], lines
    assert "sa-src-address" not in r
    assert '/ip ipsec profile add name=qkd-prof dh-group=ecp384 enc-algorithm=aes-256 hash-algorithm=sha256 lifetime=1h ppk=qkd\n' in r
    assert '/ip ipsec profile remove [find where name="qkd-prof"]' in r
    assert r.index("ipsec peer remove") < r.index("ipsec profile remove")       # peer sai antes do profile


def test_proposal_lifetime_is_one_minute_on_every_router(ctx):
    topo, plan, ws = ctx
    rr = RouterConfigRenderer(topo, plan, ws)
    for c in plan.chrs():
        lines = [l for l in rr.render_router(c.name).splitlines() if l.startswith("/ip ipsec proposal add")]
        assert len(lines) == 1 and lines[0].endswith("pfs-group=modp2048 lifetime=1m"), lines
        assert "lifetime=1h" in rr.render_router(c.name)                  # IKE SA (profile) continua 1h


def test_proposal_lifetime_is_configurable_and_validated(tmp_path, ring4_path):
    raw = yaml.safe_load(ring4_path.read_text())
    raw["ipsec"]["proposal_lifetime"] = "30s"
    topo = parse_topology(raw)
    r = RouterConfigRenderer(topo, build_plan(topo), Workspace(tmp_path, topo.name).ensure()).render_router("MKT11")
    assert "pfs-group=modp2048 lifetime=30s" in r
    raw["ipsec"]["proposal_lifetime"] = "soon"
    with pytest.raises(ConfigError):
        parse_topology(raw)


def test_neighbor_discovery_is_disabled_by_default_and_can_be_enabled(tmp_path, ring4_path):
    """Achado do SEG-01: MNDP/LLDP/CDP vazam identidade e versão em claro; por padrão o CHR não os anuncia."""
    topo = load_topology(ring4_path)
    assert topo.router.neighbor_discovery is False
    ws = Workspace(tmp_path, topo.name).ensure()
    text = RouterConfigRenderer(topo, build_plan(topo), ws).render_router("MKT11")
    assert "/ip neighbor discovery-settings set discover-interface-list=none" in text
    assert "discover-interface-list=!dynamic" not in text

    raw = yaml.safe_load(ring4_path.read_text())
    raw["router"]["neighbor_discovery"] = True
    on = parse_topology(raw)
    text = RouterConfigRenderer(on, build_plan(on), Workspace(tmp_path / "on", on.name).ensure()).render_router("MKT11")
    assert "discover-interface-list=!dynamic" in text and "discover-interface-list=none" not in text
