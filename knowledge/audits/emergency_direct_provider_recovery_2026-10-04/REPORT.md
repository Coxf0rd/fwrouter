# Emergency Direct and provider recovery re-entry correction

Date: 2026-10-04  
Source baseline: deployed/code baseline `a139e66`; documentation baseline `7ad5fb6`  
Status: source and targeted tests complete; commit, deploy, and live verification are open.

## Root cause and correction

Confirmed source defects were long controller/provider probes in common writer-guard paths, incomplete Core/runtime fencing around re-entry and provider mutations, and provider evidence/outcomes that did not consistently preserve precise freshness, mutation, last-good, and local-apply attribution. The existing phase-2 switch already required explicit provider DOWN/unavailable evidence; this work does not establish that API absence previously caused a remote-DOWN classification or switch. Re-entry now snapshots Core intent, selection revision/pool, eligibility, pending-operation ownership, binding evidence, and runtime incarnation; runs controller probes outside the writer guard; revalidates ownership before local apply/readback; then verifies connectivity outside the guard and commits terminal state with exact pending CAS. The guarded Mihomo check is a bounded native `/proxies` selection read, not full controller health aggregation.

Provider API timeout, unreachable, 5xx, 429, malformed/ambiguous response, explicit UP/DOWN, unknown mutation outcome, local apply failure, and readback failure remain distinct. Missing API response never proves remote DOWN and cannot alone trigger a switch. Explicit DOWN can authorize a switch only with fresh evidence. Ambiguous PATCH is never replayed; at most one read-only confirmation workflow uses the existing adapter and a total budget of at most 2 API requests / 15 seconds. Last-good remains intact on unknown outcomes. Incident counters reset only after confirmed recovery; Emergency Direct retains desired VPN/provider intent and any unresolved provider mutation marker. Direct apply/readback is also fenced against runtime incarnation changes before publishing a verified marker.

Provider HTTP mutations run outside the Core writer guard under the existing provider-job reservation. Fences are checked before and after remote work. Pool/revision values advanced by the operation are adopted only from its guarded local transaction; fresh unrelated state is not adopted. Selector provider fallback now carries the original selection fence across candidate probes.

A deterministic provider handoff defect was fixed by including `location_id` in prepared runtime material. Historical scheduled stale-handoff event attribution remains unproven; the source defect alone does not establish the cause of those prior events.

## Tests by level

All commands were run from `/srv/fwrouter/backend` with the project venv and isolated pytest basetemps. L6 full regression was intentionally not run.

- L0 syntax/import: `/opt/fwrouter-api/.venv/bin/python -m compileall -q fwrouter_api && /opt/fwrouter-api/.venv/bin/python -c 'import fwrouter_api.services.provider_recovery, fwrouter_api.services.provider_managed, fwrouter_api.adapters.mihomo'` — PASS.
- L1 affected unit/interleaving: `/opt/fwrouter-api/.venv/bin/pytest -q --tb=short --basetemp=/tmp/fwrouter-reentry-l1-final7-20261004 tests/test_provider_recovery.py tests/test_provider_managed_integration.py tests/test_stealthsurf_provider_boundary.py tests/test_vpn_auto_selection_interleavings.py tests/test_vpn_auto_exclusive.py` — 162 passed.
- L2 provider contract: `/opt/fwrouter-api/.venv/bin/pytest -q --tb=short --basetemp=/tmp/fwrouter-reentry-l2-final7-20261004 tests/test_provider_protocol_flow.py tests/test_stealthsurf_provider_boundary.py` — 49 passed.
- L3 component/integration: `/opt/fwrouter-api/.venv/bin/pytest -q --tb=short --basetemp=/tmp/fwrouter-reentry-l3-final7-20261004 tests/test_provider_managed_integration.py tests/test_runtime_reconcile_actions.py` — 33 passed.
- L4 recovery/runtime path: `/opt/fwrouter-api/.venv/bin/pytest -q --tb=short --basetemp=/tmp/fwrouter-reentry-l4-final7-20261004 tests/test_xray_generation_recovery.py tests/test_runtime_reconcile_actions.py` — 28 passed.
- L5 shared Core/provider/Mihomo contracts: `/opt/fwrouter-api/.venv/bin/pytest -q --tb=short --basetemp=/tmp/fwrouter-reentry-l5-final7-20261004 tests/test_mihomo_adapter.py tests/test_vpn_auto_selection_interleavings.py tests/test_vpn_auto_selection_state.py tests/test_vpn_auto_exclusive.py tests/test_vpn_auto_writer_guard.py tests/test_xray_generation_recovery.py tests/test_provider_protocol_flow.py` — 106 passed. L5 was necessary because shared Core/provider/Mihomo boundaries changed.
- `git diff --check` — PASS.

Coverage includes API timeout/503/429; explicit DOWN versus unavailable API; freshness expiry after discovery; revision, exclusive intent, runtime incarnation, binding and pending-state changes during pre/post probes; concurrent re-entry claims; reservation contention with one mutation; ambiguous PATCH with one bounded confirmation workflow (at most 2 API requests / 15 seconds) and no replay; owned SQLite pipeline receipt versus competing revision; successful response plus local apply failure attribution; verified re-entry and failed connectivity fallback; runtime change during Direct apply; repeat/idempotent recovery; and handoff location identity.

## Commit, deploy, and live verification

No commit, deploy, restart, provider API call, provider member switch/PATCH, induced Direct activation, or production outage was performed for this source checkpoint. Production live checks remain open: normal operation writer-guard duration, preserved provider/exclusive intent and member, polling volume, runtime/Xray/Mihomo in-sync, subscription refresh, oscillation, and event/evidence projection.

## Remaining limitation

There is no new durable mutation ledger in this milestone. A process crash in the narrow interval after an external PATCH may have been issued and before its outcome marker is persisted cannot be conclusively reconciled from source state alone; the bounded recovery path fails closed and does not blindly repeat the PATCH. This remains a hardening gate. Existing native Xray materialization/validation/reload subprocess deadline hardening is also open; this milestone removes external/controller/provider probes from the shared writer guard but does not claim a complete deadline for a stuck native apply subprocess.
