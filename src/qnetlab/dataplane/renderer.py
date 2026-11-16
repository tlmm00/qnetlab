"""Renderização dos scripts RouterOS (.rsc) a partir dos templates Jinja2."""
from __future__ import annotations

from pathlib import Path
from typing import Dict

from ..addressing import AddressPlan
from ..config.models import Topology
from ..rendering import render
from ..workspace import Workspace
from .context import build_router_context


class RouterConfigRenderer:
    def __init__(self, topo: Topology, plan: AddressPlan, ws: Workspace):
        self.topo, self.plan, self.ws = topo, plan, ws

    def render_router(self, chr_name: str) -> str:
        return render("mikrotik/router.rsc.j2", **build_router_context(self.topo, self.plan, self.ws, chr_name))

    def render_bootstrap(self, chr_name: str) -> str:
        return render("mikrotik/bootstrap.rsc.j2", **build_router_context(self.topo, self.plan, self.ws, chr_name))

    def render_all(self) -> Dict[str, Path]:
        """Grava rsc/<MKTij>.rsc e rsc/<MKTij>.bootstrap.rsc (modo 0600: contêm segredos)."""
        out: Dict[str, Path] = {}
        for c in self.plan.chrs():
            out[f"{c.name}.rsc"] = self.ws.write_text(self.ws.rsc_dir / f"{c.name}.rsc", self.render_router(c.name), 0o600)
            out[f"{c.name}.bootstrap.rsc"] = self.ws.write_text(
                self.ws.rsc_dir / f"{c.name}.bootstrap.rsc", self.render_bootstrap(c.name), 0o600)
        return out
