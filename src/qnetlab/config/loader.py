"""Carregamento e validação de topologias declarativas (YAML/JSON).

Recursos:
* interpolação de variáveis de ambiente: ``${VAR}`` ou ``${VAR:-padrão}``;
* rejeição de chaves desconhecidas (erros de digitação não passam em silêncio);
* topologia = anel de N sites (``ring: {size: N}``); cada site tem 2 CHRs, 1 KME e 1 NODE.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import fields
from pathlib import Path
from typing import Any, Dict

import yaml

from ..errors import ConfigError
from .models import (
    AddressingConfig,
    ContainerConfig,
    Gns3Config,
    IpsecConfig,
    KmeConfig,
    LoadConfig,
    OspfConfig,
    PkiConfig,
    RouterConfig,
    Topology,
)

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_TOP_KEYS = {
    "name", "description", "gns3", "addressing", "router", "containers", "kme", "ospf",
    "ipsec", "pki", "load", "ring",
}


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: "re.Match[str]") -> str:
            var, default = m.group(1), m.group(2)
            if var in os.environ:
                return os.environ[var]
            if default is not None:
                return default
            raise ConfigError(f"Variável de ambiente obrigatória não definida: {var}")
        return _ENV.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def expand_env(value: Any) -> Any:
    """Interpola ``${VAR}`` / ``${VAR:-padrão}`` em estruturas aninhadas."""
    return _expand_env(value)


def _section(cls: type, data: Any, label: str) -> Any:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"A seção '{label}' deve ser um mapeamento.")
    allowed = {f.name for f in fields(cls)}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(
            f"Chave(s) desconhecida(s) em '{label}': {sorted(unknown)}. Permitidas: {sorted(allowed)}"
        )
    try:
        return cls(**data)
    except TypeError as exc:  # pragma: no cover - defensivo
        raise ConfigError(f"Seção '{label}' inválida: {exc}") from exc


def parse_topology(raw: Dict[str, Any]) -> Topology:
    if not isinstance(raw, dict):
        raise ConfigError("O documento raiz deve ser um mapeamento.")
    raw = _expand_env(raw)
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise ConfigError(f"Chave(s) desconhecida(s) no topo: {sorted(unknown)}")
    if "name" not in raw:
        raise ConfigError("Campo obrigatório ausente: 'name'.")

    ring = raw.get("ring")
    if not isinstance(ring, dict) or set(ring) - {"size"}:
        raise ConfigError("Defina 'ring: {size: N}' (única chave aceita: size).")
    size = ring.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 3 or size > 24:
        raise ConfigError("ring.size deve ser um inteiro entre 3 e 24.")

    ipsec_raw = dict(raw.get("ipsec") or {})
    if "unprotected_links" in ipsec_raw:
        ul = ipsec_raw["unprotected_links"]
        if not isinstance(ul, list):
            raise ConfigError("ipsec.unprotected_links deve ser uma lista de inteiros.")
        ipsec_raw["unprotected_links"] = tuple(ul)

    topo = Topology(
        name=str(raw["name"]),
        description=str(raw.get("description", "")),
        ring_size=size,
        gns3=_section(Gns3Config, raw.get("gns3"), "gns3"),
        addressing=_section(AddressingConfig, raw.get("addressing"), "addressing"),
        router=_section(RouterConfig, raw.get("router"), "router"),
        containers=_section(ContainerConfig, raw.get("containers"), "containers"),
        kme=_section(KmeConfig, raw.get("kme"), "kme"),
        ospf=_section(OspfConfig, raw.get("ospf"), "ospf"),
        ipsec=_section(IpsecConfig, ipsec_raw, "ipsec"),
        pki=_section(PkiConfig, raw.get("pki"), "pki"),
        load=_section(LoadConfig, raw.get("load"), "load"),
    )
    topo.validate()
    _maybe_validate_schema(raw)
    return topo


def _maybe_validate_schema(raw: Dict[str, Any]) -> None:
    """Valida contra schema/topology.schema.json quando ``jsonschema`` estiver instalado."""
    try:
        import jsonschema  # type: ignore
    except ImportError:
        return
    schema_path = Path(__file__).resolve().parents[3] / "schema" / "topology.schema.json"
    if not schema_path.exists():
        return
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(raw, schema)
    except jsonschema.ValidationError as exc:
        loc = "/".join(str(p) for p in exc.absolute_path) or "<raiz>"
        raise ConfigError(f"Violação do schema em '{loc}': {exc.message}") from exc


def load_topology(path: "str | os.PathLike[str]") -> Topology:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"Arquivo de topologia não encontrado: {p}")
    text = p.read_text(encoding="utf-8")
    try:
        raw = json.loads(text) if p.suffix.lower() == ".json" else yaml.safe_load(text)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Falha ao interpretar {p}: {exc}") from exc
    return parse_topology(raw)
