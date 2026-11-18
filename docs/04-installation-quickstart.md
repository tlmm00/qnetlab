# 04 — Instalação e guia rápido

## 1. Pré-requisitos

| Item | Requisito | Como verificar |
|---|---|---|
| SO do host | Linux com KVM (para o CHR no Qemu) | `ls /dev/kvm` |
| GNS3 Server | 2.2.x, API v2, porta 3080 | `curl -s http://127.0.0.1:3080/v2/version` |
| Template CHR | Qemu **MikroTik CHR** com RouterOS ≥ 7.22 (ou atualizável), nome padrão `MikroTik CHR` | GNS3 → *Templates*, ou `curl -s :3080/v2/templates` |
| Template NAT | O nó nativo `NAT` do GNS3 (já existe por padrão) | idem |
| Rede de gerenciamento | O host alcança `192.168.122.0/24` (`virbr0`) | `ping 192.168.122.1` (ou o IP do host em `virbr0`) |
| Docker | Daemon **do sistema**, o mesmo que o GNS3 usa | `docker context use default` ; `docker info` |
| Python | ≥ 3.10 | `python3 --version` |
| OpenSSL | ≥ 1.1.1 | `openssl version` |
| Internet no host | Só para a atualização *host-side* do RouterOS e para baixar imagens/pacotes | `curl -I https://download.mikrotik.com` |

> **Atenção (Docker Desktop):** se `docker context ls` mostrar `desktop-linux` ativo, o `docker build` vai para
> `~/.docker/desktop/docker.sock`, outro daemon que não é o do GNS3. Os KMEs/NODEs criados pelo GNS3 só enxergam imagens do
> daemon do sistema: use `docker context use default`.

> **Servidor GNS3 em contêiner:** se o `gns3server` roda dentro de um contêiner Docker (confira com
> `sudo head -1 /proc/$(pgrep -f gns3server | head -1)/cgroup`: `docker-<id>.scope`), ele precisa do **socket e do cliente**
> do Docker do host: `-v /var/run/docker.sock:/var/run/docker.sock -v /usr/bin/docker:/usr/bin/docker:ro`. Sem o cliente, tudo
> funciona (criar nós usa o socket) **menos parar nós Docker**: o GNS3 roda `docker exec` ao parar, falha e o controlador
> esconde o erro (o nó continua `started`). Isso afeta `qnetlab fault node … stop` e o teste HAB-02; veja
> [11](11-troubleshooting.md).

## 2. Instalação

```console
$ git clone <seu-repositório> qnetlab && cd qnetlab
$ python3 -m venv .venv && . .venv/bin/activate
$ pip install -e '.[dev]'        # = make install; instala PyYAML, Jinja2, requests + extras (plots, schema, dev)
$ qnetlab --help
```

Extras (`pyproject.toml`): `plots` (matplotlib, para os PNGs), `schema` (jsonschema), `dev` (pytest + os dois anteriores).
Sem `plots`, as métricas são consolidadas em `summary.json/csv` mas os gráficos não são gerados.

Se o pacote for instalado fora do checkout, aponte para a raiz com `export QNETLAB_HOME=/caminho/qnetlab` (o diretório deve
conter `templates/`).

### Variáveis de ambiente

| Variável | Uso | Padrão |
|---|---|---|
| `GNS3_USER`, `GNS3_PASSWORD` | Autenticação HTTP Basic no servidor GNS3 (`gns3.user/password` nos YAMLs) | vazio = sem autenticação |
| `QNETLAB_ADMIN_PASSWORD` | Senha do `admin` dos CHRs, definida no *bootstrap* | `qnetlab-lab` |
| `QNETLAB_HOME` | Raiz do repositório quando instalado fora do checkout | auto (pai de `src/`) |

> **Atenção:** o padrão `qnetlab-lab` serve apenas para laboratórios isolados. Defina `QNETLAB_ADMIN_PASSWORD` sempre que o
> GNS3 estiver acessível por outras pessoas.

