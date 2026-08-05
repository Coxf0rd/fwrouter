# `/opt/fwrouter-api/fwrouter_api/services/rules_jobs.py`

## Purpose

Job-oriented workflow for external rules refresh. It owns fetch summaries, noop detection, full-update orchestration, apply handoff, and active artifact promotion while `rules.py` remains the compatibility facade.

## Key Functions

- `_payload_to_text(...)`
- `_build_fetch_summary(...)`
- `_is_full_update_noop(...)`
- `_try_full_update_version_noop(...)`
- `run_rules_full_update(...)`
- `submit_rules_full_update(...)`
- `apply_manual_rules(...)`

## Behavior Notes

- Full update first validates current manual/static state.
- When stored `rules_pipeline_version`, source URLs, and fetched git source versions match the active metadata, `_try_full_update_version_noop(...)` returns success before downloading large VPN lists.
- If metadata-only version probing is unavailable or stale, the job falls back to full fetch, validation, candidate artifact write, apply, and promotion.
- The later `_is_full_update_noop(...)` still compares normalized active text against normalized fetched text before deciding that already downloaded sources are unchanged.
- A full-fetch normalized-text noop refreshes metadata, including `rules_pipeline_version`, without running dataplane apply.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

Medium/high. Full update affects active rules, effective artifacts, dnsmasq reconcile, Mihomo reconcile, and selective/VPN dataplane convergence.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- Do not skip full fetch unless source versions, source URLs, and `rules_pipeline_version` match active metadata.
