#!/bin/sh

set -u

SYNC_INTERVAL_SEC="${SYNC_INTERVAL_SEC:-600}"

apk add --no-cache docker-cli >/dev/null 2>&1

echo "[sync] started, interval=${SYNC_INTERVAL_SEC}s"

while true; do
  python3 /app/vless-gateway/scripts/sync_nodes.py
  rc=$?

  if [ "$rc" -eq 20 ]; then
    echo "[sync] config changed, restarting vless-gateway-xray"
    docker restart vless-gateway-xray >/dev/null 2>&1 || true
  elif [ "$rc" -ne 0 ]; then
    echo "[sync] generator error, rc=$rc"
  else
    echo "[sync] no changes"
  fi

  sleep "$SYNC_INTERVAL_SEC"
done
