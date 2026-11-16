"""Ambiente Jinja2 compartilhado (modo estrito: variável ausente = erro)."""
from __future__ import annotations

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

from .errors import ConfigError
from .paths import templates_dir


def make_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(templates_dir())),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )


def render(template: str, **context: object) -> str:
    try:
        return make_env().get_template(template).render(**context)
    except TemplateError as exc:
        raise ConfigError(f"Falha ao renderizar '{template}': {exc}") from exc
