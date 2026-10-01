const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const settings = fs.readFileSync(path.join(root, "static/js/settings.js"), "utf8");
const admin = fs.readFileSync(path.join(root, "static/js/admin.js"), "utf8");
const user = fs.readFileSync(path.join(root, "static/js/user.js"), "utf8");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const i18nSource = fs.readFileSync(path.join(root, "static/js/fwrouter-i18n.js"), "utf8");

const sandbox = {
  window: { localStorage: { getItem: () => null, setItem: () => {} } },
  localStorage: { getItem: () => null, setItem: () => {} },
  document: {
    documentElement: { dataset: { locale: "ru" }, lang: "ru", style: { setProperty: () => {} } },
    addEventListener: () => {},
    querySelectorAll: () => [],
  },
  CustomEvent: class CustomEvent {},
};
vm.runInNewContext(i18nSource, sandbox);
const i18n = sandbox.window.FwrouterI18n;

(async () => {
for (const locale of ["ru", "en"]) {
  i18n.applyLocale(locale, { emit: false });
  const expectedToggle = locale === "ru"
    ? "Использовать только эту подписку в VPN-auto"
    : "Use only this subscription in VPN-auto";
  assert.strictEqual(i18n.t("settings.subscription.exclusive.toggle"), expectedToggle);
  for (const key of [
    "settings.subscription.exclusive.title",
    "settings.subscription.exclusive.tooltip",
    "settings.subscription.exclusive.active",
    "settings.subscription.exclusive.inactive",
    "admin.autolist.exclusive_excluded",
    "admin.autolist.exclusive_excluded_title",
    "admin.autolist.exclusive_active",
    "events.code.subscription.vpn_auto_exclusive_changed",
    "events.code.subscription.vpn_auto_exclusive_applied",
    "events.code.subscription.vpn_auto_exclusive_unconfirmed",
    "events.job_type.subscription_vpn_auto_exclusive",
    "api_error.VPN_AUTO_EXCLUSIVE_SOURCE",
    "api_error.VPN_AUTO_EXCLUSIVE_TARGETS_READBACK_MISMATCH",
    "api_error.VPN_AUTO_EXCLUSIVE_SELECTION_UNCONFIRMED",
    "api_error.VPN_AUTO_EXCLUSIVE_INTERNAL_ERROR",
    "api_error.MIHOMO_EXCLUSIVE_RECONCILE_FAILED",
    "api_error.SUBSCRIPTION_SOURCE_IS_VPN_AUTO_EXCLUSIVE",
    "api_error.SUBSCRIPTION_SOURCE_HAS_NO_ACTIVE_INVENTORY",
    "api_error.PROVIDER_ROOT_UNAVAILABLE",
  ]) assert.notStrictEqual(i18n.t(key, { source: "Provider vpn" }), key, `${locale} has ${key}`);
}

const renderSource = settings.match(/function renderVpnAutoExclusiveControls\(\) \{[\s\S]*?\n  \}/)?.[0] || "";
const actionSource = settings.match(/async function runVpnAutoExclusiveToggle\(toggle\) \{[\s\S]*?\n  \}/)?.[0] || "";
assert.ok(renderSource);
assert.ok(actionSource);
assert.match(renderSource, /vpn_auto_exclusive\?\.source_ref/);
assert.match(renderSource, /data-vpn-auto-exclusive/);
assert.doesNotMatch(renderSource, /fetchApiV2|fetchJson|provider\/|\/provider/,
  "Rendering source controls makes no provider or network requests.");
assert.match(html, /id="providerManagedControls"/,
  "The independent provider-management toggle remains available alongside exclusive mode.");

for (const locale of ["ru", "en"]) {
  i18n.applyLocale(locale, { emit: false });
  const nodes = {
    vpnSubscriptionDeleteSource: { value: "source-b" },
    vpnAutoExclusiveControls: { dataset: {}, hidden: true },
    vpnAutoExclusiveBody: { innerHTML: "", replaceChildren() { this.innerHTML = ""; } },
  };
  const calls = [];
  const context = {
    el: (id) => nodes[id],
    t: (key, params) => i18n.t(key, params),
    escapeHtml: (value) => String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    safeSubscriptionSourceLabel: (source, index) => String(source?.display_label || `Subscription ${index}`),
    vpnSubscriptionSources: [
      { source_ref: "source-a", display_label: "Provider vpn" },
      { source_ref: "source-b", display_label: "Ordinary subscription" },
    ],
    settingsWorkspace: { subscription: { vpn_auto_exclusive: { source_ref: "source-a" } } },
  };
  vm.createContext(context);
  vm.runInContext(renderSource, context);
  context.renderVpnAutoExclusiveControls();
  assert.strictEqual(nodes.vpnAutoExclusiveControls.hidden, false);
  assert.strictEqual(nodes.vpnAutoExclusiveControls.dataset.sourceRef, "source-b");
  assert.match(nodes.vpnAutoExclusiveBody.innerHTML, /Ordinary subscription/);
  assert.match(nodes.vpnAutoExclusiveBody.innerHTML, /Provider vpn/,
    "The active source is named even when another source is selected.");
  assert.match(nodes.vpnAutoExclusiveBody.innerHTML, /Использовать только эту подписку в VPN-auto|Use only this subscription in VPN-auto/);
  assert.match(nodes.vpnAutoExclusiveBody.innerHTML, /data-vpn-auto-exclusive(?! checked)/,
    "A different selected source is not shown as active.");
  assert.strictEqual(calls.length, 0);

  const requests = [];
  const status = { textContent: "" };
  const actionContext = {
    ...context,
    el: (id) => id === "vpnAutoExclusiveControls" ? nodes.vpnAutoExclusiveControls
      : id === "vpnAutoExclusiveState" ? status : nodes[id],
    window: { FwrouterUIAction: { runAction: async (spec) => {
      assert.strictEqual(spec.id, "settings.subscription.vpn_auto_exclusive");
      assert.strictEqual(spec.jobTimeoutMs, 600000);
      const response = await spec.action();
      assert.strictEqual(spec.job(response), "job-exclusive-1");
      await spec.refresh();
      return response;
    } } },
    fetchApiV2: async (url, options) => {
      requests.push({ url, body: JSON.parse(options.body) });
      return { job: { job_id: "job-exclusive-1" } };
    },
    invalidateSettingsCaches: (scopes) => assert.strictEqual(Array.from(scopes).join(","), "workspace,servers,health"),
    reloadSubscriptionProjection: async () => {},
    setText: (_id, value) => { status.textContent = value; },
    actionMessage: (error) => String(error?.message || error),
    encodeURIComponent,
    JSON,
  };
  vm.createContext(actionContext);
  vm.runInContext(actionSource, actionContext);
  await actionContext.runVpnAutoExclusiveToggle({ checked: true });
  assert.deepStrictEqual(requests, [{
    url: "/subscription/sources/source-b/vpn-auto-exclusive",
    body: { enabled: true },
  }]);
  assert.strictEqual(status.textContent, i18n.t("status.ok"));

  actionContext.settingsWorkspace.subscription.vpn_auto_exclusive.source_ref = "source-a";
  await actionContext.runVpnAutoExclusiveToggle({ checked: false });
  assert.strictEqual(requests.length, 1,
    "Turning off an unchecked source does not send a mutation against another active source.");
}

assert.match(admin, /vpnAutoExcluded: Boolean\(server\.vpn_auto_excluded\)/);
assert.match(admin, /if \(autolistServerMeta\.get\(name\)\?\.vpnAutoExcluded\) return/,
  "Synthetic changes cannot trigger Auto mutations on excluded rows.");
assert.match(admin, /if \(!vpnAutoExcluded\) \{[\s\S]*body\.vpn_auto = nextVpnAuto;/,
  "Saving unrelated Admin preferences preserves stored Auto membership for excluded rows.");
assert.match(user, /\.filter\(\(server\) => Boolean\(server\?\.preferences\?\.global_list\) !== false\)/,
  "User fixed target inventory continues to follow global-list visibility independently of Auto eligibility.");
assert.match(user, /function isVpnAutoMember\(server\) \{[\s\S]*server\?\.auto_eligible !== false[\s\S]*Boolean\(server\?\.preferences\?\.vpn_auto\)/,
  "User Auto membership remains separate from the all/fixed-target list.");

console.log("FWRouter exclusive VPN-auto subscription UI contract ok");
})().catch((error) => { console.error(error); process.exitCode = 1; });
