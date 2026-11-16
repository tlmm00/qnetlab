import pytest

from qnetlab.addressing import build_plan
from qnetlab.config import load_topology
from qnetlab.errors import PkiError
from qnetlab.pki import CertificateAuthority, Entity, entities_from_plan


@pytest.fixture()
def env(tmp_path, ring4_path):
    topo = load_topology(ring4_path)
    plan = build_plan(topo)
    ca = CertificateAuthority(tmp_path / "pki", topo.pki)
    return topo, plan, ca


def test_issue_all_san_and_chain(env):
    topo, plan, ca = env
    ents = entities_from_plan(topo, plan)
    issued = ca.issue_all(ents)
    assert len(issued) == 12                      # 4 sites x (KME + 2 SAEs de CHR)
    ca.verify_all(ents)
    sans = set(ca.san_of("kme-sim-2"))
    assert {"IP:10.0.0.9", "IP:10.0.0.13", "IP:127.0.0.1", "DNS:kme-sim-2"} <= sans
    chr11 = set(ca.san_of("sae-chr11"))
    assert {"IP:10.0.0.2", "IP:10.0.1.1", "IP:10.0.2.1", "IP:10.0.3.1", "IP:192.168.122.10"} <= chr11   # multi-IP


def test_idempotent_and_reissue_on_san_change(env):
    topo, plan, ca = env
    ents = entities_from_plan(topo, plan)
    ca.issue_all(ents)
    assert ca.issue_all(ents) == []
    changed = [Entity(e.cn, e.ips + ("10.99.99.99",), e.dns, e.server) if e.cn == "kme-sim-1" else e for e in ents]
    assert ca.issue_all(changed) == ["kme-sim-1"]
    assert "IP:10.99.99.99" in ca.san_of("kme-sim-1")


def test_key_permissions_and_eku(env):
    import os, stat
    topo, plan, ca = env
    ca.issue_all(entities_from_plan(topo, plan))
    assert stat.S_IMODE(os.stat(ca.key_path("kme-sim-1")).st_mode) == 0o600
    out = ca._run("x509", "-in", str(ca.cert_path("kme-sim-1")), "-noout", "-ext", "extendedKeyUsage")
    assert "TLS Web Server Authentication" in out
    out = ca._run("x509", "-in", str(ca.cert_path("sae-chr11")), "-noout", "-ext", "extendedKeyUsage")
    assert "TLS Web Server Authentication" not in out


def test_empty_san_rejected(env):
    _, _, ca = env
    ca.ensure_root()
    with pytest.raises(PkiError, match="SAN vazio"):
        ca.issue(Entity("X"))


def test_ec_keys(tmp_path, ring4_path):
    import dataclasses
    topo = load_topology(ring4_path)
    cfg = dataclasses.replace(topo.pki, key_type="ec-p384")
    ca = CertificateAuthority(tmp_path / "pki", cfg)
    ca.issue_all([Entity("kme-sim-1", ips=("10.0.0.1",), server=True)])
    ca.verify_chain("kme-sim-1")
