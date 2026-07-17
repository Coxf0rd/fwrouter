# Project Tree

Ниже перечислены важные каталоги и файлы. Это не дамп каждого артефакта, а рабочее дерево проекта для быстрой навигации по source/config/state.

## `/opt/fwrouter-api/`

### `/opt/fwrouter-api/README.md`

Тип: documentation  
Назначение: обзор backend и server-tree deployment модели.  
Используется: человеком, install/deploy процессом.  
Когда читается: при сопровождении и rollout.  
Критично для boot persistence: косвенно.  
Риски: может устареть относительно реального server layout.

### `/opt/fwrouter-api/pyproject.toml`

Тип: source/build metadata  
Назначение: Python package metadata, зависимости, entrypoint `fwrouter-api`.  
Используется: `uv`, `.venv`, backend запуск.  
Когда читается: install/build/runtime environment preparation.  
Критично для boot persistence: умеренно, если ломает runtime startup.

### `/opt/fwrouter-api/fwrouter_api/`

Тип: source code  
Назначение: основной control-plane backend.  
Используется: `uvicorn`, maintenance jobs, API routes.  
Когда читается: при запуске `fwrouter-api.service`.  
Критично для boot persistence: да.

### `/opt/fwrouter-api/fwrouter_api/main.py`

Тип: source code  
Назначение: создаёт FastAPI app, регистрирует routes, startup/shutdown hooks.  
Используется: `uvicorn`.  
Когда запускается: `fwrouter-api.service`.  
Критично для boot persistence: да.

### `/opt/fwrouter-api/fwrouter_api/core/paths.py`

Тип: source code  
Назначение: канонический layout `/etc`, `/var/lib`, `/var/log`, `/run`.  
Используется: почти всеми service-модулями.  
Когда читается: runtime backend.  
Критично для boot persistence: да.  
Риски: изменение путей ломает install, runtime recovery и generated artifacts.

### `/opt/fwrouter-api/fwrouter_api/services/bootstrap.py`

Тип: source code  
Назначение: startup bootstrap и recovery после reboot.  
Используется: `main.py`.  
Когда запускается: backend startup.  
Критично для boot persistence: да.

### `/opt/fwrouter-api/fwrouter_api/services/dataplane_global.py`

Тип: source code  
Назначение: контракт fwmarks, routing table, protected networks и dataplane profile.  
Используется: apply pipeline, runtime diagnostics, nft generation.  
Когда читается: apply, runtime summary, startup recovery.  
Критично для boot persistence: да.

### `/opt/fwrouter-api/fwrouter_api/services/dataplane_nft.py`

Тип: source code  
Назначение: рендерит owned nft candidate и артефакты.  
Используется: apply pipeline.  
Когда запускается: при смене глобального режима/правил.  
Критично для boot persistence: да.

### `/opt/fwrouter-api/fwrouter_api/services/apply_orchestrator.py`

Тип: source code  
Назначение: координирует apply jobs, drift detection, repair.  
Используется: API routes и startup recovery.  
Когда запускается: по API и частично на старте.  
Критично для boot persistence: да.  
Риски: нельзя нарушать lock discipline и status updates.

### `/opt/fwrouter-api/fwrouter_api/services/mihomo_config.py`

Тип: source code  
Назначение: собирает и продвигает `mihomo` config.  
Используется: apply pipeline, reconcile, runtime recovery.  
Когда запускается: при изменении server/routing state.  
Критично для boot persistence: да.

### `/opt/fwrouter-api/fwrouter_api/services/xray.py`

Тип: source code  
Назначение: xray clients, subscriptions, runtime bindings.  
Используется: API routes, gateway, scoped egress handoff.  
Когда запускается: API calls и runtime summary.  
Критично для boot persistence: умеренно.

### `/opt/fwrouter-api/fwrouter_api/routes/`

Тип: source code  
Назначение: `/api/v2` endpoints.  
Используется: UI, scripts, internal jobs.  
Когда читается: runtime backend.  
Критично для boot persistence: косвенно, особенно runtime/operations/mihomo/xray/servers routes.

