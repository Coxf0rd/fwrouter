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
assert.match(render, /data-provider-intent/);
assert.match(render, /binding\?\.configured === true/);
assert.match(render, /data-provider-key/);
assert.match(render, /data-provider-replace-key/);
assert.match(render, /data-provider-resource-id/);
assert.doesNotMatch(render, /data-provider-member|data-provider-action="switch"|data-provider-action="preferences"/);
assert.match(settings, /provider\/configuration/);
assert.match(settings, /action === "discover" \? "configs" : "configuration"/);
assert.match(settings, /data-provider-intent/);
assert.match(settings, /data-provider-action="enable"/);
assert.match(settings, /body:\s*JSON\.stringify\(payload\)/);
assert.match(settings, /renderProviderManagedControls\(\);[\s\S]*renderSubscriptionMeta\(\)/);

const listeners = [];
const sandbox = {
  window: { localStorage: { getItem: () => null, setItem: () => {} }, FwrouterUI: { translateBackendMessage: (value) => String(value || "") } },
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
vm.runInNewContext(fs.readFileSync(path.join(root, "static/js/fwrouter-settings-events.js"), "utf8"), sandbox);
const journalEvents = sandbox.window.FwrouterSettingsEvents;
const expected = [
  ...["hysteria2", "vless", "vless_variant_2410", "trojan", "trojan_variant_2901", "shadowsocks2022", "wireguard", "amneziawg2"].map(value => `settings.provider.protocol_name.${value}`),
  "api_error.PROVIDER_PROTOCOL_EXTENDED_SETTINGS_UNSUPPORTED",
  "api_error.PROVIDER_PROTOCOL_PROFILE_UNSUPPORTED",
  "settings.provider.title",
  "settings.provider.intent",
  "settings.provider.allow_automatic_member_switch",
  "settings.provider.allow_automatic_member_switch_hint",
  "events.reason_code.provider_auto_switch_disabled",
  "events.code.subscription.provider_auto_switch_policy_changed",
  "settings.provider.provider",
  "settings.provider.api_key",
  "settings.provider.key_saved",
  "settings.provider.replace_key",
  "settings.provider.key_required",
  "settings.provider.resource_id",
  "settings.provider.discovered_config",
  "settings.provider.choose_config",
  "settings.provider.discover",
  "settings.provider.save_config",
  "settings.provider.invalid_resource_id",
  "settings.provider.configured",
  "settings.provider.configured_yes",
  "settings.provider.configured_no",
  "settings.provider.status",
  "settings.provider.apply",
  "settings.provider.configure_first",
  "settings.provider.intent_enabled_pending",
  "settings.provider.intent_disabled",
  "admin.provider.group",
  "admin.provider.location",
  "admin.provider.member",
  "admin.provider.current",
  "admin.provider.applied",
  "admin.provider.auto",
  "admin.provider.priority",
  "admin.provider.pending",
  "admin.provider.effective",
  "admin.provider.switch",
  "admin.provider.save_preferences",
  "admin.provider.success",
  "admin.provider.error",
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
  const policyEvent = journalEvents.toTypedEvent({
    event_id: "provider-policy-1",
    event_class: "audit",
    event_code: "subscription.provider_auto_switch_policy_changed",
    event_type: "provider_auto_switch_policy_changed",
    reason_code: "provider_auto_switch_disabled",
    details: { reason_code: "provider_auto_switch_disabled" },
  }, "audit");
  assert.strictEqual(policyEvent.title, i18n.t("events.code.subscription.provider_auto_switch_policy_changed"));
  assert.strictEqual(policyEvent.reason, i18n.t("events.reason_code.provider_auto_switch_disabled"));
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
      { source_ref: "source-c", label: "Subscription C" },
      { source_ref: "ordinary", label: "Ordinary" },
    ],
    settingsWorkspace: { subscription: { provider_managed: { bindings: [
      { source_ref: "source-a", binding_revision: 3, enabled: true, configured: true, allow_automatic_member_switch: true, provider_id: "stealthsurf", resource_id: 42, available_configs: [{ resource_id: 42, label: "Main" }, { resource_id: 43, label: "Backup" }], protocol: "hysteria2", supported_protocols: ["hysteria2"], provider_evidence: { status: "up" } },
      { source_ref: "source-b", enabled: false, configured: false },
      { source_ref: "source-c", enabled: true, configured: false, available_configs: [] },
    ] } } },
  };
  vm.createContext(context);
  vm.runInContext(render, context);
  context.renderProviderManagedControls();
  assert.strictEqual(nodes.providerManagedControls.dataset.sourceRef, "source-a");
  assert.match(nodes.providerManagedBody.innerHTML, /Subscription A/);
  assert.match(nodes.providerManagedBody.innerHTML, /data-provider-intent/);
  assert.match(nodes.providerManagedBody.innerHTML, /data-provider-auto-switch checked/);
  assert.match(nodes.providerManagedBody.innerHTML, /data-provider-action="enable"/);
  assert.match(nodes.providerManagedBody.innerHTML, /value="" data-provider-key/);
  assert.match(nodes.providerManagedBody.innerHTML, /data-provider-action="discover"/);
  assert.match(nodes.providerManagedBody.innerHTML, /value="42" data-provider-resource-id/);
  assert.match(nodes.providerManagedBody.innerHTML, /value="42" selected>Main/);
  assert.match(nodes.providerManagedBody.innerHTML, /value="43">Backup/);
  assert.doesNotMatch(nodes.providerManagedBody.innerHTML, /data-provider-action="switch"|data-provider-action="preferences"/);
  nodes.providerManagedState.textContent = "previous action";
  nodes.vpnSubscriptionDeleteSource.value = "source-b";
  nodes.providerManagedControls._providerConfigurations = [{ resource_id: 999, label: "Leaked" }];
  nodes.providerManagedControls.dataset.resourceId = "999";
  context.renderProviderManagedControls();
  assert.strictEqual(nodes.providerManagedState.textContent, "");
  assert.strictEqual(nodes.providerManagedControls.dataset.sourceRef, "source-b");
  assert.strictEqual(nodes.providerManagedControls._providerConfigurations.length, 0);
  assert.strictEqual(nodes.providerManagedControls.dataset.resourceId, undefined);
  assert.match(nodes.providerManagedBody.innerHTML, /Subscription B/);
  assert.match(nodes.providerManagedBody.innerHTML, /data-provider-intent/);
  assert.doesNotMatch(nodes.providerManagedBody.innerHTML, /data-provider-action="discover"|data-provider-action="enable"|data-provider-action="refresh"/);
  nodes.vpnSubscriptionDeleteSource.value = "source-c";
  context.renderProviderManagedControls();
  assert.match(nodes.providerManagedBody.innerHTML, /data-provider-auto-switch(?! checked)/,
    "An unset policy is displayed disabled by default.");
  assert.match(nodes.providerManagedBody.innerHTML, /data-provider-action="discover" disabled/);
  assert.doesNotMatch(nodes.providerManagedBody.innerHTML, /data-provider-action="enable"|data-provider-action="refresh"/);
  assert.match(nodes.providerManagedBody.innerHTML, /value="" data-provider-key/);
  const incompleteBinding = context.settingsWorkspace.subscription.provider_managed.bindings.find((item) => item.source_ref === "source-c");
  incompleteBinding.configured = true;
  context.renderProviderManagedControls();
  assert.match(nodes.providerManagedBody.innerHTML, /data-provider-action="enable" disabled/);
  assert.match(nodes.providerManagedBody.innerHTML, /data-provider-action="refresh" disabled/);
  nodes.vpnSubscriptionDeleteSource.value = "source-a";
  context.renderProviderManagedControls();
  assert.match(nodes.providerManagedBody.innerHTML, /value="42" selected>Main/);
  nodes.vpnSubscriptionDeleteSource.value = "ordinary";
  context.renderProviderManagedControls();
  assert.strictEqual(nodes.providerManagedControls.hidden, false);
  assert.strictEqual(nodes.providerManagedControls.dataset.sourceRef, "ordinary");
  assert.match(nodes.providerManagedBody.innerHTML, /Ordinary/);
  assert.match(nodes.providerManagedBody.innerHTML, /data-provider-intent/);
  assert.doesNotMatch(nodes.providerManagedBody.innerHTML, /data-provider-action="discover"|data-provider-action="enable"/);
}

