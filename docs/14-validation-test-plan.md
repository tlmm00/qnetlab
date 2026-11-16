# 14 — Plano de validação do framework

Este plano define **como demonstrar, com evidências, que o QNetLab serve ao que se propõe**: estudar redes QKD (QKDNs) com
IPsec/PPK em laboratórios reproduzíveis. Ele organiza os testes pelos cinco princípios de validação — **fidelidade,
segurança, rede, desempenho e utilidade** — e separa o que já está coberto do que depende de laboratório real.

> **Como ler os "critérios".** Os limiares numéricos são **propostas** para discussão: devem ser calibrados com uma linha de base
> (FID-00) antes de virarem critérios de aceite. Os "resultados esperados hoje" vêm da leitura do código e são **hipóteses a
> confirmar**, não medições.

## 1. Resumo

| Princípio | Pergunta | Testes | Já coberto por `pytest` | Depende de laboratório real |
|---|---|---|---|---|
| **Fidelidade** | Reproduz o comportamento físico e protocolar esperado? | FID-00 … FID-10 | endereçamento, PKI, `.rsc`, KME↔KME (loopback) | IKEv2/PPK, rekey, OSPF, atraso/perda |
| **Segurança** | Detecta e impede os ataques que a teoria prevê? | SEG-01 … SEG-14 | parcialmente (SAN, cadeia) | captura de tráfego, impostores, DoS |
| **Rede** | Permite estudar problemas reais de QKDN? | REDE-01 … REDE-10 | — | falhas de KME/enlace, esgotamento de chaves |
| **Desempenho** | Quanto custa emular? | DES-01 … DES-08 | tempo da suíte | medições de host, escala |
| **Utilidade** | Integra com aplicações reais? | UTI-01 … UTI-08 | agente do NODE (sem rede real) | DFL, iperf3, outros KMEs, terceiros |

**Resultado esperado do plano:** uma *tabela de aderência* (§10) dizendo, para cada fenômeno de QKDN, se o QNetLab o
**reproduz**, o **aproxima** (e com que erro) ou **não o modela** — em vez de um "passou/não passou" genérico.

## 2. Escopo de validade: o que o framework modela

Antes de testar "fidelidade física", é preciso declarar o que existe no modelo. Hoje:

| Fenômeno | Estado | Observação |
|---|---|---|
| Canal quântico (fótons, QBER, perdas, ruído) | **Não modelado** | O KME é um simulador: chaves = `SHA-256(seed:contador)` em 256 bits. Não há QBER, taxa de bits dependente de distância, nem espionagem quântica. |
| Taxa de geração de chaves | **Parametrizada** | `kme.sync_interval_s`, `generation_delay_s`, `sync_threshold`; sem modelo físico por enlace. |
| Camada KMA (sincronização entre KMEs) | **Modelada** (simplificada) | Enlaces KMA reais no GNS3, mTLS, hash de verificação, contador. |
| Camada KSA (API ETSI GS QKD 014 para SAEs) | **Modelada** | `status`, `enc_keys`, `dec_keys`; só chaves de 256 bits. |
| Consumo de chaves pelo IPsec (`/ip ipsec key qkd`, PPK RFC 8784) | **Real** (RouterOS 7.22+) | O roteador é real; o comportamento é o do RouterOS. |
| Plano de dados clássico (IPsec, OSPF, firewall) | **Real** | CHR em Qemu: pilha de rede real, desempenho dependente da virtualização e da licença. |
| Atraso de propagação, perda, jitter do enlace | **Não modelado por padrão** | Pode ser injetado pelos filtros de link do GNS3 (§8). |
| Relés confiáveis (*trusted nodes*) multi-salto | **Parcial** | Um túnel por CHR entre sites adjacentes; não há roteamento de chaves multi-salto entre KMEs. |
| Autorização de SAEs, auditoria, revogação | **Mínima** | Autenticação por certificado; sem verificação de autorização no KME (§5). |

Consequência: o plano **testa a fidelidade de protocolo e de plano de controle/dados**, e **mede o erro** onde o modelo é uma
aproximação. Para fenômenos quânticos, ele define o que seria preciso estender (§8, HAB-07), em vez de fingir que são validados.

## 3. Metodologia comum

### 3.1 Níveis de teste

