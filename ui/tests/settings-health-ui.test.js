const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
global.window = global;
global.document = {
  documentElement: { dataset: { locale: "ru" }, lang: "ru", style: { setProperty() {} } },
  addEventListener() {},
  dispatchEvent() { return true; },
  querySelectorAll() { return []; },
};
global.FwrouterUI = {
  escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]); },
  trafficMetricLabel: (key) => key,
  formatTrafficBytes: (value) => `${value || 0} B`,
  translateBackendMessage: (value) => String(value || ""),
};

function load(relative) {
  vm.runInThisContext(fs.readFileSync(path.join(root, relative), "utf8"), { filename: relative });
}

load("static/js/fwrouter-i18n.js");
load("static/js/fwrouter-labels.js");
load("static/js/fwrouter-settings-events.js");
load("static/js/fwrouter-settings-inventory.js");

const inventory = global.FwrouterSettingsInventory;
const labels = global.FwrouterLabels;
const events = global.FwrouterSettingsEvents;
const htmlSource = fs.readFileSync(path.join(root, "index.html"), "utf8");
const userSource = fs.readFileSync(path.join(root, "static/js/user.js"), "utf8");
const adminSource = fs.readFileSync(path.join(root, "static/js/admin.js"), "utf8");
const fixture = (subject_id, health, overrides = {}) => ({
  subject_id,
  inventory_role: "vless_client",
  implementation_kind: "xray",
  desired_mode: "vpn",
  is_active: true,
  display_name: subject_id,
  health,
  ...overrides,
});

const rows = [
  fixture("healthy", { state: "healthy" }),
  fixture("pending", { state: "unknown", reason: "HEALTH_EVIDENCE_NOT_LOADED" }),
  fixture("unknown", { state: "unknown" }, { runtime_state: "active" }),
  fixture("disabled", { state: "disabled" }, { desired_mode: "disabled", is_active: false }),
  fixture("offline", { state: "warning", reason: "EXTERNAL_SOURCE_OFFLINE" }),
];

function rowFor(id) {
  return inventory.renderSettingsClientsHtml([rows.find((row) => row.subject_id === id)]);
}

assert.match(rowFor("healthy"), />Работает<|>Healthy</);
assert.match(rowFor("pending"), /Проверяем состояние/);
assert.doesNotMatch(rowFor("pending"), />Неизвестно</);
assert.match(rowFor("unknown"), />Неизвестно</, "runtime activity must not promote authoritative unknown health");
assert.match(rowFor("disabled"), />Отключено<|>Disabled</);
assert.match(rowFor("offline"), />Требует внимания<|>Needs attention</);

const legacy = fixture("legacy", { state: "unknown" }, {
  desired_mode: "selective",
  mode_support_state: "unsupported_legacy",
  supported_admin_modes: ["vpn", "disabled"],
});
const legacyHtml = inventory.renderSettingsClientsHtml([legacy]);
assert.match(legacyHtml, /Selective[^<]*Старый режим не поддерживается/);
assert.match(legacyHtml, /data-mode="vpn"/);
assert.match(legacyHtml, /data-mode="disabled"/);
assert.doesNotMatch(legacyHtml, /data-mode="selective"/, "legacy mode is retained as current state but not offered as a new choice");

const directLegacy = fixture("direct-legacy", { state: "healthy" }, {
  desired_mode: "direct",
  mode_support_state: "legacy_supported_direct",
  supported_admin_modes: ["vpn", "disabled"],
});
const directLegacyHtml = inventory.renderSettingsClientsHtml([directLegacy]);
assert.match(directLegacyHtml, /Direct · Старый режим сохранён/);
assert.match(directLegacyHtml, /data-mode="vpn"/);
assert.doesNotMatch(directLegacyHtml, /data-mode="direct"/);

const enabledAlias = fixture("enabled-alias", { state: "healthy" }, {
  desired_mode: "enabled",
  mode_support_state: "legacy_vpn_alias",
  supported_admin_modes: ["vpn", "disabled"],
});
assert.match(inventory.renderSettingsClientsHtml([enabledAlias]), /data-settings-mode-label="enabled-alias">VPN</);
const mixedProfile = fixture("mixed-profile", { state: "unknown" }, {
  desired_mode: "enabled",
  mode_support_state: "mixed",
  supported_admin_modes: ["vpn", "disabled"],
});
const mixedHtml = inventory.renderSettingsClientsHtml([mixedProfile]);
assert.match(mixedHtml, /data-settings-mode-label="mixed-profile">Смешанные режимы/);
assert.doesNotMatch(mixedHtml, /data-mode="mixed"/);

