"""Construção das imagens Docker (base + uma camada fina por nó com seus certificados).

* ``qnetlab/node:base`` — Python, ferramentas de rede, código KME e agente SAE embutidos;
* ``qnetlab/<lab>-<nó>:latest`` — base + ``/certs`` do nó (+ código-fonte customizado opcional).

Injetar certificados em uma camada por nó evita depender de bind mounts, que os nós
Docker do GNS3 não oferecem, e torna a imagem do experimento autocontida e versionável.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import List

from ..addressing import AddressPlan
from ..config.models import Topology
from ..errors import ImageBuildError
from ..paths import node_context_dir
from ..pki import CertificateAuthority
from .env import kme_environment
from .kme_config import render_kme_config
from ..rendering import render
from ..workspace import Workspace

log = logging.getLogger(__name__)


class ImageBuilder:
    def __init__(self, topo: Topology, plan: AddressPlan, ws: Workspace, ca: CertificateAuthority,
                 docker: str = "docker"):
        self.topo, self.plan, self.ws, self.ca = topo, plan, ws, ca
        self.docker_bin = shutil.which(docker)

    # ----------------------------------------------------------------- docker
    def _docker(self, *args: str, timeout: int = 1800) -> None:
        if not self.docker_bin:
            raise ImageBuildError("Docker não encontrado no PATH.")
        cmd = [self.docker_bin, *args]
        log.info("$ %s", " ".join(cmd))
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ImageBuildError(f"Falha ao executar docker: {exc}") from exc
        if res.returncode != 0:
            tail = "\n".join((res.stdout + res.stderr).strip().splitlines()[-25:])
            raise ImageBuildError(f"docker {args[0]} falhou:\n{tail}")

    def build_base(self) -> str:
        tag = self.topo.containers.base_image
        self._docker("build", "-t", tag, str(node_context_dir()))
        return tag

    # ------------------------------------------------------------ per-node ctx
    def _copy_source(self, source_dir: str, dest: Path) -> None:
        src = Path(source_dir).expanduser()
        if not src.is_dir():
            raise ImageBuildError(f"source_dir inexistente: {src}")
        shutil.copytree(src, dest)

    def prepare_kme_context(self, site: int) -> Path:
        """Contexto de build de um KME: certificados + kme_config.yaml (peers, seeds por par, topologia)."""
        kme = self.plan.sites[site].kme
        ctx = self.ws.docker_dir / kme.name
        if ctx.exists():
            shutil.rmtree(ctx)
        (ctx / "certs").mkdir(parents=True)
        shutil.copy2(self.ca.ca_cert, ctx / "certs" / "ca.crt.pem")
        shutil.copy2(self.ca.cert_path(kme.kme_id), ctx / "certs")
        key_dst = ctx / "certs" / self.ca.key_path(kme.kme_id).name
        shutil.copy2(self.ca.key_path(kme.kme_id), key_dst)
        key_dst.chmod(0o600)
        custom = bool(self.topo.containers.kme_source_dir)
        if custom:
            self._copy_source(self.topo.containers.kme_source_dir, ctx / "kme_src")
        self.ws.write_text(ctx / "kme_config.yaml", render_kme_config(self.topo, self.plan, self.ws, site), mode=0o600)
        dockerfile = render("docker/Dockerfile.node.j2", base_image=self.topo.containers.base_image,
                            has_certs=True, is_kme=True, custom_kme=custom, custom_app=False)
        self.ws.write_text(ctx / "Dockerfile", dockerfile)
        return ctx

    def prepare_node_context(self) -> Path:
        """Contexto do NODE (só quando containers.app_source_dir está definido): base + agente/código próprio."""
        ctx = self.ws.docker_dir / "NODE"
        if ctx.exists():
            shutil.rmtree(ctx)
        ctx.mkdir(parents=True)
        self._copy_source(self.topo.containers.app_source_dir, ctx / "app_src")
        dockerfile = render("docker/Dockerfile.node.j2", base_image=self.topo.containers.base_image,
                            has_certs=False, is_kme=False, custom_kme=False, custom_app=True)
        self.ws.write_text(ctx / "Dockerfile", dockerfile)
        return ctx

    def build_nodes(self) -> List[str]:
        tags = []
        for sid, sp in self.plan.sites.items():
            ctx = self.prepare_kme_context(sid)
            tag = self.topo.image_tag(sp.kme.name)
            self._docker("build", "-t", tag, str(ctx))
            tags.append(tag)
        if self.topo.containers.app_source_dir:
            tag = self.topo.node_image
            self._docker("build", "-t", tag, str(self.prepare_node_context()))
            tags.append(tag)
        return tags

    def build_all(self) -> List[str]:
        return [self.build_base(), *self.build_nodes()]

    # ---------------------------------------------------------------- compose
    def render_compose(self) -> Path:
        """docker-compose.yml só com os KMEs numa rede plana (teste rápido sem GNS3).

        Cada KME recebe um config "plano" montado por volume: os vizinhos KMA são alcançados pelo nome DNS
        do serviço (que consta do SAN do certificado), já que os /30 do GNS3 não existem aqui.
        """
        nodes = []
        for sid, sp in self.plan.sites.items():
            cfg = self.ws.write_text(self.ws.compose_dir / f"kme_config.{sp.kme.kme_id}.yaml",
                                     render_kme_config(self.topo, self.plan, self.ws, sid, flat=True), mode=0o600)
            nodes.append({"name": sp.kme.kme_id, "image": self.topo.image_tag(sp.kme.name), "config": cfg.name,
                          "env": kme_environment(self.topo, self.plan, self.ws, sid, gns3=False)})
        content = render("docker/docker-compose.yml.j2", lab=self.topo.name, nodes=nodes)
        return self.ws.write_text(self.ws.compose_dir / "docker-compose.yml", content, mode=0o600)
