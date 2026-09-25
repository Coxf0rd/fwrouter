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
  fs.readFileSync(path.join(root, "static/js/fwrouter-i18n.js"), "utf8"),
  { filename: "static/js/fwrouter-i18n.js" },
);
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
  assert.match(table, /admin-server-health--usable/);
  assert.match(table, />1\/1</);
  assert.doesNotMatch(table, />Availability:|available 1\/1/);
});

const healthTable = global.FwrouterAdminAutolist.renderAutolistTableHtml(
  ["usable", "unavailable", "unknown"],
  {
    autolistDelays: new Map([["usable", 143]]),
    autolistStatuses: new Map([["usable", "usable"], ["unavailable", "unavailable"], ["unknown", "unknown"]]),
    autolistServerMeta: new Map([
      ["usable", { topology: { healthStatus: "usable", usableMembers: 1, totalMembers: 24 } }],
      ["unavailable", { topology: { healthStatus: "unavailable", usableMembers: 0, totalMembers: 1 } }],
      ["unknown", { topology: { healthStatus: "unknown", usableMembers: 0, totalMembers: 6 } }],
    ]),
  },
);
assert.match(healthTable, /admin-server-health--usable[^>]*[\s\S]*?>1\/24</);
assert.match(healthTable, /admin-server-health--unavailable[^>]*[\s\S]*?>0\/1</);
assert.match(healthTable, /admin-server-health--unknown[^>]*[\s\S]*?>0\/6</);
assert.match(healthTable, /server-matrix__ping[\s\S]*?143 ms/);
assert.match(healthTable, /server-matrix__ping[\s\S]*?No data/);
assert.doesNotMatch(healthTable, /Unavailable \/ Timeout|Timeout/);
assert.strictEqual((healthTable.match(/server-table__cell--name/g) || []).length, 1);
assert.strictEqual((healthTable.match(/server-table__cell--ping/g) || []).length, 1);
assert.doesNotMatch(healthTable, /logical_health\.timeout|admin-server-health--timeout/);
assert.match(healthTable, /data-topology-server="unavailable"/);
assert.match(healthTable, /data-topology-members="unavailable"/);

const membersHtml = global.FwrouterAdminAutolist.renderTopologyMembersHtml([
  {
    member_id: "sub:raw-member-b",
    member_order: 1,
    presentation_index: 2,
    is_active: true,
    is_effective_active: false,
    status: "failed",
    latency_ms: null,
    error_code: "MIHOMO_DELAY_TIMEOUT",
  },
  {
    member_id: "sub:raw-member-a",
    member_order: 0,
    presentation_index: 1,
    is_active: true,
    is_effective_active: true,
    status: "healthy",
    latency_ms: 412,
  },
]);
assert.doesNotMatch(membersHtml, /sub:raw-member/);
assert.match(membersHtml, /Node 1/);
assert.match(membersHtml, /Active/);
assert.match(membersHtml, />Active<\/span>/);
assert.match(membersHtml, /Available/);
assert.match(membersHtml, /is-effective-active/);
assert.match(membersHtml, /412 ms/);
assert.match(membersHtml, /Timeout/);
assert.match(membersHtml, /No data/);
assert.doesNotMatch(membersHtml, /MIHOMO_DELAY_TIMEOUT|raw-member/);
assert.ok(membersHtml.indexOf("412 ms") < membersHtml.indexOf("Timeout"));
const failedMemberHtml = global.FwrouterAdminAutolist.renderTopologyMembersHtml([{
  member_id: "internal-only-member",
  member_order: 0,
  is_active: true,
  status: "failed",
  latency_ms: null,
  error_code: "RUNTIME_PROBE_FAILURE",
  error_message: "private runtime diagnostic",
}]);
assert.match(failedMemberHtml, /Error/);
assert.match(failedMemberHtml, /No data/);
assert.doesNotMatch(failedMemberHtml, /RUNTIME_PROBE_FAILURE|private runtime diagnostic|internal-only-member/);
const checkingMembersHtml = global.FwrouterAdminAutolist.renderTopologyMembersHtml([
  { member_id: "sub:pending", member_order: 0, is_active: true, status: "healthy", latency_ms: 15 },
], true);
assert.match(checkingMembersHtml, /ping-spinner/);
assert.match(checkingMembersHtml, /admin-server-member-latency"><span class="ping-spinner/);
assert.doesNotMatch(checkingMembersHtml, /&lt;span class=&quot;ping-spinner/);
assert.doesNotMatch(checkingMembersHtml, />15 ms</);

const largeMembersHtml = global.FwrouterAdminAutolist.renderTopologyMembersHtml(
  Array.from({ length: 100 }, (_, index) => ({
    member_id: `sub:${String(100 - index).padStart(3, "0")}`,
    member_order: index,
    presentation_index: index + 1,
    is_active: true,
    is_effective_active: index === 42,
    status: "unknown",
    latency_ms: null,
  })),
);
assert.strictEqual((largeMembersHtml.match(/admin-server-member-row--unknown/g) || []).length, 100);

const tiedOrderHtml = global.FwrouterAdminAutolist.renderTopologyMembersHtml([
  { member_id: "sub:b", member_order: 0, is_active: true, status: "healthy", latency_ms: 200 },
  { member_id: "sub:a", member_order: 0, is_active: true, status: "healthy", latency_ms: 100 },
]);
assert.ok(tiedOrderHtml.indexOf("100 ms") < tiedOrderHtml.indexOf("200 ms"));

