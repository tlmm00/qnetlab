"""Diretório de trabalho do laboratório: artefatos gerados, estado e segredos.

Layout (``<root>/<nome-da-topologia>/``)::

    pki/  rsc/  docker/  compose/  logs/  metrics/  output/  state.json

``state.json`` guarda IDs do GNS3 e segredos gerados (PSKs, semente dos KMEs).
Os segredos são criados uma única vez, o que torna reexecuções reprodutíveis
sem reutilizar nada entre laboratórios distintos. O arquivo tem modo 0600.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Dict


class Workspace:
    def __init__(self, root: "str | os.PathLike[str]", lab_name: str):
        self.root = Path(root).resolve() / lab_name
        self.lab_name = lab_name

    # diretórios -----------------------------------------------------------
    @property
    def pki_dir(self) -> Path:
        return self.root / "pki"

    @property
    def rsc_dir(self) -> Path:
        return self.root / "rsc"

    @property
    def docker_dir(self) -> Path:
        return self.root / "docker"

    @property
    def compose_dir(self) -> Path:
        return self.root / "compose"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def metrics_dir(self) -> Path:
        return self.root / "metrics"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    def ensure(self) -> "Workspace":
        for d in (self.pki_dir, self.rsc_dir, self.docker_dir, self.compose_dir,
                  self.logs_dir, self.metrics_dir, self.output_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self

    # estado ---------------------------------------------------------------
    def load_state(self) -> Dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {}

    def save_state(self, state: Dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".state-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.state_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def update_state(self, **kwargs: Any) -> Dict[str, Any]:
        state = self.load_state()
        state.update(kwargs)
        self.save_state(state)
        return state

    def secret(self, key: str, nbytes: int = 32) -> str:
        """Segredo hexadecimal persistente (gerado com ``secrets`` na primeira chamada)."""
        state = self.load_state()
        bag = state.setdefault("secrets", {})
        if key not in bag:
            bag[key] = secrets.token_hex(nbytes)
            self.save_state(state)
        return bag[key]

    def write_text(self, path: Path, content: str, mode: int = 0o644) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        os.chmod(path, mode)
        return path
