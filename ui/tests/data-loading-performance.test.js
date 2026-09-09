const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const common = fs.readFileSync(path.join(root, "static/js/fwrouter-common.js"), "utf8");
const user = fs.readFileSync(path.join(root, "static/js/user.js"), "utf8");
const admin = fs.readFileSync(path.join(root, "static/js/admin.js"), "utf8");
const settings = fs.readFileSync(path.join(root, "static/js/settings.js"), "utf8");

function response(payload) {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    headers: { get: () => "application/json" },
    clone() {
      return { async json() { return payload; } };
    },
    async text() {
      return JSON.stringify(payload);
    },
  };
}

let now = 1000;
Date.now = () => now;
global.window = global;
global.document = {
  addEventListener() {},
  querySelectorAll() {
    return [];
  },
  getElementById() {
    return null;
  },
};
global.FwrouterI18n = {
  t: (key) => key,
  translateBackendMessage: (value) => String(value || ""),
};

const urls = [];
global.fetch = async (url) => {
  urls.push(String(url));
  if (String(url).includes("/ui/whoami")) {
    return response({ ok: true, data: { whoami: { subject: { subject_id: "lan" } } } });
  }
  if (String(url).includes("/servers?")) {
    return response({ ok: true, data: { servers: [{ server_id: "s1" }] } });
  }
  if (String(url).includes("/ui/router-summary")) {
    return response({ ok: true, data: { router: { global_mode: "selective" } } });
  }
  if (String(url).includes("/ui/settings/inventory?")) {
    return response({ ok: true, data: { items: [{ subject_id: "lan" }] } });
  }
  return response({ ok: true, data: {} });
};

vm.runInThisContext(common, { filename: "static/js/fwrouter-common.js" });

(async () => {
  assert.ok(global.FwrouterDataStore, "FwrouterDataStore should be exposed globally.");

  await Promise.all([
    global.FwrouterDataStore.getWhoami(),
    global.FwrouterDataStore.getWhoami(),
    global.FwrouterDataStore.getWhoami(),
  ]);
  assert.strictEqual(urls.filter((url) => url.includes("/ui/whoami")).length, 1, "Concurrent whoami reads should dedupe.");

  await global.FwrouterDataStore.getWhoami();
  assert.strictEqual(urls.filter((url) => url.includes("/ui/whoami")).length, 1, "Fresh whoami TTL should reuse cache.");

  now += 4000;
  await global.FwrouterDataStore.getWhoami();
  assert.strictEqual(urls.filter((url) => url.includes("/ui/whoami")).length, 2, "Expired whoami TTL should reload.");

  await Promise.all([
    global.FwrouterDataStore.getServers(),
    global.FwrouterDataStore.getServers({ inventory_state: "active", limit: 1000 }),
  ]);
  assert.strictEqual(urls.filter((url) => url.includes("/servers?")).length, 1, "Equivalent servers requests should dedupe.");

  global.FwrouterDataStore.invalidate("servers");
  await global.FwrouterDataStore.getServers();
  assert.strictEqual(urls.filter((url) => url.includes("/servers?")).length, 2, "Servers invalidation should force a fresh request.");

  await Promise.all([
    global.FwrouterDataStore.getRouterSummary(),
    global.FwrouterDataStore.getRouterSummary(),
  ]);
  assert.strictEqual(urls.filter((url) => url.includes("/ui/router-summary")).length, 1, "Concurrent router-summary reads should dedupe.");

  await Promise.all([
    global.FwrouterDataStore.getSettingsInventory({ role: "lan_client", limit: 500 }),
    global.FwrouterDataStore.getSettingsInventory({ limit: 500, role: "lan_client" }),
  ]);
  assert.strictEqual(urls.filter((url) => url.includes("/ui/settings/inventory?")).length, 1, "Equivalent settings inventory requests should dedupe.");

  assert.doesNotMatch(user, /window\.addEventListener\("DOMContentLoaded"[\s\S]*wire\(\)/, "User controller must not self-bootstrap before fwrouter:view.");
  assert.doesNotMatch(admin, /window\.addEventListener\("DOMContentLoaded"[\s\S]*wire\(\)/, "Admin controller must not self-bootstrap before fwrouter:view.");
  assert.doesNotMatch(settings, /window\.addEventListener\("DOMContentLoaded"[\s\S]*wire\(\)/, "Settings controller must not self-bootstrap before fwrouter:view.");

  assert.match(user, /document\.addEventListener\("fwrouter:view"[\s\S]*if \(view === "user"\) wire\(\)/, "User should bootstrap from the active view event.");
  assert.match(admin, /document\.addEventListener\("fwrouter:view"[\s\S]*if \(view === "admin"\) wire\(\)/, "Admin should bootstrap from the active view event.");
  assert.match(settings, /document\.addEventListener\("fwrouter:view"[\s\S]*if \(view === "settings"\) wire\(\)/, "Settings should bootstrap from the active view event.");

  assert.match(user, /dataStore\.getWhoami/, "User should read whoami through DataStore.");
  assert.match(user, /dataStore\.getServers/, "User should read servers through DataStore.");
  assert.match(user, /dataStore\.getRouterSummary/, "User should read router summary through DataStore.");
  assert.match(admin, /dataStore\.getServers/, "Admin should read servers through DataStore.");
  assert.match(admin, /dataStore\.getRouterSummary/, "Admin should read router summary through DataStore.");
  assert.match(settings, /dataStore\.getSettingsWorkspace/, "Settings should read workspace through DataStore.");
  assert.match(settings, /dataStore\.getServers/, "Settings proxy list should read servers through DataStore.");
  assert.doesNotMatch(
    user.slice(user.indexOf("async function loadServersBasic"), user.indexOf("async function loadServersWithPing")),
    /await loadClientExternalIpPair/,
    "User basic server load should not block first render on external IP checks.",
  );
  assert.match(
    user.slice(user.indexOf("function applyServerPingData"), user.indexOf("function buildServerPingDataFromServers")),
    /if \(!opts\.skipIpRefresh\)[\s\S]*loadClientExternalIpPair/,
    "User IP refresh should be a single lazy follow-up that can be skipped after mutations.",
  );

  assert.doesNotMatch(
    settings.slice(settings.indexOf("function wire")),
    /loadRoutingPolicyProjection\(|fetchApiV2\("\/state\/rules"|fetchApiV2\("\/state\/subjects|fetchApiV2\("\/state\/routing"|fetchJson\("\/api\/v2\/reconcile"|fetchJson\("\/api\/v2\/diagnose"/,
    "Settings normal bootstrap should not load heavy state, reconcile, or diagnose endpoints directly.",
  );

  console.log("fwrouter data loading performance contract ok");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
