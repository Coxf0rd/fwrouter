import { chromium } from '/root/.npm/_npx/9833c18b2d85bc59/node_modules/playwright/index.mjs';
import fs from 'node:fs';

const OUT = new URL('../UI_SETTINGS_COMPLEMENT.json', import.meta.url);
const LIMIT_MS = 20_000;
const browser = await chromium.connectOverCDP('http://127.0.0.1:9222');
const context = browser.contexts()[0];
const referencePage = context?.pages().find(p => p.url().startsWith('http://127.0.0.1:5500/'));
if (!referencePage) throw new Error('No authenticated FWRouter UI page in the existing CDP context');
const origin = new URL(referencePage.url()).origin;
const originalPrefs = await referencePage.evaluate(() => ({ view: localStorage.getItem('fwrouter:view'), locale: localStorage.getItem('fwrouter.locale'), settingsTab: localStorage.getItem('fwrouter.ui.settingsTab.v1') }));
const page = await context.newPage();
const cdp = await context.newCDPSession(page);
await cdp.send('Network.enable');
await cdp.send('Performance.enable');
await cdp.send('Network.setCacheDisabled', { cacheDisabled: true });
await page.setViewportSize({ width: 1440, height: 1000 });

const dynamicState = { external_ip_blocked: 0, non_read_aborted: 0, dispatched_non_read: 0, js_errors: [] };
const cdpResponses = [];
const apiStarts = new Map();
const responseRows = [];
const requestRows = [];

function safeRoute(raw) {
  const u = new URL(raw);
  const path = u.pathname.replace(/^\/api\/v2/, '') || '/';
  const allow = ['limit','view','type','role','include_inactive','live_observations'];
  const q = new URLSearchParams();
  for (const k of allow) {
    const v = u.searchParams.get(k);
    if (v !== null && /^[A-Za-z0-9_-]{1,40}$/.test(v)) q.set(k, v);
  }
  return `${path}${q.size ? `?${q}` : ''}`;
}

const cdpMetricSnapshot = async () => {
  const { metrics = [] } = await cdp.send('Performance.getMetrics');
  const out = {};
  for (const m of metrics) if (['Timestamp','Nodes','JSHeapUsedSize','JSHeapTotalSize','TaskDuration','ScriptDuration','LayoutDuration','RecalcStyleDuration'].includes(m.name)) out[m.name] = m.value;
  return out;
};

await page.addInitScript(() => {
  window.__settingsAudit = { first_event_content_ms: null, first_inventory_content_ms: null, inventory_hydrated_ms: null, pending_seen: false, first_pending_ms: null, mutation_count: 0, long_tasks: { count: 0, total_ms: 0, max_ms: 0 } };
  const sample = () => {
    const s = window.__settingsAudit;
    if (!s || !document.documentElement) return;
    const now = performance.now();
    const eventRows = document.querySelectorAll('#settings-top [data-event-row]').length;
    const eventEmpty = Boolean(document.querySelector('#adminEventsList .settings-events__empty'));
    if (s.first_event_content_ms === null && (eventRows > 0 || eventEmpty)) {
      s.first_event_content_ms = now;
      s.first_event_kind = eventRows > 0 ? 'event_row' : 'empty_or_error_state';
      s.first_event_pending_status_key = document.getElementById('adminLogsState')?.dataset?.dynamicStatusKey || null;
    }
    const inventoryRows = [...document.querySelectorAll('#settingsClientsWrap [data-settings-client-row]')];
    const inventoryEmpty = Boolean(document.querySelector('#settingsClientsWrap .settings-events__empty'));
    if (s.first_inventory_content_ms === null && (inventoryRows.length > 0 || inventoryEmpty)) s.first_inventory_content_ms = now;
    const pendingRows = inventoryRows.filter(row => row.getAttribute('data-health-state') === 'pending').length;
    if (pendingRows > 0) {
      s.pending_seen = true;
      if (s.first_pending_ms === null) s.first_pending_ms = now;
    } else if (s.first_inventory_content_ms !== null && s.inventory_hydrated_ms === null) {
      s.inventory_hydrated_ms = now;
    }
    s.dom_snapshot = { event_rows: eventRows, event_empty_state: eventEmpty, pending_status_key: document.getElementById('adminLogsState')?.dataset?.dynamicStatusKey || null, inventory_rows: inventoryRows.length, inventory_pending_rows: pendingRows, inventory_empty_state: inventoryEmpty, event_view_visible: getComputedStyle(document.getElementById('settings-top') || document.body).display !== 'none' };
  };
  const observer = new MutationObserver(() => { window.__settingsAudit.mutation_count++; sample(); });
  const attach = () => {
    if (!document.documentElement) return;
    observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['data-health-state','data-dynamic-status-key','hidden'] });
    sample();
  };
  if (document.documentElement) attach();
  else document.addEventListener('DOMContentLoaded', attach, { once: true });
  try {
    new PerformanceObserver(list => {
      for (const e of list.getEntries()) {
        const x = window.__settingsAudit.long_tasks;
        x.count++; x.total_ms += e.duration; x.max_ms = Math.max(x.max_ms, e.duration);
      }
    }).observe({ type: 'longtask', buffered: true });
  } catch {}
});

