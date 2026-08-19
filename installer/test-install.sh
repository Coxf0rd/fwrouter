#!/bin/sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
INSTALL_SH="$SCRIPT_DIR/install.sh"
DEPS_SH="$SCRIPT_DIR/install-host-dependencies.sh"

fail() {
  echo "test-install.sh: $*" >&2
  exit 1
}

assert_exists() {
  [ -e "$1" ] || fail "expected path to exist: $1"
}

assert_not_exists() {
  [ ! -e "$1" ] || fail "expected path to be absent: $1"
}

assert_contains() {
  file="$1"
  needle="$2"
  if ! grep -Fq "$needle" "$file"; then
    fail "expected $file to contain: $needle"
  fi
}

assert_not_contains() {
  file="$1"
  needle="$2"
  if grep -Fq "$needle" "$file"; then
    fail "expected $file not to contain: $needle"
  fi
}

make_target() {
  mktemp -d "${TMPDIR:-/tmp}/fwrouter-install-test.XXXXXX"
}

run_install() {
  target="$1"
  shift
  FWROUTER_INSTALL_HOST_DEPS=0 \
  FWROUTER_SETUP_PYTHON_ENV=0 \
  FWROUTER_ENABLE_UNITS=0 \
    "$INSTALL_SH" "$@" --target "$target" >/dev/null
}

run_install_with_repo_root() {
  repo_root="$1"
  shift
  (
    cd "$repo_root"
    "$repo_root/installer/install.sh" "$@"
  ) >/dev/null
}

backend_target="$(make_target)"
run_install "$backend_target" --component backend
assert_exists "$backend_target/opt/fwrouter-api"
assert_not_exists "$backend_target/opt/fwrouter-mihomo"
assert_not_exists "$backend_target/opt/fwrouter-xray"
assert_not_exists "$backend_target/etc/systemd/system/fwrouter-mihomo.service"
assert_not_exists "$backend_target/etc/systemd/system/fwrouter-xray.service"

core_target="$(make_target)"
run_install "$core_target" --component backend --component host
assert_exists "$core_target/opt/fwrouter-api"
assert_exists "$core_target/etc/systemd/system/fwrouter-api.service"
assert_exists "$core_target/usr/local/libexec/fwrouter/dataplane-apply.sh"
assert_not_exists "$core_target/opt/fwrouter-mihomo"
assert_not_exists "$core_target/opt/fwrouter-xray"

mihomo_target="$(make_target)"
run_install "$mihomo_target" --component mihomo
assert_exists "$mihomo_target/opt/fwrouter-mihomo/docker-compose.yml"
assert_not_exists "$mihomo_target/opt/fwrouter-xray"
assert_not_exists "$mihomo_target/opt/fwrouter-api"

xray_target="$(make_target)"
run_install "$xray_target" --component xray
assert_exists "$xray_target/opt/fwrouter-xray/docker-compose.yml"
assert_not_exists "$xray_target/opt/fwrouter-mihomo"
assert_not_exists "$xray_target/opt/fwrouter-api"
assert_contains "$xray_target/opt/fwrouter-xray/docker-compose.yml" 'FWROUTER_DOCKER_PROXY_NETWORK:-fwrouter_proxy'
assert_not_contains "$xray_target/opt/fwrouter-xray/docker-compose.yml" 'proxy_net:'

backend_deps="$(mktemp "${TMPDIR:-/tmp}/fwrouter-backend-deps.XXXXXX")"
"$DEPS_SH" --dry-run --component backend >"$backend_deps"
assert_contains "$backend_deps" 'python3-venv'
assert_contains "$backend_deps" 'sqlite3'
assert_not_contains "$backend_deps" 'docker.io'
assert_not_contains "$backend_deps" 'nftables'

host_deps="$(mktemp "${TMPDIR:-/tmp}/fwrouter-host-deps.XXXXXX")"
"$DEPS_SH" --dry-run --component host >"$host_deps"
assert_contains "$host_deps" 'nftables'
assert_contains "$host_deps" 'iproute2'
assert_contains "$host_deps" 'dnsmasq'
assert_not_contains "$host_deps" 'python3-venv'

runtime_deps="$(mktemp "${TMPDIR:-/tmp}/fwrouter-runtime-deps.XXXXXX")"
"$DEPS_SH" --dry-run --component xray >"$runtime_deps"
assert_contains "$runtime_deps" 'kmod'
if ! command -v docker >/dev/null 2>&1; then
  assert_contains "$runtime_deps" 'docker'
fi

deploy_target="$(make_target)"
deploy_repo="$(make_target)"
(
  cd "$SCRIPT_DIR/.."
  tar \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    -cf - .
) | (
  cd "$deploy_repo"
  tar -xf -
)
cat >"$deploy_repo/installer/install-host-dependencies.sh" <<'EOF'
#!/bin/sh
echo "dependency installer must not run in deploy mode" >&2
exit 42
EOF
chmod 0755 "$deploy_repo/installer/install-host-dependencies.sh"
run_install_with_repo_root "$deploy_repo" --deploy --component backend --component ui --target "$deploy_target"
assert_exists "$deploy_target/opt/fwrouter-api"
assert_exists "$deploy_target/opt/fwrouter-ui"
assert_not_exists "$deploy_target/opt/fwrouter-mihomo"

echo "installer tests passed"
