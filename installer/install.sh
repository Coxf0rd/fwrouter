#!/bin/sh
set -eu

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
TARGET_ROOT="/"
INSTALL_HOST_DEPS="${FWROUTER_INSTALL_HOST_DEPS:-1}"
SETUP_PYTHON_ENV="${FWROUTER_SETUP_PYTHON_ENV:-1}"
ENABLE_UNITS="${FWROUTER_ENABLE_UNITS:-1}"
COMPONENTS=""

usage() {
  cat >&2 <<'EOF'
Usage:
  installer/install.sh --all [--target /]
  installer/install.sh --component backend [--component ui ...] [--target /]

Components: backend, ui, mihomo, xray, host, docs, all
Environment:
  FWROUTER_INSTALL_HOST_DEPS=0  skip apt dependency install
  FWROUTER_SETUP_PYTHON_ENV=0   skip backend venv setup
  FWROUTER_ENABLE_UNITS=0       skip systemd enable/daemon-reload
EOF
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --all)
      COMPONENTS="$COMPONENTS all"
      shift
      ;;
    --component)
      [ "$#" -ge 2 ] || usage
      COMPONENTS="$COMPONENTS $2"
      shift 2
      ;;
    --target)
      [ "$#" -ge 2 ] || usage
      TARGET_ROOT="${2%/}"
      [ -n "$TARGET_ROOT" ] || TARGET_ROOT="/"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      usage
      ;;
  esac
done

[ -n "$COMPONENTS" ] || usage

target_path() {
  if [ "$TARGET_ROOT" = "/" ]; then
    printf '/%s\n' "$1"
  else
    printf '%s/%s\n' "$TARGET_ROOT" "$1"
  fi
}

require_path() {
  path="$1"
  description="$2"
  if [ ! -e "$path" ]; then
    echo "install.sh: missing $description: $path" >&2
    exit 1
  fi
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
      --exclude='.env.*' \
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
      -cf - .
  ) | (
    cd "$dst"
    tar -xf -
  )
}

install_file() {
  src="$1"
  dst="$2"
  require_path "$src" "source file"
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
}

ensure_executable() {
  path="$1"
  if [ -f "$path" ]; then
    chmod 0755 "$path"
  fi
}

want_component() {
  wanted="$1"
  for component in $COMPONENTS; do
    [ "$component" = "all" ] && return 0
    [ "$component" = "$wanted" ] && return 0
  done
  return 1
}

install_backend() {
  copy_tree "$REPO_ROOT/backend" "$(target_path opt/fwrouter-api)"
  ensure_executable "$(target_path opt/fwrouter-api/scripts/bootstrap-state.sh)"
  ensure_executable "$(target_path opt/fwrouter-api/scripts/check-clean-tree-surface.sh)"
  ensure_executable "$(target_path opt/fwrouter-api/scripts/check_boot_persistence.sh)"
  ensure_executable "$(target_path opt/fwrouter-api/scripts/install-host-dependencies.sh)"
  ensure_executable "$(target_path opt/fwrouter-api/scripts/install-server-tree.sh)"
  ensure_executable "$(target_path opt/fwrouter-api/scripts/setup-python-env.sh)"
}

install_ui() {
  copy_tree "$REPO_ROOT/ui" "$(target_path opt/fwrouter-ui)"
}

install_mihomo() {
  copy_tree "$REPO_ROOT/runtimes/mihomo" "$(target_path opt/fwrouter-mihomo)"
}

install_xray() {
  copy_tree "$REPO_ROOT/runtimes/xray" "$(target_path opt/fwrouter-xray)"
}

install_docs() {
  copy_tree "$REPO_ROOT/knowledge" "$(target_path knowledge)"
  copy_tree "$REPO_ROOT/docs" "$(target_path docs)"
}

install_host() {
  copy_tree "$REPO_ROOT/host/libexec/fwrouter" "$(target_path usr/local/libexec/fwrouter)"
  copy_tree "$REPO_ROOT/host/systemd" "$(target_path etc/systemd/system)"
  copy_tree "$REPO_ROOT/host/sysctl.d" "$(target_path etc/sysctl.d)"
  copy_tree "$REPO_ROOT/host/iproute2/rt_tables.d" "$(target_path etc/iproute2/rt_tables.d)"
  install_file "$REPO_ROOT/host/sbin/fwrouter-jobs-retention-dry-run" "$(target_path usr/local/sbin/fwrouter-jobs-retention-dry-run)"
  install_file "$REPO_ROOT/host/sbin/fwrouter-subscription-refresh-job" "$(target_path usr/local/sbin/fwrouter-subscription-refresh-job)"

  for helper in \
    dataplane-common.sh dataplane-check.sh dataplane-apply.sh dataplane-rollback.sh \
    traffic-collect.sh traffic-collect-api.sh fwrouter-boot-preflight.sh \
    fwrouter-wait-port.sh fwrouter-xray-sub-gateway.py host-services.py
  do
    ensure_executable "$(target_path usr/local/libexec/fwrouter/$helper)"
  done
  ensure_executable "$(target_path usr/local/sbin/fwrouter-jobs-retention-dry-run)"
  ensure_executable "$(target_path usr/local/sbin/fwrouter-subscription-refresh-job)"
}

if [ "$TARGET_ROOT" = "/" ] && [ "$INSTALL_HOST_DEPS" != "0" ]; then
  "$REPO_ROOT/installer/install-host-dependencies.sh" --yes
fi

want_component backend && install_backend
want_component ui && install_ui
want_component mihomo && install_mihomo
want_component xray && install_xray
want_component host && install_host
want_component docs && install_docs

if [ -x "$(target_path opt/fwrouter-api/scripts/bootstrap-state.sh)" ]; then
  sh "$(target_path opt/fwrouter-api/scripts/bootstrap-state.sh)" \
    "$(target_path var/lib/fwrouter-v2)" \
    "$(target_path var/log/fwrouter)" \
    "$(target_path run/fwrouter-v2)"
fi

if [ "$TARGET_ROOT" = "/" ] && [ "$SETUP_PYTHON_ENV" != "0" ] && [ -f /opt/fwrouter-api/pyproject.toml ]; then
  /opt/fwrouter-api/scripts/setup-python-env.sh /opt/fwrouter-api
fi

if [ "$TARGET_ROOT" = "/" ] && command -v docker >/dev/null 2>&1; then
  docker network inspect proxy_net >/dev/null 2>&1 || docker network create proxy_net >/dev/null
fi

if [ "$TARGET_ROOT" = "/" ] && [ "$ENABLE_UNITS" != "0" ] && command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload
  systemctl enable fwrouter-mihomo.service fwrouter-xray.service fwrouter-api.service fwrouter-xray-sub-gateway.service
  systemctl enable fwrouter-maintenance.timer fwrouter-subscription-refresh.timer fwrouter-jobs-retention-dry-run.timer fwrouter-traffic-collect.timer
fi

if [ "$TARGET_ROOT" = "/" ] && [ -x /usr/sbin/sysctl ] && [ -f /etc/sysctl.d/99-fwrouter-routing.conf ]; then
  /usr/sbin/sysctl --system >/dev/null
fi

echo "Installed FWRouter components into $TARGET_ROOT:"
echo " $COMPONENTS"

