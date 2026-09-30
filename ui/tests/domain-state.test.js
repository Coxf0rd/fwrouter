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
loadScript("static/js/fwrouter-settings-journal.js");

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
assert.doesNotMatch(routingHtml, /Inactive/);
assert.match(routingHtml, /Unknown/);

const diagnosticsReport = {
  status: "degraded",
  generated_at: "2026-09-04T00:00:00Z",
  sections: {
    database: {
      status: "warning",
      reason_code: "LEGACY_DATABASE_REFERENCES",
      reason: "legacy database references need cleanup; no runtime impact is confirmed",
      affected_entity_count: 1,
      overall_impact: false,
    },
    subjects: {
      status: "warning",
      reason_code: "SUBJECT_OBSERVATION_STALE",
      reason: "client or source observation is stale; current routing confirmation is incomplete",
      affected_entity_count: 16,
      affected_entities: [{ display_name: "Desktop-AS" }, { display_name: "KOMPUTER" }],
      last_observation: "2026-09-05T00:00:00Z",
    },
    routing: { status: "healthy" },
    vpn: { status: "warning" },
    watchdog: { status: "healthy" },
    connections: {
      status: "warning",
      reason_code: "EXTERNAL_INTEGRATION_OBSERVATION_MISSING",
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
assert.match(diagnosticsHtml, /Desktop-AS, KOMPUTER/);
assert.match(diagnosticsHtml, /<span class="muted">Affected<\/span><strong>Desktop-AS, KOMPUTER<\/strong>/);
assert.match(diagnosticsHtml, /Problem/);
assert.match(diagnosticsHtml, /Affected/);
assert.match(diagnosticsHtml, /Last observation/);
assert.match(diagnosticsHtml, /The database still has stale legacy references/);
assert.match(diagnosticsHtml, /Client or source observations are stale/);
assert.match(diagnosticsHtml, /No successful interval collector result has been recorded/);
assert.match(diagnosticsHtml, /Check the collector configuration and its last successful run time/);
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
assert.match(diagnosticsRuHtml, /Проблема/);
assert.match(diagnosticsRuHtml, /В базе данных остались устаревшие ссылки/);
assert.match(diagnosticsRuHtml, /Данные о клиенте или источнике устарели/);
assert.match(diagnosticsRuHtml, /не записан успешный результат периодического сборщика данных/);
assert.match(diagnosticsRuHtml, /Проверьте настройки сборщика данных/);
assert.doesNotMatch(diagnosticsRuHtml, /System health|External integrations|External client connection/);

const externalOfflineReport = {
  status: "warning",
  sections: { subjects: { status: "warning", reason_code: "EXTERNAL_SOURCE_OFFLINE", affected_entity_count: 1 } },
};
global.FwrouterI18n.setLocale("en");
const externalOfflineEn = domainState.renderDiagnosticsHtml(externalOfflineReport);
assert.match(externalOfflineEn, /The source is marked offline or missing/);
assert.match(externalOfflineEn, /check the device and its network/);
global.FwrouterI18n.setLocale("ru");
const externalOfflineRu = domainState.renderDiagnosticsHtml(externalOfflineReport);
assert.match(externalOfflineRu, /Источник отмечен как offline или отсутствующий/);
assert.match(externalOfflineRu, /проверьте устройство и его сеть/);

const configuredOnlyRules = domainState.renderRoutingPolicyHtml({
  rulesSummary: { state: { status: "pending" }, metadata: [{ ruleset_type: "big_vpn", metadata_json: { count: 5 } }] },
});
assert.match(configuredOnlyRules, /Правила есть, не применены/);
assert.doesNotMatch(configuredOnlyRules, /Применены/);
const failedRules = domainState.renderRoutingPolicyHtml({
  rulesSummary: { state: { status: "failed" }, metadata: [{ ruleset_type: "big_vpn", metadata_json: { count: 5 } }] },
});
assert.match(failedRules, /Ошибка применения/);
const appliedRules = domainState.renderRoutingPolicyHtml({
  rulesSummary: {
    state: { status: "success", last_success_at: "2026-09-26T00:00:00Z" },
    metadata: [{ ruleset_type: "big_vpn", status: "active", last_success_at: "2026-09-26T00:00:00Z", metadata_json: { count: 5 } }],
  },
  rules: { rules: { reconcile: { state: "in_sync" } } },
});
assert.match(appliedRules, /Применены/);

for (const latestStatus of ["failed", "pending"]) {
  const lastGoodWithNewAttempt = domainState.renderRoutingPolicyHtml({
    rulesSummary: {
      state: { status: latestStatus, last_success_at: "2026-09-25T00:00:00Z" },
      metadata: [{ ruleset_type: "big_vpn", status: latestStatus, last_success_at: "2026-09-25T00:00:00Z", metadata_json: { count: 5 } }],
    },
    rules: { rules: { reconcile: { state: "in_sync" } } },
  });
  assert.match(lastGoodWithNewAttempt, /Применены/);
}

const driftedLastGood = domainState.renderRoutingPolicyHtml({
  rulesSummary: {
    state: { status: "success", last_success_at: "2026-09-25T00:00:00Z" },
    metadata: [{ ruleset_type: "big_vpn", status: "active", last_success_at: "2026-09-25T00:00:00Z", metadata_json: { count: 5 } }],
  },
  rules: { rules: { reconcile: { state: "runtime_drift" } } },
});
assert.match(driftedLastGood, /Последний успешный набор; runtime не подтверждён/);
assert.doesNotMatch(driftedLastGood, /Применены/);

global.FwrouterI18n.setLocale("en");
const unconfirmedLastGoodEn = domainState.renderRoutingPolicyHtml({
  rulesSummary: {
    state: { status: "success", last_success_at: "2026-09-25T00:00:00Z" },
    metadata: [{ ruleset_type: "big_vpn", status: "active", last_success_at: "2026-09-25T00:00:00Z", metadata_json: { count: 5 } }],
  },
});
assert.match(unconfirmedLastGoodEn, /Last successful set; runtime unconfirmed/);
global.FwrouterI18n.setLocale("ru");
const unconfirmedLastGoodRu = domainState.renderRoutingPolicyHtml({
  rulesSummary: {
    state: { status: "success", last_success_at: "2026-09-25T00:00:00Z" },
    metadata: [{ ruleset_type: "big_vpn", status: "active", last_success_at: "2026-09-25T00:00:00Z", metadata_json: { count: 5 } }],
  },
});
assert.match(unconfirmedLastGoodRu, /Последний успешный набор; runtime не подтверждён/);
global.FwrouterI18n.setLocale("en");
const lastGoodAndFailedAttempt = global.FwrouterSettingsJournal.renderRulesContextHtml({
  state: {
    active_status: "Last successful set is retained; current application is unconfirmed",
    latest_attempt_status: "failed",
    problem: "The latest apply attempt failed.",
    action: "Review the rules text and apply it again.",
  },
  apply: { done: true, outcome: "failed" },
});
assert.match(lastGoodAndFailedAttempt, /Last successful set is retained/);
assert.match(lastGoodAndFailedAttempt, /Latest attempt/);
assert.match(lastGoodAndFailedAttempt, /failed/);
assert.match(lastGoodAndFailedAttempt, /Problem/);
assert.match(lastGoodAndFailedAttempt, /Review the rules text/);
global.FwrouterI18n.setLocale("ru");

const codedReason = domainState.renderDiagnosticsHtml({
  status: "degraded",
  sections: { connections: { status: "degraded", reason_code: "XRAY_BINDING_MISSING", reason: "internal English text" } },
});
assert.match(codedReason, /Для настроенного клиента не применена привязка Xray/);
assert.match(codedReason, /Проблема/);
assert.match(codedReason, /Действие/);
assert.match(codedReason, /Проверьте, применена ли привязка клиента Xray/);
assert.doesNotMatch(codedReason, /internal English text(?=<\/strong>)/);
const unconfirmedRu = domainState.renderDiagnosticsHtml({ status: "unknown", sections: {}, generated_at: null, unconfirmed: true });
assert.match(unconfirmedRu, /Текущее состояние не подтверждено/);
assert.doesNotMatch(unconfirmedRu, /Актуальных предупреждений: 0/);
global.FwrouterI18n.setLocale("en");
const unconfirmedEn = domainState.renderDiagnosticsHtml({ status: "unknown", sections: {}, generated_at: null, unconfirmed: true });
assert.match(unconfirmedEn, /Current state is unconfirmed/);
const unknownSection = domainState.renderDiagnosticsHtml({
  status: "unknown",
  sections: { vpn: { status: "unknown", reason_code: "VPN_UNKNOWN" } },
});
assert.doesNotMatch(unknownSection, /Active warnings: 1/);
assert.doesNotMatch(unknownSection, /Action/);

global.FwrouterI18n.setLocale("ru");
const externalRu = domainState.renderDiagnosticsHtml({
  status: "warning",
  sections: {
    subjects: { status: "warning", reason_code: "EXTERNAL_SOURCE_OFFLINE", affected_entity_count: 1 },
    connections: { status: "warning", reason_code: "EXTERNAL_SOURCE_MISSING", affected_entity_count: 1 },
    watchdog: { status: "unknown", reason_code: "WATCHDOG_RUNTIME_NOT_CONFIRMED" },
  },
});
assert.match(externalRu, /Внешний источник подтвердил, что клиент сейчас не в сети/);
assert.match(externalRu, /Клиент отсутствует в последнем успешном снимке/);
assert.match(externalRu, /Текущее состояние автоконтроля не подтверждено/);
assert.match(externalRu, /data-diagnostics-full/);
assert.match(externalRu, /data-load-full-diagnostics/);
global.FwrouterI18n.setLocale("en");
const advancedDiagnostics = domainState.renderDiagnosticsHtml({
  status: "warning",
  generated_at: "2026-09-29T00:00:00Z",
  sections: { subjects: { status: "warning", reason_code: "EXTERNAL_SOURCE_OFFLINE" } },
}, {
  generated_at: "2026-09-29T00:00:01Z",
  problems: [{ entity_type: "external_client", entity_id: "subject:123e4567-e89b-12d3-a456-426614174000", details: { display_name: "Alice" } }],
  summary: { hidden_sections: { events: { history: [{ event_code: "old" }] } } },
});
const ordinaryDiagnostics = advancedDiagnostics.split("data-diagnostics-full")[0];
assert.doesNotMatch(ordinaryDiagnostics, /123e4567-e89b-12d3-a456-426614174000|old/);
assert.match(advancedDiagnostics, /Alice/);
assert.match(advancedDiagnostics, /Full snapshot/);
assert.match(advancedDiagnostics, /Diagnostic event history/);
const groupedUnknownDiagnostics = domainState.renderDiagnosticsHtml({ status: "warning", sections: {} }, {
  generated_at: "2026-09-29T00:00:01Z",
  problems: [{
    entity_type: "subject", entity_id: "aggregate:subject_observation_stale:vless_client:xray",
    severity: "unknown", reason_code: "SUBJECT_OBSERVATION_STALE",
    details: { affected_count: 69, affected_entities: [{ display_name: "Alice phone" }, { display_name: "Bob tablet" }], overall_impact: false },
  }],
  summary: { hidden_sections: { events: { history: [] } } },
});
assert.match(groupedUnknownDiagnostics, /Subject \(69\)/);
assert.match(groupedUnknownDiagnostics, /Alice phone, Bob tablet/);
assert.doesNotMatch(groupedUnknownDiagnostics, /aggregate:subject_observation_stale/);
const unidentifiedConfirmedDiagnostics = domainState.renderDiagnosticsHtml({ status: "warning", sections: {} }, {
  generated_at: "2026-09-29T00:00:01Z",
  problems: [
    { entity_type: "subject", entity_id: "tailscale-node:30", severity: "warning", reason_code: "EXTERNAL_SOURCE_OFFLINE", reason: "source offline", source: "subject_state_projection", details: { overall_impact: true, role: "external_network_source", provider: "tailscale" } },
    { entity_type: "subject", entity_id: "tailscale-node:23", severity: "warning", reason_code: "EXTERNAL_SOURCE_OFFLINE", reason: "source offline", source: "subject_state_projection", details: { overall_impact: true, role: "external_network_source", provider: "tailscale" } },
  ],
  summary: { hidden_sections: { events: { history: [] } } },
  sections: {},
});
assert.strictEqual((unidentifiedConfirmedDiagnostics.match(/Subject \(2\)/g) || []).length, 1);
assert.match(unidentifiedConfirmedDiagnostics, /No affected entities were listed/);
assert.doesNotMatch(unidentifiedConfirmedDiagnostics, /tailscale-node:30|tailscale-node:23/);
const emptyTechnicalDiagnostics = domainState.renderDiagnosticsHtml({ status: "healthy", sections: {} }, {
  generated_at: "2026-09-29T00:00:01Z", problems: [], sections: {},
  summary: { hidden_sections: { events: { history: [] } } },
});
assert.doesNotMatch(emptyTechnicalDiagnostics, /Diagnostic event history|Current section evidence|Current problems/);
const externalEn = domainState.renderDiagnosticsHtml({ status: "warning", sections: { subjects: { status: "warning", reason_code: "EXTERNAL_SOURCE_OFFLINE" } } });
assert.match(externalEn, /external source confirmed that this client is currently offline/);

console.log("fwrouter domain state renderers ok");
