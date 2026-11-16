"""Gera o ``kme_config.yaml`` de cada KME (esquema do KME de KMS-sim).

* ``peer_sync.peers``: um enlace KMA por vizinho do anel de KMEs (:attr:`AddressPlan.kma_links`),
  alcançado pelo IP do enlace direto /30 (172.16.100.x);
* ``seed``: segredo de 256 bits **por par** (os dois lados recebem o mesmo valor), persistido em ``state.json``;
* ``kme_topology``: todos os KMEs e seus SAEs (os dois CHRs de cada site). O KME usa os SAEs de KMEs
  remotos para rotear ``enc_keys`` até o pool do enlace KMA correspondente.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ..addressing import AddressPlan
from ..config.models import Topology
from ..rendering import render
from ..workspace import Workspace


def kma_seed(ws: Workspace, plan: AddressPlan, a: int, b: int) -> str:
    ia, ib = plan.sites[a].kme.kme_id, plan.sites[b].kme.kme_id
    lo, hi = sorted((ia, ib))
    return ws.secret(f"kma_seed:{lo}-{hi}", 32)


def kme_config_context(topo: Topology, plan: AddressPlan, ws: Workspace, site: int, flat: bool = False) -> Dict[str, Any]:
    """``flat=True``: ambiente docker-compose sem GNS3 (vizinhos alcançados pelo nome DNS do serviço)."""
    me = plan.sites[site].kme
    peers: List[Dict[str, str]] = []
    for p in plan.kme_peers(site):
        psp = plan.sites[p]
        host = psp.kme.kme_id if flat else str(plan.kme_link_ip(site, p)[1])
        peers.append({"id": psp.kme.kme_id, "host": host, "seed": kma_seed(ws, plan, site, p)})
    topology = [
        {"id": sp.kme.kme_id, "saes": [c.sae for c in sp.chrs]}
        for sp in plan.sites.values()
    ]
    return {
        "kme": {"id": me.kme_id, "port": topo.containers.kme_api_port},
        "sync": topo.kme,
        "peers": peers,
        "topology": topology,
    }


def render_kme_config(topo: Topology, plan: AddressPlan, ws: Workspace, site: int, flat: bool = False) -> str:
    return render("docker/kme_config.yaml.j2", **kme_config_context(topo, plan, ws, site, flat))