| Nível | Onde roda | Quando | Exemplos |
|---|---|---|---|
| **N0 — offline** | `pytest`, sem GNS3/Docker | a cada mudança | planos, PKI, templates, conformidade do KME em loopback |
| **N1 — fumaça** | laboratório ring4 real | a cada versão | `make up` + `verify` com `_ok: true`; túneis ativos |
| **N2 — experimento** | ring4/ring6 real | por teste do plano | capturas, injeção de falhas, carga |
| **N3 — escala/longa duração** | host dedicado | marcos | anéis maiores, 12–24 h de operação |

### 3.2 Regras para todos os experimentos

1. **Hipótese e critério antes da execução** (registrar no §10 antes de rodar).
2. **Controle positivo e negativo.** Toda detecção/bloqueio precisa de um caso em que o ataque *deve* ter efeito (ex.: enlace em
   `unprotected_links` mostra texto claro) e outro em que *não* (enlace protegido).
3. **Repetições:** ≥ 10 execuções independentes por configuração (recriando o laboratório quando o estado importa);
   relatar mediana, intervalo interquartil e IC 95 % por *bootstrap*. Descartar o *warm-up* (`load.warmup_s`).
4. **Uma variável por vez** e ordem das configurações sorteada.
5. **Registro do ambiente:** CPU/núcleos/RAM do host, kernel, versões do GNS3, RouterOS e Docker, *commit* do QNetLab, hash do
   YAML, licença do CHR. Tudo vai no pacote de evidências.
6. **Saturação do emulador é falha de medição:** se a CPU do host ou de qualquer CHR passar de um limite (proposta: 80 %) durante
   a coleta, o resultado é marcado como inválido (DES-05).
7. **Artefatos de evidência:** `output.tar.gz` do QNetLab + capturas `.pcap` + logs do experimento, com SHA-256.

### 3.3 Ferramentas

`tshark`/`wireshark` (análise das capturas dos enlaces do GNS3), `iperf3` (já na imagem do NODE), `docker stats`/`pidstat`
(host), consulta ao SQLite dos KMEs, `curl` com mTLS, scripts Python sobre `qnetlab.lab.Lab` para varreduras.

## 4. Fidelidade

**Pergunta:** o laboratório se comporta como a teoria e as normas preveem? **Referências:** ETSI GS QKD 014 (API), RFC 7296
(IKEv2), RFC 8784 (PPK), RFC 2328 (OSPFv2), ITU-T Y.3803 (KMA/KSA).

| ID | Experimento | Procedimento | Métrica | Critério proposto | Nível |
|---|---|---|---|---|---|
| FID-00 | **Linha de base** | `make up` + `make run` × 10 no ring4 em repouso | média/variância de RTT, tempo de `up`, uso de CPU | define as tolerâncias dos demais testes | N1 |
| FID-01 | Conformidade da API ETSI 014 | Suíte de testes da API do KME em loopback: campos de `status`, códigos 400/503, `size` múltiplo de 8, `number` 1–128, `key_ID` UUID, chave com 32 bytes, `enc_keys` → `dec_keys` devolve a mesma chave | % de requisitos atendidos | 100 % dos requisitos "devem" (RFC 2119) da norma; desvios listados | N0 |
| FID-02 | Igualdade de chave ponta a ponta | Durante 1 h, registrar `key_ID`/hash da chave obtida pelo CHR A e pela B (logs dos KMEs e `logs/KME*.log`) | nº de divergências; chaves repetidas | 0 divergências; 0 reutilizações de `key_ID` | N2 |
| FID-03 | PPK no IKEv2 (RFC 8784) | Capturar `ether2` e decodificar IKE: notificação de uso de PPK, `PPK_IDENTITY`, `IKE_AUTH` | presença e ordem das mensagens | mensagens presentes quando `ppk_mode: qkd`; ausentes com `none` | N2 |
| FID-04 | Cadência de *rekey* | Com `proposal_lifetime` = 30 s, 1 m, 5 m, medir intervalos entre `CREATE_CHILD_SA`/instalação de SA nos logs | intervalo medido vs. configurado | erro ≤ x % (calibrar na FID-00) | N2 |
| FID-05 | Uma chave QKD por *rekey* | Contar chaves `used` nos pools por hora vs. nº de *rekeys* | razão chaves/rekey | razão estável e documentada (1 ou 2) | N2 |
| FID-06 | Modelo analítico do *pool* | Medir enchimento/esvaziamento do `kma_keys` e comparar com o modelo (≤ 10 chaves por varredura, intervalo `sync_interval_s` + `generation_delay_s`) | erro relativo da taxa | ≤ 10 % | N2 |
| FID-07 | OSPF conforme RFC 2328 | Adjacências em `Full`, DR/BDR ausentes (`ptp`), custos aplicados, convergência após falha (REDE-04) | estado e tempo vs. *dead interval* | tempo de convergência ≈ valor teórico dos temporizadores | N2 |
| FID-08 | Atraso e perda injetados são os pedidos | Aplicar filtro de link (atraso 5/20/50 ms; perda 1/5 %) e medir RTT/perda das sondas | erro entre pedido e medido | ≤ 5 % do pedido (acima do piso de ruído da FID-00) | N2 |
| FID-09 | Determinismo | Mesma topologia ⇒ mesmo plano e mesmos `.rsc` (diferença só em segredos) em 10 execuções | diffs | 0 diffs fora dos segredos | N0 |
| FID-10 | Comparação com o laboratório de origem | Se houver dados do laboratório MITACS físico: tempo de estabelecimento do túnel, RTT, tempo de rekey | diferença emulado × original | explicar toda diferença > faixa de ruído | N2 (opcional) |

