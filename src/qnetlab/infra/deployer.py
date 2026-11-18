"""Plano de controle: cria projeto, nós e enlaces no GNS3 a partir do plano de endereçamento."""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from ..addressing import AddressPlan, Link
from ..config.models import Topology
from ..containers.env import kme_environment, node_environment, to_gns3_environment
from ..errors import Gns3Error
from ..workspace import Workspace
from .gns3_client import Gns3Client

log = logging.getLogger(__name__)



def adapter_of(ifname: str) -> int:
    """Nome da interface -> número do adaptador no GNS3: etherK -> K-1 (CHR); ethK -> K (contêiner); nat0 -> 0."""
    if ifname.startswith("ether"):
        return int(ifname[5:]) - 1
    if ifname.startswith("eth"):
        return int(ifname[3:])
    if ifname.startswith("nat"):
        return int(ifname[3:])
    raise Gns3Error(f"Interface desconhecida: {ifname}")


class TopologyDeployer:
    def __init__(self, client: Gns3Client, topo: Topology, plan: AddressPlan, ws: Workspace):
        self.client, self.topo, self.plan, self.ws = client, topo, plan, ws
        self.project_id: str = ""
        self._nat_tpl: Optional[Dict[str, Any]] = None
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.links: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ layout
    def _positions(self) -> Dict[int, Dict[str, Tuple[int, int]]]:
        n = len(self.topo.sites)
        radius = 300 + 60 * n
        out: Dict[int, Dict[str, Tuple[int, int]]] = {}
        for pos, sid in enumerate(self.topo.site_ids()):
            ang = 2 * math.pi * pos / n - math.pi / 2
            ux, uy = math.cos(ang), math.sin(ang)          # radial (para fora)
            px, py = -uy, ux                               # tangencial

            def c(r: float, off: float = 0.0) -> Tuple[int, int]:
                return (int(r * ux + off * px), int(r * uy + off * py))

            out[sid] = {
                "chr1": c(radius, -110), "chr2": c(radius, 110),
                "nat1": c(radius + 110, -110), "nat2": c(radius + 110, 110),
                "node": c(radius + 150, 0), "kme": c(radius * 0.62, 0),
            }
        return out

    # ------------------------------------------------------------------- nodes
    def _register(self, name: str, node: Dict[str, Any]) -> None:
        self.nodes[name] = {
            "id": node["node_id"],
            "type": node.get("node_type"),
            "console_host": node.get("console_host"),
            "console": node.get("console"),
        }

    def _add_router(self, name: str, tpl: Dict[str, Any], x: int, y: int) -> None:
        adapters = 1 + max(adapter_of(i) for i in (
            self.topo.router.if_kme, self.topo.router.if_ring, self.topo.router.if_sibling,
            self.topo.router.if_node, self.topo.router.if_mgmt))
        node = self.client.create_node_from_template(self.project_id, tpl["template_id"], x, y)
        node = self.client.update_node(self.project_id, node["node_id"],
                                       {"name": name, "properties": {"adapters": adapters}})
        self._register(name, node)

    def _add_container(self, name: str, image: str, adapters: int, env: Dict[str, str], x: int, y: int) -> None:
        node = self.client.create_node(self.project_id, {
            "name": name, "node_type": "docker", "x": x, "y": y,
            "properties": {"image": image, "adapters": adapters, "start_command": "",
                           "environment": to_gns3_environment(env), "console_type": "telnet"},
        })
        self._register(name, node)

    def _add_nat(self, name: str, x: int, y: int) -> None:
        """Nó NAT do GNS3 (ícone de nuvem, porta nat0), criado a partir do template embutido 'NAT'."""
        if self._nat_tpl is None:
            self._nat_tpl = self.client.find_nat_template() or {}
        if self._nat_tpl:
            node = self.client.create_node_from_template(self.project_id, self._nat_tpl["template_id"], x, y)
            node = self.client.update_node(self.project_id, node["node_id"], {"name": name})
        else:                                           # servidor sem o template: mesmo nó, símbolo de nuvem explícito
            node = self.client.create_node(self.project_id, {"name": name, "node_type": "nat", "x": x, "y": y,
                                                            "symbol": ":/symbols/cloud.svg", "properties": {}})
        self._register(name, node)

    # ------------------------------------------------------------------- links
    def _link(self, l: Link) -> None:
        def ep(node: str, ifname: str) -> Dict[str, Any]:
            return {"node_id": self.nodes[node]["id"], "adapter_number": adapter_of(ifname), "port_number": 0}
        res = self.client.create_link(self.project_id, ep(l.a, l.a_if), ep(l.b, l.b_if)) or {}
        if res.get("link_id"):                       # necessário para injeção de falhas e captura (qnetlab.faults/capture)
            self.links.append({"id": res["link_id"], "kind": l.kind,
                               "a": l.a, "a_if": l.a_if, "b": l.b, "b_if": l.b_if})

    # ------------------------------------------------------------------ deploy
    def deploy(self, recreate: bool = False) -> Dict[str, Dict[str, Any]]:
        c = self.client
        ver = c.version()
        log.info("GNS3 %s", ver.get("version", "?"))
        name = self.topo.project_name
        existing = c.get_project_by_name(name)
        if existing:
            if not recreate:
                raise Gns3Error(f"Projeto '{name}' já existe no GNS3. Use --recreate para recriá-lo.")
            self._delete(existing["project_id"])
        tpl = c.find_template(self.topo.router.gns3_template)
        project = c.create_project(name)
        self.project_id = project["project_id"]
        self.links = []
        self.ws.update_state(project_id=self.project_id)
        try:
            self._build(tpl)
            log.info("Iniciando %d nós...", len(self.nodes))
            c.start_all(self.project_id)
            c.wait_started(self.project_id)
            self._refresh_consoles()
        except Exception:
            log.error("Falha durante o deploy; o projeto parcial foi mantido para inspeção: %s", name)
            raise
        self.ws.update_state(project_id=self.project_id, nodes=self.nodes, links=self.links)
        return self.nodes

    def _build(self, tpl: Dict[str, Any]) -> None:
        pos = self._positions()
        nat = self.topo.addressing.mgmt_attach == "nat"
        for sid, sp in self.plan.sites.items():
            p = pos[sid]
            for j, c in enumerate(sp.chrs, 1):
                self._add_router(c.name, tpl, *p[f"chr{j}"])
                if nat:
                    self._add_nat(c.nat_name, *p[f"nat{j}"])
            kme = sp.kme
            self._add_container(kme.name, self.topo.image_tag(kme.name), len(kme.ifaces),
                                kme_environment(self.topo, self.plan, self.ws, sid), *p["kme"])
            node = sp.node
            self._add_container(node.name, self.topo.node_image, len(node.ifaces),
                                node_environment(self.topo, self.plan, self.ws, sid), *p["node"])
        for l in self.plan.links:
            log.debug("link %s:%s <-> %s:%s", l.a, l.a_if, l.b, l.b_if)
            self._link(l)

    def _refresh_consoles(self) -> None:
        for name, info in self.nodes.items():
            node = self.client.get_node(self.project_id, info["id"])
            info["console"] = node.get("console")
            info["console_host"] = node.get("console_host") or self._host()

    def _host(self) -> str:
        from urllib.parse import urlparse
        return urlparse(self.topo.gns3.url).hostname or "127.0.0.1"

    # ----------------------------------------------------------------- destroy
    def _delete(self, project_id: str) -> None:
        try:
            self.client.stop_all(project_id)
        except Gns3Error as exc:
            log.warning("Falha ao parar nós (seguindo): %s", exc)
        try:
            self.client.close_project(project_id)
        except Gns3Error as exc:
            log.debug("close_project: %s", exc)
        self.client.delete_project(project_id)

    def destroy(self) -> bool:
        proj = self.client.get_project_by_name(self.topo.project_name)
        if not proj:
            log.info("Projeto '%s' não existe no GNS3.", self.topo.project_name)
            return False
        self._delete(proj["project_id"])
        state = self.ws.load_state()
        state.pop("project_id", None)
        state.pop("nodes", None)
        state.pop("links", None)
        self.ws.save_state(state)
        log.info("Projeto '%s' removido.", self.topo.project_name)
        return True
