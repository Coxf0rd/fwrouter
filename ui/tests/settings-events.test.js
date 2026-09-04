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
assert.match(indexHtml, /fwrouter-i18n\.js\?v=20260904a/);
assert.match(settingsJs, /fetchJson\("\/api\/v2\/events\/recent\?limit=300"/);
assert.match(settingsJs, /fetchApiV2\(`\/logs\/operational\?limit=300/);
assert.match(settingsJs, /fetchApiV2\(`\/logs\/technical\?limit=300/);

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
