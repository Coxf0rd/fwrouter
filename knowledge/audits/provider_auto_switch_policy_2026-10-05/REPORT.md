# Provider automatic member-switch policy — source and test report

Date: 2026-10-05  
Source baseline: `8e4dd31e99dcf8cc5fb6a69a51e8a17948c7b089`  
Scope: backend policy, schema migration, effective Auto membership persistence, and focused regression evidence.

## Result

The source adds `provider_bindings.allow_automatic_member_switch`, a per-source persistent boolean with a false default. The source configuration endpoint accepts this setting locally and returns it in `subscription.provider_managed.bindings[]`. A policy-only write preserves binding and applied revisions and does not invoke provider APIs, clear probe state, reconcile runtime configuration, or reselect a server.

With the policy disabled, confirmed recovery phase 2 records `provider_auto_switch_disabled` and advances the existing pending recovery without provider status calls, candidate selection, discovery, or PATCH. A later distinct confirmed failure continues through the existing local connectivity check and Emergency Direct path. Automatic provider candidates are excluded when the policy is disabled. Recovery refreshes carry the previously observed member identity and reject different provider material before runtime apply. Explicit manual member switches remain available.

Ordinary servers outside an exclusive Provider pool retain editable configured Auto state. Effective eligibility still uses the existing exclusive-pool projection. If a preference write leaves the effective pool unchanged, Core does not advance its selection revision, invoke reconcile/reselection callbacks, or create the Xray vpn-auto pending marker.

## Validation

All test commands ran from `/srv/fwrouter/backend` with `/opt/fwrouter-api/.venv` and an isolated pytest basetemp. No production database, provider account, runtime, or network was used.

| Level | Command or suites | Result |
|---|---|---|
| L0 | `python3 tests/gates/gate.py validate` | PASS; manifest `2026.10.05.1` covers all 149 test files |
| L0 | `/opt/fwrouter-api/.venv/bin/python -m compileall -q` on changed backend/test Python files; `git diff --check` | PASS |
| L1–L3 | `/opt/fwrouter-api/.venv/bin/pytest -q --basetemp=/tmp/fwrouter-provider-auto-policy-pytest --disable-warnings --maxfail=1 tests/test_provider_configuration.py tests/test_provider_recovery.py tests/test_provider_managed_integration.py tests/test_vpn_auto_exclusive.py` | PASS; 156 tests |
| L1/L3 | Four complementary provider recovery and integration node IDs covering policy change during status probing, transition journal reason, phase-2 to Emergency Direct progression, and zero adapter calls for disabled automatic switch | PASS; 4 tests |
| L5 | `/opt/fwrouter-api/.venv/bin/pytest -q --basetemp=/tmp/fwrouter-provider-auto-policy-l5 --disable-warnings --maxfail=1 tests/test_db_migrations.py tests/test_vpn_auto_exclusive_api.py tests/test_provider_protocol_flow.py` | PASS; 39 tests |
| L1 selector interleavings | `/opt/fwrouter-api/.venv/bin/pytest -q --basetemp=/tmp/fwrouter-provider-auto-policy-interleavings --disable-warnings --maxfail=1 tests/test_vpn_auto_selection_interleavings.py` plus the one failed node rerun listed below | PASS evidence for all 13 nodes; 12 passed on first run, then the stale-revision node passed after fixture opt-in |
| L4 | `/opt/fwrouter-api/.venv/bin/python tests/gates/gate.py smoke --profile isolated` | PASS; temporary backend deploy, schema read, Health HTTP 200, critical-state HTTP 200; provider calls and production mutations disabled |

The migration test verifies schema 23→24 defaults the new flag to false, preserves binding/current/applied member identity, applied revision and credentials, and is idempotent. Existing API evidence tests in the focused provider suites retain the timeout/5xx/429/unknown-not-DOWN assertions.

The migration-anchor run exposed two assertions that enumerated the migration chain only through schema 23. They were updated to include 23→24, after which the complete focused migration/exclusive/provider anchor run passed. The selector interleaving run initially exposed a fixture that relied on the old implicit switch default. It now explicitly opts into switching so that the test continues to exercise its intended stale-selection-revision fence; the changed node passed on rerun. An isolated smoke attempt under system Python could not load TestClient dependencies; the project virtualenv command above passed the same smoke checks.

The exact selector interleaving rerun command was:

```bash
/opt/fwrouter-api/.venv/bin/pytest -q --basetemp=/tmp/fwrouter-provider-auto-policy-interleavings --disable-warnings --maxfail=1 tests/test_vpn_auto_selection_interleavings.py::test_provider_fallback_rejects_selection_revision_changed_during_candidate_probe
```

## Boundaries

Source and focused tests are complete. Across the distinct test nodes above, 212 passed. Commit, production deployment, and live verification are pending separate root authorization and review. L6 and L7 were not run. No production mutation or provider call was attempted.

## UI source acceptance

The targeted Node suites `settings-provider-controls.test.js`, `vpn-auto-exclusive.test.js`, and `admin-server-list-presentation.test.js` passed. A complementary run of the Settings suite verified RU/EN policy audit titles/reasons through the existing typed-event localization path. Configured membership is independent of effective eligibility; excluded ordinary Auto edits issue only Core preferences PATCH, with negative assertions for provider operations, probes and runtime apply. Browser live acceptance remains a separate rollout gate.

## Architectural diff review

The additive schema migration, source-scoped local policy save, early automatic-switch gates, same-member refresh pin, recovery pending CAS and configured/effective projection preserve Core ownership. Manual switch explicitly bypasses only the automatic policy gate; existing mutation/revision/runtime fences remain. Fixed Xray bindings and provider member identity are not rewritten by policy saves. The common writer guard is not extended across provider/network probes. Clean-tree surface and diff whitespace checks passed. Real outage/re-entry and remote mutation race acceptance are not claimed by isolated tests.
