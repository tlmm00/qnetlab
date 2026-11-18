# 12 — Guia de desenvolvimento

## 1. Ambiente

```console
$ python3 -m venv .venv && . .venv/bin/activate
$ pip install -e '.[dev]'          # pytest, matplotlib, jsonschema
$ pip install flask                # necessário para o teste ponta a ponta local do KME (senão é ignorado)
$ python -m pytest -q
```

Requisitos: Python ≥ 3.10, OpenSSL. **Não** é preciso GNS3, Docker nem RouterOS para desenvolver: toda a E/S externa é
injetável e os testes usam dublês.

## 2. Mapa do código

```text
src/qnetlab/
├── cli.py            argparse → Lab; códigos de saída
├── lab.py            fachada: encadeia as camadas (pki, images, render, deploy, bootstrap, upgrade, configure, verify, load, collect…)
├── config/           models.py (dataclasses imutáveis + validate) · loader.py (YAML/JSON, ${VAR}, schema)
├── addressing.py     build_plan(): Iface/Link/ChrPlan/KmePlan/NodePlan/SitePlan/TunnelPlan/AddressPlan
├── workspace.py      build/<lab>/… e state.json
├── faults.py         injeção de falhas em enlaces e nós (GNS3) e registro dos eventos
├── capture.py        captura de pacotes nos enlaces e resumo de .pcap
├── bench.py          cronômetro de etapas, amostrador de recursos, regra de saturação
├── rendering.py      Jinja2 estrito
├── errors.py         QNetLabError e derivadas
├── infra/            gns3_client.py · deployer.py · console.py (telnet/RouterOS)
├── pki/authority.py  CertificateAuthority, Entity, entities_from_plan
├── containers/       images.py · env.py · kme_config.py · dockerexec.py
├── dataplane/        context.py · renderer.py · routeros.py · package.py · configurator.py
└── telemetry/        loadgen.py · collector.py · plot_metrics.py
```

Dependências permitidas (setas = "pode importar"): `cli → lab → {infra, pki, containers, dataplane, telemetry} → {addressing,
config, workspace, rendering, errors}`. As camadas **não** se importam entre si lateralmente, exceto `telemetry` e
`containers` → `dataplane.routeros`/`containers.dockerexec` para falar com os nós.

## 3. Convenções

| Tema | Regra |
|---|---|
| Modelos | `@dataclass(frozen=True)`; valores calculados em propriedades; validação em `Topology.validate()` |
| Entrada | Chaves desconhecidas = erro; interpolação `${VAR:-x}` só no loader |
| Erros | Levante a exceção **tipada** de `errors.py` com mensagem acionável (o que houve e o que fazer). Não use `except Exception` para esconder falhas |
| E/S | Injete dependências (`runner`, `get`, `client`) para permitir dublês; evite rede/subprocess em módulos puros (`addressing`, `config`) |
| Subprocess | Sempre lista de argumentos (sem `shell=True`), `timeout` e tratamento de `returncode` |
| Templates | Jinja2 `StrictUndefined`; nunca concatene scripts RouterOS em Python; valide valores que vão entre aspas (`context._safe`) |
| Segredos | Via `Workspace.secret()`; nunca em log, em mensagens de erro nem no `output.tar.gz` |
| Idempotência | Todo script/etapa deve poder ser reexecutado; limpe antes de criar |
| Logs | `logging.getLogger(__name__)`; `info` para marcos, `debug` para detalhes; sem `print` fora da CLI |
| Idioma | Mensagens ao usuário, docstrings e comentários em português; identificadores em inglês |
| Tipagem | `from __future__ import annotations` e anotações de tipo nas assinaturas públicas |

## 4. Testes

```console
$ python -m pytest -q                          # tudo (~2 min; alguns testes consultam DNS real)
$ python -m pytest -q tests/test_render.py     # um arquivo
$ python -m pytest -q -k ipsec                 # por palavra-chave
$ python -m pytest -q tests/test_docs.py       # documentação vs código
```

