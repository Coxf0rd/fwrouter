# Database Schema

Каноническая БД проекта это SQLite файл `/var/lib/fwrouter-v2/fwrouter.db`. Она хранит persistent intent, operational metadata, jobs, logs и учетное состояние, которое должно переживать reboot. Live `nftables`/`ip rule`/`ip route` сюда не пишутся как source of truth; вместо этого БД хранит то, что backend должен восстановить после boot.

## Файл и lifecycle

- schema source: `/opt/fwrouter-api/fwrouter_api/db/schema.sql`
- runtime access: `/opt/fwrouter-api/fwrouter_api/db/connection.py`
- schema drift checks: `/opt/fwrouter-api/fwrouter_api/db/schema_state.py`
- current expected schema version: `7`
- SQLite modes:
  - `journal_mode=WAL`
  - `synchronous=NORMAL`
  - `foreign_keys=ON`
  - `busy_timeout=30000`

## Карта таблиц по доменам

### Meta/config

- `schema_meta`
  Назначение: хранит `schema_version` и прочие schema-level маркеры.
  PK: `key`

- `settings`
  Назначение: key/value JSON для misc control-plane settings, включая bypass-related config.
  PK: `key`

- `modules`
  Назначение: desired/runtime/apply состояние модулей `core`, `vpn`, `xray`, `tailscale`, `watchdog`, `selector`, `subscription`.
  PK: `module_name`

### Subjects and details

- `subjects`
  Главная таблица субъектов трафика.
  PK: `subject_id`
  Важные поля:
  - `subject_type`
  - `stable_key`
  - `desired_mode`
  - `applied_mode`
  - `runtime_state`
  - `is_active`
  - `is_deleted`
  - timestamps first/last/inactive/deleted
  - `metadata_json`

- `subject_lan`
  Detail-таблица LAN-клиентов.
  PK/FK: `subject_id -> subjects.subject_id`
  Поля: `mac_address`, `ip_address`, `hostname`, `dhcp_hostname`, `source_json`

- `subject_tailscale`
  Detail-таблица `tailscale_node`.
  PK/FK: `subject_id`
  Поля: `node_id`, `tailscale_ip`, `hostname`, `user_name`, `online`, `source_json`

- `subject_xray`
  Detail-таблица Xray clients.
  PK/FK: `subject_id`
  Поля: `client_id`, `client_uuid`, `email`, `subscription_path`, `last_subscription_at`, `enabled`

- `subject_docker`
  Detail-таблица Docker services.
  PK/FK: `subject_id`
  Поля: `compose_project`, `compose_service`, `container_name`, `container_id`, `image_name`, `ip_address`, `network_name`

- `subject_host`
  Detail-таблица host/system services.
  PK/FK: `subject_id`
  Поля: `systemd_unit`, `listen_proto`, `listen_port`, `executable`, `process_name`

- `subject_fwrouter`
  Detail-таблица внутренних FWRouter system subjects.
  PK/FK: `subject_id`
  Поля: `component_name`

### Subject overrides / policy

- `subject_server_overrides`
  Subject-level выбранный сервер и apply status.
  PK/FK: `subject_id`
  Поля: `selected_server_id`, `selected_until`, `apply_state`, `error_code`, `error_message`

- `subject_user_overrides`
  User-level временный override режима.
  PK/FK: `subject_id`
  Поля: `override_mode`, `override_until`, `created_by`

### Traffic accounting

- `traffic_monthly`
  Ежемесячные агрегаты direct/vpn/blocked per subject.
  PK: `(subject_id, period_month)`

- `traffic_counter_snapshots`
  Последний snapshot счетчика по `counter_key`.
  PK: `counter_key`
  FK: `subject_id -> subjects.subject_id` (`ON DELETE SET NULL`)

### Server inventory and routing preference

- `servers`
  Канонический inventory серверов из subscription/custom sources.
  PK: `server_id`
  Важные поля: `server_name`, `provider_name`, `country_code`, `region`, `raw_json`, `inventory_state`

- `server_preferences`
  Предпочтения участия сервера в `vpn_auto` и global list.
  PK/FK: `server_id`
  Поля: `vpn_auto`, `vpn_auto_priority`, `global_list`, `remembered_until`, `manually_deleted_at`

- `server_ping_state`
  Результаты ping/delay checks.
  PK/FK: `server_id`
  Поля: `status`, `last_ping_ms`, `checked_at`, `checked_by`, `error_code`, `error_message`, `metadata_json`

- `server_custom_https_proxy`
  Данные для custom proxy servers.
  PK/FK: `server_id`
  Поля: `proxy_type`, `host`, `port`, `username`, `password`, `tls`, `sni`, `skip_cert_verify`, `path`

### Global routing and rules

- `routing_global_state`
  Singleton-таблица intended/applied глобального routing state.
  PK: `id` с инвариантом `id = 1`
  Поля:
  - `desired_mode`, `applied_mode`
  - `selective_default`
  - `server_mode`
  - `desired_fixed_server_id`
  - `applied_fixed_server_id`
  - `fixed_server_until`
  - `active_auto_server_id`
  - `apply_state`, `error_code`, `error_message`

