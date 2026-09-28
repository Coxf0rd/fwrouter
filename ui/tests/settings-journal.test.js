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
assert.doesNotMatch(rowHtml, /ошибка → доступен/);
assert.doesNotMatch(rowHtml, /8de3d2f8-2320-4407-a300-1e2b8915f118/);

const disclosureEvent = global.FwrouterSettingsEvents.toTypedEvent({
  event_id: "safe-disclosure",
  event_code: "server.preferences_changed",
  event_class: "audit",
  event_type: "preferences_changed",
  actor: "admin:operator",
  source: "routing_admin_api",
  result: "success",
  entity_type: "server",
  entity_label: "Edge Europe",
  details: {
    actor_attribution: "caller_supplied",
    changed_fields: ["vpn_auto", "server_ids"],
    previous_value: { vpn_auto: false, server_ids: ["123e4567-e89b-12d3-a456-426614174000"] },
    new_value: { vpn_auto: true },
    result: { raw_payload: "credential-token" },
    stack: "/srv/private/path",
  },
}, "audit");
const disclosureHtml = global.FwrouterSettingsJournal.renderSelectedEventContextHtml(disclosureEvent);
const ordinaryDisclosure = disclosureHtml.split('<details class="settings-event-context__details')[0];
assert.match(disclosureEvent.title, /Edge Europe/);
assert.doesNotMatch(ordinaryDisclosure, /123e4567-e89b-12d3-a456-426614174000|credential-token|\/srv\/private\/path/);
assert.match(ordinaryDisclosure, /VPN-auto: Нет → Да/);
assert.match(ordinaryDisclosure, /admin:operator/);
assert.match(ordinaryDisclosure, /Панель маршрутизации/);
assert.match(ordinaryDisclosure, /Действие пользователя/);
assert.match(ordinaryDisclosure, /VPN-auto/);
assert.match(ordinaryDisclosure, /Успешно/);
assert.match(disclosureHtml, /123e4567-e89b-12d3-a456-426614174000|credential-token|\/srv\/private\/path/);

global.FwrouterI18n.setLocale("en");
const disclosureEventEn = global.FwrouterSettingsEvents.toTypedEvent({
  event_id: "safe-disclosure-en",
  event_code: "client.alias_changed",
  event_class: "audit",
  event_type: "alias_changed",
  actor: "admin:operator",
  source: "api",
  result: "success",
  entity_type: "subject",
  entity_label: "MacBook Air",
  details: {
    actor_attribution: "caller_supplied",
    previous_value: { alias_present: true, alias_label: "Old MacBook" },
    new_value: { alias_present: true, alias_label: "MacBook Air" },
  },
}, "audit");
const disclosureHtmlEn = global.FwrouterSettingsJournal.renderSelectedEventContextHtml(disclosureEventEn);
const ordinaryDisclosureEn = disclosureHtmlEn.split('<details class="settings-event-context__details')[0];
assert.match(disclosureEventEn.title, /MacBook Air/);
assert.match(ordinaryDisclosureEn, /Client name: Old MacBook → MacBook Air/);
assert.match(ordinaryDisclosureEn, /API/);
assert.match(ordinaryDisclosureEn, /User action/);
assert.match(ordinaryDisclosureEn, /admin:operator/);
assert.match(ordinaryDisclosureEn, /Succeeded/);
global.FwrouterI18n.setLocale("ru");

console.log("settings journal entity presentation ok");
