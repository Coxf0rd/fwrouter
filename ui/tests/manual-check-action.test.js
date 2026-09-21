const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const admin = fs.readFileSync(path.join(root, "static/js/admin.js"), "utf8");
const adminAutolist = fs.readFileSync(path.join(root, "static/js/fwrouter-admin-autolist.js"), "utf8");
const user = fs.readFileSync(path.join(root, "static/js/user.js"), "utf8");
const i18n = fs.readFileSync(path.join(root, "static/js/fwrouter-i18n.js"), "utf8");

const adminAction = admin.slice(admin.indexOf("async function runAutolistManualCheck"), admin.indexOf("async function loadAutolistPickPingData"));
assert.doesNotMatch(adminAction, /selectedAutolistServerKey/);
assert.match(adminAction, /fetchApiV2\("\/servers\/manual-check"/);
assert.match(adminAction, /scope: "admin_all"/);
assert.doesNotMatch(adminAutolist, /manual_result/);
assert.doesNotMatch(adminAutolist, /member_column\.manual/);
assert.match(adminAction, /loadAutolist\(\{ liveMeasure: false, skipOverview: true \}\);\s*setDynamicStatus/);
assert.match(adminAction, /disable:\s*\[el\("autolistPing"\)\]/);
assert.match(adminAction, /manual_check\.global_(success|partial|failed)/);

const userAction = user.slice(user.indexOf("async function runUserManualCheck"), user.indexOf("async function runSubjectProxyGetCheck"));
assert.doesNotMatch(userAction, /resolveManualCheckServerId/);
assert.match(userAction, /fetchApiV2\("\/servers\/manual-check"/);
assert.match(userAction, /body: JSON\.stringify\(\{ scope, /);
assert.match(user, /runUserManualCheck\("user_vpn_auto"/);
assert.match(user, /runUserManualCheck\("user_global"/);
assert.doesNotMatch(user, /manualCellHtml/);
assert.match(userAction, /loadServersBasic\(\{ skipIpRefresh: true \}\);\s*setDynamicStatus/);
assert.match(userAction, /disable:\s*\[button\]/);
assert.match(userAction, /manual_check\.global_(success|partial|failed)/);

assert.doesNotMatch(admin, /fetchApiV2\(["'`]\/server-ping\/sweep/);
assert.doesNotMatch(user, /fetchApiV2\(["'`]\/server-ping\/sweep/);
assert.match(admin, /topology\.effective_latency_ms/);
assert.match(user, /topology\.effective_latency_ms/);
assert.match(i18n, /"manual_check\.success"/);
assert.match(i18n, /"manual_check\.partial"/);
assert.match(i18n, /"manual_check\.failed"/);

console.log("fwrouter manual check UI contract ok");
