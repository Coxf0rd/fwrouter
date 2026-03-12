#!/usr/bin/env bash
set -euo pipefail

KEEP_DAYS="${1:-10}"
LOG_ROOT="/app/zigbee2mqtt/data/log"

if [[ ! -d "$LOG_ROOT" ]]; then
  exit 0
fi

# Zigbee2MQTT creates per-run log directories under data/log.
find "$LOG_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime "+${KEEP_DAYS}" -exec rm -rf {} +
