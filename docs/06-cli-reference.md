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
| `up` | `pki → images → deploy → bootstrap → configure → verify`. | tudo | `--recreate`, `--skip-images` |
| `load` | Executa a carga nos agentes dos NODEs. | Docker + projeto | — |
| `collect` | Coleta métricas/logs, gera gráficos e `output.tar.gz`. | Docker + mgmt | `--offline` (só consolida `metrics/` e `logs/`) |
| `run` | `load` + `collect`. | idem | — |
| `destroy` | Para e remove o projeto do GNS3. | GNS3 | — |

### Detalhes e comportamentos

- **`up` x `make up`:** o alvo do Makefile passa `--recreate`; a CLI pura **não** recria por padrão (se o projeto existe,
  `deploy`/`up` sem `--recreate` falha com mensagem clara).
- **`configure` chama `upgrade` antes** de tocar nos roteadores: nunca aplica `.rsc` num RouterOS antigo.
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
lab.destroy()
```

Útil em notebooks e para varrer parâmetros (crie variantes da topologia com `dataclasses.replace` ou gere YAMLs).
