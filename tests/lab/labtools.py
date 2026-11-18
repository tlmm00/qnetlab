"""Utilitários dos testes de laboratório."""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from qnetlab.dataplane.routeros import RouterOsClient
from qnetlab.errors import QNetLabError


def wait_until(predicate: Callable[[], Any], timeout: float, interval: float = 3.0, what: str = "condição") -> Any:
    """Repete ``predicate`` até devolver algo verdadeiro; devolve esse valor. Estoura com mensagem clara."""
    deadline = time.time() + timeout
    last: Optional[BaseException] = None
    while True:
        try:
            val = predicate()
            if val:
                return val
        except (QNetLabError, OSError) as exc:          # roteador reiniciando, contêiner subindo, REST ainda indisponível ...
            last = exc
        if time.time() >= deadline:
            raise AssertionError(f"Tempo esgotado ({timeout:.0f}s) esperando: {what}" + (f" [último erro: {last}]" if last else ""))
        time.sleep(interval)


def router(lab, name: str) -> RouterOsClient:
    c = lab.plan.chr(name)
    if c.mgmt_ip is None:
        raise AssertionError(f"{name} não tem IP de gerenciamento (addressing.mgmt_attach)")
    return RouterOsClient(str(c.mgmt_ip), lab.topo.router.admin_user, lab.topo.router.admin_password)


def ospf_full(lab, name: str) -> int:
    return sum(1 for n in router(lab, name).ospf_neighbors() if str(n.get("state", "")).lower() == "full")


def node_ping(lab, node: str, dst: str, count: int = 10) -> str:
    """ping a partir do NODE (docker exec); o tráfego NODE<->NODE é o que casa com as políticas IPsec."""
    return lab.docker(required=True).exec(node, ["ping", "-c", str(count), "-W", "2", "-i", "0.5", dst], timeout=60 + count)
