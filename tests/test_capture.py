"""Captura: ciclo start/stop/download com cliente falso e resumo de .pcap sintético."""
import struct
import threading

import pytest

from qnetlab.errors import Gns3Error, QNetLabError
from qnetlab.capture import PacketCapture, PcapError, contains_bytes, iter_packets, summarize
from qnetlab.faults import LinkIndex, LinkRef
from qnetlab.workspace import Workspace


def _eth_ip(proto: int, payload: bytes, vlan: bool = False) -> bytes:
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), 0, 0, 64, proto, 0,
                     bytes([10, 0, 1, 1]), bytes([10, 0, 1, 2])) + payload
    eth = b"\x00" * 6 + b"\x11" * 6
    return eth + (b"\x81\x00\x00\x05" if vlan else b"") + b"\x08\x00" + ip


def _udp(sport, dport, data=b""):
    return struct.pack("!HHHH", sport, dport, 8 + len(data), 0) + data


def _tcp(sport, dport, data=b""):
    return struct.pack("!HH", sport, dport) + b"\x00" * 16 + data


def write_pcap(path, frames, linktype=1, big=False, nano=False):
    e = ">" if big else "<"
    magic = {(False, False): b"\xd4\xc3\xb2\xa1", (True, False): b"\xa1\xb2\xc3\xd4",
             (False, True): b"\x4d\x3c\xb2\xa1", (True, True): b"\xa1\xb2\x3c\x4d"}[(big, nano)]
    out = magic + struct.pack(e + "HHiIII", 2, 4, 0, 0, 65535, linktype)
    for i, f in enumerate(frames):
        out += struct.pack(e + "IIII", 100 + i, 500000 if not nano else 500000000, len(f), len(f)) + f
    path.write_bytes(out)
    return path


def test_summary_counts_protocols_and_flags_cleartext(tmp_path):
    frames = [_eth_ip(50, b"\x00\x00\x00\x01" + b"E" * 40),             # ESP
              _eth_ip(50, b"\x00\x00\x00\x01" + b"E" * 40),
              _eth_ip(17, _udp(500, 500, b"ike")),                      # IKE
              _eth_ip(17, _udp(4500, 4500, b"\0\0\0\0ike")),
              _eth_ip(89, b"ospf-hello"),                               # OSPF
              _eth_ip(17, _udp(40000, 9000, b"PROBE-SECRET")),          # sonda em claro
              _eth_ip(6, _tcp(1234, 80, b"GET /")),
              _eth_ip(1, b"\x08\x00" + b"\0" * 6)]
    s = summarize(write_pcap(tmp_path / "a.pcap", frames), needle=b"PROBE-SECRET")
    assert s["packets"] == 8 and s["esp_packets"] == 2 and s["ike_packets"] == 2 and s["ospf_packets"] == 1
    assert s["ip_protocols"]["udp"] == 1 and s["ip_protocols"]["tcp"] == 1 and s["ip_protocols"]["icmp"] == 1
    assert s["cleartext_data_packets"] == 3                               # udp não-IKE + tcp + icmp
    assert s["needle_hits"] == 1 and s["duration_s"] == 7.0


def test_summary_of_protected_traffic_has_no_cleartext(tmp_path):
    s = summarize(write_pcap(tmp_path / "p.pcap", [_eth_ip(50, b"\0\0\0\1" + b"X" * 60)] * 5 +
                             [_eth_ip(17, _udp(500, 500))] * 2), needle=b"PROBE")
    assert s["cleartext_data_packets"] == 0 and s["needle_hits"] == 0 and s["esp_packets"] == 5


@pytest.mark.parametrize("big,nano", [(False, False), (True, False), (False, True), (True, True)])
def test_pcap_endianness_and_nanosecond_variants(tmp_path, big, nano):
    p = write_pcap(tmp_path / "v.pcap", [_eth_ip(50, b"x" * 8)] * 2, big=big, nano=nano)
    pk = list(iter_packets(p))
    assert len(pk) == 2 and pk[0][0] == 100.5 and pk[1][0] == 101.5


