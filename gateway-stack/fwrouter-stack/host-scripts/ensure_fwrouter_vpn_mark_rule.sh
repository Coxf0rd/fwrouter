#!/usr/bin/env bash
set -euo pipefail

# Ensure fwmark-based VPN routing has higher priority than the blanket
# "iif enp2s0 lookup main" rule. Without this, UDP/QUIC (e.g. x.com) from LAN
# may bypass VPN classification even when packets are correctly marked.

MARK="0x40000/0xff0000"
TABLE="2022"
PREF="100"

if ip rule show | grep -qE "^[0-9]+:.*fwmark ${MARK}.*lookup ${TABLE}\\b"; then
  # Rule exists; ensure there is one with higher priority than 150.
  # If the existing rule has pref < 150 we're good; otherwise add ours.
  if ip rule show | awk -v mark="$MARK" -v tbl="$TABLE" '
    $0 ~ ("fwmark " mark) && $0 ~ ("lookup " tbl) {
      split($1, a, ":"); pref=a[1]+0; if (pref>0 && pref<150) { ok=1 }
    }
    END { exit(ok?0:1) }
  '; then
    exit 0
  fi
fi

# Add (or re-add) the higher-priority rule. "File exists" is fine.
ip rule add pref "${PREF}" fwmark "${MARK}" lookup "${TABLE}" 2>/dev/null || true

