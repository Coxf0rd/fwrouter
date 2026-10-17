# Ruff Debt Package 1 — Decision Register

Scope: source review decisions; inventory is the 441 tracked maintained Python files at `main@4f0c43c` plus the bounded uncommitted cleanup. Historical `knowledge/**` snapshots and untracked evidence are excluded. Ruff 0.17.0, `F,E9`, `--ignore-noqa`; current findings are baseline debt except the explicitly resolved findings below.

## Touched-file disposition

- Fifteen changed Python files pass the strict full-file `F,E9` check; `ruff check --config ruff.toml --select F,E9 --ignore-noqa` also passes through the existing `lint-changed` gate and canaries.
- Resolved: 7 F401, 7 F811, 4 F541, and 2 F841. One duplicate-import candidate in `backend/tests/test_xray.py` was withdrawn because its recovery tests are only qualified in a separate hosted profile; this package did not alter it.
- F541 removals were only literal f-strings whose AST constants evaluated identically; no interpolation or escaped-brace behavior changed.
- Removed duplicate imports preserve the first unconditional binding and module initialization order. Test-only standard-library imports were removed only from files otherwise clean under the whole-file rule.

## Remaining finding inventory by file

| File | Remaining codes/counts | Disposition |
|---|---:|---|
| `backend/fwrouter_api/adapters/rules_sources.py` | F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `backend/fwrouter_api/adapters/xray.py` | F401 14 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/adapters/xray_real.py` | F401 2, F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `backend/fwrouter_api/db/schema_state.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/routes/xray.py` | F401 3 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/apply.py` | F401 6 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/apply_orchestrator.py` | F401 106, F403 1 | Defer: star-import facade/consumer bindings; requires explicit export and dynamic-consumer trace before replacement. |
| `backend/fwrouter_api/services/apply_orchestrator_commits.py` | F401 43, F403 1 | Defer: star-import facade/consumer bindings; requires explicit export and dynamic-consumer trace before replacement. |
| `backend/fwrouter_api/services/apply_orchestrator_drift.py` | F401 53, F403 1, F405 5 | Defer: star-import facade/consumer bindings; requires explicit export and dynamic-consumer trace before replacement. |
| `backend/fwrouter_api/services/apply_orchestrator_handlers.py` | F401 4 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/apply_orchestrator_pipeline.py` | F401 51, F403 1 | Defer: star-import facade/consumer bindings; requires explicit export and dynamic-consumer trace before replacement. |
| `backend/fwrouter_api/services/apply_orchestrator_results.py` | F401 49, F403 1, F405 1 | Defer: star-import facade/consumer bindings; requires explicit export and dynamic-consumer trace before replacement. |
| `backend/fwrouter_api/services/apply_orchestrator_state.py` | F401 46, F403 1 | Defer: star-import facade/consumer bindings; requires explicit export and dynamic-consumer trace before replacement. |
| `backend/fwrouter_api/services/apply_orchestrator_subject_handlers.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/control_plane_transfer.py` | F401 28 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/control_plane_transfer_plan.py` | F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `backend/fwrouter_api/services/dataplane_global.py` | F401 1, F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `backend/fwrouter_api/services/dataplane_nft.py` | F401 36 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/dataplane_nft_render.py` | F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `backend/fwrouter_api/services/logs.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/logs_retention.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/mihomo_config.py` | F401 57, F811 1 | Defer: duplicate binding is in a file with other whole-file debt; inspect scope/conditional semantics and qualify the containing file as one package. |
| `backend/fwrouter_api/services/mihomo_config_validation.py` | F401 2 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/mihomo_reconcile.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/mihomo_reconcile_fingerprint.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/provider_managed.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/provider_recovery.py` | F401 1, F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `backend/fwrouter_api/services/routing_manifest.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/rules.py` | F401 51 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/rules_state_selective.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/runtime.py` | F401 3, F841 2 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `backend/fwrouter_api/services/server_global_selection.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/server_inventory.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/server_preferences.py` | F401 2 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/server_state.py` | F401 2 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/server_subject_overrides.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/servers.py` | F401 33 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/subscription.py` | F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `backend/fwrouter_api/services/subscription_pipeline.py` | F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `backend/fwrouter_api/services/subscription_profiles.py` | F401 1, F541 1 | Defer: file also has F401 debt; establish exports/import side effects first, then apply only exact literal-equivalence cleanup. |
| `backend/fwrouter_api/services/system_subjects.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/ui_display_settings.py` | F401 49 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/ui_display_settings_display.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/ui_display_settings_guides.py` | F401 1, F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `backend/fwrouter_api/services/ui_display_settings_store.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/ui_state.py` | F401 48 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/ui_state_clients.py` | F403 1, F405 68, F841 1 | Defer: star-import facade/consumer bindings; requires explicit export and dynamic-consumer trace before replacement. |
| `backend/fwrouter_api/services/ui_state_inventory.py` | F403 1, F405 63, F841 1 | Defer: star-import facade/consumer bindings; requires explicit export and dynamic-consumer trace before replacement. |
| `backend/fwrouter_api/services/ui_state_summary.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/watchdog_failure_state.py` | F841 2 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `backend/fwrouter_api/services/xray.py` | F401 9 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/xray_materialize.py` | F401 1 | Defer: production import may be compatibility surface, dynamic consumer, module side effect, or split-module re-export; trace all consumers before changing. |
| `backend/fwrouter_api/services/xray_subscription_service.py` | F401 9, F541 1 | Defer: file also has F401 debt; establish exports/import side effects first, then apply only exact literal-equivalence cleanup. |
| `backend/tests/test_admin_audit_events.py` | F401 1 | Defer: test imports may be intentional fixture/setup or indicate a missing assertion; only safe when the entire file can pass strict F/E9. |
| `backend/tests/test_client_lifecycle.py` | F401 2 | Defer: test imports may be intentional fixture/setup or indicate a missing assertion; only safe when the entire file can pass strict F/E9. |
| `backend/tests/test_performance_optimizations.py` | F401 1 | Defer: test imports may be intentional fixture/setup or indicate a missing assertion; only safe when the entire file can pass strict F/E9. |
| `backend/tests/test_proof_only_legacy_admission.py` | F401 1, F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `backend/tests/test_provider_managed_integration.py` | F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `backend/tests/test_provider_recovery.py` | F811 1, F841 1 | Defer: duplicate binding is in a file with other whole-file debt; inspect scope/conditional semantics and qualify the containing file as one package. |
| `backend/tests/test_reconcile_core.py` | F401 1 | Defer: test imports may be intentional fixture/setup or indicate a missing assertion; only safe when the entire file can pass strict F/E9. |
| `backend/tests/test_routing_global_wave1.py` | F401 6, F811 1 | Defer: duplicate binding is in a file with other whole-file debt; inspect scope/conditional semantics and qualify the containing file as one package. |
| `backend/tests/test_stealthsurf_provider_boundary.py` | F401 1 | Defer: test imports may be intentional fixture/setup or indicate a missing assertion; only safe when the entire file can pass strict F/E9. |
| `backend/tests/test_subscription_lifecycle.py` | F401 2, F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `backend/tests/test_subscription_refresh_outcomes.py` | F401 1 | Defer: test imports may be intentional fixture/setup or indicate a missing assertion; only safe when the entire file can pass strict F/E9. |
| `backend/tests/test_xray.py` | F811 1 | Defer: duplicate binding is in a file with other whole-file debt; inspect scope/conditional semantics and qualify the containing file as one package. |
| `backend/tests/test_xray_debounce.py` | F841 2 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `backend/tests/test_xray_default_runner_archive.py` | F401 1 | Defer: test imports may be intentional fixture/setup or indicate a missing assertion; only safe when the entire file can pass strict F/E9. |
| `installer/tests/test_controlled_backend_lifecycle.py` | F401 1 | Defer: test imports may be intentional fixture/setup or indicate a missing assertion; only safe when the entire file can pass strict F/E9. |
| `installer/tests/test_safe_recovery.py` | F401 1 | Defer: test imports may be intentional fixture/setup or indicate a missing assertion; only safe when the entire file can pass strict F/E9. |
| `integrations/homeassistant/scripts/fwrouter_action.py` | F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `tests/acceptance/launcher.py` | F401 1, F841 2 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `tests/acceptance/provision.py` | F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `tests/application_acceptance/conftest.py` | F401 1 | Defer: test imports may be intentional fixture/setup or indicate a missing assertion; only safe when the entire file can pass strict F/E9. |
| `tests/application_acceptance/test_core_provider_mihomo.py` | F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `tests/application_acceptance/test_packet_dataplane.py` | F841 2 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `tests/application_acceptance/test_xray_generation.py` | F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `tests/application_acceptance/test_xray_worker_crash_l7.py` | F841 1 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |
| `tests/gates/test_acceptance_contract.py` | F841 2 | Defer: unused binding may reveal omitted state validation/assertion or preserve a call/query result; inspect owner and failure semantics before removal. |