def test_vlan_tagged_and_raw_ip_and_truncated_tail(tmp_path):
    assert summarize(write_pcap(tmp_path / "v.pcap", [_eth_ip(50, b"x" * 8, vlan=True)]))["esp_packets"] == 1
    raw = _eth_ip(50, b"x" * 8)[14:]
    assert summarize(write_pcap(tmp_path / "r.pcap", [raw], linktype=101))["esp_packets"] == 1
    p = write_pcap(tmp_path / "t.pcap", [_eth_ip(50, b"x" * 8)] * 3)
    p.write_bytes(p.read_bytes()[:-5])                                    # captura cortada no meio do último pacote
    assert summarize(p)["packets"] == 2


def test_non_ip_frames_counted_by_l3(tmp_path):
    arp = b"\xff" * 6 + b"\x11" * 6 + b"\x08\x06" + b"\0" * 28
    ipv6 = b"\xff" * 6 + b"\x11" * 6 + b"\x86\xdd" + b"\0" * 40
    s = summarize(write_pcap(tmp_path / "n.pcap", [arp, ipv6]))
    assert s["l3"] == {"arp": 1, "ipv6": 1} and s["ip_protocols"] == {}


def test_invalid_pcap_files(tmp_path):
    (tmp_path / "empty.pcap").write_bytes(b"")
    with pytest.raises(PcapError, match="vazio"):
        summarize(tmp_path / "empty.pcap")
    (tmp_path / "ng.pcap").write_bytes(b"\x0a\x0d\x0d\x0a" + b"\0" * 40)
    with pytest.raises(PcapError, match="pcapng"):
        summarize(tmp_path / "ng.pcap")
    (tmp_path / "x.pcap").write_bytes(b"NOTAPCAP" * 6)
    with pytest.raises(PcapError, match="assinatura"):
        summarize(tmp_path / "x.pcap")


def test_contains_bytes_across_chunk_boundary(tmp_path):
    p = tmp_path / "big.bin"
    p.write_bytes(b"a" * 1000 + b"NEEDLE" + b"b" * 1000)
    assert contains_bytes(p, b"NEEDLE", chunk=1003)                       # a agulha atravessa o limite do bloco
    assert contains_bytes(p, b"NEEDLE", chunk=7)
    assert not contains_bytes(p, b"ABSENT", chunk=100)
    with pytest.raises(ValueError):
        contains_bytes(p, b"")


def _pcap_bytes(tmp_path, n=2):
    return write_pcap(tmp_path / "ref.pcap", [_eth_ip(50, b"\0\0\0\1" + b"E" * 20)] * n).read_bytes()


class FakeCaptureClient:
    """GNS3 2.2.56: /pcap só existe durante a captura, o corpo vem sem enquadramento e o fluxo acaba depois do stop."""

    def __init__(self, body, connect_error=None, never_ends=False):
        self.calls, self.capturing, self.body = [], False, body
        self.stopped = threading.Event()
        self.connect_error, self.never_ends = connect_error, never_ends
        self.links = [{"link_id": "L1", "capturing": True}, {"link_id": "L2", "capturing": False}]

    def start_capture(self, pid, lid, dlt):
        self.calls.append(("start", lid, dlt))
        self.capturing = True
        self.stopped.clear()

    def stream_raw(self, pid, lid, dest, on_connect=None, stopping=None, idle_timeout=2.0):
        if not self.capturing:
            raise Gns3Error("GET pcap -> HTTP 409: This link has no active packet capture", status=409)
        if self.connect_error:
            raise self.connect_error
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            if on_connect:
                on_connect()
            fh.write(b"HTTP/1.1 200 OK\r\ncontent-type: application/vnd.tcpdump.pcap\r\n\r\n" + self.body[:24])
            fh.flush()
            threading.Event().wait(5) if self.never_ends else self.stopped.wait(5)
            fh.write(self.body[24:])
        return dest

    def stop_capture(self, pid, lid):
        self.calls.append(("stop", lid))
        self.capturing = False
        self.stopped.set()

    def list_links(self, pid): return self.links


