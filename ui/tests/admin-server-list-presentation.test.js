const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");

global.window = global;
global.FwrouterI18n = { t: (key) => key };
global.FwrouterUI = {
  escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));
  },
  countryCodeToFlagEmoji(code) {
    return String(code || "").toUpperCase();
  },
  flagEmojiToCountryCode() {
    return "";
  },
  stripLeadingFlagEmoji(value) {
    return String(value || "");
  },
};
global.FwrouterPingSelect = {
  renderFlaggedName(value) {
    return `<span>${global.FwrouterUI.escapeHtml(value)}</span>`;
  },
};

vm.runInThisContext(
  fs.readFileSync(path.join(root, "static/js/fwrouter-admin-autolist.js"), "utf8"),
  { filename: "static/js/fwrouter-admin-autolist.js" },
);

const html = global.FwrouterAdminAutolist.renderAdminServerName("Proxy не заходить", {
  kind: "custom_https_proxy",
});

assert.match(html, /picklist__label--proxy/);
assert.match(html, /admin-server-label/);
assert.match(html, /title="Proxy не заходить"/);
assert.match(html, />Proxy не заходить</);

assert.doesNotThrow(() => {
  const table = global.FwrouterAdminAutolist.renderAutolistTableHtml(["srv-manual"], {
    currentCandidates: ["srv-manual"],
    currentHiddenUser: [],
    currentPriorities: { "srv-manual": -1 },
    autolistServerMeta: new Map([[
      "srv-manual",
      {
        label: "Proxy не заходить",
        kind: "custom_https_proxy",
        globalList: true,
        topology: { healthStatus: "usable", usableMembers: 1, totalMembers: 1 },
      },
    ]]),
    adminCurrentProxy: "Proxy не заходить",
  });
  assert.match(table, /Proxy не заходить/);
  assert.match(table, /value="-1"/);
});

const css = fs.readFileSync(path.join(root, "static/css/admin-view.css"), "utf8");
const baseCss = fs.readFileSync(path.join(root, "static/css/base.css"), "utf8");
const responsiveCss = fs.readFileSync(path.join(root, "static/css/responsive.css"), "utf8");
const adminJs = fs.readFileSync(path.join(root, "static/js/admin.js"), "utf8");
const autolist = fs.readFileSync(path.join(root, "static/js/fwrouter-admin-autolist.js"), "utf8");
assert.match(css, /html\[data-view="admin"\] #admin-top \.server-matrix__name \.admin-server-label[\s\S]*width:\s*100%/);
assert.match(css, /html\[data-view="admin"\] #admin-top \.server-matrix__name \.picklist__label-text[\s\S]*flex:\s*1 1 auto/);
assert.match(css, /html\[data-view="admin"\] #admin-top \.server-matrix__name \.picklist__label--proxy \.picklist__label-text[\s\S]*min-width:\s*0/);
assert.match(css, /html\[data-view="admin"\] #admin-top \.server-matrix__name \.picklist__label--proxy \.picklist__flag--proxy[\s\S]*flex:\s*0 0 18px/);
assert.match(css, /@media \(max-width: 760px\)[\s\S]*grid-template-columns:\s*minmax\(0,\s*1fr\) 52px 44px 44px 42px/);
assert.match(css, /@media \(max-width: 520px\)[\s\S]*grid-template-columns:\s*minmax\(0,\s*1fr\) 44px 36px 36px 34px/);
assert.match(responsiveCss, /html\[data-view="admin"\] #admin-top :is\(\.server-matrix__head,[\s\S]*grid-template-columns:\s*minmax\(0,\s*1fr\) 52px 44px 44px 42px/);
assert.match(responsiveCss, /html\[data-view="admin"\] #admin-top :is\(\.server-matrix__head,[\s\S]*grid-template-columns:\s*minmax\(0,\s*1fr\) 44px 36px 36px 34px/);
assert.match(adminJs, /if \(!currentCandidates\.includes\(name\)\) currentCandidates\.push\(name\);[\s\S]*currentPriorities\[name\] = 1;/);
assert.match(adminJs, /priorityOrigin:\s*String\(server\?\.preferences\?\.vpn_auto_priority_origin \|\| "legacy"\)/);
assert.match(adminJs, /currentPriority === 0 && meta\.priorityOrigin !== "manual"/);
assert.match(adminJs, /currentPriority === 1 && meta\.priorityOrigin === "auto"[\s\S]*currentPriorities\[name\] = 0/);
assert.match(adminJs, /const body = \{[\s\S]*vpn_auto: nextVpnAuto[\s\S]*global_list: nextVisible/);
assert.match(adminJs, /if \(touchedPriorities\.has\(serverId\) && nextPriority !== currentPriority\) \{[\s\S]*body\.vpn_auto_priority = nextPriority;/);
assert.match(adminJs, /String\(server\.server_id \|\| ""\)/);
assert.match(adminJs, /data-topology-members/);
assert.match(autolist, /data-topology-server/);
assert.doesNotMatch(adminJs, /setDynamicStatus\("autolistState", "status\.measuring"\)/);
assert.match(
  adminJs,
  /\(liveMeasure \? loadAutolistPickPingData\(\) : loadAutolistHistoryPingData\(\)\)/,
  "Admin should render stored manual ping values after reload without a live probe.",
);

const pingSelectJs = fs.readFileSync(path.join(root, "static/js/ping-select.js"), "utf8");
assert.match(pingSelectJs, /function renderPingCell\(options\)/);
assert.match(pingSelectJs, /ping-status--pending/);
assert.match(baseCss, /\.ping-status[\s\S]*min-width:\s*64px/);

console.log("fwrouter admin server list presentation contract ok");
