"""Aplica bootstrap (console), configuração (REST/FTP) e verificação nos roteadores."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from ..addressing import AddressPlan
from ..containers.dockerexec import DockerExec
from ..config.models import Topology
from ..errors import QNetLabError, RouterOsError
from ..infra.console import RouterOsConsole
from ..pki import CertificateAuthority
from ..workspace import Workspace
from .context import router_files
from .package import fetch_package, latest_version
from .renderer import RouterConfigRenderer
from .routeros import RouterOsClient
from .verify import RouterExpectation, evaluate, expectation_for

log = logging.getLogger(__name__)


def _parallel(items: List[Any], fn: Callable[[Any], Any], label: str, workers: int = 8) -> Dict[Any, Any]:
    results: Dict[Any, Any] = {}
    errors: List[str] = []
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(items)))) as pool:
        futs = {pool.submit(fn, it): it for it in items}
        for fut in as_completed(futs):
            it = futs[fut]
            try:
                results[it] = fut.result()
            except QNetLabError as exc:
                errors.append(f"{it}: {exc}")
    if errors:
        raise RouterOsError(f"{label} falhou em {len(errors)} roteador(es):\n  " + "\n  ".join(errors))
    return results


class DataPlaneConfigurator:
    def __init__(self, topo: Topology, plan: AddressPlan, ws: Workspace, ca: CertificateAuthority,
                 docker: Optional[DockerExec] = None):
        self.topo, self.plan, self.ws, self.ca = topo, plan, ws, ca
        self.renderer = RouterConfigRenderer(topo, plan, ws)
        self.docker = docker

    def _client(self, chr_name: str) -> RouterOsClient:
        c = self.plan.chr(chr_name)
        if c.mgmt_ip is None:
            raise RouterOsError(f"{chr_name}: sem IP de gerenciamento (addressing.mgmt_attach=none).")
        return RouterOsClient(str(c.mgmt_ip), self.topo.router.admin_user, self.topo.router.admin_password)

    def _names(self) -> List[str]:
        return [c.name for c in self.plan.chrs()]

    # --------------------------------------------------------------- bootstrap
    def bootstrap(self) -> None:
        nodes = self.ws.load_state().get("nodes")
        if not nodes:
            raise RouterOsError("Estado sem nós: rode 'deploy' antes do bootstrap.")

        def one(name: str) -> str:
            info = nodes.get(name)
            if not info or not info.get("console"):
                raise RouterOsError(f"{name}: console não registrado no estado.")
            lines = [l for l in self.renderer.render_bootstrap(name).splitlines() if l.strip() and not l.startswith("#")]
            con = RouterOsConsole(info["console_host"], int(info["console"]), self.topo.router.admin_user,
                                  ["", self.topo.router.admin_password], self.topo.router.boot_timeout)
            try:
                con.login()
                con.run_many(lines)
            finally:
                con.close()
            self._client(name).wait_ready(60)
            return name

        done = _parallel(self._names(), one, "Bootstrap")
        log.info("Bootstrap concluído: %s", sorted(done.values()))

    # ----------------------------------------------------------------- upgrade
    def upgrade(self) -> Dict[str, str]:
        """Garante RouterOS >= router.min_version em todos os CHRs.

        ``update_method: host`` (padrão): o host baixa o .npk uma vez e o envia por FTP a cada CHR (sem depender de
        DNS/Internet no guest). ``device``: cada CHR baixa sozinho pelo NAT.
        """
        r = self.topo.router
        want = r.update_version or r.min_version              # versão fixada (update_version) tem prioridade

        def probe(name: str) -> tuple:
            client = self._client(name)
            client.wait_ready(120)
            return client.version(), client.architecture()

        info = _parallel(self._names(), probe, "Leitura da versão do RouterOS", workers=12)
        versions = {n: v for n, (v, _a) in info.items()}
        old = [n for n in self._names()
               if want and RouterOsClient._vkey(versions[n]) < RouterOsClient._vkey(want)]
        log.info("Versões do RouterOS: %s", dict(sorted(versions.items())))
        if not old:
            return versions
        if not r.auto_update:
            self._client(old[0]).require_version(want)                    # levanta o erro com a mensagem clara

        if r.update_method == "device":
            def dev(name: str) -> str:
                return self._client(name).ensure_version(want, str(self.plan.mgmt_gateway),
                                                         r.update_channel, r.update_dns, r.update_timeout)
            versions.update(_parallel(old, dev, "Atualização do RouterOS", workers=12))
            return versions

        target = r.update_version or latest_version(r.update_channel)
        if RouterOsClient._vkey(target) < RouterOsClient._vkey(want):
            raise RouterOsError(f"O canal '{r.update_channel}' oferece {target}, menor que a versão exigida "
                                f"({want}). Troque router.update_channel ou defina router.update_version.")
        log.info("Atualizando %d CHR(s) %s -> %s (pacote baixado uma vez no host)", len(old),
                 sorted({versions[n] for n in old}), target)
        pkgs = {arch: fetch_package(target, arch, self.ws.root / "cache")
                for arch in sorted({info[n][1] for n in old})}

        def host(name: str) -> str:
            pkg = pkgs[info[name][1]]
            return self._client(name).install_package(pkg, pkg.name, target, r.update_timeout)

        versions.update(_parallel(old, host, "Atualização do RouterOS", workers=8))
        if r.update_version:                                    # versão exata pedida
            off = {n: versions[n] for n in old if RouterOsClient._vkey(versions[n]) != RouterOsClient._vkey(target)}
            if off:
                raise RouterOsError(f"RouterOS esperado exatamente {target}, mas ficou assim: {off}")
        log.info("Versões após a atualização: %s", dict(sorted(versions.items())))
        return versions

    # --------------------------------------------------------------- configure
    def configure(self, upgrade: bool = True) -> None:
        if upgrade:                                   # `Lab.up` cronometra a atualização à parte e passa False
            self.upgrade()
        self.renderer.render_all()

        def one(name: str) -> str:
            client = self._client(name)
            client.wait_ready(120)
            log.info("%s: RouterOS %s", name, client.require_version(self.topo.router.min_version))
            files: Dict[str, Any] = {}
            for c in router_files(self.topo, self.plan, name):
                files[c["crt"]] = self.ca.dir / c["crt"]
                if c["key"]:
                    files[c["key"]] = self.ca.dir / c["key"]
            rsc_name = f"qnetlab-{name}.rsc"
            files[rsc_name] = self.ws.rsc_dir / f"{name}.rsc"
            client.upload(files)
            try:
                client.install_pki(router_files(self.topo, self.plan, name))
                client.import_rsc(rsc_name)
            finally:
                for fname in files:                                              # PSKs e chaves privadas
                    try:
                        client.execute(f'/file remove [find where name="{fname}"]')
                    except RouterOsError:
                        pass
            return name

        done = _parallel(self._names(), one, "Configuração")
        log.info("Roteadores configurados: %s", sorted(done.values()))

    # ------------------------------------------------------------------ verify
    def _trigger_tunnels(self) -> None:
        """Gera tráfego NODE->NODE que casa com as políticas para iniciar o IKE (modo policy-based).

        O tráfego precisa partir do NODE: pings do próprio roteador caem na regra de bypass.
        """
        if self.docker is None:
            log.debug("Sem acesso Docker: IKE não é disparado; ele começará com o primeiro tráfego NODE<->NODE.")
            return
        for t in self.plan.tunnels:
            if not t.protected:
                continue
            a = self.plan.chr(t.a)
            dst = self.plan.chr(t.b).ifaces["node"].peer_ip
            try:
                self.docker.exec(self.plan.sites[a.site].node.name, ["ping", "-c", "3", "-W", "2", str(dst)], timeout=30)
            except QNetLabError as exc:
                log.debug("ping de disparo %s -> %s falhou: %s", t.a, dst, exc)

    def _snapshot(self, client: RouterOsClient, exp: RouterExpectation) -> Dict[str, Any]:
        """Estado do CHR no formato de ``logs/MKT*-state.json`` (o mesmo que :func:`evaluate` recebe)."""
        protected = exp.protected
        return {
            "ospf_neighbors": client.ospf_neighbors(),
            "ipsec_active_peers": client.ipsec_active_peers(),          # também no grupo de controle (não pode haver peer)
            "ipsec_installed_sa": client.ipsec_installed_sa() if protected else [],
            "qkd": client.qkd_status() if protected and exp.ppk_mode == "qkd" else None,
        }

    def verify(self, timeout: float = 240.0) -> Dict[str, Any]:
        """Espera cada CHR atender aos critérios de :mod:`qnetlab.dataplane.verify` (OSPF, túnel **com PPK/QKD**,
        SA nos dois sentidos, grupo de controle sem IPsec). CHRs aprovados saem da espera; o relatório traz, por CHR,
        os contadores de sempre e ``ok``/``checks``/``reasons`` da última avaliação."""
        deadline = time.time() + timeout
        report: Dict[str, Any] = {}
        expect = {n: expectation_for(self.topo, self.plan, n) for n in self._names()}
        pending = set(expect)
        while pending and time.time() < deadline:
            self._trigger_tunnels()
            for name in sorted(pending):
                exp = expect[name]
                try:
                    snap = self._snapshot(self._client(name), exp)
                except RouterOsError as exc:
                    report[name] = {"ok": False, "error": str(exc), "reasons": [f"REST indisponível: {exc}"]}
                    continue
                verdict = evaluate(exp, snap)
                full = [n for n in snap["ospf_neighbors"] or [] if str(n.get("state", "")).lower() == "full"]
                report[name] = {
                    "ospf_full": len(full), "ospf_expected": exp.ospf_expected,
                    "ipsec_active_peers": len(snap["ipsec_active_peers"] or []),
                    "ipsec_installed_sa": len(snap["ipsec_installed_sa"] or []),
                    "ipsec_expected": int(exp.protected),
                    "qkd": snap["qkd"],
                    "ok": verdict.ok, "checks": verdict.checks, "reasons": verdict.reasons,
                }
                if verdict.ok:
                    pending.discard(name)
            if pending:
                time.sleep(5)
        report["_ok"] = not pending
        report["_not_ready"] = sorted(pending)
        report["_summary"] = {
            "routers": len(expect), "ok": len(expect) - len(pending),
            "ppk_mode": self.topo.ipsec.ppk_mode,
            "failed": {n: report.get(n, {}).get("reasons", ["sem avaliação"]) for n in sorted(pending)},
        }
        return report
