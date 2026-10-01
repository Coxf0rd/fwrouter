const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
global.window = global;
global.document = {
  documentElement: { dataset: { locale: "en" }, lang: "en", style: { setProperty: () => {} } },
  addEventListener: () => {},
  dispatchEvent: () => true,
  querySelectorAll: () => [],
};
global.FwrouterUI = {
  escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[char]));
  },
  countryCodeToFlagEmoji: (code) => String(code || "").toUpperCase(),
  flagEmojiToCountryCode: () => "",
  stripLeadingFlagEmoji: (value) => String(value || ""),
};
vm.runInThisContext(fs.readFileSync(path.join(root, "static/js/fwrouter-user-servers.js"), "utf8"));
vm.runInThisContext(fs.readFileSync(path.join(root, "static/js/fwrouter-i18n.js"), "utf8"));
vm.runInThisContext(fs.readFileSync(path.join(root, "static/js/fwrouter-admin-autolist.js"), "utf8"));

const members = [
  {
    member_id: "sub:runtime-hash-a", provider_member_id: "secret-looking-provider-id-101",
    provider_source_ref: "src:" + "a".repeat(64), provider_protocol: "hysteria2",
    provider_binding_revision: 8, location_id: "us", location_label: "United States",
    provider_member_ordinal: 1, is_provider_member: true, is_provider_current: true,
    is_provider_applied: false, is_effective_active: false, auto_enabled: true, priority: 2,
    provider_advertised: false, is_active: true, status: "unknown", latency_ms: null,
  },
  {
    member_id: "sub:runtime-hash-b", provider_member_id: "provider-id-202",
    provider_source_ref: "src:" + "a".repeat(64), provider_protocol: "hysteria2",
    provider_binding_revision: 8, location_id: "us", location_label: "United States",
    provider_member_ordinal: 2, is_provider_member: true, is_provider_current: false,
    is_provider_applied: true, is_effective_active: true, auto_enabled: false, priority: -1,
    is_active: true, status: "healthy", latency_ms: 91,
  },
];

let html = global.FwrouterAdminAutolist.renderTopologyMembersHtml(members, false);
assert.match(html, /United States/);
assert.match(html, /Current/);
assert.match(html, /Applied/);
assert.match(html, /Runtime effective/);
assert.match(html, /Auto select/);
assert.match(html, /data-provider-switch/);
assert.match(html, /data-provider-auto/);
assert.match(html, /data-provider-priority/);
assert.match(html, /No data/);
assert.match(html, /91 ms/);
assert.ok(html.indexOf("Provider server 1") < html.indexOf("secret-looking-provider-id-101"));
assert.match(html, /Provider server 1<small class="admin-provider-member-id">secret-looking-provider-id-101<\/small>/);
assert.match(html, /data-provider-location="us"/);
assert.match(html, /data-provider-protocol="hysteria2"/);

global.FwrouterI18n.setLocale("ru");
html = global.FwrouterAdminAutolist.renderTopologyMembersHtml([members[0]], false);
assert.match(html, /United States/);
assert.match(html, /Текущий/);
assert.match(html, /Автовыбор/);
assert.match(html, /Нет данных/);

console.log("Admin provider member presentation passed");

const adminSource = fs.readFileSync(path.join(root, 'static/js/admin.js'), 'utf8');
const actionSource = adminSource.match(/async function submitProviderMemberAction\(control, action, values\) \{[\s\S]*?\n  \}/)?.[0];
assert.ok(actionSource);
(async () => {
  const requests=[], refreshed=[];
  const target={dataset:{topologyMembers:'provider:logical'},querySelector:()=>({textContent:''}),querySelectorAll:()=>[]};
  const control={dataset:{providerSource:'src:'+'a'.repeat(64),providerMember:'1456',providerLocation:'26',providerProtocol:'hysteria2',providerRevision:'8'},closest:()=>target};
  const context={String,Number,encodeURIComponent,t:global.FwrouterI18n.t,actionMessage:String,
    window:{FwrouterUIAction:{runAction:async spec=>{await spec.action();await spec.refresh();}}},
    dataStore:{invalidate:names=>refreshed.push(names.join(','))},
    fetchApiV2:async(url,opts)=>{requests.push({url,body:JSON.parse(opts.body)});return{job_id:'fixture-job'};},
    loadAdminVpnOverview:async()=>refreshed.push('overview'),
    loadAutolist:async opts=>{assert.strictEqual(opts.liveMeasure,false);refreshed.push('canonical-list');},
  };
  vm.createContext(context);vm.runInContext(actionSource,context);
  await context.submitProviderMemberAction(control,'preferences',{auto:false,priority:-1});
  assert.strictEqual(requests.length,1);
  assert.deepStrictEqual(requests[0].body,{action:'preferences',member_id:'1456',location_id:'26',protocol:'hysteria2',expected_revision:8,auto:false,priority:-1});
  assert.ok(refreshed.includes('canonical-list'),'Provider action must refresh parent canonical Admin projection');
})().catch(error=>{console.error(error);process.exitCode=1;});
