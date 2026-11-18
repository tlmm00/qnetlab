# 11 — Solução de problemas

## Como diagnosticar

1. Rode o comando com `-v` (logs de depuração): `qnetlab configure -t … -v`.
2. Identifique a **etapa** que falhou (`pki`, `deploy`, `bootstrap`, `upgrade`, `configure`, `verify`, `load`, `collect`) e
   rode apenas ela — todas são reexecutáveis.
3. Confira os artefatos em `build/<lab>/` (`rsc/`, `logs/`, `state.json`).
4. Consulte a tabela de sintomas abaixo; as seções seguintes detalham os casos mais comuns.

| Código de saída | Quer dizer |
|---|---|
| `1` | Erro do framework — leia a mensagem (a causa costuma estar nela) |
| `2` | Argumentos inválidos da CLI |
| `3` | `verify`/`up` terminaram, mas há CHRs `_not_ready` |

## Sintomas → causa → solução

| Sintoma | Causa provável | Solução |
|---|---|---|
| `argument cmd: invalid choice: 'TOPO=…'` ou `-t` não reconhecido | `-t/-w` antes do subcomando | `qnetlab plan -t …` (subcomando primeiro). No Makefile, use `make up TOPO=…`. |
| `Chave(s) desconhecida(s) em 'ipsec': […]` | Typo no YAML | Corrija; a mensagem lista as chaves permitidas. |
| `Variável de ambiente obrigatória não definida: X` | `${X}` sem padrão no YAML | `export X=…` ou use `${X:-padrão}`. |
| `Template 'MikroTik CHR' não existe no GNS3. Disponíveis: […]` | Nome do template difere | Ajuste `router.gns3_template` para um dos nomes listados. |
| `Projeto '…' já existe no GNS3` | `deploy` sem `--recreate` | `make deploy` (já recria) ou `qnetlab deploy … --recreate`; ou `qnetlab destroy`. |
| `HTTP 401` do GNS3 | Servidor exige autenticação | `export GNS3_USER GNS3_PASSWORD`. |
| `HTTP 409 "Could not find Dynamips"` | O projeto tentou criar roteador/switch Dynamips | Já corrigido: o framework só cria CHR (Qemu), Docker e NAT. Atualize o código. |
| `docker build` falha com socket `~/.docker/desktop/docker.sock` | Contexto do Docker Desktop ativo | Ver seção abaixo. |
| `Projeto GNS3 não implantado (state.json sem project_id)` | `load`/`collect` antes do `deploy`, ou `make clean` | `make deploy` / `make up`. |
| `Estado sem nós: rode 'deploy' antes do bootstrap` | `state.json` sem `nodes` | `make deploy`. |
| `Agentes inacessíveis em […]` | Contêineres parados ou agente não iniciou | `docker ps`, `docker logs GNS3.NODEi.<id>`. |
| `Falha de comunicação com o GNS3 em …` | GNS3 parado ou `gns3.url` errada | `curl :3080/v2/version`; ajuste `gns3.url`. |
| `Tempo esgotado aguardando o login do RouterOS` / `Credenciais rejeitadas pelo RouterOS` | CHR ainda iniciando; ou senha do admin já alterada e diferente de `QNETLAB_ADMIN_PASSWORD` | Aumente `router.boot_timeout`; use a mesma `QNETLAB_ADMIN_PASSWORD` das execuções anteriores (o bootstrap tenta senha vazia e a configurada). |
| `Nós não iniciaram a tempo: […]` | Poucos recursos do host / imagem lenta | Aumente `gns3.start_timeout`; verifique CPU/RAM/KVM. |
| `Não foi possível abrir o console host:porta` | Console telnet do nó inacessível | `gns3.url` aponta para outro host? O GNS3 expõe os consoles em outro IP? |
| `RouterOS … < 7.22` e `auto_update: false` | Versão antiga sem atualização automática | `auto_update: true` ou atualize o template. |
| `O canal 'stable' oferece X, menor que a versão exigida` | Canal atrás da versão pedida | `update_channel: testing` ou defina `update_version`. |
| `RouterOS esperado exatamente 7.22.1, mas ficou assim: {…}` | Instalação não aplicou o pacote | Veja reinício do CHR; arquitetura/licença; repita `make upgrade`. |
| `Não consegui consultar a versão do canal` | Host sem Internet / DNS | Defina `router.update_version` (pula a consulta) e garanta acesso a `download.mikrotik.com`. |
| `arquivo pequeno demais para um pacote RouterOS` | Versão/arquitetura inexistente em `download.mikrotik.com` | Confira `update_version` (ex.: `7.22.1`). |
| `PkiError: OpenSSL não encontrado` | `openssl` ausente do PATH | Instale o OpenSSL. |
| `SAN ausente(s) no certificado` | Certificado antigo com IPs diferentes | `qnetlab pki` reemite o que mudou (ou `--force`). |
| `verify` ⇒ `_ok: false` | Ver seção "verify falha" | |
| `O GNS3 não conhece o projeto … do state.json` / projeto `closed` | `state.json` desatualizado (projeto apagado, GNS3 reiniciado ou trocado) | `make up` (ou `deploy --recreate`); confira com `curl -s localhost:3080/v2/projects`. |
| `O fluxo do .pcap não conectou` / `não encerrou o fluxo da captura` | O enlace está suspenso ou sem nós iniciados; ou o GNS3 não fechou o fluxo após o `stop` | Confira `qnetlab fault list` (enlace suspenso?) e os nós `started`; repita. O arquivo parcial fica em `captures/`. |
| `fault node … stop` (ou o teste do KME1): `Nó KME1 não chegou ao estado 'stopped'` + `Could not fix permissions … No such file or directory: 'docker'` | O GNS3 2.2 só para um nó Docker depois de rodar `docker exec` (corrige permissões dos volumes). Se o servidor GNS3 **roda em contêiner** (`sudo head -1 /proc/$(pgrep -f gns3server \| head -1)/cgroup` mostra `docker-<id>.scope`) ou não tem `docker` no PATH, o comando falha, o `stop` é abortado e o controlador responde 204 do mesmo jeito (o contêiner continua `Up`) | Contêiner: recrie o servidor com o cliente montado, `-v /usr/bin/docker:/usr/bin/docker:ro` (além de `-v /var/run/docker.sock:/var/run/docker.sock`), preservando os volumes dos projetos. No host: ponha `docker` no PATH do servidor. O QNetLab repete a ação direto no compute e mostra esta causa no erro. Não pare o contêiner por fora do GNS3: isso deixa a ligação de rede do nó inconsistente. |
| `fluxo sem assinatura de pcap (N bytes recebidos); início: …` | O GNS3 devolveu algo que não é pcap (erro interno, enquadramento inesperado) | O corpo cru fica em `captures/*.pcap.raw`; abra-o ou anexe-o ao reportar. O GNS3 2.2.56 devolve o corpo sem enquadramento *chunked* válido — o QNetLab lê o socket cru e extrai o pcap. |
| `collect`: muitos "coleta falhou (… No route to host)" | Rede de gerenciamento inacessível | Ver seção abaixo. |

