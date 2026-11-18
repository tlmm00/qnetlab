"""Captura de pacotes nos enlaces do GNS3 e resumo do .pcap em Python puro (sem tshark).

Fluxo: ``start`` -> (tráfego) -> ``stop``, no mesmo processo (``capturing()`` faz isso num bloco ``with``; ``run`` por uma duração).
O resumo (:func:`summarize`) conta pacotes por protocolo IP e identifica IKE (UDP/500 e 4500) e ESP (protocolo 50),
o que basta para os testes de interceptação (SEG-01/02). Análise de mensagens IKE/PPK (FID-03) exige tshark/Wireshark.

Os .pcap ficam em ``captures/`` do diretório de trabalho; podem conter tráfego de gerenciamento e chaves públicas,
mas nunca chaves privadas (que não trafegam). Trate-os como dados do experimento.
"""
from __future__ import annotations

import logging
import re
import struct
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

from .errors import Gns3Error, QNetLabError
from .faults import LinkIndex, LinkRef
from .workspace import Workspace

log = logging.getLogger(__name__)

IP_PROTOS = {1: "icmp", 2: "igmp", 4: "ipip", 6: "tcp", 17: "udp", 47: "gre", 50: "esp", 51: "ah", 89: "ospf", 112: "vrrp"}
IKE_PORTS = (500, 4500)
MNDP_PORT = 5678          # MikroTik Neighbor Discovery: broadcast em claro com identidade/versão do roteador
LLDP_ETHERTYPE = "eth-0x88cc"
CDP_MAC = b"\x01\x00\x0c\xcc\xcc\xcc"
_MAGICS = {  # magic -> (endianness, resolução em ns?)
    b"\xd4\xc3\xb2\xa1": ("<", False), b"\xa1\xb2\xc3\xd4": (">", False),
    b"\x4d\x3c\xb2\xa1": ("<", True), b"\xa1\xb2\x3c\x4d": (">", True),
}


class PcapError(QNetLabError):
    """Arquivo .pcap inválido ou em formato não suportado (apenas pcap clássico)."""


# ----------------------------------------------------------------------------- parsing
def iter_packets(path: Path) -> Iterator[Tuple[float, int, bytes]]:
    """(timestamp, linktype, bytes) de cada pacote de um .pcap clássico. Tolera arquivo truncado no fim."""
    with open(path, "rb") as fh:
        head = fh.read(24)
        if len(head) < 24:
            raise PcapError(f"{path}: arquivo vazio ou truncado ({len(head)} bytes).")
        if head[:4] == b"\x0a\x0d\x0d\x0a":
            raise PcapError(f"{path}: formato pcapng não suportado; use pcap clássico.")
        if head[:4] not in _MAGICS:
            raise PcapError(f"{path}: assinatura desconhecida {head[:4].hex()}.")
        end, nano = _MAGICS[head[:4]]
        linktype = struct.unpack(end + "I", head[20:24])[0]
        scale = 1e9 if nano else 1e6
        while True:
            rec = fh.read(16)
            if len(rec) < 16:
                return
            sec, frac, caplen, _orig = struct.unpack(end + "IIII", rec)
            data = fh.read(caplen)
            if len(data) < caplen:
                return
            yield sec + frac / scale, linktype, data


def _ip_packet(linktype: int, data: bytes) -> Tuple[str, Optional[bytes]]:
    """(L3, bytes do pacote IPv4) para Ethernet(1) / Linux cooked(113) / raw IP(101, 228)."""
    if linktype == 1 and len(data) >= 14:
        et, off = struct.unpack("!H", data[12:14])[0], 14
        while et in (0x8100, 0x88A8) and len(data) >= off + 4:
            et, off = struct.unpack("!H", data[off + 2:off + 4])[0], off + 4
        if et == 0x0800:
            return "ipv4", data[off:]
        return {0x86DD: "ipv6", 0x0806: "arp"}.get(et, f"eth-0x{et:04x}"), None
    if linktype == 113 and len(data) >= 16:
        et = struct.unpack("!H", data[14:16])[0]
        return ("ipv4", data[16:]) if et == 0x0800 else (f"sll-0x{et:04x}", None)
    if linktype in (101, 228):
        return "ipv4", data
    return f"linktype-{linktype}", None