| Arquivo | O que cobre | Dublês |
|---|---|---|
| `test_config_addressing.py` | carga/validação de YAML, schema, pools, enlaces, túneis, router-id, rotas dos NODEs, ring4/ring6 | — |
| `test_pki.py` | CA, SANs, idempotência, reemissão, cadeia | OpenSSL real |
| `test_render.py` | `.rsc` gerados (linhas esperadas do MKT11 do ring6), ordem do bypass, lifetimes, kme_config, env dos contêineres | — |
| `test_deployer.py` | nós/adaptadores/portas/enlaces/NAT no GNS3, `--recreate`, falhas parciais | cliente GNS3 falso |
| `test_console.py` | login e execução no console RouterOS | servidor telnet falso |
| `test_configurator.py` | ordem de upload/importação, limpeza de arquivos, atualização (host/device), verificação por CHR | `FakeRos`, runner Docker falso |
| `test_telemetry.py` | agente do NODE, carga, coleta, consolidação, pacote e mascaramento | `docker exec` falso |
| `test_e2e_local.py` | **2 KMEs reais** (o `main.py` vendorizado) + mTLS + sync KMA + agente + gráficos, em loopback | processos locais (exige `flask`) |
| `test_faults.py` | índice de enlaces (por nó/iface/tipo; reconstrução a partir do GNS3), faixas dos filtros, queda/religação, ações de nó, `restore()`, marcadores nos NODEs, IDs de enlace no `state.json` | cliente GNS3 falso |
| `test_capture.py` | ciclo `start`/`stop` com *thread* de fluxo (arquivo completo só após o `stop`, erros de conexão, fluxo que não termina), resumo de `.pcap` sintético (ESP, IKE, VLAN, endianness, truncado, inválido), busca de bytes | cliente GNS3 falso, `.pcap` gerado no teste |
| `test_bench.py` | cronômetro, leitores de `/proc` e `docker stats`, taxas de CPU entre amostras, percentis, regra de saturação | `/proc` falso em `tmp_path` |
| `test_gns3_client_links.py` | `Gns3Client`: `PUT` de enlace (com repetição usando `nodes` se o GNS3 exigir), ações de nó e endpoints de captura | `requests` simulado |
| `test_lab_experiments.py` | `Lab.up` cronometrado, `Lab.links()`, CLI `fault`/`capture`/`bench` | cliente GNS3 falso |
| `tests/lab/` | **laboratório real** (opt-in por `QNETLAB_TOPOLOGY`): habilitadores e testes do plano, com evidência JSON | nenhum (GNS3 real) |
| `test_docs.py` | referência de configuração em dia, links/âncoras, comandos e alvos documentados, caminhos citados | — |

Não coberto pela suíte padrão: GNS3 real, `docker build`, entrypoint com `ip`, CHR real
([13](13-limitations-roadmap.md)); parte disso é exercitada pelos testes opt-in de `tests/lab` ([tests/lab/README.md](../tests/lab/README.md)). Ao mexer nessas partes, valide manualmente com `make up` e descreva o resultado no PR.

### Escrevendo testes

- Reutilize as *fixtures* de `tests/conftest.py` (`ring4_path`, `ring6_path`) e o `tmp_path` para o *workspace*.
- Para testes de laboratório (`tests/lab`): marque com `@pytest.mark.plan("ID")`, escreva hipótese e critério no docstring **antes** de rodar, use as *fixtures* `lab`, `faults`, `capture` e `evidence`; o `faults` restaura enlaces e nós no *teardown*.
- Para roteadores, estenda o `FakeRos` de `test_configurator.py` em vez de criar mocks ad hoc.
- Evite testes que dependam do relógio ou da rede; injete `get=`/`runner=`.
- Um teste de regressão por bug: o nome deve dizer o que quebrou (ex.: `test_ipsec_commands_match_the_lab_syntax`).

## 5. Estendendo o framework

### 5.1 Adicionar um parâmetro de configuração

1. **Modelo:** adicione o campo (com tipo e padrão) ao dataclass certo em `config/models.py`; valide em `Topology.validate()`.
2. **Schema:** acrescente a propriedade em `schema/topology.schema.json` (`additionalProperties: false` rejeitaria o campo novo).
3. **Uso:** leia o campo no `context.py`/`renderer.py`/camada interessada; se afetar scripts, adicione-o ao template.
4. **Descrição:** inclua o campo em `DESCRIPTIONS` de `scripts/gen_config_reference.py` e regenere:
   `python scripts/gen_config_reference.py` (o teste falha se esquecer).
