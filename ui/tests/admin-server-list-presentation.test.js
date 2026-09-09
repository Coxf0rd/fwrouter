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

const css = fs.readFileSync(path.join(root, "static/css/admin-view.css"), "utf8");
assert.match(css, /html\[data-view="admin"\] #admin-top \.server-matrix__name \.admin-server-label[\s\S]*width:\s*100%/);
assert.match(css, /html\[data-view="admin"\] #admin-top \.server-matrix__name \.picklist__label-text[\s\S]*flex:\s*1 1 auto/);
assert.match(css, /html\[data-view="admin"\] #admin-top \.server-matrix__name \.picklist__label--proxy \.picklist__label-text[\s\S]*min-width:\s*0/);
assert.match(css, /html\[data-view="admin"\] #admin-top \.server-matrix__name \.picklist__label--proxy \.picklist__flag--proxy[\s\S]*flex:\s*0 0 18px/);

console.log("fwrouter admin server list presentation contract ok");
