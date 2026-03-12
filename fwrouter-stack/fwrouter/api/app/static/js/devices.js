/* Event-driven: page-open, manual refresh, and SSE-triggered reload. No polling timers. */
(function () {
  const el = (id) => document.getElementById(id);

  const state = {
    data: { active: [], meta: {} },
    sseTimer: null
  };

  function fmtTs(iso) {
    try { return new Date(iso).toLocaleString(); } catch (_) { return iso || ''; }
  }

  function escapeHtml(s) {
    return (s || '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  function matchFilter(obj, q) {
    if (!q) return true;
    const hay = [
      obj.mac, obj.ip, obj.hostname, obj.name,
      obj.active_ip, obj.active_hostname
    ].join(' ').toLowerCase();
    return hay.includes(q);
  }

  function renderActive(q) {
    const rows = (state.data.active || []).filter(x => matchFilter(x, q));
    if (!rows.length) {
      el('activeWrap').innerHTML = '<div class="empty">(empty)</div>';
      return;
    }

    const html = [
      '<table class="table">',
      '<thead><tr>',
      '<th>MAC</th><th>IP</th><th>Hostname</th><th>Expiry</th>',
      '</tr></thead><tbody>',
      ...rows.map(d => {
        const exp = (d.expiry_epoch === 0) ? '0 (infinite)' : String(d.expiry_epoch);
        return '<tr>' +
          `<td class="mono">${escapeHtml(d.mac || '')}</td>` +
          `<td class="mono">${escapeHtml(d.ip || '')}</td>` +
          `<td>${escapeHtml(d.hostname || '')}</td>` +
          `<td class="mono">${escapeHtml(exp)}</td>` +
        '</tr>';
      }),
      '</tbody></table>'
    ].join('');
    el('activeWrap').innerHTML = html;
  }

  function renderAll() {
    const q = (el('q').value || '').trim().toLowerCase();
    renderActive(q);

    const m = state.data.meta || {};
    const leasefile = m.leasefile ? escapeHtml(m.leasefile) : '(not found)';
    const genAt = m.generated_at ? fmtTs(m.generated_at) : '';

    el('meta').innerHTML =
      `leasefile: <span class="mono">${leasefile}</span><br/>` +
      `generated_at: <span class="mono">${escapeHtml(genAt)}</span>`;
  }

  async function loadDevices(reason) {
    el('status').textContent = reason ? `loading… (${reason})` : 'loading…';
    try {
      const r = await fetch('/api/devices', { cache: 'no-store' });
      const data = await r.json();
      state.data = data || { active: [], meta: {} };
      renderAll();
      el('status').textContent = `ok (updated ${new Date().toLocaleTimeString()})`;
    } catch (e) {
      // silent UI: no spam, show empty
      state.data = { active: [], meta: {} };
      renderAll();
      el('status').textContent = 'empty (backend unavailable)';
    }
  }

  function initSSE() {
    try {
      const es = new EventSource('/events');
      es.onmessage = function () {
        if (state.sseTimer) clearTimeout(state.sseTimer);
        state.sseTimer = setTimeout(() => loadDevices('sse'), 120);
      };
      es.onerror = function () {
        // keep quiet
      };
    } catch (_) {
      // keep quiet
    }
  }

  el('btnRefresh').addEventListener('click', () => loadDevices('manual'));
  el('q').addEventListener('input', () => renderAll());

  window.addEventListener('load', () => {
    loadDevices('page-open');
    initSSE();
  });
})();
