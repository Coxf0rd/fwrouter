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
- `opt_fwrouter_api_fwrouter_api_services_watchdog_flows_py.md` documents the extracted watchdog manual/automatic decision flow module.
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
