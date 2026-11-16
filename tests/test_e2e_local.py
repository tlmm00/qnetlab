"""Ponta a ponta local (sem GNS3/Docker): 2 KMEs REAIS (o main.py do KMS-sim vendorizado), com mTLS,
sincronização KMA, agente do NODE e consolidação. Os kme_config.yaml são os gerados pelo QNetLab, com apenas
caminhos/portas/IPs ajustados para rodar em loopback."""
import base64
import json
import os
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("flask")
yaml = pytest.importorskip("yaml")

from qnetlab.addressing import build_plan
from qnetlab.config import load_topology
from qnetlab.containers.kme_config import render_kme_config
from qnetlab.pki import CertificateAuthority, entities_from_plan
from qnetlab.telemetry.plot_metrics import consolidate
from qnetlab.workspace import Workspace

ROOT = Path(__file__).resolve().parents[1]
NODE = ROOT / "docker" / "node"
KME_MAIN = NODE / "kme" / "main.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port: int, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            raise RuntimeError(f"processo terminou: {proc.stdout.read().decode()[-800:]}")
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise TimeoutError(port)


def local_config(text: str, site: int, peer_site: int, pki: Path, work: Path, ports: dict) -> Path:
    """Adapta o config gerado para loopback: caminhos temporários, só o par KMA do teste, sync rápido."""
    cfg = yaml.safe_load(text)
    me, peer = f"kme-sim-{site}", f"kme-sim-{peer_site}"
    cfg["listen"] = f"127.0.0.1:{ports[site]}"
    cfg["tls"] = {"server_cert_path": str(pki / f"{me}.crt.pem"), "server_key_path": str(pki / f"{me}.key.pem"),
                  "client_ca_cert_path": str(pki / "ca.crt.pem")}
    cfg["storage"]["params"]["path"] = str(work / f"{me}.db")
    cfg["logging"] = {"level": "INFO", "file_path": str(work / f"{me}.log")}
    ps = cfg["peer_sync"]
    ps.update(sync_interval_s=1, generation_delay_s=0, sync_threshold=20)
    ps["peers"] = {peer: {**ps["peers"][peer], "base_url": f"https://127.0.0.1:{ports[peer_site]}",
                          "client_cert_path": str(pki / f"{me}.crt.pem"),
                          "client_key_path": str(pki / f"{me}.key.pem"),
                          "server_ca_cert_path": str(pki / "ca.crt.pem")}}
    out = work / f"{me}.yaml"
    out.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return out


def start_kme(config: Path, port: int) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, str(KME_MAIN), "--config", str(config)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    wait_port(port, p)
    return p


def client_ctx(pki: Path, sae: str) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(pki / "ca.crt.pem"))
    ctx.load_cert_chain(str(pki / f"{sae}.crt.pem"), str(pki / f"{sae}.key.pem"))
    return ctx


def call(ctx, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"},
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, context=ctx, timeout=5) as r:
        return r.status, json.load(r)


def wait_synced(ctx, port: int, master: str, slave: str, timeout: float = 40.0) -> int:
    end = time.time() + timeout
    n = 0
    while time.time() < end:
        _, st = call(ctx, f"https://127.0.0.1:{port}/api/v1/keys/{slave}/status")
        n = st["stored_key_count"]
        if n > 0:
            return n
        time.sleep(0.5)
    raise TimeoutError(f"sem chaves sincronizadas para {master}->{slave}")


@pytest.fixture(scope="module")
def lab(tmp_path_factory, ring4_path):
    topo = load_topology(ring4_path)
    plan = build_plan(topo)
    work = tmp_path_factory.mktemp("lab")
    pki = work / "pki"
    ca = CertificateAuthority(pki, topo.pki)
    ca.issue_all(entities_from_plan(topo, plan))
    ws = Workspace(work, topo.name).ensure()
    ports = {1: free_port(), 2: free_port()}
    procs = []
    try:
        for site, peer in ((1, 2), (2, 1)):
            cfg = local_config(render_kme_config(topo, plan, ws, site), site, peer, pki, work, ports)
            procs.append(start_kme(cfg, ports[site]))
        wait_synced(client_ctx(pki, "sae-chr12"), ports[1], "sae-chr12", "sae-chr21")
        yield {"pki": pki, "p1": ports[1], "p2": ports[2], "work": work}
    finally:
        for p in procs:
            p.terminate()
            p.wait(timeout=10)