def _cap(tmp_path, sleeps, **kw):
    ws = Workspace(tmp_path, "lab").ensure()
    ref = LinkRef("L1", "ring", "MKT12", "ether2", "MKT21", "ether2")
    client = FakeCaptureClient(_pcap_bytes(tmp_path), **{k: kw.pop(k) for k in ("connect_error", "never_ends") if k in kw})
    cap = PacketCapture(client, "pid", LinkIndex([ref]), ws, sleep=sleeps.append, clock=lambda: 0.0, **kw)
    return cap, ref, ws


def test_run_streams_while_capturing_and_returns_complete_pcap(tmp_path):
    sleeps = []
    cap, ref, ws = _cap(tmp_path, sleeps)
    out = cap.run(ref, 30, label="seg01")
    assert [c[0] for c in cap.client.calls] == ["start", "stop"] and sleeps == [30]
    assert out == ws.root / "captures" / "MKT12-MKT21-seg01-19700101T000000Z.pcap"
    assert out.read_bytes() == cap.client.body                               # resposta aninhada removida, pcap íntegro
    assert summarize(out)["esp_packets"] == 2 and not list(out.parent.glob("*.raw"))
    assert cap.client.calls[0][2] == "DLT_EN10MB"


def test_capturing_context_manager_exposes_path_and_stops_on_error(tmp_path):
    cap, ref, _ws = _cap(tmp_path, [])
    with cap.capturing(ref, "x") as c:
        assert c.path is None
    assert c.path.read_bytes() == cap.client.body
    with pytest.raises(RuntimeError):
        with cap.capturing(ref, "y"):
            raise RuntimeError("tráfego falhou")
    assert [x[0] for x in cap.client.calls] == ["start", "stop", "start", "stop"]    # não deixa captura ligada


def test_stop_without_start_and_double_start_are_clear_errors(tmp_path):
    cap, ref, _ws = _cap(tmp_path, [])
    with pytest.raises(QNetLabError, match="Não há captura iniciada por este processo"):
        cap.stop(ref)
    cap.start(ref)
    with pytest.raises(QNetLabError, match="Já há uma captura"):
        cap.start(ref)
    cap.stop(ref)


def test_stream_that_cannot_connect_stops_the_capture(tmp_path):
    cap, ref, _ws = _cap(tmp_path, [], connect_error=Gns3Error("HTTP 500", status=500))
    with pytest.raises(QNetLabError, match="não conectou"):
        cap.start(ref)
    assert ("stop", "L1") in cap.client.calls and not cap.client.capturing


def test_stream_that_never_ends_is_reported_with_partial_file(tmp_path):
    cap, ref, _ws = _cap(tmp_path, [], never_ends=True, stream_grace=0.2)
    cap.start(ref)
    with pytest.raises(QNetLabError, match="não encerrou o fluxo"):
        cap.stop(ref)


def test_active_lists_capturing_links(tmp_path):
    cap, *_ = _cap(tmp_path, [])
    assert cap.active() == ["L1"]


# ------------------------------------------------------------------ extract_pcap (corpo cru do GNS3)
def _chunked(data: bytes, size=7) -> bytes:
    out = b""
    for k in range(0, len(data), size):
        part = data[k:k + size]
        out += f"{len(part):x}\r\n".encode() + part + b"\r\n"
    return out + b"0\r\n\r\n"


@pytest.mark.parametrize("wrap", ["plain", "nested_http", "nested_http_chunked", "outer_chunked", "outer_chunked_nested",
                                  "junk_before_magic", "chunked_then_loose_http_trailer"])
