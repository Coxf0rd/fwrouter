const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const settings = fs.readFileSync(path.join(root, "static/js/settings.js"), "utf8");
const inventory = fs.readFileSync(path.join(root, "static/js/fwrouter-settings-inventory.js"), "utf8");
const i18n = fs.readFileSync(path.join(root, "static/js/fwrouter-i18n.js"), "utf8");

assert.match(
  html,
  /id="settingsExternalClientCreateToggle"[\s\S]*data-i18n="settings\.external_client\.add_short"/,
  "Settings external clients view should expose a domain-level create button.",
);
assert.match(
  html,
  /id="settingsExternalClientCreateHeader"[\s\S]*data-i18n="settings\.external_client\.add_short"/,
  "Settings inventory header should expose external client creation near refresh.",
);
assert.match(
  html,
  /class="settings-clients-actions-buttons"[\s\S]*id="settingsExternalClientCreateHeader"[\s\S]*id="settingsClientsRefresh"/,
  "Header create button should live in the same action group as Refresh.",
);
assert.doesNotMatch(
  settings,
  /renderSettingsConnections\(\)[\s\S]*data-settings-add-external-client/,
  "Connections view should not expose external client creation.",
);
assert.match(
  html,
  /data-settings-external-client-form/,
  "Settings external clients view should include a create form.",
);
assert.doesNotMatch(
  html,
  /settingsClientsTabXray|settingsClientsTabVlessCreate/,
  "External client creation must not introduce a provider-specific tab.",
);
assert.match(
  settings,
  /value\s*===\s*"local_client"\s*\|\|\s*value\s*===\s*"external_client"/,
  "External clients tab should remain visible even when there are no current clients.",
);
assert.match(
  settings,
  /fetchApiV2\("\/xray\/clients"[\s\S]*method:\s*"POST"/,
  "Create form should call the existing backend create adapter.",
);
assert.match(
  settings,
  /headerButton[\s\S]*settingsClientsTab\s*!==\s*"external_client"/,
  "Header create button should be visible only for External clients tab.",
);
assert.match(
  inventory,
  /data-settings-delete-kind="\$\{escapeHtml\(deleteAction\.action\)\}"/,
  "External client rows should keep their delete action when the backend exposes a client id.",
);
assert.match(
  inventory,
  /action:\s*"xray_client_group"/,
  "Subscription-profile external clients should expose a group delete action.",
);
assert.match(
  settings,
  /kind\s*===\s*"xray_client_group"[\s\S]*deleteSettingsExternalClientGroup\(id\)/,
  "Settings should handle aggregate external-client deletes.",
);
assert.match(
  settings,
  /requested_by:\s*"ui"/,
  "Create requests should be attributed to the UI.",
);
assert.match(
  settings,
  /invalidateSettingsCaches\(\["workspace",\s*"inventory",\s*"rules",\s*"health"\]\)/,
  "Create success should invalidate only related read caches.",
);
assert.match(
  i18n,
  /"settings\.external_client\.add_short":\s*"\+ Клиент"/,
  "Russian short create button translation should exist.",
);
assert.match(
  i18n,
  /"settings\.external_client\.add_short":\s*"\+ Client"/,
  "English short create button translation should exist.",
);
assert.doesNotMatch(
  i18n,
  /"settings\.external_client\.add":\s*".*(Xray|VLESS)/i,
  "Create button should not use implementation names as the primary label.",
);

console.log("fwrouter external client create UI contract ok");
