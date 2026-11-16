"""Constrói o contexto Jinja2 de cada CHR (MKTij) a partir do plano de endereçamento."""
from __future__ import annotations

from typing import Any, Dict, List

from ..addressing import AddressPlan
from ..config.models import Topology
from ..errors import ConfigError
from ..workspace import Workspace

CA_NAME = "qnetlab-ca"


def _safe(value: str, label: str) -> str:
    if any(c in value for c in '"\\\r\n$'):
        raise ConfigError(f"{label} não pode conter aspas, barra invertida, '$' ou quebras de linha.")
    return value


def router_files(topo: Topology, plan: AddressPlan, chr_name: str) -> List[Dict[str, Any]]:
    """Arquivos PEM que precisam estar no roteador (nome lógico, arquivos, chave privada?)."""
    if not topo.ipsec.enabled:
        return []
    c = plan.chr(chr_name)
    files: List[Dict[str, Any]] = [
        {"name": "qnetlab-ca", "crt": "ca.crt.pem", "key": None, "trusted": True},
        {"name": c.sae, "crt": f"{c.sae}.crt.pem", "key": f"{c.sae}.key.pem", "trusted": False},
    ]
    t = plan.tunnel_of(chr_name)
    if topo.ipsec.auth_method == "cert" and t and t.protected:
        peer = plan.chr(t.peer_of(chr_name))
        files.append({"name": peer.sae, "crt": f"{peer.sae}.crt.pem", "key": None, "trusted": False})
    return files


def build_router_context(topo: Topology, plan: AddressPlan, ws: Workspace, chr_name: str) -> Dict[str, Any]:
    c = plan.chr(chr_name)
    sp = plan.sites[c.site]
    ip = topo.ipsec
    r = topo.router
    costs = {"ring": topo.ospf.ring_cost, "sibling": topo.ospf.sibling_cost}

    interfaces = []
    for role in ("kme", "ring", "sibling", "node"):
        i = c.ifaces[role]
        interfaces.append({
            "role": role, "name": i.name, "ip": str(i.ip), "prefix": i.network.prefixlen,
            "network": str(i.network), "peer": i.peer,
            "cost": costs.get(role), "passive": role in ("kme", "node"),
        })

    tunnel = None
    qkd = None
    bypass: List[Dict[str, str]] = []
    t = plan.tunnel_of(chr_name)
    if t is not None and t.protected:
        peer = plan.chr(t.peer_of(chr_name))
        tunnel = {
            "name": f"to-{peer.name}",
            "peer_router": peer.name,
            "peer_sae": peer.sae,
            "local_addr": str(t.local_addr(chr_name)),
            "remote_addr": str(t.remote_addr(chr_name)),
            "src": str(t.local_net(chr_name)),
            "dst": str(t.remote_net(chr_name)),
            "psk": _safe(ip.psk, "ipsec.psk") if ip.psk else ws.secret(f"ike_psk:{t.name}", 32),
            "ppk_id": t.name,
            "ppk_secret": ws.secret(f"static_ppk:{t.name}", 32),
        }
        node_ip = c.ifaces["node"].ip
        bypass = [
            {"name": "router-originated", "src": f"{node_ip}/32", "dst": str(plan.node_pool)},
            {"name": "router-destined", "src": str(plan.node_pool), "dst": f"{node_ip}/32"},
        ]
        if ip.ppk_mode == "qkd":
            qkd = {
                "address": f"{c.ifaces['kme'].peer_ip}:{topo.containers.kme_api_port}",
                "certificate": c.sae,
                "kme_id": sp.kme.kme_id,
                "peer_sae_id": peer.sae,
                "key_size": ip.qkd_key_size,
                "cache_size": ip.qkd_cache_size,
            }

    # Rota estática para a rede NODE do vizinho pelo próprio enlace ether2 (garante o uso do túnel,
    # que vence o OSPF por distância administrativa).
    routes: List[Dict[str, str]] = []
    if t is not None:
        peer = plan.chr(t.peer_of(chr_name))
        routes.append({"dst": str(t.remote_net(chr_name)), "gateway": str(peer.ifaces["ring"].ip),
                       "comment": f"route {peer.name.lower()}"})

    mgmt = None
    if c.mgmt_ip is not None:
        mgmt = {"ip": str(c.mgmt_ip), "prefix": plan.mgmt_network.prefixlen, "net": str(plan.mgmt_network),
                "gw": str(plan.mgmt_gateway), "if": r.if_mgmt}
    return {
        "lab": topo.name,
        "r": {
            "name": c.name,
            "mgmt": mgmt,
            "mgmt_net": str(plan.mgmt_network),
            "ring_if": c.ifaces["ring"].name,
            "node_if": c.ifaces["node"].name,
            "interfaces": interfaces,
            "node_pool": str(plan.node_pool),
            "admin_user": _safe(topo.router.admin_user, "router.admin_user"),
            "admin_password": _safe(topo.router.admin_password, "router.admin_password"),
        },
        "ospf": {
            "instance": topo.ospf.instance, "area": topo.ospf.area, "area_id": topo.ospf.area_id,
            "router_id": str(c.router_id),
        },
        "ipsec": ip,
        "own_cert": c.sae,
        "certs": router_files(topo, plan, chr_name),
        "tunnel": tunnel,
        "bypass": bypass,
        "qkd": qkd,
        "static_routes": routes,
    }
