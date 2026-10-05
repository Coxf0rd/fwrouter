import { chromium } from '/root/.npm/_npx/9833c18b2d85bc59/node_modules/playwright/index.mjs';
import fs from 'node:fs';

const OUT = new URL('../UI_PERFORMANCE.json', import.meta.url);
const MAX_PHASE_MS = 20_000;
const RUNS = 2;
const browser = await chromium.connectOverCDP('http://127.0.0.1:9222');
const context = browser.contexts()[0];
if (!context) throw new Error('No existing CDP context');
const existing = context.pages().find(p => p.url().startsWith('http://127.0.0.1:5500/'));
if (!existing) throw new Error('No existing authenticated FWRouter UI page at 127.0.0.1:5500');
const origin = new URL(existing.url()).origin;
const originalUiPrefs = await existing.evaluate(() => ({ view: localStorage.getItem('fwrouter:view'), locale: localStorage.getItem('fwrouter.locale') }));

const routeLabel = raw => {
  const u = new URL(raw);
  let p = u.pathname.replace(/^\/api\/v2/, '') || '/';
  p = p.replace(/\/(?:[0-9a-f]{8}-[0-9a-f-]{27,}|[A-Za-z0-9_-]{24,})(?=\/|$)/g, '/{id}');
  p = p.replace(/(\/(?:servers|subjects|system-subjects|xray\/clients|xray\/subscription-profiles|subscription\/sources|ui\/external-connections)\/)[^/]+/g, '$1{id}');
  const allowed = ['role','limit','inventory_state','global_list','vpn_auto','include_inactive','include_provider_legacy','live_observations'];
  const q = new URLSearchParams();
  for (const key of allowed) {
    const value = u.searchParams.get(key);
    if (value !== null && /^[A-Za-z0-9_-]{1,40}$/.test(value)) q.set(key, value);
  }
  const suffix = q.toString();
  return `${p}${suffix ? `?${suffix}` : ''}`;
};

const metrics = async session => {
  const { metrics = [] } = await session.send('Performance.getMetrics');
  const out = {};
  for (const item of metrics) if ([
    'Timestamp','Nodes','JSHeapUsedSize','JSHeapTotalSize','TaskDuration','ScriptDuration','LayoutDuration','RecalcStyleDuration'
  ].includes(item.name)) out[item.name] = item.value;
  return out;
};

