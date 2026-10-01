const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const settings = fs.readFileSync(path.join(root, "static/js/settings.js"), "utf8");
const i18nSource = fs.readFileSync(path.join(root, "static/js/fwrouter-i18n.js"), "utf8");

assert.match(html, /id="providerManagedControls"[^>]*hidden/);
assert.match(html, /id="vpnSubscriptionDeleteSource"/);
assert.match(settings, /function renderProviderManagedControls()[\s\S]*provider\.bindings[\s\S]*source_ref/);
const render = settings.match(/function renderProviderManagedControls\(\) \{[\s\S]*?\n  \}/)?.[0] || "";
assert.ok(render);
assert.doesNotMatch(render, /fetchApiV2|fetchJson|\/provider/,
  "Rendering provider state must not issue a provider request.");
assert.match(render, /binding\.members/);
assert.match(render, /binding\.provider_evidence/);
assert.match(render, /member\?\.label/);
assert.match(render, /member\?\.local_health/);
assert.match(render, /member\?\.latency_ms/);
assert.match(render, /member\?\.available_slots !== null/);
assert.match(render, /binding\.applied_member_id/);
assert.match(render, /binding\.applied_protocol/);
assert.match(render, /binding\.observed_protocol/);
assert.match(render, /providerOutcomeLabel\(binding\.last_outcome\)/);
assert.match(settings, /known\.includes\(normalized\) \? normalized : "unknown"/);
assert.match(settings, /vpnSubscriptionDeleteSource.*addEventListener\("change", renderProviderManagedControls\)/);
assert.match(settings, /fetchApiV2\(`\/subscription\/sources\/\$\{encodeURIComponent\(sourceRef\)\}\/provider`[\s\S]*method:\s*"POST"/);
assert.match(settings, /job:\s*\(result\) => result\?\.job\?\.job_id \|\| result\?\.job_id/);
assert.match(settings, /payload\.member_id\s*=/);
assert.match(settings, /payload\.auto\s*=/);
assert.match(settings, /payload\.priority\s*=/);
assert.match(settings, /payload\.expected_revision\s*=\s*expectedRevision/);
assert.match(settings, /Number\.isInteger\(priority\) \|\| priority < -1 \|\| priority > 5/);
assert.match(render, /member\?\.switch_eligible === true/);
assert.match(settings, /body:\s*JSON\.stringify\(payload\)/);
assert.match(settings, /renderProviderManagedControls\(\);[\s\S]*renderSubscriptionMeta\(\)/);

const listeners = [];
const sandbox = {
  window: { localStorage: { getItem: () => null, setItem: () => {} } },
  localStorage: { getItem: () => null, setItem: () => {} },
  document: {
    documentElement: { dataset: { locale: "ru" }, lang: "ru", style: { setProperty: () => {} } },
    addEventListener: (name, handler) => listeners.push([name, handler]),
    querySelectorAll: () => [],
  },
  CustomEvent: class CustomEvent {},
};
vm.runInNewContext(i18nSource, sandbox);
const i18n = sandbox.window.FwrouterI18n;
const expected = [
  "settings.provider.title",
  "settings.provider.evidence",
  "settings.provider.evidence.up",
  "settings.provider.evidence.down",
  "settings.provider.evidence.unavailable",
  "settings.provider.evidence.unknown",
  "settings.provider.outcome.saved",
  "settings.provider.outcome.source_deleted",
  "settings.provider.outcome.busy",
  "settings.provider.outcome.not_configured",
  "settings.provider.enable",
  "settings.provider.disable",
  "settings.provider.refresh",
  "settings.provider.protocol",
  "settings.provider.apply_protocol",
  "settings.provider.health.unknown",
  "settings.provider.no_latency",
  "settings.provider.switch",
  "settings.provider.auto",
  "settings.provider.priority",
  "settings.provider.invalid_priority",
  "settings.provider.emergency_direct",
  "settings.provider.observed",
  "settings.provider.runtime_applied",
  "settings.provider.last_outcome",
  "settings.provider.identity_unknown",
  "settings.provider.outcome.verified",
  "settings.provider.outcome.no_op",
  "settings.provider.outcome.partial",
  "settings.provider.outcome.unconfirmed",
  "settings.provider.outcome.deferred",
  "settings.provider.outcome.failed",
  "settings.provider.outcome.disabled",
  "settings.provider.outcome.pending",
  "settings.provider.outcome.no_candidate",
  "settings.provider.outcome.unknown",
];
for (const locale of ["ru", "en"]) {
  i18n.applyLocale(locale, { emit: false });
  for (const key of expected) {
    const value = i18n.t(key, { count: 3, value: 42 });
    assert.notStrictEqual(value, key, `${locale} is missing ${key}`);
  }
  assert.match(i18n.t("settings.provider.latency", { value: 42 }), /42/);
}

console.log("fwrouter provider controls and RU/EN localization contract ok");

// Execute the real renderer against distinct saved-source projections.
for (const locale of ["ru", "en"]) {
  i18n.applyLocale(locale, { emit: false });
  const nodes = {
    providerManagedControls: { dataset: {}, hidden: true },
    providerManagedBody: { innerHTML: "", replaceChildren() { this.innerHTML = ""; } },
    providerManagedState: { textContent: "" },
    vpnSubscriptionDeleteSource: { value: "source-a" },
  };
  const context = {
    el: (id) => nodes[id],
    setText: (id, value) => { nodes[id].textContent = value; },
    t: (key, values) => i18n.t(key, values),
    escapeHtml: (value) => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;"),
    formatTs: String,
    safeSubscriptionSourceLabel: (source) => source.label,
    providerOutcomeLabel: String,
    providerHealthLabel: String,
    vpnSubscriptionSources: [
      { source_ref: "source-a", label: "Subscription A" },
      { source_ref: "source-b", label: "Subscription B" },
      { source_ref: "ordinary", label: "Ordinary" },
    ],
    settingsWorkspace: { subscription: { provider_managed: { configured: true, bindings: [
      { source_ref: "source-a", binding_revision: 3, enabled: true, protocol: "hysteria2",
        current_member_id: "1", members: [{ member_id: "1", label: "Member A" }] },
      { source_ref: "source-b", enabled: false, protocol: "hysteria2", supported_protocols: ["hysteria2"],
        current_member_id: "2", members: [{ member_id: "2", label: "Member B" }] },
    ] } } },
  };
  vm.createContext(context);
  vm.runInContext(render, context);
  context.renderProviderManagedControls();
  assert.strictEqual(nodes.providerManagedControls.dataset.sourceRef, "source-a");
  assert.match(nodes.providerManagedBody.innerHTML, /Subscription A/);
  assert.match(nodes.providerManagedBody.innerHTML, /Member A/);
  nodes.providerManagedState.textContent = "previous action";
  nodes.vpnSubscriptionDeleteSource.value = "source-b";
  context.renderProviderManagedControls();
  assert.strictEqual(nodes.providerManagedState.textContent, "");
  assert.strictEqual(nodes.providerManagedControls.dataset.sourceRef, "source-b");
  assert.match(nodes.providerManagedBody.innerHTML, /Subscription B/);
  assert.match(nodes.providerManagedBody.innerHTML, /Member B/);
  assert.doesNotMatch(nodes.providerManagedBody.innerHTML, /Member A/);
  assert.match(nodes.providerManagedBody.innerHTML, /data-provider-action="refresh" disabled/);
  nodes.vpnSubscriptionDeleteSource.value = "ordinary";
  context.renderProviderManagedControls();
  assert.strictEqual(nodes.providerManagedControls.hidden, true);
  assert.strictEqual(nodes.providerManagedControls.dataset.sourceRef, undefined);
  assert.strictEqual(nodes.providerManagedControls.dataset.revision, undefined);
  assert.strictEqual(nodes.providerManagedBody.innerHTML, "");
}
