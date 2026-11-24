"""Critérios do verify (qnetlab.dataplane.verify): função pura, snapshot no formato de logs/MKT*-state.json."""
import copy

import pytest
import yaml

from qnetlab.addressing import build_plan
from qnetlab.config import load_topology
from qnetlab.config.loader import parse_topology
from qnetlab.dataplane.verify import RouterExpectation, evaluate, expectation_for


@pytest.fixture(scope="module")
def ring4(ring4_path):
    topo = load_topology(ring4_path)
    return topo, build_plan(topo)


def healthy(exp: RouterExpectation) -> dict:
    """Snapshot de um CHR saudável, com os campos reais do RouterOS 7.22.1 (todos os valores em texto)."""
    return {
        "ospf_neighbors": [{"state": "Full", "interface": "ether2"}, {"state": "Full", "interface": "ether3"}],
        "ipsec_active_peers": [{"id": exp.remote_addr, "remote-address": exp.remote_addr, "local-address": exp.local_addr,
                                "state": "established", "ppk": "true", "side": "responder"}],
        "ipsec_installed_sa": [{"src-address": exp.local_addr, "dst-address": exp.remote_addr, "spi": "0x1"},
                               {"src-address": exp.remote_addr, "dst-address": exp.local_addr, "spi": "0x2"}],
        "qkd": {"enabled": "true", "peer-sae-id": exp.peer_sae, "kme-id": exp.kme_id, "key-size": "256",
                "total-keys-received": "15"},
    }


def test_expectation_from_plan_matches_lab_snapshot(ring4):
    """Mesmos valores do MKT12-state.json real do ring4: SA 10.0.1.5 <-> 10.0.1.6, KME1, vizinho sae-chr21."""
    topo, plan = ring4
    exp = expectation_for(topo, plan, "MKT12")
    assert (exp.protected, exp.local_addr, exp.remote_addr) == (True, "10.0.1.5", "10.0.1.6")
    assert (exp.peer_sae, exp.kme_id, exp.ppk_mode) == ("sae-chr21", "kme-sim-1", "qkd")


def test_healthy_router_passes_every_check(ring4):
    exp = expectation_for(*ring4, "MKT12")
    v = evaluate(exp, healthy(exp))
    assert v.ok and v.reasons == []
    assert set(v.checks) == {"ospf", "ipsec_peer", "ppk", "sa_out", "sa_in",
                             "qkd_enabled", "qkd_peer_sae", "qkd_kme", "qkd_keys"}
    assert all(v.checks.values())


def _mutate(snap, path, value):
    s = copy.deepcopy(snap)
    *head, last = path
    cur = s
    for k in head:
        cur = cur[k]
    if value is KeyError:
        del cur[last]
    else:
        cur[last] = value
    return s


@pytest.mark.parametrize("path, value, check, reason", [
    (("ipsec_active_peers", 0, "ppk"), "false", "ppk", "SEM PPK"),
    (("ipsec_active_peers", 0, "ppk"), KeyError, "ppk", "não expõe 'ppk'"),
    (("ipsec_active_peers", 0, "state"), "connecting", "ipsec_peer", "estado: connecting"),
    (("ipsec_active_peers", 0, "remote-address"), "10.0.1.99", "ipsec_peer", "nenhum peer"),
    (("qkd", "total-keys-received"), "0", "qkd_keys", "total-keys-received=0"),
    (("qkd", "total-keys-received"), KeyError, "qkd_keys", "(ausente)"),
    (("qkd", "enabled"), "false", "qkd_enabled", "não está habilitado"),
    (("qkd", "peer-sae-id"), "sae-chr42", "qkd_peer_sae", "esperado sae-chr21"),
    (("qkd", "kme-id"), "kme-sim-2", "qkd_kme", "esperado kme-sim-1"),
    (("qkd",), {"raw": "erro"}, "qkd_status", "não pôde ser lido"),
    (("ospf_neighbors", 1, "state"), "Init", "ospf", "1 vizinho(s) Full"),
])
def test_each_failure_is_detected_with_a_reason(ring4, path, value, check, reason):
    exp = expectation_for(*ring4, "MKT12")
    v = evaluate(exp, _mutate(healthy(exp), path, value))
    assert not v.ok and v.checks[check] is False
    assert any(reason in r for r in v.reasons), v.reasons


@pytest.mark.parametrize("keep", [0, 1])
def test_sa_must_exist_in_both_directions(ring4, keep):
    exp = expectation_for(*ring4, "MKT12")
    snap = healthy(exp)
    snap["ipsec_installed_sa"] = [snap["ipsec_installed_sa"][keep]]
    v = evaluate(exp, snap)
    assert not v.ok and v.checks["sa_out"] is (keep == 0) and v.checks["sa_in"] is (keep == 1)