def summarize(path: Path, needle: Optional[bytes] = None) -> Dict[str, Any]:
    """Resumo da captura: pacotes, bytes, duração, protocolos IP, IKE/ESP e (opcional) ocorrências de ``needle``."""
    pk = by = 0
    t0 = t1 = None
    l3: Counter = Counter()
    proto: Counter = Counter()
    udp_ports: Counter = Counter()
    hits = cdp = 0
    for ts, lt, data in iter_packets(path):
        pk, by = pk + 1, by + len(data)
        t0, t1 = (ts if t0 is None else t0), ts
        if needle and needle in data:
            hits += 1
        kind, ip = _ip_packet(lt, data)
        l3[kind] += 1
        if lt == 1 and data[:6] == CDP_MAC:                # CDP vai em 802.3/LLC para 01:00:0c:cc:cc:cc
            cdp += 1
        if ip is None or len(ip) < 20 or ip[0] >> 4 != 4:
            continue
        ihl = (ip[0] & 0x0F) * 4
        p = ip[9]
        name = IP_PROTOS.get(p, f"proto-{p}")
        if p == 17 and len(ip) >= ihl + 4:
            sport, dport = struct.unpack("!HH", ip[ihl:ihl + 4])
            ike = sport in IKE_PORTS or dport in IKE_PORTS
            name = "ike" if ike else "mndp" if MNDP_PORT in (sport, dport) else "udp"
            udp_ports[min(sport, dport) if ike else dport] += 1
        proto[name] += 1
    cleartext = sum(n for k, n in proto.items() if k in ("tcp", "udp", "icmp"))
    return {
        "file": str(path), "packets": pk, "bytes": by,
        "duration_s": round((t1 - t0), 3) if t0 is not None else 0.0,
        "l3": dict(l3), "ip_protocols": dict(proto),
        "esp_packets": proto.get("esp", 0), "ike_packets": proto.get("ike", 0),
        "ospf_packets": proto.get("ospf", 0),
        # descoberta de vizinhos (MNDP, LLDP): não é dado de aplicação, mas vaza identidade/versão em claro (finding à parte)
        "discovery_packets": proto.get("mndp", 0) + l3.get(LLDP_ETHERTYPE, 0) + cdp,
        "cleartext_data_packets": cleartext,         # tcp/udp(não-IKE, não-MNDP)/icmp: aplicação em claro
        "needle_hits": hits if needle else None,
    }


def contains_bytes(path: Path, needle: bytes, chunk: int = 1 << 20) -> bool:
    """Procura ``needle`` no arquivo inteiro (também pega o que atravessa a borda de blocos)."""
    if not needle:
        raise ValueError("needle vazio")
    keep = len(needle) - 1
    tail = b""
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                return False
            buf = tail + block
            if needle in buf:
                return True
            tail = buf[-keep:] if keep else b""


