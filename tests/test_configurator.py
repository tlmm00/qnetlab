"""Configurador com RouterOS falso (sem GNS3): ordem de arquivos, limpeza do .rsc e verificação por CHR."""
import subprocess

import pytest

from qnetlab.addressing import build_plan
from qnetlab.config import load_topology
from qnetlab.containers import DockerExec
from qnetlab.dataplane.configurator import DataPlaneConfigurator
from qnetlab.pki import CertificateAuthority, entities_from_plan
from qnetlab.workspace import Workspace


class FakeRos:
    """RouterOS falso. Com ``FakeRos.plan`` definido, responde como um CHR saudável daquele plano (peer com PPK,
    SA nos dois sentidos, key qkd com chaves recebidas). ``FakeRos.overrides[nome][método]`` troca uma resposta."""
    instances = {}
    plan = None
    overrides = {}

    def __init__(self, name):
        self.name, self.uploads, self.scripts, self.imported = name, {}, [], []
        FakeRos.instances[name] = self

    def _ov(self, method, default):
        return FakeRos.overrides.get(self.name, {}).get(method, default)

    def _tunnel(self):
        t = FakeRos.plan.tunnel_of(self.name) if FakeRos.plan else None
        if t is None:
            return None, None, None
        return t, str(t.local_addr(self.name)), str(t.remote_addr(self.name))

    def wait_ready(self, t=0): pass
    def require_version(self, m): return "7.22.1"
    def ensure_version(self, *a, **k): self.v = "7.24"; return "7.24"
    v = "7.22.1"
    def version(self): return self.v
    def architecture(self): return "x86"
    def install_package(self, local, remote, minimum, timeout=0):
        self.installed = (str(local), remote); self.v = minimum; return minimum
    def upload(self, files): self.uploads.update(files)
    def import_rsc(self, n): self.imported.append(n)
    def install_pki(self, certs): self.pki = [c['name'] for c in certs]
    def execute(self, s, timeout=0): self.scripts.append(s); return ""
    def ospf_neighbors(self):
        return self._ov("ospf_neighbors", [{"state": "Full"}, {"state": "Full"}])

    def ipsec_active_peers(self):
        t, local, remote = self._tunnel()
        default = ([{"remote-address": remote, "local-address": local, "state": "established", "ppk": "true"}]
                   if t is not None and t.protected else [])
        return self._ov("ipsec_active_peers", default)

    def ipsec_installed_sa(self):
        t, local, remote = self._tunnel()
        default = ([{"src-address": local, "dst-address": remote, "spi": "0x1"},
                    {"src-address": remote, "dst-address": local, "spi": "0x2"}] if t is not None else [])
        return self._ov("ipsec_installed_sa", default)

    def qkd_status(self):
        t, _l, _r = self._tunnel()
        plan = FakeRos.plan
        default = {}
        if t is not None:
            me = plan.chr(self.name)
            default = {"enabled": "true", "peer-sae-id": plan.chr(t.peer_of(self.name)).sae,
                       "kme-id": plan.sites[me.site].kme.kme_id, "total-keys-received": "15"}
        return self._ov("qkd_status", default)


@pytest.fixture()
def cfg(tmp_path, ring4_path, monkeypatch):
    FakeRos.instances.clear()
    FakeRos.overrides = {}
    topo = load_topology(ring4_path)
    plan = build_plan(topo)
    FakeRos.plan = plan
    ws = Workspace(tmp_path, topo.name).ensure()
    ca = CertificateAuthority(ws.pki_dir, topo.pki)
    ca.issue_all(entities_from_plan(topo, plan))
    pings = []

    def runner(cmd, timeout):
        pings.append(list(cmd))
        names = [f"GNS3.{sp.node.name}.pid" for sp in plan.sites.values()]
        return subprocess.CompletedProcess(cmd, 0, "\n".join(names), "")

    dp = DataPlaneConfigurator(topo, plan, ws, ca, docker=DockerExec("pid", runner=runner))
    monkeypatch.setattr(dp, "_client", lambda name: FakeRos.instances.get(name) or FakeRos(name))
    return dp, plan, pings


