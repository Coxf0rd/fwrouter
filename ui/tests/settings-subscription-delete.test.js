const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const settings = fs.readFileSync(path.join(root, "static/js/settings.js"), "utf8");
const i18n = fs.readFileSync(path.join(root, "static/js/fwrouter-i18n.js"), "utf8");
const css = fs.readFileSync(path.join(root, "static/css/settings-view.css"), "utf8");

const actions = html.match(/<div id="vpnSubscriptionActions"[\s\S]*?<\/div>/)?.[0] || "";
assert.match(actions, /id="vpnSubscriptionDelete"[^>]*data-i18n="html\.settings\.delete_saved_subscription"/);
assert.ok(actions.indexOf("vpnSubscriptionDelete") < actions.indexOf("vpnSubscriptionSave"), "Saved-source Delete must precede Save in the bottom action row.");
assert.match(actions, /vpnSubscriptionDeleteSource/);
assert.match(actions, /vpnSubscriptionRefresh/);
assert.match(settings, /function renderVpnSubscriptionDeleteSources\(subscription\)[\s\S]*source_ref/);
assert.match(settings, /window\.FwrouterLiquidSelect\?\.refresh\(enhancedSelect\?\.parentElement \|\| select\.parentElement\)/,
  "Saved source selector must sync its visible custom control after options change.");
assert.match(settings, /enhancedSelect\.style\.display = showSourceSelect \? "" : "none"/,
  "The custom selector must hide when no source choice is required.");
assert.match(settings, /document\.addEventListener\("fwrouter:locale",[\s\S]*renderVpnSubscriptionDeleteSources\(settingsWorkspace\?\.subscription\)/,
  "Saved source labels must refresh when the locale changes.");
assert.match(settings, /async function deleteVpnSubscriptionSource\(\)[\s\S]*\/subscription\/sources\/\$\{encodeURIComponent\(sourceRef\)\}[\s\S]*job:[\s\S]*result\?\.job\?\.job_id/);
assert.match(settings, /deleteVpnSubscriptionSource\);/);
assert.match(settings, /data-vpn-subscription-remove/);
assert.doesNotMatch(settings, /data-vpn-subscription-remove[\s\S]{0,250}method:\s*"DELETE"/,
  "Draft URL Remove must not trigger saved-source deletion.");
assert.match(settings, /settings\.subscription\.delete\.confirm/);
assert.match(settings, /details\?\.source\?\.deleted === true[\s\S]*settings\.subscription\.delete\.partial/,
  "A post-intent apply failure must be presented as partial deletion.");
assert.match(settings, /catch\(async \(error\) => \{[\s\S]*reloadSubscriptionProjection\(\)[\s\S]*subscriptionOperationErrorMessage\(error\)/);
assert.match(settings, /settings\.subscription\.batch\.partial/);
assert.match(settings, /settings-subscription-batch-result__visible-errors[\s\S]*failureReason\(item\)/);
assert.match(settings, /catch\(async \(error\) => \{[\s\S]*error\?\.payload\?\.data\?\.batch[\s\S]*url_label: t\("settings\.subscription\.batch\.url_index"[\s\S]*renderVpnSubscriptionBatchResult\(\)[\s\S]*reloadSubscriptionProjection\(\)/,
  "Rejected Save responses should expose sanitized per-source failures before authoritative reload.");
assert.match(settings, /settings-subscription-batch-result__details[\s\S]*item\.error\?\.code/);
assert.match(css, /#vpnSubscriptionDelete[\s\S]*rgba\(154, 52, 52/);
assert.match(css, /settings-client-row__meta-wrap \.settings-client-row__delete-near-link\.btn--danger[\s\S]*rgba\(154, 52, 52/);
assert.match(i18n, /"html\.settings\.delete_saved_subscription": "Удалить"/);
assert.match(i18n, /"html\.settings\.delete_saved_subscription": "Delete"/);
assert.match(i18n, /"settings\.subscription\.delete\.confirm"/);
assert.match(i18n, /"events\.code\.subscription\.source_delete_requested": "Удаление источника подписки запрошено"/);
assert.match(i18n, /"events\.type\.subscription_source_delete_apply_failed": "Не удалось завершить удаление источника подписки"/);

console.log("fwrouter saved subscription delete UI contract ok");
