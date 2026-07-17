#!/bin/sh
set -eu

SRC_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)"
TARGET_ROOT="${1:-/}"
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-/usr/bin/systemctl}"
INSTALL_HOST_DEPS="${FWROUTER_INSTALL_HOST_DEPS:-1}"
SETUP_PYTHON_ENV="${FWROUTER_SETUP_PYTHON_ENV:-1}"

require_path() {
  path="$1"
  description="$2"
  if [ ! -e "$path" ]; then
    echo "install-server-tree.sh: missing $description: $path" >&2
    exit 1
  fi
}

install_file() {
  src="$1"
  dst="$2"
  require_path "$src" "source file"
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
}

copy_tree() {
  src="$1"
  dst="$2"
  require_path "$src" "source directory"
  mkdir -p "$dst"
  (
    cd "$src"
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
      -cf - .
  ) | (
    cd "$dst"
    tar -xf -
  )
}

ensure_executable() {
  path="$1"
  if [ -f "$path" ]; then
    chmod 0755 "$path"
  fi
}

copy_tree "$SRC_ROOT/opt/fwrouter-api" "$TARGET_ROOT/opt/fwrouter-api"
copy_tree "$SRC_ROOT/opt/fwrouter-mihomo" "$TARGET_ROOT/opt/fwrouter-mihomo"
copy_tree "$SRC_ROOT/opt/fwrouter-xray" "$TARGET_ROOT/opt/fwrouter-xray"
copy_tree "$SRC_ROOT/opt/fwrouter-ui" "$TARGET_ROOT/opt/fwrouter-ui"
copy_tree "$SRC_ROOT/usr/local/libexec/fwrouter" "$TARGET_ROOT/usr/local/libexec/fwrouter"
install_file "$SRC_ROOT/etc/systemd/system/fwrouter-api.service" "$TARGET_ROOT/etc/systemd/system/fwrouter-api.service"
install_file "$SRC_ROOT/etc/systemd/system/fwrouter-mihomo.service" "$TARGET_ROOT/etc/systemd/system/fwrouter-mihomo.service"
install_file "$SRC_ROOT/etc/systemd/system/fwrouter-xray.service" "$TARGET_ROOT/etc/systemd/system/fwrouter-xray.service"
install_file "$SRC_ROOT/etc/systemd/system/fwrouter-xray-sub-gateway.service" "$TARGET_ROOT/etc/systemd/system/fwrouter-xray-sub-gateway.service"
install_file "$SRC_ROOT/etc/systemd/system/fwrouter-jobs-retention-dry-run.service" "$TARGET_ROOT/etc/systemd/system/fwrouter-jobs-retention-dry-run.service"
install_file "$SRC_ROOT/etc/systemd/system/fwrouter-jobs-retention-dry-run.timer" "$TARGET_ROOT/etc/systemd/system/fwrouter-jobs-retention-dry-run.timer"
install_file "$SRC_ROOT/etc/systemd/system/fwrouter-maintenance.service" "$TARGET_ROOT/etc/systemd/system/fwrouter-maintenance.service"
install_file "$SRC_ROOT/etc/systemd/system/fwrouter-maintenance.timer" "$TARGET_ROOT/etc/systemd/system/fwrouter-maintenance.timer"
install_file "$SRC_ROOT/etc/systemd/system/fwrouter-subscription-refresh.service" "$TARGET_ROOT/etc/systemd/system/fwrouter-subscription-refresh.service"
install_file "$SRC_ROOT/etc/systemd/system/fwrouter-subscription-refresh.timer" "$TARGET_ROOT/etc/systemd/system/fwrouter-subscription-refresh.timer"
install_file "$SRC_ROOT/etc/systemd/system/fwrouter-traffic-collect.service" "$TARGET_ROOT/etc/systemd/system/fwrouter-traffic-collect.service"
install_file "$SRC_ROOT/etc/systemd/system/fwrouter-traffic-collect.timer" "$TARGET_ROOT/etc/systemd/system/fwrouter-traffic-collect.timer"
install_file "$SRC_ROOT/usr/local/sbin/fwrouter-jobs-retention-dry-run" "$TARGET_ROOT/usr/local/sbin/fwrouter-jobs-retention-dry-run"
install_file "$SRC_ROOT/usr/local/sbin/fwrouter-subscription-refresh-job" "$TARGET_ROOT/usr/local/sbin/fwrouter-subscription-refresh-job"
install_file "$SRC_ROOT/etc/sysctl.d/99-fwrouter-routing.conf" "$TARGET_ROOT/etc/sysctl.d/99-fwrouter-routing.conf"
install_file "$SRC_ROOT/etc/iproute2/rt_tables.d/fwrouter.conf" "$TARGET_ROOT/etc/iproute2/rt_tables.d/fwrouter.conf"