const runs = [];
for (let runIndex = 0; runIndex < RUNS; runIndex++) {
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Performance.enable');
const cacheMode = runIndex === 0 ? 'page_cache_bypassed' : 'repeat_page_same_context';
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: runIndex === 0 });
  await page.setViewportSize({ width: 1440, height: 1000 });
  const requestStart = new Map();
  const responses = [];
  const apiCounts = new Map();
  const errors = [];
  let externalIpBlocked = false;
  let mutationCount = 0;
  let rejectedNonReadApiCount = 0;
  await page.route('**/api/v2/**', async route => {
    const u = new URL(route.request().url());
    if (u.pathname === '/api/v2/ui/external-ip') {
      externalIpBlocked = true;
      await route.abort();
      return;
    }
    const method = route.request().method();
    if (method !== 'GET' && method !== 'HEAD') {
      rejectedNonReadApiCount++;
      await route.abort();
      return;
    }
    await route.continue();
  });
  await page.addInitScript(() => {
    window.__uiPerfLongTasks = { count: 0, total_ms: 0, max_ms: 0 };
    try {
      new PerformanceObserver(list => {
        for (const e of list.getEntries()) {
          const x = window.__uiPerfLongTasks;
          x.count += 1; x.total_ms += e.duration; x.max_ms = Math.max(x.max_ms, e.duration);
        }
      }).observe({ type: 'longtask', buffered: true });
    } catch {}
  });
  page.on('request', req => {
    const u = new URL(req.url());
    if (!u.pathname.startsWith('/api/v2/')) return;
    if (req.method() !== 'GET' && req.method() !== 'HEAD') mutationCount++;
    const route = routeLabel(req.url());
    const key = `${req.method()} ${route}`;
    const row = apiCounts.get(key) || { count: 0, statuses: {}, timings_ms: [], transfer_bytes: [], encoded_body_bytes: [] };
    row.count++;
    apiCounts.set(key, row);
    requestStart.set(req, { key, started_at_ms: Date.now() });
  });
  page.on('response', res => {
    const req = res.request();
    const rec = requestStart.get(req);
    if (!rec) return;
    const row = apiCounts.get(rec.key);
    row.statuses[res.status()] = (row.statuses[res.status()] || 0) + 1;
    responses.push({ route: rec.key, status: res.status() });
  });
  page.on('pageerror', e => errors.push(e.name || 'pageerror'));
  page.on('console', msg => { if (msg.type() === 'error') errors.push('console.error'); });

  const phase = async (name, startTime, resourceStart = 0, baselineOverride = null, longTaskBaseline = { count: 0, total_ms: 0, max_ms: 0 }) => {
    const started = Date.now();
    const before = baselineOverride || await metrics(cdp);
    let ready = false;
    let usableSignal = null;
    const until = Math.min(started + MAX_PHASE_MS, startTime + MAX_PHASE_MS);
    while (Date.now() < until) {
      usableSignal = await page.evaluate(name => {
        const count = sel => document.querySelectorAll(sel).length;
        if (name === 'mainpage') {
          const current = document.querySelector('#serverCurrentName')?.textContent?.trim() || '';
          const rows = count('#serverSelect .picklist__row');
          return { ready: rows > 0 || (Boolean(current) && current !== '—'), server_picker_rows: rows, current_server_label_present: Boolean(current) && current !== '—' };
        }
        const roles = ['lan_client','external_network_source','vless_client','docker_runtime','host_runtime','router_core'];
        const since = performance.getEntriesByType('resource').filter(e => e.startTime >= window.__uiPerfResourceStart).map(e => e.name);
        if (name === 'settings') {
          const workspaceLoaded = performance.getEntriesByType('resource').some(e => e.name.includes('/api/v2/ui/settings/workspace') && e.responseEnd > 0);
          const logsLoaded = since.some(url => /\/api\/v2\/(events\/recent|logs\/operational|logs\/technical)/.test(url));
          const eventDomReady = count('#adminEventsList > *') > 0;
          const inventoryRows = count('#settingsClientsWrap [data-settings-client-row]');
          const inventoryEmpty = count('#settingsClientsWrap .settings-events__empty') > 0;
          return { ready: workspaceLoaded && logsLoaded && eventDomReady, workspace_loaded: workspaceLoaded, event_or_log_projection_loaded: logsLoaded, event_dom_ready: eventDomReady, event_dom_rows: count('#adminEventsList > *'), settings_inventory_rows: inventoryRows, settings_inventory_empty: inventoryEmpty };
        }
        const seen = since.filter(name => name.includes('/api/v2/ui/settings/inventory?')).map(e => new URL(e).searchParams.get('role'));
        const roles_seen = [...new Set(seen.filter(x => roles.includes(x)))];
        const server_rows = count('#autoServerTable [data-auto-server-row]');
        const inventory_rows = count('#adminDevicesWrap [data-admin-device-row], #adminDevicesWrap [data-vless-client]');
        const devices_rendered = inventory_rows > 0 || count('#adminDevicesWrap .empty, #adminDevicesWrap .admin-server-members__empty') > 0;
        const role_counts = Object.fromEntries([
          ['lan_client','#adminDevicesCountLan'],['external_network_source','#adminDevicesCountExternalNetwork'],['vless_client','#adminDevicesCountVless'],['docker_runtime','#adminDevicesCountDocker'],['host_runtime','#adminDevicesCountHost']
        ].map(([role, selector]) => [role, Number(document.querySelector(selector)?.textContent || 0)]));
        return { ready: roles_seen.length === 6 && server_rows > 0 && devices_rendered, inventory_roles_seen: roles_seen.length, server_rows, inventory_rows, devices_rendered, role_counts };
      }, name);
      if (usableSignal.ready) { ready = true; break; }
      await page.waitForTimeout(100);
    }
    await page.waitForTimeout(250);
    const after = await metrics(cdp);
    const browserData = await page.evaluate(() => {
      const perf = performance.getEntriesByType('resource');
      const api = [];
      for (const e of perf) {
        if (!e.name.includes('/api/v2/')) continue;
        const u = new URL(e.name);
        if (u.pathname === '/api/v2/ui/external-ip') continue;
        const allowed = ['role','limit','inventory_state','global_list','vpn_auto','include_inactive','include_provider_legacy','live_observations'];
        const q = new URLSearchParams();
        for (const key of allowed) { const val=u.searchParams.get(key); if(val!==null && /^[A-Za-z0-9_-]{1,40}$/.test(val)) q.set(key,val); }
        const path = u.pathname.replace(/^\/api\/v2/, '').replace(/\/(?:[0-9a-f]{8}-[0-9a-f-]{27,}|[A-Za-z0-9_-]{24,})(?=\/|$)/g, '/{id}').replace(/(\/(?:servers|subjects|system-subjects|xray\/clients|xray\/subscription-profiles|subscription\/sources|ui\/external-connections)\/)[^/]+/g, '$1{id}');
        api.push({ route: `${path}${q.size ? `?${q}` : ''}`, start_ms: e.startTime, duration_ms: e.duration, request_start_ms: e.requestStart, response_start_ms: e.responseStart, response_end_ms: e.responseEnd, transfer_bytes: e.transferSize, encoded_body_bytes: e.encodedBodySize, decoded_body_bytes: e.decodedBodySize, dns_ms: e.domainLookupEnd > e.domainLookupStart ? e.domainLookupEnd-e.domainLookupStart : 0, connect_ms: e.connectEnd > e.connectStart ? e.connectEnd-e.connectStart : 0 });
      }
      const longtasks = window.__uiPerfLongTasks || { count: 0, total_ms: 0, max_ms: 0 };
      const cardRows = selector => [...document.querySelectorAll(selector)].length;
      return { api, longtasks: { count: longtasks.count, total_ms: Math.round(longtasks.total_ms*10)/10, max_ms: Math.round(longtasks.max_ms*10)/10 }, dom: { node_count: document.getElementsByTagName('*').length, server_row_count: cardRows('#autoServerTable [data-auto-server-row]'), inventory_dom_row_count: cardRows('#adminDevicesWrap [data-admin-device-row], #adminDevicesWrap [data-vless-client]'), visible_view: document.documentElement.dataset.view, locale: document.documentElement.dataset.locale || document.documentElement.lang } };
    });
    const relevant = browserData.api.filter(x => x.start_ms >= resourceStart);
    const phaseLongTasks = { count: Math.max(0,browserData.longtasks.count-longTaskBaseline.count), total_ms: Math.max(0,Math.round((browserData.longtasks.total_ms-longTaskBaseline.total_ms)*10)/10), max_ms: browserData.longtasks.max_ms };
    const timings = relevant.map(e => ({ route: e.route, resource_start_ms: e.start_ms, request_start_ms: e.request_start_ms, pre_request_ms: e.request_start_ms >= e.start_ms ? e.request_start_ms-e.start_ms : null, ttfb_ms: e.response_start_ms >= e.request_start_ms ? e.response_start_ms-e.request_start_ms : null, download_ms: e.response_end_ms >= e.response_start_ms ? e.response_end_ms-e.response_start_ms : null, total_ms: e.duration_ms, dns_ms: e.dns_ms, connect_ms: e.connect_ms, transfer_bytes: e.transfer_bytes, encoded_body_bytes: e.encoded_body_bytes, decoded_body_bytes: e.decoded_body_bytes }));
    return { name, ready, elapsed_ms: Date.now()-startTime, bounded_wait_ms: Math.min(MAX_PHASE_MS, Date.now()-started), usable_signal: usableSignal, api_waterfall: timings, browser_metrics_delta: Object.fromEntries(Object.keys(after).filter(k => before[k] !== undefined).map(k => [k, after[k]-before[k]])), page_metrics: after, long_tasks: phaseLongTasks, dom: browserData.dom, js_errors: [...new Set(errors)] };
  };
  try {
    const navigationStart = Date.now();
    const navigationMetrics = await metrics(cdp);
    await page.goto(`${origin}/?view=user`, { waitUntil: 'domcontentloaded', timeout: MAX_PHASE_MS });
    await page.locator('[data-view="user"]').first().click({ timeout: 3000 }).catch(() => {});
    await page.evaluate(() => { window.__uiPerfResourceStart = 0; });
    const main = await phase('mainpage', navigationStart, 0, navigationMetrics);
    const adminStart = Date.now();
    const adminLongBaseline = await page.evaluate(() => ({ ...(window.__uiPerfLongTasks || {}) }));
    const adminResourceStart = await page.evaluate(() => performance.now());
    await page.locator('[data-view="admin"]').first().click({ timeout: 3000 });
    await page.evaluate(start => { window.__uiPerfResourceStart = start; }, adminResourceStart);
    const admin = await phase('admin', adminStart, adminResourceStart, null, adminLongBaseline);
    const tabs = [];
    for (const [label, selector] of [
      ['lan_client', '#adminDevicesTabLan'],
      ['external_network_source', '#adminDevicesTabExternalNetwork'],
      ['vless_client', '#adminDevicesTabVless'],
      ['docker_runtime', '#adminDevicesTabDocker'],
      ['host_runtime', '#adminDevicesTabHost'],
    ]) {
      const started = Date.now();
      const button = page.locator(selector);
      if (await button.count() && await button.isVisible()) {
        await button.click();
        await page.waitForTimeout(100);
        const snapshot = await page.evaluate(() => ({ rows: document.querySelectorAll('#adminDevicesWrap [data-admin-device-row], #adminDevicesWrap [data-vless-client]').length, empty_state: Boolean(document.querySelector('#adminDevicesWrap .empty, #adminDevicesWrap .admin-server-members__empty')), active_view: document.documentElement.dataset.view }));
        tabs.push({ role: label, elapsed_ms: Date.now()-started, ...snapshot });
      }
    }
    const localeButton = page.locator('#localeToggle');
    let enLocale = null;
    if (await localeButton.count()) {
      await localeButton.click();
      enLocale = await page.locator('html').getAttribute('data-locale');
    }
    const settingsStart = Date.now();
    const settingsLongBaseline = await page.evaluate(() => ({ ...(window.__uiPerfLongTasks || {}) }));
    const settingsResourceStart = await page.evaluate(() => performance.now());
    await page.locator('[data-view="settings"]').first().click({ timeout: 3000 });
    await page.evaluate(start => { window.__uiPerfResourceStart = start; }, settingsResourceStart);
    const settings = await phase('settings', settingsStart, settingsResourceStart, null, settingsLongBaseline);
    const adminEn = { locale: enLocale, view: 'admin', locale_toggle_only: true };
    const resourceRows = await page.evaluate(() => performance.getEntriesByType('resource').filter(e => e.name.includes('/api/v2/') && new URL(e.name).pathname !== '/api/v2/ui/external-ip').map(e => {
      const u = new URL(e.name);
      const role = u.searchParams.get('role');
      const p = u.pathname.replace(/^\/api\/v2/, '').replace(/\/(?:[0-9a-f]{8}-[0-9a-f-]{27,}|[A-Za-z0-9_-]{24,})(?=\/|$)/g, '/{id}').replace(/(\/(?:servers|subjects|system-subjects|xray\/clients|xray\/subscription-profiles|subscription\/sources|ui\/external-connections)\/)[^/]+/g, '$1{id}');
      const q = role && ['lan_client','external_network_source','vless_client','docker_runtime','host_runtime','router_core'].includes(role) ? `?role=${role}` : '';
      return { route: `${p}${q}`, duration_ms: e.duration, request_start_ms: e.requestStart, response_start_ms: e.responseStart, response_end_ms: e.responseEnd, transfer_bytes: e.transferSize, encoded_body_bytes: e.encodedBodySize, decoded_body_bytes: e.decodedBodySize };
    }));
    const byRoute = {};
    for (const e of resourceRows) { const x=byRoute[e.route]||{count:0,timings_ms:[],transfer_bytes:[],encoded_body_bytes:[],decoded_body_bytes:[]}; x.count++; x.timings_ms.push(e.duration_ms); x.transfer_bytes.push(e.transfer_bytes); x.encoded_body_bytes.push(e.encoded_body_bytes); x.decoded_body_bytes.push(e.decoded_body_bytes); byRoute[e.route]=x; }
    runs.push({ run: runIndex+1, browser_cache_mode: cacheMode, viewport: '1440x1000', mainpage: main, admin, admin_tabs: tabs, locale_after_toggle: adminEn, settings, api_route_summary: byRoute, external_ip_request_excluded: externalIpBlocked, non_get_or_head_api_requests: mutationCount, rejected_non_read_api_requests: rejectedNonReadApiCount, response_statuses: responses.map(x => x.status) });
  } finally {
    await page.close();
  }
}