**Observações.** FID-01 e FID-09 são automatizáveis hoje (N0). FID-03 exige captura do GNS3 (HAB-03) e análise com `tshark`.
O **atraso de propagação da fibra** (≈ 5 µs/km) não existe por padrão: FID-08 valida o mecanismo de injeção que o representa.

## 5. Segurança

**Pergunta:** o framework detecta e/ou impede os ataques previstos pela teoria? Cada linha tem a **expectativa da teoria**, o
**resultado esperado hoje** (hipótese) e o que fazer com o desvio.

> Escopo: ataques ao **canal clássico, ao KME e ao IPsec**. Espionagem no **canal quântico** (corte do QBER) não existe no modelo
> (§2); o SEG-14 define a extensão mínima para estudá-la.

| ID | Ameaça | Procedimento | Expectativa (teoria/norma) | Esperado hoje (hipótese) | Controles |
|---|---|---|---|---|---|
| SEG-01 | **Interceptação passiva** do tráfego entre sites | Capturar `ether2`; procurar o padrão das sondas e os endereços internos | só IKE/ESP; nenhum payload em claro | OK | positivo: enlace em `unprotected_links` mostra claro |
| SEG-02 | **Chave em claro na rede** | Capturar `ether1` (CHR↔KME) e KMA; procurar o `key` base64 entregue (de `enc_keys`) | nenhuma ocorrência (mTLS) | OK | positivo: chamar o KME em HTTP em um teste isolado |
| SEG-03 | **Queda do túnel não vaza claro** | Derrubar o peer IPsec e enviar tráfego NODE↔NODE | tráfego descartado, não encaminhado em claro | OK: a política `level=require` já descarta sem SA; o teste mostra se a regra de firewall `enforce_encryption` é redundante ou cobre um caso real (ex.: política removida) | repetir com `enforce_encryption: false` e com a política removida |
| SEG-04 | **KME falso** (CA diferente) | Subir um KME com certificado de outra CA no IP do KME | CHR recusa; `key qkd` reporta erro; túnel não sobe | OK (validação pela CA importada) | mesmo teste com certificado legítimo |
| SEG-05 | **KME com CN/SAN errado** ou **certificado expirado** | Reemitir o certificado do KME com SAN sem o IP / expirado | recusa do *handshake* | OK | positivo: certificado correto |
| SEG-06 | **SAE impostor** (certificado válido, CN ≠ SAE autorizado) | Com um certificado da mesma CA e CN `sae-intruso`, chamar `enc_keys`/`dec_keys` | KME **rejeita** (SAE não autorizado em `connected_saes`) | **Aceita** (o KME só verifica a CA) ⇒ desvio da norma | registrar como *finding* |
| SEG-07 | **Esgotamento de chaves (DoS)** | Cliente autenticado chama `enc_keys?number=128` em laço | limitação de taxa / cota por SAE; túnel legítimo continua | **Vulnerável**: o *pool* esvazia e o *rekey* falha | medir tempo até a queda do túnel |
| SEG-08 | **Reuso de chave no `dec_keys`** | Chamar `dec_keys` duas vezes para o mesmo `key_ID` | segunda entrega negada (chave de uso único) | **Entrega de novo** ⇒ desvio | — |
| SEG-09 | **Replay/forja na sincronização KMA** | Cliente com certificado válido envia `POST /kma/sync` com contador antigo ou hash errado | hash errado ⇒ rejeita; contador antigo ⇒ rejeita | hash errado: rejeita; **contador antigo com `key_ID` novo: aceita** (cria chave com material repetido) — *confirmar* | requer conhecer a *seed* para o hash válido |
| SEG-10 | **Seed divergente** entre KMEs | Alterar a *seed* de um lado | falha detectável; *pool* não enche | OK (`400 Hash mismatch`), mas **silencioso** no IPsec até faltar chave | verificar visibilidade nos logs |
| SEG-11 | **Rebaixamento (*downgrade*) do PPK** | Fazer o KME de um lado devolver chave diferente / indisponível e observar o IKE | IKE **não** estabelece (ou estabelece sem PPK só se política permitir) | **Desconhecido**: depende do RouterOS (`ppk=qkd`); *este teste define a política* | medir se há SA sem PPK |
| SEG-12 | **MITM no IKE** | Posicionar um nó entre os CHRs (ligação intermediária) e tentar modificar/derrubar IKE | autenticação falha; sem SA | OK com PSK/certificado corretos; PSK fraco é risco | variar PSK |
| SEG-13 | **Exposição do plano de gerenciamento** | Do NODE, tentar alcançar `192.168.122.0/24` e a API do agente de outros NODEs | sem acesso à gerência; API do agente restrita | gerência: sem rota (OK); **API do agente é acessível** pelos túneis e sem autenticação | — |
| SEG-14 | **Espionagem quântica (QBER)** | *Extensão necessária* (HAB-07): um canal modelado em que *Eve* eleva o QBER | com QBER acima do limiar do protocolo, o enlace **aborta** a geração; o pool seca; alarmes | **Não testável hoje** | exige modelo do canal |

