const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const admin = fs.readFileSync(path.join(root, "static/js/admin.js"), "utf8");
const user = fs.readFileSync(path.join(root, "static/js/user.js"), "utf8");
const i18n = fs.readFileSync(path.join(root, "static/js/fwrouter-i18n.js"), "utf8");

const adminAction = admin.slice(admin.indexOf("async function runAutolistManualCheck"), admin.indexOf("async function loadAutolistPickPingData"));
assert.match(adminAction, /selectedAutolistServerKey/);
assert.match(adminAction, /`\/servers\/\$\{encodeURIComponent\(serverId\)\}\/manual-check`/);
assert.match(adminAction, /disable:\s*\[el\("autolistPing"\)\]/);
assert.match(adminAction, /manual_check\.(success|partial|failed)/);

const userAction = user.slice(user.indexOf("async function runUserManualCheck"), user.indexOf("async function runSubjectProxyGetCheck"));
assert.match(userAction, /resolveManualCheckServerId/);
assert.match(userAction, /`\/servers\/\$\{encodeURIComponent\(serverId\)\}\/manual-check`/);
assert.match(userAction, /disable:\s*\[button\]/);
assert.match(userAction, /manual_check\.(success|partial|failed)/);

assert.doesNotMatch(admin, /fetchApiV2\(["'`]\/server-ping\/sweep/);
assert.doesNotMatch(user, /fetchApiV2\(["'`]\/server-ping\/sweep/);
assert.match(admin, /topology\.effective_latency_ms/);
assert.match(user, /topology\.effective_latency_ms/);
assert.match(i18n, /"manual_check\.success"/);
assert.match(i18n, /"manual_check\.partial"/);
assert.match(i18n, /"manual_check\.failed"/);

console.log("fwrouter manual check UI contract ok");
