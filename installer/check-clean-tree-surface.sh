#!/bin/sh
set -eu

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

check_file() {
  path="$1"
  [ -f "$REPO_ROOT/$path" ] || fail "missing: $path"
}

check_dir() {
  path="$1"
  [ -d "$REPO_ROOT/$path" ] || fail "missing directory: $path"
}

echo "== component roots =="
check_dir backend
check_dir ui
check_dir runtimes/mihomo
check_dir runtimes/xray
check_dir host/systemd
check_dir host/libexec/fwrouter
check_dir host/sbin
check_dir installer
check_dir knowledge

echo "== backend =="
check_file backend/pyproject.toml
check_file backend/fwrouter_api/main.py
check_file backend/.env.example

echo "== ui =="
check_file ui/index.html

echo "== runtimes =="
check_file runtimes/mihomo/docker-compose.yml
check_file runtimes/xray/docker-compose.yml

echo "== host files =="
for unit in \
  fwrouter-api.service \
  fwrouter-mihomo.service \
  fwrouter-xray.service \
  fwrouter-xray-sub-gateway.service \
  fwrouter-jobs-retention-dry-run.service \
  fwrouter-jobs-retention-dry-run.timer \
  fwrouter-maintenance.service \
  fwrouter-maintenance.timer \
  fwrouter-subscription-refresh.service \
  fwrouter-subscription-refresh.timer \
  fwrouter-traffic-collect.service \
  fwrouter-traffic-collect.timer
do
  check_file "host/systemd/$unit"
done

for helper in \
  dataplane-common.sh dataplane-check.sh dataplane-apply.sh dataplane-rollback.sh \
  fwrouter-boot-preflight.sh fwrouter-wait-port.sh fwrouter-xray-sub-gateway.py \
  host-services.py traffic-collect.sh traffic-collect-api.sh
do
  check_file "host/libexec/fwrouter/$helper"
done

check_file host/sbin/fwrouter-subscription-refresh-job
check_file host/sbin/fwrouter-jobs-retention-dry-run
check_file host/sysctl.d/99-fwrouter-routing.conf
check_file host/iproute2/rt_tables.d/fwrouter.conf

echo "== installer =="
check_file installer/install.sh
check_file installer/install-host-dependencies.sh

echo "== git safety exclusions =="
if find "$REPO_ROOT" \
  \( -path "$REPO_ROOT/.git" -o -name '__pycache__' -o -name '.pytest_cache' -o -path "$REPO_ROOT/backend/.venv" \) -prune -o \
  \( -name '.env' -o -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' -o -name '*.db-wal' -o -name '*.db-shm' -o -name '*.pyc' -o -name '*.bak' -o -name '*.bak-*' -o -name '*.tar.zst' \) \
  -print | grep -q .
then
  find "$REPO_ROOT" \
    \( -path "$REPO_ROOT/.git" -o -name '__pycache__' -o -name '.pytest_cache' -o -path "$REPO_ROOT/backend/.venv" \) -prune -o \
    \( -name '.env' -o -name '*.db' -o -name '*.sqlite' -o -name '*.sqlite3' -o -name '*.db-wal' -o -name '*.db-shm' -o -name '*.pyc' -o -name '*.bak' -o -name '*.bak-*' -o -name '*.tar.zst' \) \
    -print >&2
  fail "source tree contains secret/runtime/backup artifacts"
fi

echo "OK: FWRouter monorepo surface is clean"
