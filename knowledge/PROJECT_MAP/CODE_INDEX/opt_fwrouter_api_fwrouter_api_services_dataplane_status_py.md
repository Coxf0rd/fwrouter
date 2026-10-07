# `/opt/fwrouter-api/fwrouter_api/services/dataplane_status.py`

## Purpose

Builds the live dataplane status/readback projection used by Health and runtime status paths. It checks the applied manifest against the live owned nftables table, transparent counters, marker parity, and global-mode readback.

## Runtime impact

When an applied manifest exists, the status path passes an empty candidate path and the manifest to `dataplane-check.sh`; it does not repeat candidate `nft -c` validation for already-applied state. Candidate-manifest fallback retains candidate validation when its file exists.

Transparent counters are read from one terse JSON table snapshot (`nft -t -a -nn -j list table inet fwrouter_v2`). The projection preserves substring comment matching and the first matching rule that has a valid counter. If the table snapshot is unavailable or malformed, it falls back to the existing per-chain reads. If a valid snapshot omits or malforms a required chain, only that chain is retried. Failed reads retain the prior zero-counter behavior. This is an observational optimization; it does not alter routing, applied state, or freshness/cache behavior.

## Review notes

- `nft -t` keeps the rules/counters needed by this projection without expanding large set contents.
- Exact old/new projection parity is covered by a same-value deterministic fixture; duplicate comments, missing counters, malformed counters, missing chains, and failed read fallback are covered.
- Current bounded measurement and its limits are in `knowledge/audits/stage4b_completion_2026-10-07/COUNTER_AGGREGATION.md`.

## Guardrails

- Keep FWRouter Core authoritative for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
