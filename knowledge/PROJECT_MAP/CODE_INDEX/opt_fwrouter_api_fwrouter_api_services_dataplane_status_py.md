# `/opt/fwrouter-api/fwrouter_api_services_dataplane_status.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_services_dataplane_status.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- With an applied manifest, runtime status passes an empty candidate path and the manifest to `dataplane-check.sh`; this avoids repeating `nft -c` validation of an already-applied file. Candidate-manifest fallback still passes an existing `candidate.nft` for validation; if that file is missing, it retains the existing empty-path behavior.
- Applied status continues live owned-table/required-chain/policy-routing checks, transparent counter reads, applied-marker parity and global-mode readback. This change skips candidate syntax validation only; it does not weaken live evidence or freshness/cache behavior.
- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
