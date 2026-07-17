#!/bin/sh
set -eu

TMP_RESPONSE="$(mktemp)"
cleanup() {
  rm -f "$TMP_RESPONSE"
}
trap cleanup EXIT

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