---

## `docker build` aponta para o socket do Docker Desktop

**Sintoma:** erro de build mencionando `unix:///home/<user>/.docker/desktop/docker.sock`.

**Causa:** `docker context` ativo é `desktop-linux`. O GNS3 cria os contêineres no **daemon do sistema**, que não enxerga as
imagens construídas no Docker Desktop.

**Solução:**

```console
$ docker context ls
$ docker context use default
$ docker info | grep -i 'server version'
$ make images
```

---

## "could not resolve dns name (timeout)" na atualização

**Sintoma:** com `update_method: device`, o CHR (7.16) fica 900 s em `/system package update` e falha com
`could not resolve dns name (timeout)`, mesmo com `ping 1.1.1.1` funcionando.

**Causa:** DNS do guest atrás do NAT do GNS3 (`virbr0`) não resolve de forma confiável.

**Solução:** use o padrão **`update_method: host`**: o host baixa o `.npk` e o envia por FTP; não há DNS no CHR.

```yaml
router:
  min_version: "7.22.1"
  update_method: host
  update_version: "7.22.1"
```

Requisito: o host alcançar `download.mikrotik.com` (e `upgrade.mikrotik.com` se `update_version` for vazio).

---

## `No route to host` nos CHRs

**Sintoma:** `configure`/`verify`/`collect` falham com `Errno 113 No route to host` para `192.168.122.x`; `collect` termina com
resumo vazio e *warnings* para todos os CHRs.

