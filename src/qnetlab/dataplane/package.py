"""Pacote RouterOS (.npk) baixado UMA vez no host e enviado por FTP aos CHRs.

Evita depender de DNS/Internet dentro dos CHRs: o host que roda o qnetlab (que tem Internet) baixa o pacote principal
do canal escolhido para ``build/<lab>/cache`` e o reutiliza em todos os roteadores.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable, Optional

import requests

from ..errors import RouterOsError

log = logging.getLogger(__name__)

VERSION_URL = "https://upgrade.mikrotik.com/routeros/NEWESTa7.{channel}"
DOWNLOAD_URL = "https://download.mikrotik.com/routeros/{version}/{filename}"
MIN_NPK_BYTES = 5 * 1024 * 1024

Getter = Callable[..., "requests.Response"]


def package_filename(version: str, arch: str) -> str:
    """Nome do pacote principal: x86/CHR não tem sufixo; as demais arquiteturas usam ``-<arch>``."""
    arch = (arch or "x86").lower()
    return f"routeros-{version}.npk" if arch in ("x86", "x86_64", "x86-64") else f"routeros-{version}-{arch}.npk"


def latest_version(channel: str, get: Getter = requests.get) -> str:
    """Versão mais nova do canal (stable | long-term | testing), ex.: '7.20.6'."""
    url = VERSION_URL.format(channel=channel)
    try:
        resp = get(url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RouterOsError(f"Não consegui consultar a versão do canal '{channel}' ({url}): {exc}. "
                            "Defina router.update_version (ex.: '7.20.6') para pular a consulta.") from exc
    m = re.match(r"\s*(\d+\.\d+(?:\.\d+)?)", resp.text)
    if not m:
        raise RouterOsError(f"Resposta inesperada de {url}: {resp.text[:80]!r}")
    return m.group(1)


def fetch_package(version: str, arch: str, cache_dir: Path, get: Getter = requests.get) -> Path:
    """Baixa (ou reaproveita do cache) o .npk principal para a arquitetura informada."""
    name = package_filename(version, arch)
    dest = cache_dir / name
    if dest.exists() and dest.stat().st_size >= MIN_NPK_BYTES:
        log.info("Pacote %s já está em cache (%.1f MB)", name, dest.stat().st_size / 1e6)
        return dest
    cache_dir.mkdir(parents=True, exist_ok=True)
    url = DOWNLOAD_URL.format(version=version, filename=name)
    part = dest.with_suffix(".part")
    log.info("Baixando %s", url)
    try:
        with get(url, stream=True, timeout=(10, 60)) as resp:
            resp.raise_for_status()
            with open(part, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
    except (requests.RequestException, OSError) as exc:
        part.unlink(missing_ok=True)
        raise RouterOsError(f"Falha ao baixar {url}: {exc}") from exc
    if part.stat().st_size < MIN_NPK_BYTES:
        part.unlink(missing_ok=True)
        raise RouterOsError(f"{url}: arquivo pequeno demais para um pacote RouterOS "
                            "(versão/arquitetura inexistente?).")
    part.replace(dest)
    log.info("Pacote salvo em %s (%.1f MB)", dest, dest.stat().st_size / 1e6)
    return dest