const providerActionSource = settings.match(/async function runProviderManagedAction\(button\) \{[\s\S]*?\n  \}/)?.[0] || "";
const providerIntentSource = settings.match(/async function runProviderIntentToggle\(toggle\) \{[\s\S]*?\n  \}/)?.[0] || "";
assert.ok(providerActionSource);
assert.ok(providerIntentSource);
(async () => {
  const calls = [];
  const nodes = {
    providerManagedControls: { dataset: { sourceRef: "source-a" }, querySelectorAll: () => [], querySelector: () => null },
    providerManagedState: { textContent: "" },
    vpnSubscriptionDeleteSource: { value: "source-a" },
  };
  const intentBinding = { source_ref: "source-a", enabled: false, configured: false };
  const context = {
    el: (id) => nodes[id],
    t: (key, values) => i18n.t(key, values),
    setText: (id, value) => { nodes[id].textContent = value; },
    actionMessage: (error) => String(error?.message || error),
    settingsWorkspace: { subscription: { provider_managed: { bindings: [intentBinding] } } },
    window: { FwrouterUIAction: { runAction: async (spec) => { const result = await spec.action(); await spec.refresh?.(); return result; } } },
    fetchApiV2: async (url, options) => { calls.push({ url, body: JSON.parse(options.body) }); return { data: { binding: { source_ref: "source-a", enabled: true } } }; },
    reloadSubscriptionProjection: async () => {},
    invalidateSettingsCaches: () => {},
    loadSettingsWorkspace: async () => {},
    JSON,
    encodeURIComponent,
  };
  vm.createContext(context);
  vm.runInContext(providerActionSource + "\n" + providerIntentSource, context);
  await context.runProviderManagedAction({ dataset: { providerAction: "enable" } });
  assert.strictEqual(calls.length, 0, "provider apply is blocked while intent is off");
  await context.runProviderIntentToggle({ checked: true });
  assert.strictEqual(calls.length, 1);
  assert.match(calls[0].url, /provider\/configuration$/);
  assert.deepStrictEqual(calls[0].body, { enabled: true });
  assert.strictEqual(nodes.providerManagedState.textContent, i18n.t("settings.provider.intent_enabled_pending"));
  context.settingsWorkspace.subscription.provider_managed.bindings[0] = { source_ref: "source-a", enabled: true, configured: true, resource_id: 42 };
  await context.runProviderManagedAction({ dataset: { providerAction: "enable" } });
  assert.strictEqual(calls.length, 2);
  assert.match(calls[1].url, /\/provider$/);
  assert.deepStrictEqual(calls[1].body, { action: "enable" });
})().catch((error) => { console.error(error); process.exitCode = 1; });

