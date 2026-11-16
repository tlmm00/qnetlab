"""Cliente RouterOS v7: REST (``/rest``) para comandos/consultas e FTP para enviar arquivos.

O ``.rsc`` renderizado é enviado por FTP e aplicado com ``/import`` (falha no primeiro
erro). O serviço FTP é habilitado só durante o envio e desabilitado ao final.
"""
from __future__ import annotations

import ftplib
import io
import logging
import re
import socket
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from ..errors import RouterOsError

log = logging.getLogger(__name__)


UPDATE_HOSTS = ("upgrade.mikrotik.com", "download.mikrotik.com")


class RouterOsClient:
    def __init__(self, host: str, user: str, password: str, timeout: float = 20.0, scheme: str = "http"):
        self.host, self.user, self.password, self.timeout = host, user, password, timeout
        self.base = f"{scheme}://{host}/rest"
        self.session = requests.Session()
        self.session.auth = (user, password)

    # ---------------------------------------------------------------- REST
    def _call(self, method: str, path: str, **kw: Any) -> Any:
        kw.setdefault("timeout", self.timeout)
        try:
            resp = self.session.request(method, f"{self.base}/{path.lstrip('/')}", **kw)
        except requests.RequestException as exc:
            raise RouterOsError(f"{self.host}: falha REST em {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise RouterOsError(f"{self.host}: {method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def get(self, path: str, **params: Any) -> Any:
        return self._call("GET", path, params=params or None)

    def wait_ready(self, timeout: float = 180.0) -> None:
        deadline = time.time() + timeout
        last: Optional[Exception] = None
        while time.time() < deadline:
            try:
                self._call("GET", "system/identity", timeout=5)
                return
            except RouterOsError as exc:
                last = exc
                time.sleep(3)
        raise RouterOsError(f"{self.host}: REST indisponível após {timeout:.0f}s ({last})")

    def version(self) -> str:
        """Versão do RouterOS (ex.: '7.22.1')."""
        res = self.get("system/resource")
        raw = str(res.get("version", "")) if isinstance(res, dict) else ""
        m = re.search(r"\d+\.\d+(?:\.\d+)?", raw)
        if not m:
            raise RouterOsError(f"{self.host}: não foi possível ler a versão do RouterOS ({raw!r}).")
        return m.group(0)

    def architecture(self) -> str:
        res = self.get("system/resource")
        return str(res.get("architecture-name", "x86")) if isinstance(res, dict) else "x86"

    def install_package(self, local: "Path", remote_name: str, minimum: str, timeout: float = 900.0) -> str:
        """Envia o .npk principal por FTP e reinicia; o RouterOS instala no boot. Espera a versão >= ``minimum``."""
        deadline = time.time() + timeout
        log.info("%s: enviando %s (%.1f MB) e reiniciando", self.host, remote_name, Path(local).stat().st_size / 1e6)
        self.upload({remote_name: local})
        try:
            self._call("POST", "system/reboot", json={}, timeout=15)
        except RouterOsError:
            pass                                          # o reinício derruba a conexão
        time.sleep(20)
        final = ""
        while time.time() < deadline:
            try:
                final = self.version()
            except RouterOsError:
                time.sleep(5)
                continue
            if self._vkey(final) >= self._vkey(minimum):
                return final
            time.sleep(5)                                 # ainda na versão antiga (reiniciando/instalando)
        raise RouterOsError(f"{self.host}: após {timeout:.0f}s o RouterOS está em {final or 'indisponível'}; "
                            f"esperado >= {minimum}. O CHR tem licença/espaço para o pacote?")

    @staticmethod
    def _vkey(v: str) -> tuple:
        return tuple(int(x) for x in (v.split(".") + ["0", "0"])[:3])

    def ensure_version(self, minimum: str, gateway: str, channel: str = "stable", dns: str = "",
                       timeout: float = 900.0) -> str:
        """Atualiza o RouterOS (Internet via NAT do GNS3) se a versão for menor que ``minimum``.

        Cria uma rota padrão e DNS temporários (removidos ao final), baixa o pacote do canal ``channel`` e
        reinicia; aguarda o REST voltar já na versão nova. Devolve a versão final.
        """
        have = self.version()
        if not minimum or self._vkey(have) >= self._vkey(minimum):
            return have
        deadline = time.time() + timeout
        log.info("%s: RouterOS %s < %s — atualizando pelo canal '%s'", self.host, have, minimum, channel)
        self.execute('/ip route remove [find where comment="qnetlab-upgrade"]')
        self.execute(f'/ip route add dst-address=0.0.0.0/0 gateway={gateway} comment=qnetlab-upgrade')
        self.execute(f'/ip dns set servers={dns or f"{gateway},1.1.1.1,8.8.8.8"}')
        # O DNS do NAT costuma falhar para o CHR; o host que roda o qnetlab resolve os nomes e os entrega como
        # entradas estáticas temporárias (removidas ao final), então a atualização não depende de DNS no guest.
        self.execute('/ip dns static remove [find where comment="qnetlab-upgrade"]')
        for host in UPDATE_HOSTS:
            try:
                addr = socket.gethostbyname(host)
            except OSError as exc:
                log.warning("%s: o host não resolveu %s (%s)", self.host, host, exc)
                continue
            self.execute(f'/ip dns static add name={host} address={addr} comment=qnetlab-upgrade')
        rebooting = False
        try:
            self._call("POST", "system/package/update/set", json={"channel": channel})
            self._call("POST", "system/package/update/check-for-updates", json={}, timeout=90)
            latest, status = "", ""
            while time.time() < deadline:
                st = self.get("system/package/update")
                st = st if isinstance(st, dict) else {}
                latest, status = str(st.get("latest-version", "")), str(st.get("status", ""))
                if latest and "checking" not in status.lower():
                    break
                if status.upper().startswith("ERROR"):          # falha rápida (DNS/rota), sem esperar o timeout
                    break
                time.sleep(3)
            if not latest or self._vkey(latest) < self._vkey(minimum):
                hint = self._diagnose_internet() if not latest else ""
                raise RouterOsError(
                    f"{self.host}: o canal '{channel}' oferece {latest or 'nenhuma versão'} (status: {status!r}), "
                    f"menor que {minimum}. {hint or 'Troque router.update_channel ou use uma imagem CHR mais nova.'}")
            log.info("%s: baixando RouterOS %s e reiniciando", self.host, latest)
            try:
                self._call("POST", "system/package/update/install", json={}, timeout=60)
            except RouterOsError:
                pass                                     # o reinício derruba a conexão REST
            rebooting = True
            time.sleep(20)
            final = have
            while time.time() < deadline:
                try:
                    final = self.version()
                except RouterOsError:
                    time.sleep(5)
                    continue
                if self._vkey(final) >= self._vkey(minimum):
                    break
                time.sleep(5)                            # ainda baixando/instalando
            else:
                raise RouterOsError(f"{self.host}: RouterOS continua em {final} após {timeout:.0f}s de atualização.")
            return final
        finally:
            try:
                if rebooting:
                    self.wait_ready(max(30.0, deadline - time.time()))
                self.execute('/ip route remove [find where comment="qnetlab-upgrade"]')
                self.execute('/ip dns static remove [find where comment="qnetlab-upgrade"]')
                self.execute('/ip dns set servers=""')
            except RouterOsError as exc:
                log.warning("%s: não foi possível remover a rota/DNS temporários: %s", self.host, exc)

    def _pings_ok(self, address: str) -> bool:
        try:
            res = self._call("POST", "ping", json={"address": address, "count": "3"}, timeout=40)
        except RouterOsError:
            return False
        rows = res if isinstance(res, list) else []
        return any(isinstance(r, dict) and (str(r.get("received", "0")) not in ("", "0") or "time" in r)
                   for r in rows)

    def _diagnose_internet(self) -> str:
        """Separa 'sem rota/NAT' de 'sem DNS' quando a checagem de atualização falha."""
        if not self._pings_ok("1.1.1.1"):
            return ("O CHR não alcança a Internet (ping 1.1.1.1 sem resposta): o NAT do GNS3 (virbr0) não está "
                    "roteando. No host: confira `virsh net-list --all` (rede 'default' ativa), "
                    "`sysctl net.ipv4.ip_forward` (=1) e as regras de masquerade/FORWARD do virbr0.")
        if not self._pings_ok("upgrade.mikrotik.com"):
            return ("Há Internet (ping 1.1.1.1 responde), mas o DNS não resolve upgrade.mikrotik.com: "
                    "o UDP/53 parece bloqueado entre o NAT e a Internet (mesmo com entradas DNS estáticas o download falhou). "
                    "Defina router.update_dns ou atualize a imagem do template do GNS3 para 7.22+.")
        return "A Internet e o DNS funcionam; verifique o canal (router.update_channel) e o acesso a upgrade.mikrotik.com."

    def require_version(self, minimum: str) -> str:
        """Falha com mensagem clara se o RouterOS for mais antigo que ``minimum`` (vazio = não checa)."""
        have = self.version()
        if minimum and self._vkey(have) < self._vkey(minimum):
            raise RouterOsError(
                f"{self.host}: RouterOS {have} é anterior ao mínimo exigido ({minimum}) para /ip ipsec key qkd e ppk=qkd. "
                "Atualize a imagem CHR do template do GNS3 (ou ajuste router.min_version; router.auto_update: true atualiza sozinho).")
        return have

    def execute(self, script: str, timeout: float = 120.0) -> str:
        """Executa um script RouterOS e devolve a saída textual."""
        res = self._call("POST", "execute", json={"script": script, "as-string": ""}, timeout=timeout)
        if isinstance(res, dict):
            return str(res.get("ret", ""))
        return str(res or "")

    # ----------------------------------------------------------------- FTP
    def _set_ftp(self, enabled: bool) -> None:
        self.execute(f"/ip service set ftp disabled={'no' if enabled else 'yes'}")

    def upload(self, files: Dict[str, "Path | bytes"]) -> None:
        """Envia arquivos (nome remoto -> caminho local ou bytes) via FTP."""
        self._set_ftp(True)
        try:
            with ftplib.FTP(timeout=self.timeout) as ftp:
                ftp.connect(self.host, 21)
                ftp.login(self.user, self.password)
                for remote, src in files.items():
                    data = src if isinstance(src, bytes) else Path(src).read_bytes()
                    ftp.storbinary(f"STOR {remote}", io.BytesIO(data))
        except (ftplib.all_errors, OSError) as exc:
            raise RouterOsError(f"{self.host}: falha no upload FTP: {exc}") from exc
        finally:
            try:
                self._set_ftp(False)
            except RouterOsError as exc:
                log.warning("%s: não foi possível desabilitar o FTP: %s", self.host, exc)
        self._wait_files(list(files), timeout=20)

    def list_files(self) -> List[str]:
        return [str(f.get("name", "")) for f in (self.get("file") or []) if isinstance(f, dict)]

    def _wait_files(self, names: List[str], timeout: float = 20.0) -> None:
        """Confirma que os arquivos enviados aparecem na tabela de arquivos do RouterOS (com o nome esperado)."""
        deadline = time.time() + timeout
        have: List[str] = []
        while True:
            have = self.list_files()
            missing = [n for n in names if n not in have]
            if not missing:
                return
            if time.time() >= deadline:
                raise RouterOsError(f"{self.host}: arquivos enviados por FTP não aparecem em /file: {missing}. "
                                    f"Arquivos presentes: {sorted(have)}")
            time.sleep(1)

    # ------------------------------------------------------------------- PKI
    def _import_file(self, file_name: str, retries: int = 5) -> None:
        """/certificate import via REST (erro claro e retentativa); não depende de um script .rsc."""
        last: Optional[RouterOsError] = None
        for _ in range(retries):
            try:
                self._call("POST", "certificate/import", json={"file-name": file_name, "passphrase": ""})
                return
            except RouterOsError as exc:
                last = exc
                time.sleep(2)
        raise RouterOsError(f"{self.host}: importação de {file_name} falhou: {last}. "
                            f"Arquivos em /file: {sorted(self.list_files())}")

    def install_pki(self, certs: List[Dict[str, Any]]) -> None:
        """Importa CA + certificado/chave do roteador (já enviados por FTP), nomeia e apaga os PEMs do disco."""
        for c in certs:
            self.execute(f'/certificate remove [find where name="{c["name"]}"]')
            self.execute(f'/certificate remove [find where name~"^{c["crt"]}"]')
        for c in certs:
            self._import_file(c["crt"])
            if c.get("key"):
                self._import_file(c["key"])
            self.execute(f'/certificate set [find where name~"^{c["crt"]}"] name={c["name"]}'
                         + (" trusted=yes" if c.get("trusted") else ""))
        for c in certs:
            for f in (c["crt"], c.get("key")):
                if f:
                    self.execute(f'/file remove [find where name="{f}"]')       # inclui chaves privadas

    def import_rsc(self, remote_name: str) -> str:
        """Aplica o .rsc. O RouterOS 7.22 não imprime 'executed successfully' no fim do /import verbose, então a
        falha é detectada pelas mensagens de erro ('Script Error', 'failure:') e não pela falta de uma de sucesso."""
        out = self.execute(f"/import file-name={remote_name} verbose=yes")
        low = out.lower()
        if "script error" in low or "failure:" in low:
            tail = out[-800:]
            raise RouterOsError(f"{self.host}: /import de {remote_name} falhou:\n{tail}")
        return out

    # ---------------------------------------------------------- observabilidade
    def log_entries(self, topics: str = "ipsec") -> List[Dict[str, Any]]:
        entries = self.get("log") or []
        return [e for e in entries if topics in e.get("topics", "")]

    def ospf_neighbors(self) -> List[Dict[str, Any]]:
        return self.get("routing/ospf/neighbor") or []

    def ipsec_active_peers(self) -> List[Dict[str, Any]]:
        return self.get("ip/ipsec/active-peers") or []

    def ipsec_installed_sa(self) -> List[Dict[str, Any]]:
        return self.get("ip/ipsec/installed-sa") or []

    def qkd_status(self) -> Dict[str, Any]:
        res = self.get("ip/ipsec/key/qkd")
        return res if isinstance(res, dict) else {"raw": res}