const externalHtml = inventory.renderSettingsClientsHtml([fixture("external-time", { state: "healthy" }, {
  subscription_url: "https://router.example/s/link-token",
  last_activity_at: "2026-09-29T12:34:56Z",
})]);
assert.match(externalHtml, /<time data-settings-activity-time datetime="2026-09-29T12:34:56Z">[^<]*:\d{2}:\d{2}<\/time>/);
assert.match(externalHtml, /settings-client-row__meta-wrap[\s\S]*settings-client-row__delete-near-link/);

assert.doesNotMatch(htmlSource, /subjectProxyGetBtn|Proxy GET/);
assert.match(htmlSource, /id="serversState"/);
assert.match(htmlSource, /<div id="selfMode" class="input admin-static-mode"[^>]*>DIRECT<\/div>/);
assert.doesNotMatch(htmlSource, /<select[^>]+id="selfMode"/);
assert.doesNotMatch(userSource, /runSubjectProxyGetCheck|proxy-get-check|subjectProxyGetBtn/);
assert.doesNotMatch(adminSource, /saveRouterSelfMode|el\("selfMode"\).*change/);

const source = [
  { subject_id: "healthy", health: { state: "unknown" }, online: false, last_activity_at: "2026-09-29T12:00:01Z" },
  { subject_id: "offline", health: { state: "warning" }, online: false },
];
assert.strictEqual(inventory.mergeHealthItemsBySubjectId(rows, source), true);
assert.deepStrictEqual(rows.map((row) => row.health.state), ["unknown", "unknown", "unknown", "disabled", "warning"]);
assert.strictEqual(rows[0].online, false);
assert.strictEqual(rows[0].last_activity_at, "2026-09-29T12:00:01Z");
assert.strictEqual(rows[1].online, undefined, "one subject's presence must not bleed into another row");

const badge = { textContent: "Checking", className: "", classList: { toggle() {} } };
const summary = { textContent: "Checking" };
const action = { hidden: false, querySelector: () => ({ textContent: "old action" }) };
const row = {
  dataset: {},
  querySelector(selector) {
    if (selector === ".settings-client-row__status") return badge;
    if (selector === "[data-settings-health_summary] strong") return summary;
    if (selector === "[data-settings-health_action]") return action;
    return null;
  },
};
inventory.updateSettingsClientHealthRow(row, fixture("stale", { state: "unknown" }, { runtime_state: "active" }));
assert.strictEqual(row.dataset.healthState, "unknown");
assert.strictEqual(badge.textContent, "Неизвестно");
assert.strictEqual(summary.textContent, "Недостаточно данных, чтобы подтвердить состояние.");
assert.strictEqual(action.hidden, true);

assert.match(events.formatTs("2026-09-29T12:34:56Z", { absolute: true, seconds: true }), /:\d{2}:\d{2}$/);
assert.match(events.formatTs("not-a-date", { absolute: true }), /наблюден|observation/i);
assert.strictEqual(labels.safeHumanServerLabel("sub:0123456789abcdef0123456789abcdef"), "Имя сервера недоступно");
assert.strictEqual(labels.safeHumanServerLabel("Proxy name"), "Proxy name");
global.FwrouterI18n.setLocale("en");
assert.match(rowFor("pending"), /Checking current state/);
assert.match(inventory.renderSettingsClientsHtml([legacy]), /Legacy mode is unsupported/);
assert.match(inventory.renderSettingsClientsHtml([directLegacy]), /Legacy mode is preserved/);
global.FwrouterI18n.setLocale("ru");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function buildHydratorHarness() {
  const settingsSource = fs.readFileSync(path.join(root, "static/js/settings.js"), "utf8");
  const match = settingsSource.match(/async function hydrateSettingsInventoryHealth\([\s\S]*?\n  }(?=\n\n  async function loadSettingsInventory)/);
  assert.ok(match, "the real inventory hydrator should remain available for behavior tests");
  const factory = new Function(
    "requests", "window", "mergeSettingsInventoryHealth", "updateSettingsInventoryHealthRows", "setText", "cacheNow", "t",
    `let settingsInventoryHydrationSeq = 0;
     let settingsInventoryRequestSeq = 1;
     let settingsClientsTab = "all";
     const settingsInventoryHydrations = new Map();
     const dataStore = { getSettingsInventory: (params) => requests(params) };
     const fetchApiV2 = () => Promise.reject(new Error("unexpected direct fetch"));
     ${match[0]}
     return {
       hydrate: hydrateSettingsInventoryHealth,
       setRequestSeq(value) { settingsInventoryRequestSeq = value; },
       setTab(value) { settingsClientsTab = value; },
       pendingCount() { return settingsInventoryHydrations.size; },
     };`,
  );
  const calls = [];
  const updates = [];
  const states = [];
  const fixtureWindow = { FwrouterSettingsInventory: inventory };
  const harness = factory(
    (params) => {
      const request = deferred();
      calls.push({ params, request });
      return request.promise;
    },
    fixtureWindow,
    inventory.mergeHealthItemsBySubjectId,
    (items) => updates.push(items.map((item) => ({ subject_id: item.subject_id, state: item.health?.state }))),
    (id, value) => { updates.push({ id, value }); states.push({ id, value }); },
    () => 100,
    (key) => key,
  );
  return { harness, calls, updates, states };
}

