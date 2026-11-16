# Decisões de projeto (ADRs)

Registro curto das decisões arquiteturais, no formato *contexto → decisão → consequências*. Novas decisões entram no
fim, com o próximo número; decisões revogadas permanecem, marcadas como **Substituída**.

| ID | Decisão | Estado |
|---|---|---|
| ADR-01 | Topologia declarativa em YAML/JSON → plano determinístico | Aceita |
| ADR-02 | Somente MikroTik CHR; sem switches, sem Dynamips | Aceita |
| ADR-03 | KME do KMS-sim vendorizado sem alterações | Aceita |
| ADR-04 | Configuração dos CHRs por REST + `.rsc` gerado, idempotente | Aceita |
| ADR-05 | Certificados importados por REST, não pelo `.rsc` | Aceita |
| ADR-06 | Atualização do RouterOS *host-side* (`.npk` por FTP) | Aceita |
| ADR-07 | Nós NAT nativos do GNS3 para gerenciamento | Aceita |
| ADR-08 | KMEs/NODEs sem rede de gerenciamento (`docker exec`) | Aceita |
| ADR-09 | Um túnel e um `key qkd` por CHR | Aceita |
| ADR-10 | `proposal_lifetime` de 1 minuto | Aceita |
| ADR-11 | `enforce_encryption` e grupo de controle (`unprotected_links`) | Aceita |
| ADR-12 | Segredos em `state.json` (0600), mascarados nos pacotes | Aceita |
| ADR-13 | Referência de configuração gerada e verificada por teste | Aceita |

---

## ADR-01 — Topologia declarativa → plano determinístico

