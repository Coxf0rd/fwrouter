# Code Index

Здесь лежат точечные описания ключевых файлов проекта. Индекс покрывает файлы, которые влияют на boot persistence, dataplane, generated configs, system orchestration и API entrypoints.

Рекомендуемый порядок чтения:

1. `opt_fwrouter_api_fwrouter_api_main_py.md`
2. `opt_fwrouter_api_fwrouter_api_services_bootstrap_py.md`
3. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_py.md`
4. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_constants_py.md`
5. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_jobs_py.md`
6. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_results_py.md`
7. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_state_py.md`
8. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_drift_py.md`
9. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_pipeline_py.md`
10. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_commits_py.md`
11. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_public_py.md`
12. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_handler_common_py.md`
13. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_global_handlers_py.md`
14. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_subject_handlers_py.md`
15. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_server_handlers_py.md`
16. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_rules_handlers_py.md`
17. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_dispatch_py.md`
18. `opt_fwrouter_api_fwrouter_api_services_apply_py.md`
19. `opt_fwrouter_api_fwrouter_api_services_apply_context_py.md`
20. `opt_fwrouter_api_fwrouter_api_services_apply_results_py.md`
21. `opt_fwrouter_api_fwrouter_api_services_apply_plan_py.md`
22. `opt_fwrouter_api_fwrouter_api_services_apply_manifest_py.md`
23. `opt_fwrouter_api_fwrouter_api_services_apply_hot_swap_py.md`
22. `opt_fwrouter_api_fwrouter_api_services_maintenance_py.md`
23. `opt_fwrouter_api_fwrouter_api_services_runtime_convergence_py.md`
24. `opt_fwrouter_api_fwrouter_api_services_runtime_convergence_scheduler_py.md`
25. `opt_fwrouter_api_fwrouter_api_services_apply_versions_retention_py.md`
26. `opt_fwrouter_api_fwrouter_api_services_jobs_retention_py.md`
27. `opt_fwrouter_api_fwrouter_api_services_state_retention_py.md`
28. `opt_fwrouter_api_fwrouter_api_services_dataplane_global_py.md`
29. `opt_fwrouter_api_fwrouter_api_services_dataplane_nft_py.md`
30. `opt_fwrouter_api_fwrouter_api_services_global_mode_profiles_py.md`
31. `opt_fwrouter_api_fwrouter_api_services_mihomo_config_py.md`
32. `opt_fwrouter_api_fwrouter_api_services_mihomo_config_rules_py.md`
33. `opt_fwrouter_api_fwrouter_api_services_mihomo_config_proxies_py.md`
34. `opt_fwrouter_api_fwrouter_api_services_mihomo_config_status_py.md`
35. `opt_fwrouter_api_fwrouter_api_services_mihomo_config_validation_py.md`
33. `opt_fwrouter_api_fwrouter_api_services_mihomo_reconcile_py.md`
34. `opt_fwrouter_api_fwrouter_api_services_mihomo_reconcile_fingerprint_py.md`
35. `opt_fwrouter_api_fwrouter_api_adapters_mihomo_py.md`
35. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_handlers_py.md`
36. `opt_fwrouter_api_fwrouter_api_services_rules_py.md`
37. `opt_fwrouter_api_fwrouter_api_services_rules_state_py.md`
38. `opt_fwrouter_api_fwrouter_api_services_rules_state_store_py.md`
39. `opt_fwrouter_api_fwrouter_api_services_rules_state_selective_py.md`
40. `opt_fwrouter_api_fwrouter_api_services_rules_state_files_py.md`
41. `opt_fwrouter_api_fwrouter_api_services_rules_state_metadata_py.md`
42. `opt_fwrouter_api_fwrouter_api_services_rules_state_readmodel_py.md`
43. `opt_fwrouter_api_fwrouter_api_services_rules_compile_py.md`
44. `opt_fwrouter_api_fwrouter_api_services_rules_artifacts_py.md`
45. `opt_fwrouter_api_fwrouter_api_services_rules_jobs_py.md`
46. `usr_local_libexec_fwrouter_dataplane_apply_sh.md`
47. `etc_systemd_system_fwrouter_api_service_md.md`
48. `opt_fwrouter_api_fwrouter_api_services_runtime_py.md`
49. `opt_fwrouter_api_fwrouter_api_services_subject_policy_py.md`
50. `opt_fwrouter_api_fwrouter_api_services_scoped_egress_py.md`
51. `opt_fwrouter_api_fwrouter_api_services_servers_py.md`
52. `opt_fwrouter_api_fwrouter_api_services_server_inventory_py.md`
53. `opt_fwrouter_api_fwrouter_api_services_server_state_py.md`
54. `opt_fwrouter_api_fwrouter_api_services_server_global_selection_py.md`
55. `opt_fwrouter_api_fwrouter_api_services_server_subject_overrides_py.md`
56. `opt_fwrouter_api_fwrouter_api_services_server_preferences_py.md`
57. `opt_fwrouter_api_fwrouter_api_db_connection_py.md`
58. `opt_fwrouter_api_fwrouter_api_jobs_manager_py.md`
59. `opt_fwrouter_api_fwrouter_api_services_control_plane_transfer_py.md`
60. `opt_fwrouter_api_fwrouter_api_services_control_plane_transfer_common_py.md`
61. `opt_fwrouter_api_fwrouter_api_services_control_plane_transfer_export_py.md`
62. `opt_fwrouter_api_fwrouter_api_services_control_plane_transfer_source_py.md`
63. `opt_fwrouter_api_fwrouter_api_services_control_plane_transfer_validation_py.md`
64. `opt_fwrouter_api_fwrouter_api_services_control_plane_transfer_plan_py.md`
65. `opt_fwrouter_api_fwrouter_api_services_control_plane_transfer_import_py.md`
66. `opt_fwrouter_api_fwrouter_api_routes_system_py.md`
67. `opt_fwrouter_api_fwrouter_api_routes_servers_py.md`
68. `opt_fwrouter_api_fwrouter_api_routes_rules_py.md`
69. `opt_fwrouter_api_fwrouter_api_routes_xray_py.md`
70. `opt_fwrouter_api_fwrouter_api_services_xray_bindings_py.md`
71. `opt_fwrouter_api_fwrouter_api_services_xray_client_state_py.md`
72. `opt_fwrouter_api_fwrouter_api_services_xray_status_py.md`
73. `opt_fwrouter_api_fwrouter_api_services_xray_runtime_state_py.md`
74. `opt_fwrouter_api_fwrouter_api_services_xray_common_py.md`
75. `opt_fwrouter_api_fwrouter_api_services_xray_clients_py.md`
76. `opt_fwrouter_api_fwrouter_api_services_xray_materialize_py.md`
77. `opt_fwrouter_api_fwrouter_api_services_xray_subscription_service_py.md`
78. `opt_fwrouter_api_fwrouter_api_services_live_probe_cache_py.md`
79. `opt_fwrouter_api_fwrouter_api_services_ui_state_py.md`
80. `opt_fwrouter_api_fwrouter_api_services_ui_state_common_py.md`
81. `opt_fwrouter_api_fwrouter_api_services_ui_state_settings_py.md`
82. `opt_fwrouter_api_fwrouter_api_services_ui_state_clients_py.md`
83. `opt_fwrouter_api_fwrouter_api_services_ui_state_inventory_py.md`
84. `opt_fwrouter_api_fwrouter_api_services_ui_state_summary_py.md`
85. `opt_fwrouter_api_fwrouter_api_services_ui_display_settings_py.md`
86. `opt_fwrouter_api_fwrouter_api_services_ui_display_settings_common_py.md`
87. `opt_fwrouter_api_fwrouter_api_services_ui_display_settings_store_py.md`
88. `opt_fwrouter_api_fwrouter_api_services_ui_display_settings_display_py.md`
89. `opt_fwrouter_api_fwrouter_api_services_ui_display_settings_guides_py.md`
90. `opt_fwrouter_api_fwrouter_api_services_ui_display_settings_external_py.md`
91. `opt_fwrouter_api_fwrouter_api_services_ui_text_py.md`
92. `opt_fwrouter_api_fwrouter_api_services_ui_state_logs_py.md`
93. `opt_fwrouter_api_fwrouter_api_services_subject_groups_py.md`
94. `opt_fwrouter_api_fwrouter_api_services_watchdog_runtime_state_py.md`
95. `opt_fwrouter_api_fwrouter_api_services_watchdog_traffic_signal_py.md`
96. `opt_fwrouter_api_fwrouter_api_services_watchdog_active_quality_py.md`
97. `opt_fwrouter_api_fwrouter_api_services_watchdog_status_py.md`
98. `opt_fwrouter_api_fwrouter_api_services_watchdog_failure_state_py.md`
99. `opt_fwrouter_api_fwrouter_api_services_watchdog_decision_logs_py.md`
100. `opt_fwrouter_api_fwrouter_api_services_watchdog_result_helpers_py.md`
101. `opt_fwrouter_api_fwrouter_api_services_watchdog_scheduler_py.md`
102. `opt_fwrouter_api_fwrouter_api_services_watchdog_flow_deps_py.md`
103. `opt_fwrouter_api_fwrouter_api_services_watchdog_manual_flow_py.md`
104. `opt_fwrouter_api_fwrouter_api_services_watchdog_auto_flow_py.md`
105. `opt_fwrouter_api_fwrouter_api_services_watchdog_auto_active_quality_flow_py.md`
106. `opt_fwrouter_api_fwrouter_api_services_watchdog_auto_stall_flow_py.md`
107. `opt_fwrouter_api_fwrouter_api_services_watchdog_flows_py.md`
108. `opt_fwrouter_ui_static_js_mode_switching_md.md`

