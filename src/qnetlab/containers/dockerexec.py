"""Acesso do host aos contêineres KME/NODE via ``docker exec`` / ``docker cp``.

Os nós Docker do GNS3 não têm rede de gerenciamento (como no laboratório original: só ``docker exec``).
O nome do contêiner no Docker é ``GNS3.<nome-do-nó>.<id-do-projeto>``.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from ..errors import TelemetryError

log = logging.getLogger(__name__)

Runner = Callable[[Sequence[str], int], "subprocess.CompletedProcess[str]"]


def _default_runner(cmd: Sequence[str], timeout: int) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)


class DockerExec:
    def __init__(self, project_id: str, docker: str = "docker", runner: Optional[Runner] = None):
        self.project_id = project_id
        self._bin = docker if runner else (shutil.which(docker) or "")
        self._run_cmd = runner or _default_runner
        self._cache: Dict[str, str] = {}

    # ----------------------------------------------------------------- baixo nível
    def _docker(self, *args: str, timeout: int = 60) -> str:
        if not self._bin:
            raise TelemetryError("Docker não encontrado no PATH (necessário para alcançar KMEs/NODEs).")
        try:
            res = self._run_cmd([self._bin, *args], timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TelemetryError(f"docker {args[0]} falhou: {exc}") from exc
        if res.returncode != 0:
            raise TelemetryError(f"docker {' '.join(args[:3])} falhou: {(res.stderr or res.stdout).strip()[-300:]}")
        return res.stdout

    def container(self, node: str) -> str:
        if node in self._cache:
            return self._cache[node]
        names = self._docker("ps", "--format", "{{.Names}}").split()
        exact = re.compile(rf"^GNS3\.{re.escape(node)}\.{re.escape(self.project_id)}$", re.IGNORECASE)
        for n in names:
            if exact.match(n):
                self._cache[node] = n
                return n
        loose = [n for n in names if self.project_id in n and f".{node.lower()}." in n.lower()]
        if loose:
            self._cache[node] = loose[0]
            return loose[0]
        raise TelemetryError(f"Contêiner do nó {node} não encontrado (projeto {self.project_id}). "
                             "O deploy foi feito neste host/daemon Docker?")

    def state(self, node: str) -> str:
        """Estado do contêiner do nó no Docker, **inclusive parado** (``docker ps -a``): ``Up 3 minutes``,
        ``Exited (137) 2 seconds ago``… ou ``inexistente``. Não usa o cache de nomes."""
        out = self._docker("ps", "-a", "--format", "{{.Names}}|{{.Status}}")
        for line in out.splitlines():
            name, _, status = line.partition("|")
            if re.match(rf"^GNS3\.{re.escape(node)}\.{re.escape(self.project_id)}$", name, re.IGNORECASE):
                return status.strip()
        return "inexistente"

    # ------------------------------------------------------------------- operações
    def exec(self, node: str, argv: Sequence[str], timeout: int = 60) -> str:
        return self._docker("exec", self.container(node), *argv, timeout=timeout)

    def http(self, node: str, method: str, port: int, path: str, body: Optional[dict] = None,
             timeout: int = 20) -> str:
        """Requisição HTTP a 127.0.0.1:<port> *dentro* do contêiner (usa o curl da imagem-base)."""
        argv: List[str] = ["curl", "-sS", "--fail", "-m", str(max(1, timeout - 5)), "-X", method,
                           f"http://127.0.0.1:{port}{path}"]
        if body is not None:
            argv += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
        return self.exec(node, argv, timeout=timeout)

    def http_json(self, node: str, method: str, port: int, path: str, body: Optional[dict] = None) -> dict:
        out = self.http(node, method, port, path, body)
        try:
            return json.loads(out or "{}")
        except json.JSONDecodeError as exc:
            raise TelemetryError(f"{node}: resposta não-JSON de {path}: {out[:120]!r}") from exc

    def cp_from(self, node: str, src: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._docker("cp", f"{self.container(node)}:{src}", str(dest))
        return dest