**Contexto.** Os laboratórios MITACS (ring4, ring6) são regulares; montá-los à mão é lento e não reprodutível.
**Decisão.** Um YAML com `ring.size` e pools; `build_plan()` é função pura que calcula IPs, enlaces e túneis. Dataclasses
imutáveis validam e rejeitam chaves desconhecidas.
**Consequências.** Mesma entrada ⇒ mesmo plano; testes sem infraestrutura; qualquer tamanho de anel (3–24) sem código novo.
O ring4 do PDF original tem blocos irregulares, que foram normalizados ([03](03-topology-addressing.md#7-diferenças-em-relação-aos-pdfs)).

## ADR-02 — Somente MikroTik CHR

**Contexto.** O GNS3 respondeu `409 "Could not find Dynamips"` ao criar switches/roteadores Cisco; o usuário não possui
imagens Cisco e os laboratórios usam MikroTik.
**Decisão.** Todos os roteadores são CHR; **não há switches** — cada ligação é ponto a ponto (por isso os `/30`).
**Consequências.** Sem dependência de Dynamips/IOS; mais enlaces no projeto GNS3 (36 no ring4); a topologia lógica dos PDFs
é preservada.

## ADR-03 — KME do KMS-sim vendorizado sem alterações

**Contexto.** O grupo mantém um KME de pesquisa (Flask, ETSI GS QKD 014 + KMA/KSA).
**Decisão.** `docker/node/kme/main.py` é cópia byte a byte (SHA-256 em `UPSTREAM.md`); o framework só **gera** o
`kme_config.yaml`. `containers.kme_source_dir` permite apontar para o repositório vivo.
**Consequências.** O KME é o mesmo do restante da pesquisa; o framework acompanha o esquema de configuração dele
(`peer_sync`, `kme_topology`). Limitação herdada: só chaves de 256 bits.

## ADR-04 — Configuração por REST + `.rsc` gerado e idempotente

**Contexto.** RouterOS aceita scripts, mas falhas parciais são difíceis de detectar e reexecutar.
**Decisão.** Templates Jinja2 (modo estrito) geram um `.rsc` por CHR, enviado por FTP e executado com `/import`. Cada seção
começa removendo o que criou antes (por `comment=qnetlab-*` ou por nome/peer quando o objeto não aceita `comment`). O
sucesso é decidido por "Script Error"/"failure:" na saída (o 7.22 não imprime "executed successfully").
**Consequências.** `configure` é reaplicável. Itens sem `comment` (`profile`, `proposal`, `peer`, `identity` no 7.22) são
limpos por nome — nomes `qkd-prof`, `qkd-prop` e `to-*` são reservados pelo framework.

## ADR-05 — Certificados importados por REST

**Contexto.** `/certificate import file-name=…` dentro do `.rsc` falhou ("input does not match any value of file-name"):
o arquivo ainda não estava visível no momento da importação.
**Decisão.** O configurador envia os PEMs por FTP, **confere em `/file`**, importa por REST com retentativas, renomeia
(`qnetlab-ca`, `sae-chrXY`), marca a CA como confiável e apaga os PEMs do roteador. O `.rsc` apenas documenta os certificados esperados.
**Consequências.** Erros claros e retentáveis; a chave privada do CHR só existe no roteador depois da importação.

## ADR-06 — Atualização do RouterOS *host-side*

**Contexto.** O QKD exige RouterOS ≥ 7.22; os CHRs do template vinham em 7.16. A atualização *online* pelo próprio CHR
falhou por DNS ("could not resolve dns name (timeout)", 900 s de espera) mesmo com Internet.
**Decisão.** `router.update_method: host` (padrão): o host descobre/baixa o `.npk` (cache em `build/<lab>/cache`, tamanho
≥ 5 MB), envia por FTP, reinicia e **verifica a versão exata** (`update_version`, ex.: `7.22.1`). `device` continua
disponível. Nunca faz *downgrade*.
**Consequências.** Independe de DNS no CHR; o host precisa alcançar `download.mikrotik.com`/`upgrade.mikrotik.com`. Cada
`up --recreate` recria CHRs a partir do template, repetindo a atualização, a menos que o template seja atualizado.

## ADR-07 — Nós NAT nativos do GNS3

**Decisão.** Um nó NAT (template nativo, ícone de nuvem) por CHR, em `ether8`, nomeado `NAT1..NAT2N` na ordem dos CHRs,
como nos laboratórios. Há fallback para criar `nat` diretamente se o template não existir.
**Consequências.** Gerenciamento e Internet sem *switch*; depende de `virbr0` (`192.168.122.0/24`) estar ativo no host.

## ADR-08 — KMEs/NODEs sem rede de gerenciamento

**Decisão.** Contêineres só têm as interfaces do laboratório; o framework opera por `docker exec`/`docker cp` no contêiner
`GNS3.<nó>.<project_id>`.
**Consequências.** Mantém o plano de dados idêntico ao do laboratório físico; exige que o Docker do host seja o mesmo que o
GNS3 usa (atenção ao *context* do Docker Desktop — [11](11-troubleshooting.md#docker-build-aponta-para-o-socket-do-docker-desktop)).

## ADR-09 — Um túnel e um `key qkd` por CHR

**Contexto.** `/ip ipsec key qkd` é objeto único por roteador, com um só `peer-sae-id`.
**Decisão.** Cada CHR tem exatamente um enlace `ether2` ⇒ um túnel ⇒ um SAE par. O túnel protege o `/30` CHR↔NODE de uma ponta
contra o da outra, com SA entre os IPs de `ether2`, sem `sa-*` na política (espelha os PDFs).
**Consequências.** Sem conflito de `peer-sae-id`; a malha de túneis é um anel, não uma malha completa.

## ADR-10 — `proposal_lifetime` de 1 minuto

**Decisão.** `ipsec.proposal_lifetime: 1m` (IKE: `1h`). Valor configurável e validado por regex.
**Consequências.** Um rekey (e portanto uma chave QKD nova) por minuto por túnel — útil para exercitar o consumo de chaves;
aumenta a carga de IKE e o consumo dos pools dos KMEs.

## ADR-11 — Imposição de cifragem e grupo de controle

**Decisão.** `enforce_encryption` (padrão `true`) descarta no `forward` tráfego NODE↔NODE em claro por `ether2`.
`unprotected_links` deixa enlaces escolhidos sem túnel para comparar latência com/sem IPsec.
**Consequências.** Falha de IPsec aparece como perda de pacotes (e não como vazamento em claro); os CHRs de um enlace
listado em `unprotected_links` não recebem túnel **nem** as regras de firewall da seção 40 (inclusive o descarte), de modo
que o tráfego flui em claro só ali.

## ADR-12 — Segredos em `state.json` (0600)

**Decisão.** PSKs por túnel, *seeds* dos KMEs por par e PPK estático são gerados com `secrets` na primeira necessidade e
guardados em `build/<lab>/state.json` (escrita atômica, modo 0600). `output.tar.gz` mascara `secret=`, `ppk-secret=` e
`password=` dos `.rsc`.
**Consequências.** Reexecuções são reprodutíveis e laboratórios distintos nunca compartilham segredos; `build/` não deve ser
versionado nem compartilhado sem revisão.

## ADR-13 — Referência de configuração gerada

**Decisão.** `docs/05-configuration-reference.md` é gerado por `scripts/gen_config_reference.py` a partir dos dataclasses;
`tests/test_docs.py` falha se houver campo sem descrição ou arquivo desatualizado.
**Consequências.** Documentação de configuração não diverge do código; adicionar um campo exige descrevê-lo.