**Significado:** o **host** não consegue alcançar a rede de gerenciamento (nós NAT → `virbr0`). Não é um problema de
configuração do RouterOS — o REST nem chega ao roteador (ARP sem resposta). Isso pode ocorrer depois de funcionar (por
exemplo, após reinício dos nós NAT/GNS3, mudança no firewall do host ou queda da bridge).

**Roteiro de diagnóstico (no host):**

```console
$ ip -br addr show virbr0                       # a bridge existe e tem 192.168.122.1/24?
$ ip route get 192.168.122.10                   # sai por virbr0?
$ ping -c2 192.168.122.10
$ ip neigh show dev virbr0                      # entradas FAILED/INCOMPLETE = ARP sem resposta
$ sudo virsh net-list --all                     # rede 'default' ativa?  (se inativa: sudo virsh net-start default)
$ sudo iptables -S FORWARD | head ; sudo nft list ruleset | grep -i virbr0   # FORWARD/INPUT bloqueando a bridge?
```

**Causas comuns e correções:**

| Causa | Verificação | Correção |
|---|---|---|
| Rede libvirt `default` parada | `virsh net-list --all` | `sudo virsh net-start default` |
| Nós NAT do GNS3 sem a ligação ao CHR / CHR sem o IP de mgmt | console do CHR: `/ip address print` | `make bootstrap` (idempotente) |
| Firewall do host (ufw/nft/Docker) bloqueando `virbr0` | regras acima | liberar `virbr0` em INPUT/FORWARD |
| CHR reiniciado/atualizado sem o IP | `/ip address print` no console | `make bootstrap` |
| Sub-rede divergente | `gns3`/`addressing.mgmt_subnet` ≠ rede real do NAT | ajuste `addressing.mgmt_*` |
| Mais de um GNS3/projeto usando os mesmos IPs `.10/.20…` | `ip neigh` com MAC duplicado | feche o outro projeto |

Depois de restabelecer o acesso: `make verify` e então `make run`. Os dados já coletados dos NODEs/KMEs (via `docker exec`)
**não** dependem da rede de gerenciamento; só os dados dos CHRs dependem.

---

## `verify` falha (`_ok: false`)

O relatório lista, por CHR, `ospf_full`, `ipsec_active_peers`, `ipsec_installed_sa`, `qkd` e os `_not_ready`.

### OSPF < 2 vizinhos `Full`

- Cada CHR deve ter 2: o do `ether2` (outro site) e o do `ether3` (irmão). Veja `routing ospf neighbor print`.
- Verifique IPs e `/30` (`qnetlab plan`), estado das interfaces (`/interface print`) e se o GNS3 realmente ligou os cabos
  (`ether2`↔`ether2` do vizinho).
- `router-id` duplicado ou `area-id` diferente entre vizinhos.

### Sem peers IPsec / SA

1. `make verify` dispara 3 pings por túnel *a partir do NODE*; sem tráfego `NODE↔NODE` o IKE não inicia (pings do próprio
   roteador caem no *bypass*). Teste manual: `docker exec GNS3.NODE1.<id> ping -c3 <ip do NODE vizinho>`.
2. `/log print where topics~"ipsec"` (ou `logs/MKT*-ipsec.log`): procure por `no proposal chosen`, `authentication failed`,
   `timeout`.
