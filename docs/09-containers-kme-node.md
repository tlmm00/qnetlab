# 09 — Contêineres: KME e NODE

KMEs e NODEs usam **uma única imagem-base** (`containers.base_image`, padrão `qnetlab/node:base`) e se diferenciam pela
variável `ROLE` lida pelo *entrypoint*. No GNS3 eles são nós Docker; sem GNS3 (`docker-compose`) só os KMEs sobem.

## 1. Imagens

```text
qnetlab/node:base                       Dockerfile em docker/node/ (python:3.12-slim + iproute2, ping, iperf3, tcpdump, curl)
 ├─ /opt/qnetlab/kme/main.py            KME do KMS-sim, vendorizado SEM alterações
 ├─ /opt/qnetlab/sae/src/main.py        agente do NODE (somente biblioteca padrão)
 └─ /usr/local/bin/entrypoint.sh
qnetlab/<lab>-kme<i>:latest             camada por KME: /certs (CA + cert + chave do KME), /etc/qnetlab/kme_config.yaml
qnetlab/<lab>-node:latest               (opcional) base + sua aplicação, se containers.app_source_dir estiver definido
```

- `qnetlab images` constrói a base e uma imagem por KME (e a imagem do NODE, se aplicável). Nós Docker do GNS3 **não têm
  *bind mounts***, por isso certificados e configuração entram na imagem.