### `/opt/fwrouter-api/fwrouter_api/db/schema.sql`

Тип: source code / persistent schema  
Назначение: SQLite schema для intent, jobs, subjects, servers, modules.  
Используется: database initialization and migrations.  
Когда читается: startup backend.  
Критично для boot persistence: да.

### `/opt/fwrouter-api/fwrouter_api/db/schema_state.py`

Тип: source code  
Назначение: schema drift inspection и summary для health/system endpoints.  
Используется: database init, `/api/v2/health`, `/api/v2/system/summary`.  
Когда читается: startup и runtime diagnostics.  
Критично для boot persistence: да, как guard против schema drift.

### `/opt/fwrouter-api/fwrouter_api_maintenance.py`

Тип: source code / CLI entrypoint  
Назначение: maintenance runner, запускаемый таймером systemd.  
Используется: `fwrouter-maintenance.service`.  
Когда запускается: по timer.  
Критично для boot persistence: косвенно.

### `/opt/fwrouter-api/fwrouter_api/services/maintenance.py`

Тип: source code  
Назначение: orchestration periodic cleanup, compaction и database storage maintenance.  
Используется: `fwrouter_api_maintenance.py`.  
Когда запускается: `fwrouter-maintenance.service` и manual cleanup.  
Критично для boot persistence: да.

### `/opt/fwrouter-api/fwrouter_api/services/runtime_convergence.py`

Тип: source code  
Назначение: быстрый runtime self-heal для selective/VPN dnsmasq/dataplane contract.  
Используется: `runtime_convergence_scheduler.py`; watchdog читает только последний status.  
Когда запускается: backend scheduler tick.  
Критично для boot persistence: косвенно.

### `/opt/fwrouter-api/fwrouter_api/services/runtime_convergence_scheduler.py`

Тип: source code  
Назначение: in-process scheduler для `runtime_convergence.py`.  
Используется: `main.py` lifespan.  
Когда запускается: вместе с `fwrouter-api.service`, если включен `FWROUTER_RUNTIME_CONVERGENCE_SCHEDULER_ENABLED`.  
Критично для boot persistence: косвенно.

### `/opt/fwrouter-api/fwrouter_api/services/apply_versions_retention.py`

Тип: source code  
Назначение: retention для `apply_versions` rows и versioned manifests в `generated/dataplane/`.  
Используется: maintenance pipeline.  
Когда запускается: `fwrouter-maintenance.service`.  
Критично для boot persistence: да.

### `/opt/fwrouter-api/scripts/bootstrap-state.sh`

Тип: install/setup script  
Назначение: создаёт persistent/runtime директории.  
Используется: preflight, install script.  
Когда запускается: install и boot preflight.  
Критично для boot persistence: да.  
Риски: должен оставаться идемпотентным.

### `/opt/fwrouter-api/scripts/install-server-tree.sh`

Тип: install/setup script  
Назначение: ставит host dependencies/Python env при target `/`, раскладывает units/libexec/sysctl/rt_tables и включает сервисы/таймеры.  
Используется: администратором вручную.  
Когда запускается: deployment/install.  
Критично для boot persistence: да.

### `/opt/fwrouter-api/scripts/install-host-dependencies.sh`

Тип: install/setup script  
Назначение: ставит apt-level host dependencies для backend/dataplane/Docker на Debian/Ubuntu-like сервере.  
Используется: `install-server-tree.sh` при target `/` и администратором вручную.  
Когда запускается: initial deployment или repair окружения.  
Критично для boot persistence: да.  
Риски: non-apt дистрибутивы требуют отдельного package mapping.

### `/opt/fwrouter-api/scripts/setup-python-env.sh`

Тип: install/setup script  
Назначение: создает `/opt/fwrouter-api/.venv` и устанавливает backend package editable install.  
Используется: `install-server-tree.sh` при target `/` и администратором вручную.  
Когда запускается: initial deployment или обновление backend dependencies.  
Критично для boot persistence: да.  
Риски: `.venv` является host-local generated state и не должен попадать в git/export.

### `/opt/fwrouter-api/scripts/check_boot_persistence.sh`

