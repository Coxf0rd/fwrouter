const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const admin = fs.readFileSync(path.join(root, "static/js/admin.js"), "utf8");
const user = fs.readFileSync(path.join(root, "static/js/user.js"), "utf8");

function bodyBetween(source, start, end) {
  const startIndex = source.indexOf(start);
  assert.notStrictEqual(startIndex, -1, `${start} should exist.`);
  const endIndex = end ? source.indexOf(end, startIndex + start.length) : -1;
  return source.slice(startIndex, endIndex === -1 ? undefined : endIndex);
}

function assertMigratedAction(action) {
  assert.match(action.body, /window\.FwrouterUIAction\.runAction\(\{/, `${action.name} should use runAction.`);
  assert.match(action.body, new RegExp(`id:\\s*"${action.id.replace(/[.]/g, "\\.")}"`), `${action.name} should expose a stable action id.`);
  assert.match(action.body, action.api, `${action.name} should keep its API call.`);
  assert.match(action.body, action.scope, `${action.name} should use the expected explicit scope.`);
  assert.match(action.body, action.disable, `${action.name} should disable explicit controls.`);
  assert.match(action.body, action.pending, `${action.name} should keep the pending i18n key.`);
  assert.match(action.body, action.failed || /failedMessage:\s*"status\.error_prefix"/, `${action.name} should keep localized error handling.`);
  assert.match(action.body, /\}\)\.catch\(/, `${action.name} should handle UI lifecycle errors locally.`);
  assert.doesNotMatch(action.body, /setPendingState|setPendingStateMany|setPendingScope|flashScopeResult/, `${action.name} should not keep manual lifecycle helpers.`);
  assert.doesNotMatch(action.body, /closest\(/, `${action.name} should not discover lifecycle targets with closest().`);
}

[
  {
    name: "Admin global mode save",
    body: bodyBetween(admin, "async function saveAdminGlobalMode", "function sortedAutolistServers"),
    id: "admin.global_mode.save",
    api: /fetchApiV2\("\/routing\/global"[\s\S]*method:\s*"POST"[\s\S]*mode:\s*next\.toLowerCase\(\)[\s\S]*requested_by:\s*"ui"[\s\S]*run_now:\s*false/,
    scope: /scope:\s*scopeNode/,
    disable: /disable:\s*controls/,
    pending: /pendingMessage:\s*"status\.saving"/,
    refresh: /confirm:\s*async \(\) => waitForAppliedState[\s\S]*loadAdminVpnOverview[\s\S]*adminCurrentMode === next/,
  },
  {
    name: "Admin autolist reset",
    body: bodyBetween(admin, "async function resetAutolistManualServer", "function ensureAdminGlobalPills"),
    id: "admin.autolist.reset_manual_server",
    api: /fetchApiV2\("\/routing\/global\/fixed-server\?confirm_switch=true&requested_by=ui"[\s\S]*method:\s*"DELETE"/,
    scope: /scope:\s*applyButton/,
    disable: /disable:\s*\[applyButton\]/,
    pending: /pendingMessage:\s*"admin\.status\.return_auto"/,
    refresh: /setDevAdminCurrentProxy\(""\)[\s\S]*loadAdminVpnOverview\(\{ silent: true \}\)[\s\S]*loadAutolist\(\{ liveMeasure: false, skipOverview: true \}\)/,
  },
  {
    name: "Admin autolist activate",
    body: bodyBetween(admin, "async function activateAutolistServer", "async function loadAutolistPickPingData"),
    id: "admin.autolist.activate_server",
    api: /fetchApiV2\("\/routing\/global\/fixed-server"[\s\S]*method:\s*"POST"[\s\S]*server_id:\s*String\(match\.server_id\)[\s\S]*confirm_switch:\s*true/,
    scope: /scope:\s*applyButton/,
    disable: /disable:\s*\[applyButton\]/,
    pending: /pendingMessage:\s*"admin\.status\.switching"/,
    refresh: /setDevAdminCurrentProxy\(serverName\)[\s\S]*loadAdminVpnOverview\(\{ silent: true \}\)[\s\S]*loadAutolist\(\{ liveMeasure: false, skipOverview: true \}\)/,
  },
  {
    name: "Admin selective default",
    body: bodyBetween(admin, "async function saveSelectiveDefault", "async function saveRouterSelfMode"),
    id: "admin.selective_default.save",
    api: /fetchApiV2\("\/routing\/global"[\s\S]*selective_default:\s*String\(selDef\)\.toLowerCase\(\)[\s\S]*requested_by:\s*"ui"[\s\S]*run_now:\s*true/,
    scope: /scope:\s*selectNode/,
    disable: /disable:\s*\[selectNode\]/,
    pending: /pendingMessage:\s*"status\.saving"/,
    refresh: /job:\s*\(action\) => action\?\.job\?\.job_id[\s\S]*confirm:\s*async \(\) => waitForAppliedState[\s\S]*loadAdminVpnOverview\(\{ silent: true \}\)/,
  },
  {
    name: "Admin device save",
    body: bodyBetween(admin, "async function saveAdminDevice", "function getAdminVlessClientRow"),
    id: "admin.device.save",
    api: /fetchApiV2\(`\/subjects\/\$\{encodeURIComponent\(normalized\)\}\/alias`[\s\S]*method:\s*"PATCH"[\s\S]*fetchApiV2\(`\/subjects\/\$\{encodeURIComponent\(normalized\)\}\/mode`[\s\S]*actor_scope:\s*"admin"[\s\S]*run_now:\s*false/,
    scope: /scope:\s*row \|\| saveButton \|\| modeSelect/,
    disable: /disable:\s*\[aliasInput,\s*modeSelect,\s*saveButton\]/,
    pending: /pendingMessage:\s*"status\.saving"/,
    refresh: /job:\s*\(action\) => action\?\.job\?\.job_id[\s\S]*waitForAppliedState[\s\S]*return \{ resultTarget: freshRow \|\| freshSaveButton \|\| freshModeSelect \|\| row \|\| saveButton \|\| modeSelect \};/,
  },
  {
    name: "Admin VLESS rename",
    body: bodyBetween(admin, "async function saveAdminVlessClientName", "async function deleteAdminVlessClient"),
    id: "admin.vless_client.rename",
    api: /fetchApiV2\(`\/xray\/clients\/\$\{encodeURIComponent\(clientId\)\}`[\s\S]*method:\s*"PATCH"[\s\S]*alias:\s*name \|\| null[\s\S]*requested_by:\s*"ui"/,
    scope: /scope:\s*row \|\| triggerNode \|\| input/,
    disable: /disable:\s*\[input,\s*triggerNode\]/,
    pending: /pendingMessage:\s*"status\.saving"/,
    refresh: /await loadAdminVlessClients\(false\)[\s\S]*return \{ resultTarget: getAdminVlessClientRow\(clientId\) \|\| triggerNode \|\| input \};/,
  },
  {
    name: "Admin VLESS delete",
    body: bodyBetween(admin, "async function deleteAdminVlessClient", "function wire"),
    id: "admin.vless_client.delete",
    api: /fetchApiV2\(`\/xray\/clients\/\$\{encodeURIComponent\(clientId\)\}`[\s\S]*method:\s*"DELETE"[\s\S]*requested_by:\s*"ui"/,
    scope: /scope:\s*row \|\| triggerNode/,
    disable: /disable:\s*\[triggerNode\]/,
    pending: /pendingMessage:\s*"status\.deleting"/,
    refresh: /window\.confirm\(t\("admin\.confirm\.delete_vless"\)\)[\s\S]*await loadAdminVlessClients\(true\)/,
  },
].forEach((action) => {
  assertMigratedAction(action);
  assert.match(action.body, action.refresh, `${action.name} should keep its refresh/apply confirmation path.`);
});

[
  {
    name: "User power target apply",
    body: bodyBetween(user, "async function onPowerClick", "async function loadRouting"),
    id: "user.power.apply_target",
    api: /action:\s*async \(\) => applyTarget\(target\)[\s\S]*job:\s*\(result\) => result\?\.response\?\.job\?\.job_id/,
    scope: /scope:\s*power/,
    disable: /disable:\s*\[power\]/,
    pending: /pendingMessage:\s*"status\.applying"/,
    refresh: /waitForAppliedState\(loadUserServerOverride[\s\S]*forceRefreshIpsAfterSwitch\(\)[\s\S]*loadServersBasic\(\{ skipIpRefresh: true \}\)/,
  },
  {
    name: "User mode save",
    body: bodyBetween(user, "async function saveUserMode", "async function switchUserMode"),
    id: "user.mode.save",
    api: /fetchApiV2\(`\/subjects\/\$\{encodeURIComponent\(currentSubjectId\)\}\/mode`[\s\S]*method:\s*"POST"[\s\S]*actor_scope:\s*"user"[\s\S]*run_now:\s*false/,
    scope: /scope:\s*scopeNode/,
    disable: /disable:\s*controls/,
    pending: /pendingMessage:\s*"status\.saving"/,
    refresh: /job:\s*\(action\) => action\?\.job\?\.job_id[\s\S]*confirm:\s*async \(\) => waitForAppliedState[\s\S]*currentUserMode === safe/,
  },
  {
    name: "User mode reset to global",
    body: bodyBetween(user, "async function resetUserModeToGlobal", "async function refreshRuntimeOnReturn"),
    id: "user.mode.reset_global",
    api: /fetchApiV2\(\s*`\/subjects\/\$\{encodeURIComponent\(currentSubjectId\)\}\/mode\?requested_by=ui&run_now=false`[\s\S]*method:\s*"DELETE"/,
    scope: /scope:\s*scopeNode/,
    disable: /disable:\s*controls/,
    pending: /pendingMessage:\s*"user\.mode\.returning_global"/,
    refresh: /job:\s*\(action\) => action\?\.job\?\.job_id[\s\S]*waitForAppliedState\(loadRouting[\s\S]*loadClientExternalIpPair/,
  },
].forEach((action) => {
  assertMigratedAction(action);
  assert.match(action.body, action.refresh, `${action.name} should keep its refresh/apply confirmation path.`);
  assert.doesNotMatch(action.body, /scope:\s*el\("user-top"\)|scope:\s*document\.querySelector\("#user-top|user-hero|page container/, `${action.name} should not use a broad User scope.`);
});

assert.doesNotMatch(admin, /setPendingState|setPendingStateMany|setPendingScope|flashScopeResult|createPendingHelpers|pollJob|actionMessage/, "Admin should not import or call manual mutation lifecycle helpers.");
assert.doesNotMatch(user, /setPendingState|setPendingStateMany|setPendingScope|flashScopeResult|createPendingHelpers|pollJob|actionMessage/, "User should not import or call manual mutation lifecycle helpers.");

console.log("fwrouter admin/user UI action integration contract ok");
