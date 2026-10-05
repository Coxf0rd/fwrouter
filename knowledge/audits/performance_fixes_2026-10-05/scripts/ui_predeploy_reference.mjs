import { chromium } from '/root/.npm/_npx/9833c18b2d85bc59/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import path from 'node:path';

const DEFAULT_OUT = new URL('../UI_PREDEPLOY_REFERENCE.json', import.meta.url);
const OUT = process.env.FWROUTER_UI_PERF_OUT || DEFAULT_OUT;
const MAX_PHASE_MS = 20_000;
const OBSERVER_POLL_MS = 100;
const HARNESS_ID = 'fwrouter-ui-predeploy-reference-v1';
const browser = await chromium.connectOverCDP('http://127.0.0.1:9222');

function routeLabel(raw) {
  const url = new URL(raw);
  let pathname = url.pathname.replace(/^\/api\/v2/, '') || '/';
  pathname = pathname.replace(/\/(?:[0-9a-f]{8}-[0-9a-f-]{27,}|[A-Za-z0-9_-]{24,})(?=\/|$)/g, '/{id}');
  pathname = pathname.replace(/(\/(?:servers|subjects|system-subjects|xray\/clients|xray\/subscription-profiles|subscription\/sources|ui\/external-connections)\/)[^/]+/g, '$1{id}');
  const allowed = ['role', 'limit', 'inventory_state', 'global_list', 'vpn_auto', 'include_inactive', 'include_provider_legacy', 'live_observations'];
  const query = new URLSearchParams();
  for (const key of allowed) {
    const value = url.searchParams.get(key);
    if (value !== null && /^[A-Za-z0-9_-]{1,40}$/.test(value)) query.set(key, value);
  }
  const suffix = query.toString();
  return `${pathname}${suffix ? `?${suffix}` : ''}`;
}

function summarize(values) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!sorted.length) return { n: 0, min: null, p50: null, p95: null, max: null, mean: null };
  const quantile = p => sorted[Math.max(0, Math.ceil(p * sorted.length) - 1)];
  return {
    n: sorted.length,
    min: Math.round(sorted[0] * 10) / 10,
    p50: Math.round(quantile(0.5) * 10) / 10,
    p95: Math.round(quantile(0.95) * 10) / 10,
    max: Math.round(sorted.at(-1) * 10) / 10,
    mean: Math.round(sorted.reduce((sum, value) => sum + value, 0) / sorted.length * 10) / 10,
  };
}

function aggregateRequests(entries, responseStatuses = new Map()) {
  const grouped = new Map();
  for (const entry of entries) {
    const key = `${entry.method || 'GET'} ${entry.route}`;
    const group = grouped.get(key) || { count: 0, samples: [], transfer: 0, encoded: 0, decoded: 0, zero_transfer: 0 };
    group.count++;
    group.samples.push(entry);
    group.transfer += Number(entry.transfer_bytes || 0);
    group.encoded += Number(entry.encoded_body_bytes || 0);
    group.decoded += Number(entry.decoded_body_bytes || 0);
    if (entry.transfer_bytes === 0) group.zero_transfer++;
    grouped.set(key, group);
  }
  return Object.fromEntries([...grouped.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([key, group]) => {
    const statuses = responseStatuses.get(key) || {};
    return [key, {
      count: group.count,
      statuses,
      timing_ms: {
        resource_start_to_request_start: summarize(group.samples.map(x => x.pre_request_ms)),
        ttfb: summarize(group.samples.map(x => x.ttfb_ms)),
        download: summarize(group.samples.map(x => x.download_ms)),
        total: summarize(group.samples.map(x => x.total_ms)),
      },
      bytes: {
        transfer_total: group.transfer,
        encoded_body_total: group.encoded,
        decoded_body_total: group.decoded,
        zero_transfer_size_count: group.zero_transfer,
      },
    }];
  }));
}

