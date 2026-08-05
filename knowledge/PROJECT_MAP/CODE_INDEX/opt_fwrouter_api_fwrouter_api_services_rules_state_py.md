# `/opt/fwrouter-api/fwrouter_api/services/rules_state.py`

## Purpose

Persistent state, active/candidate artifact paths, metadata files, and `rules_state` / `rules_metadata` rows for the rules subsystem.

## Key Functions

- `get_rules_state()`
- `get_manual_rules_texts()`
- `write_rules_candidate(...)`
- `write_active_rules_state(...)`
- `update_rules_metadata_records(...)`
- `mark_rules_metadata_update_failed(...)`
- `mark_rules_job_running(...)`
- `mark_rules_job_failed(...)`
- `mark_rules_job_success(...)`
- `get_rules_overview()`
- `get_rules_summary()`

## Behavior Notes

- Active rules metadata includes source versions, source URLs, fetch summaries, counts, and `rules_pipeline_version`.
- `rules_pipeline_version` is part of safe version-only noop detection: a pipeline change forces a full fetch/rebuild once even if upstream Git commit is unchanged.
- Failed full updates preserve last-good active metadata and record the failure in `last_error_*`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

Medium. This layer is source-of-truth storage for active rules paths and metadata.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- Do not let failed refresh candidates replace last-good active counts, source versions, or fetch metadata.