## F841 line-by-line review

Every F841 from the baseline remains explicitly deferred here; the two unused `sqlite3.Error` exception bindings in `routes/system.py` were removed only after checking handlers did not inspect the exception and the full-file check became clean.

| File:line | Binding | Reason deferred |
|---|---|---|
| `backend/fwrouter_api/adapters/rules_sources.py:332` | `settings` | Expression may perform config lookup, path conversion or query/projection work; do not delete invocation without verifying behavior and callers. |
| `backend/fwrouter_api/adapters/xray_real.py:1005` | `existing_by_email` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `backend/fwrouter_api/services/control_plane_transfer_plan.py:141` | `settings_rows` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `backend/fwrouter_api/services/dataplane_global.py:837` | `selective_degraded` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `backend/fwrouter_api/services/dataplane_nft_render.py:136` | `selective_path_kind` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `backend/fwrouter_api/services/provider_recovery.py:174` | `exc` | Exception handler/file has other findings; defer until complete file can be strictly qualified and error behavior is tested. |
| `backend/fwrouter_api/services/runtime.py:257` | `dataplane_generated_path` | Expression may perform config lookup, path conversion or query/projection work; do not delete invocation without verifying behavior and callers. |
| `backend/fwrouter_api/services/runtime.py:258` | `dataplane_manifest_path` | Expression may perform config lookup, path conversion or query/projection work; do not delete invocation without verifying behavior and callers. |
| `backend/fwrouter_api/services/subscription.py:1410` | `provider_handoff` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `backend/fwrouter_api/services/subscription_pipeline.py:868` | `details` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `backend/fwrouter_api/services/ui_display_settings_guides.py:29` | `label` | Expression may perform config lookup, path conversion or query/projection work; do not delete invocation without verifying behavior and callers. |
| `backend/fwrouter_api/services/ui_state_clients.py:204` | `subscription_recent` | Expression may perform config lookup, path conversion or query/projection work; do not delete invocation without verifying behavior and callers. |
| `backend/fwrouter_api/services/ui_state_inventory.py:326` | `subscription_recent` | Expression may perform config lookup, path conversion or query/projection work; do not delete invocation without verifying behavior and callers. |
| `backend/fwrouter_api/services/watchdog_failure_state.py:248` | `recovery_pending` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `backend/fwrouter_api/services/watchdog_failure_state.py:469` | `recovery_pending` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `backend/tests/test_proof_only_legacy_admission.py:2378` | `original_active` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `backend/tests/test_provider_managed_integration.py:83` | `binding` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `backend/tests/test_provider_recovery.py:2001` | `original_refresh` | Monkeypatch/setup variable may document intended original callable or a missing assertion; removing could conceal test coverage defect. |
| `backend/tests/test_subscription_lifecycle.py:2058` | `result` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `backend/tests/test_xray_debounce.py:297` | `marked` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `backend/tests/test_xray_debounce.py:322` | `marked` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `integrations/homeassistant/scripts/fwrouter_action.py:224` | `current` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `tests/acceptance/launcher.py:1251` | `role` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `tests/acceptance/launcher.py:4189` | `samples` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `tests/acceptance/provision.py:140` | `digest_source` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `tests/application_acceptance/test_core_provider_mihomo.py:1035` | `binding` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `tests/application_acceptance/test_packet_dataplane.py:3310` | `supported_again` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `tests/application_acceptance/test_packet_dataplane.py:3364` | `restored_pool_native` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `tests/application_acceptance/test_xray_generation.py:1356` | `source_url` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `tests/application_acceptance/test_xray_worker_crash_l7.py:303` | `rows` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `tests/gates/test_acceptance_contract.py:4672` | `case` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |
| `tests/gates/test_acceptance_contract.py:4688` | `duplicate` | Could be an omitted assertion, state/ownership check, or fixture contract; preserve until the full test/call path is reviewed. |

