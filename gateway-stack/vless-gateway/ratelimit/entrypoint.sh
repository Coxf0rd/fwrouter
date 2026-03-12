#!/bin/sh

set -eu

LIMIT_UPLOAD="${LIMIT_UPLOAD:-25mb/s}"
LIMIT_DOWNLOAD="${LIMIT_DOWNLOAD:-25mb/s}"
LIMIT_BURST="${LIMIT_BURST:-50mb}"
LIMIT_PORTS="${LIMIT_PORTS:-8443,9443}"
LIMIT_APPLY_INTERVAL_SEC="${LIMIT_APPLY_INTERVAL_SEC:-30}"

apk add --no-cache iptables >/dev/null 2>&1

ensure_chain() {
  chain="$1"
  if ! iptables -S "$chain" >/dev/null 2>&1; then
    iptables -N "$chain"
  fi
}

ensure_jump() {
  parent="$1"
  child="$2"
  if ! iptables -C "$parent" -j "$child" >/dev/null 2>&1; then
    iptables -I "$parent" 1 -j "$child"
  fi
}

remove_jump() {
  parent="$1"
  child="$2"
  while iptables -C "$parent" -j "$child" >/dev/null 2>&1; do
    iptables -D "$parent" -j "$child" >/dev/null 2>&1 || break
  done
}

delete_chain_if_exists() {
  chain="$1"
  if iptables -S "$chain" >/dev/null 2>&1; then
    iptables -F "$chain" >/dev/null 2>&1 || true
    iptables -X "$chain" >/dev/null 2>&1 || true
  fi
}

delete_legacy_rule() {
  chain="$1"
  shift
  while iptables -C "$chain" "$@" >/dev/null 2>&1; do
    iptables -D "$chain" "$@" >/dev/null 2>&1 || break
  done
}

delete_legacy_by_pattern() {
  chain="$1"
  pattern="$2"
  while true; do
    num="$(iptables -L "$chain" -n --line-numbers | awk -v p="$pattern" 'index($0, p) > 0 {print $1; exit}')"
    if [ -z "$num" ]; then
      break
    fi
    iptables -D "$chain" "$num" >/dev/null 2>&1 || break
  done
}

while true; do
  ensure_chain FWR_VLESS_RATELIMIT_INPUT
  ensure_chain FWR_VLESS_RATELIMIT_OUTPUT

  if iptables -S DOCKER-USER >/dev/null 2>&1; then
    remove_jump DOCKER-USER FWR_VLESS_RATELIMIT_DOCKER
  fi
  delete_chain_if_exists FWR_VLESS_RATELIMIT_DOCKER

  ensure_jump INPUT FWR_VLESS_RATELIMIT_INPUT
  ensure_jump OUTPUT FWR_VLESS_RATELIMIT_OUTPUT

  iptables -F FWR_VLESS_RATELIMIT_INPUT
  iptables -F FWR_VLESS_RATELIMIT_OUTPUT

  delete_legacy_rule DOCKER-USER -p tcp --sport 443 -m hashlimit --hashlimit-above "$LIMIT_DOWNLOAD" --hashlimit-burst "$LIMIT_BURST" --hashlimit-mode dstip --hashlimit-name vless_dl_443 -j DROP
  delete_legacy_rule DOCKER-USER -p tcp --dport 443 -m hashlimit --hashlimit-above "$LIMIT_UPLOAD" --hashlimit-burst "$LIMIT_BURST" --hashlimit-mode srcip --hashlimit-name vless_ul_443 -j DROP
  delete_legacy_by_pattern DOCKER-USER "tcp spt:443 limit: above ${LIMIT_DOWNLOAD} burst ${LIMIT_BURST} mode dstip"
  delete_legacy_by_pattern DOCKER-USER "tcp dpt:443 limit: above ${LIMIT_UPLOAD} burst ${LIMIT_BURST} mode srcip"
  delete_legacy_rule INPUT -p tcp --dport 8443 -m hashlimit --hashlimit-above "$LIMIT_UPLOAD" --hashlimit-burst "$LIMIT_BURST" --hashlimit-mode srcip --hashlimit-name vless_ul_8443 -j DROP
  delete_legacy_rule OUTPUT -p tcp --sport 8443 -m hashlimit --hashlimit-above "$LIMIT_DOWNLOAD" --hashlimit-burst "$LIMIT_BURST" --hashlimit-mode dstip --hashlimit-name vless_dl_8443 -j DROP
  delete_legacy_rule INPUT -p tcp --dport 9443 -m hashlimit --hashlimit-above "$LIMIT_UPLOAD" --hashlimit-burst "$LIMIT_BURST" --hashlimit-mode srcip --hashlimit-name vless_ul_9443 -j DROP
  delete_legacy_rule OUTPUT -p tcp --sport 9443 -m hashlimit --hashlimit-above "$LIMIT_DOWNLOAD" --hashlimit-burst "$LIMIT_BURST" --hashlimit-mode dstip --hashlimit-name vless_dl_9443 -j DROP
  delete_legacy_by_pattern INPUT "tcp dpt:8443 limit: above ${LIMIT_UPLOAD} burst ${LIMIT_BURST} mode srcip"
  delete_legacy_by_pattern OUTPUT "tcp spt:8443 limit: above ${LIMIT_DOWNLOAD} burst ${LIMIT_BURST} mode dstip"
  delete_legacy_by_pattern INPUT "tcp dpt:9443 limit: above ${LIMIT_UPLOAD} burst ${LIMIT_BURST} mode srcip"
  delete_legacy_by_pattern OUTPUT "tcp spt:9443 limit: above ${LIMIT_DOWNLOAD} burst ${LIMIT_BURST} mode dstip"

  OLDIFS="$IFS"
  IFS=','
  for raw_port in $LIMIT_PORTS; do
    port="$(echo "$raw_port" | tr -d '[:space:]')"
    [ -n "$port" ] || continue
    case "$port" in
      ''|*[!0-9]*) continue ;;
    esac
    iptables -A FWR_VLESS_RATELIMIT_INPUT -p tcp --dport "$port" -m hashlimit --hashlimit-above "$LIMIT_UPLOAD" --hashlimit-burst "$LIMIT_BURST" --hashlimit-mode srcip --hashlimit-name "vless_ul_${port}" -j DROP
    iptables -A FWR_VLESS_RATELIMIT_OUTPUT -p tcp --sport "$port" -m hashlimit --hashlimit-above "$LIMIT_DOWNLOAD" --hashlimit-burst "$LIMIT_BURST" --hashlimit-mode dstip --hashlimit-name "vless_dl_${port}" -j DROP
  done
  IFS="$OLDIFS"

  iptables -A FWR_VLESS_RATELIMIT_INPUT -j RETURN
  iptables -A FWR_VLESS_RATELIMIT_OUTPUT -j RETURN

  sleep "$LIMIT_APPLY_INTERVAL_SEC"
done
