#!/bin/sh
set -eu

TMP_RESPONSE="$(mktemp)"
cleanup() {
  rm -f "$TMP_RESPONSE"
}
trap cleanup EXIT

defer_collection() {
  printf '{"ok":false,"deferred":true,"reason":"%s"}\n' "$1"
  exit 0
}

# systemd After= orders unit starts; it does not mean the Type=simple API is
# already serving. Admit collection only after the cached-schema health check
# reports ready. The regular timer provides the retry.
if ! command -v jq >/dev/null 2>&1; then
  defer_collection "READINESS_CHECK_UNAVAILABLE"
fi

if ! HEALTH_RESPONSE="$(/usr/bin/curl -fsS --connect-timeout 1 --max-time 2 \
  http://127.0.0.1:5000/api/v2/health 2>/dev/null)"; then
  defer_collection "API_UNAVAILABLE"
fi

if ! printf '%s' "$HEALTH_RESPONSE" \
  | jq -e '.ok == true and .data.status == "healthy" and .data.database.status == "healthy"' >/dev/null 2>&1; then
  defer_collection "API_NOT_READY"
fi

if ! /usr/bin/curl -fsS --max-time 40 \
  -X POST http://127.0.0.1:5000/api/v2/traffic/collect \
  -H 'Content-Type: application/json' \
  -d '{"use_script":true,"script_id":"traffic_collect","dry_run":false,"collector":"systemd_timer"}' \
  -o "$TMP_RESPONSE"; then
  echo '{"ok":false,"error":"traffic_collect_api_request_failed"}'
  exit 1
fi

if command -v jq >/dev/null 2>&1; then
  if jq -e '.error.code == "JOB_CONFLICT"' "$TMP_RESPONSE" >/dev/null 2>&1; then
    exit 0
  fi

  if ! jq -e '.ok == true and .data.job.status == "success"' "$TMP_RESPONSE" >/dev/null 2>&1; then
    jq -c '{
      ok,
      job_id: .data.job.job_id,
      status: .data.job.status,
      error: .error,
      invalid_count: (.data.job.result.traffic.invalid_count // null)
    }' "$TMP_RESPONSE" 2>/dev/null || {
      echo '{"ok":false,"error":"traffic_collect_api_response_parse_failed"}'
      head -c 1000 "$TMP_RESPONSE"
      echo
    }
    exit 1
  fi

  if [ "${FWROUTER_TRAFFIC_COLLECT_VERBOSE:-0}" = "1" ]; then
    jq -c '{
      ok,
      job_id: .data.job.job_id,
      status: .data.job.status,
      updated_count: (.data.job.result.traffic.updated_count // null),
      invalid_count: (.data.job.result.traffic.invalid_count // null)
    }' "$TMP_RESPONSE" 2>/dev/null || true
  fi
fi