const providerConfigSource = settings.match(/async function runProviderConfigurationAction\(button, action\) \{[\s\S]*?\n  \}/)?.[0] || "";
assert.ok(providerConfigSource);
(async () => {
  const requests = [];
  const elements = {
    "[data-provider-id]": { value: "stealthsurf" },
    "[data-provider-key]": { value: "new-secret" },
    "[data-provider-replace-key]": { checked: false },
    "[data-provider-resource-id]": { value: "73" },
    "[data-provider-config-choice]": null,
  };
  const root = { dataset: { sourceRef: "source-a" }, querySelector: (key) => elements[key], querySelectorAll: () => [] };
  const nodes = { providerManagedControls: root, providerManagedState: { textContent: "" }, vpnSubscriptionDeleteSource: {} };
  const binding = { source_ref: "source-a", enabled: true, configured: false };
  let reloadCount = 0;
  const context = {
    el: (id) => nodes[id], t: (key, values) => i18n.t(key, values),
    setText: (id, value) => { nodes[id].textContent = value; },
    actionMessage: (error) => String(error?.message || error),
    settingsWorkspace: { subscription: { provider_managed: { bindings: [binding] } } },
    window: { FwrouterUIAction: { runAction: async (spec) => { const result = await spec.action(); await spec.refresh?.(); return result; } } },
    fetchApiV2: async (url, options) => { requests.push({ url, body: JSON.parse(options.body) }); return { data: { binding: { configured: true } } }; },
    reloadSubscriptionProjection: async () => { reloadCount += 1; }, renderProviderManagedControls: () => {},
    invalidateSettingsCaches: (names) => { assert.ok(names.includes("servers"), "provider config invalidates canonical Admin cache"); },
    JSON, encodeURIComponent,
  };
  vm.createContext(context);
  vm.runInContext(providerConfigSource, context);
  await context.runProviderConfigurationAction({}, "save_config");
  assert.strictEqual(requests.length, 1);
  assert.match(requests[0].url, /provider\/configuration$/);
  assert.deepStrictEqual(requests[0].body, { provider_id: "stealthsurf", api_key: "new-secret", resource_id: 73 });
  assert.strictEqual(reloadCount, 1);
  binding.configured = true;
  await context.runProviderConfigurationAction({}, "discover");
  assert.strictEqual(requests.length, 2);
  assert.match(requests[1].url, /provider\/configs$/);
  assert.deepStrictEqual(requests[1].body, {});
  assert.strictEqual(reloadCount, 2, "discovery reloads the persisted projection and revision");
})().catch((error) => { console.error(error); process.exitCode = 1; });

