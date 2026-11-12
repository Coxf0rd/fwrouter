#!/bin/sh
set -eu

REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
TARGET_ROOT="/"
INSTALL_HOST_DEPS="${FWROUTER_INSTALL_HOST_DEPS:-1}"
SETUP_PYTHON_ENV="${FWROUTER_SETUP_PYTHON_ENV:-1}"
ENABLE_UNITS="${FWROUTER_ENABLE_UNITS:-1}"
APPLY_SYSCTL="${FWROUTER_APPLY_SYSCTL:-1}"
CREATE_DOCKER_NETWORK="${FWROUTER_CREATE_DOCKER_NETWORK:-1}"
COMPONENTS=""
DEPLOY_MODE=0
COMPONENT_LOCK_FD=""
MAINTENANCE_LOCK_MODE=""
DEPENDENCIES_PREPARED=0
HOST_PLAN=""
SELECTIVE_HOST=0

usage() {
  cat >&2 <<'EOF'
Usage:
  installer/install.sh --all [--target /]
  installer/install.sh --component backend [--component ui ...] [--target /]
  installer/install.sh --deploy --component backend [--component ui ...]
  installer/install.sh --deploy --component host --host-plan PLAN.json [--target /]

Components: backend, ui, mihomo, xray, host, docs, homeassistant, deploy, all
Options:
  --deploy                         copy selected components to an already prepared host without apt/venv/unit/sysctl setup
  --host-plan PLAN.json            publish only the allowlisted API unit and boot helper via CAS
Environment:
  FWROUTER_INSTALL_HOST_DEPS=0  skip apt dependency install
  FWROUTER_SETUP_PYTHON_ENV=0   skip backend venv setup
  FWROUTER_ENABLE_UNITS=0       skip systemd enable/daemon-reload
  FWROUTER_APPLY_SYSCTL=0       skip sysctl --system
  FWROUTER_CREATE_DOCKER_NETWORK=0 skip managed runtime Docker network creation
  FWROUTER_DOCKER_PROXY_NETWORK network for managed Docker runtimes (default: fwrouter_proxy)
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
    --deploy)
      DEPLOY_MODE=1
      shift
      ;;
    --host-plan)
      [ "$#" -ge 2 ] || usage
      [ -n "$2" ] || usage
      [ -z "$HOST_PLAN" ] || usage
      HOST_PLAN="$2"
      shift 2
      ;;
    --_component-lock-fd)
      [ "$#" -ge 2 ] || usage
      COMPONENT_LOCK_FD="$2"
      shift 2
      ;;
    --_maintenance-lock-mode)
      [ "$#" -ge 2 ] || usage
      MAINTENANCE_LOCK_MODE="$2"
      shift 2
      ;;
    --_dependencies-prepared)
      DEPENDENCIES_PREPARED=1
      shift
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

if [ -n "$HOST_PLAN" ]; then
  [ "$DEPLOY_MODE" = "1" ] || usage
  [ "$COMPONENTS" = " host" ] || usage
  SELECTIVE_HOST=1
fi

if [ "$DEPLOY_MODE" = "1" ]; then
  INSTALL_HOST_DEPS=0
  SETUP_PYTHON_ENV=0
  ENABLE_UNITS=0
  APPLY_SYSCTL=0
  CREATE_DOCKER_NETWORK=0
