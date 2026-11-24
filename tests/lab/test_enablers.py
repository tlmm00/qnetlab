"""Valida os habilitadores HAB-01..05 e executa os primeiros testes do plano (N1/N2) no laboratório real.

Cada docstring traz a hipótese e o critério, escritos ANTES da execução (docs/14-validation-test-plan.md §3.2).
"""
from __future__ import annotations

import json
import time

import pytest

from labtools import node_ping, ospf_full, router, wait_until
from qnetlab.capture import summarize
from qnetlab.errors import QNetLabError


def _ring_link(lab):
    """Um enlace de anel entre dois CHRs que não é o do túnel testado (qualquer um serve para o teste de queda)."""
    return next(l for l in lab.plan.links if l.kind == "ring")


def _require_established(lab, chr_name: str) -> None:
    """Pré-condição dos testes de tráfego: o túnel do CHR está de pé. Senão o resultado é INCONCLUSIVO (não é um achado)."""
    try:
        up = bool(router(lab, chr_name).ipsec_active_peers())
    except QNetLabError as exc:
        pytest.skip(f"inconclusivo: sem REST em {chr_name} ({exc})")
    if not up:
        pytest.skip(f"inconclusivo: túnel IPsec de {chr_name} não está estabelecido (rode test_baseline_verify_is_ok; "
                    "sem SA o tráfego é descartado pela política, o que não diz nada sobre interceptação)")


@pytest.mark.plan("FID-00")
def test_baseline_verify_is_ok(lab, evidence):
    """Critério: `verify` devolve `_ok: true` em ≤ 120 s — OSPF full e, em cada túnel protegido, peer estabelecido
    **com PPK** (`ppk=true`), SA nos dois sentidos e `/ip ipsec key qkd` com chaves recebidas do KME (ADR-14)."""
    rep = lab.verify(timeout=120)
    evidence.record("verify", rep)
    routers = {n: r for n, r in rep.items() if isinstance(r, dict) and not n.startswith("_")}
    failed = {n: r.get("checks", {}) for n, r in routers.items() if not r.get("ok", True)}
    errors = {n: r["error"] for n, r in routers.items() if "error" in r}
    no_keys = sorted(n for n, c in failed.items() if c.get("qkd_keys") is False)
    no_ppk = sorted(n for n, c in failed.items() if c.get("ppk") is False)
    no_sa = sorted(n for n, c in failed.items() if False in (c.get("sa_out"), c.get("sa_in")))
    hints = []
    if errors:
        n, e = next(iter(errors.items()))
        hints.append(f"{len(errors)} CHR(s) sem REST, p. ex. {n}: {e[:160]} (gerenciamento? veja docs/11-troubleshooting.md)")
    if no_keys:
        hints.append(f"{len(no_keys)} CHR(s) com total-keys-received=0: o KME não entrega chaves "
                     "(contêiner KME parado? confira 'docker ps -a --filter name=GNS3.KME' e 'docker logs')")
    if no_ppk:
        hints.append(f"túnel estabelecido SEM PPK em {no_ppk}: confira 'ppk=qkd' no profile e logs/MKT*-ipsec.log")
    if no_sa and not no_keys:
        hints.append(f"sem SA IPsec (em algum sentido) em {no_sa} apesar de haver chaves: veja logs/MKT*-ipsec.log")
    assert rep["_ok"], (f"CHRs não prontos: {rep['_not_ready']}. " + "; ".join(hints)
                        + f" | motivos: {rep.get('_summary', {}).get('failed')}")


