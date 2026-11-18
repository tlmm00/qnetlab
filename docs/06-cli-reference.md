# 06 — Referência da CLI e do Makefile

## 1. Sintaxe

```text
qnetlab <comando> -t <topologia> [-w <diretório>] [-v] [opções do comando]
```

> **Importante:** `-t`, `-w` e `-v` pertencem ao **subcomando** e vêm **depois** dele
> (`qnetlab plan -t topologies/ring6.yaml`, não `qnetlab -t … plan`). Um erro `invalid choice` costuma indicar a ordem errada.

### Opções comuns (todos os comandos)

| Opção | Obrigatória | Padrão | Descrição |
|---|---|---|---|
| `-t`, `--topology` | sim | — | Arquivo YAML/JSON da topologia. |
| `-w`, `--workdir` | não | `build` | Raiz dos artefatos; o laboratório usa `<workdir>/<name>/`. |
| `-v`, `--verbose` | não | 0 | `-v` = logs de depuração. |

### Códigos de saída

| Código | Significado |
|---|---|
| `0` | Sucesso. |
| `1` | Erro do framework (`QNetLabError`: configuração, GNS3, console, PKI, RouterOS, telemetria…). A mensagem vai ao *log*. |
| `2` | Uso incorreto da CLI (argumentos inválidos — vem do `argparse`). |
| `3` | `verify`/`up` concluíram, mas a verificação falhou (`"_ok": false`). |
| `130` | Interrompido por `Ctrl-C`. |

Exceções não previstas (bugs) **não** são engolidas: aparecem com *traceback*.

## 2. Comandos

| Comando | O que faz | Precisa de | Opções específicas |
|---|---|---|---|
| `validate` | Valida a topologia e o plano de endereçamento; imprime um resumo. | nada | — |
| `plan` | Mostra nós, IPs, enlaces e túneis. | nada | `--json` |
| `render` | Gera PKI, `.rsc` e `docker-compose.yml` **offline**. | OpenSSL | — |
| `pki` | Gera/atualiza a Root CA e os certificados. | OpenSSL | `--force` (reemite tudo, **inclui nova CA**) |
| `images` | `docker build` da imagem-base (e de imagens próprias, se configuradas). | Docker | — |
| `deploy` | Cria projeto, nós e enlaces no GNS3 e inicia. | GNS3 | `--recreate` (apaga e recria projeto existente) |
| `bootstrap` | IP de gerenciamento, `www`/`ftp` e senha dos CHRs via console serial. | GNS3 + projeto | — |
| `upgrade` | Atualiza o RouterOS dos CHRs se abaixo de `router.min_version`/`update_version`. Imprime `<CHR>: RouterOS <versão>`. | mgmt; Internet no host (ou no CHR) | — |
| `configure` | Executa `pki`, depois `upgrade` e aplica PKI + OSPF + IPsec + firewall + logging. | mgmt | — |
| `verify` | Verifica OSPF, IPsec e QKD; imprime JSON. Exit `3` se falhar. | mgmt + Docker | `--timeout S` (padrão 240) |
| `up` | `pki → images → deploy → bootstrap → upgrade → configure → verify`, cronometrando cada etapa (`bench/up-<UTC>.json`). | tudo | `--recreate`, `--skip-images` |
| `load` | Executa a carga nos agentes dos NODEs. | Docker + projeto | — |
| `collect` | Coleta métricas/logs, gera gráficos e `output.tar.gz`. | Docker + mgmt | `--offline` (só consolida `metrics/` e `logs/`) |
| `run` | `load` + `collect`. | idem | — |
| `fault` | Injeta falhas em enlaces e nós do laboratório no ar (§2.1). | GNS3 + projeto | subcomandos `list`, `link`, `down`, `up`, `clear`, `node` |
| `capture` | Captura de pacotes nos enlaces do GNS3 e resumo de `.pcap` (§2.1). | GNS3 + projeto (exceto `summary`) | subcomandos `list`, `run`, `summary` |
| `bench` | Custo do emulador: tempo por etapa e consumo de CPU/RAM (§2.1). | projeto; host local para os recursos | subcomandos `sample`, `up` |
| `destroy` | Para e remove o projeto do GNS3. | GNS3 | — |