5. **YAMLs:** atualize `topologies/*.yaml` se o padrão for relevante para os cenários.
6. **Testes:** valor padrão, valor inválido, efeito no artefato gerado.
7. **Docs:** mencione em [08](08-routeros-config.md)/[10](10-operations-telemetry.md) se mudar o comportamento.

### 5.2 Mudar a configuração dos roteadores

Edite `templates/mikrotik/NN_*.rsc.j2` (e o contexto em `dataplane/context.py`), rode
`qnetlab render -t topologies/ring4.yaml`, **leia o `.rsc`**, aplique em um CHR de teste e só então rode a suíte.
Lembre-se das regras de [08 §9](08-routeros-config.md#9-lições-de-sintaxe-do-routeros-722-por-que-o-gerador-é-como-é)
(nada de `comment=` em profile/proposal/peer/identity; limpeza por nome).

### 5.3 Novo cenário de topologia

Copie `topologies/ring4.yaml`, ajuste `name`, `ring.size` e o que quiser variar. Se o tamanho exceder o limite de
gerenciamento, reduza `addressing.mgmt_step`. Rode `qnetlab validate` e `qnetlab plan`.
Formas diferentes de anel (malhas, múltiplos CHRs por site) exigem estender `addressing.build_plan()` e
`config.models` — comece pelos testes de `test_config_addressing.py`.

### 5.4 Nova métrica

1. Emita um evento JSON-lines no agente (`docker/node/sae/src/main.py`: `emit({...})`) ou via `POST /event`.
2. Trate o novo `type` em `telemetry/plot_metrics.py` (`summarize`, `make_plots`).
3. Acrescente testes em `test_telemetry.py` com um `*.jsonl` de exemplo.
4. Documente o esquema em [09 §4](09-containers-kme-node.md) e [10 §4](10-operations-telemetry.md).

### 5.5 Novo comando de CLI

Adicione o método em `Lab`, o subparser em `cli.build_parser()` e o ramo em `main()`; adicione o alvo no `Makefile`
(`.PHONY`) — `test_docs.py` exigirá a documentação em [06](06-cli-reference.md).

### 5.6 Trocar/atualizar o KME

Veja [09 §3](09-containers-kme-node.md). Para re-vendorizar: copie o novo `main.py` para `docker/node/kme/`, atualize o SHA-256
em `UPSTREAM.md` e confira o esquema do `kme_config.yaml` (`templates/docker/kme_config.yaml.j2`,
`test_kme_config_matches_user_schema`).

## 6. Documentação

- Escrita em Markdown, em `docs/`, versionada com o código; diagramas em **Mermaid** (renderizam no GitHub/GitLab).
- Uma página = um propósito; comece pelo que o leitor quer fazer; links relativos entre páginas.
- `05-configuration-reference.md` é **gerado**; não edite à mão.
- `tests/test_docs.py` falha se: referência desatualizada, link/âncora quebrado, comando da CLI ou alvo do Makefile sem
  documentação, ou caminho do repositório citado que não existe.
- Exemplos de comando/saída devem vir de execuções reais (`qnetlab plan`, `qnetlab render`).
- Valide diagramas localmente: `mmdc -i arquivo.mmd -o /tmp/x.svg` (opcional).
- Atualize a tabela de [decisions.md](decisions.md) ao tomar uma decisão arquitetural (próximo ADR numerado).

## 7. Fluxo de contribuição sugerido

1. Crie um *branch* a partir do principal; mudanças pequenas e focadas.
2. Escreva/ajuste testes primeiro (ou junto).
3. `python -m pytest -q` verde; se tocou em RouterOS/GNS3/Docker, valide manualmente e descreva.
4. Atualize docs (e regenere a referência).
5. Mensagem de commit no imperativo, com o *porquê*; um assunto por commit.
6. Revise o diff procurando segredos (`secret=`, `.pem`, `state.json`) antes de publicar.

## 8. Releases

Versão em `pyproject.toml` (`version`) e `src/qnetlab/__init__.py`. Use *tags* `vMAJOR.MINOR.PATCH`; registre mudanças
quebrando compatibilidade de YAML (chaves renomeadas/removidas) e de artefatos em `build/` (formato de `state.json`).