def test_configure_uploads_own_cert_ca_and_script_then_removes_script(cfg):
    dp, plan, _ = cfg
    dp.configure()
    assert len(FakeRos.instances) == 2 * len(plan.sites)
    r = FakeRos.instances["MKT11"]
    assert set(r.uploads) >= {"ca.crt.pem", "sae-chr11.crt.pem", "sae-chr11.key.pem", "qnetlab-MKT11.rsc"}
    assert not any(k.startswith("sae-chr12") for k in r.uploads)       # nunca o material de outro CHR
    assert r.pki == ["qnetlab-ca", "sae-chr11"]                         # PKI instalada antes do script
    assert r.imported == ["qnetlab-MKT11.rsc"]
    removed = " ".join(r.scripts)                                      # script (PSK) e PEMs apagados
    assert "qnetlab-MKT11.rsc" in removed and "sae-chr11.key.pem" in removed


def test_verify_ok_and_triggers_ike_from_node_container(cfg):
    dp, plan, pings = cfg
    rep = dp.verify(timeout=5)
    assert rep["_ok"] and len(rep) == 2 * len(plan.sites) + 3                 # CHRs + _ok, _not_ready, _summary
    pk = [c for c in pings if "ping" in c]
    assert len(pk) == len(plan.tunnels) and all("exec" in c and c[2].startswith("GNS3.NODE") for c in pk)
    assert rep["MKT11"]["ospf_expected"] == 2 and rep["MKT11"]["ipsec_expected"] == 1
    assert rep["MKT11"]["ok"] and rep["MKT11"]["reasons"] == []
    assert rep["MKT11"]["checks"]["ppk"] and rep["MKT11"]["checks"]["qkd_keys"]
    assert rep["_summary"] == {"routers": 2 * len(plan.sites), "ok": 2 * len(plan.sites), "ppk_mode": "qkd",
                               "failed": {}}


@pytest.mark.parametrize("method, value, reason", [
    ("ipsec_active_peers", [{"remote-address": "10.0.1.6", "state": "established", "ppk": "false"}], "SEM PPK"),
    ("qkd_status", {"enabled": "true", "peer-sae-id": "sae-chr21", "kme-id": "kme-sim-1",
                    "total-keys-received": "0"}, "total-keys-received=0"),
])
def test_verify_fails_when_tunnel_does_not_use_qkd(cfg, monkeypatch, method, value, reason):
    """Túnel estabelecido e com SA, mas sem PPK (ou sem chaves do KME): antes passava, agora reprova só esse CHR."""
    dp, plan, _ = cfg
    monkeypatch.setattr("qnetlab.dataplane.configurator.time.sleep", lambda s: None)
    t = plan.tunnel_of("MKT12")
    assert str(t.remote_addr("MKT12")) == "10.0.1.6"                       # premissa do caso parametrizado
    FakeRos.overrides = {"MKT12": {method: value}}
    rep = dp.verify(timeout=0.2)
    assert not rep["_ok"] and rep["_not_ready"] == ["MKT12"]
    assert rep["MKT12"]["ok"] is False and any(reason in r for r in rep["MKT12"]["reasons"])
    assert rep["MKT12"]["ipsec_installed_sa"] == 2                          # o critério antigo aprovaria
    assert list(rep["_summary"]["failed"]) == ["MKT12"] and rep["_summary"]["ok"] == 2 * len(plan.sites) - 1


def test_verify_reports_rest_errors_as_reasons(cfg, monkeypatch):
    from qnetlab.errors import RouterOsError
    dp, plan, _ = cfg
    monkeypatch.setattr("qnetlab.dataplane.configurator.time.sleep", lambda s: None)

    def boom():
        raise RouterOsError("192.168.122.10: falha REST")

    FakeRos("MKT11").ospf_neighbors = boom
    rep = dp.verify(timeout=0.2)
    assert rep["_not_ready"] == ["MKT11"] and "error" in rep["MKT11"]
    assert rep["_summary"]["failed"]["MKT11"][0].startswith("REST indisponível")


