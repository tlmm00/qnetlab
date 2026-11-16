<!-- ARQUIVO GERADO por scripts/gen_config_reference.py — não edite à mão. -->
# 05 — Referência de configuração

Uma topologia é um arquivo YAML (ou JSON) declarativo. Tipos e padrões abaixo são extraídos do código (`src/qnetlab/config/models.py`); o schema JSON equivalente está em `schema/topology.schema.json`.

**Regras gerais**

- Chaves desconhecidas são **rejeitadas** (erro de digitação não passa em silêncio).
- Seções omitidas usam os padrões. Só `name` e `ring.size` são obrigatórios.
- Interpolação de ambiente em qualquer string: `${VAR}` (obrigatória) ou `${VAR:-padrão}`.
- Valide sempre com `qnetlab validate -t <arquivo>` antes de implantar.

## Raiz

| Chave | Tipo | Padrão | Descrição |
|---|---|---|---|
| `name` | string | obrigatório | Nome da topologia. Letras, dígitos, `-` e `_`. Nomeia o diretório `build/<name>/` e o projeto GNS3. |
| `description` | string | `""` | Texto livre. |
| `ring.size` | inteiro | obrigatório | Número de sites do anel, de 3 a 24 (cada site = 2 CHRs + KME + NODE). Limite de gerenciamento: veja `addressing.mgmt_step`. |

## `gns3`

Conexão com o servidor GNS3 (REST v2).

| Chave | Tipo | Padrão | Descrição |
|---|---|---|---|
| `url` | str | `"http://127.0.0.1:3080"` | URL base do servidor GNS3. O cliente acrescenta `/v{api_version}`. |
| `api_version` | int | `2` | Versão da API REST do GNS3 (a 2.2.x usa `2`). |
| `user` | str | `""` | Usuário HTTP Basic do servidor (vazio = sem autenticação). Use `${GNS3_USER:-}`. |
| `password` | str | `""` | Senha HTTP Basic. Use `${GNS3_PASSWORD:-}`; nunca grave a senha no YAML. |
| `project_name` | str | `""` | Nome do projeto no GNS3. Vazio = `name` da topologia. |
| `compute_id` | str | `"local"` | ID do compute do GNS3 onde os nós são criados. |
| `verify_tls` | bool | `true` | Valida o certificado TLS quando `url` é `https://`. |
| `timeout` | float | `30.0` | Timeout (s) de cada requisição REST. |
| `start_timeout` | float | `300.0` | Tempo máximo (s) para iniciar/parar todos os nós e aguardar o estado `started`. |

## `addressing`

Pools de endereços e rede de gerenciamento. Cada enlace ponto a ponto recebe um `/30` do pool correspondente (`.1` e `.2`). Veja [03 — Topologia e endereçamento](03-topology-addressing.md).

| Chave | Tipo | Padrão | Descrição |
|---|---|---|---|
| `kme_access_pool` | str | `"10.0.0.0/24"` | KME ↔ CHR (`ether1`): KME `.1`, CHR `.2`. |
| `ring_pool` | str | `"10.0.1.0/24"` | CHR ↔ CHR entre sites (`ether2`, enlace protegido por IPsec/QKD). O extremo de menor (site, j) fica com `.1`. |
| `sibling_pool` | str | `"10.0.2.0/24"` | CHR ↔ CHR dentro do site (`ether3`): MKTi1 `.1`, MKTi2 `.2`. |
| `node_pool` | str | `"10.0.3.0/24"` | CHR ↔ NODE (`ether7`): CHR `.1`, NODE `.2`. É o espaço de endereços protegido pelos túneis. |
| `kma_pool` | str | `"172.16.100.0/24"` | KME ↔ KME (enlaces KMA): KMEi `.1` (eth0), KME(i+1) `.2` (eth1). |
| `router_id_pool` | str | `"10.255.0.0/24"` | Base do router-id OSPF: `base + 10*site + j`. |
| `mgmt_subnet` | str | `"192.168.122.0/24"` | Rede do nó NAT do GNS3 (normalmente `virbr0`, `192.168.122.0/24`). O host precisa alcançá-la. |
| `mgmt_gateway` | str | `"192.168.122.1"` | Gateway da rede de gerenciamento (o IP do host em `virbr0`). |
| `mgmt_attach` | str | `"nat"` | `nat`: um nó NAT por CHR em `router.if_mgmt`; `none`: sem gerenciamento (não use com `configure`). |
| `mgmt_start` | int | `10` | Último octeto do IP de gerenciamento do 1º CHR (MKT11). |
| `mgmt_step` | int | `10` | Incremento entre CHRs consecutivos (MKT11, MKT12, MKT21…). Com `mgmt_step: 10` cabem anéis de até 12 sites; com `5`, até 24. |

## `router`

Roteadores MikroTik CHR: template, credenciais, versão do RouterOS e papel de cada interface.