def test_enc_dec_roundtrip_between_kmes(lab):
    """Contrato ETSI 014: master (CHR MKT12) pede enc_keys no KME1; o escravo (MKT21) resolve no KME2."""
    pki = lab["pki"]
    st, body = call(client_ctx(pki, "sae-chr12"), f"https://127.0.0.1:{lab['p1']}/api/v1/keys/sae-chr21/enc_keys",
                    {"number": 1, "size": 256})
    assert st == 200
    key = body["keys"][0]
    assert len(base64.b64decode(key["key"])) == 32
    st, body2 = call(client_ctx(pki, "sae-chr21"), f"https://127.0.0.1:{lab['p2']}/api/v1/keys/sae-chr12/dec_keys",
                     {"key_IDs": [{"key_ID": key["key_ID"]}]})
    assert body2["keys"][0]["key"] == key["key"]         # chave idêntica nas duas pontas (seed compartilhada)


def test_routeros_style_small_size_is_bytes(lab):
    """RouterOS com key-size=32 pede size=32: o KME interpreta <=128 como bytes (=256 bits)."""
    pki = lab["pki"]
    st, enc = call(client_ctx(pki, "sae-chr12"), f"https://127.0.0.1:{lab['p1']}/api/v1/keys/sae-chr21/enc_keys",
                   {"number": 1, "size": 32})
    assert st == 200 and len(base64.b64decode(enc["keys"][0]["key"])) == 32


def test_mtls_required(lab):
    no_cert = ssl.create_default_context(cafile=str(lab["pki"] / "ca.crt.pem"))
    with pytest.raises((ssl.SSLError, urllib.error.URLError, ConnectionError)):
        call(no_cert, f"https://127.0.0.1:{lab['p1']}/api/v1/keys/sae-chr21/status")


def test_status_endpoint_identifies_sae_by_certificate_cn(lab):
    _, st = call(client_ctx(lab["pki"], "sae-chr12"), f"https://127.0.0.1:{lab['p1']}/api/v1/keys/sae-chr21/status")
    assert st["master_SAE_ID"] == "sae-chr12" and st["slave_SAE_ID"] == "sae-chr21"
    assert st["source_KME_ID"] == "kme-sim-1" and st["target_KME_ID"] == "kme-sim-2" and st["key_size"] == 256


def test_collector_key_pool_query_runs_against_real_kme_db(lab):
    """A consulta SQL do coletor (usada via docker exec) funciona no banco real do KME."""
    from qnetlab.telemetry.collector import TelemetryCollector
    out = subprocess.run([sys.executable, "-c", TelemetryCollector._KEY_STATUS, str(lab["work"] / "kme-sim-1.db")],
                         capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    rows = json.loads(out.stdout)
    assert any(peer == "kme-sim-2" and status == "synchronized" and n > 0 for peer, status, n in rows)


def test_node_agent_generates_load_and_metrics_are_consolidated(lab, tmp_path):
    """Agente do NODE (sondas UDP; sem requisição de chaves neste laboratório) + consolidação."""
    probe, ctl = free_port(), free_port()
    env = {**os.environ, "SAE_ID": "NODE1", "NODE_NAME": "NODE1", "KME_URL": "",
           "PEERS": "NODE2=127.0.0.1=1=", "PROBE_PORT": str(probe), "METRICS_PORT": str(ctl),
           "DATA_DIR": str(tmp_path / "data"), "KEY_SIZE_BITS": "256"}
    agent = subprocess.Popen([sys.executable, str(NODE / "sae" / "src" / "main.py")], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        wait_port(ctl, agent)
        base = f"http://127.0.0.1:{ctl}"
        req = urllib.request.Request(f"{base}/control/start", method="POST",
                                     data=json.dumps({"duration": 2, "interval_ms": 50, "size": 64,
                                                      "key_interval_s": 0}).encode())
        assert json.load(urllib.request.urlopen(req))["status"] == "started"
        end = time.time() + 15
        while time.time() < end and json.load(urllib.request.urlopen(f"{base}/control/status"))["running"]:
            time.sleep(0.3)
        raw = urllib.request.urlopen(f"{base}/metrics.jsonl").read().decode()
    finally:
        agent.terminate()
        agent.wait(timeout=10)
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "NODE1.jsonl").write_text(raw)
    s = consolidate(tmp_path / "m", tmp_path / "plots")
    probes = s["probes"][0]
    assert probes["dst"] == "NODE2" and probes["protected"] and probes["sent"] >= 20 and probes["loss_pct"] == 0