const autoSwitchRenderer = settings.match(/function renderProviderManagedControls\(\) \{[\s\S]*?\n  \}/)?.[0] || "";
const autoSwitchAction = settings.match(/async function runProviderAutoSwitchToggle\(toggle\) \{[\s\S]*?\n  \}/)?.[0] || "";
assert.ok(autoSwitchRenderer);
assert.ok(autoSwitchAction);
assert.match(autoSwitchRenderer, /binding\?\.allow_automatic_member_switch === true/);
assert.match(autoSwitchRenderer, /data-provider-auto-switch/);
assert.doesNotMatch(autoSwitchRenderer, /fetchApiV2|provider\/configs|\/provider`/,
  "Rendering the persistent policy does not contact the provider.");
assert.match(autoSwitchAction, /provider\/configuration/);
assert.match(autoSwitchAction, /allow_automatic_member_switch: enabled/);
assert.doesNotMatch(autoSwitchAction, /\/provider\/configs|manual-check|probeRuntime|applyRuntime|runtimeApply/,
  "Saving the local policy does not start provider operations, probes, or runtime apply.");
(async () => {
  {
    const requests = [];
    const nodes = {
      providerManagedControls: { dataset: { sourceRef: "source-a" } },
      providerManagedState: { textContent: "" },
      vpnSubscriptionDeleteSource: { value: "source-a" },
    };
    const context = {
      el: (id) => nodes[id],
      t: (key) => key,
      setText: (id, value) => { nodes[id].textContent = value; },
      actionMessage: (error) => String(error?.message || error),
      window: { FwrouterUIAction: { runAction: async (spec) => { await spec.action(); await spec.refresh(); } } },
      fetchApiV2: async (url, options) => { requests.push({ url, body: JSON.parse(options.body) }); return {}; },
      invalidateSettingsCaches: (names) => assert.deepStrictEqual(Array.from(names), ["workspace"]),
      reloadSubscriptionProjection: async () => {},
      encodeURIComponent, JSON,
    };
    vm.createContext(context);
    vm.runInContext(autoSwitchAction, context);
    await context.runProviderAutoSwitchToggle({ checked: true });
    assert.deepStrictEqual(requests, [{
      url: "/subscription/sources/source-a/provider/configuration",
      body: { allow_automatic_member_switch: true },
    }]);
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
