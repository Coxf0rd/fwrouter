#!/bin/sh
set -eu

# 1. Runtime config discovery
MIHOMO_CONFIG="${FWROUTER_MIHOMO_CONFIG:-/var/lib/fwrouter-v2/generated/mihomo/config.yaml}"
XRAY_CONFIG="${FWROUTER_XRAY_CONFIG:-/var/lib/fwrouter-v2/xray/config.json}"
XRAY_CONTAINER_NAME="${FWROUTER_XRAY_CONTAINER_NAME:-fwrouter-xray}"
MIHOMO_API=""
MIHOMO_SECRET=""

if [ -f "$MIHOMO_CONFIG" ]; then
    MIHOMO_SECRET="$(grep '^secret:' "$MIHOMO_CONFIG" | awk '{print $2}')"
    CONTROLLER="$(grep '^external-controller:' "$MIHOMO_CONFIG" | awk '{print $2}')"
    if [ -n "$CONTROLLER" ]; then
        MIHOMO_API="$(echo "$CONTROLLER" | sed 's/0\.0\.0\.0/127.0.0.1/')"
    fi
fi

# 2. NFTables processing
TABLE_OUTPUT=""
if command -v nft >/dev/null 2>&1; then
    TABLE_OUTPUT="$(nft list table inet fwrouter_v2 2>/dev/null || true)"
fi

extract_bytes() {
    PATTERN="$1"
    printf '%s\n' "$TABLE_OUTPUT" \
        | sed -n "s/.*counter packets [0-9][0-9]* bytes \\([0-9][0-9]*\\).*$PATTERN.*/\\1/p" \
        | head -n1
}

DIRECT_BYTES="$(extract_bytes 'comment \"global direct path\"')"
VPN_BYTES="$(extract_bytes 'comment \"fwrouter vpn mark tcp:')"
[ -n "${VPN_BYTES:-}" ] || VPN_BYTES="$(extract_bytes 'comment \"fwrouter vpn mark udp:')"
[ -n "${VPN_BYTES:-}" ] || VPN_BYTES="$(extract_bytes 'comment \"fwrouter global vpn mark:')"
[ -n "${VPN_BYTES:-}" ] || VPN_BYTES="$(extract_bytes 'comment \"fwrouter global vpn tproxy:')"

[ -n "${DIRECT_BYTES:-}" ] || DIRECT_BYTES=0
[ -n "${VPN_BYTES:-}" ] || VPN_BYTES=0

# Create temporary file for counters
TMP_FILE=$(mktemp)

# Legacy global counters
cat <<EOC > "$TMP_FILE"
{"counter_key":"fwrouter:global:direct","subject_id":"fwrouter:global","path":"direct","rx_bytes":$DIRECT_BYTES,"tx_bytes":0,"metadata":{"source":"nftables","scope":"global"}}
{"counter_key":"fwrouter:global:vpn","subject_id":"fwrouter:global","path":"vpn","rx_bytes":$VPN_BYTES,"tx_bytes":0,"metadata":{"source":"nftables","scope":"global"}}
EOC

# Mihomo global counters
if [ -n "$MIHOMO_API" ]; then
    MIHOMO_STATS=$(curl -s -f -H "Authorization: Bearer $MIHOMO_SECRET" "http://$MIHOMO_API/connections" 2>/dev/null) || MIHOMO_STATS=""
    if [ -n "$MIHOMO_STATS" ]; then
        DOWNLOAD=$(echo "$MIHOMO_STATS" | jq -r '.downloadTotal // 0')
        UPLOAD=$(echo "$MIHOMO_STATS" | jq -r '.uploadTotal // 0')
        if [ "$DOWNLOAD" -gt 0 ] || [ "$UPLOAD" -gt 0 ]; then
             echo "{\"counter_key\":\"mihomo:global\",\"subject_id\":\"fwrouter:global\",\"path\":\"vpn\",\"rx_bytes\":$DOWNLOAD,\"tx_bytes\":$UPLOAD,\"metadata\":{\"source\":\"mihomo\"}}" >> "$TMP_FILE"
        fi
    fi
fi

