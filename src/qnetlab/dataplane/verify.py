"""Critérios do ``verify``: decide, a partir de um *snapshot* do CHR, se o túnel está de pé **e usa o PPK/QKD**.

Função pura (sem E/S). Recebe o que o plano espera de um CHR (:class:`RouterExpectation`) e o estado lido do
RouterOS no mesmo formato dos ``logs/MKT*-state.json`` do coletor::

    {"ospf_neighbors": [...], "ipsec_active_peers": [...], "ipsec_installed_sa": [...], "qkd": {...} | None}

e devolve um :class:`RouterVerdict` com o resultado de cada critério e os motivos de falha em texto.

Critérios (docs/decisions.md, ADR-14):

* todos: ``ospf_full >= 2`` (enlace do anel + enlace irmão);
* túnel protegido: peer ``established`` com ``remote-address`` = endereço do vizinho no plano e SA instalada
  nos **dois** sentidos entre os endereços do túnel;
* ``ppk_mode`` ``qkd`` ou ``static``: esse peer com ``ppk=true`` (campo ausente = falha: não há como provar o PPK);
* ``ppk_mode: qkd``: ``/ip ipsec key qkd`` com ``enabled=true``, ``peer-sae-id`` e ``kme-id`` esperados e
  ``total-keys-received > 0``;
* enlace do grupo de controle (``ipsec.unprotected_links``): **nenhum** peer estabelecido com o vizinho.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from ..addressing import AddressPlan
from ..config.models import Topology

OSPF_EXPECTED = 2        # enlace do anel (ether2) + enlace irmão (ether3)


@dataclass(frozen=True)
class RouterExpectation:
    """O que o plano exige de um CHR."""
    name: str
    protected: bool                  # o enlace ether2 deste CHR é um túnel IPsec (False = grupo de controle/sem IPsec)
    local_addr: str
    remote_addr: str
    ppk_mode: str = "qkd"            # qkd | static | none
    peer_sae: str = ""               # peer-sae-id esperado no /ip ipsec key qkd
    kme_id: str = ""                 # kme-id esperado no /ip ipsec key qkd
    ospf_expected: int = OSPF_EXPECTED


@dataclass
class RouterVerdict:
    ok: bool
    checks: Dict[str, bool] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)


def expectation_for(topo: Topology, plan: AddressPlan, chr_name: str) -> RouterExpectation:
    c = plan.chr(chr_name)
    t = plan.tunnel_of(chr_name)
    if t is None:                                    # não ocorre num anel regular; tratado como "sem túnel"
        return RouterExpectation(chr_name, False, "", "", topo.ipsec.ppk_mode)
    peer = plan.chr(t.peer_of(chr_name))
    return RouterExpectation(
        name=chr_name,
        protected=t.protected,
        local_addr=str(t.local_addr(chr_name)),
        remote_addr=str(t.remote_addr(chr_name)),
        ppk_mode=topo.ipsec.ppk_mode,
        peer_sae=peer.sae,
        kme_id=plan.sites[c.site].kme.kme_id,
    )


# ------------------------------------------------------------------- utilitários
def _flag(value: Any) -> Optional[bool]:
    """Booleanos do REST do RouterOS vêm como texto ("true"/"false"). ``None`` = campo ausente/ilegível."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("true", "yes"):
        return True
    if s in ("false", "no"):
        return False
    return None


def _int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _rows(value: Any) -> List[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    return [r for r in (value or []) if isinstance(r, Mapping)]


def _peer_addr(row: Mapping[str, Any]) -> str:
    return str(row.get("remote-address") or row.get("id") or "").split("/")[0]


# --------------------------------------------------------------------- avaliação
def evaluate(exp: RouterExpectation, snap: Mapping[str, Any]) -> RouterVerdict:
    checks: Dict[str, bool] = {}
    reasons: List[str] = []

    def check(name: str, ok: bool, reason: str) -> bool:
        checks[name] = ok
        if not ok:
            reasons.append(reason)
        return ok

    full = [n for n in _rows(snap.get("ospf_neighbors")) if str(n.get("state", "")).lower() == "full"]
    check("ospf", len(full) >= exp.ospf_expected,
          f"OSPF: {len(full)} vizinho(s) Full, esperados {exp.ospf_expected}")

    peers = [p for p in _rows(snap.get("ipsec_active_peers")) if _peer_addr(p) == exp.remote_addr]
    established = [p for p in peers if str(p.get("state", "")).lower() == "established"]

    if not exp.protected:
        # Grupo de controle: o enlace deve trafegar em claro; um peer estabelecido invalida o controle.
        check("control_no_peer", not established,
              f"enlace sem proteção (grupo de controle), mas há peer IPsec estabelecido com {exp.remote_addr}")
        return RouterVerdict(not reasons, checks, reasons)

    states = ", ".join(sorted({str(p.get("state")) for p in peers}))
    check("ipsec_peer", bool(established),
          f"IPsec: nenhum peer 'established' com {exp.remote_addr}" + (f" (estado: {states})" if peers else ""))
    peer: Optional[Mapping[str, Any]] = established[0] if established else None

    if exp.ppk_mode in ("qkd", "static") and peer is not None:
        ppk = _flag(peer.get("ppk"))
        if ppk is None:
            check("ppk", False, "IPsec: o RouterOS não expõe 'ppk' em active-peers; não é possível provar o uso do PPK")
        else:
            check("ppk", ppk, f"IPsec: peer {exp.remote_addr} estabelecido SEM PPK (ppk=false)")

    sas = _rows(snap.get("ipsec_installed_sa"))
    pairs = {(str(s.get("src-address", "")).split("/")[0], str(s.get("dst-address", "")).split("/")[0]) for s in sas}
    check("sa_out", (exp.local_addr, exp.remote_addr) in pairs,
          f"IPsec: sem SA instalada {exp.local_addr} -> {exp.remote_addr}")
    check("sa_in", (exp.remote_addr, exp.local_addr) in pairs,
          f"IPsec: sem SA instalada {exp.remote_addr} -> {exp.local_addr}")

    if exp.ppk_mode == "qkd":
        qkd = snap.get("qkd")
        qkd = qkd[0] if isinstance(qkd, list) and qkd and isinstance(qkd[0], Mapping) else qkd
        if not isinstance(qkd, Mapping) or not qkd or "raw" in qkd:
            check("qkd_status", False, "QKD: /ip ipsec key qkd não pôde ser lido")
        else:
            check("qkd_enabled", _flag(qkd.get("enabled")) is True, "QKD: /ip ipsec key qkd não está habilitado")
            got_sae = str(qkd.get("peer-sae-id", ""))
            check("qkd_peer_sae", got_sae == exp.peer_sae,
                  f"QKD: peer-sae-id={got_sae or '(vazio)'}, esperado {exp.peer_sae}")
            got_kme = str(qkd.get("kme-id", ""))
            check("qkd_kme", got_kme == exp.kme_id, f"QKD: kme-id={got_kme or '(vazio)'}, esperado {exp.kme_id}")
            keys = _int(qkd.get("total-keys-received"))
            check("qkd_keys", bool(keys and keys > 0),
                  f"QKD: total-keys-received={qkd.get('total-keys-received', '(ausente)')}: o KME não entregou chaves")

    return RouterVerdict(not reasons, checks, reasons)
