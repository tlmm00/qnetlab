"""Fachada de orquestração: encadeia as quatro camadas do framework.

Camadas:
  1. infra      -> :class:`qnetlab.infra.TopologyDeployer`   (GNS3: projeto, nós, enlaces)
  2. pki/images -> :class:`qnetlab.pki.CertificateAuthority`, :class:`qnetlab.containers.ImageBuilder`
  3. dataplane  -> :class:`qnetlab.dataplane.DataPlaneConfigurator` (OSPF, IPsec/PPK, firewall)
  4. telemetry  -> :class:`qnetlab.telemetry.LoadRunner`, :class:`TelemetryCollector`
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from .addressing import AddressPlan, build_plan
from .bench import ResourceSampler, StageTimer, environment, saturation, write_report
from .capture import PacketCapture
from .config import load_topology
from .config.models import Topology
from .errors import QNetLabError
from .faults import FaultInjector, LinkIndex
from .containers import DockerExec, ImageBuilder
from .dataplane import DataPlaneConfigurator, RouterConfigRenderer
from .infra import Gns3Client, TopologyDeployer
from .pki import CertificateAuthority, entities_from_plan
from .telemetry import LoadRunner, TelemetryCollector
from .workspace import Workspace

log = logging.getLogger(__name__)


class Lab:
    def __init__(self, topology_path: "str | Path", workdir: "str | Path" = "build"):
        self.topo: Topology = load_topology(topology_path)
        self.plan: AddressPlan = build_plan(self.topo)
        self.topology_path = Path(topology_path)
        self.ws = Workspace(workdir, self.topo.name).ensure()
        self._ca: CertificateAuthority | None = None
        self._client: Gns3Client | None = None

    @property
    def ca(self) -> CertificateAuthority:
        if self._ca is None:
            self._ca = CertificateAuthority(self.ws.pki_dir, self.topo.pki)
        return self._ca

    # -------------------------------------------------------------- camada 2
    def pki(self, force: bool = False) -> List[str]:
        ents = entities_from_plan(self.topo, self.plan)
        issued = self.ca.issue_all(ents, force=force)
        self.ca.verify_all(ents)
        return issued

    def images(self) -> List[str]:
        self.pki()
        return ImageBuilder(self.topo, self.plan, self.ws, self.ca).build_all()

    def render(self) -> Dict[str, Any]:
        """Gera TODOS os artefatos offline (sem GNS3/Docker): PKI, .rsc e docker-compose."""
        self.pki()
        rsc = RouterConfigRenderer(self.topo, self.plan, self.ws).render_all()
        compose = ImageBuilder(self.topo, self.plan, self.ws, self.ca).render_compose()
        return {"pki": str(self.ws.pki_dir), "rsc": [str(p) for p in rsc.values()], "compose": str(compose)}

    # -------------------------------------------------------------- camada 1
    @property
    def client(self) -> Gns3Client:
        if self._client is None:
            self._client = Gns3Client(self.topo.gns3)
        return self._client

    def deploy(self, recreate: bool = False) -> Dict[str, Any]:
        return TopologyDeployer(self.client, self.topo, self.plan, self.ws).deploy(recreate=recreate)

    def destroy(self) -> bool:
        return TopologyDeployer(self.client, self.topo, self.plan, self.ws).destroy()

    # --------------------------------------------------- falhas, captura, medição
    def _deployed_state(self) -> Dict[str, Any]:
        st = self.ws.load_state()
        if not st.get("project_id") or not st.get("nodes"):
            raise QNetLabError("Projeto GNS3 não implantado (state.json sem project_id/nós): rode 'deploy'/'up'.")
        return st

    def check_deployed(self) -> Dict[str, Any]:
        """Confere se o ``project_id`` do ``state.json`` existe e está aberto no GNS3 (erro único e claro, em vez de 404)."""
        st = self._deployed_state()
        projects = {p["project_id"]: p for p in self.client.list_projects()}
        proj = projects.get(st["project_id"])
        if proj is None:
            same_name = next((p for p in projects.values() if p.get("name") == self.topo.project_name), None)
            hint = (f" Existe um projeto '{self.topo.project_name}' com outro ID ({same_name['project_id']}): o state.json está "
                    "desatualizado." if same_name else "")
            raise QNetLabError(f"O GNS3 não conhece o projeto {st['project_id']} do state.json (apagado, ou servidor GNS3 "
                               f"diferente?).{hint} Recrie o laboratório com 'make up' (ou 'qnetlab deploy --recreate').")
        if proj.get("status") != "opened":
            raise QNetLabError(f"O projeto '{proj.get('name')}' está '{proj.get('status')}' no GNS3 (o servidor foi "
                               "reiniciado?). Abra-o no GNS3 e inicie os nós, ou recrie com 'make up'.")
        return st

    def links(self) -> LinkIndex:
        """Enlaces do GNS3 com nomes lógicos. Usa o ``state.json``; se ele não tiver os IDs (deploy antigo),
        reconstrói a partir do GNS3 e grava no estado."""
        st = self._deployed_state()
        if st.get("links"):
            return LinkIndex.from_state(st)
        idx = LinkIndex.from_gns3(self.client.list_links(st["project_id"]), st["nodes"], self.plan)
        if not idx.links:
            raise QNetLabError("Nenhum enlace do projeto casa com o plano: o projeto no GNS3 foi alterado ou recriado?")
        self.ws.update_state(links=[{"id": l.id, "kind": l.kind, "a": l.a, "a_if": l.a_if, "b": l.b, "b_if": l.b_if}
                                    for l in idx.links])
        return idx

    def faults(self, mark_nodes: bool = True) -> FaultInjector:
        """Injetor de falhas. ``mark_nodes``: também grava cada ação em ``metrics.jsonl`` dos NODEs (se houver Docker)."""
        st = self.check_deployed()
        docker = self.docker() if mark_nodes else None
        markers = [sp.node.name for sp in self.plan.sites.values()] if docker else []
        return FaultInjector(self.client, st["project_id"], st["nodes"], self.links(), self.ws, docker, markers,
                             marker_port=self.topo.containers.metrics_port)

    def capture(self) -> PacketCapture:
        return PacketCapture(self.client, self.check_deployed()["project_id"], self.links(), self.ws)

    def sampler(self, interval: float = 2.0, use_docker: bool = True) -> ResourceSampler:
        return ResourceSampler(lambda: self.ws.load_state().get("project_id"), interval, use_docker=use_docker)

    def environment(self) -> Dict[str, Any]:
        extra: Dict[str, Any] = {"lab": self.topo.name, "ring_size": self.topo.ring_size,
                                 "chrs": len(self.plan.chrs()), "links": len(self.plan.links),
                                 "router_version_pinned": self.topo.router.update_version or self.topo.router.min_version}
        try:
            extra["gns3"] = self.client.version().get("version")
        except QNetLabError:
            extra["gns3"] = None
        return environment(self.topology_path, extra)

    # -------------------------------------------------------------- camada 3
    def docker(self, required: bool = False) -> "DockerExec | None":
        """Acesso `docker exec` aos NODEs/KMEs (sem rede de gerenciamento). Requer o projeto implantado."""
        pid = self.ws.load_state().get("project_id")
        if not pid:
            if required:
                raise QNetLabError("Projeto GNS3 não implantado (state.json sem project_id): rode 'deploy'/'up'.")
            return None
        return DockerExec(pid)

    def _dp(self) -> DataPlaneConfigurator:
        return DataPlaneConfigurator(self.topo, self.plan, self.ws, self.ca, docker=self.docker())

    def bootstrap(self) -> None:
        self._dp().bootstrap()

    def upgrade(self) -> Dict[str, str]:
        return self._dp().upgrade()

    def configure(self, upgrade: bool = True) -> None:
        self.pki()
        self._dp().configure(upgrade=upgrade)

    def verify(self, timeout: float = 240.0) -> Dict[str, Any]:
        return self._dp().verify(timeout)

    # -------------------------------------------------------------- camada 4
    def load(self) -> Dict[str, List[str]]:
        return LoadRunner(self.topo, self.plan, self.docker(required=True)).run()

    def collect(self, live: bool = True) -> Dict[str, Any]:
        return TelemetryCollector(self.topo, self.plan, self.ws, self.docker()).run(collect_live=live)

    # ------------------------------------------------------------- pipelines
    def up(self, recreate: bool = False, build_images: bool = True,
           timer: StageTimer | None = None) -> Dict[str, Any]:
        """PKI -> imagens -> GNS3 -> bootstrap -> atualização -> configuração -> verificação.

        Cada etapa é cronometrada; os tempos vão para ``bench/up-<UTC>.json`` (insumo do DES-03).
        """
        timer = timer or StageTimer()
        report: Dict[str, Any] = {}
        try:
            with timer.stage("pki"):
                self.pki()
            if build_images:
                with timer.stage("images"):
                    ImageBuilder(self.topo, self.plan, self.ws, self.ca).build_all()
            with timer.stage("deploy"):
                self.deploy(recreate=recreate)
            with timer.stage("bootstrap"):
                self.bootstrap()
            with timer.stage("upgrade"):
                self.upgrade()
            with timer.stage("configure"):
                self.configure(upgrade=False)
            with timer.stage("verify"):
                report = self.verify()
            return report
        finally:
            path = write_report(self.ws, "up", {**timer.report(), "lab": self.topo.name,
                                                "verify_ok": report.get("_ok")})
            log.info("Tempos por etapa gravados em %s", path)

    def run_experiment(self) -> Dict[str, Any]:
        self.load()
        return self.collect()