def test_routeros_client_pki_uses_rest_and_waits_for_files(monkeypatch):
    from qnetlab.dataplane.routeros import RouterOsClient
    from qnetlab.errors import RouterOsError
    c = RouterOsClient("1.2.3.4", "admin", "x")
    calls = []
    monkeypatch.setattr(c, "_call", lambda m, p, **kw: calls.append((m, p, kw.get("json"))) or [])
    monkeypatch.setattr(c, "execute", lambda s, timeout=0: calls.append(("exec", s)) or "")
    c.install_pki([{"name": "qnetlab-ca", "crt": "ca.crt.pem", "key": None, "trusted": True},
                   {"name": "sae-chr11", "crt": "sae-chr11.crt.pem", "key": "sae-chr11.key.pem", "trusted": False}])
    imports = [x[2]["file-name"] for x in calls if x[0] == "POST"]
    assert imports == ["ca.crt.pem", "sae-chr11.crt.pem", "sae-chr11.key.pem"]      # certificado antes da chave
    assert any(x[0] == "exec" and "name=qnetlab-ca trusted=yes" in x[1] for x in calls)
    assert any(x[0] == "exec" and 'remove [find where name="sae-chr11.key.pem"]' in x[1] for x in calls)
    # arquivos ausentes: erro lista o que existe
    monkeypatch.setattr(c, "list_files", lambda: ["other.txt"])
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(RouterOsError, match="Arquivos presentes"):
        c._wait_files(["ca.crt.pem"], timeout=0)


def test_require_version(monkeypatch):
    from qnetlab.dataplane.routeros import RouterOsClient
    from qnetlab.errors import RouterOsError
    c = RouterOsClient("1.2.3.4", "admin", "x")
    for raw, ok in (("7.22.1 (stable)", True), ("7.22 (stable)", True), ("7.23rc1 (testing)", True),
                    ("7.19.4 (long-term)", False), ("7.9 (stable)", False)):
        monkeypatch.setattr(c, "get", lambda p, _r=raw, **k: {"version": _r})
        if ok:
            assert c.require_version("7.22")
        else:
            with pytest.raises(RouterOsError, match="anterior ao mínimo"):
                c.require_version("7.22")
    assert c.require_version("")                       # checagem desligada


class ScriptedRos:
    """RouterOsClient com a máquina de estados de uma atualização: 7.19 -> (reinício) -> 7.24."""
    def __init__(self, latest="7.24", installed="7.19.4"):
        from qnetlab.dataplane.routeros import RouterOsClient
        self.c = RouterOsClient("1.2.3.4", "admin", "x")
        self.latest, self.installed, self.log, self.down = latest, installed, [], 0
        self.c.get = self.get
        self.c._call = self.call
        self.c.execute = lambda sc, timeout=0: self.log.append(sc) or ""
        self.c.wait_ready = lambda t=0: None

    def get(self, path, **kw):
        if path == "system/resource":
            if self.down:
                self.down -= 1
                from qnetlab.errors import RouterOsError
                raise RouterOsError("rebooting")
            return {"version": f"{self.installed} (stable)"}
        if path == "system/package/update":
            return {"status": "New version is available", "latest-version": self.latest}
        raise AssertionError(path)

    def call(self, method, path, **kw):
        self.log.append(f"{method} {path} {kw.get('json')}")
        if path.endswith("/install"):
            self.installed, self.down = self.latest, 2
            from qnetlab.errors import RouterOsError
            raise RouterOsError("connection reset")           # o reinício derruba o REST
        return []


