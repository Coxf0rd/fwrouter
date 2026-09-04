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
  trafficMetricLabel: (item) => item?.label || item?.key || String(item || ""),
  formatTrafficBytes: (value) => `${Number(value || 0)} B`,
};
global.FwrouterSettingsEvents = {
  formatTs: (value) => String(value || ""),
};

function loadScript(relativePath) {
  const source = fs.readFileSync(path.join(root, relativePath), "utf8");
  vm.runInThisContext(source, { filename: relativePath });
}

loadScript("static/js/fwrouter-i18n.js");
loadScript("static/js/fwrouter-labels.js");
loadScript("static/js/fwrouter-settings-inventory.js");

const labels = global.FwrouterLabels;
const inventory = global.FwrouterSettingsInventory;

assert.strictEqual(labels.subjectDomainCategory({ inventory_role: "vless_client", implementation_kind: "xray" }), "external_client");
assert.strictEqual(labels.settingsSubjectKindLabel("xray"), "External client");
assert.strictEqual(labels.implementationLabel({ implementation_kind: "xray" }), "Xray/VLESS");

assert.strictEqual(labels.subjectDomainCategory({ inventory_role: "external_network_source", implementation_kind: "tailscale" }), "external_network_source");
assert.strictEqual(labels.settingsSubjectKindLabel("tailscale"), "Network source");
assert.strictEqual(labels.implementationLabel({ implementation_kind: "tailscale" }), "Tailscale");

assert.strictEqual(labels.subjectDomainCategory({ inventory_role: "docker_runtime", implementation_kind: "docker" }), "service");
assert.strictEqual(labels.settingsSubjectKindLabel("docker_runtime"), "Service");
assert.strictEqual(labels.settingsSubjectKindLabel("router_core"), "System");
assert.strictEqual(global.FwrouterI18n.t("display.system.title.mihomo"), "VPN connection");
assert.strictEqual(global.FwrouterI18n.t("display.system.title.tailscale"), "Network source");

const html = inventory.renderSettingsClientsHtml([
  {
    subject_id: "xray:alice",
    inventory_role: "vless_client",
    implementation_kind: "xray",
    display_name: "Alice",
    client_id: "alice",
    is_active: true,
    desired_mode: "direct",
    applied_mode: "direct",
    traffic_month: {},
  },
  {
    subject_id: "tailscale:node",
    inventory_role: "external_network_source",
    implementation_kind: "tailscale",
    display_name: "Node",
    is_active: true,
    desired_mode: "global",
    applied_mode: "global",
    traffic_month: {},
  },
  {
    subject_id: "docker:svc",
    inventory_role: "docker_runtime",
    implementation_kind: "docker",
    display_name: "Service",
    is_active: true,
    desired_mode: "direct",
    applied_mode: "direct",
    traffic_month: {},
  },
], { hiddenSubjectIds: new Set(), trafficPreferences: {} });

assert.match(html, /External client/);
assert.match(html, /Network source/);
assert.match(html, /Service/);
assert.match(html, /Implementation/);
assert.match(html, /Xray\/VLESS/);
assert.match(html, /Tailscale/);
assert.doesNotMatch(html, /Vless client/i);
assert.doesNotMatch(html, /Xray client/i);
assert.doesNotMatch(html, /Docker runtime/i);

const externalAction = inventory.settingsClientActionAdapter({
  subject_id: "xray:alice",
  inventory_role: "vless_client",
  implementation_kind: "xray",
  client_id: "alice",
});
assert.deepStrictEqual(externalAction, {
  action: "xray_client",
  domain_category: "external_client",
  id: "alice",
});

console.log("fwrouter UI domain model mappings ok");