**Itens de acompanhamento.** SEG-06 a SEG-09 provavelmente revelarão **desvios do KMS-sim** (vendorizado sem alterações). O plano
trata isso como resultado válido: documenta o desvio e decide entre (a) aceitar como limitação do simulador, (b) corrigir no
repositório do KMS-sim, (c) acrescentar uma camada de autorização no QNetLab.

## 6. Rede: estudo de problemas reais de QKDN

**Pergunta:** o laboratório permite reproduzir e medir os problemas operacionais de uma QKDN? Os enlaces **KMA** (KME↔KME) e os
enlaces de **dados** (CHR↔CHR) são links GNS3 separados, então é possível falhar um sem o outro — cenário central em QKDNs
(canal quântico caído com canal clássico ativo, e o contrário).

| ID | Problema real | Procedimento | Métricas | Pergunta que responde |
|---|---|---|---|---|
| REDE-01 | **Esgotamento de chaves** (consumo > geração) | Reduzir a taxa (`generation_delay_s`↑, `sync_interval_s`↑) ou encurtar `proposal_lifetime` até o *pool* esvaziar | nível do *pool* no tempo, instante da primeira falha de *rekey*, comportamento do túnel | quanto tempo o túnel sobrevive sem chaves novas? |
| REDE-02 | **Falha do KME** | Parar o contêiner do KME de um site por T = 30 s, 2 min, 10 min | tempo até a queda do túnel, perda de pacotes, tempo de recuperação | o plano de dados é afetado imediatamente ou só no próximo *rekey*? |
| REDE-03 | **Falha do enlace quântico/KMA com dados ativos** | `suspend` do link KME↔KME | esvaziamento do *pool*, tempo até impacto no IPsec | independência entre os dois planos |
| REDE-04 | **Falha do enlace de dados com KMA ativo** | `suspend` do link `ether2` | convergência OSPF, DPD, reestabelecimento do túnel, reaproveitamento de chaves | efeito sobre o consumo de chaves e o tráfego |
| REDE-05 | **Canal clássico degradado** (perda 1–10 %, atraso/jitter) | Filtros do GNS3 em `ether2` e nos enlaces KMA | tempo de entrega de chave, falhas de IKE, RTT p95/p99, perda | robustez de IKE e KMA a canal ruim |
| REDE-06 | **Sobrecarga de *rekey*** | `proposal_lifetime` = 10 s … 1 h × tráfego | picos de RTT/perda no instante do *rekey*, CPU do CHR, chaves/hora | custo da renovação frequente (compromisso segurança × desempenho) |
| REDE-07 | **Escala do anel** | Anéis de 4, 6, 8, 12 sites | tempo de convergência, nº de *rekeys*/min, carga dos KMEs | como o plano de chaves escala? |
| REDE-08 | **Alcance multi-salto** | NODE1 → NODE3 (não adjacentes) | caminho, quais saltos são cifrados, o que aparece em claro | efeito do modelo "um túnel por CHR": só há proteção salto a salto |
| REDE-09 | **Reordenação de falhas** | Falhas simultâneas (KME + enlace de dados) e sequenciais | tempo total de indisponibilidade | modos de falha compostos |
| REDE-10 | **Reprodutibilidade de um cenário de falha** | Repetir REDE-02 × 10 com a mesma semente/roteiro | variância do tempo de recuperação | o laboratório permite *comparar* estratégias de resiliência? |

