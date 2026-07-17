#!/bin/sh
set -eu

report() {
    title="$1"
    shift
    printf '\n== %s ==\n' "$title"
    "$@" 2>&1 || true
}

echo "FWRouter boot persistence check"
echo "Timestamp: $(date -Is)"

report "Systemd unit files" systemctl list-unit-files --type=service --type=timer
report "FWRouter service status" systemctl status --no-pager fwrouter-mihomo.service fwrouter-xray.service fwrouter-api.service fwrouter-xray-sub-gateway.service
report "Enabled flags" systemctl is-enabled fwrouter-mihomo.service fwrouter-xray.service fwrouter-api.service fwrouter-xray-sub-gateway.service fwrouter-maintenance.timer fwrouter-subscription-refresh.timer fwrouter-traffic-collect.timer
report "Active flags" systemctl is-active fwrouter-mihomo.service fwrouter-xray.service fwrouter-api.service fwrouter-xray-sub-gateway.service docker.service dnsmasq.service netfilter-persistent.service
report "Unit files on disk" ls -l /etc/systemd/system/fwrouter-api.service /etc/systemd/system/fwrouter-mihomo.service /etc/systemd/system/fwrouter-xray.service /etc/systemd/system/fwrouter-xray-sub-gateway.service
report "nft ruleset" nft list ruleset
report "ip rules" ip rule show
report "ip routes (table all)" ip route show table all
report "rt_tables.d" sh -c 'for f in /etc/iproute2/rt_tables.d/*; do [ -f "$f" ] || continue; echo "--- $f"; cat "$f"; done'
report "sysctl routing values" sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding net.ipv4.conf.all.rp_filter net.ipv4.conf.default.rp_filter net.ipv4.conf.all.src_valid_mark
report "tun device" ls -l /dev/net/tun
report "listening ports" ss -ltnup
report "docker containers" docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
report "Recent systemd errors" journalctl -b -p err --no-pager -n 100