fi

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
    copy_tmp=$(mktemp -d "${TMPDIR:-/tmp}/fwrouter-copy.XXXXXX") || exit 1
    copy_fifo="$copy_tmp/archive.fifo"
    if ! mkfifo "$copy_fifo"; then
      rmdir "$copy_tmp"
      exit 1
    fi
    copy_producer_pid=""
    copy_cleanup() {
      if [ -n "$copy_producer_pid" ]; then
        kill "$copy_producer_pid" 2>/dev/null || true
        wait "$copy_producer_pid" 2>/dev/null || true
      fi
      rm -f "$copy_fifo"
      rmdir "$copy_tmp" 2>/dev/null || true
    }
    trap 'copy_cleanup' 0
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    (
      cd "$src" || exit 1
      exec tar \
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
    ) > "$copy_fifo" &
    copy_producer_pid=$!

    if (cd "$dst" && tar -xf -) < "$copy_fifo"; then
      copy_receiver_status=0
    else
      copy_receiver_status=$?
    fi
    if wait "$copy_producer_pid"; then
      copy_sender_status=0
    else
      copy_sender_status=$?
    fi
    copy_producer_pid=""
    if [ "$copy_receiver_status" -ne 0 ] || [ "$copy_sender_status" -ne 0 ]; then
      echo "install.sh: component tree copy failed (sender=$copy_sender_status receiver=$copy_receiver_status)" >&2
      exit 1
    fi
  )
}

install_file() {
  src="$1"
  dst="$2"
  require_path "$src" "source file"
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
}

install_deploy_module_atomically() {
  src="$1"
  dst="$2"
  require_path "$src" "deployment updater source"
  dst_dir=$(dirname "$dst")
  mkdir -p "$dst_dir"
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    [ -f "$dst" ] && [ ! -L "$dst" ] \
      && [ "$(stat -c '%u' -- "$dst")" = "$(id -u)" ] \
      && [ "$(stat -c '%h' -- "$dst")" = "1" ] \
      && [ "$(stat -c '%a' -- "$dst")" = "644" ] || {
      echo "install.sh: existing deployment updater is unsafe" >&2
      return 1
    }
  fi
  tmp=$(mktemp "$dst_dir/.local-deploy-updater.XXXXXX") || return 1
  if install -m 0644 "$src" "$tmp" && sync -f "$tmp" && mv -f "$tmp" "$dst" && sync -f "$dst_dir"; then
    return 0
  fi
  rm -f "$tmp"
  return 1
}

install_deploy_wrapper_atomically() {
  src="$1"
  dst="$2"
  require_path "$src" "deployment wrapper source"
  dst_dir=$(dirname "$dst")
  mkdir -p "$dst_dir"
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    [ -f "$dst" ] && [ ! -L "$dst" ] || {
      echo "install.sh: existing deployment wrapper is not a regular file" >&2
      return 1
    }
    [ "$(stat -c '%u' -- "$dst")" = "$(id -u)" ] \
      && [ "$(stat -c '%h' -- "$dst")" = "1" ] \
      && [ "$(stat -c '%a' -- "$dst")" = "755" ] || {
      echo "install.sh: existing deployment wrapper has unsafe owner, links or mode" >&2
      return 1
    }
    if cmp -s "$src" "$dst"; then
      return 0
    fi
    echo "install.sh: refusing to replace a different deployment wrapper" >&2
    return 1
  fi
  tmp=$(mktemp "$dst_dir/.fwrouter-deploy-wrapper.XXXXXX") || return 1
  if install -m 0755 "$src" "$tmp" && sync -f "$tmp" && mv -f "$tmp" "$dst" && sync -f "$dst_dir"; then
    return 0
  fi
  rm -f "$tmp"
  return 1
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

if [ "$DEPENDENCIES_PREPARED" = "1" ] && { [ -z "$COMPONENT_LOCK_FD" ] || { ! want_component backend && ! want_component ui && ! want_component deploy && [ "$SELECTIVE_HOST" != "1" ]; }; }; then
  echo "install.sh: internal lock-runner options require a backend/UI/deploy/selective-host component lock" >&2
  exit 1
fi

want_explicit_component() {
  wanted="$1"
  for component in $COMPONENTS; do
    [ "$component" = "$wanted" ] && return 0
  done
  return 1
}

selected_components() {
  if want_component all; then
    printf '%s\n' backend ui mihomo xray host docs
    return
  fi
  for component in $COMPONENTS; do
    printf '%s\n' "$component"
  done
}

enable_unit_if_installed() {
  unit="$1"
  if [ -f "/etc/systemd/system/$unit" ]; then
    systemctl enable "$unit"
  else
    echo "install.sh: skipping enable for missing unit $unit" >&2
  fi
}

install_backend() {
  copy_tree "$REPO_ROOT/backend" "$(target_path opt/fwrouter-api)"
  while IFS= read -r obsolete; do
    case "$obsolete" in
      ""|\#*) continue ;;
      *) rm -f "$(target_path opt/fwrouter-api)/$obsolete" ;;
    esac
  done < "$REPO_ROOT/installer/obsolete-backend-paths.txt"
  ensure_executable "$(target_path opt/fwrouter-api/scripts/bootstrap-state.sh)"
  ensure_executable "$(target_path opt/fwrouter-api/scripts/check-clean-tree-surface.sh)"
  ensure_executable "$(target_path opt/fwrouter-api/scripts/check_boot_persistence.sh)"
  ensure_executable "$(target_path opt/fwrouter-api/scripts/install-host-dependencies.sh)"
  ensure_executable "$(target_path opt/fwrouter-api/scripts/install-server-tree.sh)"
  ensure_executable "$(target_path opt/fwrouter-api/scripts/setup-python-env.sh)"
}