(async () => {
  const { harness, calls, updates } = buildHydratorHarness();
  const light = { items: [
    fixture("one", { state: "unknown", reason: "HEALTH_EVIDENCE_NOT_LOADED" }),
    fixture("two", { state: "unknown", reason: "HEALTH_EVIDENCE_NOT_LOADED" }),
  ] };
  const cacheEntry = { payload: light, loadedAt: 1 };
  const first = harness.hydrate("all", ["all"], 1, cacheEntry, light);
  const duplicate = harness.hydrate("all", ["all"], 1, cacheEntry, light);
  assert.notStrictEqual(first, duplicate, "async callers may receive distinct wrapper promises");
  assert.strictEqual(calls.length, 1, "deduplication prevents duplicate full inventory reads");
  calls[0].request.resolve({ items: [
    fixture("one", { state: "healthy" }, { online: true, last_activity_at: "2026-09-29T12:34:56Z" }),
    fixture("two", { state: "warning", reason: "EXTERNAL_SOURCE_OFFLINE" }, { online: false }),
  ] });
  await first;
  await duplicate;
  assert.deepStrictEqual(light.items.map((item) => item.health.state), ["healthy", "warning"]);
  assert.strictEqual(light.items[0].online, true);
  assert.strictEqual(light.items[1].online, false);
  assert.strictEqual(cacheEntry.loadedAt, 100, "hydrated evidence refreshes the existing inventory cache payload");

  for (const fullItems of [[fixture("one", { state: "healthy" })], []]) {
    const partial = buildHydratorHarness();
    const partialPayload = { items: [fixture("one", { state: "unknown", reason: "HEALTH_EVIDENCE_NOT_LOADED" }), fixture("missing", { state: "unknown", reason: "HEALTH_EVIDENCE_NOT_LOADED" })] };
    const task = partial.harness.hydrate("all", ["all"], 1, { payload: partialPayload }, partialPayload);
    partial.calls[0].request.resolve({ items: fullItems });
    await task;
    assert.strictEqual(partialPayload.items[1].health.reason, "HEALTH_EVIDENCE_UNAVAILABLE", "rows absent from accepted full evidence do not remain stuck in pending");
    assert.ok(partial.updates.some((item) => item?.id === "settingsClientsState"), "unmatched rows show the localized unavailable state");
  }

  const race = buildHydratorHarness();
  const oldLight = { items: [fixture("old", { state: "unknown", reason: "HEALTH_EVIDENCE_NOT_LOADED" })] };
  const newLight = { items: [fixture("new", { state: "unknown", reason: "HEALTH_EVIDENCE_NOT_LOADED" })] };
  const oldTask = race.harness.hydrate("all", ["all"], 1, { payload: oldLight }, oldLight);
  race.harness.setRequestSeq(2);
  const newTask = race.harness.hydrate("all", ["all"], 2, { payload: newLight }, newLight);
  assert.strictEqual(race.calls.length, 2, "a newer inventory sequence is not blocked by an older pending read");
  race.calls[0].request.resolve({ items: [fixture("old", { state: "healthy" })] });
  await oldTask;
  assert.strictEqual(oldLight.items[0].health.reason, "HEALTH_EVIDENCE_NOT_LOADED", "obsolete results cannot mutate their former payload");
  race.calls[1].request.resolve({ items: [fixture("new", { state: "warning" }, { online: false })] });
  await newTask;
  assert.strictEqual(newLight.items[0].health.state, "warning");

  const failed = buildHydratorHarness();
  const failedLight = { items: [fixture("failed", { state: "unknown", reason: "HEALTH_EVIDENCE_NOT_LOADED" })] };
  const failedTask = failed.harness.hydrate("all", ["all"], 1, { payload: failedLight }, failedLight);
  failed.calls[0].request.reject(new Error("backend temporarily unavailable"));
  await failedTask;
  assert.strictEqual(failedLight.items[0].health.reason, "HEALTH_EVIDENCE_UNAVAILABLE");
  assert.ok(failed.updates.some((item) => item?.id === "settingsClientsState"));
  console.log("fwrouter settings health hydration lifecycle ok");
})().catch((error) => { process.nextTick(() => { throw error; }); });

console.log("fwrouter settings health UI behavior ok");