def test_no_peer_at_all(ring4):
    exp = expectation_for(*ring4, "MKT12")
    snap = healthy(exp)
    snap["ipsec_active_peers"] = []
    v = evaluate(exp, snap)
    assert not v.ok and not v.checks["ipsec_peer"] and "ppk" not in v.checks    # sem peer, não há PPK a avaliar


def test_qkd_list_response_is_accepted(ring4):
    """Algumas versões do REST devolvem objetos únicos como lista de um elemento."""
    exp = expectation_for(*ring4, "MKT12")
    snap = healthy(exp)
    snap["qkd"] = [snap["qkd"]]
    assert evaluate(exp, snap).ok


def _variant(path, **ipsec):
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["ipsec"].update(ipsec)
    topo = parse_topology(raw)
    return topo, build_plan(topo)


def test_static_ppk_requires_ppk_but_not_qkd(ring4_path):
    topo, plan = _variant(ring4_path, ppk_mode="static")
    exp = expectation_for(topo, plan, "MKT12")
    snap = healthy(exp)
    snap["qkd"] = None                                             # não há key qkd em modo estático
    v = evaluate(exp, snap)
    assert v.ok and "ppk" in v.checks and not any(k.startswith("qkd") for k in v.checks)
    snap["ipsec_active_peers"][0]["ppk"] = "false"
    assert not evaluate(exp, snap).ok


def test_ppk_none_does_not_require_ppk(ring4_path):
    topo, plan = _variant(ring4_path, ppk_mode="none")
    exp = expectation_for(topo, plan, "MKT12")
    snap = healthy(exp)
    snap["qkd"] = None
    snap["ipsec_active_peers"][0]["ppk"] = "false"
    v = evaluate(exp, snap)
    assert v.ok and "ppk" not in v.checks


def test_control_group_link_must_stay_in_clear(ring4_path):
    """unprotected_links: [2] -> enlace MKT22<->MKT31 sem IPsec. Peer estabelecido ali invalida o grupo de controle."""
    topo, plan = _variant(ring4_path, unprotected_links=[2])
    exp = expectation_for(topo, plan, "MKT22")
    assert not exp.protected
    clear = {"ospf_neighbors": [{"state": "Full"}, {"state": "Full"}], "ipsec_active_peers": [],
             "ipsec_installed_sa": [], "qkd": None}
    v = evaluate(exp, clear)
    assert v.ok and set(v.checks) == {"ospf", "control_no_peer"}
    clear["ipsec_active_peers"] = [{"remote-address": exp.remote_addr, "state": "established", "ppk": "true"}]
    v = evaluate(exp, clear)
    assert not v.ok and "grupo de controle" in v.reasons[0]
    # os outros enlaces continuam exigindo QKD
    assert expectation_for(topo, plan, "MKT12").protected


def test_evaluates_real_state_json_shape(ring4):
    """Trecho literal do MKT12-state.json coletado no ring4 (RouterOS 7.22.1), com as chaves mascaradas."""
    snap = {
        "ospf_neighbors": [
            {"address": "10.0.1.6", "interface": "ether2", "state": "Full", "router-id": "10.255.0.21"},
            {"address": "10.0.2.1", "interface": "ether3", "state": "Full", "router-id": "10.255.0.11"}],
        "ipsec_active_peers": [
            {".id": "*1", "id": "10.0.1.6", "local-address": "10.0.1.5", "natt-peer": "false", "ph2-total": "1",
             "port": "4500", "ppk": "true", "remote-address": "10.0.1.6", "responder": "true",
             "side": "responder", "state": "established"}],
        "ipsec_installed_sa": [
            {".id": "*7", "ESP": "true", "dst-address": "10.0.1.5", "src-address": "10.0.1.6", "enc-key": "***"},
            {".id": "*8", "ESP": "true", "dst-address": "10.0.1.6", "src-address": "10.0.1.5", "enc-key": "***"}],
        "qkd": {"address": "10.0.0.5:8443", "cache-size": "10", "cache-state": "10", "certificate": "sae-chr12",
                "enabled": "true", "key-size": "256", "kme-id": "kme-sim-1", "peer-sae-id": "sae-chr21",
                "total-keys-received": "15"},
    }
    v = evaluate(expectation_for(*ring4, "MKT12"), snap)
    assert v.ok, v.reasons