install_ui() {
  require_path "$REPO_ROOT/ui" "UI source directory"
  require_path "$REPO_ROOT/backend" "backend compatibility source"
  require_path "$(target_path opt/fwrouter-api)" "installed backend for UI compatibility check"
  root_arg=""
  [ "$TARGET_ROOT" != "/" ] || root_arg="--require-root"
  # shellcheck disable=SC2086
  python3 "$REPO_ROOT/installer/ui_publication.py" $root_arg \
    --source "$REPO_ROOT/ui" \
    --target "$(target_path opt/fwrouter-ui)" \
    --backend-source "$REPO_ROOT/backend" \
    --backend-installed "$(target_path opt/fwrouter-api)" \
    --component-lock-fd "$COMPONENT_LOCK_FD"
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

install_homeassistant() {
  install_file \
    "$REPO_ROOT/integrations/homeassistant/packages/fwrouter_control.yaml" \
    "$(target_path app/config/homeassistant/packages/fwrouter_control.yaml)"
  install_file \
    "$REPO_ROOT/integrations/homeassistant/scripts/fwrouter_action.py" \
    "$(target_path app/config/homeassistant/scripts/fwrouter_action.py)"
  ensure_executable "$(target_path app/config/homeassistant/scripts/fwrouter_action.py)"
}

install_deploy_command() {
  module_target="$(target_path usr/local/libexec/fwrouter/local_deploy.py)"
  wrapper_target="$(target_path usr/local/sbin/fwrouter-deploy)"
  wrapper_source="$REPO_ROOT/host/sbin/fwrouter-deploy"
  require_path "$wrapper_source" "deployment wrapper source"
  if [ -e "$module_target" ] || [ -L "$module_target" ]; then
    [ -f "$module_target" ] && [ ! -L "$module_target" ] \
      && [ "$(stat -c '%u' -- "$module_target")" = "$(id -u)" ] \
      && [ "$(stat -c '%h' -- "$module_target")" = "1" ] \
      && [ "$(stat -c '%a' -- "$module_target")" = "644" ] || {
      echo "install.sh: existing deployment updater is unsafe" >&2
      exit 1
    }
  fi
  if [ -e "$wrapper_target" ] || [ -L "$wrapper_target" ]; then
    [ -f "$wrapper_target" ] && [ ! -L "$wrapper_target" ] \
      && [ "$(stat -c '%u' -- "$wrapper_target")" = "$(id -u)" ] \
      && [ "$(stat -c '%h' -- "$wrapper_target")" = "1" ] \
      && [ "$(stat -c '%a' -- "$wrapper_target")" = "755" ] \
      && cmp -s "$wrapper_source" "$wrapper_target" || {
      echo "install.sh: existing deployment wrapper is unsafe or differs from source" >&2
      exit 1
    }
  fi
  install_deploy_module_atomically "$REPO_ROOT/installer/local_deploy.py" "$module_target"
  install_deploy_wrapper_atomically "$wrapper_source" "$wrapper_target"
}

install_host() {
  copy_tree "$REPO_ROOT/host/libexec/fwrouter" "$(target_path usr/local/libexec/fwrouter)"
  copy_tree "$REPO_ROOT/host/systemd" "$(target_path etc/systemd/system)"
  copy_tree "$REPO_ROOT/host/sysctl.d" "$(target_path etc/sysctl.d)"
  copy_tree "$REPO_ROOT/host/iproute2/rt_tables.d" "$(target_path etc/iproute2/rt_tables.d)"
  install_file "$REPO_ROOT/host/sbin/fwrouter-jobs-retention-dry-run" "$(target_path usr/local/sbin/fwrouter-jobs-retention-dry-run)"
  install_file "$REPO_ROOT/host/sbin/fwrouter-subscription-refresh-job" "$(target_path usr/local/sbin/fwrouter-subscription-refresh-job)"

  for helper in \
    dataplane-common.sh dataplane-check.sh dataplane-apply.sh dataplane-rollback.sh dataplane-snapshot-state.py \
    traffic-collect.sh traffic-collect-api.sh fwrouter-boot-preflight.sh \
    fwrouter-wait-port.sh fwrouter-xray-sub-gateway.py host-services.py \
    docker-subject-events.sh
  do
    ensure_executable "$(target_path usr/local/libexec/fwrouter/$helper)"
  done
  ensure_executable "$(target_path usr/local/sbin/fwrouter-jobs-retention-dry-run)"
  ensure_executable "$(target_path usr/local/sbin/fwrouter-subscription-refresh-job)"
}

if [ "$DEPENDENCIES_PREPARED" = "0" ] && [ "$TARGET_ROOT" = "/" ] && [ "$INSTALL_HOST_DEPS" != "0" ]; then
  dep_args="--yes"
  for component in $(selected_components); do
    case "$component" in
      backend|ui|host|mihomo|xray)
        dep_args="$dep_args --component $component"
        ;;
    esac
  done
  if [ "$dep_args" != "--yes" ]; then
    # shellcheck disable=SC2086
    "$REPO_ROOT/installer/install-host-dependencies.sh" $dep_args
  fi
