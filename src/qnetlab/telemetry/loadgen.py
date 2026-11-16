"""Injeção de carga: comanda os agentes dos NODEs (``docker exec``; os NODEs não têm rede de gerenciamento)."""
from __future__ import annotations

import logging
import time
from typing import Dict, List

from ..addressing import AddressPlan
from ..config.models import Topology
from ..containers.dockerexec import DockerExec
from ..errors import QNetLabError, TelemetryError

log = logging.getLogger(__name__)


class LoadRunner:
    def __init__(self, topo: Topology, plan: AddressPlan, docker: DockerExec):
        self.topo, self.plan, self.docker = topo, plan, docker
        self.port = topo.containers.metrics_port

    def _nodes(self) -> List[str]:
        return [sp.node.name for sp in self.plan.sites.values()]

    def _call(self, node: str, method: str, path: str, body: dict | None = None) -> dict:
        return self.docker.http_json(node, method, self.port, path, body)

    def wait_agents(self, timeout: float = 120.0) -> None:
        deadline = time.time() + timeout
        pending = set(self._nodes())
        while pending and time.time() < deadline:
            for node in sorted(pending):
                try:
                    self._call(node, "GET", "/control/status")
                    pending.discard(node)
                except QNetLabError:
                    pass
            if pending:
                time.sleep(2)
        if pending:
            raise TelemetryError(f"Agentes inacessíveis em {sorted(pending)}. Os contêineres estão rodando "
                                 "(docker ps) e o agente iniciou (docker logs)?")

    def run(self) -> Dict[str, List[str]]:
        """Inicia a carga em todos os NODEs, aguarda warm-up + duração e retorna os pares usados."""
        ld = self.topo.load
        self.wait_agents()
        if ld.warmup_s:
            log.info("Warm-up de %ss (permite o IKE/QKD estabilizar)", ld.warmup_s)
            time.sleep(ld.warmup_s)
        params = {"duration": ld.duration_s, "interval_ms": ld.probe_interval_ms,
                  "size": ld.probe_size_bytes, "key_interval_s": ld.key_interval_s}
        started: Dict[str, List[str]] = {}
        for node in self._nodes():
            try:
                started[node] = self._call(node, "POST", "/control/start", params).get("peers", [])
            except QNetLabError as exc:
                raise TelemetryError(f"Falha ao iniciar carga em {node}: {exc}") from exc
        log.info("Carga iniciada por %ss em %d NODEs", ld.duration_s, len(started))
        time.sleep(ld.duration_s + 3)
        self._wait_idle(60)
        return started

    def _wait_idle(self, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            busy = []
            for node in self._nodes():
                try:
                    if self._call(node, "GET", "/control/status").get("running"):
                        busy.append(node)
                except QNetLabError:
                    pass
            if not busy:
                return
            time.sleep(2)
        log.warning("Alguns agentes ainda estavam ocupados ao final da espera.")
