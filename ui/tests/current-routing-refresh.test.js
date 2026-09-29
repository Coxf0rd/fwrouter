const assert = require("assert");
const fs = require("fs");
const path = require("path");

const uiRoot = path.resolve(__dirname, "..");
const commonSource = fs.readFileSync(path.join(uiRoot, "static/js/fwrouter-common.js"), "utf8");
const userSource = fs.readFileSync(path.join(uiRoot, "static/js/user.js"), "utf8");

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function buildRefreshHarness() {
  const helpers = commonSource.match(/function routerSummaryViewActive\(\)[\s\S]*?function subscribeVisibleRouterSummary\(listener\) \{[\s\S]*?\n  }/);
  assert.ok(helpers, "shared router summary refresh helpers should be present");
  const doc = { visibilityState: "visible", documentElement: { dataset: { view: "" } }, addEventListener() {} };
  const timers = new Map();
  const calls = [];
  const listeners = new Set();
  let nextTimer = 1;
  const win = {
    setInterval(fn, ms) { const id = nextTimer++; timers.set(id, { fn, ms }); return id; },
    clearInterval(id) { timers.delete(id); },
    addEventListener() {},
  };
  const store = {
    getRouterSummary(options) {
      const request = deferred();
      calls.push({ options, request });
      return request.promise;
    },
  };
  const create = new Function(
    "document", "window", "FwrouterDataStore", "routerSummaryListeners",
    `const ROUTER_SUMMARY_REFRESH_MS = 15000;
     let routerSummaryRefreshTimer = null;
     let routerSummaryRefreshPending = null;
     let routerSummaryActiveView = "";
     let lastRouterSummary = null;
     ${helpers[0]}
     return { syncRouterSummaryRefresh, refreshVisibleRouterSummary, timers: () => routerSummaryRefreshTimer, last: () => lastRouterSummary };`,
  );
  const harness = create(doc, win, store, listeners);
  listeners.add((router) => calls.push({ dispatched: router }));
  return { doc, timers, calls, harness };
}

(async () => {
  const { doc, timers, calls, harness } = buildRefreshHarness();
  const provenanceSource = commonSource.match(/function renderCurrentAutoProvenance\(node, router, effectiveMode\) \{[\s\S]*?\n  }/)?.[0];
  assert.ok(provenanceSource, "shared auto provenance renderer should exist");
  const renderProvenance = new Function("window", "t", `${provenanceSource}; return renderCurrentAutoProvenance;`)({ FwrouterSettingsEvents: { formatTs: () => "time" } }, (key) => key);
  const provenanceNode = { hidden: false, textContent: "" };
  renderProvenance(provenanceNode, { global_mode: "DIRECT", current_server_source: "auto" });
  assert.strictEqual(provenanceNode.hidden, true, "global DIRECT does not display stale auto-selection provenance");
  renderProvenance(provenanceNode, { global_mode: "DIRECT", current_server_source: "auto" }, "VPN");
  assert.strictEqual(provenanceNode.hidden, false, "a User VPN override can still show its active auto target provenance");
  harness.syncRouterSummaryRefresh("user", true);
  assert.strictEqual(calls.length, 1, "view entry does an immediate summary GET");
  assert.strictEqual([...timers.values()][0].ms, 15000, "visible refresh interval is bounded to 15 seconds");
  const interval = [...timers.values()][0].fn;
  interval();
  interval();
  assert.strictEqual(calls.length, 1, "slow summary requests are deduplicated");
  calls[0].request.resolve({ router: { current_server_name: "A" } });
  await harness.refreshVisibleRouterSummary();
  assert.strictEqual(calls[1].dispatched.current_server_name, "A");
  assert.strictEqual(harness.last().current_server_name, "A");

  doc.visibilityState = "hidden";
  harness.syncRouterSummaryRefresh("user", false);
  assert.strictEqual(timers.size, 0, "hidden pages stop the polling timer");
  await harness.refreshVisibleRouterSummary();
  assert.strictEqual(calls.length, 2, "hidden pages issue no summary GET");
  doc.visibilityState = "visible";
  harness.syncRouterSummaryRefresh("admin", true);
  assert.strictEqual(calls.length, 3, "visibility return immediately refreshes once");
  calls[2].request.resolve({ router: { current_server_name: "B" } });
  await harness.refreshVisibleRouterSummary();

  const applySource = userSource.match(/function applyUserRoutingSummary\(router\) \{[\s\S]*?\n  }/)?.[0];
  assert.ok(applySource, "User routing summary patcher should exist");
  const draft = { value: "unsaved setting" };
  const provenance = { hidden: false, textContent: "old cause" };
  const labelCalls = [];
  const provenanceCalls = [];
  const userDoc = { documentElement: { dataset: { view: "user" } } };
  const win = {
    FwrouterLabels: { safeHumanServerLabel: (value) => value || "Server name unavailable" },
    FwrouterUI: { renderCurrentAutoProvenance: (node, router) => provenanceCalls.push({ node, router }) },
  };
  const makePatcher = new Function(
    "document", "powerApplyInFlight", "userServerOverride", "currentUserMode", "currentUserModeSource", "isAutoOverride", "setServerCurrentLabel", "el", "window", "t",
    `return (${applySource});`,
  );
  const patcher = (settings) => makePatcher(
    userDoc,
    Boolean(settings.pending),
    settings.override || "VPN-AUTO",
    settings.mode || "VPN",
    settings.source || "USER_OVERRIDE",
    (value) => String(value || "").startsWith("VPN-AUTO"),
    (value) => labelCalls.push(value),
    () => provenance,
    win,
    (key) => key,
  );
  patcher({ override: "server-subject-fixed" })({ current_server_name: "global-A", current_server_source: "auto" });
  assert.strictEqual(labelCalls.length, 0, "subject-fixed selections do not inherit global auto labels");
  assert.strictEqual(provenance.hidden, true, "fixed routing clears stale auto provenance");
  patcher({ mode: "DIRECT", source: "GLOBAL" })({ global_mode: "DIRECT", current_server_name: "global-A", current_server_source: "manual" });
  assert.strictEqual(labelCalls.at(-1), "DIRECT", "inherited global DIRECT updates the current label");
  patcher({ mode: "VPN", source: "ADMIN_OVERRIDE" })({ global_mode: "DIRECT", current_server_name: "vpn-A", current_server_source: "auto" });
  assert.strictEqual(labelCalls.at(-1), "vpn-A", "forced VPN mode is not overwritten by global DIRECT");
  assert.strictEqual(draft.value, "unsaved setting", "summary updates leave settings drafts untouched");
  patcher({ pending: true })({ current_server_name: "new" });
  assert.strictEqual(labelCalls.at(-1), "vpn-A", "poll does not overwrite the target during an in-flight apply");

  console.log("fwrouter visible routing refresh behavior ok");
})().catch((error) => { process.nextTick(() => { throw error; }); });