fi

# Backend/UI source operations and selective host publication use the common
# component lock. Selective host deploy also holds the exclusive maintenance
# lease and exits before any generic installer post-actions.
if want_component backend || want_component ui || [ "$SELECTIVE_HOST" = "1" ] \
  || want_explicit_component deploy; then
  if want_component backend; then
    REQUIRED_MAINTENANCE_LOCK_MODE="exclusive"
  elif [ "$SELECTIVE_HOST" = "1" ]; then
    REQUIRED_MAINTENANCE_LOCK_MODE="exclusive"
  else
    REQUIRED_MAINTENANCE_LOCK_MODE="shared"
  fi
  if [ -z "$COMPONENT_LOCK_FD" ]; then
    set --
    for component in $COMPONENTS; do
      set -- "$@" --component "$component"
    done
    [ "$DEPLOY_MODE" = "0" ] || set -- "$@" --deploy
    [ "$TARGET_ROOT" = "/" ] || set -- "$@" --target "$TARGET_ROOT"
    [ "$SELECTIVE_HOST" = "0" ] || set -- "$@" --host-plan "$HOST_PLAN"
    set -- "$@" --_dependencies-prepared
    exec python3 "$REPO_ROOT/installer/component_install_lock.py" \
      --run-parent "$(target_path opt)" -- "$REPO_ROOT/installer/install.sh" "$@"
  fi
  [ "$COMPONENT_LOCK_FD" = "9" ] || {
    echo "install.sh: invalid inherited component lock descriptor" >&2
    exit 1
  }
  [ "$MAINTENANCE_LOCK_MODE" = "$REQUIRED_MAINTENANCE_LOCK_MODE" ] || {
    echo "install.sh: inherited maintenance lease mode does not match selected components" >&2
    exit 1
  }
  python3 "$REPO_ROOT/installer/component_install_lock.py" \
    --verify-parent "$(target_path opt)" --fd "$COMPONENT_LOCK_FD" \
    --mode "$REQUIRED_MAINTENANCE_LOCK_MODE"