| Chave | Tipo | Padrão | Descrição |
|---|---|---|---|
| `gns3_template` | str | `"MikroTik CHR"` | Nome do template do CHR no GNS3. |
| `admin_user` | str | `"admin"` | Usuário administrador do RouterOS. |
| `admin_password` | str | `"${QNETLAB_ADMIN_PASSWORD:-qnetlab-lab}"` | Senha do admin, definida no `bootstrap`. Use `${QNETLAB_ADMIN_PASSWORD:-…}`. |
| `boot_timeout` | float | `420.0` | Tempo máximo (s) para o console serial do CHR apresentar o prompt de login no `bootstrap`. |
| `min_version` | str | `"7.22"` | Versão mínima do RouterOS (QKD/PPK exigem ≥ 7.22). Vazio desliga a checagem. |
| `auto_update` | bool | `true` | Atualiza o CHR automaticamente quando a versão é menor que a pretendida. |
| `update_channel` | str | `"stable"` | Canal de atualização: `stable`, `long-term` ou `testing`. |
| `update_method` | str | `"host"` | `host`: o host baixa o `.npk` e o envia por FTP (recomendado); `device`: o CHR baixa (precisa de DNS/Internet). |
| `update_version` | str | `""` | Fixa a versão exata (ex.: `7.22.1`); o resultado é verificado. Vazio = `min_version`. Não pode ser menor que `min_version`. |
| `update_dns` | str | `""` | DNS usado só durante a atualização `device` (vazio = gateway do NAT). |
| `update_timeout` | float | `900.0` | Tempo máximo (s) de download/envio + reinício por roteador. |
| `if_kme` | str | `"ether1"` | Interface do CHR voltada ao KME do site. |
| `if_ring` | str | `"ether2"` | Interface voltada ao CHR do site vizinho (carrega o túnel IPsec/QKD). |
| `if_sibling` | str | `"ether3"` | Interface voltada ao outro CHR do mesmo site. |
| `if_node` | str | `"ether7"` | Interface voltada ao NODE do site. |
| `if_mgmt` | str | `"ether8"` | Interface voltada ao nó NAT (gerenciamento/REST). |

## `containers`

Imagem Docker dos KMEs/NODEs e portas dos serviços.

| Chave | Tipo | Padrão | Descrição |
|---|---|---|---|
| `base_image` | str | `"qnetlab/node:base"` | Tag da imagem-base única usada por KMEs e NODEs. |
| `kme_source_dir` | str | `""` | Pasta com um `main.py` de KME que substitui o vendorizado (ex.: `~/Codes/KMS-sim/src`). |
| `app_source_dir` | str | `""` | Pasta com a sua aplicação de NODE (ex.: cliente DFL) que substitui o agente embutido. |
| `kme_api_port` | int | `8443` | Porta HTTPS (mTLS) da API ETSI QKD 014 do KME. |
| `metrics_port` | int | `9100` | Porta HTTP do agente do NODE (`/control/*`, `/metrics.jsonl`), acessada via `docker exec`. |
| `probe_port` | int | `9000` | Porta UDP do eco/sondas entre NODEs. |
| `key_size_bits` | int | `256` | Tamanho de chave em bits. O KMS-sim só entrega 256; outro valor exige `kme_source_dir`. |

## `kme`

Parâmetros de `peer_sync` do KME (esquema do KMS-sim). Os padrões são os do código do KME.

| Chave | Tipo | Padrão | Descrição |
|---|---|---|---|
| `sync_interval_s` | float | `5` | Intervalo (s) entre varreduras de sincronização de chaves com cada vizinho. |
| `generation_delay_s` | float | `0.5` | Pausa (s) entre chaves de uma varredura — controla a taxa de geração simulada. |
| `sync_threshold` | int | `100` | Quantidade de chaves sincronizadas mantidas por vizinho. |
| `request_timeout_ms` | int | `5000` | Timeout (ms) das requisições de sincronização entre KMEs. |
| `max_retries` | int | `3` | Tentativas por requisição de sincronização. |
| `log_level` | str | `"INFO"` | Nível de log do KME (`DEBUG`, `INFO`, …). |

## `ospf`

OSPFv2 entre os CHRs.

| Chave | Tipo | Padrão | Descrição |
|---|---|---|---|
| `instance` | str | `"ospf-v2"` | Nome da instância OSPF. |
| `area` | str | `"backbone"` | Nome da área. |
| `area_id` | str | `"0.0.0.0"` | Identificador da área (backbone = `0.0.0.0`). |
| `ring_cost` | int | `10` | Custo OSPF dos enlaces entre sites (`ether2`). |
| `sibling_cost` | int | `10` | Custo OSPF do enlace entre os dois CHRs do site (`ether3`). |

## `ipsec`

IPsec IKEv2 com PPK nos enlaces entre sites. Cada CHR tem exatamente um enlace `ether2` e, portanto, um túnel e um objeto `/ip ipsec key qkd`.

