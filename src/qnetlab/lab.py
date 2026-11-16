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
from .config import load_topology
from .config.models import Topology
from .errors import QNetLabError
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
        self.ws = Workspace(workdir, self.topo.name).ensure()
        self._ca: CertificateAuthority | None = None

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
    def deploy(self, recreate: bool = False) -> Dict[str, Any]:
        client = Gns3Client(self.topo.gns3)
        return TopologyDeployer(client, self.topo, self.plan, self.ws).deploy(recreate=recreate)

    def destroy(self) -> bool:
        client = Gns3Client(self.topo.gns3)
        return TopologyDeployer(client, self.topo, self.plan, self.ws).destroy()

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

    def configure(self) -> None:
        self.pki()
        self._dp().configure()

    def verify(self, timeout: float = 240.0) -> Dict[str, Any]:
        return self._dp().verify(timeout)

    # -------------------------------------------------------------- camada 4
    def load(self) -> Dict[str, List[str]]:
        return LoadRunner(self.topo, self.plan, self.docker(required=True)).run()

    def collect(self, live: bool = True) -> Dict[str, Any]:
        return TelemetryCollector(self.topo, self.plan, self.ws, self.docker()).run(collect_live=live)

    # ------------------------------------------------------------- pipelines
    def up(self, recreate: bool = False, build_images: bool = True) -> Dict[str, Any]:
        """PKI -> imagens -> GNS3 -> bootstrap -> configuração -> verificação."""
        self.pki()
        if build_images:
            ImageBuilder(self.topo, self.plan, self.ws, self.ca).build_all()
        self.deploy(recreate=recreate)
        self.bootstrap()
        self.configure()
        return self.verify()

    def run_experiment(self) -> Dict[str, Any]:
        self.load()
        return self.collect()