await page.route('**/api/v2/**', async route => {
  const req = route.request();
  const u = new URL(req.url());
  if (u.pathname === '/api/v2/ui/external-ip') {
    dynamicState.external_ip_blocked++;
    await route.abort();
    return;
  }
  if (req.method() !== 'GET' && req.method() !== 'HEAD') {
    dynamicState.non_read_aborted++;
    await route.abort();
    return;
  }
  await route.continue();
});

page.on('request', req => {
  const u = new URL(req.url());
  const method = req.method();
  const route = u.pathname.startsWith('/api/v2/') ? safeRoute(req.url()) : u.pathname;
  const isApi = u.pathname.startsWith('/api/v2/');
  requestRows.push({ method, route, resource_type: req.resourceType(), is_api: isApi });
  if (method !== 'GET' && method !== 'HEAD' && isApi) dynamicState.dispatched_non_read++;
  apiStarts.set(req, { start_ms: Date.now(), method, route, is_api: isApi });
});
page.on('response', res => {
  const rec = apiStarts.get(res.request());
  if (rec?.is_api) responseRows.push({ method: rec.method, route: rec.route, status: res.status() });
});
cdp.on('Network.responseReceived', e => {
  const u = new URL(e.response.url);
  cdpResponses.push({ route: u.pathname.startsWith('/api/v2/') ? safeRoute(e.response.url) : u.pathname, fromDiskCache: Boolean(e.response.fromDiskCache), fromServiceWorker: Boolean(e.response.fromServiceWorker), fromPrefetchCache: Boolean(e.response.fromPrefetchCache), encodedDataLength: e.response.encodedDataLength || 0 });
});
page.on('pageerror', e => dynamicState.js_errors.push(e.name || 'pageerror'));
page.on('console', msg => { if (msg.type() === 'error') dynamicState.js_errors.push('console.error'); });