# Xray per-client counters via runtime stats API
if command -v docker >/dev/null 2>&1 && command -v jq >/dev/null 2>&1 && [ -f "$XRAY_CONFIG" ]; then
    XRAY_API_SERVER="$(
        jq -r '
            (.api.tag // "fwrouter-api") as $api_tag
            | (
                [
                    .inbounds[]?
                    | select((.tag // "") == $api_tag)
                    | "\((.listen // "127.0.0.1")):\(.port // 10085)"
                ][0]
            ) // "127.0.0.1:10085"
        ' "$XRAY_CONFIG" 2>/dev/null || true
    )"

    XRAY_BINDINGS="$(
        jq -r '
            .inbounds[]?
            | select((.protocol // "") == "vless")
            | .settings.clients[]?
            | select((.email // "") != "" and (.id // "") != "")
            | .id as $client_id
            | [.email, ((.fwrouterBinding.subject_id // "") | if . != "" then . else ("xray:" + $client_id) end)]
            | @tsv
        ' "$XRAY_CONFIG" 2>/dev/null || true
    )"

    if [ -n "$XRAY_BINDINGS" ]; then
        XRAY_STATS="$(docker exec "$XRAY_CONTAINER_NAME" xray api statsquery --server="$XRAY_API_SERVER" -pattern "user>>>" 2>/dev/null || true)"
        if [ -n "$XRAY_STATS" ]; then
            XRAY_STATS_FILE=$(mktemp)
            printf '%s\n' "$XRAY_STATS" \
                | jq -r '
                    (.stat // .stats // [])[]?
                    | select((.name // "") | startswith("user>>>"))
                    | [.name, ((.value // 0) | tonumber)]
                    | @tsv
                ' > "$XRAY_STATS_FILE" 2>/dev/null || true

            printf '%s\n' "$XRAY_BINDINGS" | while IFS="$(printf '\t')" read -r EMAIL SUBJECT_ID; do
                [ -n "${EMAIL:-}" ] || continue
                [ -n "${SUBJECT_ID:-}" ] || continue

                RX_BYTES="$(awk -F '\t' -v key="user>>>$EMAIL>>>traffic>>>downlink" '$1 == key { print $2; exit }' "$XRAY_STATS_FILE")"
                TX_BYTES="$(awk -F '\t' -v key="user>>>$EMAIL>>>traffic>>>uplink" '$1 == key { print $2; exit }' "$XRAY_STATS_FILE")"
                [ -n "${RX_BYTES:-}" ] || RX_BYTES=0
                [ -n "${TX_BYTES:-}" ] || TX_BYTES=0

                if [ "$RX_BYTES" -gt 0 ] || [ "$TX_BYTES" -gt 0 ]; then
                    jq -cn \
                        --arg counter_key "xray:subject:$SUBJECT_ID" \
                        --arg subject_id "$SUBJECT_ID" \
                        --arg email "$EMAIL" \
                        --argjson rx_bytes "$RX_BYTES" \
                        --argjson tx_bytes "$TX_BYTES" \
                        '{
                            counter_key: $counter_key,
                            subject_id: $subject_id,
                            path: "vpn",
                            rx_bytes: $rx_bytes,
                            tx_bytes: $tx_bytes,
                            metadata: {
                                source: "xray_api",
                                scope: "client",
                                email: $email
                            }
                        }' >> "$TMP_FILE"
                fi
            done

            rm -f "$XRAY_STATS_FILE"
        fi
    fi
fi

# Named counters cnt_*
if command -v nft >/dev/null 2>&1; then
    nft -j list counters 2>/dev/null | jq -c '
        .nftables[]
        | select(.counter != null and (.counter.name | startswith("cnt_")))
        | .counter
        | {
            counter_key: ("nft:counter:" + .name),
            rx_bytes: (.bytes // 0),
            tx_bytes: 0,
            metadata: {
                source: "nftables",
                name: .name
            }
        }
    ' >> "$TMP_FILE"
fi

# Output as JSON
echo "{\"counters\":["
sed 's/$/,/' "$TMP_FILE" | sed '$ s/,$//'
echo "]}"

rm "$TMP_FILE"
