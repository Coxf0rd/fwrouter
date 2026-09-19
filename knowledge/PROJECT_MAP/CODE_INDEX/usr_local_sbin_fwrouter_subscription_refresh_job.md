# `/usr/local/sbin/fwrouter-subscription-refresh-job`

## Purpose

Systemd wrapper for periodic verified subscription refresh through the backend job API.

## Review Notes

The timer submits the tracked `subscription_refresh` job with the shared
`subscription_refresh` lock. It waits for the full runtime-verified lifecycle;
it does not run a persistence-only preparation job.
Default wait is 600 seconds through `DEFAULT_WAIT_SECONDS`; the systemd unit
timeout must stay above that value.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
