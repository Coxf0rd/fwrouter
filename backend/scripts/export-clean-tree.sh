#!/bin/sh
set -eu

SRC_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)"
TARGET_DIR="${1:-}"

if [ -z "$TARGET_DIR" ]; then
  echo "Usage: $0 TARGET_DIR" >&2
  exit 1
fi

rm -rf "$TARGET_DIR"
mkdir -p "$TARGET_DIR"

(
  cd "$SRC_ROOT"
  tar \
    --exclude='.env' \
    --exclude='.venv' \
    --exclude='.pytest_cache' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='*.db' \
    --exclude='*.sqlite' \
    --exclude='*.sqlite3' \
    --exclude='*.bak' \
    --exclude='*.bak-*' \
    --exclude='.git' \
    --exclude='containerd' \
    -cf - \
    ./opt/fwrouter-api \
    ./opt/fwrouter-mihomo \
    ./opt/fwrouter-xray \
    ./opt/fwrouter-ui \
    ./usr/local/libexec/fwrouter \
    ./etc/systemd/system/fwrouter-api.service \
    ./etc/systemd/system/fwrouter-mihomo.service \
    ./etc/systemd/system/fwrouter-xray.service \
    ./etc/systemd/system/fwrouter-xray-sub-gateway.service \
    ./etc/systemd/system/fwrouter-jobs-retention-dry-run.service \
    ./etc/systemd/system/fwrouter-jobs-retention-dry-run.timer \
    ./etc/systemd/system/fwrouter-maintenance.service \
    ./etc/systemd/system/fwrouter-maintenance.timer \
    ./etc/systemd/system/fwrouter-subscription-refresh.service \
    ./etc/systemd/system/fwrouter-subscription-refresh.timer \
    ./etc/systemd/system/fwrouter-traffic-collect.service \
    ./etc/systemd/system/fwrouter-traffic-collect.timer \
    ./usr/local/sbin/fwrouter-jobs-retention-dry-run \
    ./usr/local/sbin/fwrouter-subscription-refresh-job \
    ./etc/sysctl.d/99-fwrouter-routing.conf \
    ./etc/iproute2/rt_tables.d/fwrouter.conf \
    ./knowledge \
    ./docs \
    ./.gitignore
) | (
  cd "$TARGET_DIR"
  tar -xf -
)

echo "Exported clean FWRouter tree into $TARGET_DIR"
