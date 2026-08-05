# `/usr/local/libexec/fwrouter/dataplane_check.sh`

## Purpose

Generated code-index entry for `/usr/local/libexec/fwrouter/dataplane_check.sh`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

Candidate syntax validation mirrors clean apply semantics: when the live owned
table exists, the check prepends `delete table inet fwrouter_v2` to a temporary
validation file before running `nft -c`. This prevents false failures when a
valid candidate changes set attributes that cannot be redefined in-place.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
