# 01 — Visão geral e glossário

## 1. Propósito

Pesquisas em redes quântico-seguras precisam de ambientes **repetíveis**: o mesmo anel, com os mesmos endereços, certificados,
parâmetros de IPsec e carga, executado quantas vezes for necessário e com resultados comparáveis. Montar isso à mão no
GNS3 (24 nós e 36 enlaces no anel de 4 sites; 36 e 54 no de 6; mais PKI e scripts RouterOS) é lento e propenso a erro.

O QNetLab transforma esse ambiente em **código**: um arquivo YAML descreve o anel e um pipeline determinístico produz,
a partir dele, o projeto GNS3, a PKI, as imagens Docker, a configuração dos roteadores, a carga de teste e um pacote de
resultados.

## 2. O que o QNetLab faz

- Cria no GNS3 (REST, porta 3080) um anel de **N sites** (N de 3 a 24; os cenários prontos usam 4 e 6). Cada site tem
  2 roteadores MikroTik CHR, 1 KME, 1 NODE e um nó NAT do GNS3 por CHR. Não há switches.
- Planeja endereçamento, enlaces e túneis de forma **determinística** (mesma topologia ⇒ mesmo plano).
- Emite uma **PKI** com OpenSSL: Root CA, certificados dos KMEs (servidor+cliente) e dos SAEs (os próprios CHRs).
- Constrói a imagem Docker única de KMEs/NODEs (KME do KMS-sim vendorizado + agente de telemetria).
- Garante a versão do RouterOS (≥ 7.22, ou exatamente a fixada em `router.update_version`), atualizando os CHRs sozinho.
- Configura os CHRs: OSPFv2, IPsec IKEv2 com **PPK obtido por QKD** (`/ip ipsec key qkd`), *bypass* de tráfego do
  próprio roteador, firewall que impede tráfego dos NODEs em claro, logging de depuração.
- Verifica o resultado (vizinhos OSPF, peers/SAs IPsec, estado QKD).
- Gera carga (sondas UDP entre NODEs adjacentes), coleta logs/métricas, produz gráficos e `output.tar.gz`.
- Destrói o laboratório.

## 3. O que o QNetLab **não** faz

| Fora de escopo | Observação |
|---|---|
| Fornecer o GNS3, a imagem do MikroTik CHR ou licenças | O template `MikroTik CHR` deve existir no GNS3. |
| Roteadores Cisco/Dynamips ou *switches* | Decisão de projeto: **somente MikroTik CHR** (veja [Decisões](decisions.md), ADR-02). |
| Implementar um KME próprio | Usa o KME do KMS-sim (Flask, ETSI GS QKD 014), vendorizado **sem alterações**. |
| Física quântica | Os KMEs *simulam* a geração/sincronização de chaves por *seed* compartilhada. |
| Segurança de produção | É um laboratório de pesquisa: segredos de teste, mgmt sem TLS, PSK de laboratório ([07](07-security.md)). |
| Medir vazão (throughput) | O agente mede RTT e perda por UDP; para vazão use `iperf3` (instalado na imagem). |
| Testes automatizados contra GNS3 real | Verificação ponta a ponta é manual ([13](13-limitations-roadmap.md)). |

## 4. Público e pré-requisitos de conhecimento

Pesquisadores e engenheiros de redes que conhecem GNS3, RouterOS, IPsec e noções de QKD. Não é preciso conhecer o
código para operar o framework; para estendê-lo, veja [12 — Desenvolvimento](12-development.md).

## 5. Cenários prontos

| Arquivo | Sites | CHRs | KMEs | NODEs | NATs | Túneis IPsec |
|---|---|---|---|---|---|---|
| `topologies/ring4.yaml` | 4 | 8 | 4 | 4 | 8 | 4 |
| `topologies/ring6.yaml` | 6 | 12 | 6 | 6 | 12 | 6 |

Reproduzem `Rede_MITACS_1_ring4.pdf` e `Rede_MITACS_3_ring6.pdf`. O ring4 usa blocos regulares de `/30` em vez dos blocos
irregulares do PDF original (normalização assumida; veja [03](03-topology-addressing.md#7-diferenças-em-relação-aos-pdfs)).

## 6. Glossário

| Termo | Significado |
|---|---|
| **QKD** | *Quantum Key Distribution*: distribuição de chaves simétricas com segurança baseada em princípios quânticos. |
| **KME** | *Key Management Entity* (ETSI GS QKD 014): serve chaves por API REST (mTLS) aos SAEs. Aqui, um contêiner Flask do KMS-sim. |
| **KMA / KSA** | Sub-camadas de gerenciamento/suprimento de chaves da ITU-T Y.3803. *Enlace KMA* = ligação KME↔KME por onde as chaves são sincronizadas. |
| **SAE** | *Secure Application Entity*: o consumidor de chaves. Neste laboratório, **cada CHR é um SAE** (`sae-chrXY`). |
| **PPK** | *Postquantum Preshared Key* (RFC 8784): chave simétrica extra misturada ao IKEv2 para resistência quântica. Aqui vem do QKD. |
| **IKEv2 / ESP** | Protocolo de troca de chaves / de encapsulamento do IPsec. |
| **Profile / Proposal / Peer / Identity / Policy** | Objetos IPsec do RouterOS (IKE, ESP, vizinho, credencial, o que cifrar). |
| **CHR** | *Cloud Hosted Router*: RouterOS x86 virtualizável. |
| **NODE** | Contêiner com 2 NICs por site; cliente da aplicação (ex.: aprendizado federado DFL) e agente de telemetria. |
| **NAT (nó)** | Nó "nuvem" nativo do GNS3 que liga um CHR (em `ether8`) à rede `virbr0` do host: gerenciamento e Internet. |
| **mgmt** | Rede de gerenciamento `192.168.122.0/24` (REST/FTP dos CHRs). KMEs e NODEs **não** têm mgmt: usa-se `docker exec`. |
| **Bypass** | Política IPsec `action=none` para tráfego do próprio roteador, que não deve ser encapsulado. |
| **Plano** (`AddressPlan`) | Estrutura imutável com IPs, enlaces e túneis calculados a partir da topologia. |
| **Workspace** | Diretório `build/<topologia>/` com artefatos, logs, métricas e `state.json`. |
| **Idempotente** | Reexecutar o comando leva ao mesmo estado final sem acumular lixo. |
