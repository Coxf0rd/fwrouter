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
  /id="settingsExternalClientCreateHeader"[\s\S]*data-i18n="settings\.external_client\.add_short"/,
  "Settings inventory header should expose the only external client creation button near refresh.",
);
assert.doesNotMatch(
  html,
  /id="settingsExternalClientCreateToggle"/,
  "Settings external clients view should not expose a duplicate create toggle.",
);
assert.match(
  html,
  /class="settings-clients-actions-buttons"[\s\S]*id="settingsExternalClientCreateHeader"[\s\S]*id="settingsConnectionsAddHeader"[\s\S]*id="settingsClientsRefresh"/,
  "Header create and connection buttons should live in the same action group as Refresh.",
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
assert.match(
  html,
  /data-i18n="settings\.external_client\.link_part"/,
  "Create form should ask for the user-visible link suffix.",
);
assert.match(
  html,
  /data-i18n="settings\.external_client\.link_prefix"/,
  "Create form should show the fixed public link prefix.",
);
assert.doesNotMatch(
  html,
  /data-i18n="settings\.external_client\.email"/,
  "Create form should not expose the compatibility email field name.",
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
  /id:\s*"settings\.external_client\.create"[\s\S]*fetchApiV2\("\/xray\/clients"[\s\S]*method:\s*"POST"/,
  "Create form should call the existing backend create adapter through the shared action lifecycle.",
);
assert.match(
  settings,
  /async function createSettingsExternalClient\(form\)[\s\S]*window\.FwrouterUIAction\.runAction\(\{/,
  "External client creation should use the shared UI action lifecycle.",
);
assert.match(
  settings,
  /id:\s*"settings\.external_client\.create"[\s\S]*button:\s*submit[\s\S]*scope:\s*form[\s\S]*resultTarget:\s*el\("settingsExternalClientCreateState"\)[\s\S]*messageTarget:\s*el\("settingsExternalClientCreateState"\)/,
  "External client creation should use explicit button, form scope, result target, and message target.",
);
assert.match(
  settings,
  /disable:\s*\[aliasInput,\s*emailInput,\s*submit,\s*toggle\]/,
  "External client creation should disable the form fields and create controls while running.",
);
assert.match(
  settings,
  /pendingMessage:\s*"status\.saving"[\s\S]*successMessage:\s*"settings\.external_client\.created"[\s\S]*failedMessage:\s*"status\.error_prefix"/,
  "External client creation should use existing i18n message keys for lifecycle messages.",
);
assert.doesNotMatch(
  settings.match(/async function createSettingsExternalClient\(form\) \{([\s\S]*?)\n  \}/)?.[1] || "",
  /closest\(/,
  "External client creation should not discover targets with closest().",
);
assert.match(
  settings,
  /headerButton[\s\S]*settingsClientsTab\s*!==\s*"external_client"/,
  "Header create button should be visible only for External clients tab.",
);
assert.match(
  settings,
  /connectionButton[\s\S]*settingsClientsTab\s*!==\s*"connections"/,
  "Connection add button should be visible only for Connections tab.",
);
assert.match(
  inventory,
  /settings-client-row__buttons[\s\S]*data-settings-delete-kind="\$\{escapeHtml\(deleteAction\.action\)\}"[\s\S]*data-settings-save-item/,
  "External client rows should render Delete before Save.",
);
assert.match(
  inventory,
  /action:\s*"xray_client_group"/,
  "Subscription-profile external clients should expose a group delete action.",
);
assert.match(
  settings,
  /kind\s*===\s*"xray_client_group"[\s\S]*deleteSettingsExternalClientGroup\(id,\s*deleteBtn\)/,
  "Settings should handle aggregate external-client deletes.",
);
assert.match(
  settings,
  /\/xray\/subscription-profiles\/\$\{encodeURIComponent\(token\)\}/,
  "Aggregate external-client deletes should disable the subscription profile identity.",
);
assert.match(
  settings,
  /requested_by:\s*"ui"/,
  "Create requests should be attributed to the UI.",
);
assert.match(
  settings,
  /normalizeExternalClientLinkPart/,
  "Create form should normalize pasted link fragments to a short suffix.",
);
assert.match(
  settings,
  /email:\s*linkPart/,
  "Create form should pass the link suffix through the existing compatibility create field.",
);
assert.match(
  settings,
  /id:\s*"settings\.external_client\.create"[\s\S]*invalidateSettingsCaches\(\["workspace",\s*"inventory",\s*"rules",\s*"health"\]\)/,
  "Create success should invalidate only related read caches.",
);
assert.match(
  i18n,
  /"settings\.external_client\.add_short":\s*"\+ Клиент"/,
  "Russian short create button translation should exist.",
);
assert.match(
  i18n,
  /"settings\.external_client\.link_part":\s*"Имя в ссылке"/,
  "Russian link suffix label should exist.",
);
assert.match(
  i18n,
  /"settings\.external_client\.link_prefix":\s*"\/s\/"/,
  "The visible link prefix should be the domain-neutral subscription route.",
);
assert.match(
  settings,
  /connectionLocationLabel\(value\)[\s\S]*settings\.connections\.location\.docker[\s\S]*settings\.connections\.location\.host[\s\S]*settings\.connections\.location\.ip[\s\S]*settings\.connections\.location\.manual/,
  "Connection location labels should use i18n keys.",
);
assert.match(
  settings,
  /externalConnectionDescription\(connectionType\)[\s\S]*settings\.connections\.description\.external_vpn_module[\s\S]*settings\.connections\.description\.external_network_source[\s\S]*settings\.connections\.description\.external_management/,
  "External connection descriptions should use i18n keys.",
);
assert.match(
  i18n,
  /"settings\.connections\.location\.docker"[\s\S]*"settings\.connections\.description\.external_vpn_module"/,
  "Russian connection location and description translations should exist.",
);
assert.match(
  i18n,
  /"settings\.external_client\.add_short":\s*"\+ Client"/,
  "English short create button translation should exist.",
);
assert.match(
  i18n,
  /"settings\.connections\.location\.docker"[\s\S]*"settings\.connections\.description\.external_vpn_module"/,
  "English connection location and description translations should exist.",
);
assert.doesNotMatch(
  i18n,
  /"settings\.external_client\.add":\s*".*(Xray|VLESS)/i,
  "Create button should not use implementation names as the primary label.",
);
assert.match(
  inventory,
  /client\.subscription_url/,
  "External client rows should use the /s/name subscription URL as the primary meta text when available.",
);

console.log("fwrouter external client create UI contract ok");
