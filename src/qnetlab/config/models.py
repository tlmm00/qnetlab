"""Modelos declarativos (imutáveis) que representam uma topologia de laboratório.

O arquivo YAML/JSON é convertido para estes dataclasses por
:mod:`qnetlab.config.loader`. Nenhuma outra camada lê o YAML diretamente.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import IPv4Network
from typing import Tuple, Union

from ..errors import ConfigError

PPK_MODES = ("qkd", "static", "none")
KEY_TYPES = ("rsa-2048", "rsa-3072", "rsa-4096", "ec-p256", "ec-p384")
MGMT_ATTACH = ("nat", "none")
IKE_AUTH = ("psk", "cert")
PEER_SELECTION = ("adjacent", "none")


@dataclass(frozen=True)
class Gns3Config:
    url: str = "http://127.0.0.1:3080"
    api_version: int = 2
    user: str = ""
    password: str = ""
    project_name: str = ""          # vazio -> nome da topologia
    compute_id: str = "local"
    verify_tls: bool = True
    timeout: float = 30.0
    start_timeout: float = 300.0


@dataclass(frozen=True)
class AddressingConfig:
    """Pools de endereços. Cada enlace ponto a ponto recebe um /30 (``.1`` e ``.2``) do seu pool."""
    kme_access_pool: str = "10.0.0.0/24"    # KME <-> CHR (ether1): KME .1, CHR .2
    ring_pool: str = "10.0.1.0/24"          # CHR <-> CHR entre sites (ether2); o de menor (site, j) fica com .1
    sibling_pool: str = "10.0.2.0/24"       # CHR <-> CHR dentro do site (ether3): MKTi1 .1, MKTi2 .2
    node_pool: str = "10.0.3.0/24"          # CHR <-> NODE (ether7): CHR .1, NODE .2
    kma_pool: str = "172.16.100.0/24"       # KME <-> KME (enlaces KMA): KMEi .1, KME(i+1) .2
    router_id_pool: str = "10.255.0.0/24"   # router-id OSPF = base + 10*site + j
    mgmt_subnet: str = "192.168.122.0/24"   # rede do nó NAT do GNS3 (virbr0)
    mgmt_gateway: str = "192.168.122.1"
    mgmt_attach: str = "nat"                # nat: um nó NAT por CHR em ether8 | none
    mgmt_start: int = 10                    # 1º CHR (MKT11) = .10, depois de 'mgmt_step' em 'mgmt_step'
    mgmt_step: int = 10


@dataclass(frozen=True)
class RouterConfig:
    gns3_template: str = "MikroTik CHR"
    admin_user: str = "admin"
    admin_password: str = "${QNETLAB_ADMIN_PASSWORD:-qnetlab-lab}"
    boot_timeout: float = 420.0
    min_version: str = "7.22"       # versão mínima do RouterOS (QKD/PPK); "" desliga a checagem
    auto_update: bool = True        # atualiza o CHR sozinho (via NAT/Internet) se a versão for menor que min_version
    update_channel: str = "stable"  # canal do /system package update: stable | long-term | testing
    update_method: str = "host"     # host: o host baixa o .npk e o envia por FTP | device: o CHR baixa (precisa de DNS/Internet)
    update_version: str = ""        # fixa a versão (ex.: "7.20.6"); vazio = a mais nova do canal
    update_dns: str = ""            # DNS usado só durante a atualização (vazio = gateway do NAT)
    update_timeout: float = 900.0   # tempo máximo (s) de download + reinício por roteador
    neighbor_discovery: bool = False  # MNDP/LLDP/CDP: desligado por padrão (anunciam identidade e versão do RouterOS em claro)
    # Papel de cada interface do CHR (iguais às do laboratório MITACS)
    if_kme: str = "ether1"          # -> KME do site
    if_ring: str = "ether2"         # -> CHR do site vizinho (enlace protegido por IPsec/QKD)
    if_sibling: str = "ether3"      # -> outro CHR do mesmo site
    if_node: str = "ether7"         # -> NODE (cliente DFL) do site
    if_mgmt: str = "ether8"         # -> nó NAT do GNS3 (gerenciamento)


@dataclass(frozen=True)
class ContainerConfig:
    base_image: str = "qnetlab/node:base"
    kme_source_dir: str = ""        # se definido, substitui o código KME embutido
    app_source_dir: str = ""        # se definido, substitui o agente SAE embutido
    kme_api_port: int = 8443
    metrics_port: int = 9100
    probe_port: int = 9000
    key_size_bits: int = 256


@dataclass(frozen=True)
class KmeConfig:
    """Parâmetros do ``peer_sync`` do KME (esquema do KMS-sim). Padrões = padrões do código do KME."""
    sync_interval_s: float = 5
    generation_delay_s: float = 0.5     # pausa entre chaves de uma varredura (taxa de geração simulada)
    sync_threshold: int = 100           # chaves sincronizadas mantidas por vizinho
    request_timeout_ms: int = 5000
    max_retries: int = 3
    log_level: str = "INFO"


@dataclass(frozen=True)
class OspfConfig:
    instance: str = "ospf-v2"
    area: str = "backbone"
    area_id: str = "0.0.0.0"
    ring_cost: int = 10             # enlaces entre sites (ether2)
    sibling_cost: int = 10          # enlace entre os dois CHRs do site (ether3)


@dataclass(frozen=True)
class IpsecConfig:
    """IPsec IKEv2 com PPK nos enlaces entre sites (um túnel por enlace ``ether2``).

    Cada CHR tem exatamente um enlace ``ether2``, portanto exatamente um túnel e um ``/ip ipsec key qkd``
    (objeto único por roteador) — sem conflito de ``peer-sae-id``.
    """
    enabled: bool = True
    dh_group: str = "ecp384"
    pfs_group: str = "modp2048"
    hash_algorithm: str = "sha256"
    enc_algorithm: str = "aes-256"
    proposal_auth: str = "sha256"
    proposal_enc: str = "aes-256-cbc"
    lifetime: str = "1h"                       # IKE SA (profile)
    proposal_lifetime: str = "1m"              # Child/ESP SA (proposal): rechaveia a cada minuto (uma chave QKD nova por rekey)
    auth_method: str = "psk"                   # psk | cert
    psk: str = ""                              # vazio -> um segredo aleatório por túnel (em state.json)
    ppk_mode: str = "qkd"                      # qkd | static | none
    qkd_key_size: int = 256                    # "key-size" do /ip ipsec key qkd (o KME trata <=128 como bytes)
    qkd_cache_size: int = 10
    enforce_encryption: bool = True            # descarta tráfego NODE<->NODE sem IPsec em ether2
    unprotected_links: Tuple[int, ...] = ()    # enlaces do anel (1..N) deixados em claro (grupo de controle)


@dataclass(frozen=True)
class PkiConfig:
    key_type: str = "rsa-3072"
    validity_days: int = 825
    ca_validity_days: int = 3650
    organization: str = "QNetLab"
    country: str = "BR"


@dataclass(frozen=True)
class LoadConfig:
    duration_s: int = 60
    warmup_s: int = 10
    probe_interval_ms: int = 100
    probe_size_bytes: int = 64
    key_interval_s: int = 0        # reservado: neste laboratório os SAEs são os CHRs, não os NODEs (0 = desligado)
    peers: str = "adjacent"        # adjacent | none (NODEs só alcançam os vizinhos pelos túneis)


@dataclass(frozen=True)
class SiteSpec:
    """Um site = 2 CHRs (MKTi1, MKTi2), 1 KME e 1 NODE (cliente DFL com duas interfaces)."""
    id: int

    def chr(self, j: int) -> str:
        return f"MKT{self.id}{j}"

    def chr_sae(self, j: int) -> str:
        return f"sae-chr{self.id}{j}"

    def nat(self, j: int) -> str:
        """Nó NAT do CHR MKT{id}{j}: numeração sequencial como nos laboratórios (NAT1 = MKT11, NAT2 = MKT12, NAT3 = MKT21...)."""
        return f"NAT{2 * (self.id - 1) + j}"

    @property
    def chrs(self) -> Tuple[str, str]:
        return (self.chr(1), self.chr(2))

    @property
    def kme(self) -> str:
        return f"KME{self.id}"

    @property
    def kme_id(self) -> str:
        return f"kme-sim-{self.id}"

    @property
    def node(self) -> str:
        return f"NODE{self.id}"


@dataclass(frozen=True)
class Topology:
    name: str
    description: str = ""
    ring_size: int = 4
    gns3: Gns3Config = field(default_factory=Gns3Config)
    addressing: AddressingConfig = field(default_factory=AddressingConfig)
    router: RouterConfig = field(default_factory=RouterConfig)
    containers: ContainerConfig = field(default_factory=ContainerConfig)
    kme: KmeConfig = field(default_factory=KmeConfig)
    ospf: OspfConfig = field(default_factory=OspfConfig)
    ipsec: IpsecConfig = field(default_factory=IpsecConfig)
    pki: PkiConfig = field(default_factory=PkiConfig)
    load: LoadConfig = field(default_factory=LoadConfig)

    # ------------------------------------------------------------------ helpers
    @property
    def project_name(self) -> str:
        return self.gns3.project_name or self.name

    @property
    def sites(self) -> Tuple[SiteSpec, ...]:
        return tuple(SiteSpec(i) for i in range(1, self.ring_size + 1))

    def site(self, site_id: int) -> SiteSpec:
        if not 1 <= site_id <= self.ring_size:
            raise ConfigError(f"Site inexistente: {site_id}")
        return SiteSpec(site_id)

    def site_ids(self) -> Tuple[int, ...]:
        return tuple(range(1, self.ring_size + 1))

    def image_tag(self, node_name: str) -> str:
        return f"qnetlab/{self.name}-{node_name}:latest".lower()

    @property
    def node_image(self) -> str:
        """Imagem dos NODEs: a base (agente embutido) ou uma imagem própria se app_source_dir estiver definido."""
        return self.image_tag("node") if self.containers.app_source_dir else self.containers.base_image

    # --------------------------------------------------------------- validation
    def validate(self) -> None:
        import re
        from ipaddress import ip_address

        if not self.name or not all(c.isalnum() or c in "-_" for c in self.name):
            raise ConfigError("'name' deve conter apenas letras, dígitos, '-' ou '_'.")
        if not isinstance(self.ring_size, int) or not 3 <= self.ring_size <= 24:
            raise ConfigError("ring.size deve ser um inteiro entre 3 e 24.")

        for label in ("kme_access_pool", "ring_pool", "sibling_pool", "node_pool", "kma_pool",
                      "router_id_pool", "mgmt_subnet"):
            try:
                IPv4Network(getattr(self.addressing, label))
            except ValueError as exc:
                raise ConfigError(f"addressing.{label} inválido: {exc}") from exc
        try:
            ip_address(self.addressing.mgmt_gateway)
        except ValueError as exc:
            raise ConfigError(f"addressing.mgmt_gateway inválido: {exc}") from exc
        if self.addressing.mgmt_attach not in MGMT_ATTACH:
            raise ConfigError(f"addressing.mgmt_attach deve ser um de {MGMT_ATTACH}")

        if self.router.min_version and not re.fullmatch(r"\d+\.\d+(\.\d+)?", self.router.min_version):
            raise ConfigError("router.min_version deve ser como '7.22' ou '7.22.1' (ou vazio).")
        if self.router.update_method not in ("host", "device"):
            raise ConfigError("router.update_method deve ser 'host' ou 'device'.")
        if self.router.update_version and not re.fullmatch(r"\d+\.\d+(\.\d+)?", self.router.update_version):
            raise ConfigError("router.update_version deve ser como '7.20.6' (ou vazio).")
        if self.router.update_version and self.router.min_version:
            key = lambda v: tuple(int(x) for x in (v.split(".") + ["0", "0"])[:3])
            if key(self.router.update_version) < key(self.router.min_version):
                raise ConfigError("router.update_version não pode ser menor que router.min_version.")
        if self.router.update_channel not in ("stable", "long-term", "testing"):
            raise ConfigError("router.update_channel deve ser stable, long-term ou testing.")
        roles = {k: getattr(self.router, k) for k in ("if_kme", "if_ring", "if_sibling", "if_node", "if_mgmt")}
        for k, v in roles.items():
            if not re.fullmatch(r"ether[1-9][0-9]?", str(v)):
                raise ConfigError(f"router.{k} deve ser 'etherN' (recebido {v!r}).")
        if len(set(roles.values())) != len(roles):
            raise ConfigError(f"router.if_*: cada papel precisa de uma interface distinta ({roles}).")

        if not re.fullmatch(r"\d+[smhdw]|\d{2}:\d{2}:\d{2}", self.ipsec.proposal_lifetime):
            raise ConfigError("ipsec.proposal_lifetime deve ser como '1m', '30s', '1h' ou '00:01:00'.")
        if self.ipsec.ppk_mode not in PPK_MODES:
            raise ConfigError(f"ipsec.ppk_mode deve ser um de {PPK_MODES}")
        if self.ipsec.auth_method not in IKE_AUTH:
            raise ConfigError(f"ipsec.auth_method deve ser um de {IKE_AUTH}")
        for n in self.ipsec.unprotected_links:
            if not isinstance(n, int) or not 1 <= n <= self.ring_size:
                raise ConfigError(f"ipsec.unprotected_links: enlace {n!r} fora de 1..{self.ring_size}.")
        if self.pki.key_type not in KEY_TYPES:
            raise ConfigError(f"pki.key_type deve ser um de {KEY_TYPES}")
        if not self.containers.kme_source_dir and self.containers.key_size_bits != 256:
            raise ConfigError("O KME do KMS-sim só entrega chaves de 256 bits: containers.key_size_bits deve ser 256.")
        if self.load.peers not in PEER_SELECTION:
            raise ConfigError(f"load.peers deve ser um de {PEER_SELECTION}")