**Entregável por teste:** gráfico (nível do *pool*, vazão/perda e estado do túnel no tempo) + tabela com *tempo até a falha*,
*duração da indisponibilidade* e *tempo de recuperação*.

## 7. Desempenho: quanto custa emular?

**Pergunta:** quais recursos e quanto tempo o laboratório consome, e até que escala é válido?

| ID | Experimento | Procedimento | Métricas | Observação |
|---|---|---|---|---|
| DES-01 | **Pegada de recursos** por tipo de nó | Medir RSS/CPU de cada CHR (processo Qemu), KME, NODE e GNS3 em repouso e sob carga | CPU, RAM, disco, `nofile` por nó | base do modelo de custo |
| DES-02 | **Escala** | Ring 4, 6, 8, 12 (e 24 com `mgmt_step: 5`) | RAM e CPU totais; tempo de `up`; ponto de saturação | ajustar RAM(N) = a + b·N e prever o limite do host |
| DES-03 | **Tempo por etapa** | Cronometrar `pki`, `images`, `deploy`, `bootstrap`, `upgrade`, `configure`, `verify` por N | s por etapa; fração dominada pelo boot e pela atualização do RouterOS | com e sem `.npk` em cache; com imagem do template já atualizada |
| DES-04 | **Custo de uma rodada de experimento** | `make run` com cargas de 60 s e 300 s | duração total, tamanho do `output.tar.gz`, nº de arquivos | |
| DES-05 | **Validade da medição (saturação)** | Registrar CPU do host/CHRs durante o `load` e variar a taxa de sondas | % CPU, *steal*, jitter de RTT em repouso | resultados com CPU > limiar são inválidos (§3.2) |
| DES-06 | **Sobrecarga do IPsec** | iperf3 e RTT com e sem túnel (`unprotected_links`); variar tamanho de pacote | vazão, RTT, CPU do CHR | **verificar a licença do CHR**: versões gratuitas limitam a taxa e distorcem a vazão |
| DES-07 | **Sobrecarga do QKD no plano de controle** | Comparar `ppk_mode: qkd` × `static` × `none` | tempo de estabelecimento do túnel, tempo de *rekey*, chaves/min | custo de "ir ao KME" a cada *rekey* |
| DES-08 | **Custo para o pesquisador** | Tempo humano e de máquina para repetir um experimento do zero; horas-núcleo por experimento | horas, núcleos-hora; equivalente em nuvem | argumento de custo × testbed físico |

**Entregáveis:** tabela "recursos por nó", curva RAM/CPU × N com o limite de escala recomendado para o hardware usado, e o
orçamento de tempo de `up`. A suíte `pytest` leva ≈ 2 min por causa de consultas DNS reais nos testes de atualização; isolá-los
com um marcador (`network`) é uma melhoria barata (§8).

## 8. Utilidade: integração com aplicações reais