const context = browser.contexts()[0];
if (!context) {
  await browser.close();
  throw new Error('No existing CDP context');
}
const existing = context.pages().find(page => page.url().startsWith('http://127.0.0.1:5500/'));
if (!existing) {
  await browser.close();
  throw new Error('No existing authenticated FWRouter UI page at 127.0.0.1:5500');
}
const origin = new URL(existing.url()).origin;
const originalUiPrefs = await existing.evaluate(() => ({
  view: localStorage.getItem('fwrouter:view'),
  locale: localStorage.getItem('fwrouter.locale'),
}));

try {
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  const requestStarts = new Map();
  const responses = new Map();
  const errors = [];
  let externalIpBlocked = false;
  let nonReadApiAttempts = 0;
  let rejectedNonReadApiAttempts = 0;
  try {
    await cdp.send('Network.enable');
    await cdp.send('Performance.enable');
    await cdp.send('Network.setCacheDisabled', { cacheDisabled: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.route('**/api/v2/**', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname === '/api/v2/ui/external-ip') {
        externalIpBlocked = true;
        await route.abort();
        return;
      }
      if (request.method() !== 'GET' && request.method() !== 'HEAD') {
        rejectedNonReadApiAttempts++;
        await route.abort();
        return;
      }
      await route.continue();
    });
    await page.addInitScript(() => {
      window.__uiPerfLongTasks = { count: 0, total_ms: 0, max_ms: 0 };
      window.__uiPerfPhaseLongTasks = { count: 0, total_ms: 0, max_ms: 0 };
      try {
        new PerformanceObserver(list => {
          for (const entry of list.getEntries()) {
            const totals = window.__uiPerfLongTasks;
            totals.count++;
            totals.total_ms += entry.duration;
            totals.max_ms = Math.max(totals.max_ms, entry.duration);
            const phaseTotals = window.__uiPerfPhaseLongTasks;
            phaseTotals.count++;
            phaseTotals.total_ms += entry.duration;
            phaseTotals.max_ms = Math.max(phaseTotals.max_ms, entry.duration);
          }
        }).observe({ type: 'longtask', buffered: true });
      } catch {}
    });
    page.on('request', request => {
      const url = new URL(request.url());
      if (!url.pathname.startsWith('/api/v2/')) return;
      const method = request.method();
      if (method !== 'GET' && method !== 'HEAD') nonReadApiAttempts++;
      const route = routeLabel(request.url());
      requestStarts.set(request, { method, route });
    });
    page.on('response', response => {
      const request = response.request();
      const info = requestStarts.get(request);
      if (!info) return;
      const key = `${info.method} ${info.route}`;
      const statuses = responses.get(key) || {};
      statuses[response.status()] = (statuses[response.status()] || 0) + 1;
      responses.set(key, statuses);
    });
    page.on('pageerror', error => errors.push(error.name || 'pageerror'));
    page.on('console', message => { if (message.type() === 'error') errors.push('console.error'); });

    const readMetrics = async () => {
      const { metrics = [] } = await cdp.send('Performance.getMetrics');
      return Object.fromEntries(metrics.filter(item => [
        'Timestamp', 'Nodes', 'JSHeapUsedSize', 'JSHeapTotalSize', 'TaskDuration', 'ScriptDuration', 'LayoutDuration', 'RecalcStyleDuration',
      ].includes(item.name)).map(item => [item.name, item.value]));
    };
    const phase = async (name, triggeredAt, resourceStart = 0, before = null) => {
      const waitStarted = Date.now();
      const metricsBefore = before || await readMetrics();
      let signal = null;
      let observedAt = null;
      const deadline = waitStarted + MAX_PHASE_MS;
      while (Date.now() < deadline) {
        signal = await page.evaluate(phaseName => {
          const count = selector => document.querySelectorAll(selector).length;
          if (phaseName === 'user') {
            const current = document.querySelector('#serverCurrentName')?.textContent?.trim() || '';
            const rows = count('#serverSelect .picklist__row');
            return { ready: rows > 0 || (Boolean(current) && current !== '—'), picker_rows: rows, current_label_present: Boolean(current) && current !== '—' };
          }
          const roles = ['lan_client', 'external_network_source', 'vless_client', 'docker_runtime', 'host_runtime', 'router_core'];
          const since = performance.getEntriesByType('resource').filter(entry => entry.startTime >= window.__uiPerfResourceStart);
          if (phaseName === 'settings') {
            const eventResponseLoaded = since.some(entry => entry.responseEnd > 0 && /\/api\/v2\/(events\/recent|logs\/operational|logs\/technical)/.test(entry.name));
            const inventoryResponseLoaded = since.some(entry => entry.responseEnd > 0 && entry.name.includes('/api/v2/ui/settings/inventory'));
            const eventRows = count('#adminEventsList [data-event-row]');
            const eventEmptyMarkers = count('#adminEventsList .settings-events__empty');
            const inventoryRows = count('#settingsClientsWrap [data-settings-client-row]');
            const usableEvents = eventResponseLoaded && eventRows > 0;
            const usableInventory = inventoryResponseLoaded && inventoryRows > 0;
            return {
              ready: usableEvents || usableInventory,
              event_response_loaded: eventResponseLoaded,
              event_rows: eventRows,
              event_empty_markers: eventEmptyMarkers,
              inventory_response_loaded: inventoryResponseLoaded,
              inventory_rows: inventoryRows,
              usable_event_content: usableEvents,
              usable_inventory_content: usableInventory,
            };
          }
          const roleRequests = since.filter(entry => entry.name.includes('/api/v2/ui/settings/inventory?'))
            .map(entry => new URL(entry.name).searchParams.get('role'));
          const rolesSeen = [...new Set(roleRequests.filter(role => roles.includes(role)))];
          const serverRows = count('#autoServerTable [data-auto-server-row]');
          const inventoryRows = count('#adminDevicesWrap [data-admin-device-row], #adminDevicesWrap [data-vless-client]');
          const inventoryEmpty = count('#adminDevicesWrap .empty, #adminDevicesWrap .admin-server-members__empty') > 0;
          return {
            ready: rolesSeen.length === 6 && serverRows > 0 && (inventoryRows > 0 || inventoryEmpty),
            inventory_roles_seen: rolesSeen.length,
            server_rows: serverRows,
            inventory_rows: inventoryRows,
            inventory_empty_state: inventoryEmpty,
          };
        }, name);
        if (signal.ready) { observedAt = Date.now(); break; }
        await page.waitForTimeout(OBSERVER_POLL_MS);
      }
      await page.waitForTimeout(250);
      const metricsAfter = await readMetrics();
      const browserData = await page.evaluate(() => {
        const requests = [];
        for (const entry of performance.getEntriesByType('resource')) {
          if (!entry.name.includes('/api/v2/')) continue;
          const url = new URL(entry.name);
          if (url.pathname === '/api/v2/ui/external-ip') continue;
          const allowed = ['role', 'limit', 'inventory_state', 'global_list', 'vpn_auto', 'include_inactive', 'include_provider_legacy', 'live_observations'];
          const query = new URLSearchParams();
          for (const key of allowed) {
            const value = url.searchParams.get(key);
            if (value !== null && /^[A-Za-z0-9_-]{1,40}$/.test(value)) query.set(key, value);
          }
          let pathname = url.pathname.replace(/^\/api\/v2/, '') || '/';
          pathname = pathname.replace(/\/(?:[0-9a-f]{8}-[0-9a-f-]{27,}|[A-Za-z0-9_-]{24,})(?=\/|$)/g, '/{id}');
          pathname = pathname.replace(/(\/(?:servers|subjects|system-subjects|xray\/clients|xray\/subscription-profiles|subscription\/sources|ui\/external-connections)\/)[^/]+/g, '$1{id}');
          const preRequest = entry.requestStart >= entry.startTime ? entry.requestStart - entry.startTime : null;
          const ttfb = entry.responseStart >= entry.requestStart && entry.requestStart > 0 ? entry.responseStart - entry.requestStart : null;
          const download = entry.responseEnd >= entry.responseStart && entry.responseStart > 0 ? entry.responseEnd - entry.responseStart : null;
          requests.push({
            method: 'GET',
            route: `${pathname}${query.size ? `?${query}` : ''}`,
            start_ms: entry.startTime,
            pre_request_ms: preRequest,
            ttfb_ms: ttfb,
            download_ms: download,
            total_ms: entry.duration,
            transfer_bytes: entry.transferSize,
            encoded_body_bytes: entry.encodedBodySize,
            decoded_body_bytes: entry.decodedBodySize,
          });
        }
        const visible = node => Boolean(node && node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden');
        const hasErrorText = node => visible(node) && /error|failed|ошибк|не удалось/i.test(node.textContent || '');
        const longTasks = window.__uiPerfLongTasks || { count: 0, total_ms: 0, max_ms: 0 };
        const phaseLongTasks = window.__uiPerfPhaseLongTasks || { count: 0, total_ms: 0, max_ms: 0 };
        return {
          requests,
          long_tasks: { count: longTasks.count, total_ms: Math.round(longTasks.total_ms * 10) / 10, max_ms: Math.round(longTasks.max_ms * 10) / 10 },
          phase_long_tasks: { count: phaseLongTasks.count, total_ms: Math.round(phaseLongTasks.total_ms * 10) / 10, max_ms: Math.round(phaseLongTasks.max_ms * 10) / 10 },
          dom: {
            node_count: document.getElementsByTagName('*').length,
            server_row_count: document.querySelectorAll('#autoServerTable [data-auto-server-row]').length,
            admin_inventory_row_count: document.querySelectorAll('#adminDevicesWrap [data-admin-device-row], #adminDevicesWrap [data-vless-client]').length,
            settings_event_row_count: document.querySelectorAll('#adminEventsList [data-event-row]').length,
            settings_empty_marker_count: document.querySelectorAll('#adminEventsList .settings-events__empty').length,
            visible_alert_count: [...document.querySelectorAll('[role="alert"]')].filter(visible).length,
            visible_status_error_count: ['adminLogsState', 'settingsClientsState'].filter(id => hasErrorText(document.getElementById(id))).length,
            view: document.documentElement.dataset.view || null,
          },
        };
      });
      const phaseRequests = browserData.requests.filter(entry => entry.start_ms >= resourceStart);
      const phaseLongTasks = browserData.phase_long_tasks;
      return {
        name,
        ready: Boolean(signal?.ready),
        elapsed_ms: Date.now() - triggeredAt,
        bounded_wait_ms: Math.min(MAX_PHASE_MS, Date.now() - waitStarted),
        readiness_observed_after_ms: observedAt === null ? null : observedAt - triggeredAt,
        observer_poll_interval_ms: OBSERVER_POLL_MS,
        observer_timing_uncertainty: 'nominal polling quantization is at most one interval; page scheduling stalls are unmeasured',
        usable_signal: signal,
        api_request_summary: aggregateRequests(phaseRequests),
        browser_metrics_delta: Object.fromEntries(Object.keys(metricsAfter).filter(key => metricsBefore[key] !== undefined).map(key => [key, metricsAfter[key] - metricsBefore[key]])),
        long_tasks: phaseLongTasks,
        dom: browserData.dom,
        js_error_count: errors.length,
        js_error_kinds: [...new Set(errors)],
      };
    };

    const navigationStart = Date.now();
    const navigationMetrics = await readMetrics();
    navigationMetrics.__longTasks = { count: 0, total_ms: 0 };
    await page.goto(`${origin}/?view=user`, { waitUntil: 'domcontentloaded', timeout: MAX_PHASE_MS });
    await page.locator('[data-view="user"]').first().click({ timeout: 3000 }).catch(() => {});
    await page.evaluate(() => { window.__uiPerfResourceStart = 0; });
    const user = await phase('user', navigationStart, 0, navigationMetrics);

    const adminStart = Date.now();
    await page.evaluate(() => { window.__uiPerfPhaseLongTasks = { count: 0, total_ms: 0, max_ms: 0 }; });
    const adminMetrics = await readMetrics();
    const adminLongTasks = await page.evaluate(() => ({ ...(window.__uiPerfLongTasks || {}) }));
    adminMetrics.__longTasks = { count: adminLongTasks.count || 0, total_ms: adminLongTasks.total_ms || 0 };
    const adminResourceStart = await page.evaluate(() => performance.now());
    await page.locator('[data-view="admin"]').first().click({ timeout: 3000 });
    await page.evaluate(start => { window.__uiPerfResourceStart = start; }, adminResourceStart);
    const admin = await phase('admin', adminStart, adminResourceStart, adminMetrics);

    const settingsStart = Date.now();
    await page.evaluate(() => { window.__uiPerfPhaseLongTasks = { count: 0, total_ms: 0, max_ms: 0 }; });
    const settingsMetrics = await readMetrics();
    const settingsLongTasks = await page.evaluate(() => ({ ...(window.__uiPerfLongTasks || {}) }));
    settingsMetrics.__longTasks = { count: settingsLongTasks.count || 0, total_ms: settingsLongTasks.total_ms || 0 };
    const settingsResourceStart = await page.evaluate(() => performance.now());
    await page.locator('[data-view="settings"]').first().click({ timeout: 3000 });
    await page.evaluate(start => { window.__uiPerfResourceStart = start; }, settingsResourceStart);
    const settings = await phase('settings', settingsStart, settingsResourceStart, settingsMetrics);

    const browserData = await page.evaluate(() => performance.getEntriesByType('resource').filter(entry => entry.name.includes('/api/v2/') && new URL(entry.name).pathname !== '/api/v2/ui/external-ip').map(entry => {
      const url = new URL(entry.name);
      let pathname = url.pathname.replace(/^\/api\/v2/, '') || '/';
      pathname = pathname.replace(/\/(?:[0-9a-f]{8}-[0-9a-f-]{27,}|[A-Za-z0-9_-]{24,})(?=\/|$)/g, '/{id}');
      pathname = pathname.replace(/(\/(?:servers|subjects|system-subjects|xray\/clients|xray\/subscription-profiles|subscription\/sources|ui\/external-connections)\/)[^/]+/g, '$1{id}');
      const allowed = ['role', 'limit', 'inventory_state', 'global_list', 'vpn_auto', 'include_inactive', 'include_provider_legacy', 'live_observations'];
      const query = new URLSearchParams();
      for (const key of allowed) { const value = url.searchParams.get(key); if (value !== null && /^[A-Za-z0-9_-]{1,40}$/.test(value)) query.set(key, value); }
      const preRequest = entry.requestStart >= entry.startTime ? entry.requestStart - entry.startTime : null;
      const ttfb = entry.responseStart >= entry.requestStart && entry.requestStart > 0 ? entry.responseStart - entry.requestStart : null;
      const download = entry.responseEnd >= entry.responseStart && entry.responseStart > 0 ? entry.responseEnd - entry.responseStart : null;
      return { method: 'GET', route: `${pathname}${query.size ? `?${query}` : ''}`, start_ms: entry.startTime, pre_request_ms: preRequest, ttfb_ms: ttfb, download_ms: download, total_ms: entry.duration, transfer_bytes: entry.transferSize, encoded_body_bytes: entry.encodedBodySize, decoded_body_bytes: entry.decodedBodySize };
    }));
    const output = {
      schema_version: 1,
      harness_id: HARNESS_ID,
      captured_at_local_date: new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Krasnoyarsk', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date()),
      source_baseline_reference: '6ef940b (prior UI audit source reference; live commit not independently queried)',
      browser: { product: 'Chromium', version: '153.0.8010.12', source: 'existing local CDP session', installed_or_updated: false },
      page_origin: 'Loopback UI at 127.0.0.1:5500; no remote client network path measured',
      method: {
        flow: ['User main page', 'Admin main view', 'Settings main view'],
        runs: 1,
        sequential: true,
        viewport: '1440x1000',
        phase_timeout_ms: MAX_PHASE_MS,
        browser_cache: 'Page cache disabled for this single flow. Playwright route interception can bypass normal browser caching. No warm-browser comparison is claimed; backend caches remain untouched and age naturally.',
        api_timing: 'Per-route aggregates use PerformanceResourceTiming. Pre-request is resource start to requestStart; TTFB is requestStart to responseStart; download is responseStart to responseEnd; total is duration. TTFB includes browser/network/server waiting and is not backend execution time.',
        aggregation: 'No request URLs, payloads or identities are retained. Route keys redact IDs and retain only an allowlist of bounded query dimensions. Timings report n, min, p50, p95, max and mean; bytes are summed by route.',
        readiness: 'User requires a picker row or current-server label. Admin requires six distinct inventory-role responses, server rows, and either inventory rows or a rendered empty inventory state. Settings requires actual event rows after an events/log response or actual inventory rows after an inventory response; empty/error markers alone do not pass.',
        observer: `Readiness is sampled every ${OBSERVER_POLL_MS} ms. The nominal polling quantization is at most one interval; browser scheduling stalls are unmeasured. The report records time from view action to observed usable marker.`,
        dom_errors: 'Captures counts for visible role=alert nodes, visible Settings status text classified as error, and page/console error kinds only; no message text is retained.',
        response_content: 'No raw API bodies, identity fields, query tokens, credentials or browser storage values are retained.',
        external_ip: 'The /ui/external-ip API route is aborted before dispatch if attempted.',
        safety: 'Every API method other than GET or HEAD is aborted before dispatch and counted.',
        preferences: 'Existing local view/locale values are restored in finally; no server settings are changed.',
      },
      api_requests: aggregateRequests(browserData, responses),
      api_request_count: browserData.length,
      external_ip_attempt_blocked: externalIpBlocked,
      non_read_api_attempts: nonReadApiAttempts,
      rejected_non_read_api_attempts: rejectedNonReadApiAttempts,
      response_status_counts: Object.fromEntries([...responses.entries()].sort(([a], [b]) => a.localeCompare(b))),
      phases: { user, admin, settings },
      browser_cache_zero_transfer_count: browserData.filter(entry => entry.transfer_bytes === 0).length,
      final_js_error_count: errors.length,
      final_js_error_kinds: [...new Set(errors)],
      ui_preferences_restored: true,
    };
    fs.writeFileSync(OUT, JSON.stringify(output, null, 2) + '\n', { mode: 0o600 });
    console.log(JSON.stringify({ output: typeof OUT === 'string' ? path.resolve(OUT) : OUT.pathname, phases: Object.fromEntries(Object.entries(output.phases).map(([key, value]) => [key, { ready: value.ready, elapsed_ms: value.elapsed_ms, readiness_observed_after_ms: value.readiness_observed_after_ms }])), api_request_count: output.api_request_count, external_ip_attempt_blocked: output.external_ip_attempt_blocked, non_read_api_attempts: output.non_read_api_attempts, rejected_non_read_api_attempts: output.rejected_non_read_api_attempts, dom_errors: Object.fromEntries(Object.entries(output.phases).map(([key, value]) => [key, { alerts: value.dom.visible_alert_count, status_errors: value.dom.visible_status_error_count, js: value.js_error_count }])) }));
  } finally {
    await page.close();
  }
} finally {
  await existing.evaluate(prefs => {
    const restore = (key, value) => value === null ? localStorage.removeItem(key) : localStorage.setItem(key, value);
    restore('fwrouter:view', prefs.view);
    restore('fwrouter.locale', prefs.locale);
  }, originalUiPrefs).catch(() => {});
  await browser.close();
}