@pytest.mark.plan("HAB-01")
def test_gns3_accepts_filters_and_suspend(lab, faults, evidence):
    """Critério: o GNS3 aceita o payload de `filters`/`suspend` e devolve o mesmo estado na leitura seguinte."""
    link = _ring_link(lab)
    ref = faults.links.one(link.a, link.b, kind="ring")
    applied = faults.set_link(ref, delay_ms=20, jitter_ms=2, loss_pct=1, corrupt_pct=1)
    got = lab.client.get_link(lab.ws.load_state()["project_id"], ref.id)
    evidence.record("sent", applied)
    evidence.record("read_back", got.get("filters"))
    assert got["filters"] == applied
    faults.set_link(ref, bpf=["ip proto 1"])                     # descarta ICMP: formato do filtro BPF
    assert lab.client.get_link(lab.ws.load_state()["project_id"], ref.id)["filters"].get("bpf")
    faults.clear(ref)
    assert not lab.client.get_link(lab.ws.load_state()["project_id"], ref.id)["filters"]


@pytest.mark.plan("HAB-01")
@pytest.mark.disruptive
def test_link_down_breaks_ospf_and_up_recovers(lab, faults, evidence):
    """Critério: ao suspender um enlace de anel, a adjacência OSPF cai em ≤ 120 s; ao religar, volta a `Full` em ≤ 180 s."""
    link = _ring_link(lab)
    ref = faults.links.one(link.a, link.b, kind="ring")
    before = ospf_full(lab, link.a)
    assert before >= 2, f"{link.a} deveria ter ≥2 vizinhos Full antes da falha (tem {before})"
    faults.down(ref)
    dropped = wait_until(lambda: ospf_full(lab, link.a) < before, 120, what=f"{link.a} perder um vizinho OSPF")
    faults.up(ref)
    wait_until(lambda: ospf_full(lab, link.a) >= before, 180, what=f"{link.a} recuperar o vizinho OSPF")
    evidence.record("link", ref.label)
    evidence.record("full_before", before)
    assert dropped


@pytest.mark.plan("HAB-02")
@pytest.mark.disruptive
def test_stop_and_start_kme_container(lab, faults, evidence):
    """Critério: após `stop` o contêiner do KME1 some do Docker (exec falha); após `start` volta a responder."""
    docker = lab.docker(required=True)
    docker.exec("KME1", ["true"])                                   # estava no ar
    faults.node("KME1", "stop")
    with pytest.raises(QNetLabError):
        docker.exec("KME1", ["true"])
    faults.node("KME1", "start")
    wait_until(lambda: docker.exec("KME1", ["true"]) is not None, 60, what="KME1 voltar a responder")
    evidence.record("node", "KME1")


@pytest.mark.plan("HAB-04")
def test_up_stage_timings_were_recorded(lab, evidence):
    """Critério: o último `up` deixou `bench/up-*.json` com as etapas pki, deploy, bootstrap, upgrade, configure e verify."""
    reports = sorted(lab.ws.bench_dir.glob("up-*.json"))
    if not reports:
        pytest.skip("nenhum 'up' cronometrado neste diretório de trabalho (rode 'make up' ou 'qnetlab bench up')")
    rep = json.loads(reports[-1].read_text())
    stages = [s["stage"] for s in rep["stages"]]
    evidence.record("stages", rep["stages"])
    assert {"pki", "deploy", "bootstrap", "upgrade", "configure", "verify"} <= set(stages)   # "images" some com --skip-images
    assert all(s["seconds"] >= 0 for s in rep["stages"])


@pytest.mark.plan("HAB-04")
def test_resource_sampler_sees_the_lab(lab, evidence):
    """Critério: em 6 s de amostragem o amostrador enxerga todos os CHRs (qemu) e contêineres do projeto."""
    sampler = lab.sampler(interval=2.0)
    with sampler:
        time.sleep(6)
    summ = sampler.summary()
    chrs = {k.split(".")[1] for k in summ if k.startswith("chr.") and k.endswith("rss_mb")}
    conts = {k.split(".")[1] for k in summ if k.startswith("containers.") and k.endswith("mem_mb")}
    evidence.record("summary", {k: v for k, v in summ.items() if k.endswith("_total.rss_mb") or k.startswith("host.")})
    if not chrs:
        pytest.skip("nenhum processo qemu local: o servidor GNS3 roda em outro host? (o amostrador só vê o host local)")
    missing_chr = {c.name for c in lab.plan.chrs()} - chrs
    assert not missing_chr, f"CHRs sem processo qemu do projeto: {sorted(missing_chr)}"
    expected = {sp.kme.name for sp in lab.plan.sites.values()} | {sp.node.name for sp in lab.plan.sites.values()}
    assert not expected - conts, (f"contêineres ausentes do 'docker stats' (parados ou reiniciando?): {sorted(expected - conts)}. "
                                  "Confira: docker ps -a --filter name=GNS3. ; docker logs GNS3.<nó>.<id>")


