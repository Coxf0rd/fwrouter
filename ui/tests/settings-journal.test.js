const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
global.window = global;
global.document = {
  documentElement: { dataset: { locale: "ru" }, lang: "ru", style: { setProperty: () => {} } },
  addEventListener: () => {},
  dispatchEvent: () => true,
  querySelectorAll: () => [],
};
global.localStorage = { getItem: () => null, setItem: () => {} };
global.FwrouterUI = {
  escapeHtml: (value) => String(value ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
  translateBackendMessage: (value) => String(value || ""),
};

function loadScript(relativePath) {
  vm.runInThisContext(fs.readFileSync(path.join(root, relativePath), "utf8"), { filename: relativePath });
}

loadScript("static/js/fwrouter-i18n.js");
loadScript("static/js/fwrouter-settings-events.js");
loadScript("static/js/fwrouter-settings-journal.js");

const id = "subject:8de3d2f8-2320-4407-a300-1e2b8915f118";
const item = global.FwrouterSettingsEvents.toTypedEvent({
  event_id: "event-1",
  event_code: "HEALTH_MEMBER_RECOVERED",
  event_type: "legacy_name",
  severity: "info",
  entity_type: "subject",
  entity_id: id,
  entity_label: "NikitaPlus",
  timestamp: "2026-09-27T10:00:00Z",
}, "operational");
const html = global.FwrouterSettingsJournal.renderSelectedEventContextHtml(item);
const primary = html.split('<details class="settings-event-context__details')[0];
assert.match(primary, /NikitaPlus/);
assert.doesNotMatch(primary, /8de3d2f8-2320-4407-a300-1e2b8915f118/);
assert.match(html, /8de3d2f8-2320-4407-a300-1e2b8915f118/);

const withoutAlias = global.FwrouterSettingsJournal.renderSelectedEventContextHtml({ ...item, entity_label: "" });
const withoutAliasPrimary = withoutAlias.split('<details class="settings-event-context__details')[0];
assert.doesNotMatch(withoutAliasPrimary, /8de3d2f8-2320-4407-a300-1e2b8915f118/);
assert.doesNotMatch(withoutAliasPrimary, /journal\.field\.entity|Объект|Entity/);

console.log("settings journal entity presentation ok");
