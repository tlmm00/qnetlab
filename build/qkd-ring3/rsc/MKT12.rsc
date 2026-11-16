# =============================================================================
# QNetLab — configuração de MKT12 (laboratório qkd-ring3)
# Arquivo GERADO: não edite à mão; altere a topologia e rode `qnetlab configure`.
# Reaplicável: cada seção remove antes o que foi criado antes (itens com comment=qnetlab*).
# =============================================================================

# --- 00 base: identidade, endereços das interfaces e rotas estáticas ----------
# (o IP de gerenciamento em ether8 é configurado no bootstrap e não é tocado aqui)
/system identity set name=MKT12
/ip address remove [find where comment~"^qnetlab-"]
/ip address add address=10.0.0.6/30 interface=ether1 comment="qnetlab-kme KME1"
/ip address add address=10.0.1.5/30 interface=ether2 comment="qnetlab-ring MKT21"
/ip address add address=10.0.2.2/30 interface=ether3 comment="qnetlab-sibling MKT11"
/ip address add address=10.0.3.5/30 interface=ether7 comment="qnetlab-node NODE1"
/ip route remove [find where comment~"^qnetlab-"]
/ip route add dst-address=10.0.3.8/30 gateway=10.0.1.6 comment="qnetlab-static route mkt21"

# --- 10 OSPFv2 (RouterOS v7): ether2/ether3 ativas; interfaces de KME e NODE passivas -----
/routing ospf interface-template remove [find where comment~"^qnetlab-"]
/routing ospf area remove [find where name=backbone]
/routing ospf instance remove [find where name=ospf-v2]
/routing ospf instance add name=ospf-v2 version=2 router-id=10.255.0.12 comment=qnetlab-ospf
/routing ospf area add name=backbone area-id=0.0.0.0 instance=ospf-v2 comment=qnetlab-ospf
/routing ospf interface-template add area=backbone interfaces=ether1 passive comment="qnetlab-ospf kme"
/routing ospf interface-template add area=backbone interfaces=ether2 type=ptp cost=10 comment="qnetlab-ospf ring MKT21"
/routing ospf interface-template add area=backbone interfaces=ether3 type=ptp cost=10 comment="qnetlab-ospf sibling MKT11"
/routing ospf interface-template add area=backbone interfaces=ether7 passive comment="qnetlab-ospf node"
# --- 20 PKI: feita pelo configurador via REST antes deste script ------------------
# (envio por FTP + /certificate import + renomeação; os PEMs são apagados do roteador).
# O CN do certificado do roteador é a sua identidade SAE perante o KME (modo QKD).
# certificado esperado: qnetlab-ca (trusted)
# certificado esperado: sae-chr12

# --- 30 IPsec IKEv2 quantum-safe ----------------------------------------------
# PPK (RFC 8784/9867): modo "qkd" | autenticação IKE: "psk"
# (profile/proposal/peer/identity não aceitam "comment" no RouterOS 7.22: a limpeza é por nome/peer, como no laboratório)
/ip ipsec policy remove [find where comment~"^qnetlab-"]
/ip ipsec identity remove [find where peer~"^to-"]
/ip ipsec peer remove [find where name~"^to-"]
/ip ipsec proposal remove [find where name="qkd-prop"]
/ip ipsec profile remove [find where name="qkd-prof"]

# Gerenciador de chaves QKD (objeto único por roteador: um peer-sae-id).
# O CN de "certificate" é o SAE ID perante o KME; "kme-id" é validado contra o certificado do KME.
/ip ipsec key qkd set enabled=yes address=10.0.0.5:8443 certificate=sae-chr12 kme-id=kme-sim-1 peer-sae-id=sae-chr21 key-size=256 cache-size=10
/ip ipsec profile add name=qkd-prof dh-group=ecp384 enc-algorithm=aes-256 hash-algorithm=sha256 lifetime=1h ppk=qkd
/ip ipsec proposal add name=qkd-prop auth-algorithms=sha256 enc-algorithms=aes-256-cbc pfs-group=modp2048 lifetime=1m

# Bypass (action=none) ANTES da política de túnel: tráfego originado/destinado ao próprio roteador
# dentro do pool NODE não é encapsulado.
/ip ipsec policy add src-address=10.0.3.5/32 dst-address=10.0.3.0/24 action=none comment="qnetlab-bypass router-originated"
/ip ipsec policy add src-address=10.0.3.0/24 dst-address=10.0.3.5/32 action=none comment="qnetlab-bypass router-destined"

# Túnel MKT12 <-> MKT21 (SA 10.0.1.5 <-> 10.0.1.6), protege 10.0.3.4/30 <-> 10.0.3.8/30
/ip ipsec peer add name=to-MKT21 address=10.0.1.6/32 local-address=10.0.1.5 exchange-mode=ike2 profile=qkd-prof
/ip ipsec identity add peer=to-MKT21 auth-method=pre-shared-key secret="847186da67474394bce269376ce202af0d9dfb21dfb9b593d2328fee11755a06"
/ip ipsec policy add peer=to-MKT21 tunnel=yes src-address=10.0.3.4/30 dst-address=10.0.3.8/30 proposal=qkd-prop action=encrypt level=require comment="qnetlab-policy"

# --- 40 firewall/NAT: IKE/ESP em ether2, isenção de NAT e bloqueio de tráfego NODE em claro ----
/ip firewall filter remove [find where comment~"^qnetlab-"]
/ip firewall nat remove [find where comment~"^qnetlab-"]
/ip firewall filter add chain=output out-interface=ether2 protocol=udp dst-port=500,4500 action=accept comment=qnetlab-ike-out
/ip firewall filter add chain=output out-interface=ether2 protocol=ipsec-esp action=accept comment=qnetlab-esp-out
/ip firewall filter add chain=input in-interface=ether2 protocol=udp dst-port=500,4500 action=accept comment=qnetlab-ike-in
/ip firewall filter add chain=input in-interface=ether2 protocol=ipsec-esp action=accept comment=qnetlab-esp-in
# Nada do pool NODE trafega em claro pelo enlace entre sites
/ip firewall filter add chain=forward out-interface=ether2 src-address=10.0.3.0/24 dst-address=10.0.3.0/24 ipsec-policy=out,none action=drop comment=qnetlab-drop-clear-out
/ip firewall filter add chain=forward in-interface=ether2 src-address=10.0.3.0/24 dst-address=10.0.3.0/24 ipsec-policy=in,none action=drop comment=qnetlab-drop-clear-in
/ip firewall nat add chain=srcnat action=accept src-address=10.0.3.4/30 dst-address=10.0.3.8/30 comment="qnetlab-nat-bypass MKT12-MKT21"

# --- 50 logging: depuração IPsec em memória (extraída pelo coletor) -------------
/system logging remove [find where comment~"^qnetlab-"]
/system logging action set memory memory-lines=10000
/system logging add topics=ipsec,debug action=memory comment=qnetlab-ipsec-debug