@pytest.mark.plan("SEG-01")
def test_passive_interception_sees_only_ike_and_esp(lab, capture, evidence):
    """Hipótese: um espião passivo no enlace do túnel só vê IKE (UDP 500/4500) e ESP; nenhum TCP/UDP/ICMP de aplicação.
    Critério: em 30 s de captura com ping NODE<->NODE, `esp_packets` > 0 e `cleartext_data_packets` == 0.
    Controle positivo: `test_seg01_positive_control` (enlace em `unprotected_links` deve mostrar o ping em claro)."""
    tunnels = [t for t in lab.plan.tunnels if t.protected]
    if not tunnels:
        pytest.skip("topologia sem túneis protegidos")
    t = tunnels[0]
    ref = capture.links.one(t.a, t.b, kind="ring")
    dst = lab.plan.chr(t.b).ifaces["node"].peer_ip
    src_node = lab.plan.sites[lab.plan.chr(t.a).site].node.name
    _require_established(lab, t.a)
    with capture.capturing(ref, "seg01") as cap:
        node_ping(lab, src_node, str(dst), count=20)
    pcap = evidence.attach(cap.path)
    s = summarize(pcap)
    evidence.record("link", ref.label)
    evidence.record("summary", s)
    assert s["packets"] > 0, "captura vazia: o enlace não trafegou nada (túnel/roteamento?)"
    assert s["esp_packets"] > 0, "nenhum ESP: o ping não passou pelo túnel"
    assert s["cleartext_data_packets"] == 0, f"tráfego de aplicação em claro no enlace do túnel: {s['ip_protocols']}"
    if lab.topo.router.neighbor_discovery:                     # ligada de propósito: não reprova, mas registra o vazamento
        if s["discovery_packets"]:
            evidence.record("finding", f"{s['discovery_packets']} pacote(s) de descoberta de vizinhos (MNDP/LLDP/CDP) em claro no "
                                       "enlace do túnel: expõem identidade e versão do RouterOS.")
    else:                                                       # desligada na topologia: o laboratório tem de obedecer
        assert s["discovery_packets"] == 0, (
            f"{s['discovery_packets']} pacote(s) de descoberta de vizinhos (MNDP/LLDP/CDP) apesar de router.neighbor_discovery=false: "
            "o laboratório está com a configuração antiga (rode 'make configure')")


@pytest.mark.plan("SEG-01")
def test_seg01_positive_control(lab, capture, evidence):
    """Controle positivo do SEG-01: em um enlace do anel SEM túnel (`ipsec.unprotected_links`), o ping aparece em claro
    (ICMP > 0 e nenhum ESP). Pula se a topologia não tiver enlace desprotegido."""
    open_ = [t for t in lab.plan.tunnels if not t.protected]
    if not open_:
        pytest.skip("sem enlace em ipsec.unprotected_links: defina-o na topologia para ter o controle positivo")
    t = open_[0]
    ref = capture.links.one(t.a, t.b, kind="ring")
    dst = lab.plan.chr(t.b).ifaces["node"].peer_ip
    src_node = lab.plan.sites[lab.plan.chr(t.a).site].node.name
    with capture.capturing(ref, "seg01-control") as cap:
        node_ping(lab, src_node, str(dst), count=20)
    s = summarize(evidence.attach(cap.path))
    evidence.record("summary", s)
    assert s["ip_protocols"].get("icmp", 0) > 0 and s["esp_packets"] == 0, \
        "ping não passou em claro por este enlace (o roteamento pode ter escolhido outro caminho?)"
