# =============================================================================
# QNetLab — configuração de MKT31 (laboratório qkd-ring4-control)
# Arquivo GERADO: não edite à mão; altere a topologia e rode `qnetlab configure`.
# Reaplicável: cada seção remove antes o que foi criado antes (itens com comment=qnetlab*).
# =============================================================================

# --- 00 base: identidade, endereços das interfaces e rotas estáticas ----------
# (o IP de gerenciamento em ether8 é configurado no bootstrap e não é tocado aqui)
/system identity set name=MKT31
/ip address remove [find where comment~"^qnetlab-"]
/ip address add address=10.0.0.18/30 interface=ether1 comment="qnetlab-kme KME3"
/ip address add address=10.0.1.10/30 interface=ether2 comment="qnetlab-ring MKT22"
/ip address add address=10.0.2.9/30 interface=ether3 comment="qnetlab-sibling MKT32"
/ip address add address=10.0.3.17/30 interface=ether7 comment="qnetlab-node NODE3"
/ip route remove [find where comment~"^qnetlab-"]
/ip route add dst-address=10.0.3.12/30 gateway=10.0.1.9 comment="qnetlab-static route mkt22"

# Descoberta de vizinhos (MNDP/LLDP/CDP): anuncia identidade e versão do RouterOS em claro em TODAS as interfaces, inclusive
# no enlace do túnel (achado do SEG-01). Controlado por router.neighbor_discovery.
/ip neighbor discovery-settings set discover-interface-list=none

# --- 10 OSPFv2 (RouterOS v7): ether2/ether3 ativas; interfaces de KME e NODE passivas -----
/routing ospf interface-template remove [find where comment~"^qnetlab-"]
/routing ospf area remove [find where name=backbone]
/routing ospf instance remove [find where name=ospf-v2]
/routing ospf instance add name=ospf-v2 version=2 router-id=10.255.0.31 comment=qnetlab-ospf
/routing ospf area add name=backbone area-id=0.0.0.0 instance=ospf-v2 comment=qnetlab-ospf
/routing ospf interface-template add area=backbone interfaces=ether1 passive comment="qnetlab-ospf kme"
/routing ospf interface-template add area=backbone interfaces=ether2 type=ptp cost=10 comment="qnetlab-ospf ring MKT22"
/routing ospf interface-template add area=backbone interfaces=ether3 type=ptp cost=10 comment="qnetlab-ospf sibling MKT32"
/routing ospf interface-template add area=backbone interfaces=ether7 passive comment="qnetlab-ospf node"
# --- 20 PKI: feita pelo configurador via REST antes deste script ------------------
# (envio por FTP + /certificate import + renomeação; os PEMs são apagados do roteador).
# O CN do certificado do roteador é a sua identidade SAE perante o KME (modo QKD).
# certificado esperado: qnetlab-ca (trusted)
# certificado esperado: sae-chr31

# --- 30 IPsec IKEv2 quantum-safe ----------------------------------------------
# PPK (RFC 8784/9867): modo "qkd" | autenticação IKE: "psk"
# (profile/proposal/peer/identity não aceitam "comment" no RouterOS 7.22: a limpeza é por nome/peer, como no laboratório)
/ip ipsec policy remove [find where comment~"^qnetlab-"]
/ip ipsec identity remove [find where peer~"^to-"]
/ip ipsec peer remove [find where name~"^to-"]
/ip ipsec proposal remove [find where name="qkd-prop"]
/ip ipsec profile remove [find where name="qkd-prof"]


# --- 40 firewall/NAT: IKE/ESP em ether2, isenção de NAT e bloqueio de tráfego NODE em claro ----
/ip firewall filter remove [find where comment~"^qnetlab-"]
/ip firewall nat remove [find where comment~"^qnetlab-"]

# --- 50 logging: depuração IPsec em memória (extraída pelo coletor) -------------
/system logging remove [find where comment~"^qnetlab-"]
/system logging action set memory memory-lines=10000
/system logging add topics=ipsec,debug action=memory comment=qnetlab-ipsec-debug