- `rules_state`
  Singleton-таблица указателей на rule artifacts и общего status.
  PK: `id` с инвариантом `id = 1`
  Поля:
  - artifact paths для manual/static/effective rules
  - `selective_default`
  - `last_apply_job_id`, `last_update_job_id`
  - `status`, timestamps, `error_code`, `error_message`

- `rules_metadata`
  Состояние конкретных rule bundles по типам `manual`, `static_direct`, `big_direct`, `big_vpn`, `effective`.
  PK: `ruleset_id`

- `apply_versions`
  История apply attempts и связанных manifest/artifact dirs.
  PK: `apply_id`
  FK: `job_id -> jobs.job_id`

### Subscription plane

- `subscription_state`
  Singleton-таблица URL и статуса provider subscription refresh.
  PK: `id = 1`

- `subscription_accounts`
  Logical subscription accounts / profiles.
  PK: `account_id`
  Уникальное поле: `slug`

- `subscription_clients`
  Client tokens для публичных подписок.
  PK: `client_id`
  FK: `account_id -> subscription_accounts.account_id`

### Jobs and logs

- `jobs`
  Универсальная очередь/история jobs.
  PK: `job_id`
  Поля: `job_type`, `status`, `lock_key`, `requested_by`, `input_json`, `result_json`, `artifact_dir`, timestamps

- `operational_logs`
  Productized operational events.
  PK: `event_id`
  Поля: `level`, `event_type`, `subject_id`, `message`, `details_json`, `created_at`

## Основные связи

- `subjects` это parent для всех `subject_*` detail-таблиц и для `subject_*_overrides`, `traffic_monthly`
- `servers` это parent для `server_preferences`, `server_ping_state`, `server_custom_https_proxy`
- `routing_global_state` ссылается на `servers` для fixed/active server ids
- `rules_state` ссылается на `jobs`
- `apply_versions` ссылается на `jobs`
- `subscription_clients` ссылается на `subscription_accounts`

## Основные индексы

Критичные:

- `idx_subjects_active_stable_key`
  Уникальность `(subject_type, stable_key)` для не-удаленных subjects
- `idx_subjects_type_active`
- `idx_subjects_type_deleted`
- `idx_server_preferences_vpn_auto`
- `idx_server_ping_state_status`
- `idx_subject_server_overrides_until`
- `idx_subject_user_overrides_until`
- `idx_jobs_type_created`
- `idx_jobs_status_created`
- `idx_jobs_active_lock_unique`
  Partial unique index по `jobs.lock_key` для `queued/running` rows; атомарно не даёт создать второй активный apply/rules job с тем же lock.
- `idx_traffic_counter_snapshots_subject`

## Singleton tables

Эти таблицы логически должны иметь одну запись:

- `routing_global_state` (`id = 1`)
- `rules_state` (`id = 1`)
- `subscription_state` (`id = 1`)

Нарушение этого инварианта сломает backend assumptions.

## Ключевые инварианты схемы

- `subjects.subject_type` ограничен перечислением: `lan`, `tailscale`, `tailscale_node`, `xray`, `host`, `docker`, `fwrouter`
- `routing_global_state.desired_mode` ограничен `direct|selective|vpn`
- `routing_global_state.fixed_server_until` задает backend TTL для global fixed-server выбора; при истечении state возвращается в `server_mode='auto'`.
- `server_preferences.vpn_auto_priority` в диапазоне `-1..5`
- `traffic_counter_snapshots.path` ограничен `direct|vpn|blocked`
- soft-delete для subjects реализован через `is_deleted`, а не физическое удаление как стандартный путь
- `fwrouter:global` и builtin system subjects должны оставаться валидными subjects

## Что в БД является source of truth

Явно source of truth:

- `routing_global_state`
- `subjects` и их overrides
- `servers` и `server_preferences`
- `rules_state` и `rules_metadata`
- `subscription_state`, `subscription_accounts`, `subscription_clients`
- `modules`

Не является source of truth в смысле live kernel dataplane:

- `applied_mode` само по себе не гарантирует, что live `nftables`/`ip rule` уже в этом состоянии
- `server_ping_state` это operational cache
- `traffic_counter_snapshots` это accounting/runtime telemetry

## Как БД участвует в boot persistence

После reboot backend читает SQLite как persisted intent и operational baseline:

- восстанавливает desired routing mode
- восстанавливает subject effective-state semantics
- знает, какие server preferences и overrides действуют
- знает, какой subscription/rules state должен существовать

Но backend обязан заново materialize'ить live kernel/network state поверх этого intent.

## Что обновлять при изменениях схемы

- `DATABASE_SCHEMA.md`
- `CODE_INDEX/opt_fwrouter_api_fwrouter_api_db_schema_sql_md.md`
- `CODE_INDEX/opt_fwrouter_api_fwrouter_api_db_schema_state_py.md`
- `CODE_INDEX/opt_fwrouter_api_fwrouter_api_db_connection_py.md`
- `ARCHITECTURE.md`, если меняется source-of-truth model
- `INVARIANTS.md`, если меняются singleton/override/policy guarantees
