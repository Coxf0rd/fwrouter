const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");

global.window = global;
global.document = {
  documentElement: {
    dataset: { locale: "en" },
    lang: "en",
    style: { setProperty: () => {} },
  },
  addEventListener: () => {},
  querySelectorAll: () => [],
};
global.FwrouterUI = {
  escapeHtml: (value) => String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;"),
  translateBackendMessage: (value) => String(value || ""),
};

function loadScript(relativePath) {
  const source = fs.readFileSync(path.join(root, relativePath), "utf8");
  vm.runInThisContext(source, { filename: relativePath });
}

loadScript("static/js/fwrouter-i18n.js");
loadScript("static/js/fwrouter-labels.js");
loadScript("static/js/fwrouter-settings-domain-state.js");

const domainState = global.FwrouterSettingsDomainState;

const routingHtml = domainState.renderRoutingPolicyHtml({
  rulesSummary: {
    state: { selective_default: "direct" },
    metadata: [
      { ruleset_type: "static_direct", metadata_json: { count: 2 } },
      { ruleset_type: "big_direct", metadata_json: { count: 3 } },
      { ruleset_type: "big_vpn", metadata_json: { count: 99617 } },
      { ruleset_type: "effective", metadata_json: { effective_counts: { total: 99640, protected: 14 } } },
    ],
    manual: {
      active_validation: {
        rules: [
          { action: "DIRECT", kind: "domain", value: "2ip.ru", source: "manual", match: "exact", line: 1 },
          { action: "VPN", kind: "domain_suffix", value: ".facebook.com", source: "manual", match: "domain_suffix", line: 2 },
        ],
      },
    },
  },
  subjects: {
    items: [
      {
        entity: { id: "xray:alice", role: "vless_client", label: "Alice" },
        intent: { mode: "vpn", details: { implementation_kind: "xray" } },
        effective: { mode: "vpn", selected_server_id: "srv-1", dataplane_path: "vpn" },
        reason: { code: "applied" },
      },
      {
        entity: { id: "lan:laptop", role: "lan_client", label: "Laptop" },
        intent: { mode: "global", details: { implementation_kind: "lan" } },
        effective: { mode: "direct", dataplane_path: "direct" },
        reason: { mode_source: "inherited" },
      },
    ],
  },
  routing: {
    routing: {
      intent: { mode: "direct" },
      effective: { desired_global_mode: "direct" },
      reconcile: { state: "in_sync" },
    },
  },
  reconcile: { entities: [] },
});

assert.match(routingHtml, /Alice/);
assert.match(routingHtml, /External client/);
assert.match(routingHtml, /selected VPN path/);
assert.match(routingHtml, /runtime applied/);
assert.match(routingHtml, /Laptop/);
assert.match(routingHtml, /Local client/);
assert.match(routingHtml, /direct/);
assert.match(routingHtml, /Real rules/);
assert.match(routingHtml, /Manual rules/);
assert.match(routingHtml, /domain 2ip\.ru/);
assert.match(routingHtml, /domains \*\.facebook\.com/);
assert.match(routingHtml, /Static Direct rules/);
assert.match(routingHtml, /Direct list/);
assert.match(routingHtml, /VPN list/);
assert.match(routingHtml, /99(?:,| )640/);
assert.doesNotMatch(routingHtml, /Xray client/i);
assert.doesNotMatch(routingHtml, /Vless client/i);

const diagnosticsHtml = domainState.renderDiagnosticsHtml({
  status: "degraded",
  generated_at: "2026-09-04T00:00:00Z",
  sections: {
    database: { status: "ok" },
    subjects: { status: "ok" },
    routing: { status: "ok" },
    vpn: { status: "warning" },
    watchdog: { status: "ok" },
    xray: { status: "degraded" },
    events: { status: "warning" },
  },
  problems: [
    {
      entity_type: "xray",
      entity_id: "xray:alice",
      severity: "degraded",
      reason: "runtime binding missing",
      source: "xray_reconcile",
      details: {},
    },
  ],
});

assert.match(diagnosticsHtml, /System health/);
assert.match(diagnosticsHtml, /External integrations/);
assert.match(diagnosticsHtml, /External client connection/);
assert.match(diagnosticsHtml, /Implementation: Xray\/VLESS/);
assert.doesNotMatch(diagnosticsHtml, /Xray runtime failed/i);

console.log("fwrouter domain state renderers ok");
