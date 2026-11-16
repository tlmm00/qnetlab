"""Coleta de logs/métricas (roteadores, KMEs, APPs) e empacotamento em output.tar.gz."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, List

from ..addressing import AddressPlan
from ..config.models import Topology
from ..dataplane.routeros import RouterOsClient
from ..containers.dockerexec import DockerExec
from ..errors import QNetLabError, RouterOsError, TelemetryError
from ..workspace import Workspace
from .plot_metrics import consolidate

log = logging.getLogger(__name__)


class TelemetryCollector:
    def __init__(self, topo: Topology, plan: AddressPlan, ws: Workspace, docker: DockerExec | None = None):
        self.topo, self.plan, self.ws = topo, plan, ws
        self.docker = docker
        self.warnings: List[str] = []

    def _warn(self, msg: str) -> None:
        log.warning(msg)
        self.warnings.append(msg)

    # ------------------------------------------------------------ roteadores
    def collect_routers(self) -> None:
        for c in self.plan.chrs():
            if c.mgmt_ip is None:
                continue
            client = RouterOsClient(str(c.mgmt_ip), self.topo.router.admin_user, self.topo.router.admin_password)
            try:
                lines = [f"{e.get('time', '')} {e.get('topics', '')} {e.get('message', '')}"
                         for e in client.log_entries("ipsec")]
                (self.ws.logs_dir / f"{c.name}-ipsec.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
                snap = {
                    "ospf_neighbors": client.ospf_neighbors(),
                    "ipsec_active_peers": client.ipsec_active_peers(),
                    "ipsec_installed_sa": client.ipsec_installed_sa(),
                    "qkd": client.qkd_status(),
                }
                (self.ws.logs_dir / f"{c.name}-state.json").write_text(json.dumps(snap, indent=2), encoding="utf-8")
            except RouterOsError as exc:
                self._warn(f"{c.name}: coleta falhou ({exc})")

    # ------------------------------------------------------------- contêineres
    def collect_nodes(self) -> None:
        """Métricas dos agentes (docker exec + curl local)."""
        for sp in self.plan.sites.values():
            name = sp.node.name
            try:
                data = self.docker.http(name, "GET", self.topo.containers.metrics_port, "/metrics.jsonl")
                (self.ws.metrics_dir / f"{name}.jsonl").write_text(data, encoding="utf-8")
            except QNetLabError as exc:
                self._warn(f"{name}: coleta falhou ({exc})")

    _KEY_STATUS = (
        "import json,sqlite3,sys;c=sqlite3.connect(sys.argv[1]);"
        "print(json.dumps([list(r) for r in c.execute("
        "'SELECT peer_kme_id,status,COUNT(*) FROM kma_keys GROUP BY 1,2')]))"
    )

    def collect_kmes(self) -> None:
        """Log do KME (/var/log/kme.log) e nível dos pools de chaves por vizinho KMA (sqlite do KME)."""
        for sp in self.plan.sites.values():
            kme = sp.kme
            try:
                self.docker.cp_from(kme.name, "/var/log/kme.log", self.ws.logs_dir / f"{kme.name}.log")
            except QNetLabError as exc:
                self._warn(f"{kme.name}: log indisponível ({exc})")
            try:
                out = self.docker.exec(kme.name, ["python3", "-c", self._KEY_STATUS,
                                                  f"/opt/kme/data/{kme.kme_id.lower()}.db"])
                rows = json.loads(out or "[]")
                pools: Dict[str, Dict[str, int]] = {}
                for peer, status, n in rows:
                    pools.setdefault(peer, {})[status] = n
                (self.ws.logs_dir / f"{kme.name}-keys.json").write_text(
                    json.dumps({"kme_id": kme.kme_id, "pools": pools}, indent=2), encoding="utf-8")
            except (QNetLabError, ValueError) as exc:
                self._warn(f"{kme.name}: pools de chaves indisponíveis ({exc})")

    # ---------------------------------------------------------------- pacote
    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def package(self, plots_dir: Path) -> Path:
        out = self.ws.output_dir / "output.tar.gz"
        members: List[Path] = []
        for d in (self.ws.metrics_dir, self.ws.logs_dir, plots_dir, self.ws.rsc_dir):
            if d.exists():
                members += [p for p in sorted(d.rglob("*")) if p.is_file()]
        # .rsc contêm PSKs: vão para o pacote apenas com segredos mascarados
        manifest: Dict[str, Any] = {
            "lab": self.topo.name, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "warnings": self.warnings, "files": {},
        }
        with tarfile.open(out, "w:gz") as tar:
            for p in members:
                rel = p.relative_to(self.ws.root)
                if p.suffix == ".rsc":
                    data = self._mask_secrets(p.read_text(encoding="utf-8")).encode()
                    info = tarfile.TarInfo(str(rel))
                    info.size = len(data)
                    info.mtime = int(p.stat().st_mtime)
                    tar.addfile(info, io.BytesIO(data))
                    manifest["files"][str(rel)] = hashlib.sha256(data).hexdigest()
                else:
                    tar.add(p, arcname=str(rel))
                    manifest["files"][str(rel)] = self._sha256(p)
            blob = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(blob)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(blob))
        return out

    @staticmethod
    def _mask_secrets(text: str) -> str:
        text = re.sub(r'(secret=)"[^"]*"', r'\1"***"', text)
        text = re.sub(r'(ppk-secret=)"[^"]*"', r'\1"***"', text)
        text = re.sub(r'(password=)"[^"]*"', r'\1"***"', text)
        return text

    def run(self, collect_live: bool = True) -> Dict[str, Any]:
        for d in (self.ws.metrics_dir, self.ws.logs_dir, self.ws.output_dir):
            d.mkdir(parents=True, exist_ok=True)
        if collect_live:
            if self.docker is not None:
                self.collect_nodes()
                self.collect_kmes()
            else:
                self._warn("sem acesso Docker (projeto não implantado?): NODEs/KMEs não coletados")
            self.collect_routers()
        plots_dir = self.ws.root / "plots"
        try:
            summary = consolidate(self.ws.metrics_dir, plots_dir)
        except TelemetryError as exc:
            self._warn(f"sem métricas para consolidar: {exc}")
            summary = {}
        pkg = self.package(plots_dir)
        return {"package": str(pkg), "summary": summary, "warnings": self.warnings}
