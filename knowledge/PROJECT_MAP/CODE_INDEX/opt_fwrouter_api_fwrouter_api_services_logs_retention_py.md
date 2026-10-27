# `/opt/fwrouter-api/fwrouter_api_services_logs_retention.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_services_logs_retention.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

JSONL cleanup first checks for expired records with bounded memory. A no-expiry
run returns counters without opening/writing a temporary copy. Expired cleanup
rescans with the same cutoff and atomically replaces only if the second pass
still deletes records; dry-run never rewrites. The existing append/replace
coordination limitation on an actual expired rewrite remains unresolved.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.

## Stage 5 retention qualification

Generic cleanup now separates protected audit/security HOLD from ordinary expiry.
JSONL retention/startup scrub coordinates with appenders through the shared
`jsonl_io.py` file lease. See [access/retention contract](../../LOGGING_ACCESS_AND_RETENTION.md);
source qualification does not establish production access or retention capacity.
