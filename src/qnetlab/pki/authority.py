"""Camada de PKI: Root CA + certificados X.509 por entidade, via OpenSSL.

Cada KME recebe um certificado de servidor/cliente com CN = ``kme-id`` (ex.: ``kme-sim-1``) e SAN com
*todos* os IPs em que pode ser alcançado. Cada SAE (os CHRs: ``sae-chr11``...) recebe um
certificado de cliente com CN = ``sae-id``.

O RouterOS valida o certificado do KME pelo IP usado na conexão, por isso os SANs
são estritos e completos. Reexecuções são idempotentes: um certificado só é
reemitido se o conjunto de SANs/CN/tipo de chave mudou ou se estiver ausente.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List

from ..addressing import AddressPlan
from ..config.models import PkiConfig, Topology
from ..errors import PkiError

log = logging.getLogger(__name__)

@dataclass(frozen=True)
class Entity:
    """Entidade a ser certificada."""
    cn: str
    ips: tuple = ()
    dns: tuple = ()
    server: bool = False        # inclui EKU serverAuth (KMEs)

    def fingerprint(self, key_type: str) -> str:
        blob = json.dumps(
            {"cn": self.cn, "ips": sorted(self.ips), "dns": sorted(self.dns),
             "server": self.server, "key_type": key_type},
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()


def entities_from_plan(topo: Topology, plan: AddressPlan) -> List[Entity]:
    """Entidades certificadas: KMEs (servidor+cliente) e os SAEs, que neste laboratório são os CHRs."""
    out: List[Entity] = []
    for sid, sp in plan.sites.items():
        kme = sp.kme
        # O RouterOS valida o IP usado na conexão: todos os IPs do KME vão no SAN.
        out.append(Entity(
            cn=kme.kme_id,
            ips=("127.0.0.1", *[str(i.ip) for i in kme.ifaces.values()]),
            dns=(kme.kme_id, kme.name.lower(), "localhost"),
            server=True,
        ))
        for c in sp.chrs:
            ips = [str(i.ip) for i in c.ifaces.values()]
            if c.mgmt_ip is not None:
                ips.append(str(c.mgmt_ip))
            out.append(Entity(cn=c.sae, ips=tuple(ips), dns=(c.name.lower(),)))
    return out


class CertificateAuthority:
    def __init__(self, directory: Path, cfg: PkiConfig, openssl: str = "openssl"):
        self.dir = Path(directory)
        self.cfg = cfg
        self.openssl = shutil.which(openssl)
        if not self.openssl:
            raise PkiError("OpenSSL não encontrado no PATH.")
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    @property
    def ca_cert(self) -> Path:
        return self.dir / "ca.crt.pem"

    @property
    def ca_key(self) -> Path:
        return self.dir / "ca.key.pem"

    @property
    def _manifest(self) -> Path:
        return self.dir / "manifest.json"

    def cert_path(self, cn: str) -> Path:
        return self.dir / f"{cn}.crt.pem"

    def key_path(self, cn: str) -> Path:
        return self.dir / f"{cn}.key.pem"

    # ---------------------------------------------------------------- helpers
    def _run(self, *args: str, stdin: bytes | None = None) -> str:
        cmd = [self.openssl, *args]
        log.debug("openssl %s", " ".join(args))
        try:
            res = subprocess.run(cmd, input=stdin, capture_output=True, check=False, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PkiError(f"Falha ao executar OpenSSL: {exc}") from exc
        if res.returncode != 0:
            raise PkiError(f"OpenSSL falhou ({' '.join(args[:2])}): {res.stderr.decode(errors='replace').strip()}")
        return res.stdout.decode(errors="replace")

    def _genkey(self, path: Path) -> None:
        kt = self.cfg.key_type
        if kt.startswith("rsa-"):
            args = ["genpkey", "-algorithm", "RSA", "-pkeyopt", f"rsa_keygen_bits:{kt.split('-')[1]}"]
        else:
            curve = {"ec-p256": "P-256", "ec-p384": "P-384"}[kt]
            args = ["genpkey", "-algorithm", "EC", "-pkeyopt", f"ec_paramgen_curve:{curve}",
                    "-pkeyopt", "ec_param_enc:named_curve"]
        self._run(*args, "-out", str(path))
        os.chmod(path, 0o600)

    def _subject(self, cn: str) -> str:
        org = self.cfg.organization.replace("/", " ")
        return f"/C={self.cfg.country}/O={org}/CN={cn}"

    def _load_manifest(self) -> Dict[str, str]:
        if self._manifest.exists():
            return json.loads(self._manifest.read_text(encoding="utf-8"))
        return {}

    # -------------------------------------------------------------- operations
    def ensure_root(self, force: bool = False) -> None:
        if self.ca_cert.exists() and self.ca_key.exists() and not force:
            log.info("Root CA já existente: %s", self.ca_cert)
            return
        log.info("Gerando Root CA (%s)", self.cfg.key_type)
        self._genkey(self.ca_key)
        self._run(
            "req", "-x509", "-new", "-key", str(self.ca_key), "-sha384",
            "-days", str(self.cfg.ca_validity_days),
            "-subj", self._subject("QNetLab Root CA"),
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-addext", "subjectKeyIdentifier=hash",
            "-out", str(self.ca_cert),
        )
        # CA nova invalida tudo que foi emitido antes.
        if self._manifest.exists():
            self._manifest.unlink()

    def issue(self, ent: Entity, force: bool = False) -> bool:
        """Emite (ou reaproveita) o certificado da entidade. Retorna True se emitiu."""
        manifest = self._load_manifest()
        fp = ent.fingerprint(self.cfg.key_type)
        crt, key = self.cert_path(ent.cn), self.key_path(ent.cn)
        if not force and crt.exists() and key.exists() and manifest.get(ent.cn) == fp:
            log.debug("Certificado de %s já está atualizado", ent.cn)
            return False
        if not ent.ips and not ent.dns:
            raise PkiError(f"{ent.cn}: SAN vazio não é permitido.")

        self._genkey(key)
        with tempfile.TemporaryDirectory(prefix="qnetlab-pki-") as tmp:
            tmpd = Path(tmp)
            csr = tmpd / "req.csr"
            ext = tmpd / "ext.cnf"
            self._run("req", "-new", "-key", str(key), "-subj", self._subject(ent.cn), "-out", str(csr))
            eku = "serverAuth,clientAuth" if ent.server else "clientAuth"
            ku = "digitalSignature,keyEncipherment" if self.cfg.key_type.startswith("rsa") else "digitalSignature,keyAgreement"
            lines = [
                "basicConstraints=critical,CA:FALSE",
                f"keyUsage=critical,{ku}",
                f"extendedKeyUsage={eku}",
                "subjectKeyIdentifier=hash",
                "authorityKeyIdentifier=keyid",
                "subjectAltName=@alt",
                "[alt]",
            ]
            lines += [f"IP.{i}={ip}" for i, ip in enumerate(dict.fromkeys(ent.ips), 1)]
            lines += [f"DNS.{i}={d}" for i, d in enumerate(dict.fromkeys(ent.dns), 1)]
            ext.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._run(
                "x509", "-req", "-in", str(csr), "-CA", str(self.ca_cert), "-CAkey", str(self.ca_key),
                "-CAcreateserial", "-CAserial", str(self.dir / "ca.srl"),
                "-days", str(self.cfg.validity_days), "-sha384", "-extfile", str(ext),
                "-out", str(crt),
            )
        manifest[ent.cn] = fp
        self._manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        log.info("Certificado emitido: %s", ent.cn)
        return True

    def issue_all(self, entities: Iterable[Entity], force: bool = False) -> List[str]:
        self.ensure_root(force=force)
        issued = []
        for ent in entities:
            if self.issue(ent, force=force):
                issued.append(ent.cn)
        return issued

    # ------------------------------------------------------------ verification
    def verify_chain(self, cn: str) -> None:
        out = self._run("verify", "-CAfile", str(self.ca_cert), str(self.cert_path(cn)))
        if "OK" not in out:
            raise PkiError(f"Cadeia inválida para {cn}: {out.strip()}")

    def san_of(self, cn: str) -> List[str]:
        """Retorna os SANs (``IP:x`` / ``DNS:y``) de um certificado emitido."""
        out = self._run("x509", "-in", str(self.cert_path(cn)), "-noout", "-ext", "subjectAltName")
        items: List[str] = []
        for line in out.splitlines():
            if "Subject Alternative Name" in line:
                continue
            for part in line.split(","):
                part = part.strip().replace("IP Address:", "IP:")
                if part:
                    items.append(part)
        return items

    def verify_all(self, entities: Iterable[Entity]) -> None:
        for ent in entities:
            self.verify_chain(ent.cn)
            sans = set(self.san_of(ent.cn))
            expected = {f"IP:{ip}" for ip in ent.ips} | {f"DNS:{d}" for d in ent.dns}
            missing = expected - sans
            if missing:
                raise PkiError(f"{ent.cn}: SAN ausente(s) no certificado: {sorted(missing)}")
