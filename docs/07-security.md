# 07 — Segurança

> O QNetLab é um **laboratório de pesquisa**. Ele aplica boas práticas onde são baratas (PKI estrita, segredos fora do código,
> mascaramento em pacotes), mas faz concessões de laboratório (mgmt em HTTP/FTP, senha padrão, PSK) documentadas abaixo.
> **Não** use a configuração gerada em produção sem revisão.

## 1. Modelo de ameaça (escopo do laboratório)

| Ativo | Ameaça considerada | Controle |
|---|---|---|
| Tráfego NODE↔NODE entre sites | Interceptação/modificação no enlace `ether2` | IPsec IKEv2 + ESP AES-256, PPK de origem QKD (RFC 8784), rekey a cada `proposal_lifetime`; descarte de tráfego em claro (`enforce_encryption`) |
| Chaves QKD | Cliente não autorizado pedindo chaves ao KME | mTLS; o KME identifica o SAE pelo **CN** do certificado cliente |
| Sessão CHR↔KME | Falsificação do KME | RouterOS valida o certificado do KME contra `qnetlab-ca` e `kme-id`; **SANs estritos** (todos os IPs do KME) |
| Segredos do laboratório (PSK, *seeds*, senha) | Vazamento por pacote/log/versionamento | `state.json` 0600; mascaramento em `output.tar.gz`; `.gitignore` de `build/`; PEMs e `.rsc` apagados dos roteadores |
| Estado dos roteadores | Configuração acidental | `.rsc` idempotentes e revisáveis (`qnetlab render`) |

**Fora de escopo:** atacante com acesso ao host, ao GNS3 ou ao Docker do host; ataques ao plano de gerenciamento
(`192.168.122.0/24`); canais laterais; a segurança "quântica" do QKD em si (o KME é um simulador).

## 2. PKI

### Hierarquia

```text
QNetLab Root CA   (RSA-3072 por padrão, SHA-384, pathlen:0, validade 3650 d)
├── kme-sim-i     (servidor + cliente)  CN=kme-sim-i   SAN: 127.0.0.1 + todos os IPs do KME + DNS kme-sim-i, kmei, localhost
└── sae-chrXY     (somente cliente)     CN=sae-chrXY   SAN: todos os IPs do CHR (inclui mgmt) + DNS mktxy
```

| Propriedade | Valor |
|---|---|
| Tipo de chave | `pki.key_type`: `rsa-2048/3072/4096`, `ec-p256`, `ec-p384` |
| Assinatura | SHA-384 |
| Validade | `pki.validity_days` (825) para folhas; `pki.ca_validity_days` (3650) para a CA |
| Restrições | `CA:FALSE` nas folhas; `keyUsage` crítico; EKU `serverAuth,clientAuth` (KME) ou `clientAuth` (SAE) |
| Chave privada | gerada localmente (`openssl genpkey`), modo `0600` |

### Por que os SANs são estritos

O RouterOS valida o certificado do KME **pelo IP usado na conexão**. O SAN do KME precisa conter todos os IPs em que ele pode
ser alcançado (`eth2`, `eth3`, KMA…). Se um IP faltar, o `key qkd` falha no *handshake*. `qnetlab pki` verifica a cadeia e
confere os SANs **depois** de emitir (`verify_all`).

### Identidade do SAE = CN

O KME lê o SAE do certificado cliente (`SSL_CLIENT_CN`). Portanto o CN do certificado do CHR **é** o seu `sae-id`, e o mesmo
valor aparece em `kme_topology.connected_saes` e em `peer-sae-id` do outro lado do túnel.

### Ciclo de vida

| Operação | Efeito |
|---|---|
| `qnetlab pki` | Emite só o que falta ou mudou (comparando *fingerprint* de CN+SAN+tipo de chave). Idempotente. |
| `qnetlab pki --force` | **Nova CA** e reemissão de tudo. Depois: `qnetlab images` (certificados dos KMEs ficam nas imagens) e `qnetlab configure`. |
| Mudou IP/anel | O plano muda → *fingerprint* muda → só as entidades afetadas são reemitidas. |
| Expiração | Não há renovação automática; reemita com `--force` antes de `pki.validity_days`. |

### Onde ficam as chaves

| Local | Conteúdo | Observação |
|---|---|---|
| `build/<lab>/pki/` | CA, certificados e **chaves privadas** | Nunca copie `*.key.pem` para outro lugar; `build/` está no `.gitignore`. |
| Imagens Docker dos KMEs | certificado e **chave privada** do KME (camada `/certs`) | Nós Docker do GNS3 não têm *bind mounts*. **Não publique** essas imagens em registry. |
| CHR | certificado + chave do SAE, importados no armazém de certificados | Os PEMs enviados por FTP são removidos após a importação. |

## 3. Segredos

