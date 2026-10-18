# Remaining Ruff Findings After Complex Contract Cleanup

Baseline:386findings in45files, same441tracked maintainedPython scope,Ruff0.17.0F/E9ignore-noqa. Thisisexplicitlyunresolveddebt,notanallowlist. F403/F405zero. No blanketexemptionsareconfigured.

| Source file | Remaining rules/counts | Required evidence before edit |
|---|---:|---|
| `backend/fwrouter_api/adapters/xray.py` | F401 14 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/db/schema_state.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/routes/xray.py` | F401 3 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/apply.py` | F401 6 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/apply_orchestrator_handlers.py` | F401 4 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/apply_orchestrator_subject_handlers.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/control_plane_transfer.py` | F401 28 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/dataplane_nft.py` | F401 36 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/logs.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/logs_retention.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/mihomo_config.py` | F401 57, F811 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/mihomo_config_validation.py` | F401 2 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/mihomo_reconcile.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/mihomo_reconcile_fingerprint.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/provider_managed.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/routing_manifest.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/rules.py` | F401 51 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/rules_state_selective.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/server_global_selection.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/server_inventory.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/server_preferences.py` | F401 2 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/server_state.py` | F401 2 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/server_subject_overrides.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/servers.py` | F401 33 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/subscription_profiles.py` | F401 1, F541 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/system_subjects.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/ui_display_settings.py` | F401 49 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/ui_display_settings_display.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/ui_display_settings_store.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/ui_state.py` | F401 48 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/ui_state_summary.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/xray.py` | F401 9 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/xray_materialize.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/fwrouter_api/services/xray_subscription_service.py` | F401 9, F541 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/tests/test_admin_audit_events.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/tests/test_client_lifecycle.py` | F401 2 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/tests/test_performance_optimizations.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/tests/test_reconcile_core.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/tests/test_stealthsurf_provider_boundary.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/tests/test_subscription_refresh_outcomes.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `backend/tests/test_xray_default_runner_archive.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `installer/tests/test_controlled_backend_lifecycle.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `installer/tests/test_safe_recovery.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `tests/application_acceptance/conftest.py` | F401 1 | Trace public exports, dynamic facade/monkeypatch consumers and initialization side effects; qualify the complete file |
| `tests/application_acceptance/test_xray_worker_crash_l7.py` | F841 1 | Establish required post-delete durable-row assertion; qualified disposable L7 only, NOT RUN in this task |