3. PSK divergente entre as pontas: reaplique `make configure` (os dois lados leem o mesmo `state.json`).
4. Rota para o par: `ping` entre os IPs de `ether2` dos dois CHRs.
5. Firewall: devem existir as regras `qnetlab-ike-*` e `qnetlab-esp-*`.
6. `key qkd` com `total-keys-received: 0` em todos os CHRs: o KME não entrega chaves. Confira se os contêineres KME estão
   rodando (`docker ps -a --filter name=GNS3.KME`; ausentes do `docker stats` = parados) e leia `docker logs GNS3.KME1.<id>`.
   O `tests/lab` (baseline e amostrador) aponta esse caso.

### Túnel de pé, mas `key qkd` com erro

- `/ip ipsec key qkd print` e `/log print where message~"qkd"`.
- **Certificado/SAN:** o KME precisa ter, no SAN, o IP usado em `address=` (`10.0.0.1` etc.). `qnetlab pki` valida; se mudou
  algo, `qnetlab pki` → `qnetlab images` → `qnetlab deploy` (os certificados dos KMEs estão nas imagens).
- **CN do SAE:** o CN do certificado do CHR deve ser `sae-chrXY` e constar em `kme_topology` do KME.
- **KME fora do ar:** `docker logs GNS3.KMEi.<id>`; teste a conectividade CHR→KME (`/ping 10.0.0.1`).
- **Pools vazios:** veja `logs/KME*-keys.json`; os KMEs precisam de tempo para sincronizar chaves (`sync_interval_s`,
  `generation_delay_s`).
- **Relógio:** certificados têm `notBefore`; o CHR com data muito anterior rejeita o certificado — verifique `/system clock print`
  e `/system ntp client`.

---

## Falhas na atualização/configuração do RouterOS

| Mensagem/sintoma | Explicação | O que fazer |
|---|---|---|
| `input does not match any value of file-name` | Importação de certificado dentro do `.rsc` (versões antigas do QNetLab) | Atualize o código: a PKI agora é por REST, com conferência em `/file`. |
| `/import` falha em `/ip ipsec profile add … comment=…` (coluna do `comment`) | RouterOS 7.22 não aceita `comment` em profile/proposal/peer/identity | Atualize o código (templates sem `comment`; limpeza por nome). |
| `/import` termina sem "executed successfully" | O 7.22 não imprime essa linha | Já tratado: a falha é detectada por "Script Error"/"failure:". |
| Falha de FTP/REST logo após o reboot da atualização | O CHR ainda subia | O framework espera; se persistir, aumente `router.update_timeout`. |
| Updates repetidos a cada `make up` | Cada `--recreate` recria CHRs da imagem 7.16 | Atualize a imagem do template no GNS3 uma vez. |

Para inspecionar exatamente o que será importado: `make render` e leia `build/<lab>/rsc/<CHR>.rsc`.

---

## Problemas de ambiente comuns

| Sintoma | Solução |
|---|---|
| `make test` demorado (~2 min) | Alguns testes de atualização consultam DNS real; é esperado. |
| `pip install` recusa instalar ("externally managed") | Use um `venv` (recomendado) ou `--break-system-packages`. |
| `templates` não encontrado | `pip install -e .` a partir do checkout, ou `export QNETLAB_HOME=…`. |
| Perdi o `state.json` (`make clean`) mas o projeto existe no GNS3 | `qnetlab deploy … --recreate` recria e regrava o estado; ou apague o projeto no GNS3. |
| Quero recomeçar do zero | `make destroy && make clean && make up` |
| Gráficos não aparecem | `pip install -e '.[plots]'` (matplotlib). |

## Coletando informações para pedir ajuda

```console
$ qnetlab plan -t <yaml> --json > plan.json
$ qnetlab verify -t <yaml> -v 2> verify.log
$ tar czf debug.tgz build/<lab>/logs build/<lab>/rsc plan.json verify.log   # sem state.json nem pki/
```

> **Atenção:** os `.rsc` em `build/<lab>/rsc/` contêm PSKs; mascare `secret=` antes de enviar (veja o checklist em
> [07 §6](07-security.md)).
