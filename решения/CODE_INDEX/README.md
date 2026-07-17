# Code Index

Здесь лежат точечные описания ключевых файлов проекта. Индекс покрывает файлы, которые влияют на boot persistence, dataplane, generated configs, system orchestration и API entrypoints.

Рекомендуемый порядок чтения:

1. `opt_fwrouter_api_fwrouter_api_main_py.md`
2. `opt_fwrouter_api_fwrouter_api_services_bootstrap_py.md`
3. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_py.md`
4. `opt_fwrouter_api_fwrouter_api_services_maintenance_py.md`
5. `opt_fwrouter_api_fwrouter_api_services_runtime_convergence_py.md`
6. `opt_fwrouter_api_fwrouter_api_services_runtime_convergence_scheduler_py.md`
7. `opt_fwrouter_api_fwrouter_api_services_apply_versions_retention_py.md`
8. `opt_fwrouter_api_fwrouter_api_services_jobs_retention_py.md`
9. `opt_fwrouter_api_fwrouter_api_services_state_retention_py.md`
10. `opt_fwrouter_api_fwrouter_api_services_dataplane_global_py.md`
11. `opt_fwrouter_api_fwrouter_api_services_dataplane_nft_py.md`
12. `opt_fwrouter_api_fwrouter_api_services_global_mode_profiles_py.md`
13. `opt_fwrouter_api_fwrouter_api_services_mihomo_config_py.md`
14. `opt_fwrouter_api_fwrouter_api_adapters_mihomo_py.md`
15. `opt_fwrouter_api_fwrouter_api_services_apply_orchestrator_handlers_py.md`
16. `opt_fwrouter_api_fwrouter_api_services_rules_py.md`
17. `opt_fwrouter_api_fwrouter_api_services_rules_state_py.md`
18. `opt_fwrouter_api_fwrouter_api_services_rules_artifacts_py.md`
19. `opt_fwrouter_api_fwrouter_api_services_rules_jobs_py.md`
20. `usr_local_libexec_fwrouter_dataplane_apply_sh.md`
21. `etc_systemd_system_fwrouter_api_service_md.md`
22. `opt_fwrouter_api_fwrouter_api_services_runtime_py.md`
23. `opt_fwrouter_api_fwrouter_api_services_subject_policy_py.md`
24. `opt_fwrouter_api_fwrouter_api_services_scoped_egress_py.md`
25. `opt_fwrouter_api_fwrouter_api_db_connection_py.md`
26. `opt_fwrouter_api_fwrouter_api_jobs_manager_py.md`
27. `opt_fwrouter_api_fwrouter_api_routes_system_py.md`
28. `opt_fwrouter_api_fwrouter_api_routes_servers_py.md`
29. `opt_fwrouter_api_fwrouter_api_routes_rules_py.md`
30. `opt_fwrouter_api_fwrouter_api_routes_xray_py.md`
31. `opt_fwrouter_api_fwrouter_api_services_live_probe_cache_py.md`
32. `opt_fwrouter_api_fwrouter_api_services_ui_state_py.md`
33. `opt_fwrouter_api_fwrouter_api_services_subject_groups_py.md`
34. `opt_fwrouter_ui_static_js_mode_switching_md.md`

## Быстрая карта доменов

- startup/boot: `main.py`, `bootstrap.py`, `runtime_prewarm.py`, `maintenance_scheduler.py`, `runtime_convergence_scheduler.py`, systemd unit docs
- apply/dataplane: `apply_orchestrator*.py`, `apply.py`, `dataplane_*.py`, `adapters/dataplane.py`, libexec `dataplane-*.sh`
- policy/routing: `subject_policy.py`, `scoped_egress.py`, `servers.py`, `routing_manifest.py`, `dataplane_global.py`
- Mihomo: `adapters/mihomo.py`, `services/mihomo*.py`, `custom_servers.py`, `selector.py`
- Xray/subscription: `xray.py`, `xray_subscription.py`, `xray_handoff.py`, `subscription.py`, `subscription_pipeline.py`, `subscription_profiles.py`, `subject_groups.py`
- rules/DNS: `rules*.py`, `dnsmasq.py`, `rules_sources.py`, `rules_artifacts.py`
- UI read-model: `ui_state.py`, route docs, `opt_fwrouter_ui_static_js_mode_switching_md.md`
- maintenance/retention/logs: `maintenance.py`, `maintenance_scheduler.py`, `runtime_convergence.py`, `runtime_convergence_scheduler.py`, `jobs_retention.py`, `logs.py`, `logs_retention.py`, `state_retention.py`, `apply_versions_retention.py`
- transfer/database/admin: `control_plane_transfer.py`, `database_admin.py`, `schema_state.py`, `server_layout.py`
- systemd timer wrappers outside `/opt/fwrouter-api`: `usr_local_sbin_fwrouter_subscription_refresh_job.md`, `usr_local_sbin_fwrouter_jobs_retention_dry_run.md`

## Coverage check

Для механической проверки, что у каждого Python/shell файла внутри `/opt/fwrouter-api` вне `tests/` есть карточка:

```bash
python3 - <<'PY'
from pathlib import Path
root = Path('/opt/fwrouter-api')
docs = Path('/решения/CODE_INDEX')
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
