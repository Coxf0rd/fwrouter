#!/bin/sh
set -eu

ASSUME_YES=0
DRY_RUN=0
COMPONENTS=""

usage() {
  echo "Usage: $0 [--yes] [--dry-run] [--component backend|ui|host|mihomo|xray|all]" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    -y|--yes)
      ASSUME_YES=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --component)
      [ "$#" -ge 2 ] || usage
      COMPONENTS="$COMPONENTS $2"
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

[ -n "$COMPONENTS" ] || COMPONENTS="all"

if [ "$(id -u)" -ne 0 ]; then
  echo "install-host-dependencies.sh: must run as root" >&2
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "install-host-dependencies.sh: apt-get not found; install dependencies manually for this distro" >&2
  exit 1
fi

apt_has_package() {
  apt-cache show "$1" >/dev/null 2>&1
}

want_component() {
  wanted="$1"
  for component in $COMPONENTS; do
    case "$component" in
      backend|ui|host|mihomo|xray|all)
        ;;
      *)
        echo "install-host-dependencies.sh: unknown component: $component" >&2
        exit 2
        ;;
    esac
    [ "$component" = "all" ] && return 0
    [ "$component" = "$wanted" ] && return 0
  done
  return 1
}

add_packages() {
  PACKAGES="$PACKAGES $*"
}

PACKAGES=""

BACKEND_PACKAGES="
  ca-certificates
  curl
  jq
  python3
  python3-pip
  python3-venv
  sqlite3
  tar
  zstd
"

HOST_PACKAGES="
  conntrack
  dnsmasq
  dnsutils
  iproute2
  iptables
  kmod
  nftables
  procps
"

RUNTIME_PACKAGES="
  kmod
"

DOCKER_PACKAGES=""
DOCKER_AVAILABLE=0
DOCKER_COMPOSE_AVAILABLE=0

if want_component backend; then
  add_packages $BACKEND_PACKAGES
fi

if want_component host; then
  add_packages $HOST_PACKAGES
fi

if want_component mihomo || want_component xray; then
  add_packages $RUNTIME_PACKAGES
  if command -v docker >/dev/null 2>&1; then
    DOCKER_AVAILABLE=1
    if docker compose version >/dev/null 2>&1; then
      DOCKER_COMPOSE_AVAILABLE=1
    fi
  fi

  if [ "$DOCKER_AVAILABLE" -eq 0 ] && apt_has_package docker.io; then
    DOCKER_PACKAGES="$DOCKER_PACKAGES docker.io"
  fi
  if [ "$DOCKER_COMPOSE_AVAILABLE" -eq 0 ] && apt_has_package docker-compose-plugin; then
    DOCKER_PACKAGES="$DOCKER_PACKAGES docker-compose-plugin"
  elif [ "$DOCKER_COMPOSE_AVAILABLE" -eq 0 ] && apt_has_package docker-compose; then
    DOCKER_PACKAGES="$DOCKER_PACKAGES docker-compose"
  fi
  add_packages $DOCKER_PACKAGES
fi

APT_YES=""
if [ "$ASSUME_YES" -eq 1 ]; then
  APT_YES="-y"
fi

echo "FWRouter host dependencies:"
for package in $PACKAGES; do
  echo "  $package"
done | awk '!seen[$0]++'

if [ -z "$(printf '%s' "$PACKAGES" | tr -d '[:space:]')" ]; then
  echo "No apt packages required for selected components."
  exit 0
fi

if (want_component mihomo || want_component xray) && [ "$DOCKER_AVAILABLE" -eq 0 ] && [ -z "$DOCKER_PACKAGES" ]; then
  echo "WARNING: no Docker package candidate found in apt repositories; install Docker + compose plugin manually" >&2
fi

if [ "$DRY_RUN" -eq 1 ]; then
  exit 0
fi

apt-get update
DEDUPED_PACKAGES="$(printf '%s\n' $PACKAGES | awk '!seen[$0]++')"
# shellcheck disable=SC2086
apt-get install $APT_YES $DEDUPED_PACKAGES

if (want_component mihomo || want_component xray) && ! command -v docker >/dev/null 2>&1; then
  echo "install-host-dependencies.sh: docker command is still missing after apt install" >&2
  exit 1
fi

if (want_component mihomo || want_component xray) && ! docker compose version >/dev/null 2>&1; then
  echo "install-host-dependencies.sh: 'docker compose' plugin is missing" >&2
  exit 1
fi

if (want_component mihomo || want_component xray) && ! [ -c /dev/net/tun ]; then
  mkdir -p /dev/net
  if command -v modprobe >/dev/null 2>&1; then
    modprobe tun >/dev/null 2>&1 || true
  fi
fi

echo "FWRouter host dependencies installed"
