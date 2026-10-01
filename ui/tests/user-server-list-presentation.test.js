const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const i18n = fs.readFileSync(path.join(root, "static/js/fwrouter-i18n.js"), "utf8");

global.window = global;
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
global.Image = function Image() {};

vm.runInThisContext(
  fs.readFileSync(path.join(root, "static/js/fwrouter-user-servers.js"), "utf8"),
  { filename: "static/js/fwrouter-user-servers.js" },
);

const proxyHtml = global.FwrouterUserServers.renderServerListName({
  name: "Proxy не заходить",
  kind: "custom_https_proxy",
});

assert.match(proxyHtml, /picklist__label--proxy/);
assert.match(proxyHtml, /user-server-label/);
assert.match(proxyHtml, /title="Proxy не заходить"/);
assert.match(proxyHtml, />Proxy не заходить</);

const vpnHtml = global.FwrouterUserServers.renderServerListName({
  name: "de Frankfurt",
  kind: "vpn_server",
});

assert.doesNotMatch(vpnHtml, /picklist__label--proxy/);
assert.match(vpnHtml, /picklist__label--with-flag/);
assert.match(vpnHtml, /title="Frankfurt"/);
assert.strictEqual(global.FwrouterUserServers.isSelectableTargetServer({ selectable: true }), true);
assert.strictEqual(global.FwrouterUserServers.isSelectableTargetServer({ selectable: false }), false);
assert.strictEqual(global.FwrouterUserServers.isSelectableTargetServer({ selectable: false, provider_internal_member: true }), false);
assert.strictEqual(global.FwrouterUserServers.isSelectableTargetServer({ provider_managed_legacy: true, selectable: true }), true,
  "Provider-managed legacy rows with selectable=true remain available to ordinary user targets.");

const user = fs.readFileSync(path.join(root, "static/js/user.js"), "utf8");
assert.match(
  user,
  /const rowByName = new Map\(allRows\.map\(\(row\) => \[row\.name, row\]\)\);[\s\S]*serverRowCells\(row/,
  "Auto server picker should preserve row metadata when rendering names.",
);
assert.match(
  user,
  /kind:\s*String\(server\.kind \|\| ""\)/,
  "User server rows should keep server kind metadata for custom proxy rendering.",
);
assert.match(user, /normalized === "stale" \? t\("health\.evidence\.stale"\)/,
  "Stale freshness should remain secondary to the Unknown primary display state.");
assert.match(user, /return \[renderServerListName\(row\), health, pingCellHtml\(delay, status, pending\), ""\]/,
  "User rows should have separate name, health, latency/state and current badge slots.");
assert.doesNotMatch(user, /key:\s*"manual",\s*label:\s*t\("html\.action\.check_ping"\)/,
  "User server picker should not render an orphan manual-check heading.");
assert.match(user, /function userServerColumns\(\)[\s\S]*key: "name"[\s\S]*key: "status"[\s\S]*key: "ping"[\s\S]*key: "current"[\s\S]*?\];/,
  "User server picker should use the same four fixed slots in both lists.");
assert.match(user, /projectCanonicalHealth\(server\?\.topology\)/, "Canonical topology projection must survive user picker row mapping.");
assert.doesNotMatch(
  user.slice(user.indexOf("function buildServerPingDataFromServers"), user.indexOf("async function loadServersWithPingData")),
  /server\?\.ping\?\.last_ping_ms|server\.ping\.last_ping_ms/,
  "User runtime latency must not use manual ping fields.",
);
assert.match(
  user,
  /function isVpnAutoMember\(server\) \{[\s\S]*server\?\.auto_eligible !== false[\s\S]*Boolean\(server\?\.preferences\?\.vpn_auto\);[\s\S]*\.filter\(\(server\) => isVpnAutoMember\(server\)\)/,
  "User VPN-auto picker should honor canonical automatic eligibility without deriving it from priority.",
);
assert.doesNotMatch(
  user,
  /isAutoSelectableServer|vpn_auto_priority[\s\S]*>=\s*0/,
  "User VPN-auto membership must not be filtered by automatic eligibility.",
);
const loadServersWithPingData = user.slice(
  user.indexOf("async function loadServersWithPingData"),
  user.indexOf("async function runSubjectProxyGetCheck"),
);
assert.ok(loadServersWithPingData.length > 0);
assert.doesNotMatch(
  loadServersWithPingData,
  /setDynamicStatus\("serversState", "status\.measuring"\)/,
  "User ping refresh should show the spinner instead of text measurement status.",
);

const css = fs.readFileSync(path.join(root, "static/css/base.css"), "utf8");
assert.match(css, /html\[data-view="user"\] \.user-layout__left \.user-server-label[\s\S]*width:\s*100%/);
assert.match(css, /html\[data-view="user"\] \.user-layout__left \.picklist__label--proxy \.picklist__label-text[\s\S]*text-overflow:\s*ellipsis/);
assert.match(css, /\.ping-status[\s\S]*min-width:\s*64px/);
const userCss = fs.readFileSync(path.join(root, "static/css/user-view.css"), "utf8");
assert.match(userCss, /\.user-layout__left :is\(\.picklist__head, \.picklist__row\)[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\) 24px minmax\(0, 88px\) 58px/);
assert.match(userCss, /@media \(max-width: 420px\)[\s\S]*grid-template-columns:\s*minmax\(0, 1fr\) 20px minmax\(0, 68px\) 54px/);
assert.match(userCss, /\.user-server-health \{[\s\S]*color:\s*var\(--text-muted/);
assert.match(userCss, /\.user-server-health--available[\s\S]*status-ok-text/);
assert.match(userCss, /\.user-server-health--unavailable[\s\S]*status-error-text/);
const projection = global.FwrouterUserServers.projectCanonicalHealth;
assert.strictEqual(projection({ health_status: "usable" }), "healthy");
assert.strictEqual(projection({ health_status: "unavailable" }), "error");
assert.strictEqual(projection({ health_status: "unknown", checked_at: "2026-09-30T12:00:00Z" }), "unknown");
assert.strictEqual(projection({ health_status: "unknown", health_reason: "health_evidence_incomplete" }), "unknown");
assert.strictEqual(projection({ health_status: "unknown", freshness: "unknown" }), "unknown");
assert.strictEqual(projection({ health_status: "unknown" }), "no_data");
assert.strictEqual(projection({ health_status: "unknown", breakdown: { stale: 1 } }), "stale");
assert.match(i18n, /"admin\.autolist\.logical_health\.unknown": "Неизвестно"/);
assert.match(i18n, /"admin\.autolist\.logical_health\.unknown": "Unknown"/);
assert.match(i18n, /"admin\.autolist\.logical_health\.no_data": "Нет данных"/);
assert.match(i18n, /"admin\.autolist\.logical_health\.no_data": "No data"/);

console.log("fwrouter user server list presentation contract ok");