**Pergunta:** um grupo de pesquisa consegue colocar **a sua aplicação** e **o seu KME** no laboratório e obter resultados?

| ID | Experimento | Procedimento | Critério | Observação |
|---|---|---|---|---|
| UTI-01 | **Aplicação do grupo (DFL)** | `containers.app_source_dir` com o cliente DFL; rodadas de treinamento entre NODEs vizinhos | aplicação executa pelos túneis; eventos `accuracy` coletados; tempo de integração (h) | evidência principal de utilidade |
| UTI-02 | **Aplicações padrão** | iperf3, HTTP/gRPC, SSH, vídeo por UDP entre NODEs | funcionam sem alterações; sem quedas no *rekey* | |
| UTI-03 | **MTU/fragmentação** | `ping -M do -s <tam>` e TCP com MSS entre NODEs pelo túnel | MTU efetivo documentado; sem *blackhole* | sobrecarga do ESP é causa clássica de falhas |
| UTI-04 | **Cliente ETSI 014 externo** | Um SAE de teste (fora do CHR) chamando o KME com mTLS | obtém chaves; mesmo `key_ID` no par | exige certificados para o NODE (HAB-06) |
| UTI-05 | **Troca do KME** | `containers.kme_source_dir` com outra implementação/versão do KME | túnel sobe sem alterar o QNetLab; o que mudou na configuração | mede a *portabilidade* (o contrato é a API ETSI 014) |
| UTI-06 | **Uso por script** | Varredura de parâmetros (ex.: `proposal_lifetime` × taxa de chaves) via `qnetlab.lab.Lab` + agregação | execução sem intervenção; resultados em formato único | integração com notebooks e CI |
| UTI-07 | **Reprodução por terceiros** | Alguém fora do projeto segue `docs/` e reproduz um experimento de referência | resultado dentro do IC 95 % da linha de base; tempo gasto; dúvidas registradas | teste de usabilidade da documentação |
| UTI-08 | **Extensibilidade** | Adicionar um parâmetro de configuração e uma métrica nova | tempo e linhas de código; testes verdes | mede o custo de evoluir |

## 9. Habilitadores: o que o framework precisa ganhar para executar o plano

Alguns testes **não são executáveis hoje** sem pequenas extensões. Prioridade: **A** = bloqueia vários testes, **B** = útil,
**C** = futuro.

| ID | Habilitador | Para que serve | Esboço | Prioridade |
|---|---|---|---|---|
| HAB-01 | Guardar `link_id` no `state.json` e **injeção de falhas** (`suspend`, atraso, perda, corrupção, BPF) | FID-08, REDE-02…09, SEG-03 | O GNS3 2.2 expõe `filters` e `suspend` nos links; `Gns3Client` ainda não os usa | **A** |
| HAB-02 | **Controle de contêineres** (parar/iniciar KME/NODE) | REDE-02, SEG-11 | `docker stop/start` via `DockerExec` ou API de nós do GNS3 | **A** |
| HAB-03 | **Captura de pacotes** por enlace (`start_capture`/`stop_capture`) + resumo com `tshark` | SEG-01/02, FID-03 | endpoints de captura do GNS3 | **A** |
| HAB-04 | **Cronometragem por etapa** e **amostrador de recursos do host** (`qnetlab bench`) | DES-01…05 | tempos no `Lab.up`; amostragem periódica de CPU/RAM por processo/contêiner | **A** |
| HAB-05 | **Harness de testes de laboratório** (marcador `lab`, opt-in) que executa os experimentos e grava JSON de evidência | todos os N1/N2 | pytest com `Lab` real; falhas não rodam sem GNS3 | **A** |
| HAB-06 | **Certificado de SAE para o NODE** (opcional) | UTI-04, SEG-06/07 | emitir `sae-nodeX` e instalar no NODE | B |
| HAB-07 | **Modelo de canal quântico** no KME (taxa de bits por distância, QBER, abortar acima do limiar) | SEG-14, REDE-01 | ou *wrapper*/extensão do KMS-sim; precisa de decisão de escopo | C |
| HAB-08 | Marcador `network` nos testes de DNS e **suíte de conformidade da API ETSI 014** | FID-01, DES-08 | `tests/` | B |
| HAB-09 | `unprotected_links` com **controle de grupo** documentado nos relatórios | SEG-01, DES-06 | já existe; só padronizar o uso | B |

