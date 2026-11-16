"""Planejamento determinístico de endereçamento, enlaces e túneis (anel de sites com 2 CHRs).

Estrutura (``i`` = site, ``j`` = 1|2)::

    MKTi1 ==e3== MKTi2           e3: enlace dentro do site
      |  \\         |  \\
     e1   e7       e1   e7       e1: KME   e7: NODE   e8: NAT (gerenciamento)
      |    \\       |    \\
    KMEi    NODEi (eth0<-MKTi1, eth1<-MKTi2)
    MKTi2 ==e2== MKT(i+1)1       e2: enlace entre sites (protegido por IPsec/QKD)
    KMEi eth0 ==KMA== KME(i+1) eth1   (anel de KMEs)

Convenções (todas derivadas de ``i`` e ``j``; cada enlace é um /30 com ``.1`` e ``.2``):

* KME<->CHR  (kme_access_pool, bloco 2(i-1)+(j-1)): KME ``.1`` (eth{1+j}), CHR ``.2`` (ether1);
* CHR<->CHR entre sites (ring_pool, bloco ``r % N`` para o enlace ``r`` = MKTr2<->MKT(r+1)1);
  o extremo de menor (site, j) recebe ``.1``;
* CHR<->CHR no site (sibling_pool, bloco i-1): MKTi1 ``.1``, MKTi2 ``.2``;
* CHR<->NODE (node_pool, bloco 2(i-1)+(j-1)): CHR ``.1`` (ether7), NODE ``.2`` (eth{j-1});
* KME<->KME  (kma_pool, bloco r-1 para o enlace ``r``): KMEr ``.1`` (eth0), KME(r+1) ``.2`` (eth1);
* gerenciamento: CHR k (0-based, na ordem MKT11, MKT12, MKT21...) = ``mgmt_start + k*mgmt_step`` em ether8;
* router-id OSPF = ``router_id_pool.base + 10*i + j``.

O resultado é um :class:`AddressPlan` puro (sem E/S), usado por todas as camadas.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network
from typing import Dict, List, Tuple

from .config.models import SiteSpec, Topology
from .errors import AddressingError


@dataclass(frozen=True)
class Iface:
    """Interface de um nó: nome, IP e o que há do outro lado."""
    name: str                # etherK (CHR) ou ethK (contêiner)
    ip: IPv4Address
    network: IPv4Network
    peer: str                # nome do nó do outro lado
    peer_ip: IPv4Address
    role: str

    @property
    def cidr(self) -> str:
        return f"{self.ip}/{self.network.prefixlen}"


@dataclass(frozen=True)
class Link:
    """Enlace físico do GNS3 (ponto a ponto)."""
    kind: str                # kme-access | ring | sibling | node | kma | nat
    a: str
    a_if: str
    b: str
    b_if: str
    network: IPv4Network | None = None


@dataclass(frozen=True)
class ChrPlan:
    name: str
    sae: str                 # SAE ID (CN do certificado)
    site: int
    j: int
    mgmt_ip: IPv4Address | None
    router_id: IPv4Address
    ifaces: Dict[str, Iface]  # por papel: kme, ring, sibling, node

    @property
    def nat_name(self) -> str:
        return f"NAT{2 * (self.site - 1) + self.j}"


@dataclass(frozen=True)
class KmePlan:
    name: str
    kme_id: str
    site: int
    ifaces: Dict[str, Iface]  # por papel: kma_next, kma_prev, chr1, chr2


@dataclass(frozen=True)
class NodePlan:
    name: str
    site: int
    ifaces: Dict[str, Iface]  # chr1 (eth0), chr2 (eth1)
    routes: Tuple[Tuple[str, str, str], ...]   # (rede, via, dispositivo)


@dataclass(frozen=True)
class SitePlan:
    site: SiteSpec
    chrs: Tuple[ChrPlan, ChrPlan]
    kme: KmePlan
    node: NodePlan


@dataclass(frozen=True)
class TunnelPlan:
    """Túnel IPsec/IKEv2 sobre um enlace ``ether2`` entre dois CHRs."""
    link: int                # 1..N
    a: str                   # CHR (nome)
    b: str
    a_addr: IPv4Address      # endereço da SA (IP de ether2)
    b_addr: IPv4Address
    a_net: IPv4Network       # rede CHR<->NODE protegida na ponta A
    b_net: IPv4Network
    protected: bool

    @property
    def name(self) -> str:
        return f"{self.a}-{self.b}"

    def peer_of(self, chr_name: str) -> str:
        return self.b if chr_name == self.a else self.a

    def local_addr(self, chr_name: str) -> IPv4Address:
        return self.a_addr if chr_name == self.a else self.b_addr

    def remote_addr(self, chr_name: str) -> IPv4Address:
        return self.b_addr if chr_name == self.a else self.a_addr

    def local_net(self, chr_name: str) -> IPv4Network:
        return self.a_net if chr_name == self.a else self.b_net

    def remote_net(self, chr_name: str) -> IPv4Network:
        return self.b_net if chr_name == self.a else self.a_net


@dataclass(frozen=True)
class AddressPlan:
    topology: str
    mgmt_network: IPv4Network
    mgmt_gateway: IPv4Address
    node_pool: IPv4Network
    sites: Dict[int, SitePlan]
    links: Tuple[Link, ...]
    tunnels: Tuple[TunnelPlan, ...]
    kma_links: Tuple[Tuple[int, int], ...] = ()    # (site_a, site_b) em ordem do anel

    # ---------------------------------------------------------------- consultas
    def chrs(self) -> List[ChrPlan]:
        return [c for sp in self.sites.values() for c in sp.chrs]

    def chr(self, name: str) -> ChrPlan:
        for c in self.chrs():
            if c.name == name:
                return c
        raise KeyError(name)

    def tunnel_of(self, chr_name: str) -> TunnelPlan | None:
        for t in self.tunnels:
            if chr_name in (t.a, t.b):
                return t
        return None

    def kme_peers(self, site: int) -> List[int]:
        """Sites vizinhos (anel de KMEs): os que têm enlace KMA com ``site``."""
        return sorted({b if a == site else a for a, b in self.kma_links if site in (a, b)})

    def kme_link_ip(self, site: int, peer: int) -> Tuple[IPv4Address, IPv4Address]:
        """(IP local, IP do vizinho) do enlace KMA entre dois KMEs."""
        kme = self.sites[site].kme
        for iface in kme.ifaces.values():
            if iface.role.startswith("kma") and iface.peer == self.sites[peer].kme.name:
                return iface.ip, iface.peer_ip
        raise KeyError((site, peer))

    def neighbors(self, site: int) -> Tuple[int, int]:
        n = len(self.sites)
        return ((site - 2) % n + 1, site % n + 1)       # (anterior, próximo)


# ---------------------------------------------------------------------------
def _block(pool: IPv4Network, idx: int, label: str) -> IPv4Network:
    if (idx + 1) * 4 > pool.num_addresses:
        raise AddressingError(f"Pool {label} ({pool}) sem espaço para o enlace #{idx} (/30). Aumente o pool.")
    return IPv4Network((int(pool.network_address) + 4 * idx, 30))


def _check_pools_disjoint(pools: Dict[str, IPv4Network]) -> None:
    names = list(pools)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if pools[a].overlaps(pools[b]):
                raise AddressingError(f"Pools sobrepostos: {a}={pools[a]} e {b}={pools[b]}")


def build_plan(topo: Topology) -> AddressPlan:
    a = topo.addressing
    pools = {
        "kme_access": IPv4Network(a.kme_access_pool), "ring": IPv4Network(a.ring_pool),
        "sibling": IPv4Network(a.sibling_pool), "node": IPv4Network(a.node_pool),
        "kma": IPv4Network(a.kma_pool), "router_id": IPv4Network(a.router_id_pool),
        "mgmt": IPv4Network(a.mgmt_subnet),
    }
    _check_pools_disjoint(pools)
    mgmt = pools["mgmt"]
    if mgmt.prefixlen != 24:
        raise AddressingError("A rede de gerenciamento deve ser um /24.")
    for k in ("kme_access", "ring", "sibling", "node", "kma"):
        if pools[k].prefixlen > 30:
            raise AddressingError(f"Pool {k} pequeno demais para um /30.")
    gw = IPv4Address(a.mgmt_gateway)
    if gw not in mgmt:
        raise AddressingError(f"mgmt_gateway {gw} fora de {mgmt}.")

    n = topo.ring_size
    rid_base = pools["router_id"].network_address
    nat = a.mgmt_attach == "nat"
    if nat:
        last = a.mgmt_start + a.mgmt_step * (2 * n - 1)
        if a.mgmt_start < 2 or a.mgmt_step < 1 or last > 254:
            raise AddressingError(
                f"Endereços de gerenciamento ({a.mgmt_start} + {a.mgmt_step}*k, k<{2 * n}) excedem o /24; "
                "reduza mgmt_step ou o tamanho do anel.")

    links: List[Link] = []
    chr_ifaces: Dict[Tuple[int, int], Dict[str, Iface]] = {}
    kme_ifaces: Dict[int, Dict[str, Iface]] = {i: {} for i in range(1, n + 1)}
    node_ifaces: Dict[int, Dict[str, Iface]] = {i: {} for i in range(1, n + 1)}
    for i in range(1, n + 1):
        for j in (1, 2):
            chr_ifaces[(i, j)] = {}
    names = {i: SiteSpec(i) for i in range(1, n + 1)}
    r = topo.router

    def add(i: int, j: int, role: str, ifname: str, net: IPv4Network, host: int, peer: str, peer_host: int) -> None:
        chr_ifaces[(i, j)][role] = Iface(ifname, net[host], net, peer, net[peer_host], role)

    for i in range(1, n + 1):
        s = names[i]
        for j in (1, 2):
            b = 2 * (i - 1) + (j - 1)
            # KME <-> CHR
            net = _block(pools["kme_access"], b, "kme_access_pool")
            add(i, j, "kme", r.if_kme, net, 2, s.kme, 1)
            kme_ifaces[i][f"chr{j}"] = Iface(f"eth{1 + j}", net[1], net, s.chr(j), net[2], f"chr{j}")
            links.append(Link("kme-access", s.kme, f"eth{1 + j}", s.chr(j), r.if_kme, net))
            # CHR <-> NODE
            net = _block(pools["node"], b, "node_pool")
            add(i, j, "node", r.if_node, net, 1, s.node, 2)
            node_ifaces[i][f"chr{j}"] = Iface(f"eth{j - 1}", net[2], net, s.chr(j), net[1], f"chr{j}")
            links.append(Link("node", s.node, f"eth{j - 1}", s.chr(j), r.if_node, net))
        # CHR <-> CHR no site
        net = _block(pools["sibling"], i - 1, "sibling_pool")
        add(i, 1, "sibling", r.if_sibling, net, 1, s.chr(2), 2)
        add(i, 2, "sibling", r.if_sibling, net, 2, s.chr(1), 1)
        links.append(Link("sibling", s.chr(1), r.if_sibling, s.chr(2), r.if_sibling, net))

    kma: List[Tuple[int, int]] = []
    ring_links: List[Tuple[int, Tuple[int, int], Tuple[int, int], IPv4Network]] = []
    for k in range(1, n + 1):
        nxt = k % n + 1
        # enlace do anel (e2): MKTk2 <-> MKT(k+1)1
        ea, eb = (k, 2), (nxt, 1)
        net = _block(pools["ring"], k % n, "ring_pool")
        lo_first = ea if ea < eb else eb              # o de menor (site, j) fica com .1
        for (si, sj), other in ((ea, eb), (eb, ea)):
            host = 1 if (si, sj) == lo_first else 2
            add(si, sj, "ring", r.if_ring, net, host, names[other[0]].chr(other[1]), 3 - host)
        links.append(Link("ring", names[ea[0]].chr(ea[1]), r.if_ring, names[eb[0]].chr(eb[1]), r.if_ring, net))
        ring_links.append((k, ea, eb, net))
        # enlace KMA: KMEk eth0 <-> KME(k+1) eth1
        knet = _block(pools["kma"], k - 1, "kma_pool")
        kme_ifaces[k]["kma_next"] = Iface("eth0", knet[1], knet, names[nxt].kme, knet[2], "kma_next")
        kme_ifaces[nxt]["kma_prev"] = Iface("eth1", knet[2], knet, names[k].kme, knet[1], "kma_prev")
        links.append(Link("kma", names[k].kme, "eth0", names[nxt].kme, "eth1", knet))
        kma.append((k, nxt))

    sites: Dict[int, SitePlan] = {}
    k_idx = 0
    for i in range(1, n + 1):
        s = names[i]
        chrs = []
        for j in (1, 2):
            mgmt_ip = mgmt[a.mgmt_start + a.mgmt_step * k_idx] if nat else None
            k_idx += 1
            rid = rid_base + 10 * i + j
            if rid not in pools["router_id"]:
                raise AddressingError(f"router-id de {s.chr(j)} ({rid}) fora do pool {pools['router_id']}.")
            chrs.append(ChrPlan(s.chr(j), s.chr_sae(j), i, j, mgmt_ip, rid, chr_ifaces[(i, j)]))
            if nat:
                links.append(Link("nat", s.nat(j), "nat0", s.chr(j), r.if_mgmt))
        sites[i] = SitePlan(s, (chrs[0], chrs[1]), KmePlan(s.kme, s.kme_id, i, kme_ifaces[i]),
                            NodePlan(s.node, i, node_ifaces[i], ()))
    if nat and gw in {c.mgmt_ip for sp in sites.values() for c in sp.chrs}:
        raise AddressingError(f"mgmt_gateway {gw} colide com o endereço de um CHR.")

    # túneis: um por enlace do anel
    by_name = {c.name: c for sp in sites.values() for c in sp.chrs}
    tunnels: List[TunnelPlan] = []
    for k, ea, eb, _net in ring_links:
        ca, cb = by_name[names[ea[0]].chr(ea[1])], by_name[names[eb[0]].chr(eb[1])]
        tunnels.append(TunnelPlan(
            link=k, a=ca.name, b=cb.name,
            a_addr=ca.ifaces["ring"].ip, b_addr=cb.ifaces["ring"].ip,
            a_net=ca.ifaces["node"].network, b_net=cb.ifaces["node"].network,
            protected=topo.ipsec.enabled and k not in topo.ipsec.unprotected_links,
        ))

    # rotas dos NODEs: vizinho anterior via eth0 (MKTi1), próximo via eth1 (MKTi2)
    final_sites: Dict[int, SitePlan] = {}
    for i, sp in sites.items():
        prev_i, next_i = ((i - 2) % n + 1, i % n + 1)
        prev_net = sites[prev_i].chrs[1].ifaces["node"].network      # lado de MKT(i-1)2
        next_net = sites[next_i].chrs[0].ifaces["node"].network      # lado de MKT(i+1)1
        routes = (
            (str(prev_net), str(sp.chrs[0].ifaces["node"].ip), "eth0"),
            (str(next_net), str(sp.chrs[1].ifaces["node"].ip), "eth1"),
        )
        final_sites[i] = SitePlan(sp.site, sp.chrs, sp.kme,
                                  NodePlan(sp.node.name, i, sp.node.ifaces, routes))
    return AddressPlan(topo.name, mgmt, gw, pools["node"], final_sites, tuple(links), tuple(tunnels), tuple(kma))