def test_extract_pcap_handles_every_framing_seen_or_plausible(tmp_path, wrap):
    from qnetlab.capture import extract_pcap
    pcap = _pcap_bytes(tmp_path, 3)
    hdr = b"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.tcpdump.pcap\r\n"
    raw = {
        "plain": pcap,
        "nested_http": hdr + b"\r\n" + pcap,                                          # o que o GNS3 2.2.56 devolveu
        "nested_http_chunked": hdr + b"Transfer-Encoding: chunked\r\n\r\n" + _chunked(pcap),
        "outer_chunked": _chunked(pcap),                                              # GNS3 "normal"
        "outer_chunked_nested": _chunked(hdr + b"\r\n" + pcap),
        "junk_before_magic": b"lixo\r\n" + pcap,
        # o que o GNS3 2.2.56 realmente enviou (visto no .pcap.raw do laboratório): chunks + resposta HTTP solta + "0"
        "chunked_then_loose_http_trailer": _chunked(pcap)[:-5] + b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nDate: x\r\n\r\n0\r\n\r\n",
    }[wrap]
    out = extract_pcap(raw)
    assert out == pcap and len(list(iter_packets_from(tmp_path, out))) == 3


def iter_packets_from(tmp_path, data):
    f = tmp_path / "x.pcap"
    f.write_bytes(data)
    return iter_packets(f)


def test_extract_pcap_reports_what_it_received_when_there_is_no_pcap():
    from qnetlab.capture import extract_pcap
    with pytest.raises(PcapError, match="sem assinatura.*b'<html>erro"):
        extract_pcap(b"<html>erro interno</html>")
    with pytest.raises(PcapError, match="7 bytes"):
        extract_pcap(b"zz\r\nabc")
    with pytest.raises(PcapError, match="chunk inválido"):
        extract_pcap(b"5;ext\r\nhello\r\nzz\r\nabc")                   # enquadramento corrompido no meio


def test_neighbor_discovery_is_counted_apart_from_application_cleartext(tmp_path):
    """MNDP (UDP 5678 broadcast, visto no laboratório) não é dado de aplicação: não conta como claro, mas é reportado."""
    frames = [_eth_ip(17, _udp(5678, 5678, b"\x34\x00\x00\x00MKT12")) for _ in range(2)] + \
             [_eth_ip(50, b"\0\0\0\1" + b"E" * 20), _eth_ip(17, _udp(4500, 4500, b"\0\0\0\0ike"))]
    s = summarize(write_pcap(tmp_path / "m.pcap", frames), needle=b"MKT12")
    assert s["cleartext_data_packets"] == 0 and s["discovery_packets"] == 2 and s["needle_hits"] == 2
    assert s["ip_protocols"]["mndp"] == 2
    probe = summarize(write_pcap(tmp_path / "n.pcap", frames + [_eth_ip(17, _udp(40000, 9000, b"X"))]))
    assert probe["cleartext_data_packets"] == 1                              # sonda real continua sendo detectada


def test_lldp_and_cdp_frames_count_as_discovery(tmp_path):
    lldp = b"\x01\x80\xc2\x00\x00\x0e" + b"\xaa" * 6 + b"\x88\xcc" + b"\x02\x07MKT12"          # ethertype LLDP
    cdp = b"\x01\x00\x0c\xcc\xcc\xcc" + b"\xaa" * 6 + b"\x00\x20" + b"\xaa\xaa\x03\x00\x00\x0c\x20\x00MKT12"   # 802.3/SNAP
    s = summarize(write_pcap(tmp_path / "d.pcap", [lldp, cdp, _eth_ip(50, b"\0\0\0\1" + b"E" * 20)]), needle=b"MKT12")
    assert s["discovery_packets"] == 2 and s["cleartext_data_packets"] == 0 and s["needle_hits"] == 2