Тип: diagnostic script  
Назначение: read-only проверка boot readiness и persistence.  
Используется: вручную администратором или агентом.  
Когда запускается: до и после reboot.  
Критично для boot persistence: не меняет систему, но критичен для диагностики.

## `/usr/local/libexec/fwrouter/`

### `/usr/local/libexec/fwrouter/dataplane-apply.sh`

Тип: shell script  
Назначение: применяет owned `nftables` table и policy routing contract.  
Используется: script adapter, apply pipeline.  
Когда запускается: apply/recovery.  
Критично для boot persistence: да.

### `/usr/local/libexec/fwrouter/dataplane-check.sh`

Тип: shell script  
Назначение: проверяет candidate/live dataplane contract.  
Используется: bootstrap recovery, diagnostics, apply pipeline.  
Когда запускается: startup checks и API operations.  
Критично для boot persistence: да.

### `/usr/local/libexec/fwrouter/dataplane-rollback.sh`

Тип: shell script  
Назначение: откатывает host dataplane к snapshot/last-good.  
Используется: apply pipeline rollback.  
Когда запускается: при ошибках apply.  
Критично для boot persistence: да.

### `/usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh`

Тип: shell script  
Назначение: проверяет `/dev/net/tun`, `nft`, `ip`, bootstrap state, `sysctl`, `rt_tables`.  
Используется: `fwrouter-api`, `fwrouter-mihomo`, `fwrouter-xray`.  
Когда запускается: ExecStartPre/ExecStart сервисов.  
Критично для boot persistence: да.

### `/usr/local/libexec/fwrouter/fwrouter-wait-port.sh`

Тип: helper script  
Назначение: ждёт доступности TCP порта.  
Используется: units для API/Mihomo readiness.  
Когда запускается: boot/restart.  
Критично для boot persistence: да, снижает race conditions.

### `/usr/local/libexec/fwrouter/fwrouter-xray-sub-gateway.py`

Тип: source code / runtime helper  
Назначение: отдельный HTTP gateway для xray subscription endpoints.  
Используется: `fwrouter-xray-sub-gateway.service`.  
Когда запускается: boot и runtime.  
Критично для boot persistence: умеренно.

### `/usr/local/libexec/fwrouter/traffic-collect.sh`

Тип: shell script  
Назначение: снимает traffic counters из Mihomo/Xray/nftables. Для global VPN accounting читает `fwrouter vpn mark tcp:*`/`udp:*` comments и оставляет fallback на legacy `fwrouter global vpn mark:*`. Xray per-client accounting читает StatsService и привязывает sample к `fwrouterBinding.subject_id`, либо к fallback `xray:<client_uuid>`, если binding metadata временно отсутствует.  
Используется: traffic collect job.  
Когда запускается: timer.  
Критично для boot persistence: косвенно.

## `/usr/local/sbin/`

### `/usr/local/sbin/fwrouter-subscription-refresh-job`

Тип: Python CLI wrapper  
Назначение: создает backend job `subscription_refresh_prepare` через `/api/v2/jobs`.  
Используется: `fwrouter-subscription-refresh.service`.  
Когда запускается: по timer `fwrouter-subscription-refresh.timer`.  
Критично для boot persistence: умеренно.

### `/usr/local/sbin/fwrouter-jobs-retention-dry-run`

Тип: Python CLI wrapper  
Назначение: создает backend job `jobs_retention_cleanup` только в dry-run режиме и проверяет, что ничего не удалено.  
Используется: `fwrouter-jobs-retention-dry-run.service`.  
Когда запускается: по timer `fwrouter-jobs-retention-dry-run.timer`.  
Критично для boot persistence: низкая/умеренная; это diagnostic retention guard.

## `/etc/systemd/system/`

### `/etc/systemd/system/fwrouter-mihomo.service`

Тип: systemd unit / persistent config  
Назначение: поднимает Mihomo container и ждёт controller port.  
Используется: boot orchestration.  
Когда запускается: boot, manual restart.  
Критично для boot persistence: да.

### `/etc/systemd/system/fwrouter-xray.service`

