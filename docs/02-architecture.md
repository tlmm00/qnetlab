# 02 — Arquitetura

## 1. Visão de contexto

```mermaid
flowchart TB
    user(["Pesquisador"])
    subgraph host["Host (Linux)"]
        cli["qnetlab (CLI / Lab)"]
        ws[("build/&lt;lab&gt;/<br/>pki · rsc · logs · metrics · state.json")]
        gns3["Servidor GNS3 :3080"]
        docker["Docker daemon"]
        virbr["virbr0 192.168.122.0/24"]
    end
    subgraph lab["Projeto GNS3"]
        chr["MikroTik CHR ×2N"]
        kme["KME ×N (contêiner)"]
        node["NODE ×N (contêiner)"]
        nat["NAT ×2N"]
    end
    user --> cli
    cli <--> ws
    cli -- "REST: projeto, nós, enlaces" --> gns3
    cli -- "docker build / exec / cp" --> docker
    cli -- "console telnet (bootstrap)" --> chr
    cli -- "REST :80 + FTP :21 (mgmt)" --> chr
    gns3 --- lab
    docker --- kme
    docker --- node
    nat --- virbr
    chr --- nat
```

Quatro canais de controle, cada um com um propósito:

| Canal | Alvo | Uso | Camada |
|---|---|---|---|
| HTTP REST (3080) | GNS3 | criar projeto/nós/enlaces, iniciar/parar | 1 |
| Console telnet serial | CHR | *bootstrap*: IP de mgmt, `www`, `ftp`, senha | 3 |
| REST `/rest` + FTP | CHR (via NAT/mgmt) | comandos, importar certificados e `.rsc`, atualizar RouterOS, coletar logs | 3, 4 |
| `docker exec` / `docker cp` | KME, NODE | ler métricas, logs, pools de chaves; disparar tráfego | 3, 4 |

KMEs e NODEs **não têm rede de gerenciamento**: toda interação passa por `docker exec` no contêiner
`GNS3.<nó>.<id-do-projeto>` (o ID está em `state.json`).

## 2. As quatro camadas

```mermaid
flowchart TB
    lab["lab.py (fachada)"]
    subgraph L1["Camada 1 · infra"]
        gc["gns3_client"] ~~~ dep["deployer"] ~~~ con["console"]
    end
    subgraph L2["Camada 2 · PKI e contêineres"]
        ca["pki/authority"] ~~~ img["containers/images"] ~~~ env["env · kme_config"] ~~~ dx["dockerexec"]
    end
    subgraph L3["Camada 3 · dataplane"]
        ros["routeros"] ~~~ pkg["package"] ~~~ ctx["context · renderer"] ~~~ dpc["configurator"]
    end
    subgraph L4["Camada 4 · telemetria"]
        lg["loadgen"] ~~~ col["collector"] ~~~ plt["plot_metrics"]
    end
    base["Base: config · addressing · workspace · rendering · errors"]
    lab --> L1 & L2 & L3 & L4
    L1 & L2 & L3 & L4 --> base
```

### Camada base

| Módulo | Responsabilidade |
|---|---|
| `config.loader` | Lê YAML/JSON, interpola `${VAR}`, rejeita chaves desconhecidas, constrói `Topology`. |
| `config.models` | Dataclasses **imutáveis** (`frozen=True`) e `Topology.validate()`. Única fonte de tipos e padrões. |
| `addressing` | Função pura `build_plan(topology) → AddressPlan` (sem E/S): IPs, enlaces, túneis, rotas dos NODEs. |
| `workspace` | Diretórios de saída e `state.json` (IDs do GNS3 e segredos, modo `0600`, escrita atômica). |
| `rendering` | Ambiente Jinja2 com `StrictUndefined`: variável ausente = erro, nunca texto vazio. |
| `errors` | Hierarquia `QNetLabError` (`ConfigError`, `AddressingError`, `Gns3Error`, `ConsoleError`, `PkiError`, `ImageBuildError`, `RouterOsError`, `TelemetryError`). |

### Camada 1 — infra (`qnetlab.infra`)

- `Gns3Client`: sessão `requests` com *retry* em GET/PUT/DELETE (502/503/504), mensagens de erro com status e corpo.
  Localiza templates (`MikroTik CHR` e o template nativo `NAT`).
- `TopologyDeployer`: cria o projeto, os nós (CHR por template, KME/NODE como `docker`, NAT pelo template nativo, renomeado
  `NAT1..NAT2N`), liga os enlaces por `adapter_number`/`port_number`, inicia tudo e grava `project_id`/`nodes` em
  `state.json`. `--recreate` apaga um projeto de mesmo nome antes.
- `TelnetConsole` / `RouterOsConsole`: login serial/telnet para o *bootstrap*, tolerante a prompts repetidos.

Mapeamento de interfaces → portas do GNS3:

| Nó | Nome da interface | `adapter_number` | `port_number` |
|---|---|---|---|
| CHR | `etherK` | K − 1 | 0 |
| Docker (KME/NODE) | `ethK` | K | 0 |
| NAT | `nat0` | 0 | 0 |

### Camada 2 — PKI e contêineres

