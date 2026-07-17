#!/bin/sh
set -eu

SERVICE_NAME="${FWROUTER_SERVICE_NAME:-fwrouter-api.service}"
BASE_URL="${FWROUTER_BASE_URL:-http://127.0.0.1:5000/api/v2}"
HEALTH_URL="${BASE_URL}/health"
MAX_WAIT_SECONDS="${FWROUTER_RECONCILE_WAIT_SECONDS:-15}"

echo "== daemon reload =="
systemctl daemon-reload

echo
echo "== restart ${SERVICE_NAME} =="
systemctl restart "$SERVICE_NAME"
systemctl status "$SERVICE_NAME" --no-pager || true

echo
echo "== wait for api =="
ready=0
elapsed=0
while [ "$elapsed" -lt "$MAX_WAIT_SECONDS" ]; do
    if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
    elapsed=$((elapsed + 1))
done

if [ "$ready" -ne 1 ]; then
    echo "API did not become ready within ${MAX_WAIT_SECONDS}s" >&2
    exit 1
fi

echo
echo "== bootstrap =="
bash /opt/fwrouter-api/scripts/post-clean-reset-bootstrap.sh

echo
echo "== subscription =="
curl -fsS "${BASE_URL}/subscription" | python3 -m json.tool

echo
echo "== runtime =="
curl -fsS "${BASE_URL}/runtime" | python3 -m json.tool
