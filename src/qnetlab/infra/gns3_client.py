"""Cliente fino e robusto para a REST API do servidor GNS3 (porta 3080)."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

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
