# `/usr/local/sbin/fwrouter_subscription_refresh_job`

## Purpose

Generated code-index entry for `/usr/local/sbin/fwrouter_subscription_refresh_job`.

## Review Notes

The timer submits the tracked `subscription_refresh` job with the shared
`subscription_refresh` lock. It waits for the full runtime-verified lifecycle;
it does not run a persistence-only preparation job.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