Os caminhos propostos (apenas esboço, ainda inexistentes):

```text
tests/lab/                     testes N1/N2 (marcador "lab")
tests/lab/conftest.py          fixture Lab real, ambiente e evidências
tests/test_kme_conformance.py  FID-01 (N0)
src/qnetlab/faults.py          HAB-01/02 (links e contêineres)
src/qnetlab/capture.py         HAB-03
src/qnetlab/bench.py           HAB-04
```

## 10. Roteiro, registro de evidências e critérios globais

### 10.1 Fases

| Fase | Conteúdo | Saída |
|---|---|---|
| **0 — Base** (sem laboratório) | FID-01, FID-09, SEG-04/05 (certificados, em loopback), marcador `network`, HAB-08 | suíte N0 ampliada |
| **1 — Habilitadores** | HAB-01…05 | `make bench`, `make capture`, `make fault` |
| **2 — Linha de base** | FID-00, DES-03, N1 no ring4 e ring6 | tolerâncias e orçamento de tempo |
| **3 — Fidelidade e segurança** | FID-02…08, SEG-01…13 | tabela de aderência (fidelidade) e lista de *findings* |
| **4 — Rede** | REDE-01…10 | relatório de cenários de falha |
| **5 — Desempenho e escala** | DES-01, 02, 04…08 | modelo de custo e limite de escala |
| **6 — Utilidade** | UTI-01…08 | caso de uso DFL e relatório de reprodução |

### 10.2 Registro de evidência (um por teste)

```text
ID / data / responsável
Hipótese e critério (escritos ANTES)
Configuração: YAML (hash), commit do QNetLab, versões (GNS3, RouterOS, Docker), host (CPU, RAM, kernel), licença do CHR
Procedimento executado (comandos) e nº de repetições
Resultados: tabela, gráficos, arquivos (output.tar.gz, .pcap) + SHA-256
Controles positivo e negativo: resultado
Conclusão: confirmou / refutou / inconclusivo — desvios e decisão (aceitar, corrigir, documentar)
```

### 10.3 Critérios globais propostos

- **Fidelidade:** FID-01…FID-09 satisfeitos dentro das tolerâncias da FID-00; desvios documentados no §2 e na tabela de aderência.
- **Segurança:** SEG-01…05, 10 e 12 aprovados; SEG-06…09, 11 e 13 resultam em *finding* classificado (aceito/corrigido); SEG-14 declarado
  "não modelado" até a existência do HAB-07.
- **Rede:** ao menos REDE-01…05 reproduzíveis com variância reportada (REDE-10).
- **Desempenho:** modelo RAM/CPU × N publicado com o limite de escala validado experimentalmente.
- **Utilidade:** UTI-01 e UTI-07 concluídos (aplicação do grupo + reprodução por terceiro).

### 10.4 Ameaças à validade

| Ameaça | Mitigação |
|---|---|
| Simulador de KME ≠ equipamento QKD real | declarar o escopo (§2); não extrapolar taxa/segurança quântica |
| Virtualização distorce desempenho (CPU compartilhada, licença do CHR, *steal*) | DES-05, relatar o hardware, repetir em hosts distintos |
| Medição perturba o sistema (sondas, capturas) | taxas baixas na linha de base; comparar com/sem captura |
| Estado residual entre execuções | recriar o laboratório (`make up` recria) e registrar o `project_id` |
| Viés do experimentador (limiares escolhidos depois) | critérios no registro **antes** de rodar; pré-definir *outliers* |
| Dependência da versão do RouterOS | fixar `router.update_version`; repetir a linha de base ao atualizar |
| Dados de um único anel | repetir em ring4 e ring6 (e em outro tamanho para escala) |

## 11. Próximos passos sugeridos

1. Revisar este plano (limiares, prioridades) e escolher 3–5 testes "de ouro" para a primeira rodada: **FID-02, SEG-01, SEG-03, REDE-02, DES-03**.
2. Implementar os habilitadores **A** (HAB-01…05) — são pequenos e destravam a maior parte do plano.
3. Executar a Fase 2 (linha de base) e só então congelar os critérios numéricos.
