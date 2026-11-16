#!/bin/sh
# Entrypoint de KMEs e APPs.
#
# Variáveis (injetadas pelo framework, via GNS3 ou docker compose):
#   ROLE            kme | app                         (obrigatória)
#   KME_CONFIG      caminho do kme_config.yaml (role kme)
#   NODE_NAME       nome do nó (ex.: KME1)
#   IFACES          "eth0=10.0.3.2/30,eth1=10.0.3.6/30" (modo GNS3; nós ponto a ponto, sem gerenciamento)
#   ROUTES          "rede via gw dev ethK;..." (rotas estáticas)
#   SKIP_NET        =1 para não configurar IPs (docker compose / testes locais)
set -eu

log() { echo "[entrypoint] $*"; }

wait_iface() {
  i=0
  while [ ! -e "/sys/class/net/$1" ]; do
    i=$((i + 1))
    [ "$i" -gt 60 ] && { log "interface $1 não apareceu"; return 1; }
    sleep 1
  done
}

configure_network() {
  [ "${SKIP_NET:-0}" = "1" ] && { log "SKIP_NET=1: rede preservada"; return 0; }
  if [ -n "${IFACES:-}" ]; then
    OLDIFS=$IFS; IFS=,
    for spec in $IFACES; do
      ifn="${spec%%=*}"; cidr="${spec#*=}"
      IFS=$OLDIFS
      wait_iface "$ifn"
      ip addr flush dev "$ifn" || true
      ip addr add "$cidr" dev "$ifn"
      ip link set "$ifn" up
      IFS=,
    done
    IFS=$OLDIFS
  fi
  if [ -n "${ROUTES:-}" ]; then
    echo "$ROUTES" | tr ';' '\n' | while read -r route; do
      [ -n "$route" ] && ip route replace $route
    done
  fi
  ip -br addr || true
}

configure_network

case "${ROLE:?ROLE é obrigatória (kme|app)}" in
  kme)
    : "${KME_ID:?}"
    CONFIG="${KME_CONFIG:-/etc/qnetlab/kme_config.yaml}"
    log "iniciando KME ${KME_ID} (KMS-sim, mTLS) com ${CONFIG}"
    cd /opt/qnetlab/kme
    exec python3 main.py --config "$CONFIG"
    ;;
  app)
    log "iniciando agente SAE ${SAE_ID:-?}"
    exec python /opt/qnetlab/sae/src/main.py
    ;;
  *)
    log "ROLE inválida: ${ROLE}"; exit 64 ;;
esac
