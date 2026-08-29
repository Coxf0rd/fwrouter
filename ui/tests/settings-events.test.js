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
loadScript("static/js/fwrouter-settings-events.js");

const events = global.FwrouterSettingsEvents;
const i18n = global.FwrouterI18n;

const indexHtml = fs.readFileSync(path.join(root, "index.html"), "utf8");
const tabSources = Array.from(indexHtml.matchAll(/data-log-source="([^"]+)"/g)).map((match) => match[1]);
assert.deepStrictEqual(tabSources, ["all", "error", "watchdog", "routing", "server", "system", "rules", "controls"]);

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
  operational({ event_type: "server_warning", category: "server", level: "warning" }),
  technical({ event_type: "scheduler_failed", component: "maintenance", level: "error" }),
  operational({ event_type: "watchdog_switch_suppressed", category: "server", level: "info" }),
  technical({ event_type: "dataplane_check_failed", component: "dataplane", level: "warning" }),
  operational({ event_type: "vpn_auto_server_switched", category: "routing", level: "info" }),
  technical({ event_type: "runtime_convergence_completed", component: "runtime", level: "info" }),
  operational({ event_type: "manual_rules_apply_completed", category: "rules", level: "info" }),
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
  ["scheduler_failed", "dataplane_check_failed", "runtime_convergence_completed"],
);

assert.strictEqual(events.isJournalTab("rules"), false);
assert.strictEqual(events.isJournalTab("controls"), false);
assert.strictEqual(events.isJournalTab("server"), true);

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
