"""Gns3Client: enlaces, nós e captura (requests simulado, sem servidor)."""
import socket
import threading
import time

import pytest

from qnetlab.config.models import Gns3Config
from qnetlab.errors import Gns3Error
from qnetlab.infra.gns3_client import Gns3Client


class Resp:
    def __init__(self, status=200, body=b"{}", chunks=()):
        self.status_code, self.content, self.text = status, body, body.decode()
        self._chunks = chunks

    def json(self): import json; return json.loads(self.content)
    def iter_content(self, chunk_size=0): return iter(self._chunks)
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture()
def client(monkeypatch):
    c = Gns3Client(Gns3Config())
    c.calls = []
    c.script = []

    def request(method, url, **kw):
        c.calls.append((method, url.replace(c.base, ""), kw.get("json")))
        return c.script.pop(0) if c.script else Resp()

    monkeypatch.setattr(c.session, "request", request)
    return c


def test_update_link_sends_partial_body(client):
    client.update_link("P", "L", {"suspend": True})
    assert client.calls == [("PUT", "/projects/P/links/L", {"suspend": True})]


def test_update_link_retries_with_nodes_when_server_demands_them(client):
    link = b'{"nodes":[{"node_id":"n1","adapter_number":1,"port_number":0},{"node_id":"n2","adapter_number":2}]}'
    client.script = [Resp(400, b'{"message":"nodes is required"}'), Resp(200, link), Resp(200)]
    client.update_link("P", "L", {"filters": {"delay": [5, 0]}})
    assert [c[0] for c in client.calls] == ["PUT", "GET", "PUT"]
    assert client.calls[2][2] == {"filters": {"delay": [5, 0]}, "nodes": [
        {"node_id": "n1", "adapter_number": 1, "port_number": 0}, {"node_id": "n2", "adapter_number": 2, "port_number": 0}]}


def test_update_link_does_not_mask_other_errors(client):
    client.script = [Resp(404, b"not found")]
    with pytest.raises(Gns3Error) as e:
        client.update_link("P", "L", {"suspend": True})
    assert e.value.status == 404 and len(client.calls) == 1


def test_node_action_endpoints_and_validation(client):
    for a in ("start", "stop", "suspend", "reload"):
        client.node_action("P", "N", a)
    assert [c[1] for c in client.calls] == [f"/projects/P/nodes/N/{a}" for a in ("start", "stop", "suspend", "reload")]
    with pytest.raises(Gns3Error, match="desconhecida"):
        client.node_action("P", "N", "explode")


class FakeGns3Socket:
    """Servidor TCP real em loopback. ``script``: lista de bytes enviados em sequência; ``close`` encerra a conexão no fim."""

    def __init__(self, script, close=True, pause=0.0):
        self.script, self.close, self.pause = script, close, pause
        self.request = b""
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.release = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.srv.accept()
        self.request = conn.recv(4096)
        for part in self.script:
            conn.sendall(part)
            time.sleep(self.pause)
        if self.close:
            conn.close()
        else:
            self.release.wait(10)
            conn.close()


def _gns3(port, **kw):
    return Gns3Client(Gns3Config(url=f"http://127.0.0.1:{port}", timeout=5, **kw))


GNS3_QUIRK = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: application/vnd.tcpdump.pcap\r\n\r\n"


def test_stream_raw_keeps_unframed_body_that_urllib3_rejects(tmp_path):
    inner = b"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.tcpdump.pcap\r\n\r\n" + b"\xd4\xc3\xb2\xa1" + b"X" * 20
    srv = FakeGns3Socket([GNS3_QUIRK, inner[:30], inner[30:]])               # cabeçalho chunked, corpo = resposta HTTP aninhada
    seen = []
    out = _gns3(srv.port, user="u", password="p").stream_raw("P", "L", tmp_path / "a" / "c.raw", on_connect=lambda: seen.append(1))
    assert out.read_bytes() == inner and seen == [1]
    assert srv.request.startswith(b"GET /v2/projects/P/links/L/pcap HTTP/1.1") and b"Authorization: Basic dTpw" in srv.request
    from qnetlab.capture import extract_pcap
    assert extract_pcap(out.read_bytes()).startswith(b"\xd4\xc3\xb2\xa1")


def test_stream_raw_http_error_carries_status(tmp_path):
    srv = FakeGns3Socket([b"HTTP/1.1 409 Conflict\r\nContent-Length: 40\r\n\r\n", b'{"message": "This link has no active capture"}'])
    with pytest.raises(Gns3Error) as e:
        _gns3(srv.port).stream_raw("P", "L", tmp_path / "c.raw")
    assert e.value.status == 409 and "no active capture" in str(e.value)


def test_stream_raw_ends_on_idle_when_stopping_even_if_server_keeps_connection(tmp_path):
    srv = FakeGns3Socket([GNS3_QUIRK, b"\xd4\xc3\xb2\xa1abcd"], close=False)
    flag = threading.Event()
    threading.Timer(0.5, flag.set).start()                                   # "stop_capture" chamado em 0,5 s
    t0 = time.time()
    out = _gns3(srv.port).stream_raw("P", "L", tmp_path / "c.raw", stopping=flag.is_set, idle_timeout=0.3)
    srv.release.set()
    assert out.read_bytes() == b"\xd4\xc3\xb2\xa1abcd" and time.time() - t0 < 3


def test_stream_raw_connection_refused_is_a_gns3_error(tmp_path):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    with pytest.raises(Gns3Error, match="Falha no fluxo"):
        _gns3(port).stream_raw("P", "L", tmp_path / "c.raw")


def test_capture_endpoints(client):
    client.start_capture("P", "L")
    client.stop_capture("P", "L")
    assert client.calls[0] == ("POST", "/projects/P/links/L/start_capture", {"data_link_type": "DLT_EN10MB"})
    assert client.calls[1][:2] == ("POST", "/projects/P/links/L/stop_capture")


def test_compute_node_action_returns_the_real_error_the_controller_hides(client, monkeypatch):
    class R:
        def __init__(self, code, body): self.status_code, self._b, self.text = code, body, str(body)
        def json(self): return self._b
    seen = []
    monkeypatch.setattr(client.session, "post", lambda url, **kw: (seen.append(url), R(409, {"message": "Could not fix permissions"}))[1])
    assert client.compute_node_action("P", "N", "docker", "stop") == "Could not fix permissions"
    assert seen[0].endswith("/v2/compute/projects/P/docker/nodes/N/stop")
    monkeypatch.setattr(client.session, "post", lambda url, **kw: R(204, {}))
    assert client.compute_node_action("P", "N", "docker", "stop") == "ok"