ensure_executable "$TARGET_ROOT/opt/fwrouter-api/scripts/bootstrap-state.sh"
ensure_executable "$TARGET_ROOT/opt/fwrouter-api/scripts/check-clean-tree-surface.sh"
ensure_executable "$TARGET_ROOT/opt/fwrouter-api/scripts/check_boot_persistence.sh"
ensure_executable "$TARGET_ROOT/opt/fwrouter-api/scripts/install-host-dependencies.sh"
ensure_executable "$TARGET_ROOT/opt/fwrouter-api/scripts/install-server-tree.sh"
ensure_executable "$TARGET_ROOT/opt/fwrouter-api/scripts/setup-python-env.sh"
ensure_executable "$TARGET_ROOT/usr/local/libexec/fwrouter/dataplane-common.sh"
ensure_executable "$TARGET_ROOT/usr/local/libexec/fwrouter/dataplane-check.sh"
ensure_executable "$TARGET_ROOT/usr/local/libexec/fwrouter/dataplane-apply.sh"
ensure_executable "$TARGET_ROOT/usr/local/libexec/fwrouter/dataplane-rollback.sh"
ensure_executable "$TARGET_ROOT/usr/local/libexec/fwrouter/traffic-collect.sh"
ensure_executable "$TARGET_ROOT/usr/local/libexec/fwrouter/traffic-collect-api.sh"
ensure_executable "$TARGET_ROOT/usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh"
ensure_executable "$TARGET_ROOT/usr/local/libexec/fwrouter/fwrouter-wait-port.sh"
ensure_executable "$TARGET_ROOT/usr/local/libexec/fwrouter/fwrouter-xray-sub-gateway.py"
ensure_executable "$TARGET_ROOT/usr/local/libexec/fwrouter/host-services.py"
ensure_executable "$TARGET_ROOT/usr/local/sbin/fwrouter-jobs-retention-dry-run"
ensure_executable "$TARGET_ROOT/usr/local/sbin/fwrouter-subscription-refresh-job"

mkdir -p "$TARGET_ROOT/var/lib/fwrouter-v2"
sh "$TARGET_ROOT/opt/fwrouter-api/scripts/bootstrap-state.sh" \
  "$TARGET_ROOT/var/lib/fwrouter-v2" \
  "$TARGET_ROOT/var/log/fwrouter" \
  "$TARGET_ROOT/run/fwrouter-v2"

if [ "$TARGET_ROOT" = "/" ] && [ -x "$SYSTEMCTL_BIN" ]; then
  if [ "$INSTALL_HOST_DEPS" != "0" ]; then
    "$TARGET_ROOT/opt/fwrouter-api/scripts/install-host-dependencies.sh" --yes
  fi
  if [ "$SETUP_PYTHON_ENV" != "0" ]; then
    "$TARGET_ROOT/opt/fwrouter-api/scripts/setup-python-env.sh" "$TARGET_ROOT/opt/fwrouter-api"
  fi
  "$SYSTEMCTL_BIN" daemon-reload
  "$SYSTEMCTL_BIN" enable fwrouter-mihomo.service fwrouter-xray.service fwrouter-api.service fwrouter-xray-sub-gateway.service
  "$SYSTEMCTL_BIN" enable fwrouter-maintenance.timer fwrouter-subscription-refresh.timer fwrouter-jobs-retention-dry-run.timer fwrouter-traffic-collect.timer
fi

if [ "$TARGET_ROOT" = "/" ] && [ -x /usr/sbin/sysctl ] && [ -f "$TARGET_ROOT/etc/sysctl.d/99-fwrouter-routing.conf" ]; then
  /usr/sbin/sysctl --system >/dev/null
fi

echo "Installed FWRouter server tree into $TARGET_ROOT"
echo "Next steps:"
echo "  1. create /opt/fwrouter-api/.env on the host from .env.example"
echo "  2. verify docker compose runtime files and start fwrouter-mihomo.service / fwrouter-xray.service / fwrouter-api.service"
