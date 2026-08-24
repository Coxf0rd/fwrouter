# Code Index

This directory contains generated code-index cards for the FWRouter source and live deployment surfaces.

Use these cards as navigation aids only. Before changing behavior, read the real source file, the relevant architecture document, and the matching tests.

Regeneration rules:

- Keep entries in English.
- Keep cards concise and operationally useful.
- Update a card when the file responsibility, runtime side effects, boot relevance, or risk profile changes.
- Do not store secrets, runtime state, logs, or local AI scratch data here.
- `opt_fwrouter_api_tests_conftest_py.md` documents pytest isolation from live dataplane/runtime state.
- `opt_fwrouter_api_fwrouter_api_services_apply_py.md` documents the core apply pipeline.
- `opt_fwrouter_api_fwrouter_api_services_apply_plan_py.md` documents apply planning and job-context helpers.
- `opt_fwrouter_api_fwrouter_api_services_apply_manifest_py.md` documents apply manifest DTO helpers.
- `opt_fwrouter_api_fwrouter_api_services_apply_hot_swap_py.md` documents classify-chain hot-swap helpers.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_runtime_state_py.md` documents the extracted watchdog persistent-state helper.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_traffic_signal_py.md` documents the extracted watchdog traffic-signal analyzer.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_active_quality_py.md` documents the extracted watchdog active-server quality helper.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_status_py.md` documents the extracted watchdog status helper.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_failure_state_py.md` documents the extracted watchdog debounce/cooldown helper.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_decision_logs_py.md` documents the extracted watchdog decision-log helper.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_result_helpers_py.md` documents the extracted watchdog result helper.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_scheduler_py.md` documents the extracted watchdog scheduler helper.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_flow_deps_py.md` documents shared watchdog flow constants and dependency contract.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_manual_flow_py.md` documents the extracted watchdog manual/runtime check flow.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_auto_flow_py.md` documents the extracted watchdog automatic scheduler decision flow.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_auto_stall_flow_py.md` documents the automatic watchdog confirmed stalled-traffic branch.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_auto_active_quality_flow_py.md` documents the automatic watchdog response-traffic active-quality branch.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_flows_py.md` documents the watchdog flow compatibility facade.
- `opt_fwrouter_api_fwrouter_api_services_servers_py.md` documents the compatibility facade for server selection services.
- `opt_fwrouter_api_fwrouter_api_services_server_inventory_py.md` documents server inventory listing, lookup, and Mihomo sync.
- `opt_fwrouter_api_fwrouter_api_services_server_state_py.md` documents persisted global routing state helpers.
- `opt_fwrouter_api_fwrouter_api_services_server_global_selection_py.md` documents global fixed/auto server apply flows.
- `opt_fwrouter_api_fwrouter_api_services_server_subject_overrides_py.md` documents per-subject manual server overrides.
- `opt_fwrouter_api_fwrouter_api_services_server_preferences_py.md` documents VPN-auto and global-list preference updates.
- `opt_fwrouter_api_fwrouter_api_services_ui_state_py.md` documents the compatibility facade for UI read-model services.
- `opt_fwrouter_api_fwrouter_api_services_ui_state_common_py.md` documents shared UI read-model helpers.
- `opt_fwrouter_api_fwrouter_api_services_ui_state_settings_py.md` documents persisted UI display settings.
- `opt_fwrouter_api_fwrouter_api_services_ui_state_clients_py.md` documents UI client list DTOs and client counts.
- `opt_fwrouter_api_fwrouter_api_services_ui_state_inventory_py.md` documents settings inventory DTOs.
- `opt_fwrouter_api_fwrouter_api_services_ui_state_summary_py.md` documents router summary and settings workspace DTOs.
- `opt_fwrouter_api_fwrouter_api_services_ui_text_py.md` documents the shared locale-aware UI text registry.
- `opt_fwrouter_api_fwrouter_api_adapters_xray_common_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_adapters_xray_noop_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_adapters_xray_real_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_commits_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_constants_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_dispatch_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_drift_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_global_handlers_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_handler_common_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_jobs_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_pipeline_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_public_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_results_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_rules_handlers_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_server_handlers_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_state_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_subject_handlers_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_xray_clients_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_xray_common_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_xray_materialize_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_xray_subscription_service_py.md` documents the extracted apply/Xray split module.
- `opt_fwrouter_api_fwrouter_api_services_ui_display_settings_py.md` documents the UI display settings compatibility facade.
- `opt_fwrouter_api_fwrouter_api_services_ui_display_settings_common_py.md` documents shared UI display settings constants and normalizers.
- `opt_fwrouter_api_fwrouter_api_services_ui_display_settings_store_py.md` documents persisted UI display settings storage helpers.
- `opt_fwrouter_api_fwrouter_api_services_ui_display_settings_display_py.md` documents Settings > Connections display-system assembly.
- `opt_fwrouter_api_fwrouter_api_services_ui_display_settings_guides_py.md` documents external connection guides and readiness metadata.
- `opt_fwrouter_api_fwrouter_api_services_ui_display_settings_external_py.md` documents custom external connection write and contract APIs.
- `opt_fwrouter_api_fwrouter_api_services_control_plane_transfer_py.md` documents the control-plane transfer compatibility facade.
- `opt_fwrouter_api_fwrouter_api_services_control_plane_transfer_common_py.md` documents shared control-plane transfer constants and helpers.
- `opt_fwrouter_api_fwrouter_api_services_control_plane_transfer_export_py.md` documents control-plane snapshot export.
- `opt_fwrouter_api_fwrouter_api_services_control_plane_transfer_source_py.md` documents snapshot source resolution and file listing.
- `opt_fwrouter_api_fwrouter_api_services_control_plane_transfer_validation_py.md` documents snapshot validation.
- `opt_fwrouter_api_fwrouter_api_services_control_plane_transfer_plan_py.md` documents dry-run import planning.
- `opt_fwrouter_api_fwrouter_api_services_control_plane_transfer_import_py.md` documents snapshot import/writeback.

- `opt_fwrouter_api_fwrouter_api_services_rules_compile_py.md` documents rules validation, normalization, source-policy, compiler, and renderer helpers.

- `opt_fwrouter_api_fwrouter_api_services_mihomo_config_validation_py.md` documents Mihomo candidate structural validation helpers.
- `opt_fwrouter_api_fwrouter_api_services_mihomo_reconcile_fingerprint_py.md` documents the persistent input fingerprint used to skip unchanged Mihomo full reconcile work.

- `opt_fwrouter_api_fwrouter_api_services_rules_state_store_py.md` documents base rules_state row/path storage helpers.

- `opt_fwrouter_api_fwrouter_api_services_rules_state_selective_py.md` documents selective-default active artifact sync helpers.

- `opt_fwrouter_api_fwrouter_api_services_rules_state_files_py.md` documents active/candidate rules file and last-good storage helpers.

- `opt_fwrouter_api_fwrouter_api_services_rules_state_metadata_py.md` documents rules metadata rows and job state helpers.

- `opt_fwrouter_api_fwrouter_api_services_rules_state_readmodel_py.md` documents lightweight rules UI/API read-model helpers.
