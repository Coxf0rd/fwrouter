const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");

global.window = global;
global.localStorage = {
  getItem: () => null,
  setItem: () => {},
};
global.document = {
  documentElement: {
    dataset: { locale: "ru" },
    lang: "ru",
    style: { setProperty: () => {} },
  },
  addEventListener: () => {},
  dispatchEvent: () => true,
  querySelectorAll: () => [],
};
global.FwrouterUI = {
  translateBackendMessage: (message) => String(message || ""),
};

function loadScript(relativePath) {
  const source = fs.readFileSync(path.join(root, relativePath), "utf8");
  vm.runInThisContext(source, { filename: relativePath });
}

loadScript("static/js/fwrouter-i18n.js");
loadScript("static/js/fwrouter-labels.js");
loadScript("static/js/fwrouter-settings-events.js");

const events = global.FwrouterSettingsEvents;
const i18n = global.FwrouterI18n;

const indexHtml = fs.readFileSync(path.join(root, "index.html"), "utf8");
const settingsJs = fs.readFileSync(path.join(root, "static/js/settings.js"), "utf8");
const tabSources = Array.from(indexHtml.matchAll(/data-log-source="([^"]+)"/g)).map((match) => match[1]);
assert.deepStrictEqual(tabSources, ["all", "error", "watchdog", "routing", "server", "system", "diagnostic", "rules", "diagnostics", "controls"]);
assert.match(indexHtml, /settings-view\.css\?v=20260906d/);
assert.match(indexHtml, /fwrouter-i18n\.js\?v=20260906c/);
assert.match(indexHtml, /fwrouter-labels\.js\?v=20260905b/);
assert.match(indexHtml, /fwrouter-settings-inventory\.js\?v=20260906d/);
assert.match(indexHtml, /fwrouter-settings-events\.js\?v=20260905c/);
assert.match(indexHtml, /fwrouter-settings-domain-state\.js\?v=20260906a/);
assert.match(indexHtml, /settings\.js\?v=20260906d/);
assert.match(indexHtml, /<details class="admin-advanced settings-rules-editor">/);
assert.doesNotMatch(indexHtml, /settings-rules-editor" open/);
assert.match(indexHtml, /id="vpnSubscriptionUrlList"/);
assert.match(indexHtml, /id="vpnSubscriptionAddUrl"/);
assert.match(indexHtml, /id="vpnSubscriptionBatchResult"/);
assert.doesNotMatch(indexHtml, /<textarea[^>]+vpnSubscription/i);
assert.match(settingsJs, /fetchJson\("\/api\/v2\/events\/recent\?limit=300"/);
assert.match(settingsJs, /apiPathSupported\("\/api\/v2\/events\/recent"\)/);
assert.match(settingsJs, /apiPathSupported\("\/api\/v2\/diagnose"\)/);
assert.match(settingsJs, /const settingsReadCache =/);
assert.match(settingsJs, /events: 60000/);
assert.match(settingsJs, /function cacheFresh\(entry, ttlMs\)/);
assert.match(settingsJs, /function invalidateSettingsCaches\(scopes\)/);
assert.match(settingsJs, /function scheduleRulesPolicyRefresh\(rules, cacheEntry\)/);
assert.match(settingsJs, /if \(settingsTab !== "rules"\) return/);
assert.match(settingsJs, /if \(!opts\.force && cacheEntry\.payload\)/);
assert.match(settingsJs, /if \(!opts\.force && cacheEntry\.promise\) return cacheEntry\.promise/);
assert.match(settingsJs, /loadSettingsLogs\(\{ source: settingsTab, force: true \}\)/);
assert.match(settingsJs, /loadDiagnostics\(\{ force: true \}\)/);
assert.match(settingsJs, /loadRules\(\{ force: true \}\)/);
assert.match(settingsJs, /fetchApiV2\(`\/logs\/operational\?limit=300/);
assert.match(settingsJs, /fetchApiV2\(`\/logs\/technical\?limit=300/);
assert.match(settingsJs, /"mihomo", "tailscale"/);
assert.match(settingsJs, /function collectVpnSubscriptionUrls\(\)/);
assert.match(settingsJs, /fwrouter\.settings\.vpnSubscriptionUrls/);
assert.match(settingsJs, /function getStoredVpnSubscriptionUrls\(\)/);
assert.match(settingsJs, /function setStoredVpnSubscriptionUrls\(urls\)/);
assert.match(settingsJs, /function populateVpnSubscriptionFields\(urls\)/);
assert.match(settingsJs, /const urls = collectVpnSubscriptionUrls\(\);/);
assert.match(settingsJs, /setStoredVpnSubscriptionUrls\(urls\);/);
assert.match(settingsJs, /body: JSON\.stringify\(\{\s*urls,/s);
assert.match(settingsJs, /if \(ev\.key !== "Enter"\) return;[\s\S]*ev\.preventDefault\(\);[\s\S]*addVpnSubscriptionField\(\);/);
assert.doesNotMatch(settingsJs, /vpnSubscriptionUrl"\)\?\.addEventListener\("keydown"[\s\S]*saveVpnSubscriptionUrl\(\);/);
assert.match(settingsJs, /loadSettingsProxyServers\(true\)/);

["ru", "en"].forEach((locale) => {
  i18n.setLocale(locale);
  [
    "html.settings.save_subscription",
    "html.settings.save_subscriptions",
    "html.settings.add_subscription_field",
    "html.settings.remove_subscription_field",
    "settings.subscription.batch.added",
    "settings.subscription.batch.imported",
    "settings.subscription.batch.existing",
    "settings.subscription.batch.errors",
    "settings.subscription.batch.details",
  ].forEach((key) => {
    assert.notStrictEqual(i18n.t(key, { count: 2, index: 1 }), key, `${key} should be localized for ${locale}`);
  });
});
i18n.setLocale("ru");

function operational(overrides) {
  return events.toLegacyEvent({
    event_id: overrides.event_id || overrides.event_type || "event",
    created_at: overrides.created_at || "2026-08-29T00:00:00Z",
    level: overrides.level || "info",
    event_type: overrides.event_type || "event",
    category: overrides.category,
    subject_id: overrides.subject_id,
    details: overrides.details || {},
    message: overrides.message || overrides.event_type || "event",
  });
}

function technical(overrides) {
  return events.toLegacyTechnicalEvent({
    timestamp: overrides.timestamp || "2026-08-29T00:00:00Z",
    level: overrides.level || "info",
    component: overrides.component || "system",
    event_type: overrides.event_type || "event",
    category: overrides.category,
    details: overrides.details || {},
    message: overrides.message || overrides.event_type || "event",
  });
}

const sample = [
  events.toTypedEvent({ event_id: "server_warning", event_type: "server_warning", entity_type: "server", level: "warning" }, "operational"),
  events.toTypedEvent({ event_id: "scheduler_failed", event_type: "scheduler_failed", entity_type: "system", level: "error" }, "operational"),
  events.toTypedEvent({ event_id: "watchdog_switch_suppressed", event_type: "watchdog_switch_suppressed", entity_type: "watchdog", level: "info" }, "operational"),
  events.toTypedEvent({ event_id: "dataplane_check_failed", event_type: "dataplane_check_failed", entity_type: "routing", level: "warning" }, "operational"),
  events.toTypedEvent({ event_id: "vpn_auto_server_switched", event_type: "vpn_auto_server_switched", entity_type: "vpn", level: "info" }, "operational"),
  events.toTypedEvent({ event_id: "runtime_convergence_completed", event_type: "runtime_convergence_completed", entity_type: "system", level: "info" }, "operational"),
  events.toTypedEvent({ event_id: "manual_rules_apply_completed", event_type: "manual_rules_apply_completed", entity_type: "rules", level: "info" }, "operational"),
];

assert.strictEqual(sample.filter((item) => events.matchesJournalTab(item, "all")).length, sample.length);
assert.deepStrictEqual(
  sample.filter((item) => events.matchesJournalTab(item, "error")).map((item) => item.event_type),
  ["server_warning", "scheduler_failed", "dataplane_check_failed"],
);

assert.deepStrictEqual(
  sample.filter((item) => events.matchesJournalTab(item, "watchdog")).map((item) => item.event_type),
  ["watchdog_switch_suppressed"],
);

assert.deepStrictEqual(
  sample.filter((item) => events.matchesJournalTab(item, "routing")).map((item) => item.event_type),
  ["dataplane_check_failed", "manual_rules_apply_completed"],
);

assert.deepStrictEqual(
  sample.filter((item) => events.matchesJournalTab(item, "server")).map((item) => item.event_type),
  ["server_warning", "vpn_auto_server_switched"],
);

assert.deepStrictEqual(
  sample.filter((item) => events.matchesJournalTab(item, "system")).map((item) => item.event_type),
  ["scheduler_failed", "runtime_convergence_completed"],
);

assert.strictEqual(events.isJournalTab("rules"), false);
assert.strictEqual(events.isJournalTab("controls"), false);
assert.strictEqual(events.isJournalTab("diagnostics"), false);
assert.strictEqual(events.isJournalTab("diagnostic"), true);
assert.strictEqual(events.isJournalTab("server"), true);

const legacyTechnical = technical({ component: "watchdog", event_type: "watchdog_switch_suppressed", level: "warning" });
assert.strictEqual(events.matchesJournalTab(legacyTechnical, "diagnostic"), true);
assert.strictEqual(events.matchesJournalTab(legacyTechnical, "watchdog"), false);
assert.strictEqual(events.matchesJournalTab(legacyTechnical, "all"), false);

const typed = [
  events.toTypedEvent({
    event_id: "a1",
    timestamp: "2026-08-29T00:00:00Z",
    action: "config_change",
    actor: "user:admin",
    source: "api",
    entity_type: "routing",
    entity_id: "global",
    result: "success",
  }, "audit"),
  events.toTypedEvent({
    event_id: "o1",
    timestamp: "2026-08-29T00:00:01Z",
    severity: "warning",
    event_type: "reconcile_drift",
    entity_type: "routing",
    entity_id: "global",
    message: "Routing drift.",
  }, "operational"),
  events.toTypedEvent({
    event_id: "d1",
    timestamp: "2026-08-29T00:00:02Z",
    severity: "debug",
    event_type: "probe_result",
    entity_type: "vpn",
    entity_id: "vpn",
    message: "Probe payload.",
  }, "diagnostic"),
];

assert.deepStrictEqual(
  typed.filter((item) => events.matchesJournalTab(item, "all")).map((item) => item.id),
  ["a1", "o1"],
);
assert.deepStrictEqual(
  typed.filter((item) => events.matchesJournalTab(item, "diagnostic")).map((item) => item.id),
  ["d1"],
);
assert.deepStrictEqual(
  typed.filter((item) => events.matchesJournalTab(item, "routing")).map((item) => item.id),
  ["a1", "o1"],
);
assert.strictEqual(
  events.toTypedEvent({
    event_id: "x1",
    timestamp: "2026-08-29T00:00:03Z",
    severity: "error",
    event_type: "runtime_failed",
    entity_type: "xray",
    entity_id: "xray",
    message: "Xray binding failed",
  }, "operational").message,
  "Подключение внешних клиентов недоступно",
);

assert.strictEqual(
  events.toTypedEvent({
    event_id: "x2",
    timestamp: "2026-08-29T00:00:04Z",
    severity: "info",
    event_type: "xray_binding_materialized",
    entity_type: "xray",
    entity_id: "xray:alice",
    message: "xray_binding_materialized",
  }, "operational").message,
  "Маршрут внешнего клиента обновлён",
);

const userVisibleEventTypes = [
  "apply_dry_run_completed",
  "apply_failed",
  "apply_finished",
  "control_plane_database_rebuilt",
  "control_plane_maintenance_completed",
  "control_plane_snapshot_imported",
  "core_bypass_disabled",
  "core_bypass_enabled",
  "database_rebuild_schema_mismatch",
  "database_schema_mismatch_detected",
  "external_action",
  "external_collector_failed",
  "external_collector_scheduler_failed",
  "global_fixed_server_applied",
  "global_fixed_server_cleared",
  "global_fixed_server_expired",
  "job_debug",
  "job_handler_exception",
  "maintenance_scheduler_failed",
  "mihomo_candidate_config_validated",
  "mihomo_candidate_config_written",
  "mihomo_candidate_promote_failed",
  "mihomo_candidate_promoted",
  "mihomo_selective_default_fast_reconciled",
  "probe_result",
  "reconcile_drift",
  "mihomo_reconcile_failed",
  "mihomo_reconcile_skipped",
  "mihomo_reconciled",
  "mutation_set_global_mode_success",
  "routing_artifact_drift_detected",
  "routing_changed",
  "routing_live_drift_detected",
  "rules_full_update_dnsmasq_failed",
  "rules_full_update_failed",
  "rules_full_update_fetch_failed",
  "rules_full_update_noop",
  "rules_full_update_policy_failed",
  "rules_full_update_succeeded",
  "rules_full_update_version_noop",
  "rules_manual_update_dnsmasq_failed",
  "runtime_convergence_cooldown_entered",
  "runtime_convergence_scheduler_failed",
  "runtime_enforcement_probe_failed",
  "runtime_failed",
  "runtime_state_cleanup_completed",
  "startup_dnsmasq_reconcile_failed",
  "startup_intended_routing_reapplied",
  "startup_live_routing_recovered",
  "startup_live_routing_recovery_deferred",
  "startup_live_routing_recovery_failed",
  "startup_mihomo_selector_restore_failed",
  "startup_mihomo_selector_restored",
  "startup_scoped_subject_routing_reapplied",
  "subject_inventory_scheduler_failed",
  "subject_inventory_sync_warning",
  "subject_inventory_synced",
  "subject_server_override_expired",
  "subject_taxonomy_normalized",
  "subject_user_override_expired",
  "subscription_refresh_applied",
  "subscription_refresh_apply_failed",
  "subscription_refresh_completed",
  "subscription_refresh_failed",
  "system_subject_deleted",
  "traffic_accounting_collected",
  "traffic_accounting_completed",
  "traffic_accounting_failed",
  "traffic_collection_partial_failure",
  "traffic_collection_script_error",
  "traffic_collection_script_failed",
  "traffic_collection_script_invalid_json",
  "traffic_collection_script_invalid_shape",
  "traffic_history_cleanup_completed",
  "vpn_auto_server_switched",
  "vpn_watchdog_fail_open_direct",
  "vpn_watchdog_failover",
  "vpn_watchdog_healthy",
  "vpn_watchdog_no_traffic",
  "watchdog_switch_applied",
  "watchdog_switch_suppressed",
  "watchdog_scheduler_failed",
  "xray_binding_materialization_failed",
  "xray_binding_materialized",
  "xray_client_alias_updated",
  "xray_client_create_blocked",
  "xray_client_created",
  "xray_client_deleted",
  "xray_public_subscription_reconcile_crashed",
  "xray_public_subscription_reconcile_failed",
  "xray_reloaded",
  "xray_service_error",
  "xray_subjects_synced",
  "manual_rules_apply_completed",
  "manual_rules_apply_failed",
];

for (const locale of ["ru", "en"]) {
  i18n.setLocale(locale);
  userVisibleEventTypes.forEach((eventType) => {
    const label = events.eventTypeLabel(eventType);
    assert.notStrictEqual(label, eventType, `${locale} missing label for ${eventType}`);
    assert.doesNotMatch(label, /^events\./);
    assert.doesNotMatch(label, /^(mihomo|xray|tailscale|docker)_/i);
    const rendered = events.toTypedEvent({
      event_id: `${locale}-${eventType}`,
      timestamp: "2026-09-05T00:00:00Z",
      event_type: eventType,
      entity_type: eventType.includes("xray") ? "xray" : eventType.includes("vpn") || eventType.includes("mihomo") ? "vpn" : "system",
      message: eventType,
      severity: "info",
    }, "operational");
    assert.notStrictEqual(rendered.message, eventType, `${locale} raw event rendered for ${eventType}`);
    assert.doesNotMatch(rendered.message, /^events\./);
    assert.doesNotMatch(rendered.message, /Mihomo|Xray|Tailscale|Docker/);
  });
}
i18n.setLocale("ru");

const staleFreshness = events.freshnessFor("2026-01-01T00:00:00Z", {
  stale: true,
  now: new Date("2026-09-05T00:00:00Z"),
});
assert.strictEqual(staleFreshness.state, "stale");
assert.match(staleFreshness.text, /Данные устарели/);

assert.strictEqual(
  events.toTypedEvent({
    event_id: "d2",
    timestamp: "2026-08-29T00:00:05Z",
    severity: "error",
    event_type: "probe_result",
    message: "raw probe failed",
  }, "diagnostic").level,
  "info",
);
const diagnosticWithEntity = events.toTypedEvent({
  event_id: "d3",
  timestamp: "2026-08-29T00:00:06Z",
  severity: "error",
  event_type: "probe_result",
  entity_type: "vpn",
  entity_id: "vpn",
}, "diagnostic");
assert.strictEqual(events.matchesJournalTab(diagnosticWithEntity, "error"), false);
assert.strictEqual(events.matchesJournalTab(diagnosticWithEntity, "diagnostic"), true);

const grouped = events.groupRepeatedEvents([
  events.toTypedEvent({ event_id: "w1", timestamp: "2026-08-29T00:00:12Z", severity: "info", event_type: "no_traffic", entity_type: "watchdog", entity_id: "vpn" }, "diagnostic"),
  events.toTypedEvent({ event_id: "w2", timestamp: "2026-08-29T00:00:11Z", severity: "info", event_type: "no_traffic", entity_type: "watchdog", entity_id: "vpn" }, "diagnostic"),
  events.toTypedEvent({ event_id: "w3", timestamp: "2026-08-29T00:00:10Z", severity: "info", event_type: "no_traffic", entity_type: "watchdog", entity_id: "vpn" }, "diagnostic"),
]);
assert.strictEqual(grouped.length, 1);
assert.strictEqual(grouped[0].repeat_count, 3);

const labels = global.FwrouterLabels;
assert.deepStrictEqual(
  [labels.presentationState({ is_active: false }).state, labels.presentationState({ is_active: false }).severity],
  ["inactive", "inactive"],
);
assert.deepStrictEqual(
  [labels.presentationState({ reconcile_state: "stale" }).state, labels.presentationState({ reconcile_state: "stale" }).severity],
  ["warning", "warning"],
);
assert.deepStrictEqual(
  [labels.presentationState({ reconcile_state: "drift" }).state, labels.presentationState({ reconcile_state: "drift" }).severity],
  ["degraded", "warning"],
);
assert.deepStrictEqual(
  [labels.presentationState({ runtime_state: "failed" }).state, labels.presentationState({ runtime_state: "failed" }).severity],
  ["failed", "error"],
);

assert.strictEqual(i18n.t("events.category.all"), "Все");
assert.strictEqual(i18n.t("events.category.error"), "Ошибки");
assert.strictEqual(i18n.t("events.category.routing"), "Маршрутизация");
assert.strictEqual(i18n.t("events.category.server"), "Серверы");
assert.strictEqual(i18n.t("events.category.system"), "Система");
assert.strictEqual(i18n.t("events.category.controls"), "Управление");
document.documentElement.dataset.locale = "en";
assert.strictEqual(i18n.t("events.category.all"), "All");
assert.strictEqual(i18n.t("events.category.error"), "Errors");
assert.strictEqual(i18n.t("events.category.routing"), "Routing");
assert.strictEqual(i18n.t("events.category.server"), "Servers");
assert.strictEqual(i18n.t("events.category.system"), "System");
assert.strictEqual(i18n.t("events.category.controls"), "Management");

console.log("settings-events journal tab semantics ok");
