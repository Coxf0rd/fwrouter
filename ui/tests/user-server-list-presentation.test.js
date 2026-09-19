const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");

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

const user = fs.readFileSync(path.join(root, "static/js/user.js"), "utf8");
assert.match(
  user,
  /const rowByName = new Map\(allRows\.map\(\(row\) => \[row\.name, row\]\)\);[\s\S]*renderServerListName\(row\)/,
  "Auto server picker should preserve row metadata when rendering names.",
);
assert.match(
  user,
  /kind:\s*String\(server\.kind \|\| ""\)/,
  "User server rows should keep server kind metadata for custom proxy rendering.",
);
assert.match(
  user,
  /FwrouterPingSelect\.renderPingCell/,
  "User server rows should use the shared ping cell renderer.",
);
assert.match(
  user,
  /function isVpnAutoMember\(server\) \{[\s\S]*return Boolean\(server\?\.preferences\?\.vpn_auto\);[\s\S]*\.filter\(\(server\) => isVpnAutoMember\(server\)\)/,
  "User VPN-auto picker should render membership, including manual-only priority -1 servers.",
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

console.log("fwrouter user server list presentation contract ok");