| Segredo | Origem | Onde vive | Exposição |
|---|---|---|---|
| PSK do IKE por túnel | `secrets.token_hex(32)` ou `ipsec.psk` | `state.json` → `.rsc` do CHR (`secret=`) | mascarado no pacote; `.rsc` apagado do CHR |
| *Seed* KMA por par de KMEs | `secrets.token_hex(32)` | `state.json` → `kme_config.yaml` (na imagem) | ambos os lados precisam do mesmo valor |
| PPK estático (`ppk_mode: static`) | `secrets.token_hex(32)` | `state.json` → `.rsc` | só para testes; mascarado |
| Senha do `admin` | `QNETLAB_ADMIN_PASSWORD` | ambiente → `*.bootstrap.rsc` (local) | não vai ao `state.json`; mascarada (`password=`) no pacote |
| Senha do GNS3 | `GNS3_PASSWORD` | ambiente | |

Garantias do código:

- `state.json` é escrito de forma **atômica** (arquivo temporário + `os.replace`) com modo `0600`.
- Os segredos são gerados **uma vez** por laboratório (reexecuções reutilizam) e **nunca** compartilhados entre laboratórios
  (cada `build/<nome>/` tem o seu).
- `output.tar.gz` mascara `secret="…"`, `ppk-secret="…"` e `password="…"` nos `.rsc` (substitui por `***`) e traz
  `manifest.json` com SHA-256 de cada arquivo.
- A senha do `admin` aparece em claro em `build/<lab>/rsc/<CHR>.bootstrap.rsc` (arquivo local, enviado ao CHR só pelo console); no pacote ela é mascarada.

> **Atenção:** `build/<lab>/rsc/*.rsc` (fora do pacote) contêm PSKs em claro, e `build/<lab>/state.json` contém todos os
> segredos. Não versione, não anexe a *issues* e não compartilhe `build/` sem revisar.

## 4. Concessões de laboratório (e como endurecer)

| Concessão | Risco | Como endurecer |
|---|---|---|
| REST do RouterOS em HTTP (`www`, porta 80) e FTP em claro na rede de gerenciamento | Credenciais e chaves privadas trafegam sem TLS em `192.168.122.0/24` | Isole `virbr0`; para uso fora do laboratório, troque por `www-ssl` + `sftp`/SCP (requer evoluir `RouterOsClient`) |
| `www`/`ftp` restritos à rede de gerenciamento (`address=<mgmt_net>`) | — | Já aplicado pelo *bootstrap* |
| Senha padrão `qnetlab-lab` | Acesso administrativo trivial | Defina `QNETLAB_ADMIN_PASSWORD` |
| `auth_method: psk` por padrão | Chave pré-compartilhada em vez de certificados | `ipsec.auth_method: cert` (reutiliza os certificados SAE) |
| `ppk_mode: static` | PPK não é QKD | Use `qkd` (padrão) |
| Logging `ipsec,debug` em memória | Depuração verbosa | Remova a seção 50 ou reduza o nível para produção |
| Comportamento do IKE quando o PPK não está disponível | O RFC 8784 admite PPK opcional (*fallback* sem PPK) | Em `ppk_mode: static` o template usa `ppk=required`; em `qkd` usa `ppk=qkd`. Confirme na documentação do RouterOS 7.22 o comportamento do `ppk=qkd` quando o KME está indisponível (não validado neste projeto) |
| Imagens com chave privada do KME | Vazamento se a imagem for publicada | Mantenha as imagens locais |
| Tráfego do roteador e KME↔CHR fora dos túneis (bypass) | Intencional: evita dependência circular chave↔túnel | Mantenha o KMA e `ether1` em segmento confiável |

## 5. Proteções contra falhas de configuração

- **Injeção em scripts RouterOS:** valores que entram entre aspas nos `.rsc` (ex.: `ipsec.psk`) não podem conter aspas, `\`, `$` nem quebras de linha (`ConfigError`).
- **Chaves desconhecidas no YAML são erro** (evita parâmetros de segurança ignorados por typo).
- **Validação antes de agir:** `ring.size`, pools, `ppk_mode`, `key_type`, versões do RouterOS etc. são verificados em `validate`.
- **Versão mínima do RouterOS** (`router.min_version`, padrão 7.22): `configure` aborta se o CHR for mais antigo (e
  `auto_update: false`); `update_version` não pode ser menor que `min_version`.
- **`enforce_encryption: true`:** se o IPsec falhar, o tráfego NODE↔NODE é **perdido** em vez de passar em claro (`drop`).
- **Mascaramento obrigatório** de segredos no pacote de resultados.

## 6. Checklist antes de compartilhar resultados

- [ ] Compartilhar **apenas** `output.tar.gz` (não `build/`).
- [ ] Conferir `tar tzf output.tar.gz | grep -i key` — não deve haver chaves privadas.
- [ ] Conferir `tar xzOf output.tar.gz rsc/MKT11.rsc | grep -i secret` — deve mostrar `***`.
- [ ] Confirmar que `logs/` não contém senhas coladas por você em depurações.
- [ ] Trocar a senha do `admin` se o GNS3 for acessível a terceiros.