fi

if [ "$SELECTIVE_HOST" = "1" ]; then
  python3 "$REPO_ROOT/installer/host_publication.py" \
    --source-root "$REPO_ROOT" \
    --target-root "$TARGET_ROOT" \
    --plan "$HOST_PLAN" \
    --component-lock-fd "$COMPONENT_LOCK_FD"
  echo "Installed FWRouter selective host files into $TARGET_ROOT"
  exit 0
fi

want_component backend && install_backend
want_component ui && install_ui
want_component mihomo && install_mihomo
want_component xray && install_xray
want_component host && install_host
want_component docs && install_docs
want_explicit_component homeassistant && install_homeassistant
want_explicit_component deploy && install_deploy_command

if [ "$COMPONENTS" = " deploy" ]; then
  echo "Installed FWRouter deployment command into $TARGET_ROOT"
  exit 0
fi

if [ -x "$(target_path opt/fwrouter-api/scripts/bootstrap-state.sh)" ]; then
  sh "$(target_path opt/fwrouter-api/scripts/bootstrap-state.sh)" \
    "$(target_path var/lib/fwrouter-v2)" \
    "$(target_path var/log/fwrouter)" \
    "$(target_path run/fwrouter-v2)"
fi

if [ -f "$(target_path opt/fwrouter-api/.env)" ]; then
  chmod 0600 "$(target_path opt/fwrouter-api/.env)"
fi

if [ "$TARGET_ROOT" = "/" ] && [ "$SETUP_PYTHON_ENV" != "0" ] && [ -f /opt/fwrouter-api/pyproject.toml ]; then
  /opt/fwrouter-api/scripts/setup-python-env.sh /opt/fwrouter-api
fi

if [ "$TARGET_ROOT" = "/" ] && [ "$CREATE_DOCKER_NETWORK" != "0" ] && (want_component mihomo || want_component xray) && command -v docker >/dev/null 2>&1; then
  PROXY_NETWORK="${FWROUTER_DOCKER_PROXY_NETWORK:-fwrouter_proxy}"
  docker network inspect "$PROXY_NETWORK" >/dev/null 2>&1 || docker network create "$PROXY_NETWORK" >/dev/null
fi

if [ "$TARGET_ROOT" = "/" ] && [ "$ENABLE_UNITS" != "0" ] && command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload
  want_component backend && enable_unit_if_installed fwrouter-api.service
  if want_component host; then
    enable_unit_if_installed fwrouter-maintenance.timer
    enable_unit_if_installed fwrouter-jobs-retention-dry-run.timer
    enable_unit_if_installed fwrouter-traffic-collect.timer
    if command -v docker >/dev/null 2>&1; then
      enable_unit_if_installed fwrouter-docker-subject-events.service
    else
      echo "install.sh: skipping Docker inventory event service enable; docker command is unavailable" >&2
    fi
  fi
  want_component mihomo && enable_unit_if_installed fwrouter-mihomo.service
  if want_component xray; then
    enable_unit_if_installed fwrouter-xray.service
    enable_unit_if_installed fwrouter-xray-sub-gateway.service
    enable_unit_if_installed fwrouter-subscription-refresh.timer
  fi
fi

if [ "$TARGET_ROOT" = "/" ] && [ "$APPLY_SYSCTL" != "0" ] && [ -x /usr/sbin/sysctl ] && [ -f /etc/sysctl.d/99-fwrouter-routing.conf ]; then
  /usr/sbin/sysctl --system >/dev/null
fi

echo "Installed FWRouter components into $TARGET_ROOT:"
echo " $COMPONENTS"
if [ "$DEPLOY_MODE" = "1" ]; then
  echo " deploy mode: skipped apt dependencies, backend venv setup, unit enable, Docker network creation, and sysctl reload"
fi
