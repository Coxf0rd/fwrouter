PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS modules (
    module_name TEXT PRIMARY KEY,
    desired_state TEXT NOT NULL,
    runtime_state TEXT NOT NULL,
    apply_state TEXT NOT NULL DEFAULT 'clean',
    status_text TEXT,
    error_code TEXT,
    error_message TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (desired_state IN ('enabled', 'disabled')),
    CHECK (runtime_state IN ('not_configured', 'running', 'stopped', 'failed', 'degraded', 'paused')),
    CHECK (apply_state IN ('clean', 'pending', 'applying', 'failed'))
);

CREATE TABLE IF NOT EXISTS subjects (
    subject_id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    stable_key TEXT NOT NULL,
    display_name TEXT,
    alias TEXT,
    desired_mode TEXT NOT NULL,
    applied_mode TEXT,
    apply_state TEXT NOT NULL DEFAULT 'clean',
    runtime_state TEXT NOT NULL DEFAULT 'not_configured',
    is_active INTEGER NOT NULL DEFAULT 0,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT,
    last_traffic_at TEXT,
    inactive_since TEXT,
    deleted_at TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (subject_type IN ('lan', 'tailscale', 'tailscale_node', 'xray', 'host', 'docker', 'fwrouter')),
    CHECK (desired_mode IN ('global', 'direct', 'selective', 'vpn', 'disabled', 'enabled', 'forced_vpn')),
    CHECK (applied_mode IS NULL OR applied_mode IN ('global', 'direct', 'selective', 'vpn', 'disabled', 'enabled', 'forced_vpn')),
    CHECK (apply_state IN ('clean', 'pending', 'applying', 'failed')),
    CHECK (runtime_state IN ('not_configured', 'active', 'inactive', 'missing', 'running', 'stopped', 'failed', 'degraded', 'paused')),
    CHECK (is_active IN (0, 1)),
    CHECK (is_deleted IN (0, 1))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_subjects_active_stable_key
ON subjects (subject_type, stable_key)
WHERE is_deleted = 0;

CREATE INDEX IF NOT EXISTS idx_subjects_type_active
ON subjects (subject_type, is_active, last_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_subjects_type_deleted
ON subjects (subject_type, is_deleted, deleted_at);

CREATE TABLE IF NOT EXISTS subject_lan (
    subject_id TEXT PRIMARY KEY,
    mac_address TEXT,
    ip_address TEXT,
    hostname TEXT,
    dhcp_hostname TEXT,
    source_json TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (subject_id) REFERENCES subjects(subject_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_subject_lan_mac
ON subject_lan (mac_address);

CREATE INDEX IF NOT EXISTS idx_subject_lan_ip
ON subject_lan (ip_address);

CREATE TABLE IF NOT EXISTS subject_tailscale (
    subject_id TEXT PRIMARY KEY,
    node_id TEXT,
    tailscale_ip TEXT,
    hostname TEXT,
    user_name TEXT,
    online INTEGER NOT NULL DEFAULT 0,
    source_json TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (online IN (0, 1)),
    FOREIGN KEY (subject_id) REFERENCES subjects(subject_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_subject_tailscale_node_id
ON subject_tailscale (node_id);

CREATE INDEX IF NOT EXISTS idx_subject_tailscale_ip
ON subject_tailscale (tailscale_ip);

CREATE TABLE IF NOT EXISTS subject_xray (
    subject_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    client_uuid TEXT,
    email TEXT,
    subscription_path TEXT,
    last_subscription_at TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    source_json TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (enabled IN (0, 1)),
    FOREIGN KEY (subject_id) REFERENCES subjects(subject_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_subject_xray_client_id
ON subject_xray (client_id);

CREATE TABLE IF NOT EXISTS subject_docker (
    subject_id TEXT PRIMARY KEY,
    compose_project TEXT,
    compose_service TEXT,
    container_name TEXT,
    container_id TEXT,
    image_name TEXT,
    ip_address TEXT,
    network_name TEXT,
    source_json TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (subject_id) REFERENCES subjects(subject_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_subject_docker_compose
ON subject_docker (compose_project, compose_service);

CREATE INDEX IF NOT EXISTS idx_subject_docker_container_name
ON subject_docker (container_name);

CREATE TABLE IF NOT EXISTS subject_host (
    subject_id TEXT PRIMARY KEY,
    systemd_unit TEXT,
    listen_proto TEXT,
    listen_port INTEGER,
    executable TEXT,
    process_name TEXT,
    source_json TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (subject_id) REFERENCES subjects(subject_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_subject_host_systemd_unit
ON subject_host (systemd_unit);

CREATE INDEX IF NOT EXISTS idx_subject_host_listener
ON subject_host (listen_proto, listen_port);

CREATE TABLE IF NOT EXISTS subject_fwrouter (
    subject_id TEXT PRIMARY KEY,
    component_name TEXT NOT NULL,
    source_json TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (subject_id) REFERENCES subjects(subject_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_subject_fwrouter_component
ON subject_fwrouter (component_name);


CREATE TABLE IF NOT EXISTS traffic_monthly (
    subject_id TEXT NOT NULL,
    period_month TEXT NOT NULL,
    direct_rx_bytes INTEGER NOT NULL DEFAULT 0,
    direct_tx_bytes INTEGER NOT NULL DEFAULT 0,
    vpn_rx_bytes INTEGER NOT NULL DEFAULT 0,
    vpn_tx_bytes INTEGER NOT NULL DEFAULT 0,
    blocked_rx_bytes INTEGER NOT NULL DEFAULT 0,
    blocked_tx_bytes INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (subject_id, period_month),
    FOREIGN KEY (subject_id) REFERENCES subjects(subject_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_traffic_monthly_period
ON traffic_monthly (period_month);

CREATE TABLE IF NOT EXISTS traffic_counter_snapshots (
    counter_key TEXT PRIMARY KEY,
    subject_id TEXT,
    path TEXT NOT NULL,
    rx_bytes INTEGER NOT NULL DEFAULT 0,
    tx_bytes INTEGER NOT NULL DEFAULT 0,
    collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    metadata_json TEXT,
    CHECK (path IN ('direct', 'vpn', 'blocked')),
    FOREIGN KEY (subject_id) REFERENCES subjects(subject_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_traffic_counter_snapshots_subject
ON traffic_counter_snapshots (subject_id, collected_at DESC);

CREATE TABLE IF NOT EXISTS servers (
    server_id TEXT PRIMARY KEY,
    server_name TEXT NOT NULL,
    provider_name TEXT,
    country_code TEXT,
    region TEXT,
    raw_json TEXT,
    inventory_state TEXT NOT NULL DEFAULT 'active',
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    missing_since TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (inventory_state IN ('active', 'missing', 'deleted'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_servers_server_name
ON servers (server_name);

CREATE INDEX IF NOT EXISTS idx_servers_inventory_state
ON servers (inventory_state, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS server_preferences (
    server_id TEXT PRIMARY KEY,
    vpn_auto INTEGER NOT NULL DEFAULT 0,
    vpn_auto_priority INTEGER NOT NULL DEFAULT 0,
    global_list INTEGER NOT NULL DEFAULT 1,
    remembered_until TEXT,
    manually_deleted_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (vpn_auto IN (0, 1)),
    CHECK (vpn_auto_priority >= -1 AND vpn_auto_priority <= 5),
    CHECK (global_list IN (0, 1)),
    FOREIGN KEY (server_id) REFERENCES servers(server_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_server_preferences_vpn_auto
ON server_preferences (vpn_auto);

CREATE INDEX IF NOT EXISTS idx_server_preferences_global_list
ON server_preferences (global_list);

CREATE TABLE IF NOT EXISTS server_ping_state (
    server_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'unknown',
    last_ping_ms INTEGER,
    checked_at TEXT,
    checked_by TEXT,
    error_code TEXT,
    error_message TEXT,
    metadata_json TEXT,
    CHECK (status IN ('unknown', 'success', 'failed', 'skipped')),
    FOREIGN KEY (server_id) REFERENCES servers(server_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_server_ping_state_status
ON server_ping_state (status, checked_at DESC);

CREATE TABLE IF NOT EXISTS server_custom_https_proxy (
    server_id TEXT PRIMARY KEY,
    proxy_type TEXT NOT NULL DEFAULT 'http',
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    username TEXT,
    password TEXT,
    tls INTEGER NOT NULL DEFAULT 1,
    sni TEXT,
    skip_cert_verify INTEGER NOT NULL DEFAULT 0,
    path TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (proxy_type IN ('http', 'socks5')),
    CHECK (port >= 1 AND port <= 65535),
    CHECK (tls IN (0, 1)),
    CHECK (skip_cert_verify IN (0, 1)),
    FOREIGN KEY (server_id) REFERENCES servers(server_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS routing_global_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    desired_mode TEXT NOT NULL DEFAULT 'direct',
    applied_mode TEXT,
    selective_default TEXT NOT NULL DEFAULT 'direct',
    server_mode TEXT NOT NULL DEFAULT 'auto',
    desired_fixed_server_id TEXT,
    applied_fixed_server_id TEXT,
    active_auto_server_id TEXT,
    apply_state TEXT NOT NULL DEFAULT 'clean',
    error_code TEXT,
    error_message TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (desired_mode IN ('direct', 'selective', 'vpn')),
    CHECK (applied_mode IS NULL OR applied_mode IN ('direct', 'selective', 'vpn')),
    CHECK (selective_default IN ('direct', 'vpn')),
    CHECK (server_mode IN ('auto', 'fixed')),
    CHECK (apply_state IN ('clean', 'pending', 'applying', 'failed')),
    FOREIGN KEY (desired_fixed_server_id) REFERENCES servers(server_id) ON DELETE SET NULL,
    FOREIGN KEY (applied_fixed_server_id) REFERENCES servers(server_id) ON DELETE SET NULL,
    FOREIGN KEY (active_auto_server_id) REFERENCES servers(server_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS subject_server_overrides (
    subject_id TEXT PRIMARY KEY,
    selected_server_id TEXT,
    selected_until TEXT,
    apply_state TEXT NOT NULL DEFAULT 'clean',
    error_code TEXT,
    error_message TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (apply_state IN ('clean', 'pending', 'applying', 'failed')),
    FOREIGN KEY (subject_id) REFERENCES subjects(subject_id) ON DELETE CASCADE,
    FOREIGN KEY (selected_server_id) REFERENCES servers(server_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_subject_server_overrides_until
ON subject_server_overrides (selected_until);

CREATE TABLE IF NOT EXISTS subject_user_overrides (
    subject_id TEXT PRIMARY KEY,
    override_mode TEXT,
    override_until TEXT,
    created_by TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (override_mode IS NULL OR override_mode IN ('direct', 'selective', 'vpn')),
    FOREIGN KEY (subject_id) REFERENCES subjects(subject_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_subject_user_overrides_until
ON subject_user_overrides (override_until);

CREATE TABLE IF NOT EXISTS subscription_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    url TEXT,
    status TEXT NOT NULL DEFAULT 'not_configured',
    last_refresh_at TEXT,
    last_success_at TEXT,
    server_inventory_updated_at TEXT,
    error_code TEXT,
    error_message TEXT,
    metadata_json TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('not_configured', 'idle', 'running', 'success', 'failed'))
);

CREATE TABLE IF NOT EXISTS subscription_accounts (
    account_id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (enabled IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_subscription_accounts_enabled
ON subscription_accounts (enabled, slug);

CREATE TABLE IF NOT EXISTS subscription_clients (
    client_id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    token TEXT NOT NULL UNIQUE,
    app_type TEXT NOT NULL DEFAULT 'auto',
    enabled INTEGER NOT NULL DEFAULT 1,
    display_name TEXT,
    last_seen_at TEXT,
    last_user_agent TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (enabled IN (0, 1)),
    FOREIGN KEY (account_id) REFERENCES subscription_accounts(account_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_subscription_clients_account_enabled
ON subscription_clients (account_id, enabled, token);

CREATE TABLE IF NOT EXISTS rules_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    manual_draft_path TEXT,
    manual_active_path TEXT,
    static_direct_path TEXT,
    big_direct_path TEXT,
    big_vpn_path TEXT,
    effective_json_path TEXT,
    effective_text_path TEXT,
    metadata_path TEXT,
    selective_default TEXT NOT NULL DEFAULT 'direct',
    last_apply_job_id TEXT,
    last_update_job_id TEXT,
    status TEXT NOT NULL DEFAULT 'not_configured',
    last_success_at TEXT,
    last_failed_at TEXT,
    error_code TEXT,
    error_message TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (selective_default IN ('direct', 'vpn')),
    CHECK (status IN ('not_configured', 'idle', 'running', 'clean', 'pending', 'applying', 'success', 'failed')),
    FOREIGN KEY (last_apply_job_id) REFERENCES jobs(job_id) ON DELETE SET NULL,
    FOREIGN KEY (last_update_job_id) REFERENCES jobs(job_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS rules_metadata (
    ruleset_id TEXT PRIMARY KEY,
    ruleset_type TEXT NOT NULL,
    version_name TEXT,
    source_url TEXT,
    active_path TEXT,
    downloaded_at TEXT,
    activated_at TEXT,
    status TEXT NOT NULL DEFAULT 'not_configured',
    last_success_at TEXT,
    last_failed_at TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    last_job_id TEXT,
    metadata_json TEXT,
    CHECK (ruleset_type IN ('manual', 'static_direct', 'big_direct', 'big_vpn', 'effective')),
    CHECK (status IN ('not_configured', 'idle', 'running', 'active', 'inactive', 'success', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_rules_metadata_type
ON rules_metadata (ruleset_type, status);

CREATE TABLE IF NOT EXISTS apply_versions (
    apply_id TEXT PRIMARY KEY,
    job_id TEXT,
    manifest_path TEXT NOT NULL,
    artifact_dir TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    promoted_at TEXT,
    status TEXT NOT NULL,
    summary_json TEXT,
    CHECK (status IN ('generated', 'applied', 'rolled_back', 'failed')),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_apply_versions_created
ON apply_versions (created_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    lock_key TEXT,
    requested_by TEXT,
    input_json TEXT,
    result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    artifact_dir TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (status IN ('queued', 'running', 'success', 'failed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_type_created
ON jobs (job_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created
ON jobs (status, created_at DESC);

CREATE TABLE IF NOT EXISTS operational_logs (
    event_id TEXT PRIMARY KEY,
    level TEXT NOT NULL DEFAULT 'info',
    event_type TEXT NOT NULL,
    subject_id TEXT,
    message TEXT NOT NULL,
    details_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (level IN ('debug', 'info', 'warning', 'error'))
);

CREATE INDEX IF NOT EXISTS idx_operational_logs_created
ON operational_logs (created_at DESC);

INSERT INTO schema_meta (key, value, updated_at)
VALUES ('schema_version', '7', CURRENT_TIMESTAMP)
ON CONFLICT(key) DO UPDATE SET
    value = excluded.value,
    updated_at = excluded.updated_at
WHERE schema_meta.value <> excluded.value;

INSERT OR IGNORE INTO modules (module_name, desired_state, runtime_state, status_text)
VALUES
    ('core', 'enabled', 'not_configured', 'FWRouter core is not initialized yet.'),
    ('vpn', 'enabled', 'not_configured', 'VPN module is not initialized yet.'),
    ('xray', 'enabled', 'not_configured', 'Xray module is not initialized yet.'),
    ('tailscale', 'enabled', 'not_configured', 'Tailscale module is not managed by FWRouter yet.'),
    ('watchdog', 'enabled', 'not_configured', 'Watchdog is not initialized yet.'),
    ('selector', 'enabled', 'not_configured', 'VPN auto-selector is not initialized yet.'),
    ('subscription', 'enabled', 'not_configured', 'Subscription module is not initialized yet.');