## 3. Validação do ambiente (sem GNS3)

```console
$ make test                                       # suíte completa (~2 min; alguns testes consultam DNS)
$ make validate TOPO=topologies/ring4.yaml        # OK: 'qkd-ring4' (4 sites, 8 CHRs, 36 enlaces, 4 túneis)
$ make plan TOPO=topologies/ring4.yaml            # IPs, enlaces, túneis
$ make render TOPO=topologies/ring4.yaml          # gera PKI + .rsc + compose em build/qkd-ring4/ (revise os .rsc!)
```

`render` não exige GNS3 nem Docker: é a melhor forma de revisar o que será aplicado.

## 4. Subindo o laboratório (4 ou 6 sites)

```console
$ export QNETLAB_ADMIN_PASSWORD='uma-senha-de-lab'
$ make up TOPO=topologies/ring6.yaml      # ring4: omitir TOPO (padrão)
```

`make up` executa, em ordem: **pki → images → deploy (`--recreate`) → bootstrap → upgrade → configure → verify**. Duração
típica: dominada pelo boot dos CHRs (`router.boot_timeout`, 420 s) e pela atualização do RouterOS (se necessária).

Saída esperada ao final: um JSON de `verify` com `"_ok": true` e código de saída `0`. Código `3` = verificação falhou (veja
[11](11-troubleshooting.md)); `1` = erro do framework.

### Passo a passo manual (útil para depurar)

```console
$ make pki          # Root CA + certificados (idempotente)
$ make images       # docker build da imagem-base
$ make deploy       # cria projeto/nós/enlaces no GNS3 e inicia (recria se existir)
$ make bootstrap    # IP de mgmt, REST/FTP, senha — via console serial
$ make upgrade      # (opcional) só atualiza o RouterOS
$ make configure    # PKI no roteador + OSPF + IPsec/PPK + firewall + logging
$ make verify       # OSPF Full, peers/SA IPsec, QKD
```

Cada alvo aceita `TOPO=` e `WORK=` (diretório de artefatos, padrão `build`).

## 5. Rodando o primeiro experimento

```console
$ make run          # carga nos NODEs + coleta + gráficos + pacote
$ ls build/qkd-ring6/output/        # output.tar.gz
$ ls build/qkd-ring6/plots/         # rtt_time.png rtt_cdf.png loss.png ...
```

Detalhes e variações em [10 — Operação e telemetria](10-operations-telemetry.md).

## 6. Inspeção manual

```console
$ ssh admin@192.168.122.10                                  # MKT11 (se o SSH estiver habilitado) — ou use o console do GNS3
$ docker ps --format '{{.Names}}' | grep '^GNS3\.'          # contêineres do laboratório
$ docker exec -it GNS3.KME1.<project_id> sh                 # shell do KME1 (project_id em build/<lab>/state.json)
$ docker logs GNS3.KME1.<project_id> | tail
```

No RouterOS:

```routeros
/ip ipsec active-peers print
/ip ipsec installed-sa print
/ip ipsec key qkd print
/routing ospf neighbor print
/log print where topics~"ipsec"
```

## 7. Encerrando

```console
$ make destroy      # para e remove o projeto do GNS3 (build/ permanece)
$ make clean        # apaga build/ (NÃO remove o projeto do GNS3) — apaga também a PKI e state.json!
```

> **Atenção:** `make clean` remove `build/`, inclusive `state.json` (segredos e `project_id`). Rode `make destroy` antes se
> ainda houver projeto no GNS3, ou você precisará apagá-lo manualmente.

## 8. Docker Compose (sem GNS3)

`qnetlab render` também gera `build/<lab>/compose/docker-compose.yml` com os KMEs (e `kme_config.*.yaml`), útil para testar
o KME e a PKI sem o GNS3. Os CHRs não participam.
