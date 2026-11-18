#!/usr/bin/env python3
"""Gera ``docs/05-configuration-reference.md`` a partir dos dataclasses de ``qnetlab.config.models``.

Tipos e valores-padrão vêm do código (nunca ficam desatualizados); as descrições vêm de ``DESCRIPTIONS``
abaixo. Um teste (``tests/test_docs.py``) falha se:

* algum campo do modelo não tiver descrição aqui;
* o arquivo versionado diferir do que este script gera.

Uso::

    python scripts/gen_config_reference.py          # reescreve o arquivo
    python scripts/gen_config_reference.py --check  # só verifica (código de saída 1 se desatualizado)
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qnetlab.config import models as m  # noqa: E402

TARGET = ROOT / "docs" / "05-configuration-reference.md"

# (seção YAML, classe) na ordem em que aparecem no arquivo.
SECTIONS: List[Tuple[str, type, str]] = [
    ("gns3", m.Gns3Config, "Conexão com o servidor GNS3 (REST v2)."),
    ("addressing", m.AddressingConfig,
     "Pools de endereços e rede de gerenciamento. Cada enlace ponto a ponto recebe um `/30` do pool correspondente "
     "(`.1` e `.2`). Veja [03 — Topologia e endereçamento](03-topology-addressing.md)."),
    ("router", m.RouterConfig, "Roteadores MikroTik CHR: template, credenciais, versão do RouterOS e papel de cada interface."),
    ("containers", m.ContainerConfig, "Imagem Docker dos KMEs/NODEs e portas dos serviços."),
    ("kme", m.KmeConfig, "Parâmetros de `peer_sync` do KME (esquema do KMS-sim). Os padrões são os do código do KME."),
    ("ospf", m.OspfConfig, "OSPFv2 entre os CHRs."),
    ("ipsec", m.IpsecConfig,
     "IPsec IKEv2 com PPK nos enlaces entre sites. Cada CHR tem exatamente um enlace `ether2` e, portanto, um túnel "
     "e um objeto `/ip ipsec key qkd`."),
    ("pki", m.PkiConfig, "Autoridade certificadora (OpenSSL) e certificados."),
    ("load", m.LoadConfig, "Carga de teste executada pelos agentes dos NODEs (`qnetlab load` / `run`)."),
]

DESCRIPTIONS: Dict[str, Dict[str, str]] = {
    "gns3": {
        "url": "URL base do servidor GNS3. O cliente acrescenta `/v{api_version}`.",
        "api_version": "Versão da API REST do GNS3 (a 2.2.x usa `2`).",
        "user": "Usuário HTTP Basic do servidor (vazio = sem autenticação). Use `${GNS3_USER:-}`.",
        "password": "Senha HTTP Basic. Use `${GNS3_PASSWORD:-}`; nunca grave a senha no YAML.",
        "project_name": "Nome do projeto no GNS3. Vazio = `name` da topologia.",
        "compute_id": "ID do compute do GNS3 onde os nós são criados.",
        "verify_tls": "Valida o certificado TLS quando `url` é `https://`.",
        "timeout": "Timeout (s) de cada requisição REST.",
        "start_timeout": "Tempo máximo (s) para iniciar/parar todos os nós e aguardar o estado `started`.",
    },
    "addressing": {
        "kme_access_pool": "KME ↔ CHR (`ether1`): KME `.1`, CHR `.2`.",
        "ring_pool": "CHR ↔ CHR entre sites (`ether2`, enlace protegido por IPsec/QKD). O extremo de menor (site, j) fica com `.1`.",
        "sibling_pool": "CHR ↔ CHR dentro do site (`ether3`): MKTi1 `.1`, MKTi2 `.2`.",
        "node_pool": "CHR ↔ NODE (`ether7`): CHR `.1`, NODE `.2`. É o espaço de endereços protegido pelos túneis.",
        "kma_pool": "KME ↔ KME (enlaces KMA): KMEi `.1` (eth0), KME(i+1) `.2` (eth1).",
        "router_id_pool": "Base do router-id OSPF: `base + 10*site + j`.",
        "mgmt_subnet": "Rede do nó NAT do GNS3 (normalmente `virbr0`, `192.168.122.0/24`). O host precisa alcançá-la.",
        "mgmt_gateway": "Gateway da rede de gerenciamento (o IP do host em `virbr0`).",
        "mgmt_attach": "`nat`: um nó NAT por CHR em `router.if_mgmt`; `none`: sem gerenciamento (não use com `configure`).",
        "mgmt_start": "Último octeto do IP de gerenciamento do 1º CHR (MKT11).",
        "mgmt_step": "Incremento entre CHRs consecutivos (MKT11, MKT12, MKT21…). Com `mgmt_step: 10` cabem anéis de até 12 sites; com `5`, até 24.",
    },
    "router": {
        "gns3_template": "Nome do template do CHR no GNS3.",
        "admin_user": "Usuário administrador do RouterOS.",
        "admin_password": "Senha do admin, definida no `bootstrap`. Use `${QNETLAB_ADMIN_PASSWORD:-…}`.",
        "boot_timeout": "Tempo máximo (s) para o console serial do CHR apresentar o prompt de login no `bootstrap`.",
        "min_version": "Versão mínima do RouterOS (QKD/PPK exigem ≥ 7.22). Vazio desliga a checagem.",
        "auto_update": "Atualiza o CHR automaticamente quando a versão é menor que a pretendida.",
        "update_channel": "Canal de atualização: `stable`, `long-term` ou `testing`.",
        "update_method": "`host`: o host baixa o `.npk` e o envia por FTP (recomendado); `device`: o CHR baixa (precisa de DNS/Internet).",
        "update_version": "Fixa a versão exata (ex.: `7.22.1`); o resultado é verificado. Vazio = `min_version`. Não pode ser menor que `min_version`.",
        "update_dns": "DNS usado só durante a atualização `device` (vazio = gateway do NAT).",
        "update_timeout": "Tempo máximo (s) de download/envio + reinício por roteador.",
        "neighbor_discovery": "Liga a descoberta de vizinhos (MNDP/LLDP/CDP). Desligada por padrão: ela anuncia, em claro e em todas as interfaces (inclusive a do túnel), a identidade e a versão do RouterOS (achado do SEG-01).",
        "if_kme": "Interface do CHR voltada ao KME do site.",
        "if_ring": "Interface voltada ao CHR do site vizinho (carrega o túnel IPsec/QKD).",
        "if_sibling": "Interface voltada ao outro CHR do mesmo site.",
        "if_node": "Interface voltada ao NODE do site.",
        "if_mgmt": "Interface voltada ao nó NAT (gerenciamento/REST).",
    },
    "containers": {
        "base_image": "Tag da imagem-base única usada por KMEs e NODEs.",
        "kme_source_dir": "Pasta com um `main.py` de KME que substitui o vendorizado (ex.: `~/Codes/KMS-sim/src`).",
        "app_source_dir": "Pasta com a sua aplicação de NODE (ex.: cliente DFL) que substitui o agente embutido.",
        "kme_api_port": "Porta HTTPS (mTLS) da API ETSI QKD 014 do KME.",
        "metrics_port": "Porta HTTP do agente do NODE (`/control/*`, `/metrics.jsonl`), acessada via `docker exec`.",
        "probe_port": "Porta UDP do eco/sondas entre NODEs.",
        "key_size_bits": "Tamanho de chave em bits. O KMS-sim só entrega 256; outro valor exige `kme_source_dir`.",
    },
    "kme": {
        "sync_interval_s": "Intervalo (s) entre varreduras de sincronização de chaves com cada vizinho.",
        "generation_delay_s": "Pausa (s) entre chaves de uma varredura — controla a taxa de geração simulada.",
        "sync_threshold": "Quantidade de chaves sincronizadas mantidas por vizinho.",
        "request_timeout_ms": "Timeout (ms) das requisições de sincronização entre KMEs.",
        "max_retries": "Tentativas por requisição de sincronização.",
        "log_level": "Nível de log do KME (`DEBUG`, `INFO`, …).",
    },
    "ospf": {
        "instance": "Nome da instância OSPF.",
        "area": "Nome da área.",
        "area_id": "Identificador da área (backbone = `0.0.0.0`).",
        "ring_cost": "Custo OSPF dos enlaces entre sites (`ether2`).",
        "sibling_cost": "Custo OSPF do enlace entre os dois CHRs do site (`ether3`).",
    },
    "ipsec": {
        "enabled": "Gera túneis IPsec (`false` = laboratório só com OSPF).",
        "dh_group": "Grupo DH do IKE (profile).",
        "pfs_group": "Grupo PFS do ESP (proposal).",
        "hash_algorithm": "Hash do IKE (profile).",
        "enc_algorithm": "Cifra do IKE (profile).",
        "proposal_auth": "Autenticação do ESP (proposal).",
        "proposal_enc": "Cifra do ESP (proposal).",
        "lifetime": "Tempo de vida da SA do IKE (profile).",
        "proposal_lifetime": "Tempo de vida da Child/ESP SA (proposal). Com `1m`, o túnel rechaveia a cada minuto (uma chave QKD por rekey). Formatos: `30s`, `1m`, `1h`, `00:01:00`.",
        "auth_method": "`psk` ou `cert` (autenticação do IKE).",
        "psk": "PSK fixo; vazio = um segredo aleatório por túnel, guardado em `state.json`.",
        "ppk_mode": "`qkd` (chave PPK vem do KME), `static` (PPK estático, só para testes) ou `none`.",
        "qkd_key_size": "`key-size` de `/ip ipsec key qkd` (o KME trata ≤ 128 como bytes).",
        "qkd_cache_size": "`cache-size` de `/ip ipsec key qkd`.",
        "enforce_encryption": "Descarta no `forward` tráfego NODE↔NODE em claro (sem política IPsec) em `ether2`.",
        "unprotected_links": "Enlaces do anel (1..N) deixados em claro — grupo de controle para comparar latência com/sem IPsec.",
    },
    "pki": {
        "key_type": "Tipo de chave: `rsa-2048`, `rsa-3072`, `rsa-4096`, `ec-p256` ou `ec-p384`.",
        "validity_days": "Validade (dias) dos certificados de KMEs e CHRs.",
        "ca_validity_days": "Validade (dias) da Root CA.",
        "organization": "Atributo `O` dos certificados.",
        "country": "Atributo `C` dos certificados.",
    },
    "load": {
        "duration_s": "Duração (s) da carga.",
        "warmup_s": "Espera (s) antes de iniciar, para o IKE/QKD estabilizar.",
        "probe_interval_ms": "Intervalo (ms) entre sondas UDP por par.",
        "probe_size_bytes": "Tamanho nominal (bytes) da sonda, mínimo 16; o payload UDP real tem `size − 4` bytes (veja o README do agente).",
        "key_interval_s": "Reservado (0 = desligado): neste laboratório os SAEs são os CHRs, não os NODEs.",
        "peers": "`adjacent`: cada NODE sonda os NODEs vizinhos pelos túneis; `none`: sem sondas.",
    },
}


def _type_name(tp: object) -> str:
    s = str(tp).replace("typing.", "")
    s = s.removeprefix("<class '").removesuffix("'>")
    return s.replace("Tuple[int, ...]", "lista de inteiros").replace("tuple[int, ...]", "lista de inteiros")


def _default(f) -> str:
    if f.default is not MISSING:
        v = f.default
    elif f.default_factory is not MISSING:  # type: ignore[misc]
        return "—"
    else:
        return "obrigatório"
    if isinstance(v, str):
        return f"`\"{v}\"`" if v else "`\"\"`"
    if isinstance(v, tuple):
        return "`[]`" if not v else f"`{list(v)}`"
    return f"`{str(v).lower() if isinstance(v, bool) else v}`"


def missing_descriptions() -> List[str]:
    out = []
    for key, cls, _ in SECTIONS:
        for f in fields(cls):
            if f.name not in DESCRIPTIONS.get(key, {}):
                out.append(f"{key}.{f.name}")
    return out


def render() -> str:
    miss = missing_descriptions()
    if miss:
        raise SystemExit(f"Campos sem descrição em DESCRIPTIONS: {miss}")
    top = [
        ("name", "string", "obrigatório", "Nome da topologia. Letras, dígitos, `-` e `_`. Nomeia o diretório `build/<name>/` e o projeto GNS3."),
        ("description", "string", "`\"\"`", "Texto livre."),
        ("ring.size", "inteiro", "obrigatório", "Número de sites do anel, de 3 a 24 (cada site = 2 CHRs + KME + NODE). Limite de gerenciamento: veja `addressing.mgmt_step`."),
    ]
    L: List[str] = []
    a = L.append
    a("<!-- ARQUIVO GERADO por scripts/gen_config_reference.py — não edite à mão. -->")
    a("# 05 — Referência de configuração")
    a("")
    a("Uma topologia é um arquivo YAML (ou JSON) declarativo. Tipos e padrões abaixo são extraídos do código "
      "(`src/qnetlab/config/models.py`); o schema JSON equivalente está em `schema/topology.schema.json`.")
    a("")
    a("**Regras gerais**")
    a("")
    a("- Chaves desconhecidas são **rejeitadas** (erro de digitação não passa em silêncio).")
    a("- Seções omitidas usam os padrões. Só `name` e `ring.size` são obrigatórios.")
    a("- Interpolação de ambiente em qualquer string: `${VAR}` (obrigatória) ou `${VAR:-padrão}`.")
    a("- Valide sempre com `qnetlab validate -t <arquivo>` antes de implantar.")
    a("")
    a("## Raiz")
    a("")
    a("| Chave | Tipo | Padrão | Descrição |")
    a("|---|---|---|---|")
    for k, t, d, desc in top:
        a(f"| `{k}` | {t} | {d} | {desc} |")
    a("")
    for key, cls, intro in SECTIONS:
        a(f"## `{key}`")
        a("")
        a(intro)
        a("")
        a("| Chave | Tipo | Padrão | Descrição |")
        a("|---|---|---|---|")
        for f in fields(cls):
            a(f"| `{f.name}` | {_type_name(f.type)} | {_default(f)} | {DESCRIPTIONS[key][f.name]} |")
        a("")
    a("## Validações aplicadas")
    a("")
    a("| Regra | Erro se violada |")
    a("|---|---|")
    for rule, err in [
        ("`name` com caracteres fora de `[A-Za-z0-9_-]`", "`ConfigError`"),
        ("`ring.size` fora de 3..24", "`ConfigError`"),
        ("pools/rede/gateway que não são IPv4 válidos", "`ConfigError`"),
        ("`router.min_version` / `update_version` fora de `N.N[.N]`; `update_version` < `min_version`", "`ConfigError`"),
        ("`router.update_method` ∉ {host, device}; `update_channel` ∉ {stable, long-term, testing}", "`ConfigError`"),
        ("`router.if_*` que não seja `etherN` ou que se repita entre papéis", "`ConfigError`"),
        ("`ipsec.proposal_lifetime` fora de `30s` / `1m` / `1h` / `00:01:00`", "`ConfigError`"),
        ("`ipsec.ppk_mode` ∉ {qkd, static, none}; `auth_method` ∉ {psk, cert}", "`ConfigError`"),
        ("`ipsec.unprotected_links` com enlace fora de 1..N", "`ConfigError`"),
        ("`pki.key_type` desconhecido", "`ConfigError`"),
        ("`containers.key_size_bits` ≠ 256 sem `kme_source_dir`", "`ConfigError`"),
        ("`load.peers` ∉ {adjacent, none}", "`ConfigError`"),
        ("pools sobrepostos, pool sem espaço para os `/30` necessários, `mgmt_subnet` que não seja `/24`, `mgmt_gateway` fora da rede ou colidindo com um CHR, ou IP de gerenciamento além de `.254`", "`AddressingError`"),
    ]:
        a(f"| {rule} | {err} |")
    a("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="não escreve; falha se o arquivo estiver desatualizado")
    args = ap.parse_args()
    text = render()
    if args.check:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != text:
            print(f"{TARGET} está desatualizado: rode 'python scripts/gen_config_reference.py'", file=sys.stderr)
            return 1
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text, encoding="utf-8")
    print(f"escrito: {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
