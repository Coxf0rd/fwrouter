#!/bin/sh
set -eu

STATE_ROOT="${1:-/var/lib/fwrouter-v2}"
LOG_ROOT="${2:-/var/log/fwrouter}"
RUN_ROOT="${3:-/run/fwrouter-v2}"

mkdir -p \
  "$STATE_ROOT/cache" \
  "$STATE_ROOT/generated/mihomo" \
  "$STATE_ROOT/generated/dataplane" \
  "$STATE_ROOT/jobs" \
  "$STATE_ROOT/last-good/dataplane" \
  "$STATE_ROOT/last-good/mihomo" \
  "$STATE_ROOT/last-good/rules" \
  "$STATE_ROOT/mihomo" \
  "$STATE_ROOT/rules" \
  "$STATE_ROOT/state" \
  "$STATE_ROOT/xray" \
  "$LOG_ROOT/operational" \
  "$LOG_ROOT/technical" \
  "$LOG_ROOT/xray" \
  "$RUN_ROOT"

touch "$STATE_ROOT/rules/.gitkeep"
touch "$STATE_ROOT/jobs/.gitkeep"
touch "$STATE_ROOT/cache/.gitkeep"
touch "$STATE_ROOT/state/.gitkeep"
touch "$STATE_ROOT/generated/mihomo/.gitkeep"
touch "$STATE_ROOT/generated/dataplane/.gitkeep"
touch "$STATE_ROOT/mihomo/.gitkeep"
touch "$STATE_ROOT/xray/.gitkeep"

echo "Bootstrapped FWRouter state roots:"
echo "  STATE_ROOT=$STATE_ROOT"
echo "  LOG_ROOT=$LOG_ROOT"
echo "  RUN_ROOT=$RUN_ROOT"
