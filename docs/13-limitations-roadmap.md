# 13 — Limitações, riscos conhecidos e roteiro

## 1. Estado de validação

| Parte | Como foi validada | Confiança |
|---|---|---|
| Configuração, endereçamento (ring6 conferido com a tabela do laboratório), túneis, PKI/OpenSSL, templates `.rsc`, compose | testes automatizados | alta |
| KME do KMS-sim com o `kme_config.yaml` gerado, sync KMA entre 2 KMEs, mTLS com SAN de IP, consulta SQL do coletor, agente do NODE, consolidação/gráficos | teste ponta a ponta local real (2 processos do `main.py` + OpenSSL) | alta |
| Login/execução no console RouterOS | servidor telnet falso | média |
| Deployer GNS3 (nós, adaptadores, portas, enlaces, NAT) e `docker exec` | cliente GNS3 e runner Docker falsos | média |
| Sintaxe RouterOS 7.22.1 (OSPF, IPsec, firewall, logging), importação de certificados, atualização do `.npk` | execução **real** pelo usuário, com correções incrementais (ver [11](11-troubleshooting.md)) | média-alta |
| `docker build` e *entrypoint* com `ip` reais; verificação final (`verify` com IPsec+QKD) e `run` em GNS3 real | **não coberto por testes automáticos** | a confirmar |

O ambiente de desenvolvimento do framework não dispõe de GNS3, Docker daemon nem iproute2; por isso, na primeira execução em um
ambiente novo use `-v` e espere ajustes finos.

## 2. Limitações conhecidas

### Escopo e topologia

- Apenas **anel de sites regulares** (2 CHRs + KME + NODE por site), N de 3 a 24. Malhas, outros formatos e mais de dois CHRs
  por site exigem estender `addressing.build_plan()`.
- Um túnel IPsec/QKD por CHR (limitação do objeto único `/ip ipsec key qkd`). Não há malha completa de túneis.
- Somente **MikroTik CHR** (sem Cisco/Dynamips, sem switches) e **GNS3 2.2.x** (API v2).
- O endereçamento do ring4 foi **normalizado** em relação ao PDF original ([03 §7](03-topology-addressing.md#7-diferenças-em-relação-aos-pdfs)).

### Gerenciamento

- O host precisa alcançar `192.168.122.0/24` (nós NAT → `virbr0`). Sem isso, `bootstrap` (após o console), `upgrade`,
  `configure`, `verify` e a coleta dos CHRs falham.
- Capacidade de IPs de gerenciamento: `mgmt_start + mgmt_step·(2N−1) ≤ 254` (`mgmt_step: 10` ⇒ N ≤ 12; `5` ⇒ N ≤ 24).
- KMEs e NODEs só são alcançáveis por `docker exec`: `load`, `collect` e o gatilho do IKE rodam **no host do Docker do GNS3**.
  GNS3 remoto (outro host) não é suportado nessas etapas.
- REST em HTTP e FTP em claro na rede de gerenciamento ([07](07-security.md)).

### RouterOS

- Exige RouterOS ≥ 7.22 (`/ip ipsec key qkd`, `ppk=qkd`). CHRs mais novos que o alvo não são rebaixados.
- Cada `make up --recreate` recria CHRs a partir da imagem do template ⇒ a atualização se repete se a imagem estiver antiga.
- CHR sem licença (trial) pode restringir atualização/velocidade.
- A semântica de `key-size` do QKD (bits × bytes) diverge entre a referência CLI e a página de manual do RouterOS; o padrão
  (256) reproduz o laboratório de referência.
- `ppk_mode: static` tem sintaxe de `identity` **não validada** (`VERIFY` no template).
- O comportamento do IKE quando o PPK/KME está indisponível não foi caracterizado.

### KME e chaves

- O KME é um **simulador**: só entrega chaves de 256 bits e sincroniza por *seed* compartilhada.
- A coleta dos KMEs é por `docker exec` (log e SQLite), não por mTLS na API.
- O agente do NODE mede **RTT e perda por UDP**; pedidos de chave aos KMEs estão desligados (os SAEs são os CHRs). Vazão: `iperf3`.

### Telemetria

- `metrics.jsonl` é acumulativo dentro do contêiner; execuções repetidas misturam eventos no mesmo arquivo.
- Sem coleta contínua de CPU/memória dos CHRs nem de contadores de interface (apenas logs IPsec e snapshots de estado).
- Os gráficos exigem `matplotlib`.

### Desempenho

- O boot de 8–12 CHRs consome CPU/RAM significativas; o tempo de `up` é dominado por boot e atualização do RouterOS.
- `make test` leva ~2 min por testes de atualização que consultam DNS real (candidato a ser isolado — ver roteiro).

## 3. Riscos e mitigação

| Risco | Impacto | Mitigação |
|---|---|---|
| Mudança de sintaxe em versões futuras do RouterOS | `/import` falha | Fixe `update_version`; revise `render` ao subir de versão; teste de sintaxe em um CHR |
| `download.mikrotik.com` inacessível | `upgrade` falha | Pré-popular `build/<lab>/cache/` com o `.npk`; atualizar a imagem do template |
| Perda de `state.json` | `deploy --recreate` necessário | Não rode `make clean` com projeto ativo; faça backup seguro (contém segredos) |
| Conflito de IPs de gerenciamento entre laboratórios | ARP duplicado, falhas intermitentes | Um laboratório por vez ou `mgmt_start` distinto |
| Imagens com chave privada do KME | Vazamento se publicadas | Não use *registry* público |

## 4. Roteiro (propostas, não compromissos)

**Curto prazo**
- [ ] Isolar do `make test` os testes que usam DNS real (marcador `network`), reduzindo a suíte a segundos.
- [ ] `collect`: abortar cedo (com uma mensagem única) quando o primeiro CHR estiver inacessível, em vez de repetir o *timeout* por roteador.
- [ ] Teste de sintaxe do `.rsc` contra um CHR 7.22.1 em CI (self-hosted).
- [ ] Validar `ppk_mode: static` e o comportamento de PPK obrigatório/opcional.

**Médio prazo**
- [ ] Transporte seguro na gerência (`www-ssl`, SFTP/SCP) como opção.
- [ ] Execução de experimentos em lote (varredura de parâmetros) com `Lab` e consolidação comparativa.
- [ ] Métricas de CPU/memória/contadores de interface dos CHRs e de consumo/nível dos pools de chaves ao longo do tempo.
- [ ] Rotação/renovação de certificados e `--only <nó>` para reconfiguração parcial.
- [ ] Suporte a GNS3 remoto (execução de comandos nos contêineres via API do GNS3 ou agente).

**Longo prazo**
- [ ] Outras formas de topologia (malha, estrela, múltiplos CHRs por site).
- [ ] Integração com KMEs/QKD reais (substituindo o simulador) e com aplicações DFL completas.
- [ ] Suporte a outros emuladores (EVE-NG, Containerlab) mantendo o mesmo plano de endereçamento.

## 5. Como reportar problemas

Inclua: versão do QNetLab (`pip show qnetlab`), YAML usado, versão do GNS3 e do RouterOS, comando executado com `-v`, trecho
relevante de `build/<lab>/logs/` e a saída de `qnetlab plan --json`. **Remova segredos** (`secret=`, PSKs, `state.json`) antes
de anexar ([07 §6](07-security.md)).
