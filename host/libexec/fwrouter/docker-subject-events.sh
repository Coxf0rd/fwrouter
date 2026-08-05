#!/bin/sh
set -eu

API_URL="${FWROUTER_API_URL:-http://127.0.0.1:5000/api/v2/system-subjects/sync}"
DEBOUNCE_SECONDS="${FWROUTER_DOCKER_EVENTS_DEBOUNCE_SECONDS:-2}"

trigger_sync() {
  curl \
    --silent \
    --show-error \
    --max-time 20 \
    --header 'Content-Type: application/json' \
    --data '{"requested_by":"docker-events","run_now":true,"discover_docker":true,"discover_host":false}' \
    "$API_URL" >/dev/null
}

while :; do
  if ! command -v docker >/dev/null 2>&1; then
    sleep 30
    continue
  fi

  docker events \
    --filter type=container \
    --filter event=create \
    --filter event=start \
    --filter event=stop \
    --filter event=die \
    --filter event=destroy \
    --filter event=rename \
    --format '{{json .}}' |
  while IFS= read -r _event; do
    sleep "$DEBOUNCE_SECONDS"
    trigger_sync || true
  done

  sleep 5
done