### 2.1 Experimentos: `fault`, `capture`, `bench`

Servem aos testes do [plano de validação](14-validation-test-plan.md). Exigem o laboratório no ar (`state.json` com os IDs
de nós **e de enlaces**; laboratórios implantados antes desta versão têm os IDs reconstruídos a partir do GNS3 na primeira
chamada). Um enlace é indicado por `NÓ[:iface]` e, se preciso, pela outra ponta e/ou `--kind`
(`kme-access`, `ring`, `sibling`, `node`, `kma`, `nat`). Se a seleção casar com mais de um enlace, o comando lista os
candidatos e não faz nada.

```console
$ qnetlab fault list    -t topologies/ring4.yaml                         # filtros, suspensão e captura de cada enlace
$ qnetlab fault link    MKT12 MKT21 --delay 30 --jitter 3 --loss 2 -t …  # atraso ± jitter (ms), perda (%) ...
$ qnetlab fault down    KME1 KME2 -t …                                   # "corta o cabo" (suspend); `up` religa
$ qnetlab fault node    KME1 stop -t …                                   # stop | start | restart | pause | resume
$ qnetlab fault clear   --all -t …                                       # remove filtros e religa tudo
$ qnetlab capture run   MKT12 MKT21 --duration 30 --label seg01 -t …     # captura 30 s, grava o .pcap e resume
$ qnetlab capture summary build/qkd-ring4/captures/<arquivo>.pcap --needle 10.0.3.1
$ qnetlab bench sample  --duration 60 --label idle -t …                  # CPU/RAM do host, CHRs e contêineres
$ qnetlab bench up      --recreate --label ring4 -t …                    # `up` com tempos por etapa e amostragem
```

| Comando | Comportamento |
|---|---|
| `fault link` | Aplica `--delay MS [--jitter MS]`, `--loss PCT`, `--corrupt PCT`, `--drop-every N`, `--bpf EXPR` (repetível; **descarta** o que casar). **Substitui** todos os filtros do enlace. Exige ao menos um filtro. |
| `fault down` / `up` | Suspende / religa o enlace (`suspend` do GNS3). |
| `fault clear` | Sem argumentos ou com `--all`: restaura **todos** os enlaces alterados; com `A [B]`: só aquele. |
| `fault node` | `stop`, `start`, `restart` (`reload`), `pause` (`suspend`), `resume`. Espera o estado final (`--no-wait` para não esperar). Vale para CHR, KME e NODE. |
| `capture list` | Enlaces com captura ativa no GNS3. |
| `capture run` | Captura por `--duration` s e grava em `build/<lab>/captures/`. O GNS3 2.2 só serve o `.pcap` **durante** a captura (depois, 409) e pode guardá-lo em disco de outro usuário (`root`), então o `qnetlab` baixa pela API em uma *thread* enquanto captura. Para capturar durante outra ação, rode em segundo plano (`… capture run … &`) ou use `PacketCapture.capturing()` em Python. Imprime o resumo (pacotes por protocolo, `esp_packets`, `ike_packets`, `cleartext_data_packets`). |
| `capture summary` | Resume um `.pcap` clássico (offline, sem `-t`); `--needle` conta pacotes que contêm o texto. |
| `bench sample` | Amostra o host a cada `--interval` s por `--duration` s e grava `bench/<label>-<UTC>.json` (resumo, amostras, ambiente, regra de saturação). |
| `bench up` | Executa o `up` sob amostragem; o relatório inclui `stages`. Exit `3` se o `verify` falhar. |

Toda ação de `fault` é registrada em `build/<lab>/logs/fault-events.jsonl` (`ts` em segundos Unix) e enviada como evento
`fault` ao `metrics.jsonl` de cada NODE acessível, para correlacionar a falha com RTT, perda e chaves.

Os alvos `make bench`, `make capture` e `make fault` previstos no plano **não existem** no Makefile (que é mantido à parte).
Sugestão, se quiser adicioná-los:

```make
bench:          ## amostra recursos por 60 s
	$(Q) bench sample $(A) --duration 60 --label idle

fault-clear:    ## restaura enlaces alterados
	$(Q) fault clear $(A) --all
```

### Detalhes e comportamentos

- **`up` x `make up`:** o alvo do Makefile passa `--recreate`; a CLI pura **não** recria por padrão (se o projeto existe,
  `deploy`/`up` sem `--recreate` falha com mensagem clara).
- **`configure` chama `upgrade` antes** de tocar nos roteadores: nunca aplica `.rsc` num RouterOS antigo. O `up` executa o
  `upgrade` como etapa própria (para cronometrá-lo) e chama `configure` sem repeti-lo.
- **`bootstrap`** é idempotente (reexecutável): remove DHCP client, reescreve o IP de gerenciamento e reaplica a senha.
- **`pki`** só reemite certificados cujo CN/SAN/tipo de chave mudou. `--force` gera **nova CA**: todos os certificados
  mudam e é preciso reaplicar `configure` e reconstruir as imagens (`images`).
- **`verify`** primeiro dispara tráfego entre os NODEs (3 pings por túnel, via `docker exec`) para forçar o IKE, depois
  consulta cada CHR a cada 5 s até `--timeout`. Um CHR está *pronto* quando tem ≥ 2 vizinhos OSPF em `Full` (anel +
  irmão) e, se há túnel protegido, ≥ 1 peer IPsec ativo **e** ≥ 1 SA instalada. O estado de `/ip ipsec key qkd` entra no
  relatório mas não condiciona o resultado. O JSON traz, por CHR, `ospf_full`, `ospf_expected`, `ipsec_active_peers`,
  `ipsec_installed_sa`, `ipsec_expected`, `qkd` (ou `error`), mais `_ok` e `_not_ready` (lista dos CHRs pendentes).
- **`collect`** nunca aborta por um nó inacessível: registra um *warning* (que vai ao `manifest.json` e ao JSON de saída) e segue.
- **`destroy`** não apaga `build/`.

## 3. Alvos do Makefile

Variáveis: `TOPO` (padrão `topologies/ring4.yaml`), `WORK` (padrão `build`). `make <alvo> TOPO=topologies/ring6.yaml`.

| Alvo | Equivale a | Observações |
|---|---|---|
| `install` | `pip install -e '.[dev]'` | |
| `test` | `python -m pytest -q` | sem GNS3/Docker |
| `validate` · `plan` · `render` · `pki` · `images` | `qnetlab <cmd> -t $(TOPO) -w $(WORK)` | |
| `up` | `qnetlab up … --recreate` | pipeline completo |
| `deploy` | `qnetlab deploy … --recreate` | |
| `bootstrap` · `upgrade` · `configure` · `verify` | idem | |
| `load` · `collect` · `run` | idem | |
| `destroy` | `qnetlab destroy …` | |
| `clean` | `rm -rf $(WORK)` | **não** remove o projeto do GNS3 |

## 4. Uso programático

A CLI é uma camada fina sobre `qnetlab.lab.Lab`:

```python
from qnetlab.lab import Lab

lab = Lab("topologies/ring4.yaml", "build")
lab.pki()                       # lista de certificados emitidos
report = lab.up(recreate=True)  # dict com o resultado do verify
assert report["_ok"]
lab.run_experiment()            # load + collect → {"package": ..., "summary": ..., "warnings": [...]}

faults = lab.faults()           # FaultInjector: enlaces (filtros, queda) e nós (parar/iniciar)
link = faults.links.one("MKT12", "MKT21")
faults.set_link(link, delay_ms=30, jitter_ms=3)
pcap = lab.capture().run(link, duration=30)         # .pcap em build/<lab>/captures/
faults.restore()                # remove filtros, religa enlaces e reinicia nós parados por este objeto
with lab.sampler() as s:        # CPU/RAM do host, dos CHRs (qemu) e dos contêineres
    ...
print(s.summary()["host.cpu_pct"])
lab.destroy()
```

Útil em notebooks e para varrer parâmetros (crie variantes da topologia com `dataclasses.replace` ou gere YAMLs).
