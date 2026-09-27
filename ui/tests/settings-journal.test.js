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

const unknownLegacy = global.FwrouterSettingsEvents.toTypedEvent({
  event_id: "unknown-legacy",
  event_type: "untranslated_old_event",
  message: "raw mixed-language technical payload",
  entity_type: "subject",
  entity_id: "subject:private-runtime-id",
}, "operational");
const unknownHtml = global.FwrouterSettingsJournal.renderSelectedEventContextHtml(unknownLegacy);
const unknownPrimary = unknownHtml.split('<details class="settings-event-context__details')[0];
assert.strictEqual(unknownLegacy.message, global.FwrouterI18n.t("events.type.default"));
assert.doesNotMatch(unknownPrimary, /raw mixed-language technical payload|private-runtime-id/);
assert.match(unknownHtml, /raw mixed-language technical payload/);

const rowHtml = global.FwrouterSettingsJournal.renderEventsHtml([
  { ...item, ts: "2026-09-27T10:00:00Z", message: item.title, safe_summary: "Состояние участника: ошибка → доступен" },
], 0, () => 0);
assert.match(rowHtml, /\d{2}\.\d{2}\.\d{2} \d{2}:\d{2}/);
assert.match(rowHtml, /NikitaPlus/);
assert.match(rowHtml, /ошибка → доступен/);
assert.doesNotMatch(rowHtml, /8de3d2f8-2320-4407-a300-1e2b8915f118/);

console.log("settings journal entity presentation ok");
