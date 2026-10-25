# `/usr/local/libexec/fwrouter/dataplane_rollback.sh`

## Purpose

Restores verified per-apply nft/IP snapshots under the existing Core owner.
Metadata and snapshot identity must validate before mutation; missing/corrupt
metadata is an error, never an implicit previously missing table. Restore uses
captured prior owner-routing facts, not the new manifest's desired contract.
See [Core snapshot recovery](../../CORE_SNAPSHOT_RECOVERY.md).

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
