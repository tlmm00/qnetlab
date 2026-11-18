"""Injeção de falhas: enlaces (atraso, jitter, perda, corrupção, BPF, queda) e nós (parar, iniciar, pausar).

Usa só a API REST do GNS3 (``filters`` e ``suspend`` dos enlaces; ``start``/``stop``/``suspend``/``reload`` dos nós),
portanto vale para CHRs, KMEs e NODEs. Toda ação é registrada em ``logs/fault-events.jsonl`` (``ts`` em segundos
Unix), e, opcionalmente, também nos ``metrics.jsonl`` dos NODEs (evento ``fault``), para correlacionar a falha com
RTT, perda e taxa de chaves.

Semântica importante: ``set_link`` **substitui** todos os filtros do enlace (é o comportamento do GNS3); chame-a uma
vez com todos os parâmetros desejados. ``clear`` remove os filtros e religa o enlace.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from .addressing import AddressPlan
from .errors import Gns3Error, QNetLabError
from .infra.deployer import adapter_of
from .workspace import Workspace

log = logging.getLogger(__name__)

NODE_ACTIONS: Dict[str, tuple] = {
    # ação do usuário -> (ação GNS3, estado esperado depois)
    "stop": ("stop", "stopped"),
    "start": ("start", "started"),
    "restart": ("reload", "started"),
    "pause": ("suspend", "suspended"),
    "resume": ("start", "started"),
}


@dataclass(frozen=True)
class LinkRef:
    """Enlace do GNS3 com os nomes lógicos das pontas (ex.: ``MKT11:ether2``)."""
    id: str
    kind: str
    a: str
    a_if: str
    b: str
    b_if: str

    @property
    def label(self) -> str:
        return f"{self.a}:{self.a_if} <-> {self.b}:{self.b_if}"


# ----------------------------------------------------------------------- índice de enlaces
class LinkIndex:
    """Resolve ``NODE[:iface]`` (+ ``NODE[:iface]``, ``kind``) para enlaces do laboratório."""

    def __init__(self, links: Iterable[LinkRef]):
        self.links: List[LinkRef] = list(links)

    @classmethod
    def from_state(cls, state: Dict[str, Any]) -> "LinkIndex":
        return cls(LinkRef(l["id"], l["kind"], l["a"], l["a_if"], l["b"], l["b_if"]) for l in state.get("links", []))

    @classmethod
    def from_gns3(cls, gns3_links: Sequence[Dict[str, Any]], nodes: Dict[str, Dict[str, Any]],
                  plan: AddressPlan) -> "LinkIndex":
        """Reconstrói o índice a partir do GNS3 (laboratórios implantados antes de o ``state.json`` guardar os IDs)."""
        by_ends: Dict[frozenset, Dict[str, Any]] = {}
        for gl in gns3_links:
            ends = frozenset((n["node_id"], int(n["adapter_number"])) for n in gl.get("nodes", []))
            by_ends[ends] = gl
        out: List[LinkRef] = []
        for l in plan.links:
            try:
                ends = frozenset(((nodes[l.a]["id"], adapter_of(l.a_if)), (nodes[l.b]["id"], adapter_of(l.b_if))))
            except KeyError:
                continue
            gl = by_ends.get(ends)
            if gl:
                out.append(LinkRef(gl["link_id"], l.kind, l.a, l.a_if, l.b, l.b_if))
        return cls(out)

    def find(self, a: str, b: Optional[str] = None, kind: Optional[str] = None) -> List[LinkRef]:
        def split(x: str) -> tuple:
            node, _, iface = x.partition(":")
            return node.upper(), iface.lower()

        def end_ok(sel: tuple, node: str, iface: str) -> bool:
            return sel[0] == node.upper() and (not sel[1] or sel[1] == iface.lower())

        sa, sb = split(a), (split(b) if b else None)
        out = []
        for l in self.links:
            if kind and l.kind != kind:
                continue
            direct = end_ok(sa, l.a, l.a_if) and (sb is None or end_ok(sb, l.b, l.b_if))
            swapped = end_ok(sa, l.b, l.b_if) and (sb is None or end_ok(sb, l.a, l.a_if))
            if direct or swapped:
                out.append(l)
        return out

    def one(self, a: str, b: Optional[str] = None, kind: Optional[str] = None) -> LinkRef:
        found = self.find(a, b, kind)
        what = f"{a}" + (f" <-> {b}" if b else "") + (f" (kind={kind})" if kind else "")
        if not found:
            raise QNetLabError(f"Nenhum enlace casa com {what}. Use 'qnetlab fault list' para ver os enlaces.")
        if len(found) > 1:
            opts = "; ".join(f"{l.label} [{l.kind}]" for l in found)
            raise QNetLabError(f"{what} casa com {len(found)} enlaces; seja mais específico "
                               f"(NODE:iface, segundo nó ou --kind): {opts}")
        return found[0]


# ---------------------------------------------------------------------------- filtros
def _check(name: str, value: float, lo: float, hi: float) -> int:
    if not (lo <= value <= hi):
        raise QNetLabError(f"{name}={value} fora da faixa aceita pelo GNS3 [{lo}, {hi}]")
    return int(value)


def build_filters(delay_ms: Optional[float] = None, jitter_ms: float = 0, loss_pct: Optional[float] = None,
                  corrupt_pct: Optional[float] = None, drop_every: Optional[int] = None,
                  bpf: Optional[Sequence[str]] = None) -> Dict[str, List[Any]]:
    """Monta o objeto ``filters`` do GNS3 (chaves: delay, packet_loss, corrupt, frequency_drop, bpf)."""
    f: Dict[str, List[Any]] = {}
    if jitter_ms and delay_ms is None:
        raise QNetLabError("jitter exige delay (o GNS3 aplica atraso ± jitter).")
    if delay_ms is not None:
        f["delay"] = [_check("delay_ms", delay_ms, 0, 32767), _check("jitter_ms", jitter_ms, 0, 32767)]
    if loss_pct is not None:
        f["packet_loss"] = [_check("loss_pct", loss_pct, 0, 100)]
    if corrupt_pct is not None:
        f["corrupt"] = [_check("corrupt_pct", corrupt_pct, 0, 100)]
    if drop_every is not None:
        f["frequency_drop"] = [_check("drop_every", drop_every, -1, 32767)]
    if bpf:
        f["bpf"] = ["\n".join(bpf)]            # uma expressão por linha; o filtro DESCARTA o que casar
    return f


# ---------------------------------------------------------------------------- injetor
class FaultInjector:
    def __init__(self, client: Any, project_id: str, nodes: Dict[str, Dict[str, Any]], links: LinkIndex,
                 ws: Optional[Workspace] = None, docker: Any = None, marker_nodes: Sequence[str] = (),
                 marker_port: int = 9100, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
        self.client, self.project_id, self.nodes, self.links = client, project_id, nodes, links
        self.ws, self.docker, self.marker_nodes = ws, docker, list(marker_nodes)
        self.marker_port = marker_port
        self._down_nodes: Dict[str, str] = {}              # nós parados/pausados por este injetor
        self._sleep, self._clock = sleep, clock

    # ------------------------------------------------------------------ registro
    def _record(self, action: str, target: str, **params: Any) -> Dict[str, Any]:
        ev = {"ts": round(self._clock(), 3), "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self._clock())),
              "action": action, "target": target, "params": params}
        log.info("fault: %s %s %s", action, target, params or "")
        if self.ws is not None:
            self.ws.logs_dir.mkdir(parents=True, exist_ok=True)
            with open(self.ws.logs_dir / "fault-events.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ev) + "\n")
        if self.docker is not None:
            payload = {"type": "fault", "action": action, "target": target, "params": params}
            for n in self.marker_nodes:
                try:
                    self.docker.http(n, "POST", self.marker_port, "/event", payload, timeout=10)
                except QNetLabError as exc:                      # NODE parado/pausado: sem marcador, sem erro
                    log.debug("marcador de falha não entregue a %s: %s", n, exc)
        return ev

    # ------------------------------------------------------------------- enlaces
    def set_link(self, ref: LinkRef, **filters: Any) -> Dict[str, Any]:
        """Aplica atraso/perda/corrupção/BPF (substitui os filtros anteriores). Retorna o ``filters`` aplicado."""
        built = build_filters(**filters)
        self.client.update_link(self.project_id, ref.id, {"filters": built})
        self._record("link-filters", ref.label, link=ref.id, filters=built)
        return built

    def down(self, ref: LinkRef) -> None:
        self.client.update_link(self.project_id, ref.id, {"suspend": True})
        self._record("link-down", ref.label, link=ref.id)

    def up(self, ref: LinkRef) -> None:
        self.client.update_link(self.project_id, ref.id, {"suspend": False})
        self._record("link-up", ref.label, link=ref.id)

    def clear(self, ref: LinkRef) -> None:
        self.client.update_link(self.project_id, ref.id, {"filters": {}, "suspend": False})
        self._record("link-clear", ref.label, link=ref.id)

    def clear_all(self) -> int:
        """Remove filtros e religa os enlaces que estiverem alterados. Retorna quantos foram restaurados."""
        n = 0
        for gl in self.client.list_links(self.project_id):
            if gl.get("filters") or gl.get("suspend"):
                ref = next((l for l in self.links.links if l.id == gl["link_id"]), None)
                self.client.update_link(self.project_id, gl["link_id"], {"filters": {}, "suspend": False})
                self._record("link-clear", ref.label if ref else gl["link_id"], link=gl["link_id"])
                n += 1
        return n

    def link_state(self) -> List[Dict[str, Any]]:
        """Estado atual (filtros/suspensão/captura) de cada enlace conhecido."""
        live = {gl["link_id"]: gl for gl in self.client.list_links(self.project_id)}
        out = []
        for l in self.links.links:
            gl = live.get(l.id, {})
            out.append({**asdict(l), "filters": gl.get("filters") or {}, "suspended": bool(gl.get("suspend")),
                        "capturing": bool(gl.get("capturing"))})
        return out

    # --------------------------------------------------------------------- nós
    def node(self, name: str, action: str, wait: bool = True, timeout: float = 120.0) -> Dict[str, Any]:
        """stop | start | restart | pause | resume em um nó (CHR, KME, NODE ou NAT)."""
        if action not in NODE_ACTIONS:
            raise QNetLabError(f"Ação inválida '{action}'. Use: {', '.join(NODE_ACTIONS)}")
        info = self.nodes.get(name) or self.nodes.get(name.upper())
        if not info:
            raise QNetLabError(f"Nó '{name}' não existe. Nós: {', '.join(sorted(self.nodes))}")
        gns3_action, expected = NODE_ACTIONS[action]
        self._record(f"node-{action}", name, node=info["id"])
        self.client.node_action(self.project_id, info["id"], gns3_action)
        if action in ("stop", "pause"):
            self._down_nodes[name] = action
        else:
            self._down_nodes.pop(name, None)
        if wait:
            try:
                self.client.wait_node_status(self.project_id, info["id"], expected, timeout)
            except Gns3Error as exc:
                raise QNetLabError(f"{exc}\n{self._diagnose_node(name, info, gns3_action)}") from exc
        return {"node": name, "action": action, "state": expected if wait else "requested"}

    def _diagnose_node(self, name: str, info: Dict[str, Any], gns3_action: str = "") -> str:
        """Diagnóstico quando um nó não chega ao estado pedido. O controlador do GNS3 responde 204 ao stop/start mesmo se o
        compute falhar; por isso repetimos a ação direto no compute (só para nós Docker) para obter o erro real."""
        lines, node_type = [], ""
        try:
            n = self.client.get_node(self.project_id, info["id"])
            node_type = n.get("node_type") or ""
            lines.append(f"GNS3: status={n.get('status')} tipo={node_type} compute={n.get('compute_id')}")
        except Exception as exc:                         # diagnóstico nunca mascara o erro original
            lines.append(f"GNS3: não foi possível consultar o nó ({exc})")
        if self.docker is not None and hasattr(self.docker, "state"):
            try:
                lines.append(f"Docker: {self.docker.state(name)}")
            except Exception as exc:
                lines.append(f"Docker: não foi possível consultar ({exc})")
        if node_type == "docker" and gns3_action in ("stop", "start") and hasattr(self.client, "compute_node_action"):
            try:
                res = self.client.compute_node_action(self.project_id, info["id"], "docker", gns3_action)
            except Exception as exc:
                res = f"falha ao consultar o compute ({exc})"
            lines.append(f"Compute do GNS3 ({gns3_action} direto): {res}")
            if "'docker'" in res and "No such file" in res:
                lines.append("CAUSA: o processo do servidor GNS3 não encontra o binário 'docker' (ele roda 'docker exec' ao parar "
                             "um nó). Se o servidor roda em um CONTÊINER Docker, monte o cliente nele "
                             "(-v /usr/bin/docker:/usr/bin/docker:ro, além do socket /var/run/docker.sock) e recrie o contêiner; "
                             "se roda no host, ponha 'docker' no PATH dele (ex.: link em /usr/local/bin/docker). "
                             "Para saber onde ele roda: sudo head -1 /proc/$(pgrep -f gns3server | head -1)/cgroup")
        else:
            lines.append("O GNS3 responde 204 ao stop/start mesmo quando o compute falha: veja o log do servidor GNS3.")
        return "\n".join(lines)

    def restore(self) -> Dict[str, Any]:
        """Desfaz tudo: remove filtros, religa enlaces e reinicia os nós que ESTE injetor parou/pausou.

        Pensado para o ``teardown`` de experimentos (garante que uma falha de teste não deixa o laboratório quebrado).
        """
        restored_links = self.clear_all()
        started = []
        for name in list(self._down_nodes):
            self.node(name, "start")
            started.append(name)
        return {"links": restored_links, "nodes": started}
