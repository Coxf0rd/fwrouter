const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");

global.window = global;
global.FwrouterI18n = { t: (key) => key };
global.FwrouterUI = {
  escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));
  },
  trafficMetricLabel(value) {
    return String(value || "");
  },
  formatTrafficBytes(value) {
    return `${Number(value || 0)} B`;
  },
};
global.FwrouterLabels = {
  compactModeLabel: (value) => String(value || ""),
  compactSourceLabel: (value) => String(value || ""),
  subjectDomainCategory: (item) => String(item?.inventory_role || ""),
  implementationLabel: (value) => String(value?.implementation_kind || value || ""),
};

vm.runInThisContext(
  fs.readFileSync(path.join(root, "static/js/fwrouter-admin-devices.js"), "utf8"),
  { filename: "static/js/fwrouter-admin-devices.js" },
);

const adminHtml = global.FwrouterAdminDevices.renderAdminVlessClientsHtml([{
  id: "client-1",
  email: "misha",
  subscription_url: "https://vpn.example.test/s/misha?token=secret",
  local_name: "Misha",
  enabled: true,
  implementation_kind: "xray",
}]);

assert.match(adminHtml, />\s*\/s\/misha · admin\.devices\.enabled/);
assert.ok(adminHtml.includes('title="https://vpn.example.test/s/misha?token=secret"'));
assert.ok(!adminHtml.includes(">https://vpn.example.test/s/misha?token=secret"));

const adminPathHtml = global.FwrouterAdminDevices.renderAdminVlessClientsHtml([{
  id: "client-2",
  email: "alice",
  subscription_path: "/s/alice",
  local_name: "Alice",
  enabled: true,
  implementation_kind: "xray",
}]);

assert.match(adminPathHtml, />\s*\/s\/alice · admin\.devices\.enabled/);
assert.doesNotMatch(adminPathHtml, />\s*alice · admin\.devices\.enabled/);

const css = fs.readFileSync(path.join(root, "static/css/admin-view.css"), "utf8");
assert.match(
  css,
  /@media \(max-width: 760px\)[\s\S]*html\[data-view="admin"\] #admin-top \.server-matrix \{[\s\S]*overflow:\s*hidden/,
  "Admin mobile server matrix should not rely on horizontal table scrolling.",
);
assert.match(
  css,
  /@media \(max-width: 760px\)[\s\S]*\.server-matrix__head,[\s\S]*\.server-matrix__body \{[\s\S]*min-width:\s*0;[\s\S]*width:\s*100%/,
  "Admin mobile server matrix body should fit its container.",
);
assert.match(
  css,
  /@media \(max-width: 520px\)[\s\S]*\.server-matrix__name \.picklist__badge \{[\s\S]*display:\s*none/,
  "Admin narrow mobile rows should hide secondary badges to keep proxy and VPN names readable.",
);

const settingsInventorySource = fs.readFileSync(path.join(root, "static/js/fwrouter-settings-inventory.js"), "utf8");
assert.match(
  settingsInventorySource,
  /const subscriptionUrl = String\(client\.subscription_url \|\| ""\)\.trim\(\);[\s\S]*\? \(subscriptionUrl \|\| connectionUri \|\| client\.subscription_path \|\| subjectId\)/,
  "Settings external client rows should still prefer the subscription URL/path presentation.",
);

console.log("fwrouter admin client presentation contract ok");