Тип: systemd unit / persistent config  
Назначение: поднимает Xray container после проверки Docker network.  
Используется: boot orchestration.  
Когда запускается: boot, manual restart.  
Критично для boot persistence: да.

### `/etc/systemd/system/fwrouter-api.service`

Тип: systemd unit / persistent config  
Назначение: запускает backend `uvicorn`.  
Используется: boot orchestration.  
Когда запускается: boot, manual restart.  
Критично для boot persistence: да.

### `/etc/systemd/system/fwrouter-xray-sub-gateway.service`

Тип: systemd unit / persistent config  
Назначение: поднимает API-proxy gateway для subscription downloads.  
Используется: boot orchestration.  
Когда запускается: boot, manual restart.  
Критично для boot persistence: умеренно.

### `/etc/systemd/system/fwrouter-*.timer`

Тип: systemd timer / persistent config  
Назначение: maintenance, retention, traffic collection, subscription refresh.  
Используется: systemd timers.  
Когда запускается: post-boot и по расписанию.  
Критично для boot persistence: умеренно.

## DNS and resolver config

### `/etc/dnsmasq.d/fwrouter-rules.conf`

Тип: generated persistent config  
Назначение: domain-aware selective routing, `nftset` population и selective upstream overrides.  
Используется: `dnsmasq`.  
Когда читается: runtime DNS resolution.  
Критично для boot persistence: да, для domain-based selective routing.

### `/etc/dnsmasq.d/fwrouter-dhcp-dns.conf`

Тип: generated persistent config  
Назначение: заставляет LAN-клиентов использовать DNS роутера.  
Используется: `dnsmasq` DHCP path.  
Когда читается: runtime DHCP/DNS.  
Критично для boot persistence: умеренно.

### `/etc/dnsmasq.d/lan.conf`

Тип: persistent config  
Назначение: LAN DHCP range/static leases; DHCP DNS option must contain only router DNS (`192.168.0.1`).  
Используется: `dnsmasq` DHCP path.  
Когда читается: runtime DHCP lease negotiation.  
Критично для boot persistence: да, для корректного domain-aware selective routing.

### `/etc/dnsmasq.d/fwrouter-upstream-dns.conf`

Тип: persistent config  
Назначение: фиксирует public upstream DNS для `dnsmasq` и отключает зависимость от ISP DNS через `no-resolv`.  
Используется: `dnsmasq`.  
Когда читается: service start/reload.  
Критично для boot persistence: да.  
Риски: если public DNS недоступны, сломается обычный resolver path.

### `/etc/dhcp/dhclient.conf`

Тип: persistent system config  
Назначение: фиксирует public DNS для host resolver через `supersede domain-name-servers`.  
Используется: `dhclient` на WAN интерфейсе.  
Когда читается: DHCP renew/reboot.  
Критично для boot persistence: да.  
Риски: нельзя бездумно вернуть ISP DNS, иначе локальный `dnsmasq` снова начнет получать ложные ответы от апстрима.

## Runtime, state and generated data

### `/var/lib/fwrouter-v2/fwrouter.db`

Тип: persistent state  
Назначение: канонический intent и operational state backend.  
Используется: backend services.  
Когда читается: startup и runtime.  
Критично для boot persistence: да.

### `/var/lib/fwrouter-v2/generated/`

Тип: generated artifacts  
Назначение: generated dataplane, mihomo, effective rules.  
Используется: backend, containers, apply scripts.  
Когда читается: boot recovery и runtime.  
Критично для boot persistence: да.

### `/var/lib/fwrouter-v2/last-good/`

Тип: generated backup artifacts  
Назначение: known-good snapshots для rollback и recovery.  
Используется: apply pipeline и startup diagnostics.  
Когда читается: при сбоях и на boot recovery.  
Критично для boot persistence: да.

### `/var/lib/fwrouter-v2/debug/`

Тип: runtime/debug artifacts  
Назначение: диагностика и rollback traces.  
Используется: acceptance/debug scripts.  
Когда читается: вручную.  
Критично для boot persistence: нет.  
Риски: не считать каноническим source of truth.