- Sem `app_source_dir`, os NODEs usam a própria imagem-base (`ROLE=app`).
- O contexto de cada KME fica em `build/<lab>/docker/KMEi/`; a chave privada do KME vai para dentro da imagem
  ([07](07-security.md#onde-ficam-as-chaves)).

## 2. Entrypoint e variáveis de ambiente

`docker/node/entrypoint.sh` configura a rede e executa o papel.

| Variável | Papel | Descrição |
|---|---|---|
| `ROLE` | ambos (obrigatória) | `kme` ou `app`. Outro valor ⇒ sai com código 64. |
| `NODE_NAME` | ambos | Nome do nó (ex.: `KME1`, `NODE1`). |
| `IFACES` | ambos | `eth0=10.0.3.2/30,eth1=10.0.3.6/30`: IPs a configurar (`ip addr add`). Espera cada interface (até 60 s). |
| `ROUTES` | NODE | Rotas separadas por `;`: `10.0.3.28/30 via 10.0.3.1 dev eth0;…` (`ip route replace`). |
| `SKIP_NET` | ambos | `1` ⇒ não mexe na rede (docker-compose/testes). |
| `KME_ID` | kme (obrigatória) | Ex.: `kme-sim-1`. |
| `KME_CONFIG` | kme | Caminho do config (padrão `/etc/qnetlab/kme_config.yaml`). |
| `SAE_ID` | app | Identidade do NODE (= nome do nó). |
| `PEERS` | app | `nome=ip=protegido=sae,…`: NODEs vizinhos a sondar (ex.: `NODE4=10.0.3.30=1=,NODE2=10.0.3.10=1=`); `protegido` = `1` se o enlace tem túnel; o 4º campo (`sae`) fica vazio neste laboratório. |
| `PROBE_PORT` | app | Porta UDP do eco/sondas (padrão `9000`). |
| `METRICS_PORT` | app | Porta HTTP do agente (padrão `9100`). |
| `KEY_SIZE_BITS` | app | Tamanho de chave (reservado). |
| `DATA_DIR` | app | Onde gravar `metrics.jsonl` (padrão `/data`). |
| `CERT_DIR` | app | Certificados do cliente (padrão `/certs`). |

> O *entrypoint* executa `ip addr/route`, que exige privilégio de rede; os nós Docker do GNS3 rodam com privilégios suficientes (se usar o compose manualmente, use `SKIP_NET=1`, que é o que o compose gerado define).

## 3. KME (KMS-sim)

O `main.py` é cópia do KMS-sim (Flask, ETSI GS QKD 014 + sub-camadas KMA/KSA da ITU-T Y.3803); origem e SHA-256 em
`docker/node/kme/UPSTREAM.md`. **Documentação detalhada do código** (algoritmo de sincronização, API, banco, limitações):
[`docker/node/kme/README.md`](../docker/node/kme/README.md). Executa `python3 main.py --config kme_config.yaml` em `/opt/qnetlab/kme`.

### Configuração gerada (`kme_config.yaml`)

Gerada por `templates/docker/kme_config.yaml.j2` a partir do plano e da seção `kme:`. Exemplo para `kme-sim-1` (ring4;
*seeds* truncadas) na variante **compose** (`flat`), em que os vizinhos são alcançados pelo nome DNS do serviço; no GNS3 o
`base_url` é `https://172.16.100.x:8443` (IP do enlace KMA do vizinho):

```yaml
listen: "0.0.0.0:8443"
local_kme_id: "kme-sim-1"
use_mTLS: true
tls:
  server_cert_path: "/certs/kme-sim-1.crt.pem"
  server_key_path: "/certs/kme-sim-1.key.pem"
  client_ca_cert_path: "/certs/ca.crt.pem"
storage:
  type: "sqlite"
  params: { path: "/opt/kme/data/kme-sim-1.db" }
peer_sync:
  type: "http_mtls"
  sync_interval_s: 5
  generation_delay_s: 0.5
  sync_threshold: 100
  request_timeout_ms: 5000
  max_retries: 3
  peers:
    kme-sim-2: { base_url: "https://kme-sim-2:8443", seed: "99d0…a9e9", client_cert_path: …, client_key_path: …, server_ca_cert_path: … }
    kme-sim-4: { base_url: "https://kme-sim-4:8443", seed: "59e2…dc02", … }
kme_topology:
  kme-sim-1: { connected_saes: ["sae-chr11", "sae-chr12"] }
  kme-sim-2: { connected_saes: ["sae-chr21", "sae-chr22"] }
  # … um bloco por KME
logging: { level: "INFO", file_path: "/var/log/kme.log" }
```

| Ponto | Detalhe |
|---|---|
| Vizinhos de `peer_sync` | Os dois KMEs do anel KMA (anterior e próximo). No GNS3 o `base_url` é o IP do enlace KMA do vizinho (`172.16.100.x`, porta 8443). |
| `seed` | 256 bits **por par** de KMEs, idêntica nas duas pontas, gerada uma vez (`state.json`). A mesma *seed* ⇒ os dois KMEs derivam as mesmas chaves. |
| `kme_topology` | Todos os KMEs e seus SAEs (os CHRs); o KME deriva as rotas de chave pelos enlaces KMA. |
| Armazenamento | SQLite em `/opt/kme/data/<kme-id>.db`; tabela `kma_keys` (`peer_kme_id`, `status`, …). |
| Log | `/var/log/kme.log` (coletado em `logs/KMEi.log`). |
| Chaves | Só **256 bits**; pedidos com `size` ≤ 128 são lidos como **bytes**. Por isso `containers.key_size_bits` precisa ser 256 sem `kme_source_dir`. |
| Taxa de geração | `kme.generation_delay_s` e `kme.sync_interval_s` controlam a velocidade com que os *pools* enchem; `kme.sync_threshold` limita as chaves mantidas por vizinho. |

### Usando o seu checkout do KME

```yaml
containers:
  kme_source_dir: ~/Codes/KMS-sim/src   # pasta que contém main.py (copiada para /opt/qnetlab/kme)
```

Rode `qnetlab images` e `qnetlab deploy` depois de alterar. Se o seu KME mudar o esquema do `kme_config.yaml`, ajuste
`templates/docker/kme_config.yaml.j2` e `src/qnetlab/containers/kme_config.py`.

### Operação manual

```console
$ docker exec -it GNS3.KME1.<project_id> sh
# tail -f /var/log/kme.log
# python3 -c "import sqlite3;c=sqlite3.connect('/opt/kme/data/kme-sim-1.db');print(list(c.execute('SELECT peer_kme_id,status,COUNT(*) FROM kma_keys GROUP BY 1,2')))"
```

(O `<project_id>` está em `build/<lab>/state.json`.)

## 4. NODE e o agente de telemetria

O NODE tem **2 NICs** (uma por CHR do site) e rotas estáticas para os NODEs vizinhos. O agente embutido
(`docker/node/sae/src/main.py`) usa só a biblioteca padrão; a documentação do código está em
[`docker/node/sae/README.md`](../docker/node/sae/README.md).

### Funções

1. **Eco UDP** na `PROBE_PORT`: devolve o que recebe, para os pares medirem RTT.
2. **Sondas UDP** aos NODEs vizinhos (`PEERS`) durante a carga: seq + timestamp + *padding* (payload UDP de `probe_size_bytes − 4` bytes);
   RTT por resposta (timeout de 1 s por sonda).
3. **API de controle HTTP** na `METRICS_PORT` (acessada por `docker exec … curl localhost`, pois o NODE não tem mgmt).
4. **Log de eventos** em JSON-lines (`/data/metrics.jsonl`).

### API de controle

| Método e caminho | Corpo | Resposta |
|---|---|---|
| `GET /control/status` | — | `{"sae_id","phase","started","params","running"}` |
| `POST /control/start` | `{"duration":60,"interval_ms":100,"size":64,"key_interval_s":0}` | `{"status":"started","peers":[…]}` ou `{"status":"busy"}` |
| `POST /control/stop` | — | `{"status":"stopping"}` |
| `GET /metrics.jsonl` | — | todo o log (`application/x-ndjson`) |
| `POST /event` | JSON livre (`type` padrão `event`) | `{"status":"ok"}`; entra no `metrics.jsonl` |

### Esquema dos eventos (`metrics.jsonl`)

Todo evento recebe `t` (epoch, s) e `node`.

| `type` | Campos | Origem |
|---|---|---|
| `boot` | `sae_id`, `peers` | início do agente |
| `phase` | `phase`: `start` (com `params`) \| `end` | início/fim da carga |
| `probe` | `peer`, `protected`, `seq`, `rtt_ms` (`null` = perdida), `size` | uma sonda UDP |
| `key_request` | `peer`, `ok`, `error`, `latency_ms` | reservado (desligado: os SAEs são os CHRs) |
| `accuracy` | `round`, `value` | opcional, enviado pela sua aplicação (aprendizado distribuído) |
| *qualquer outro* | livre | `POST /event` |

Exemplo: `{"type":"probe","peer":"NODE2","protected":true,"seq":17,"rtt_ms":4.213,"size":64,"t":1731312000.5,"node":"NODE1"}`.

### Colocando a sua aplicação (ex.: cliente DFL)

```yaml
containers:
  app_source_dir: ~/Codes/dfl-client    # deve conter src/main.py
```

- O diretório é copiado para `/opt/qnetlab/sae/` e o *entrypoint* executa `python /opt/qnetlab/sae/src/main.py`.
- Receberá as variáveis do §2 (`SAE_ID`, `PEERS`, `METRICS_PORT`, …). Os vizinhos estão em `PEERS`; alcance-os pelos IPs
  dos `/30` do NODE (o tráfego entre sites passa pelos túneis).
- Para continuar usando `qnetlab load/collect`, a aplicação deve expor `GET /control/status`, `POST /control/start`,
  `GET /metrics.jsonl` na `METRICS_PORT` com a semântica acima; do contrário, rode a sua carga por conta própria e
  coloque os `*.jsonl` em `build/<lab>/metrics/` antes de `qnetlab collect --offline`.
- Instale dependências extras estendendo `docker/node/requirements.txt` (a imagem-base é reconstruída por `qnetlab images`).
- Eventos de aplicação: `POST http://localhost:9100/event` com `{"type":"accuracy","round":3,"value":0.91}`.

## 5. Acesso e depuração

| Tarefa | Comando |
|---|---|
| Listar contêineres do lab | `docker ps --format '{{.Names}}' \| grep '^GNS3\.'` |
| Shell | `docker exec -it GNS3.<nó>.<project_id> sh` |
| Logs do processo | `docker logs GNS3.<nó>.<project_id>` |
| Interfaces e rotas | `docker exec GNS3.NODE1.<id> ip -br addr` · `ip route` |
| Status do agente | `docker exec GNS3.NODE1.<id> curl -s localhost:9100/control/status` |
| Testar um túnel | `docker exec GNS3.NODE1.<id> ping -c3 10.0.3.30` (NODE do site vizinho) |
| Capturar | `docker exec GNS3.NODE1.<id> tcpdump -ni eth0` (ou capture o enlace no GNS3) |
| Vazão (iperf3) | `iperf3 -s` num NODE, `iperf3 -c <ip>` no outro |
