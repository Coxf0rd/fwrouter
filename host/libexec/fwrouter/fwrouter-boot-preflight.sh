#!/bin/sh
set -eu

STATE_ROOT="${FWROUTER_STATE_ROOT:-/var/lib/fwrouter-v2}"
LOG_ROOT="${FWROUTER_LOG_ROOT:-/var/log/fwrouter}"
RUN_ROOT="${FWROUTER_RUN_ROOT:-/run/fwrouter-v2}"
SYSCTL_FILE="/etc/sysctl.d/99-fwrouter-routing.conf"
RT_TABLES_DIR="/etc/iproute2/rt_tables.d"
RT_TABLES_FILE="$RT_TABLES_DIR/fwrouter.conf"

if [ ! -c /dev/net/tun ]; then
    echo "fwrouter preflight: /dev/net/tun is not available" >&2
    exit 1
fi

mkdir -p "$STATE_ROOT" "$LOG_ROOT" "$RUN_ROOT"
/opt/fwrouter-api/scripts/bootstrap-state.sh "$STATE_ROOT" "$LOG_ROOT" "$RUN_ROOT" >/dev/null

mkdir -p "$RT_TABLES_DIR"
cat >"$RT_TABLES_FILE" <<'EOF'
100 fwrouter_vpn
EOF

if [ -f "$SYSCTL_FILE" ] && command -v sysctl >/dev/null 2>&1; then
    sysctl --system >/dev/null
fi

if ! command -v nft >/dev/null 2>&1; then
    echo "fwrouter preflight: nft command is not available" >&2
    exit 1
fi

if ! command -v ip >/dev/null 2>&1; then
    echo "fwrouter preflight: ip command is not available" >&2
    exit 1
fi
