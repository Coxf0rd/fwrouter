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
  dispatchEvent: () => true,
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
loadScript("static/js/fwrouter-settings-events.js");
loadScript("static/js/fwrouter-settings-domain-state.js");

const domainState = global.FwrouterSettingsDomainState;
const settingsCss = fs.readFileSync(path.join(root, "static/css/settings-view.css"), "utf8");

assert.match(
  settingsCss,
  /grid-template-columns:\s*minmax\(150px,\s*1\.3fr\)\s*minmax\(140px,\s*1\.1fr\)\s*110px\s*minmax\(180px,\s*1\.4fr\)\s*120px;/,
);

const routingHtml = domainState.renderRoutingPolicyHtml({
  rulesSummary: {
    state: { selective_default: "direct" },
    metadata: [
      { ruleset_type: "static_direct", metadata_json: { count: 0 } },
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
  reconcile: {
    entities: [
      { entity_type: "module", entity_id: "vpn", reconcile_state: "drift" },
      { entity_type: "routing", entity_id: "global", reconcile_state: "in_sync" },
    ],
  },
});

assert.match(routingHtml, /Alice/);
assert.match(routingHtml, /External client/);
assert.match(routingHtml, /selected VPN path/);
assert.match(routingHtml, /runtime applied/);
assert.match(routingHtml, /Laptop/);
assert.match(routingHtml, /Local client/);
assert.match(routingHtml, /direct/);
assert.match(routingHtml, /Real rules/);
assert.match(routingHtml, /Source \/ Scope/);
assert.match(routingHtml, /Destination/);
assert.match(routingHtml, /Decision/);
assert.match(routingHtml, /Reason/);
assert.match(routingHtml, /Status/);
assert.match(routingHtml, /Manual rules/);
assert.match(routingHtml, /Static Direct rules/);
assert.match(routingHtml, /Direct list/);
assert.match(routingHtml, /VPN list/);
assert.match(routingHtml, /99(?:,| )640/);
assert.match(routingHtml, /drift: 0/);
assert.match(routingHtml, /settings-policy-decisions/);
assert.doesNotMatch(routingHtml, /settings-policy-decisions" open/);
assert.doesNotMatch(routingHtml, /Xray client/i);
assert.doesNotMatch(routingHtml, /Vless client/i);
assert.strictEqual((routingHtml.match(/settings-domain-row--rule/g) || []).length, 6);
assert.match(routingHtml, /Inactive/);

const diagnosticsReport = {
  status: "degraded",
  generated_at: "2026-09-04T00:00:00Z",
  sections: {
    database: {
      status: "warning",
      reason: "legacy database references need cleanup; no runtime impact is confirmed",
      affected_entity_count: 1,
      overall_impact: false,
    },
    subjects: {
      status: "warning",
      reason: "client or source observation is stale; current routing confirmation is incomplete",
      affected_entity_count: 16,
      last_observation: "2026-09-05T00:00:00Z",
    },
    routing: { status: "healthy" },
    vpn: { status: "warning" },
    watchdog: { status: "healthy" },
    connections: {
      status: "warning",
      reason: "external integration observation missing",
      affected_entity_count: 1,
      overall_impact: false,
    },
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
};
const diagnosticsHtml = domainState.renderDiagnosticsHtml(diagnosticsReport);

assert.match(diagnosticsHtml, /System health/);
assert.match(diagnosticsHtml, /External integrations/);
assert.match(diagnosticsHtml, /settings-diagnostics-section-card__summary/);
assert.match(diagnosticsHtml, /settings-diagnostics-section-card__expanded/);
assert.match(diagnosticsHtml, /settings-diagnostics-section-card__affected/);
assert.match(diagnosticsHtml, /Reason/);
assert.match(diagnosticsHtml, /Affected/);
assert.match(diagnosticsHtml, /Last observation/);
assert.match(diagnosticsHtml, /The database still has stale legacy references/);
assert.match(diagnosticsHtml, /Observation data for some active clients or sources is stale/);
assert.match(diagnosticsHtml, /An optional external connection has no current state observation/);
assert.match(diagnosticsHtml, /Active warnings: 2/);
assert.doesNotMatch(diagnosticsHtml.match(/settings-diagnostics-section-card__summary[\s\S]*?<\/summary>/)[0], /legacy database references/i);
assert.doesNotMatch(diagnosticsHtml, />Events</);
assert.doesNotMatch(diagnosticsHtml, /Xray runtime failed/i);
assert.doesNotMatch(diagnosticsHtml, /settings-diagnostics-section-card" open/);

global.FwrouterI18n.setLocale("ru");
const diagnosticsRuHtml = domainState.renderDiagnosticsHtml(diagnosticsReport);
assert.match(diagnosticsRuHtml, /Состояние системы/);
assert.match(diagnosticsRuHtml, /База данных/);
assert.match(diagnosticsRuHtml, /Внешние интеграции/);
assert.match(diagnosticsRuHtml, /Причина/);
assert.match(diagnosticsRuHtml, /В базе данных остались устаревшие ссылки/);
assert.match(diagnosticsRuHtml, /Данные о части активных клиентов или источников устарели/);
assert.match(diagnosticsRuHtml, /Для необязательного внешнего подключения нет актуальных данных/);
assert.doesNotMatch(diagnosticsRuHtml, /System health|External integrations|External client connection/);

console.log("fwrouter domain state renderers ok");
