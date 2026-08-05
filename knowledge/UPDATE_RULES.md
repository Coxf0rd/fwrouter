# Update Rules

Main rule: after any change to code, configs, systemd units, nftables, routing logic, install scripts, or API, update the affected files in `knowledge/` with a narrow, targeted edit.

User-facing guides live at the root of `knowledge/`. The technical project map, architecture, ADRs, and `CODE_INDEX` live in `PROJECT_MAP/`.

## If A Systemd Unit Changes

- update `PROJECT_MAP/SYSTEMD.md`
- update `PROJECT_MAP/BOOT_FLOW.md`
- update the matching file in `PROJECT_MAP/CODE_INDEX/`
- check `PROJECT_MAP/INVARIANTS.md`

## If Boot / Startup Recovery Logic Changes

- update `PROJECT_MAP/ARCHITECTURE.md`
- update `PROJECT_MAP/RUNTIME_FLOW.md`
- update `PROJECT_MAP/BOOT_FLOW.md`
- update `PROJECT_MAP/CONFIGS_AND_STATE.md` if artifacts or directories changed
- update the index for `bootstrap.py` or the related file

## If Nftables Logic Changes

- update `PROJECT_MAP/NFTABLES.md`
- update `PROJECT_MAP/NETWORK_MODEL.md`
- update `PROJECT_MAP/POLICY_ROUTING.md` if marks, tables, or priorities change
- update `PROJECT_MAP/BOOT_FLOW.md` if recovery changes
- update the matching ADR if the architecture changed

## If Policy Routing Logic Changes

- update `PROJECT_MAP/POLICY_ROUTING.md`
- update `PROJECT_MAP/NETWORK_MODEL.md`
- update `PROJECT_MAP/SYSCTL.md` if kernel prerequisites change
- update the index for `dataplane-apply.sh` and related Python services

## If Mihomo / Xray Contract Changes

- update `PROJECT_MAP/MIHOMO.md` or `PROJECT_MAP/XRAY.md`
- update `PROJECT_MAP/NETWORK_MODEL.md`
- update `PROJECT_MAP/SYSTEMD.md` if units or readiness checks change
- update `PROJECT_MAP/CODE_INDEX/` for matching compose/service/adapter files

## If Install / Setup Scripts Change

- update `INSTALL_AND_DEPLOY.md`
- update `PROJECT_MAP/PROJECT_TREE.md`
- update the matching `PROJECT_MAP/CODE_INDEX/*.md`
- check `PROJECT_MAP/INVARIANTS.md`

## If API / CLI Changes

- update `API_AND_CLI.md`
- update `PROJECT_MAP/QUICK_START_FOR_AGENTS.md` if primary entrypoints changed
- update the index for route/service entrypoints

## If The Database Schema Or DB Model Changes

- update `PROJECT_MAP/DATABASE_SCHEMA.md`
- update `PROJECT_MAP/PROJECT_TREE.md` if new schema/runtime files appeared
- update `PROJECT_MAP/CODE_INDEX/opt_fwrouter_api_fwrouter_api_db_connection_py.md`
- update `PROJECT_MAP/CODE_INDEX/opt_fwrouter_api_fwrouter_api_db_schema_sql_md.md`
- update `PROJECT_MAP/CODE_INDEX/opt_fwrouter_api_fwrouter_api_db_schema_state_py.md`
- check `PROJECT_MAP/ARCHITECTURE.md` and `PROJECT_MAP/INVARIANTS.md` if source-of-truth or lifecycle state changed

## If A New Important File Is Added

- add it to `PROJECT_MAP/PROJECT_TREE.md`
- add a new file in `PROJECT_MAP/CODE_INDEX/` if it affects boot, routing, apply, config generation, or runtime orchestration
- update an ADR when needed
