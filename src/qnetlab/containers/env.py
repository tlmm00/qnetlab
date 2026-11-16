"""Ambiente dos contêineres (KME e NODE), compartilhado por GNS3 e Compose.

Os contêineres não têm gerenciamento próprio: as interfaces (``ethK``) vêm de ``IFACES`` e as rotas de
``ROUTES`` (``rede via gateway dev ethK``), aplicadas pelo entrypoint. Acesso do host = ``docker exec``.
"""
from __future__ import annotations

from typing import Dict, List

from ..addressing import AddressPlan
from ..config.models import Topology
from ..workspace import Workspace


def peers_env(topo: Topology, plan: AddressPlan, site: int) -> str:
    """Pares de sonda do NODE: vizinhos do anel, alcançados pelos túneis (nome=ip=protegido=sae)."""
    if topo.load.peers == "none":
        return ""
    prev_i, next_i = plan.neighbors(site)
    me = plan.sites[site]
    items = []
    # anterior: MKT(i-1)2 está atrás do NODE(i-1).eth1; o caminho sai por eth0 (MKTi1)
    # próximo:  MKT(i+1)1 está atrás do NODE(i+1).eth0; o caminho sai por eth1 (MKTi2)
    for other, my_chr, peer_chr, peer_if in ((prev_i, me.chrs[0], plan.sites[prev_i].chrs[1], "chr2"),
                                              (next_i, me.chrs[1], plan.sites[next_i].chrs[0], "chr1")):
        t = plan.tunnel_of(my_chr.name)
        ip = plan.sites[other].node.ifaces[peer_if].ip
        prot = int(bool(t and t.protected and t.peer_of(my_chr.name) == peer_chr.name))
        items.append(f"{plan.sites[other].node.name}={ip}={prot}=")
    return ",".join(dict.fromkeys(items))


def _ifaces(ifaces) -> str:
    return ",".join(f"{i.name}={i.cidr}" for i in sorted(ifaces.values(), key=lambda x: x.name))


def kme_environment(topo: Topology, plan: AddressPlan, ws: Workspace, site: int, gns3: bool = True) -> Dict[str, str]:
    """Ambiente do KME: papel e rede. A configuração do KME vai no ``kme_config.yaml`` da imagem."""
    kme = plan.sites[site].kme
    env = {
        "ROLE": "kme",
        "NODE_NAME": kme.name,
        "KME_ID": kme.kme_id,
        "KME_CONFIG": "/etc/qnetlab/kme_config.yaml",
    }
    if gns3:
        env["IFACES"] = _ifaces(kme.ifaces)
    else:
        env["SKIP_NET"] = "1"
    return env


def node_environment(topo: Topology, plan: AddressPlan, ws: Workspace, site: int, gns3: bool = True) -> Dict[str, str]:
    node = plan.sites[site].node
    env = {
        "ROLE": "app",
        "NODE_NAME": node.name,
        "SAE_ID": node.name,
        "PEERS": peers_env(topo, plan, site),
        "PROBE_PORT": str(topo.containers.probe_port),
        "METRICS_PORT": str(topo.containers.metrics_port),
        "KEY_SIZE_BITS": str(topo.containers.key_size_bits),
    }
    if gns3:
        env["IFACES"] = _ifaces(node.ifaces)
        env["ROUTES"] = ";".join(f"{net} via {via} dev {dev}" for net, via, dev in node.routes)
    else:
        env["SKIP_NET"] = "1"
    return env


# compatibilidade com o nome antigo
app_environment = node_environment


def to_gns3_environment(env: Dict[str, str]) -> str:
    """Formato do campo ``environment`` dos nós Docker do GNS3 (um KEY=VALUE por linha)."""
    return "\n".join(f"{k}={v}" for k, v in sorted(env.items()))