const phaseWait = async (name, timeoutMs = LIMIT_MS) => {
  const start = Date.now();
  const beforeMetrics = await cdpMetricSnapshot();
  const ready = await page.waitForFunction(() => {
    const a = window.__settingsAudit;
    return Boolean(a && a.first_event_content_ms !== null && a.dom_snapshot?.event_view_visible);
  }, null, { timeout: timeoutMs }).then(() => true).catch(() => false);
  const afterMetrics = await cdpMetricSnapshot();
  const snap = await page.evaluate(() => {
    const perf = performance.getEntriesByType('resource');
    const safe = e => {
      const u = new URL(e.name);
      const api = u.pathname.startsWith('/api/v2/');
      if (api) return safeRouteInPage(e.name);
      return u.pathname;
    };
    function safeRouteInPage(raw) {
      const u = new URL(raw); const p = u.pathname.replace(/^\/api\/v2/, '') || '/';
      const q = new URLSearchParams();
      for (const k of ['limit','view','type','role','include_inactive','live_observations']) { const v=u.searchParams.get(k); if(v!==null && /^[A-Za-z0-9_-]{1,40}$/.test(v)) q.set(k,v); }
      return `${p}${q.size ? `?${q}` : ''}`;
    }
    const resources = perf.map(e => ({ route: safe(e), api: new URL(e.name).pathname.startsWith('/api/v2/'), start_ms: e.startTime, request_start_ms: e.requestStart, response_start_ms: e.responseStart, response_end_ms: e.responseEnd, duration_ms: e.duration, transfer_bytes: e.transferSize, encoded_body_bytes: e.encodedBodySize, decoded_body_bytes: e.decodedBodySize, cache_candidate: e.transferSize === 0 && e.decodedBodySize > 0 }));
    const a = window.__settingsAudit || {};
    const rows = [...document.querySelectorAll('#settings-top [data-event-row]')];
    const inventory = [...document.querySelectorAll('#settingsClientsWrap [data-settings-client-row]')];
    return { time_ms: performance.now(), event_first_content_ms: a.first_event_content_ms, event_first_content_kind: a.first_event_kind || null, inventory_first_content_ms: a.first_inventory_content_ms, inventory_hydrated_ms: a.inventory_hydrated_ms, pending_seen: a.pending_seen, first_pending_ms: a.first_pending_ms, mutation_count: a.mutation_count, long_tasks: a.long_tasks, dom: { node_count: document.getElementsByTagName('*').length, event_rows: rows.length, event_empty: Boolean(document.querySelector('#adminEventsList .settings-events__empty')), event_pending_status_key: document.getElementById('adminLogsState')?.dataset?.dynamicStatusKey || null, inventory_rows: inventory.length, inventory_pending_rows: inventory.filter(x => x.dataset.healthState === 'pending').length, inventory_empty: Boolean(document.querySelector('#settingsClientsWrap .settings-events__empty')), settings_view_visible: getComputedStyle(document.getElementById('settings-top')).display !== 'none', locale: document.documentElement.dataset.locale || document.documentElement.lang }, resources };
  });
  const resources = snap.resources.filter(e => e.route !== '/ui/external-ip').map(e => ({ ...e, pre_request_ms: e.request_start_ms > 0 ? Math.max(0,e.request_start_ms-e.start_ms) : null, ttfb_ms: e.response_start_ms > 0 && e.request_start_ms > 0 ? Math.max(0,e.response_start_ms-e.request_start_ms) : null, download_ms: e.response_end_ms > 0 && e.response_start_ms > 0 ? Math.max(0,e.response_end_ms-e.response_start_ms) : null }));
  delete snap.resources;
  return { name, ready, elapsed_wall_ms: Date.now()-start, metrics_delta: Object.fromEntries(Object.keys(afterMetrics).filter(k => beforeMetrics[k] !== undefined).map(k => [k,afterMetrics[k]-beforeMetrics[k]])), cdp_metrics: afterMetrics, ...snap, resources };
};

