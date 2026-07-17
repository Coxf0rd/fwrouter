#!/usr/bin/env bash
set -euo pipefail

API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:5000/api/v2}"
REQUESTED_BY="${REQUESTED_BY:-post-reset-rules-full-update}"

echo "== health =="
curl -fsS "${API_BASE_URL}/health"
echo
echo

echo "== rules full update =="
curl -fsS -X POST "${API_BASE_URL}/rules/full-update" \
  -H 'Content-Type: application/json' \
  -d "{\"requested_by\":\"${REQUESTED_BY}\",\"run_now\":true}"
echo
echo

echo "== rules effective =="
curl -fsS "${API_BASE_URL}/rules/effective"
echo
echo

echo "== runtime =="
curl -fsS "${API_BASE_URL}/runtime"
echo