## Remaining F811 review

| File:line | Name | Disposition |
|---|---|---|
| `backend/fwrouter_api/services/mihomo_config.py:102` | `_runtime_proxy_inventory_count` | Deferred: containing file has other debt and/or test recovery/profile scope is not qualified in this package; no whole-file cleanup here. |
| `backend/tests/test_provider_recovery.py:1002` | `SimpleNamespace` | Deferred: containing file has other debt and/or test recovery/profile scope is not qualified in this package; no whole-file cleanup here. |
| `backend/tests/test_routing_global_wave1.py:10` | `initialize_database` | Deferred: containing file has other debt and/or test recovery/profile scope is not qualified in this package; no whole-file cleanup here. |
| `backend/tests/test_xray.py:44` | `initialize_database` | Deferred: containing file has other debt and/or test recovery/profile scope is not qualified in this package; no whole-file cleanup here. |

## Accounting and limitations

- Baseline: F401 755, F405 137, F841 34, F811 11, F403 8, F541 6; no F821/F822/F823/E9.
- Current: F401 748, F405 137, F841 32, F811 4, F403 8, F541 2; no F821/F822/F823/E9. Total 931 (baseline 951).
- The four Xray rollback nodes were run identically on current and clean source-only baseline using the same exact-node runner and hash-locked CI dependency environment: both runs failed the same assertions (`test_xray.py:2821` and `:2913`). They are not attributed to this cleanup; full qualified hosted CI remains the acceptance gate, and local exact-node failures are reported rather than masked.
- Routine exact-node tests passed 11/11 with exact coverage, sanitized JUnit and cleanup receipts. `tests/gates/test_smoke_contract.py` separately passed 14/14 by its unittest entry point. Other attempts that selected tests requiring host temporary-file or subprocess permissions failed closed in the routine profile; they are not product-PASS claims and must be handled only by the existing qualified hosted profile.
- This cleanup makes no claim that all Ruff debt is safe, that all imports are unused, or that every F841 is a defect. Remaining entries need follow-up ownership and evidence.