let output;
try {
  const beforeNavigationMetrics = await cdpMetricSnapshot();
  const navigationWallStart = Date.now();
  const navigation = await page.goto(`${origin}/?view=settings`, { waitUntil: 'domcontentloaded', timeout: LIMIT_MS }).then(() => ({ domcontentloaded: true })).catch(e => ({ domcontentloaded: false, error_name: e.name }));
  const initial = await phaseWait('initial_settings', LIMIT_MS);
  const initialMetricsDelta = Object.fromEntries(Object.keys(initial.cdp_metrics).filter(k => beforeNavigationMetrics[k] !== undefined).map(k => [k,initial.cdp_metrics[k]-beforeNavigationMetrics[k]]));
  const preToggleLocale = await page.locator('html').getAttribute('data-locale');
  const localeStart = Date.now();
  const localeResourceCountBefore = await page.evaluate(() => performance.getEntriesByType('resource').length);
  await page.locator('#localeToggle').click({ timeout: 3000 });
  const localeChangedTo = await page.locator('html').getAttribute('data-locale');
  await page.waitForFunction(v => document.documentElement.dataset.locale === v && document.querySelectorAll('#settings-top [data-event-row]').length + (document.querySelector('#adminEventsList .settings-events__empty') ? 1 : 0) > 0, localeChangedTo, { timeout: 5000 }).catch(() => {});
  const localeElapsed = Date.now()-localeStart;
  const localeSnap = await page.evaluate(before => { const r=performance.getEntriesByType('resource').slice(before); return { locale: document.documentElement.dataset.locale || document.documentElement.lang, event_rows: document.querySelectorAll('#settings-top [data-event-row]').length, empty_state: Boolean(document.querySelector('#adminEventsList .settings-events__empty')), event_pending_status_key: document.getElementById('adminLogsState')?.dataset?.dynamicStatusKey || null, new_api_resource_count:r.filter(e=>new URL(e.name).pathname.startsWith('/api/v2/') && new URL(e.name).pathname!=='/api/v2/ui/external-ip').length, new_resource_count:r.length }; }, localeResourceCountBefore);

  const warmTabRuns = [];
  for (const [tab, selector] of [['error','button[data-log-source="error"]'],['all','button[data-log-source="all"]']]) {
    const started = Date.now();
    const beforeResourceCount = await page.evaluate(() => performance.getEntriesByType('resource').length);
    await page.locator(selector).click({ timeout: 3000 });
    const ready = await page.waitForFunction(() => {
      const pane = document.getElementById('adminEventsList');
      return Boolean(pane && (pane.querySelector('[data-event-row]') || pane.querySelector('.settings-events__empty')));
    }, null, { timeout: 5000 }).then(() => true).catch(() => false);
    const result = await page.evaluate(before => {
      const resources = performance.getEntriesByType('resource').slice(before);
      return { locale: document.documentElement.dataset.locale || document.documentElement.lang, event_rows: document.querySelectorAll('#settings-top [data-event-row]').length, empty_state: Boolean(document.querySelector('#adminEventsList .settings-events__empty')), pending_status_key: document.getElementById('adminLogsState')?.dataset?.dynamicStatusKey || null, new_api_resource_count: resources.filter(e => new URL(e.name).pathname.startsWith('/api/v2/')).length, new_resource_count: resources.length };
    }, beforeResourceCount);
    warmTabRuns.push({ tab, ready, elapsed_wall_ms: Date.now()-started, ...result });
  }

  const final = await phaseWait('post_locale_and_warm_return', 1000);
  const resourceSummary = {};
  for (const e of final.resources) {
    const x = resourceSummary[e.route] || { count:0, transfer_bytes:0, encoded_body_bytes:0, decoded_body_bytes:0, cache_candidate_count:0, timings_ms:[] };
    x.count++; x.transfer_bytes += e.transfer_bytes; x.encoded_body_bytes += e.encoded_body_bytes; x.decoded_body_bytes += e.decoded_body_bytes; x.cache_candidate_count += Number(e.cache_candidate); x.timings_ms.push(Math.round(e.duration_ms*10)/10); resourceSummary[e.route]=x;
  }
  output = {
    schema_version: 1,
    capture_date_local: new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Krasnoyarsk',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date()),
    source_baseline: '6ef940b',
    browser: { product:'Chromium', version:browser.version(), source:'existing local CDP context', cache_disabled_for_owned_page:true },
    page_origin: 'Loopback UI at 127.0.0.1:5500; remote client network not measured',
    method: { page_cache:'Disabled for initial navigation. Warm tab return reuses the same loaded page; no browser/backend cache reset or service refresh.', time_limit_ms:LIMIT_MS, readiness:'MutationObserver records first [data-event-row] or .settings-events__empty render. Pending state is recorded from source-owned data-dynamic-status-key and inventory data-health-state=pending markers. Warm return measures error-category and all-category tabs after locale switch.', safety:'Only GET/HEAD API requests were allowed; external-ip and non-read methods were aborted before network dispatch.', payloads:'No response bodies, event labels, identities, query values beyond allowlisted filters, credentials, or storage contents are retained.', cache_evidence:'CDP fromDiskCache/fromServiceWorker/fromPrefetchCache flags are recorded per completed response. Performance transferSize=0 is reported only as a cache candidate, not proof of a warm HTTP cache.' },
    navigation: { domcontentloaded:navigation.domcontentloaded, elapsed_wall_ms:Date.now()-navigationWallStart, error_name:navigation.error_name || null, before_to_initial_cdp_metrics_delta:initialMetricsDelta },
    initial_settings: initial,
    locale_change: { from:preToggleLocale, to:localeChangedTo, elapsed_wall_ms:localeElapsed, content_after_toggle:localeSnap },
    warm_tab_return: warmTabRuns,
    final_settings_observation: final,
    resource_summary: resourceSummary,
    cdp_cache_observations: { response_count:cdpResponses.length, disk_cache_count:cdpResponses.filter(x=>x.fromDiskCache).length, service_worker_count:cdpResponses.filter(x=>x.fromServiceWorker).length, prefetch_cache_count:cdpResponses.filter(x=>x.fromPrefetchCache).length, routes:cdpResponses.map(x=>({route:x.route,fromDiskCache:x.fromDiskCache,fromServiceWorker:x.fromServiceWorker,fromPrefetchCache:x.fromPrefetchCache,encodedDataLength:x.encodedDataLength})) },
    request_counts: { api_dispatched:requestRows.filter(x=>x.is_api && x.route !== '/ui/external-ip').length, static_requests:requestRows.filter(x=>!x.is_api).length, all_requests:requestRows.length, non_read_dispatched:dynamicState.dispatched_non_read, non_read_aborted:dynamicState.non_read_aborted, external_ip_aborted:dynamicState.external_ip_blocked, api_by_route:Object.fromEntries(Object.entries(requestRows.filter(x=>x.is_api && x.route !== '/ui/external-ip').reduce((a,x)=>(a[x.route]=(a[x.route]||0)+1,a),{}))), static_by_path:Object.fromEntries(Object.entries(requestRows.filter(x=>!x.is_api).reduce((a,x)=>(a[x.route]=(a[x.route]||0)+1,a),{}))) },
    response_status_counts:responseRows.reduce((a,x)=>(a[x.status]=(a[x.status]||0)+1,a),{}),
    js_errors:[...new Set(dynamicState.js_errors)]
  };
  fs.writeFileSync(OUT, JSON.stringify(output,null,2)+'\n', { mode:0o600 });
  console.log(JSON.stringify({output:OUT.pathname,initial_ready:initial.ready,initial_first_event_ms:initial.event_first_content_ms,initial_kind:initial.event_first_content_kind,initial_elapsed_ms:initial.elapsed_wall_ms,locale_elapsed_ms:localeElapsed,warm: warmTabRuns.map(x=>({tab:x.tab,ready:x.ready,elapsed_ms:x.elapsed_wall_ms,api_requests:x.new_api_resource_count})),inventory_rows:initial.dom.inventory_rows,pending_seen:initial.pending_seen,external_ip_aborted:dynamicState.external_ip_blocked,non_read_aborted:dynamicState.non_read_aborted}));
} finally {
  await referencePage.evaluate(prefs => {
    const restore=(key,value)=>value===null?localStorage.removeItem(key):localStorage.setItem(key,value);
    restore('fwrouter:view',prefs.view); restore('fwrouter.locale',prefs.locale); restore('fwrouter.ui.settingsTab.v1',prefs.settingsTab);
  },originalPrefs).catch(()=>{});
  await page.close().catch(()=>{});
  await browser.close();
}
