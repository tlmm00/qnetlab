"""Agente SAE/APP: gerador de carga + coletor de métricas locais (somente stdlib).

Funções:
* eco UDP (porta PROBE_PORT) para que os pares meçam RTT/perda;
* sob comando (``POST /control/start``) envia sondas UDP aos pares e, periodicamente,
  requisita chaves ao KME local por mTLS (latência de obtenção de chave);
* grava eventos JSON-lines em ``/data/metrics.jsonl`` e os serve em ``:METRICS_PORT/metrics.jsonl``;
* eventos de aplicação (ex.: acurácia de aprendizado distribuído) podem ser acrescentados com
  ``POST /event`` (corpo JSON) e entram no mesmo arquivo com ``type`` livre.

Variáveis: SAE_ID, NODE_NAME, KME_URL, PEERS ("nome=ip=protegido=sae_id,..."),
PROBE_PORT, METRICS_PORT, KEY_SIZE_BITS.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SAE_ID = os.environ.get("SAE_ID", "SAE_APP")
NODE = os.environ.get("NODE_NAME", SAE_ID)
KME_URL = os.environ.get("KME_URL", "")
PROBE_PORT = int(os.environ.get("PROBE_PORT", "9000"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9100"))
KEY_SIZE = int(os.environ.get("KEY_SIZE_BITS", "256"))
DATA = Path(os.environ.get("DATA_DIR", "/data"))
CERT_DIR = Path(os.environ.get("CERT_DIR", "/certs"))

PEERS = []
for item in filter(None, os.environ.get("PEERS", "").split(",")):
    parts = item.split("=")
    if len(parts) == 4:
        PEERS.append({"name": parts[0], "ip": parts[1], "protected": parts[2] == "1", "sae": parts[3]})

_lock = threading.Lock()
_stop = threading.Event()
_running = threading.Event()
_state = {"phase": "idle", "started": None, "params": {}}


def emit(record: dict) -> None:
    record.setdefault("t", time.time())
    record.setdefault("node", NODE)
    line = json.dumps(record, separators=(",", ":"))
    with _lock:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(DATA / "metrics.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# ------------------------------------------------------------------ eco UDP
def udp_echo() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", PROBE_PORT))
    while True:
        data, addr = sock.recvfrom(65535)
        try:
            sock.sendto(data, addr)
        except OSError:
            pass


# ------------------------------------------------------------------- sondas
def probe_loop(peer: dict, interval_s: float, size: int, duration_s: float) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1.0)
    pad = b"\0" * max(0, size - 16)
    seq, end = 0, time.time() + duration_s
    while time.time() < end and not _stop.is_set():
        seq += 1
        t0 = time.perf_counter()
        sock.sendto(struct.pack("!Id", seq, t0) + pad, (peer["ip"], PROBE_PORT))
        rtt = None
        deadline = time.time() + 1.0
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                break
            rseq = struct.unpack("!I", data[:4])[0]
            if rseq == seq:
                rtt = (time.perf_counter() - t0) * 1000.0
                break
        emit({"type": "probe", "peer": peer["name"], "protected": peer["protected"], "seq": seq,
              "rtt_ms": None if rtt is None else round(rtt, 3), "size": size})
        sleep = interval_s - (time.perf_counter() - t0)
        if sleep > 0:
            time.sleep(sleep)


# -------------------------------------------------------------------- chaves
def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=str(CERT_DIR / "ca.crt.pem"))
    ctx.load_cert_chain(str(CERT_DIR / f"{SAE_ID}.crt.pem"), str(CERT_DIR / f"{SAE_ID}.key.pem"))
    return ctx


def key_loop(peer: dict, interval_s: float, duration_s: float) -> None:
    if not KME_URL:
        return
    ctx = _ssl_ctx()
    end = time.time() + duration_s
    while time.time() < end and not _stop.is_set():
        url = f"{KME_URL}/api/v1/keys/{urllib.parse.quote(peer['sae'])}/enc_keys"
        body = json.dumps({"number": 1, "size": KEY_SIZE}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        t0 = time.perf_counter()
        ok, err = False, ""
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
                ok = resp.status == 200 and bool(json.load(resp).get("keys"))
        except Exception as exc:  # noqa: BLE001 - registramos qualquer falha como evento
            err = type(exc).__name__
        emit({"type": "key_request", "peer": peer["name"], "ok": ok, "error": err,
              "latency_ms": round((time.perf_counter() - t0) * 1000.0, 3)})
        _stop.wait(interval_s)


# -------------------------------------------------------------- controle HTTP
def start_load(params: dict) -> dict:
    if _running.is_set():
        return {"status": "busy"}
    interval = float(params.get("interval_ms", 100)) / 1000.0
    size = max(16, int(params.get("size", 64)))
    duration = float(params.get("duration", 60))
    key_interval = float(params.get("key_interval_s", 5))
    _stop.clear()
    _running.set()
    _state.update(phase="running", started=time.time(), params=params)
    emit({"type": "phase", "phase": "start", "params": params})

    def runner() -> None:
        threads = []
        for peer in PEERS:
            threads.append(threading.Thread(target=probe_loop, args=(peer, interval, size, duration), daemon=True))
        if PEERS:
            threads.append(threading.Thread(target=key_loop, args=(PEERS[0], key_interval, duration), daemon=True))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        emit({"type": "phase", "phase": "end"})
        _state.update(phase="idle")
        _running.clear()

    threading.Thread(target=runner, daemon=True).start()
    return {"status": "started", "peers": [p["name"] for p in PEERS]}


class Control(BaseHTTPRequestHandler):
    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/metrics.jsonl":
            f = DATA / "metrics.jsonl"
            data = f.read_bytes() if f.exists() else b""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/control/status":
            self._json(200, {"sae_id": SAE_ID, **_state, "running": _running.is_set()})
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "JSON inválido"})
            return
        if path == "/control/start":
            self._json(200, start_load(payload))
        elif path == "/control/stop":
            _stop.set()
            self._json(200, {"status": "stopping"})
        elif path == "/event":
            payload.setdefault("type", "event")
            emit(payload)
            self._json(200, {"status": "ok"})
        else:
            self.send_error(404)

    def log_message(self, *args: object) -> None:
        pass


def main() -> None:
    threading.Thread(target=udp_echo, daemon=True, name="udp-echo").start()
    srv = ThreadingHTTPServer(("0.0.0.0", METRICS_PORT), Control)
    emit({"type": "boot", "sae_id": SAE_ID, "peers": [p["name"] for p in PEERS]})
    print(f"[sae] {SAE_ID} pronto: eco UDP :{PROBE_PORT}, controle HTTP :{METRICS_PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
