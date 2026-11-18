"""Cliente fino e robusto para a REST API do servidor GNS3 (porta 3080)."""
from __future__ import annotations

import base64
import logging
import socket
import ssl
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..config.models import Gns3Config
from ..errors import Gns3Error

log = logging.getLogger(__name__)


class Gns3Client:
    def __init__(self, cfg: Gns3Config):
        self.cfg = cfg
        self.base = f"{cfg.url.rstrip('/')}/v{cfg.api_version}"
        self.session = requests.Session()
        self.session.verify = cfg.verify_tls
        if cfg.user:
            self.session.auth = (cfg.user, cfg.password)
        retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(502, 503, 504),
                      allowed_methods=frozenset({"GET", "PUT", "DELETE"}))
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    # ----------------------------------------------------------------- núcleo
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base}{path}"
        kwargs.setdefault("timeout", self.cfg.timeout)
        try:
            resp = self.session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise Gns3Error(f"Falha de comunicação com o GNS3 em {url}: {exc}") from exc
        if resp.status_code >= 400:
            raise Gns3Error(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}",
                            status=resp.status_code, body=resp.text)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ------------------------------------------------------------- informações
    def version(self) -> Dict[str, Any]:
        return self._request("GET", "/version")

    def list_templates(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/templates")

    def find_nat_template(self) -> Optional[Dict[str, Any]]:
        """Template embutido do nó NAT (ícone de nuvem). None se o servidor não o oferecer."""
        tpls = self.list_templates()
        for t in tpls:
            if t.get("template_type") == "nat":
                return t
        return next((t for t in tpls if t.get("name") == "NAT"), None)

    def find_template(self, name: str) -> Dict[str, Any]:
        tpls = self.list_templates()
        exact = [t for t in tpls if t.get("name") == name]
        fuzzy = [t for t in tpls if name.lower() in t.get("name", "").lower()]
        found = exact or fuzzy
        if not found:
            names = sorted(t.get("name", "?") for t in tpls)
            raise Gns3Error(f"Template '{name}' não existe no GNS3. Disponíveis: {names}")
        if len(found) > 1 and not exact:
            log.warning("Template '%s' ambíguo; usando '%s'", name, found[0]["name"])
        return found[0]

    # ----------------------------------------------------------------- projetos
    def list_projects(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/projects")

    def get_project_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        return next((p for p in self.list_projects() if p.get("name") == name), None)

    def create_project(self, name: str) -> Dict[str, Any]:
        return self._request("POST", "/projects", json={"name": name})

    def open_project(self, project_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/projects/{project_id}/open", json={})

    def close_project(self, project_id: str) -> None:
        self._request("POST", f"/projects/{project_id}/close", json={})

    def delete_project(self, project_id: str) -> None:
        self._request("DELETE", f"/projects/{project_id}")

    # --------------------------------------------------------------------- nós
    def create_node_from_template(self, project_id: str, template_id: str, x: int, y: int) -> Dict[str, Any]:
        return self._request("POST", f"/projects/{project_id}/templates/{template_id}",
                             json={"x": x, "y": y, "compute_id": self.cfg.compute_id})

    def create_node(self, project_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = {"compute_id": self.cfg.compute_id, **payload}
        return self._request("POST", f"/projects/{project_id}/nodes", json=payload)

    def update_node(self, project_id: str, node_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", f"/projects/{project_id}/nodes/{node_id}", json=payload)

    def get_node(self, project_id: str, node_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/projects/{project_id}/nodes/{node_id}")

    def list_nodes(self, project_id: str) -> List[Dict[str, Any]]:
        return self._request("GET", f"/projects/{project_id}/nodes")

    def start_all(self, project_id: str) -> None:
        self._request("POST", f"/projects/{project_id}/nodes/start", json={}, timeout=self.cfg.start_timeout)

    def stop_all(self, project_id: str) -> None:
        self._request("POST", f"/projects/{project_id}/nodes/stop", json={}, timeout=self.cfg.start_timeout)

    #: ação lógica -> endpoint do GNS3 (``POST /projects/{id}/nodes/{id}/<endpoint>``)
    NODE_ACTIONS = {"start": "start", "stop": "stop", "suspend": "suspend", "reload": "reload"}

    def node_action(self, project_id: str, node_id: str, action: str) -> Optional[Dict[str, Any]]:
        """start | stop | suspend (pausa) | reload (reinicia) de UM nó."""
        if action not in self.NODE_ACTIONS:
            raise Gns3Error(f"Ação de nó desconhecida: {action} (use {sorted(self.NODE_ACTIONS)})")
        return self._request("POST", f"/projects/{project_id}/nodes/{node_id}/{self.NODE_ACTIONS[action]}",
                             json={}, timeout=self.cfg.start_timeout)

    def compute_node_action(self, project_id: str, node_id: str, node_type: str, action: str) -> str:
        """Chama a ação direto no *compute* (``/v2/compute/projects/{id}/<tipo>/nodes/{id}/<ação>``) e devolve o resultado.

        Serve para **diagnóstico**: o controlador do GNS3 2.2 engole os erros do compute em ``stop`` (responde 204 mesmo se
        falhar); falando com o compute o erro volta no corpo da resposta. Devolve ``"ok"`` ou a mensagem de erro.
        """
        url = f"{self.cfg.url.rstrip('/')}/v{self.cfg.api_version}/compute/projects/{project_id}/{node_type}/nodes/{node_id}/{action}"
        try:
            resp = self.session.post(url, json={}, timeout=self.cfg.start_timeout)
        except requests.RequestException as exc:
            return f"falha de comunicação: {exc}"
        if resp.status_code < 400:
            return "ok"
        try:
            return str(resp.json().get("message") or resp.text[:300])
        except ValueError:
            return resp.text[:300]

    def wait_node_status(self, project_id: str, node_id: str, status: str, timeout: float = 120.0) -> Dict[str, Any]:
        deadline = time.time() + timeout
        node: Dict[str, Any] = {}
        while time.time() < deadline:
            node = self.get_node(project_id, node_id)
            if node.get("status") == status:
                return node
            time.sleep(1)
        raise Gns3Error(f"Nó {node.get('name', node_id)} não chegou ao estado '{status}' em {timeout:.0f}s "
                        f"(atual: {node.get('status')})")

    def wait_started(self, project_id: str, timeout: Optional[float] = None) -> None:
        deadline = time.time() + (timeout or self.cfg.start_timeout)
        while time.time() < deadline:
            pending = [n["name"] for n in self.list_nodes(project_id) if n.get("status") != "started"]
            if not pending:
                return
            log.debug("Aguardando nós: %s", pending)
            time.sleep(2)
        raise Gns3Error(f"Nós não iniciaram a tempo: {pending}")

    # ------------------------------------------------------------------ enlaces
    def create_link(self, project_id: str, a: Dict[str, int | str], b: Dict[str, int | str]) -> Dict[str, Any]:
        return self._request("POST", f"/projects/{project_id}/links", json={"nodes": [a, b]})

    def list_links(self, project_id: str) -> List[Dict[str, Any]]:
        return self._request("GET", f"/projects/{project_id}/links")

    def get_link(self, project_id: str, link_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/projects/{project_id}/links/{link_id}")

    def update_link(self, project_id: str, link_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """``PUT`` parcial: ``{"filters": {...}}`` substitui TODOS os filtros; ``{"suspend": bool}`` liga/desliga o enlace.

        Se o servidor recusar o corpo parcial (HTTP 400, p. ex. por exigir ``nodes``), repete uma vez com as pontas atuais.
        """
        path = f"/projects/{project_id}/links/{link_id}"
        try:
            return self._request("PUT", path, json=payload)
        except Gns3Error as exc:
            if exc.status != 400 or "nodes" in payload:
                raise
            log.debug("PUT parcial recusado (%s); repetindo com 'nodes'", exc)
            current = self.get_link(project_id, link_id)
            ends = [{"node_id": n["node_id"], "adapter_number": n["adapter_number"],
                     "port_number": n.get("port_number", 0)} for n in current.get("nodes", [])]
            return self._request("PUT", path, json={**payload, "nodes": ends})

    # ------------------------------------------------------------------ captura
    def start_capture(self, project_id: str, link_id: str, data_link_type: str = "DLT_EN10MB") -> Dict[str, Any]:
        return self._request("POST", f"/projects/{project_id}/links/{link_id}/start_capture",
                             json={"data_link_type": data_link_type})

    def stop_capture(self, project_id: str, link_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/projects/{project_id}/links/{link_id}/stop_capture", json={})

    def stream_raw(self, project_id: str, link_id: str, dest: Path, on_connect: Optional[Callable[[], None]] = None,
                   stopping: Optional[Callable[[], bool]] = None, idle_timeout: float = 2.0) -> Path:
        """Grava em ``dest`` os bytes **crus** (depois dos cabeçalhos HTTP) de ``GET .../pcap``.

        Por que cru: o GNS3 2.2.56 anuncia ``Transfer-Encoding: chunked`` mas não enquadra o corpo (a primeira "linha de
        tamanho" é ``HTTP/1.1 200 OK``), o que faz o urllib3 falhar. Quem chama extrai o pcap com
        :func:`qnetlab.capture.extract_pcap`. Termina no fim do fluxo (GNS3 fecha a conexão após ``stop_capture``) ou
        quando ``stopping()`` é verdadeiro e não chega nada por ``idle_timeout`` s. O fluxo só existe com a captura ativa.
        """
        u = urlparse(self.base)
        https = u.scheme == "https"
        host, port = u.hostname or "127.0.0.1", u.port or (443 if https else 80)
        auth = ""
        if self.cfg.user:
            token = base64.b64encode(f"{self.cfg.user}:{self.cfg.password}".encode()).decode()
            auth = f"Authorization: Basic {token}\r\n"
        req = (f"GET {u.path}/projects/{project_id}/links/{link_id}/pcap HTTP/1.1\r\nHost: {u.netloc}\r\n"
               f"Accept: */*\r\nConnection: close\r\n{auth}\r\n").encode()
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            sock: socket.socket = socket.create_connection((host, port), timeout=self.cfg.timeout)
            if https:
                ctx = ssl.create_default_context() if self.cfg.verify_tls else ssl._create_unverified_context()
                sock = ctx.wrap_socket(sock, server_hostname=host)
            with sock, open(dest, "wb") as fh:
                sock.sendall(req)
                sock.settimeout(idle_timeout)
                head, started = b"", time.time()
                while True:
                    try:
                        data = sock.recv(1 << 16)
                    except socket.timeout:
                        if head is not None and time.time() - started > self.cfg.timeout and not fh.tell():
                            raise Gns3Error(f"GNS3 não respondeu ao fluxo da captura em {self.cfg.timeout:.0f}s")
                        if stopping and stopping():
                            break
                        continue
                    if not data:
                        break
                    if head is not None:                                   # ainda lendo os cabeçalhos HTTP
                        head += data
                        end = head.find(b"\r\n\r\n")
                        if end < 0:
                            continue
                        status = int(head.split(b" ", 2)[1])
                        if status >= 400:
                            body = head[end + 4:]
                            if not body:                                   # o corpo do erro pode vir no pacote seguinte
                                sock.settimeout(0.5)
                                try:
                                    body = sock.recv(4096)
                                except (socket.timeout, OSError):
                                    pass
                            raise Gns3Error(f"GET pcap -> HTTP {status}: {body[:200].decode(errors='replace')}",
                                            status=status)
                        data, head = head[end + 4:], None
                        if on_connect:
                            on_connect()
                    fh.write(data)
                    fh.flush()
        except (OSError, ValueError, IndexError) as exc:
            raise Gns3Error(f"Falha no fluxo da captura de {link_id}: {exc}") from exc
        return dest