## Быстрая карта доменов

- startup/boot: `main.py`, `bootstrap.py`, `runtime_prewarm.py`, `maintenance_scheduler.py`, `runtime_convergence_scheduler.py`, systemd unit docs
- apply/dataplane: `apply_orchestrator.py` facade, `apply_orchestrator_*`, `apply.py`, `apply_context.py`, `apply_results.py`, `apply_plan.py`, `apply_manifest.py`, `apply_hot_swap.py`, `dataplane_*.py`, `adapters/dataplane.py`, libexec `dataplane-*.sh`
- policy/routing: `subject_policy.py`, `scoped_egress.py`, `servers.py` facade, `server_state.py`, `server_global_selection.py`, `server_subject_overrides.py`, `server_preferences.py`, `routing_manifest.py`, `dataplane_global.py`
- Mihomo: `adapters/mihomo.py`, `services/mihomo*.py`, `mihomo_config_rules.py`, `mihomo_config_proxies.py`, `mihomo_config_status.py`, `custom_servers.py`, `selector.py`
- Xray/subscription: `xray.py` facade, `xray_clients.py`, `xray_materialize.py`, `xray_subscription_service.py`, `xray_bindings.py`, `xray_client_state.py`, `xray_status.py`, `xray_runtime_state.py`, `xray_subscription.py`, `xray_handoff.py`, `subscription.py`, `subscription_pipeline.py`, `subscription_profiles.py`, `subject_groups.py`
- rules/DNS: `rules*.py`, `dnsmasq.py`, `rules_sources.py`, `rules_artifacts.py`
- UI read-model: `ui_state.py` facade, `ui_state_common.py`, `ui_state_settings.py`, `ui_state_clients.py`, `ui_state_inventory.py`, `ui_state_summary.py`, `ui_display_settings.py` facade and `ui_display_settings_*`, route docs, `opt_fwrouter_ui_static_js_mode_switching_md.md`
- maintenance/retention/logs: `maintenance.py`, `maintenance_scheduler.py`, `runtime_convergence.py`, `runtime_convergence_scheduler.py`, `jobs_retention.py`, `logs.py`, `logs_retention.py`, `state_retention.py`, `apply_versions_retention.py`
- watchdog: `watchdog.py` is the public facade; `watchdog_flows.py` is the compatibility flow facade; `watchdog_manual_flow.py` and `watchdog_auto_flow.py` own manual/automatic orchestration; `watchdog_auto_active_quality_flow.py` and `watchdog_auto_stall_flow.py` own large automatic decision branches; helper modules own status, persistent `watchdog_state`, debounce/cooldown, traffic signal analysis, active-server quality checks, decision logs, result DTOs, and scheduler lifecycle
- transfer/database/admin: `control_plane_transfer.py` facade and `control_plane_transfer_*`, `database_admin.py`, `schema_state.py`, `server_layout.py`
- tests: `tests/conftest.py` отвечает за изоляцию pytest от live dataplane/runtime state
- systemd timer wrappers outside `/opt/fwrouter-api`: `usr_local_sbin_fwrouter_subscription_refresh_job.md`, `usr_local_sbin_fwrouter_jobs_retention_dry_run.md`

## Coverage check

Для механической проверки, что у каждого Python/shell файла внутри `/opt/fwrouter-api` вне `tests/` есть карточка:

```bash
python3 - <<'PY'
from pathlib import Path
root = Path('/opt/fwrouter-api')
docs = Path('/решения/PROJECT_MAP/CODE_INDEX')
missing = []
for p in sorted(root.rglob('*')):
    if not p.is_file() or p.suffix not in {'.py', '.sh'}:
        continue
    if any(part in {'.venv', '__pycache__'} for part in p.parts):
        continue
    rel = str(p.relative_to(root))
    if rel.startswith('tests/'):
        continue
    name = ('/opt/fwrouter-api/' + rel).strip('/').replace('/', '_').replace('.', '_').replace('-', '_') + '.md'
    if not (docs / name).exists():
        missing.append(rel)
print('\\n'.join(missing))
print('missing_count', len(missing))
PY
```