- `CertificateAuthority` (OpenSSL via `subprocess`): Root CA, certificados por entidade, idempotente por *fingerprint*
  de (CN, SANs, tipo de chave). Detalhes em [07](07-security.md).
- `ImageBuilder`: monta o contexto Docker, `docker build` da imagem-base, gera `docker-compose.yml` para testes sem GNS3.
- `kme_config`: gera o `kme_config.yaml` de cada KME (vizinhos, *seeds* por par, SAEs conectados).
- `DockerExec`: `exec`, `http`/`http_json` (via `curl` dentro do contêiner) e `cp_from`.

### Camada 3 — dataplane (`qnetlab.dataplane`)

- `AddressPlan → context.py → RouterConfigRenderer`: o contexto Jinja de cada CHR é montado a partir do plano; os templates
  `templates/mikrotik/*.rsc.j2` produzem `build/<lab>/rsc/<CHR>.rsc` (e `.bootstrap.rsc`).
- `RouterOsClient`: cliente REST/FTP do RouterOS 7 (comandos, importação, versão, atualização, consultas de estado).
- `package.py`: descobre a versão mais recente do canal e baixa/cacheia o `.npk` para a atualização *host-side*.
- `DataPlaneConfigurator`: orquestra `bootstrap`, `upgrade`, `configure`, `verify` (CHRs em paralelo, até 8 por vez).

### Camada 4 — telemetria (`qnetlab.telemetry`)

- `LoadRunner`: comanda os agentes dos NODEs (aquecimento, início, espera, parada).
- `TelemetryCollector`: reúne métricas dos NODEs, logs/estado dos CHRs e KMEs, consolida (`plot_metrics`) e empacota
  `output.tar.gz` com manifesto SHA-256 e segredos mascarados.

## 3. Fluxo do pipeline `up`

```mermaid
sequenceDiagram
    autonumber
    actor U as Usuário
    participant L as Lab
    participant CA as PKI (OpenSSL)
    participant D as Docker
    participant G as GNS3
    participant R as CHR (console + REST)
    U->>L: qnetlab up --recreate
    L->>CA: issue_all + verify_all
    L->>D: docker build (imagem base)
    L->>G: criar projeto, nós, enlaces, start
    L->>R: bootstrap (console): IP mgmt, www, ftp, senha
    L->>R: upgrade (REST/FTP): checar versão, instalar .npk se necessário, reboot
    loop para cada CHR (em paralelo)
        L->>R: upload crt/key/.rsc (FTP)
        L->>R: /certificate import (REST) + renomear
        L->>R: /import .rsc (OSPF, IPsec, firewall, logging)
        L->>R: apagar arquivos enviados
    end
    L->>D: pings entre NODEs (dispara o IKE)
    L->>R: verify (OSPF Full, peers/SA IPsec, qkd)
    L-->>U: relatório JSON (exit 0 se _ok)
```

Cada etapa é também um subcomando independente ([06](06-cli-reference.md)); `up` apenas as encadeia.

## 4. Fluxo de chaves (como o QKD chega ao IPsec)

```mermaid
sequenceDiagram
    participant K1 as KME1
    participant K2 as KME2
    participant A as MKT12 (SAE sae-chr12)
    participant B as MKT21 (SAE sae-chr21)
    K1-->>K2: peer_sync (seed por par, enlace KMA, mTLS)
    Note over K1,K2: pools de chaves idênticas em ambos
    A->>K1: GET key (mTLS, CN=sae-chr12) para peer-sae-id=sae-chr21
    K1-->>A: key_ID + chave
    A->>B: IKEv2: IKE_AUTH com ID do PPK (key_ID)
    B->>K2: GET key por key_ID (mTLS, CN=sae-chr21)
    K2-->>B: mesma chave
    Note over A,B: SA derivada com PPK (RFC 8784), rekey a cada proposal_lifetime
```

O ponto central é o **objeto único `/ip ipsec key qkd`** de cada CHR (um `peer-sae-id`). Como cada CHR tem exatamente um
enlace `ether2`, há exatamente um túnel por CHR e não há ambiguidade.

## 5. Princípios de projeto

1. **Declarativo e determinístico.** Topologia → plano é função pura; mesmos insumos, mesmos IPs.
2. **Idempotente.** Cada `.rsc` remove o que criou antes; PKI só reemite o que mudou; `deploy --recreate` recria.
3. **Uma fonte de verdade.** Tipos/padrões nos dataclasses; schema e referência derivam deles/validados por teste.
4. **Falhar cedo, com mensagem objetiva.** Exceções tipadas; a CLI converte em mensagem + código de saída.
5. **Segredos fora do código e dos pacotes.** `state.json` 0600; `.rsc` mascarados no `output.tar.gz`; chaves privadas nunca
   deixam `build/<lab>/pki`.
6. **Testável sem infraestrutura.** E/S (GNS3, REST, Docker, console) é injetável; os testes rodam offline.
7. **Somente MikroTik.** Sem Dynamips/Cisco, sem switches: a conectividade é ponto a ponto.

## 6. Decisões de projeto

As decisões e seus motivos estão em [decisions.md](decisions.md).