global.FwrouterI18n.setLocale("ru");
const russianTable = global.FwrouterAdminAutolist.renderAutolistTableHtml(["server"], {
  autolistServerMeta: new Map([["server", {
    topology: { healthStatus: "usable", usableMembers: 1, totalMembers: 2 },
  }]]),
});
const russianMembers = global.FwrouterAdminAutolist.renderTopologyMembersHtml([{
  member_id: "sub:hidden",
  member_order: 0,
  is_active: true,
  is_effective_active: true,
  status: "healthy",
  latency_ms: 100,
}]);
assert.match(russianTable, /Доступен, доступно узлов: 1\/2/);
assert.match(russianMembers, /Узел 1/);
assert.match(russianMembers, />Активен<\/span>/);
assert.match(russianMembers, /aria-label="Активен"/);
assert.match(russianMembers, /Доступен/);
assert.doesNotMatch(russianMembers, /sub:hidden/);

const css = fs.readFileSync(path.join(root, "static/css/admin-view.css"), "utf8");
const baseCss = fs.readFileSync(path.join(root, "static/css/base.css"), "utf8");
const responsiveCss = fs.readFileSync(path.join(root, "static/css/responsive.css"), "utf8");
const adminJs = fs.readFileSync(path.join(root, "static/js/admin.js"), "utf8");
const autolist = fs.readFileSync(path.join(root, "static/js/fwrouter-admin-autolist.js"), "utf8");
assert.match(css, /html\[data-view="admin"\] #admin-top \.server-matrix__name \.admin-server-label[\s\S]*flex:\s*1 1 auto[\s\S]*width:\s*auto/);
assert.match(css, /html\[data-view="admin"\] #admin-top \.server-matrix__name \{[\s\S]*flex-wrap:\s*wrap/);
assert.match(css, /\.server-matrix__row:has\(> \.admin-server-members:not\(\[hidden\]\)\)[\s\S]*align-self:\s*start/);
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
assert.match(adminJs, /server\?\.topology\?\.effective_latency_ms/);
assert.doesNotMatch(adminJs, /autolistDelays[\s\S]*server\.ping\.last_ping_ms/);
assert.match(autolist, /const hasMemberExpansion = topology\.totalMembers > 0;/);
assert.match(adminJs, /const expandedTopologyServerIds = new Set\(\);/);
assert.match(adminJs, /expandedTopologyServerIds\.forEach\([\s\S]*setTopologyMembersExpanded\(serverId, toggle, true\)/);
assert.match(adminJs, /expandedTopologyServerIds\.add\(serverId\);/);
assert.match(adminJs, /expandedTopologyServerIds\.delete\(serverId\);/);
assert.doesNotMatch(adminJs, /document\.addEventListener\("dblclick"/);
assert.match(adminJs, /candidate\.classList\.toggle\("is-selected", candidate === row\)/);
assert.doesNotMatch(adminJs, /setDynamicStatus\("autolistState", "status\.measuring"\)/);
assert.match(
  adminJs,
  /\(liveMeasure \? loadAutolistPickPingData\(\) : loadAutolistHistoryPingData\(\)\)/,
  "Admin should render stored manual ping values after reload without a live probe.",
);

const pingSelectJs = fs.readFileSync(path.join(root, "static/js/ping-select.js"), "utf8");
assert.match(pingSelectJs, /function renderPingCell\(options\)/);
assert.match(pingSelectJs, /ping-status--pending/);
assert.match(autolist, /function renderEffectiveLatency\(delay, status, pending\)[\s\S]*health\.latency\.no_data/);
assert.match(autolist, /function renderEffectiveLatency\(delay, status, pending\)[\s\S]*if \(pending\)[\s\S]*value === "usable"/);
assert.match(autolist, /health\.status\.timeout/);
assert.match(baseCss, /\.ping-status[\s\S]*min-width:\s*64px/);
assert.match(baseCss, /\.manual-check-summary[\s\S]*min-height:\s*20px/);
assert.doesNotMatch(css, /#autolistState:empty/);
assert.match(css, /#adminVpnState:empty[\s\S]*#selectiveState:empty/);
assert.match(css, /\.admin-server-health--usable[\s\S]*var\(--status-ok-text/);
assert.match(css, /\.admin-server-health--unavailable[\s\S]*var\(--status-error-text/);
assert.match(css, /\.admin-server-health--unknown[\s\S]*var\(--text-muted/);
assert.match(css, /\.admin-server-members[\s\S]*max-height:\s*280px[\s\S]*overflow-x:\s*hidden[\s\S]*overflow-y:\s*auto/);
assert.match(css, /\.admin-server-members-table[\s\S]*width:\s*100%[\s\S]*min-width:\s*0/);
assert.match(css, /grid-template-columns:\s*minmax\(72px,\s*1fr\) minmax\(54px,\s*0\.65fr\) minmax\(74px,\s*0\.8fr\) minmax\(88px,\s*1\.15fr\)/);
assert.match(css, /\.admin-server-members\.is-loading[\s\S]*min-height:\s*64px[\s\S]*padding:\s*12px/);
assert.match(css, /\.admin-server-members__loading[\s\S]*gap:\s*8px/);
assert.match(adminJs, /target\.classList\.add\("is-loading"\)[\s\S]*admin-server-members__loading/);
assert.match(adminJs, /finally \{[\s\S]*target\.classList\.remove\("is-loading"\)/);
assert.match(responsiveCss, /@media \(max-width: 420px\)[\s\S]*grid-template-columns:\s*minmax\(34px,\s*1fr\) 48px 55px minmax\(70px,\s*1\.5fr\)/);
assert.match(responsiveCss, /\.admin-server-member-row > span \{[\s\S]*min-width:\s*0/);

console.log("fwrouter admin server list presentation contract ok");
