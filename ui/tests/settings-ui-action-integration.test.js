const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const settings = fs.readFileSync(path.join(root, "static/js/settings.js"), "utf8");

const refreshMatch = settings.match(/async function refreshVpnSubscription\(\) \{([\s\S]*?)\n  \}/);
assert.ok(refreshMatch, "refreshVpnSubscription should exist.");

const refreshBody = refreshMatch[1];

assert.match(
  refreshBody,
  /window\.FwrouterUIAction\.runAction\(\{/,
  "Subscription refresh should use the shared UI action lifecycle.",
);
assert.match(
  refreshBody,
  /id:\s*"settings\.subscription\.refresh"/,
  "Subscription refresh should provide a stable action id.",
);
assert.match(
  refreshBody,
  /button:\s*el\("vpnSubscriptionRefresh"\)/,
  "Subscription refresh should use the refresh button as the button target.",
);
assert.match(
  refreshBody,
  /scope:\s*document\.querySelector\("#settingsControlsPane \.settings-subscription-card"\)/,
  "Subscription refresh should target only the subscription card scope.",
);
assert.match(
  refreshBody,
  /resultTarget:\s*el\("vpnSubscriptionState"\)/,
  "Subscription refresh should use the existing state node as the result target.",
);
assert.match(
  refreshBody,
  /messageTarget:\s*el\("vpnSubscriptionState"\)/,
  "Subscription refresh should use the existing state node as the message target.",
);
assert.match(
  refreshBody,
  /pendingMessage:\s*"status\.updating"/,
  "Subscription refresh should preserve the previous pending message key.",
);
assert.match(
  refreshBody,
  /successMessage:\s*"status\.ready"/,
  "Subscription refresh should preserve the previous success message key.",
);
assert.match(
  refreshBody,
  /failedMessage:\s*"status\.error_prefix"/,
  "Subscription refresh should use the existing localized error wrapper.",
);
assert.match(
  refreshBody,
  /fetchApiV2\("\/subscription\/refresh",\s*\{\s*method:\s*"POST"\s*\}\)/,
  "Subscription refresh should keep the same backend API call.",
);
assert.match(
  refreshBody,
  /invalidateSettingsCaches\(\["workspace",\s*"rules",\s*"health"\]\)/,
  "Subscription refresh should keep the same cache invalidation.",
);
assert.match(
  refreshBody,
  /await loadSettingsWorkspace\(\);/,
  "Subscription refresh should keep the same post-refresh.",
);
assert.doesNotMatch(
  refreshBody,
  /closest\(/,
  "Subscription refresh should not discover targets with closest().",
);

const uiAction = fs.readFileSync(path.join(root, "static/js/fwrouter-ui-action.js"), "utf8");
assert.match(uiAction, /function finishAction/, "ActionManager should keep a success path.");
assert.match(uiAction, /function failAction/, "ActionManager should keep an error path.");

const createMatch = settings.match(/async function createSettingsExternalClient\(form\) \{([\s\S]*?)\n  \}/);
assert.ok(createMatch, "createSettingsExternalClient should exist.");
const createBody = createMatch[1];

assert.match(
  createBody,
  /window\.FwrouterUIAction\.runAction\(\{/,
  "External client create should use the shared UI action lifecycle.",
);
assert.match(
  createBody,
  /id:\s*"settings\.external_client\.create"/,
  "External client create should provide a stable action id.",
);
assert.match(
  createBody,
  /button:\s*submit/,
  "External client create should use the create button as the button target.",
);
assert.match(
  createBody,
  /scope:\s*form/,
  "External client create should target only the create form scope.",
);
assert.match(
  createBody,
  /resultTarget:\s*el\("settingsExternalClientCreateState"\)/,
  "External client create should use the existing status node as result target.",
);
assert.match(
  createBody,
  /messageTarget:\s*el\("settingsExternalClientCreateState"\)/,
  "External client create should use the existing status node as message target.",
);
assert.match(
  createBody,
  /disable:\s*\[aliasInput,\s*emailInput,\s*submit,\s*toggle\]/,
  "External client create should disable form controls while running.",
);
assert.match(
  createBody,
  /pendingMessage:\s*"status\.saving"/,
  "External client create should preserve the previous pending message key.",
);
assert.match(
  createBody,
  /successMessage:\s*"settings\.external_client\.created"/,
  "External client create should preserve the previous success message key.",
);
assert.match(
  createBody,
  /failedMessage:\s*"status\.error_prefix"/,
  "External client create should use the existing localized error wrapper.",
);
assert.match(
  createBody,
  /fetchApiV2\("\/xray\/clients"[\s\S]*method:\s*"POST"/,
  "External client create should keep the same backend API call.",
);
assert.match(
  createBody,
  /toggleSettingsExternalClientCreate\(false\)[\s\S]*settingsClientsTab\s*=\s*"external_client"[\s\S]*invalidateSettingsCaches\(\["workspace",\s*"inventory",\s*"rules",\s*"health"\]\)[\s\S]*await loadSettingsWorkspace\(\);[\s\S]*await loadSettingsInventory\(\{\s*force:\s*true,\s*live_observations:\s*true\s*\}\);/,
  "External client create should keep the same successful refresh sequence.",
);
assert.match(
  createBody,
  /\}\)\.catch\(\(\)\s*=>\s*\{\}\);/,
  "External client create should keep errors handled by UI lifecycle without bubbling to the delegated submit handler.",
);
assert.doesNotMatch(
  createBody,
  /closest\(/,
  "External client create should not discover targets with closest().",
);

function bodyBetween(start, end) {
  const startIndex = settings.indexOf(start);
  assert.notStrictEqual(startIndex, -1, `${start} should exist.`);
  const endIndex = end ? settings.indexOf(end, startIndex + start.length) : -1;
  return settings.slice(startIndex, endIndex === -1 ? undefined : endIndex);
}

assert.match(
  settings,
  /const SETTINGS_TAB_STORAGE_KEY = "fwrouter\.ui\.settingsTab\.v1"/,
  "Settings should persist the last selected tab under a stable storage key.",
);
assert.match(
  settings,
  /let settingsTab = readStoredSettingsTab\(\);/,
  "Settings should restore the last tab before bootstrap loaders run.",
);
assert.match(
  settings,
  /function normalizeSettingsTab\(value\)[\s\S]*SETTINGS_TAB_VALUES\.has\(normalized\) \? normalized : "all"/,
  "Settings should validate restored tab values.",
);
assert.match(
  settings,
  /function persistSettingsTab\(value\)[\s\S]*window\.localStorage\.setItem\(SETTINGS_TAB_STORAGE_KEY, normalizeSettingsTab\(value\)\)/,
  "Settings tab changes should be saved to localStorage.",
);
assert.match(
  settings,
  /if \(source === "controls"\) \{[\s\S]*setSettingsTab\(source\);[\s\S]*syncSettingsTabs\(\);[\s\S]*loadSettingsProxyServers\(true\);[\s\S]*return;/,
  "Opening Settings Controls should persist the tab and force-refresh the proxy list.",
);
assert.match(
  settings,
  /if \(isJournalTab\(settingsTab\)\) \{[\s\S]*loadSettingsLogs\(\{ source: settingsTab \}\);[\s\S]*\} else if \(settingsTab === "controls"\) \{[\s\S]*loadSettingsProxyServers\(true\);/,
  "Reloading Settings on a restored Controls tab should immediately load the proxy list.",
);
assert.match(
  settings,
  /const custom = \(settingsServers \|\| \[\]\)\.filter\(\(server\) => String\(server\.kind \|\| ""\) === "custom_https_proxy"\)/,
  "Settings proxy rendering should keep showing custom proxy rows from the servers API.",
);

const migratedSimpleSettingsActions = [
  {
    name: "Rules save",
    body: bodyBetween("async function saveRules()", "async function saveVpnSubscriptionUrl()"),
    id: "settings.rules.save",
    api: /fetchApiV2\("\/rules\/manual"[\s\S]*method:\s*"POST"/,
    button: /button:\s*el\("rulesSave"\)/,
    scope: /scope:\s*document\.querySelector\("#settingsRulesPane \.settings-rules-editor"\)/,
    result: /resultTarget:\s*el\("rulesState"\)/,
    message: /messageTarget:\s*el\("rulesState"\)/,
    disable: /disable:\s*\[el\("rulesText"\),\s*el\("rulesSave"\)\]/,
    pending: /pendingMessage:\s*"status\.saving"/,
    success: /successMessage:\s*null/,
    refresh: /invalidateSettingsCaches\(\["rules",\s*"health"\]\)[\s\S]*await loadRulesUpstreamStatus\(\);/,
  },
  {
    name: "Proxy create",
    body: bodyBetween("async function createSettingsProxy()", "async function deleteSettingsProxy"),
    id: "settings.proxy.create",
    api: /fetchApiV2\("\/servers\/custom\/proxy"[\s\S]*method:\s*"POST"/,
    button: /button:\s*el\("settingsProxyCreate"\)/,
    scope: /scope:\s*settingsProxyScope\(\)/,
    result: /resultTarget:\s*el\("settingsProxyState"\)/,
    message: /messageTarget:\s*el\("settingsProxyState"\)/,
    disable: /disable:\s*settingsProxyControls\(\)/,
    pending: /pendingMessage:\s*"status\.saving"/,
    success: /successMessage:\s*"status\.ready"/,
    refresh: /setSettingsProxyType\("http"\)[\s\S]*await loadSettingsProxyServers\(true\);/,
  },
  {
    name: "Proxy delete",
    body: bodyBetween("async function deleteSettingsProxy", "async function saveSettingsItem"),
    id: "settings.proxy.delete",
    api: /fetchApiV2\(`\/servers\/custom\/proxy\/\$\{encodeURIComponent\(normalized\)\}\?requested_by=ui`[\s\S]*method:\s*"DELETE"/,
    button: /button:\s*triggerNode/,
    scope: /scope:\s*settingsProxyRowFromButton\(triggerNode\) \|\| el\("settingsProxyList"\)/,
    result: /resultTarget:\s*el\("settingsProxyState"\)/,
    message: /messageTarget:\s*el\("settingsProxyState"\)/,
    disable: /disable:\s*\[triggerNode\]/,
    pending: /pendingMessage:\s*"status\.deleting"/,
    success: /successMessage:\s*"status\.ready"/,
    refresh: /await loadSettingsProxyServers\(true\);/,
  },
  {
    name: "External client delete",
    body: bodyBetween("async function deleteSettingsExternalClient(clientId, triggerNode)", "async function deleteSettingsExternalClientGroup"),
    id: "settings.external_client.delete",
    api: /fetchApiV2\(`\/xray\/clients\/\$\{encodeURIComponent\(normalized\)\}`[\s\S]*method:\s*"DELETE"/,
    button: /button:\s*triggerNode/,
    scope: /scope:\s*getSettingsClientRow\(normalized\)/,
    result: /resultTarget:\s*el\("settingsClientsState"\)/,
    message: /messageTarget:\s*el\("settingsClientsState"\)/,
    disable: /disable:\s*\[triggerNode\]/,
    pending: /pendingMessage:\s*"status\.deleting"/,
    success: /successMessage:\s*"status\.ok"/,
    refresh: /invalidateSettingsCaches\(\["workspace",\s*"inventory",\s*"rules",\s*"health"\]\)[\s\S]*await loadSettingsWorkspace\(\);/,
  },
  {
    name: "External client group delete",
    body: bodyBetween("async function deleteSettingsExternalClientGroup", "async function deleteSettingsSystemSubject"),
    id: "settings.external_client_group.delete",
    api: /fetchApiV2\(`\/xray\/subscription-profiles\/\$\{encodeURIComponent\(token\)\}`[\s\S]*method:\s*"DELETE"/,
    button: /button:\s*triggerNode/,
    scope: /scope:\s*getSettingsClientRow\(normalized\)/,
    result: /resultTarget:\s*el\("settingsClientsState"\)/,
    message: /messageTarget:\s*el\("settingsClientsState"\)/,
    disable: /disable:\s*\[triggerNode\]/,
    pending: /pendingMessage:\s*"status\.deleting"/,
    success: /successMessage:\s*"status\.ok"/,
    refresh: /invalidateSettingsCaches\(\["workspace",\s*"inventory",\s*"rules",\s*"health"\]\)[\s\S]*await loadSettingsWorkspace\(\);/,
  },
  {
    name: "System subject delete",
    body: bodyBetween("async function deleteSettingsSystemSubject", "function toggleSettingsTrafficChoice"),
    id: "settings.system_subject.delete",
    api: /fetchApiV2\(`\/system-subjects\/\$\{encodeURIComponent\(normalized\)\}\?requested_by=ui`[\s\S]*method:\s*"DELETE"/,
    button: /button:\s*triggerNode/,
    scope: /scope:\s*getSettingsClientRow\(normalized\)/,
    result: /resultTarget:\s*el\("settingsClientsState"\)/,
    message: /messageTarget:\s*el\("settingsClientsState"\)/,
    disable: /disable:\s*\[triggerNode\]/,
    pending: /pendingMessage:\s*"status\.deleting"/,
    success: /successMessage:\s*"status\.ok"/,
    refresh: /invalidateSettingsCaches\(\["workspace",\s*"inventory",\s*"health"\]\)[\s\S]*await loadSettingsWorkspace\(\);/,
  },
];

migratedSimpleSettingsActions.forEach((action) => {
  assert.match(
    action.body,
    /window\.FwrouterUIAction\.runAction\(\{/,
    `${action.name} should use the shared UI action lifecycle.`,
  );
  assert.match(action.body, new RegExp(`id:\\s*"${action.id.replace(/[.]/g, "\\.")}"`), `${action.name} should provide a stable action id.`);
  assert.match(action.body, action.api, `${action.name} should keep the same backend API call.`);
  assert.match(action.body, action.button, `${action.name} should use an explicit button target.`);
  assert.match(action.body, action.scope, `${action.name} should use an explicit narrow scope target.`);
  assert.match(action.body, action.result, `${action.name} should use the existing status node as result target.`);
  assert.match(action.body, action.message, `${action.name} should use the existing status node as message target.`);
  assert.match(action.body, action.disable, `${action.name} should disable only explicit controls.`);
  assert.match(action.body, action.pending, `${action.name} should use the existing pending i18n key.`);
  assert.match(action.body, action.success, `${action.name} should preserve the previous success behavior.`);
  assert.match(action.body, /failedMessage:\s*"status\.error_prefix"/, `${action.name} should keep the existing localized error wrapper.`);
  assert.match(action.body, action.refresh, `${action.name} should keep the previous refresh side effects.`);
  assert.match(action.body, /\}\)\.catch\(/, `${action.name} should keep delegated handlers from receiving handled UI errors.`);
  assert.doesNotMatch(action.body, /closest\(/, `${action.name} should not discover lifecycle targets with closest().`);
});

assert.match(
  settings,
  /deleteSettingsExternalClient\(id,\s*deleteBtn\)[\s\S]*deleteSettingsExternalClientGroup\(id,\s*deleteBtn\)[\s\S]*deleteSettingsSystemSubject\(id,\s*deleteBtn\)/,
  "Delegated settings client delete handlers should pass the real action button target.",
);
assert.match(
  settings,
  /deleteSettingsProxy\(serverId,\s*deleteProxyBtn\)/,
  "Delegated proxy delete handler should pass the real action button target.",
);

const migratedMediumSettingsActions = [
  {
    name: "Subscription save",
    body: bodyBetween("async function saveVpnSubscriptionUrl()", "async function refreshVpnSubscription()"),
    id: "settings.subscription.save",
    api: /fetchApiV2\("\/subscription"[\s\S]*method:\s*"POST"/,
    button: /button:\s*el\("vpnSubscriptionSave"\)/,
    scope: /scope:\s*document\.querySelector\("#settingsControlsPane \.settings-subscription-card"\)/,
    result: /resultTarget:\s*el\("vpnSubscriptionState"\)/,
    message: /messageTarget:\s*el\("vpnSubscriptionState"\)/,
    disable: /disable:\s*\[[\s\S]*vpnSubscriptionUrlInputs\(\)[\s\S]*el\("vpnSubscriptionAddUrl"\)[\s\S]*el\("vpnSubscriptionSave"\)[\s\S]*\]/,
    pending: /pendingMessage:\s*"status\.saving"/,
    success: /successMessage:\s*"status\.ready"/,
    failed: /failedMessage:\s*\{\s*key:\s*"status\.error_prefix"[\s\S]*"settings\.subscription\.batch\.failed"/,
    refresh: /lastVpnSubscriptionBatchResult\s*=\s*data\?\.batch[\s\S]*invalidateSettingsCaches\(\["workspace",\s*"health",\s*"servers"\]\)[\s\S]*await loadSettingsWorkspace\(\);/,
  },
  {
    name: "Rules apply",
    body: bodyBetween("async function refreshRules()", "async function updateAllRules()"),
    id: "settings.rules.apply",
    api: /fetchApiV2\("\/rules\/manual\/apply"[\s\S]*method:\s*"POST"/,
    button: /button:\s*el\("rulesRefresh"\)/,
    scope: /scope:\s*document\.querySelector\("#settingsRulesPane \.settings-rules-editor"\)/,
    result: /resultTarget:\s*el\("rulesState"\)/,
    message: /messageTarget:\s*el\("rulesState"\)/,
    disable: /disable:\s*\[el\("rulesText"\),\s*el\("rulesRefresh"\),\s*el\("rulesSave"\)\]/,
    pending: /pendingMessage:\s*"status\.applying"/,
    success: /successMessage:\s*"status\.ok"/,
    failed: /failedMessage:\s*"status\.error_prefix"/,
    refresh: /invalidateSettingsCaches\(\["rules",\s*"health"\]\)[\s\S]*await loadRules\(\{\s*force:\s*true\s*\}\);/,
    errorSideEffect: /catch\(async \(e\)[\s\S]*await loadRulesUpstreamStatus\(\);[\s\S]*renderRulesStatus/,
  },
  {
    name: "Rules full update",
    body: bodyBetween("async function updateAllRules()", "async function saveRules()"),
    id: "settings.rules.full_update",
    api: /fetchApiV2\("\/rules\/full-update"[\s\S]*method:\s*"POST"/,
    button: /button:\s*el\("rulesRefreshAll"\)/,
    scope: /scope:\s*document\.querySelector\("#settingsRulesPane \.settings-rules-editor"\)/,
    result: /resultTarget:\s*el\("rulesState"\)/,
    message: /messageTarget:\s*el\("rulesState"\)/,
    disable: /disable:\s*\[el\("rulesText"\),\s*el\("rulesRefresh"\),\s*el\("rulesRefreshAll"\),\s*el\("rulesSave"\)\]/,
    pending: /pendingMessage:\s*"status\.refreshing"/,
    success: /successMessage:\s*null/,
    failed: /failedMessage:\s*"status\.error_prefix"/,
    refresh: /invalidateSettingsCaches\(\["rules",\s*"health",\s*"inventory"\]\)[\s\S]*await loadRules\(\{\s*force:\s*true\s*\}\);[\s\S]*await loadSettingsWorkspace\(\);[\s\S]*settings\.rules\.result\.updated/,
    errorSideEffect: /catch\(async \(e\)[\s\S]*await loadRulesUpstreamStatus\(\);/,
  },
  {
    name: "External connection create",
    body: bodyBetween("async function submitSettingsExternalSystem", "function buildSettingsConnectionPatchPayload"),
    id: "settings.external_connection.create",
    api: /fetchApiV2\("\/ui\/external-connections"[\s\S]*method:\s*"POST"/,
    button: /button:\s*submit/,
    scope: /scope:\s*form/,
    result: /resultTarget:\s*el\("settingsClientsState"\)/,
    message: /messageTarget:\s*el\("settingsClientsState"\)/,
    disable: /disable:\s*settingsFormControls\(form\)/,
    pending: /pendingMessage:\s*"status\.saving"/,
    success: /successMessage:\s*"status\.ok"/,
    failed: /failedMessage:\s*"status\.error_prefix"/,
    refresh: /closeSettingsExternalSystemDialog\(\)[\s\S]*invalidateSettingsCaches\(\["workspace",\s*"inventory",\s*"health"\]\)[\s\S]*await loadSettingsWorkspace\(\);[\s\S]*settingsClientsTab\s*=\s*"connections"/,
  },
  {
    name: "External connection update",
    body: bodyBetween("async function saveSettingsConnectionDetails", "function syncSettingsConnectionEditForm"),
    id: "settings.external_connection.update",
    api: /fetchApiV2\(`\/ui\/external-connections\/\$\{encodeURIComponent\(connectionId\)\}`[\s\S]*method:\s*"PATCH"/,
    button: /button:\s*submit/,
    scope: /scope:\s*form/,
    result: /resultTarget:\s*el\("settingsClientsState"\)/,
    message: /messageTarget:\s*el\("settingsClientsState"\)/,
    disable: /disable:\s*settingsFormControls\(form\)/,
    pending: /pendingMessage:\s*"status\.saving"/,
    success: /successMessage:\s*"status\.ok"/,
    failed: /failedMessage:\s*"status\.error_prefix"/,
    refresh: /invalidateSettingsCaches\(\["workspace",\s*"inventory",\s*"health"\]\)[\s\S]*await loadSettingsWorkspace\(\);[\s\S]*openSettingsConnectionDetails\(connectionId\);/,
  },
  {
    name: "External connection delete",
    body: bodyBetween("async function deleteSettingsExternalSystem", "function wire()"),
    id: "settings.external_connection.delete",
    api: /fetchApiV2\(`\/ui\/external-connections\/\$\{encodeURIComponent\(connectionId\)\}`[\s\S]*method:\s*"DELETE"/,
    button: /button,\s*\n\s*scope:\s*getSettingsSystemRow\(connectionId\) \|\| button/,
    scope: /scope:\s*getSettingsSystemRow\(connectionId\) \|\| button/,
    result: /resultTarget:\s*el\("settingsClientsState"\)/,
    message: /messageTarget:\s*el\("settingsClientsState"\)/,
    disable: /disable:\s*\[button\]/,
    pending: /pendingMessage:\s*"status\.deleting"/,
    success: /successMessage:\s*"status\.ok"/,
    failed: /failedMessage:\s*"status\.error_prefix"/,
    refresh: /closeSettingsConnectionDetails\(\)[\s\S]*invalidateSettingsCaches\(\["workspace",\s*"inventory",\s*"health"\]\)[\s\S]*await loadSettingsWorkspace\(\);[\s\S]*settingsClientsTab\s*=\s*"connections"/,
  },
  {
    name: "Subject display visibility",
    body: bodyBetween("async function toggleSettingsAdminVisibility", "async function saveSettingsDisplayFromSystems"),
    id: "settings.display.subject_visibility",
    api: /fetchApiV2\("\/ui\/settings\/display"[\s\S]*method:\s*"PUT"/,
    button: /button,\s*\n\s*scope:\s*row \|\| button/,
    scope: /scope:\s*row \|\| button/,
    result: /resultTarget:\s*el\("settingsClientsState"\)/,
    message: /messageTarget:\s*el\("settingsClientsState"\)/,
    disable: /disable:\s*\[button\]/,
    pending: /pendingMessage:\s*"status\.saving"/,
    success: /successMessage:\s*null/,
    failed: /failedMessage:\s*"status\.error_prefix"/,
    refresh: /applyDisplaySettings\(\);[\s\S]*renderSettingsClients\(\);[\s\S]*fwrouter:display-settings-updated/,
    errorSideEffect: /catch\(\(\) => \{[\s\S]*settingsHiddenSubjectIds[\s\S]*renderSettingsClients\(\);/,
  },
  {
    name: "System display visibility",
    body: bodyBetween("async function saveSettingsDisplayFromSystems", "function toggleSettingsSystemVisibility"),
    id: "settings.display.system_visibility",
    api: /fetchApiV2\("\/ui\/settings\/display"[\s\S]*method:\s*"PUT"/,
    button: /button:\s*triggerNode/,
    scope: /scope:\s*triggerNode/,
    result: /resultTarget:\s*el\("settingsClientsState"\)/,
    message: /messageTarget:\s*el\("settingsClientsState"\)/,
    disable: /disable:\s*\[triggerNode\]/,
    pending: /pendingMessage:\s*"status\.saving"/,
    success: /successMessage:\s*"status\.ok"/,
    failed: /failedMessage:\s*"status\.error_prefix"/,
    refresh: /applyDisplaySettings\(\);[\s\S]*invalidateSettingsCaches\(\["workspace",\s*"inventory"\]\)[\s\S]*await loadSettingsWorkspace\(\);/,
  },
];

migratedMediumSettingsActions.forEach((action) => {
  assert.match(action.body, /window\.FwrouterUIAction\.runAction\(\{/, `${action.name} should use the shared UI action lifecycle.`);
  assert.match(action.body, new RegExp(`id:\\s*"${action.id.replace(/[.]/g, "\\.")}"`), `${action.name} should provide a stable action id.`);
  assert.match(action.body, action.api, `${action.name} should keep the same backend API call.`);
  assert.match(action.body, action.button, `${action.name} should use an explicit button target.`);
  assert.match(action.body, action.scope, `${action.name} should use an explicit narrow scope target.`);
  assert.match(action.body, action.result, `${action.name} should use the existing status node as result target.`);
  assert.match(action.body, action.message, `${action.name} should use the existing status node as message target.`);
  assert.match(action.body, action.disable, `${action.name} should disable only explicit controls.`);
  assert.match(action.body, action.pending, `${action.name} should use the existing pending i18n key.`);
  assert.match(action.body, action.success, `${action.name} should preserve the previous success behavior.`);
  assert.match(action.body, action.failed, `${action.name} should keep localized error behavior.`);
  assert.match(action.body, action.refresh, `${action.name} should keep previous refresh side effects.`);
  if (action.errorSideEffect) assert.match(action.body, action.errorSideEffect, `${action.name} should keep its previous error side effect.`);
});

const saveSettingsItemBody = bodyBetween(
  "async function saveSettingsItem(subjectId, forcedMode, triggerNode)",
  "async function deleteSettingsExternalClient",
);

assert.match(
  saveSettingsItemBody,
  /window\.FwrouterUIAction\.runAction\(\{/,
  "Settings item save should use the shared UI action lifecycle.",
);
assert.match(
  saveSettingsItemBody,
  /id:\s*"settings\.client\.save"/,
  "Settings item save should provide a stable action id.",
);
assert.match(
  saveSettingsItemBody,
  /button:\s*actionButton[\s\S]*scope:\s*row \|\| actionButton[\s\S]*resultTarget:\s*el\("settingsClientsState"\)[\s\S]*messageTarget:\s*el\("settingsClientsState"\)/,
  "Settings item save should use explicit button, row scope, result target, and message target.",
);
assert.match(
  saveSettingsItemBody,
  /disable:\s*controls/,
  "Settings item save should disable only controls from the current row.",
);
assert.match(
  saveSettingsItemBody,
  /pendingMessage:\s*"status\.saving"[\s\S]*successMessage:\s*"status\.ok"[\s\S]*failedMessage:\s*"status\.error_prefix"/,
  "Settings item save should use existing i18n lifecycle keys.",
);
assert.match(
  saveSettingsItemBody,
  /settingsClientActionAdapter\(client\)[\s\S]*fetchApiV2\(`\/xray\/clients\/\$\{encodeURIComponent\(clientId\)\}`[\s\S]*method:\s*"PATCH"/,
  "Settings item save should keep the Xray client PATCH branch.",
);
assert.match(
  saveSettingsItemBody,
  /fetchApiV2\(`\/subjects\/\$\{encodeURIComponent\(normalized\)\}\/alias`[\s\S]*method:\s*"PATCH"/,
  "Settings item save should keep the subject alias PATCH branch.",
);
assert.match(
  saveSettingsItemBody,
  /return fetchApiV2\(`\/subjects\/\$\{encodeURIComponent\(normalized\)\}\/mode`[\s\S]*method:\s*"POST"[\s\S]*actor_scope:\s*"admin"[\s\S]*run_now:\s*false/,
  "Settings item save should keep the subject mode POST payload.",
);
assert.match(
  saveSettingsItemBody,
  /job:\s*\(modeAction\) => modeAction\?\.job\?\.job_id/,
  "Settings item save should expose the mode job id to ActionManager polling.",
);
assert.match(
  saveSettingsItemBody,
  /onProgress:\s*\(status\) => status === "queued" \? "status\.queued" : "status\.applying"/,
  "Settings item save should keep queued/applying progress messages through ActionManager.",
);
assert.match(
  saveSettingsItemBody,
  /settingsTrafficPreferences\[normalized\] = selectedTraffic[\s\S]*fetchApiV2\("\/ui\/settings\/display"[\s\S]*method:\s*"PUT"[\s\S]*invalidateSettingsCaches\(\["workspace",\s*"inventory",\s*"rules",\s*"health"\]\)[\s\S]*await loadSettingsWorkspace\(\);/,
  "Settings item save should keep traffic/display update and workspace refresh.",
);
assert.match(
  saveSettingsItemBody,
  /return \{ resultTarget: freshRow \|\| freshSaveButton \|\| freshModeSelect \|\| actionButton \};/,
  "Settings item save should retarget success feedback after row refresh.",
);
assert.doesNotMatch(
  saveSettingsItemBody,
  /setPendingStateMany|setPendingScope|finally\s*\{/,
  "Settings item save should not keep manual pending lifecycle cleanup.",
);

console.log("fwrouter settings UI action integration contract ok");
