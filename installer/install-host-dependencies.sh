#!/bin/sh
set -eu

ASSUME_YES=0
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    -y|--yes)
      ASSUME_YES=1
      ;;
    --dry-run)
      DRY_RUN=1
      ;;
    *)
      echo "Usage: $0 [--yes] [--dry-run]" >&2
      exit 2
      ;;
  esac
done

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

BASE_PACKAGES="
  ca-certificates
  conntrack
  curl
  dnsmasq
  dnsutils
  iproute2
  iptables
  jq
  kmod
  nftables
  procps
  python3
  python3-pip
  python3-venv
  sqlite3
  tar
  zstd
"

DOCKER_PACKAGES=""
if apt_has_package docker.io; then
  DOCKER_PACKAGES="$DOCKER_PACKAGES docker.io"
fi
if apt_has_package docker-compose-plugin; then
  DOCKER_PACKAGES="$DOCKER_PACKAGES docker-compose-plugin"
elif apt_has_package docker-compose; then
  DOCKER_PACKAGES="$DOCKER_PACKAGES docker-compose"
fi

PACKAGES="$BASE_PACKAGES $DOCKER_PACKAGES"
APT_YES=""
if [ "$ASSUME_YES" -eq 1 ]; then
  APT_YES="-y"
fi

echo "FWRouter host dependencies:"
for package in $PACKAGES; do
  echo "  $package"
done

if [ -z "$DOCKER_PACKAGES" ]; then
  echo "WARNING: no Docker package candidate found in apt repositories; install Docker + compose plugin manually" >&2
fi

if [ "$DRY_RUN" -eq 1 ]; then
  exit 0
fi

apt-get update
# shellcheck disable=SC2086
apt-get install $APT_YES $PACKAGES

if ! command -v docker >/dev/null 2>&1; then
  echo "install-host-dependencies.sh: docker command is still missing after apt install" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "install-host-dependencies.sh: 'docker compose' plugin is missing" >&2
  exit 1
fi

if ! [ -c /dev/net/tun ]; then
  mkdir -p /dev/net
  if command -v modprobe >/dev/null 2>&1; then
    modprobe tun >/dev/null 2>&1 || true
  fi
fi

echo "FWRouter host dependencies installed"

