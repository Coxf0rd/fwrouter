# Project Tree

This file is a navigation map, not a complete dump of every artifact.

## Source Root

- `/srv/fwrouter`: canonical git source root.
- `/srv/fwrouter/backend`: FastAPI control plane, services, adapters, database schema, routes, jobs, tests, and helper scripts.
- `/srv/fwrouter/ui`: static UI served by the backend.
- `/srv/fwrouter/runtimes/mihomo`: Mihomo runtime wrapper and compose file.
- `/srv/fwrouter/runtimes/xray`: Xray runtime wrapper and compose file.
- `/srv/fwrouter/host`: host integration files for systemd, libexec helpers, sysctl, iproute2, and timer wrappers.
- `/srv/fwrouter/installer`: source-to-live install and validation tooling.
- `/srv/fwrouter/knowledge`: persistent in-repo project knowledge map.

## Backend Hotspots

- `backend/fwrouter_api/main.py`: FastAPI app, routes, startup/shutdown lifecycle.
- `backend/fwrouter_api/core/paths.py`: canonical path layout for `/etc`, `/var/lib`, `/var/log`, and `/run`.
- `backend/fwrouter_api/db/schema.sql`: SQLite schema for persistent intent, modules, jobs, subjects, servers, rules, subscriptions, and traffic accounting.
- `backend/fwrouter_api/db/schema_state.py`: schema drift inspection and health summaries.
- `backend/fwrouter_api/services/bootstrap.py`: startup bootstrap and reboot recovery.
- `backend/fwrouter_api/services/dataplane_global.py`: global routing contract, marks, table ids, protected networks, and dataplane profiles.
- `backend/fwrouter_api/services/dataplane_nft.py`: nftables candidate rendering and dataplane artifacts.
- `backend/fwrouter_api/services/apply.py`: apply jobs, drift repair, runtime state updates.
- `backend/fwrouter_api/services/mihomo_config.py`: generated Mihomo config and contours.
- `backend/fwrouter_api/services/mihomo_runtime.py`: Mihomo runtime inspection and selector state.
- `backend/fwrouter_api/services/xray.py`: Xray clients, generated config, and bindings.
- `backend/fwrouter_api/services/xray_handoff.py`: explicit Xray-to-Mihomo handoff.
- `backend/fwrouter_api/services/runtime.py`: runtime summary assembly.
- `backend/fwrouter_api/services/runtime_convergence.py`: lightweight runtime self-heal for selective/VPN dnsmasq/dataplane contracts.
- `backend/fwrouter_api/services/runtime_convergence_scheduler.py`: periodic runtime convergence scheduler.
- `backend/fwrouter_api/services/rules.py`: rule source refresh, list parsing, and generated rules state.
- `backend/fwrouter_api/services/subscription_pipeline.py`: subscription refresh pipeline.
- `backend/fwrouter_api/services/traffic.py`: traffic accounting persistence and aggregation.
- `backend/fwrouter_api/routes/`: `/api/v2` route handlers used by UI, jobs, and scripts.

## Installer And Host Scripts

- `installer/install.sh`: deploys selected components into live paths and enables only the units/timers owned by selected components.
- `installer/test-install.sh`: validates component install boundaries and dependency dry-runs without touching live paths.
- `installer/install-host-dependencies.sh`: installs Debian/Ubuntu host dependencies.
- `installer/check-clean-tree-surface.sh`: validates that git/export surface excludes runtime state and includes expected host files.
- `host/libexec/fwrouter/dataplane-apply.sh`: applies owned nftables table and policy-routing contract.
- `host/libexec/fwrouter/dataplane-check.sh`: checks candidate/live dataplane contract.
- `host/libexec/fwrouter/dataplane-common.sh`: shared dataplane shell contract helpers.
- `host/libexec/fwrouter/dataplane-rollback.sh`: rolls back owned dataplane state.
- `host/libexec/fwrouter/fwrouter-boot-preflight.sh`: validates host prerequisites before service startup.
- `host/libexec/fwrouter/fwrouter-wait-port.sh`: waits for TCP readiness.
- `host/libexec/fwrouter/fwrouter-xray-sub-gateway.py`: subscription gateway process.
- `host/libexec/fwrouter/traffic-collect.sh`: traffic counter collector.
- `host/libexec/fwrouter/traffic-collect-api.sh`: API-facing traffic collect wrapper.
- `host/sbin/fwrouter-subscription-refresh-job`: creates subscription refresh jobs through the backend API.
- `host/sbin/fwrouter-jobs-retention-dry-run`: dry-run retention guard.

## Systemd Surface

- `host/systemd/fwrouter-mihomo.service`: starts Mihomo container and waits for controller readiness.
- `host/systemd/fwrouter-xray.service`: starts Xray container after configured Docker network preflight.
- `host/systemd/fwrouter-api.service`: starts backend `uvicorn` and owns startup recovery without requiring optional runtimes.
- `host/systemd/fwrouter-xray-sub-gateway.service`: starts subscription gateway after API readiness.
- `host/systemd/fwrouter-docker-subject-events.service`: accelerates Docker subject inventory updates.
- `host/systemd/dnsmasq.service.d/fwrouter-restart.conf`: adds FWRouter-owned restart policy to the distribution `dnsmasq.service`.
- `host/systemd/fwrouter-subscription-refresh.timer`: scheduled subscription refresh.
- `host/systemd/fwrouter-maintenance.timer`: scheduled maintenance.
- `host/systemd/fwrouter-jobs-retention-dry-run.timer`: scheduled dry-run retention diagnostics.
- `host/systemd/fwrouter-traffic-collect.timer`: scheduled traffic collection.

## Live Targets

- `/opt/fwrouter-api`: deployed backend.
- `/opt/fwrouter-ui`: deployed UI.
- `/opt/fwrouter-mihomo`: deployed Mihomo runtime wrapper.
- `/opt/fwrouter-xray`: deployed Xray runtime wrapper.
- `/etc/systemd/system`: deployed units and timers.
- `/usr/local/libexec/fwrouter`: deployed host helpers.
- `/usr/local/sbin`: deployed scheduled wrappers.
- `/etc/sysctl.d`: deployed sysctl fragment.
- `/etc/iproute2`: deployed routing table fragment.

## Persistent Runtime State

- `/var/lib/fwrouter-v2/fwrouter.db`: canonical persistent intent and operational state.
- `/var/lib/fwrouter-v2/generated`: generated dataplane, Mihomo, Xray, and rules artifacts.
- `/var/lib/fwrouter-v2/last-good`: last-known-good snapshots for rollback and recovery.
- `/var/lib/fwrouter-v2/jobs`: job artifacts and compact summaries.
- `/var/log/fwrouter`: operational, technical, and Xray logs.
- `/run/fwrouter-v2`: runtime-only locks and status files.

## Navigation Rules

- Read the real source file before changing behavior.
- Check the matching code-index card for nearby risk notes.
- For dataplane changes, read `NETWORK_MODEL.md`, `NFTABLES.md`, `POLICY_ROUTING.md`, `MIHOMO.md`, and relevant ADRs.
- For boot changes, read `BOOT_FLOW.md`, `SYSTEMD.md`, `SYSCTL.md`, and `INSTALL_AND_DEPLOY.md`.
- For state changes, read `DATABASE_SCHEMA.md`, `CONFIGS_AND_STATE.md`, and affected tests.
