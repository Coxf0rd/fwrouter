# Code Index

This directory contains generated code-index cards for the FWRouter source and live deployment surfaces.

Use these cards as navigation aids only. Before changing behavior, read the real source file, the relevant architecture document, and the matching tests.

Regeneration rules:

- Keep entries in English.
- Keep cards concise and operationally useful.
- Update a card when the file responsibility, runtime side effects, boot relevance, or risk profile changes.
- Do not store secrets, runtime state, logs, or local AI scratch data here.
- `opt_fwrouter_api_tests_conftest_py.md` documents pytest isolation from live dataplane/runtime state.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_runtime_state_py.md` documents the extracted watchdog persistent-state helper.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_traffic_signal_py.md` documents the extracted watchdog traffic-signal analyzer.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_active_quality_py.md` documents the extracted watchdog active-server quality helper.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_status_py.md` documents the extracted watchdog status helper.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_failure_state_py.md` documents the extracted watchdog debounce/cooldown helper.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_decision_logs_py.md` documents the extracted watchdog decision-log helper.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_result_helpers_py.md` documents the extracted watchdog result helper.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_scheduler_py.md` documents the extracted watchdog scheduler helper.
- `opt_fwrouter_api_fwrouter_api_services_watchdog_flows_py.md` documents the extracted watchdog manual/automatic decision flow module.