await existing.evaluate(prefs => {
  const restore = (key, value) => value === null ? localStorage.removeItem(key) : localStorage.setItem(key, value);
  restore('fwrouter:view', prefs.view);
  restore('fwrouter.locale', prefs.locale);
}, originalUiPrefs);

const output = {
  schema_version: 1,
  captured_at_local_date: new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Krasnoyarsk', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date()),
  source_baseline: '6ef940b',
  browser: { product: 'Chromium', version: '153.0.8010.12', source: 'existing CDP on the local host', installed_or_updated: false },
  page_origin: 'Loopback UI at 127.0.0.1:5500 (no remote client-network path measured)',
  method: { runs: RUNS, sequential: true, phase_timeout_ms: MAX_PHASE_MS, browser_cache: 'Run 1 disables the page cache; run 2 reuses the same context. Playwright route interception can disable HTTP caching, so warm browser-cache use is unverified. Backend caches remain untouched and age naturally.', api_timing: 'resource_start_ms to request_start_ms is a pre-request interval; requestStart to responseStart is browser-observed TTFB; responseStart to responseEnd is download time. TTFB includes network and server waiting, not uniquely backend work.', response_content: 'No raw API response bodies, identity fields, query tokens, credentials, or storage values are retained.', external_ip: 'A page route aborts /ui/external-ip before dispatch; zero endpoint requests means it was not attempted in these flows.', safety: 'Any unexpected non-GET/HEAD API request is aborted before dispatch and counted.', readiness: 'Main page: server picker rows or current-server label. Admin: six distinct role requests plus server rows selected by source-owned row markers. Settings: a workspace response seen anywhere in the page session plus event/log projection and DOM content. Each phase stops at 20 seconds.' },
  runs,
};
fs.writeFileSync(OUT, JSON.stringify(output, null, 2) + '\n', { mode: 0o600 });
console.log(JSON.stringify({ output: OUT.pathname, runs: runs.map(r => ({ run:r.run, mainpage_ms:r.mainpage.elapsed_ms, mainpage_ready:r.mainpage.ready, admin_ms:r.admin.elapsed_ms, admin_ready:r.admin.ready, settings_ms:r.settings.elapsed_ms, settings_ready:r.settings.ready, tabs:r.admin_tabs.length, route_count:Object.keys(r.api_route_summary).length, external_ip_request_excluded:r.external_ip_request_excluded, non_get_or_head_api_requests:r.non_get_or_head_api_requests, rejected_non_read_api_requests:r.rejected_non_read_api_requests })) }));
await browser.close();
