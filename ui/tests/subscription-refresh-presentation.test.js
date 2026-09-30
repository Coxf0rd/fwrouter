const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(path.join(root, relative), "utf8");
const settings = read("static/js/settings.js");
const admin = read("static/js/admin.js");
const user = read("static/js/user.js");
const autolist = read("static/js/fwrouter-admin-autolist.js");
const inventory = read("static/js/fwrouter-settings-inventory.js");
const common = read("static/js/fwrouter-common.js");
const events = read("static/js/fwrouter-settings-events.js");
const adminCss = read("static/css/admin-view.css");
const ha = read("../integrations/homeassistant/packages/fwrouter_control.yaml");
const action = read("../integrations/homeassistant/scripts/fwrouter_action.py");

assert.match(settings, /const display = String\(item\?\.display_label \|\| ""\)/);
assert.match(settings, /safeOriginLabel/);
assert.match(settings, /\/subscription\/sources\/\$\{encodeURIComponent\(sourceRef\)\}\/refresh/);
assert.match(settings, /source_outcomes/);
assert.match(settings, /settings-subscription-batch-result__visible-errors/);
assert.match(settings, /settings-subscription-batch-result__details/);
assert.match(settings, /intent_saved/);
assert.match(settings, /runtime_verified/);
assert.match(settings, /last_good_retained/);
const normalizationSource = settings.match(/function normalizeSubscriptionOperationResult\(value\) \{[\s\S]*?\n  \}/)?.[0];
assert.ok(normalizationSource, "Subscription results should have one nested job-result normalizer.");
const normalizeResult = vm.runInNewContext(`(${normalizationSource})`);
const jobResult = normalizeResult({
  outcome: "partial",
  runtime_verified: false,
  intent_saved: true,
  last_good_retained: true,
  source_outcomes: [{ source_ref: "src:private", display_label: "https://provider.example/…", outcome: "failed", error_code: "FETCH_FAILED" }],
  subscription: { refresh: { batch: { submitted_count: 1, imported_servers: 4, errors: 1, items: [{ ok: true, servers_count: 4 }] } } },
});
assert.strictEqual(jobResult.imported_servers, 4);
assert.strictEqual(jobResult.items[0].servers_count, 4);
assert.strictEqual(jobResult.source_outcomes[0].outcome, "failed");
assert.strictEqual(jobResult.runtime_verified, false);
const syncResult = normalizeResult({
  outcome: "failed", intent_saved: true, runtime_verified: false, last_good_retained: true,
  refresh: { error: { code: "RUNTIME_APPLY_FAILED", message: "Runtime apply failed." } },
  batch: { submitted_count: 1, errors: 1, items: [{ ok: false, error_code: "PARSE_FAILED" }] },
});
assert.strictEqual(syncResult.items[0].ok, false);
assert.strictEqual(syncResult.last_good_retained, true);
assert.strictEqual(syncResult.error_code, "RUNTIME_APPLY_FAILED");
assert.strictEqual(syncResult.error_message, "Runtime apply failed.");
const nestedJobResult = normalizeResult({
  subscription: { refresh: { batch: { submitted_count: 1, items: [{ ok: true, servers_count: 3 }] } } },
});
assert.strictEqual(nestedJobResult.items[0].servers_count, 3);

assert.match(admin, /fetchApiV2\("\/subscription\/refresh"/);
assert.match(admin, /jobTimeoutMs:\s*600000/);
assert.match(admin, /dataStore\?\.invalidate\?\.\(\["servers", "routerSummary", "settingsWorkspace", "health"\]\)/);
assert.match(admin, /await loadAutolist\(\{ liveMeasure: false \}\);[\s\S]*setText\("autolistState", message\)/);

assert.match(autolist, /const displayStatus = status === "stale" \? "unknown" : status/);
assert.match(autolist, /const freshness = value === "stale" \? t\("health\.evidence\.stale"\)/);
assert.match(adminCss, /\.server-matrix__ping\s*\{[\s\S]*min-width:\s*0[\s\S]*overflow:\s*hidden/);
assert.match(user, /normalized === "stale" \? t\("health\.evidence\.stale"\)/);
assert.match(user, /normalized === "no_data" \? "health\.latency\.no_data"/);
assert.match(inventory, /data-settings-delete-kind="\$\{escapeHtml\(deleteAction\.action\)\}"[\s\S]*data-settings-save-item/);

assert.match(common, /error\.result = job\?\.result \|\| null/);
assert.match(events, /if \(result === "partial"\) return "warning"/);

assert.match(ha, /enabled_candidate_names/);
assert.match(ha, /not lower\.startswith\(\('sub:', 'server:', 'member:'/);
assert.match(ha, /trigger\.to_state\.context\.user_id is not none/);
assert.match(ha, /trigger\.to_state\.context\.parent_id is none/);
assert.match(ha, /enabled_candidate_ids'\) is not string[\s\S]*enabled_candidate_ids'\) is not mapping/);
assert.match(ha, /ns\.server_id if ns\.matches == 1 else ''/);
assert.match(action, /effective_route_change=\{effective_status\}/);
assert.doesNotMatch(action, /effective_name\s*=/);

for (const code of ["eu", "ae", "ar"]) {
  const asset = read(`static/flags/${code}.svg`);
  assert.match(asset, /^<svg\b/);
}

console.log("subscription refresh, state projection, HA and flag presentation contract ok");
