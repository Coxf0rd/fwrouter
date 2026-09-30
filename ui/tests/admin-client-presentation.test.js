const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");

global.window = global;
global.document = { documentElement: { dataset: { locale: "ru" } } };
global.FwrouterI18n = { t: (key) => key, locale: () => "ru" };
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
  translateBackendMessage(value) {
    return String(value || "");
  },
};
global.FwrouterLabels = {
  compactModeLabel: (value) => String(value || ""),
  compactSourceLabel: (value) => String(value || ""),
  subjectDomainCategory: (item) => String(item?.inventory_role || ""),
  implementationLabel: (value) => String(value?.implementation_kind || value || ""),
};

vm.runInThisContext(
  fs.readFileSync(path.join(root, "static/js/fwrouter-settings-events.js"), "utf8"),
  { filename: "static/js/fwrouter-settings-events.js" },
);

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
  last_seen: "2026-09-29T12:34:56.123456Z",
}]);

assert.match(adminHtml, />\s*\/s\/misha · admin\.devices\.enabled/);
assert.ok(adminHtml.includes('title="https://vpn.example.test/s/misha?token=secret"'));
assert.ok(!adminHtml.includes(">https://vpn.example.test/s/misha?token=secret"));
assert.match(adminHtml, /<div class="device-row__last-seen"><time data-admin-vless-last-seen datetime="2026-09-29T12:34:56\.123Z">\d{2}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}<\/time><\/div>/);
assert.ok(
  adminHtml.indexOf('class="device-title"') < adminHtml.indexOf('class="device-row__last-seen"')
    && adminHtml.indexOf('class="device-row__last-seen"') < adminHtml.indexOf('class="muted mono device-row__meta"'),
  "External-client timestamp must be the second line, directly after the title and before technical metadata.",
);
assert.doesNotMatch(adminHtml, /Enabled[^<]*2026-09-29T/);
assert.doesNotMatch(adminHtml, /12:34:56\.123456Z/);

global.FwrouterI18n.locale = () => "en";
const englishTimestamp = global.FwrouterAdminDevices.renderAdminVlessClientsHtml([{
  id: "client-en",
  email: "en",
  enabled: true,
  last_seen: "2026-09-29 12:34:56.987654",
}]);
assert.match(englishTimestamp, /<time data-admin-vless-last-seen datetime="2026-09-29T12:34:56\.987Z">\d{2}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}<\/time>/);
assert.doesNotMatch(englishTimestamp, /2026-09-29 12:34:56\.987654/);

const missingTimestamp = global.FwrouterAdminDevices.renderAdminVlessClientsHtml([{
  id: "client-no-time",
  email: "no-time",
  enabled: true,
}]);
assert.match(missingTimestamp, /<time data-admin-vless-last-seen>time\.no_observation<\/time>/);

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
assert.match(css, /\.device-row__last-seen\s*\{[\s\S]*white-space:\s*nowrap[\s\S]*text-overflow:\s*ellipsis/);
assert.match(
  css,
  /@media \(max-width: 760px\)[\s\S]*\.server-matrix__head,[\s\S]*\.server-matrix__body \{[\s\S]*min-width:\s*0;[\s\S]*width:\s*100%/,
  "Admin mobile server matrix body should fit its container.",
);
assert.match(
  css,
  /@media \(max-width: 520px\)[\s\S]*\.server-matrix__name \.picklist__badge \{[\s\S]*display:\s*none/,
  "Admin narrow mobile rows may hide secondary badges.",
);
assert.match(
  css,
  /#admin-top \.server-matrix__name \.admin-server-current-slot \.picklist__badge\s*\{[\s\S]*display:\s*inline-flex/,
  "The current-server badge remains visible in its reserved slot on narrow screens.",
);

const settingsInventorySource = fs.readFileSync(path.join(root, "static/js/fwrouter-settings-inventory.js"), "utf8");
assert.match(
  settingsInventorySource,
  /const subscriptionUrl = String\(client\.subscription_url \|\| ""\)\.trim\(\);[\s\S]*\? \(subscriptionUrl \|\| connectionUri \|\| client\.subscription_path \|\| subjectId\)/,
  "Settings external client rows should still prefer the subscription URL/path presentation.",
);

console.log("fwrouter admin client presentation contract ok");