def test_auto_update_runs_temporary_route_dns_install_and_cleanup(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("socket.gethostbyname", lambda h: {"upgrade.mikrotik.com": "159.148.147.251"}.get(h) or (_ for _ in ()).throw(OSError("nx")))
    r = ScriptedRos()
    assert r.c.ensure_version("7.22", "192.168.122.1", "stable") == "7.24"
    log = r.log
    i = lambda needle: next(n for n, x in enumerate(log) if needle in str(x))
    assert i("dst-address=0.0.0.0/0 gateway=192.168.122.1") < i("check-for-updates") < i("/install")
    assert "'channel': 'stable'" in " ".join(map(str, log))
    assert i("/install") < max(n for n, x in enumerate(log) if "remove [find where comment=\"qnetlab-upgrade\"]" in x)
    assert any("dns set servers=\"\"" in x for x in log)         # DNS temporário removido
    # nomes resolvidos NO HOST viram entradas estáticas temporárias (o DNS do NAT falhava no CHR)
    assert any("/ip dns static add name=upgrade.mikrotik.com address=159.148.147.251 comment=qnetlab-upgrade" in x for x in log)
    assert not any("name=download.mikrotik.com" in x for x in log)  # o host não resolveu: pulado
    assert i("dns static add") < i("check-for-updates")
    assert sum("dns static remove" in x for x in log) >= 2           # limpeza prévia + final


def test_auto_update_noop_when_recent(monkeypatch):
    r = ScriptedRos(installed="7.23.1")
    assert r.c.ensure_version("7.22", "192.168.122.1") == "7.23.1" and r.log == []


def test_auto_update_fails_when_channel_too_old_and_cleans_up(monkeypatch):
    from qnetlab.errors import RouterOsError
    monkeypatch.setattr("time.sleep", lambda s: None)
    r = ScriptedRos(latest="7.18")
    with pytest.raises(RouterOsError, match="canal 'long-term' oferece 7.18"):
        r.c.ensure_version("7.22", "192.168.122.1", "long-term")
    assert not any("/install" in str(x) for x in r.log)
    assert any("remove [find where comment=" in x for x in r.log)  # rota temporária removida mesmo no erro


def test_configure_runs_upgrade_first(cfg):
    dp, plan, _ = cfg
    dp.configure()
    versions = dp.upgrade()
    assert set(versions) == {c.name for c in plan.chrs()} and set(versions.values()) == {"7.22.1"}   # já ok: sem update


def _unpin(dp):
    """Os YAMLs fixam update_version=7.22.1; estes testes exercitam o modo 'versão mais nova do canal'."""
    import dataclasses
    dp.topo = dataclasses.replace(dp.topo, router=dataclasses.replace(dp.topo.router, min_version="7.22",
                                                                     update_version=""))


def _old_routers(monkeypatch, tmp_path):
    """Faz os CHRs falsos começarem em 7.16 e o 'download' devolver um .npk falso em cache."""
    calls = []
    monkeypatch.setattr(FakeRos, "v", "7.16.2", raising=False)

    def fake_fetch(version, arch, cache_dir, get=None):
        calls.append((version, arch, str(cache_dir)))
        f = tmp_path / f"routeros-{version}.npk"
        f.write_bytes(b"x")
        return f

    monkeypatch.setattr("qnetlab.dataplane.configurator.fetch_package", fake_fetch)
    monkeypatch.setattr("qnetlab.dataplane.configurator.latest_version", lambda ch, **k: calls.append(("latest", ch)) or "7.24.1")
    return calls


def test_upgrade_host_downloads_once_and_installs_on_every_old_router(cfg, monkeypatch, tmp_path):
    dp, plan, _ = cfg
    _unpin(dp)
    calls = _old_routers(monkeypatch, tmp_path)
    versions = dp.upgrade()
    assert set(versions.values()) == {"7.24.1"}
    assert [c for c in calls if c[0] != "latest"] == [("7.24.1", "x86", str(dp.ws.root / "cache"))]   # 1 download p/ 8 CHRs
    assert ("latest", "stable") in calls
    assert all(FakeRos.instances[c.name].installed[1] == "routeros-7.24.1.npk" for c in plan.chrs())


def test_upgrade_pinned_version_skips_lookup(cfg, monkeypatch, tmp_path):
    import dataclasses
    dp, plan, _ = cfg
    calls = _old_routers(monkeypatch, tmp_path)
    r = dataclasses.replace(dp.topo.router, min_version="7.22", update_version="7.23.1")
    dp.topo = dataclasses.replace(dp.topo, router=r)
    dp.upgrade()
    assert not any(c[0] == "latest" for c in calls) and calls[0][0] == "7.23.1"


def test_upgrade_aborts_when_channel_is_too_old(cfg, monkeypatch, tmp_path):
    from qnetlab.errors import RouterOsError
    dp, plan, _ = cfg
    _unpin(dp)
    calls = _old_routers(monkeypatch, tmp_path)
    monkeypatch.setattr("qnetlab.dataplane.configurator.latest_version", lambda ch, **k: "7.18.2")
    with pytest.raises(RouterOsError, match="menor que a versão exigida"):
        dp.upgrade()
    assert not any(FakeRos.instances[c.name].__dict__.get("installed") for c in plan.chrs())


def test_package_module(tmp_path):
    from qnetlab.dataplane import package as pk
    from qnetlab.errors import RouterOsError
    assert pk.package_filename("7.20.6", "x86_64") == "routeros-7.20.6.npk"
    assert pk.package_filename("7.20.6", "arm64") == "routeros-7.20.6-arm64.npk"

    class Resp:
        def __init__(self, text="", data=b""):
            self.text, self._d = text, data
        def raise_for_status(self): pass
        def iter_content(self, chunk_size): yield self._d
        def __enter__(self): return self
        def __exit__(self, *a): pass

    assert pk.latest_version("stable", get=lambda url, **k: Resp("7.20.6 1768000000")) == "7.20.6"
    with pytest.raises(RouterOsError, match="inesperada"):
        pk.latest_version("stable", get=lambda url, **k: Resp("<html>"))
    urls = []
    big = b"\0" * (6 * 1024 * 1024)
    p = pk.fetch_package("7.20.6", "x86", tmp_path / "c", get=lambda url, **k: urls.append(url) or Resp(data=big))
    assert p.name == "routeros-7.20.6.npk" and urls == ["https://download.mikrotik.com/routeros/7.20.6/routeros-7.20.6.npk"]
    pk.fetch_package("7.20.6", "x86", tmp_path / "c", get=lambda *a, **k: 1 / 0)        # cache: sem novo download
    with pytest.raises(RouterOsError, match="pequeno demais"):
        pk.fetch_package("9.9.9", "x86", tmp_path / "c", get=lambda url, **k: Resp(data=b"<html>404"))
    assert not list((tmp_path / "c").glob("*.part"))


def test_install_package_uploads_reboots_and_waits(monkeypatch, tmp_path):
    from qnetlab.dataplane.routeros import RouterOsClient
    from qnetlab.errors import RouterOsError
    monkeypatch.setattr("time.sleep", lambda s: None)
    c = RouterOsClient("1.2.3.4", "admin", "x")
    f = tmp_path / "routeros-7.20.6.npk"
    f.write_bytes(b"x" * 10)
    events, versions = [], iter(["7.16.2", RouterOsError("down"), "7.20.6"])
    monkeypatch.setattr(c, "upload", lambda files: events.append(("upload", list(files))))
    monkeypatch.setattr(c, "_call", lambda m, p, **kw: events.append((m, p)))

    def ver():
        v = next(versions)
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(c, "version", ver)
    assert c.install_package(f, f.name, "7.20", 60) == "7.20.6"
    assert events[0] == ("upload", ["routeros-7.20.6.npk"]) and ("POST", "system/reboot") in events


def test_auto_update_dns_failure_fails_fast_with_diagnosis(monkeypatch):
    from qnetlab.errors import RouterOsError
    monkeypatch.setattr("time.sleep", lambda s: None)
    r = ScriptedRos(latest="")
    r.get_orig = r.get

    def get(path, **kw):
        if path == "system/package/update":
            return {"status": "ERROR: could not resolve dns name (timeout)"}
        return r.get_orig(path, **kw)

    r.c.get = get
    real_call = r.call

    def call(method, path, **kw):
        if path == "ping":
            ok = kw["json"]["address"] == "1.1.1.1"          # IP responde, nome não: problema de DNS
            return [{"host": "1.1.1.1", "received": "1", "time": "10ms"}] if ok else [{"status": "timeout"}]
        return real_call(method, path, **kw)

    r.c._call = call
    with pytest.raises(RouterOsError, match="DNS não resolve upgrade.mikrotik.com"):
        r.c.ensure_version("7.22", "192.168.122.1", "stable")
    assert any("servers=192.168.122.1,1.1.1.1,8.8.8.8" in x for x in r.log if isinstance(x, str))
    assert any('dns set servers=""' in x for x in r.log if isinstance(x, str))         # limpeza


def test_auto_update_no_route_is_diagnosed_as_nat_problem(monkeypatch):
    from qnetlab.errors import RouterOsError
    monkeypatch.setattr("time.sleep", lambda s: None)
    r = ScriptedRos(latest="")
    r.c.get = lambda path, **kw: {"status": "ERROR: could not resolve dns name (timeout)"} \
        if path == "system/package/update" else r.get(path, **kw)
    with pytest.raises(RouterOsError, match="não alcança a Internet"):
        r.c.ensure_version("7.22", "192.168.122.1", "stable")


def test_exact_pinned_version_upgrades_below_target_and_verifies(cfg, monkeypatch, tmp_path):
    import dataclasses
    dp, plan, _ = cfg
    calls = _old_routers(monkeypatch, tmp_path)
    monkeypatch.setattr(FakeRos, "v", "7.22.0", raising=False)         # 7.22.0 satisfaz min 7.22, mas não o 7.22.1 fixado
    r = dataclasses.replace(dp.topo.router, min_version="7.22", update_version="7.22.1")
    dp.topo = dataclasses.replace(dp.topo, router=r)

    def install(self, local, remote, minimum, timeout=0):
        self.installed = (str(local), remote)
        self.v = minimum                                             # instala exatamente a versão pedida
        return minimum
    monkeypatch.setattr(FakeRos, "install_package", install)
    versions = dp.upgrade()
    assert set(versions.values()) == {"7.22.1"} and calls[0][0] == "7.22.1"
    assert not any(c[0] == "latest" for c in calls)
    assert all(FakeRos.instances[c.name].installed[1] == "routeros-7.22.1.npk" for c in plan.chrs())


def test_exact_version_mismatch_after_install_is_an_error(cfg, monkeypatch, tmp_path):
    import dataclasses
    from qnetlab.errors import RouterOsError
    dp, plan, _ = cfg
    _old_routers(monkeypatch, tmp_path)
    r = dataclasses.replace(dp.topo.router, min_version="7.22", update_version="7.22.1")
    dp.topo = dataclasses.replace(dp.topo, router=r)
    monkeypatch.setattr(FakeRos, "install_package", lambda self, l, rn, m, t=0: "7.23.0")
    with pytest.raises(RouterOsError, match="exatamente 7.22.1"):
        dp.upgrade()


def test_update_version_below_min_version_is_rejected(ring4_path):
    import yaml
    from qnetlab.config import parse_topology
    from qnetlab.errors import ConfigError
    raw = yaml.safe_load(ring4_path.read_text())
    raw["router"].update(min_version="7.23", update_version="7.22.1")
    with pytest.raises(ConfigError, match="update_version"):
        parse_topology(raw)


def test_import_rsc_success_without_success_banner_and_failure_detection(monkeypatch):
    from qnetlab.dataplane.routeros import RouterOsClient
    from qnetlab.errors import RouterOsError
    c = RouterOsClient("1.2.3.4", "admin", "x")
    ok = "#line 73\n/system logging action set memory memory-lines=10000\n#line 74\n/system logging add topics=ipsec"
    monkeypatch.setattr(c, "execute", lambda s, timeout=0: ok)
    assert c.import_rsc("x.rsc") == ok                               # termina na última linha, sem erro: sucesso
    for bad in ("#line 45\n/ip ipsec profile add x\nScript Error: expected end of command (line 45 column 117)",
                "failure: no such item"):
        monkeypatch.setattr(c, "execute", lambda s, timeout=0, _b=bad: _b)
        with pytest.raises(RouterOsError, match="falhou"):
            c.import_rsc("x.rsc")