| Chave | Tipo | Padrão | Descrição |
|---|---|---|---|
| `enabled` | bool | `true` | Gera túneis IPsec (`false` = laboratório só com OSPF). |
| `dh_group` | str | `"ecp384"` | Grupo DH do IKE (profile). |
| `pfs_group` | str | `"modp2048"` | Grupo PFS do ESP (proposal). |
| `hash_algorithm` | str | `"sha256"` | Hash do IKE (profile). |
| `enc_algorithm` | str | `"aes-256"` | Cifra do IKE (profile). |
| `proposal_auth` | str | `"sha256"` | Autenticação do ESP (proposal). |
| `proposal_enc` | str | `"aes-256-cbc"` | Cifra do ESP (proposal). |
| `lifetime` | str | `"1h"` | Tempo de vida da SA do IKE (profile). |
| `proposal_lifetime` | str | `"1m"` | Tempo de vida da Child/ESP SA (proposal). Com `1m`, o túnel rechaveia a cada minuto (uma chave QKD por rekey). Formatos: `30s`, `1m`, `1h`, `00:01:00`. |
| `auth_method` | str | `"psk"` | `psk` ou `cert` (autenticação do IKE). |
| `psk` | str | `""` | PSK fixo; vazio = um segredo aleatório por túnel, guardado em `state.json`. |
| `ppk_mode` | str | `"qkd"` | `qkd` (chave PPK vem do KME), `static` (PPK estático, só para testes) ou `none`. |
| `qkd_key_size` | int | `256` | `key-size` de `/ip ipsec key qkd` (o KME trata ≤ 128 como bytes). |
| `qkd_cache_size` | int | `10` | `cache-size` de `/ip ipsec key qkd`. |
| `enforce_encryption` | bool | `true` | Descarta no `forward` tráfego NODE↔NODE em claro (sem política IPsec) em `ether2`. |
| `unprotected_links` | lista de inteiros | `[]` | Enlaces do anel (1..N) deixados em claro — grupo de controle para comparar latência com/sem IPsec. |

## `pki`

Autoridade certificadora (OpenSSL) e certificados.

| Chave | Tipo | Padrão | Descrição |
|---|---|---|---|
| `key_type` | str | `"rsa-3072"` | Tipo de chave: `rsa-2048`, `rsa-3072`, `rsa-4096`, `ec-p256` ou `ec-p384`. |
| `validity_days` | int | `825` | Validade (dias) dos certificados de KMEs e CHRs. |
| `ca_validity_days` | int | `3650` | Validade (dias) da Root CA. |
| `organization` | str | `"QNetLab"` | Atributo `O` dos certificados. |
| `country` | str | `"BR"` | Atributo `C` dos certificados. |

## `load`

Carga de teste executada pelos agentes dos NODEs (`qnetlab load` / `run`).

| Chave | Tipo | Padrão | Descrição |
|---|---|---|---|
| `duration_s` | int | `60` | Duração (s) da carga. |
| `warmup_s` | int | `10` | Espera (s) antes de iniciar, para o IKE/QKD estabilizar. |
| `probe_interval_ms` | int | `100` | Intervalo (ms) entre sondas UDP por par. |
| `probe_size_bytes` | int | `64` | Tamanho nominal (bytes) da sonda, mínimo 16; o payload UDP real tem `size − 4` bytes (veja o README do agente). |
| `key_interval_s` | int | `0` | Reservado (0 = desligado): neste laboratório os SAEs são os CHRs, não os NODEs. |
| `peers` | str | `"adjacent"` | `adjacent`: cada NODE sonda os NODEs vizinhos pelos túneis; `none`: sem sondas. |

## Validações aplicadas

| Regra | Erro se violada |
|---|---|
| `name` com caracteres fora de `[A-Za-z0-9_-]` | `ConfigError` |
| `ring.size` fora de 3..24 | `ConfigError` |
| pools/rede/gateway que não são IPv4 válidos | `ConfigError` |
| `router.min_version` / `update_version` fora de `N.N[.N]`; `update_version` < `min_version` | `ConfigError` |
| `router.update_method` ∉ {host, device}; `update_channel` ∉ {stable, long-term, testing} | `ConfigError` |
| `router.if_*` que não seja `etherN` ou que se repita entre papéis | `ConfigError` |
| `ipsec.proposal_lifetime` fora de `30s` / `1m` / `1h` / `00:01:00` | `ConfigError` |
| `ipsec.ppk_mode` ∉ {qkd, static, none}; `auth_method` ∉ {psk, cert} | `ConfigError` |
| `ipsec.unprotected_links` com enlace fora de 1..N | `ConfigError` |
| `pki.key_type` desconhecido | `ConfigError` |
| `containers.key_size_bits` ≠ 256 sem `kme_source_dir` | `ConfigError` |
| `load.peers` ∉ {adjacent, none} | `ConfigError` |
| pools sobrepostos, pool sem espaço para os `/30` necessários, `mgmt_subnet` que não seja `/24`, `mgmt_gateway` fora da rede ou colidindo com um CHR, ou IP de gerenciamento além de `.254` | `AddressingError` |
