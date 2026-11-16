"""Localização dos recursos do repositório (templates, contexto Docker)."""
from __future__ import annotations

import os
from pathlib import Path

from .errors import ConfigError


def repo_root() -> Path:
    """Raiz do repositório. Sobrescreva com QNETLAB_HOME se instalado fora do checkout."""
    env = os.environ.get("QNETLAB_HOME")
    root = Path(env) if env else Path(__file__).resolve().parents[2]
    if not (root / "templates").is_dir():
        raise ConfigError(
            f"Diretório 'templates' não encontrado em {root}. "
            "Use 'pip install -e .' a partir do checkout ou defina QNETLAB_HOME."
        )
    return root


def templates_dir() -> Path:
    return repo_root() / "templates"


def node_context_dir() -> Path:
    return repo_root() / "docker" / "node"
