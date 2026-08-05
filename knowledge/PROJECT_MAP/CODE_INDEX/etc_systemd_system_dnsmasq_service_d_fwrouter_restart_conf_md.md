# `/etc/systemd/system/dnsmasq.service.d/fwrouter-restart.conf`

## Purpose

Adds FWRouter-owned restart policy to the host `dnsmasq.service`.

## Runtime Impact

`dnsmasq` is the LAN DNS/DHCP dependency for domain-aware selective routing. If
it exits after nftset materialization errors, systemd restarts it after a short
delay instead of leaving LAN DNS down until the next manual or scheduled
reconcile.

## Guardrails

- Keep the drop-in minimal; do not replace the distribution dnsmasq unit.
- Keep restart delay non-zero so repeated configuration failures do not spin
  aggressively.