def _dechunk(data: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(data):
        j = data.find(b"\r\n", i)
        if j < 0:
            break
        if data[i:i + 5] == b"HTTP/":
            break                       # GNS3 2.2.56 anexa uma resposta HTTP solta depois do último chunk de dados
        try:
            size = int(data[i:j].split(b";")[0].strip() or b"0", 16)
        except ValueError as exc:
            raise PcapError(f"fluxo com tamanho de chunk inválido: {data[i:j][:40]!r}") from exc
        if size == 0:
            break
        out += data[j + 2:j + 2 + size]
        i = j + 2 + size + 2
    return bytes(out)


def extract_pcap(raw: bytes) -> bytes:
    """Extrai o .pcap do corpo cru devolvido por ``GET .../pcap`` do GNS3.

    Aceita (em até 3 camadas): resposta HTTP aninhada no corpo (o GNS3 2.2.56 repassa a do compute), corpo ``chunked`` e
    pcap puro. Como último recurso, procura a assinatura do pcap no fluxo. Levanta :class:`PcapError` com o início dos bytes
    recebidos se não achar nada (diagnóstico).
    """
    data = raw
    for _ in range(3):
        if data[:5] == b"HTTP/":
            end = data.find(b"\r\n\r\n")
            if end < 0:
                break
            chunked = b"transfer-encoding: chunked" in data[:end].lower()
            data = data[end + 4:]
            data = _dechunk(data) if chunked else data
            continue
        if data[:4] in _MAGICS:
            return data
        if re.match(rb"^[0-9a-fA-F]+(;[^\r\n]*)?\r\n", data):
            data = _dechunk(data)
            continue
        break
    if data[:4] in _MAGICS:
        return data
    hits = [i for i in (data.find(m) for m in _MAGICS) if i >= 0]
    if hits:
        return data[min(hits):]
    raise PcapError(f"fluxo sem assinatura de pcap ({len(raw)} bytes recebidos); início: {raw[:120]!r}")


# ----------------------------------------------------------------------------- captura
class _Stream:
    """Thread que baixa o .pcap enquanto a captura roda (o GNS3 2.2 só o serve nesse período)."""

    def __init__(self, client: Any, project_id: str, ref: LinkRef, dest: Path):
        self.ref, self.dest = ref, dest
        self.stopping = threading.Event()            # definido depois do stop_capture: o fluxo pode terminar ao ficar ocioso
        self.connected = threading.Event()
        self.ready = threading.Event()               # conectou OU a thread terminou (com erro)
        self.error: Optional[BaseException] = None
        self._client, self._pid = client, project_id
        self.thread = threading.Thread(target=self._run, name=f"pcap-{ref.id[:8]}", daemon=True)

    def _on_connect(self) -> None:
        self.connected.set()
        self.ready.set()

    def _run(self) -> None:
        try:
            self._loop()
        finally:
            self.ready.set()

    def _finish(self, raw: Path) -> None:
        """Converte o corpo cru em .pcap limpo; em caso de erro mantém o ``.raw`` ao lado para diagnóstico."""
        self.dest.write_bytes(extract_pcap(raw.read_bytes()))
        raw.unlink()

    def _loop(self) -> None:
        deadline = time.time() + 15
        while True:
            try:
                raw = self.dest.with_name(self.dest.name + ".raw")
                self._client.stream_raw(self._pid, self.ref.id, raw, on_connect=self._on_connect,
                                        stopping=self.stopping.is_set)
                self._finish(raw)
                return
            except Gns3Error as exc:
                if exc.status in (404, 409) and not self.connected.is_set() and time.time() < deadline:
                    time.sleep(0.5)                  # o arquivo da captura ainda não foi criado
                    continue
                self.error = exc
                return
            except PcapError as exc:
                self.error = QNetLabError(f"{exc} (bytes crus mantidos em {self.dest.name}.raw)")
                return
            except Exception as exc:                 # nunca deixar a thread morrer em silêncio
                self.error = exc
                return


class Capturing:
    """Resultado de :meth:`PacketCapture.capturing`: ``path`` é preenchido ao sair do bloco."""
    path: Optional[Path] = None


class PacketCapture:
    """Captura em um enlace do GNS3.

    O GNS3 2.2 só serve ``GET /pcap`` **enquanto** a captura está ativa (depois do ``stop``, 409) e não deixa o arquivo
    legível ao seu usuário se o servidor roda como outro usuário (ex.: ``root``). Por isso ``start`` abre o fluxo em uma
    *thread* e ``stop`` encerra a captura, o que faz o GNS3 fechar o fluxo. Funciona com GNS3 local ou remoto, mas
    **apenas dentro do mesmo processo** (não há ``start``/``stop`` em comandos separados da CLI).
    """

    def __init__(self, client: Any, project_id: str, links: LinkIndex, ws: Workspace,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 stream_grace: float = 30.0):
        self.client, self.project_id, self.links, self.ws = client, project_id, links, ws
        self._sleep, self._clock, self._grace = sleep, clock, stream_grace
        self._streams: Dict[str, _Stream] = {}

    @property
    def out_dir(self) -> Path:
        return self.ws.root / "captures"

    def start(self, ref: LinkRef, label: str = "", data_link_type: str = "DLT_EN10MB") -> Path:
        """Inicia a captura e o download. Devolve o caminho do ``.pcap`` que será gravado (completo após ``stop``)."""
        if ref.id in self._streams:
            raise QNetLabError(f"Já há uma captura em andamento em {ref.label}.")
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self._clock()))
        dest = self.out_dir / (f"{ref.a}-{ref.b}" + (f"-{label}" if label else "") + f"-{stamp}.pcap")
        self.client.start_capture(self.project_id, ref.id, data_link_type)
        stream = _Stream(self.client, self.project_id, ref, dest)
        stream.thread.start()
        stream.ready.wait(timeout=20)
        if not stream.connected.is_set() or stream.error:
            self.client.stop_capture(self.project_id, ref.id)
            raise QNetLabError(f"A captura de {ref.label} foi iniciada, mas o fluxo do .pcap não conectou: "
                               f"{stream.error or 'tempo esgotado'}")
        self._streams[ref.id] = stream
        log.info("captura iniciada em %s -> %s", ref.label, dest)
        return dest

    def stop(self, ref: LinkRef) -> Path:
        """Para a captura, espera o fluxo terminar e devolve o ``.pcap`` completo."""
        stream = self._streams.pop(ref.id, None)
        if stream is None:
            raise QNetLabError(f"Não há captura iniciada por este processo em {ref.label}: use 'capture run' ou "
                               "PacketCapture.start()/capturing() no mesmo processo.")
        self.client.stop_capture(self.project_id, ref.id)
        stream.stopping.set()
        log.info("captura parada em %s", ref.label)
        stream.thread.join(timeout=self._grace)
        if stream.thread.is_alive():
            raise QNetLabError(f"O GNS3 não encerrou o fluxo da captura em {self._grace:.0f}s; "
                               f"arquivo parcial em {stream.dest}")
        if stream.error:
            raise QNetLabError(f"Falha ao baixar a captura de {ref.label}: {stream.error}")
        return stream.dest

    @contextmanager
    def capturing(self, ref: LinkRef, label: str = "") -> Iterator[Capturing]:
        """``with capture.capturing(ref, "seg01") as c: ...`` -> ``c.path`` após o bloco. Para a captura mesmo com erro."""
        holder = Capturing()
        self.start(ref, label)
        try:
            yield holder
        finally:
            holder.path = self.stop(ref)

    def run(self, ref: LinkRef, duration: float, label: str = "") -> Path:
        """Captura por ``duration`` s e devolve o ``.pcap``."""
        with self.capturing(ref, label) as c:
            self._sleep(duration)
        assert c.path is not None
        return c.path

    def active(self) -> list:
        return [g["link_id"] for g in self.client.list_links(self.project_id) if g.get("capturing")]
