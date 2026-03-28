// admin.js — autolist config + unified rules editor
(function () {
  const el = (id) => document.getElementById(id);

  async function fetchJson(url, opts) {
    const r = await fetch(url, opts || {});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j.detail || j.error || (r.status + " " + r.statusText)));
    return j;
  }

  function setText(id, txt) {
    const e = el(id);
    if (!e) return;
    const value = txt || "";
    e.textContent = value;
    if (e.classList.contains("pill")) {
      e.hidden = !value;
    }
  }

  function setSelectValue(id, value, fallback) {
    const node = el(id);
    if (!node) return;
    const next = (value || fallback || "").toUpperCase();
    node.value = next || (fallback || "");
    if (node.value !== next && fallback) node.value = fallback;
    node.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  let adminDevicesTab = "lan";
  let adminDevicesData = [];
  const TS_SUFFIX = ".vpn.minisk.ru";
  function cleanHostname(name) {
    if (!name) return "";
    let n = String(name).trim().replace(/\.$/, "");
    if (n.endsWith(TS_SUFFIX)) {
      return n.slice(0, -TS_SUFFIX.length);
    }
    if (n.endsWith(TS_SUFFIX + ".")) {
      return n.slice(0, -(TS_SUFFIX.length + 1));
    }
    return n;
  }

  function isTailscaleIp(ip) {
    if (!ip) return false;
    return ip.startsWith("100.64.");
  }

  let currentCandidates = [];
  let currentHiddenUser = [];
  let autolistServers = [];
  let autolistDelays = new Map();
  let autolistSortKey = "";
  let autolistSortDir = "asc";

  function formatPing(delay) {
    if (typeof delay === "number" && delay > 0) return `${delay} ms`;
    if (delay === 0 || delay === -1) return "timeout";
    return "—";
  }

  function sortedAutolistServers() {
    const list = autolistServers.slice();
    if (!autolistSortKey) return list;
    list.sort((left, right) => {
      if (autolistSortKey === "name") {
        return autolistSortDir === "asc"
          ? left.localeCompare(right, "ru")
          : right.localeCompare(left, "ru");
      }
      if (autolistSortKey === "ping") {
        const l = autolistDelays.get(left);
        const r = autolistDelays.get(right);
        const lv = (typeof l === "number" && l > 0) ? l : 999999;
        const rv = (typeof r === "number" && r > 0) ? r : 999999;
        return autolistSortDir === "asc" ? lv - rv : rv - lv;
      }
      if (autolistSortKey === "auto") {
        const l = currentCandidates.includes(left) ? 1 : 0;
        const r = currentCandidates.includes(right) ? 1 : 0;
        if (l !== r) return autolistSortDir === "asc" ? l - r : r - l;
        return left.localeCompare(right, "ru");
      }
      if (autolistSortKey === "hidden") {
        const l = currentHiddenUser.includes(left) ? 1 : 0;
        const r = currentHiddenUser.includes(right) ? 1 : 0;
        if (l !== r) return autolistSortDir === "asc" ? l - r : r - l;
        return left.localeCompare(right, "ru");
      }
      return 0;
    });
    return list;
  }

  function sortHead(label, key) {
    const active = autolistSortKey === key;
    const arrow = active ? (autolistSortDir === "asc" ? " ↑" : " ↓") : "";
    return `<button type="button" class="picklist__sort ${active ? "is-active" : ""}" data-auto-sort="${escapeHtml(key)}">${escapeHtml(label)}${arrow}</button>`;
  }

  function renderAutolistServers() {
    const wrap = el("autoServerTable");
    if (!wrap) return;
    const rows = sortedAutolistServers().map((name) => {
      const checkedAuto = currentCandidates.includes(name) ? "checked" : "";
      const checkedHidden = currentHiddenUser.includes(name) ? "checked" : "";
      const delay = autolistDelays.has(name) ? autolistDelays.get(name) : null;
      const nameHtml = window.FwrouterPingSelect?.renderFlaggedName ? window.FwrouterPingSelect.renderFlaggedName(name) : escapeHtml(name);
      return `<div class="server-matrix__row">
        <div class="server-matrix__name" title="${escapeHtml(name)}">${nameHtml}</div>
        <div class="server-matrix__ping">${escapeHtml(formatPing(delay))}</div>
        <label class="server-matrix__check"><input type="checkbox" data-auto-candidate="${escapeHtml(name)}" ${checkedAuto} /><span>Да</span></label>
        <label class="server-matrix__check"><input type="checkbox" data-auto-hidden="${escapeHtml(name)}" ${checkedHidden} /><span>Скрыть</span></label>
      </div>`;
    }).join("");
    wrap.innerHTML = `<div class="server-matrix__head">
      <div>${sortHead("Сервер", "name")}</div>
      <div>${sortHead("Пинг", "ping")}</div>
      <div>${sortHead("Auto-list", "auto")}</div>
      <div>${sortHead("Скрыть у пользователя", "hidden")}</div>
    </div>
    <div class="server-matrix__body">${rows || '<div class="muted" style="padding:12px 0;">нет серверов</div>'}</div>`;
  }

  function getAutolistPingRequest() {
    const group = el("autoGroup")?.value || "PROXY";
    const url = el("autoUrl")?.value || "http://www.gstatic.com/generate_204";
    const timeoutMs = Number(el("autoTimeout")?.value || 2500);
    const maxTests = Math.max(1, autolistServers.length || 1);
    const budgetMs = Math.max(timeoutMs * maxTests + 3000, 5000);
    return { group, url, timeoutMs, maxTests, budgetMs };
  }

  function getAutolistPingCacheKey() {
    const req = getAutolistPingRequest();
    return ["mihomo-servers", req.group, req.url, req.timeoutMs, req.maxTests].join("|");
  }

  async function loadAutolistPickPingData() {
    const req = getAutolistPingRequest();
    try {
      setText("autolistState", "измерение…");
      const params = new URLSearchParams({
        group: req.group,
        url: req.url,
        timeout_ms: String(req.timeoutMs),
        measure: "1",
        max_tests: String(req.maxTests),
        budget_ms: String(req.budgetMs),
      });
      return await fetchJson(`/api/mihomo/servers?${params.toString()}`, { cache: "no-store" });
    } catch (e) {
      setText("autolistState", "error: " + e.message);
      throw e;
    }
  }

  function applyAutolistPickPingData(srv) {
    const list = (srv.servers || [])
      .map((item) => item && item.name)
      .filter((name) => name && name !== "DIRECT");
    autolistServers = list.slice();
    autolistDelays = new Map((srv.servers || []).map((item) => [item.name, item.delay]));
    renderAutolistServers();
    setText("autolistState", "");
  }

  async function loadAutolist() {
    setText("autolistState", "");
    try {
      const [j, grp, srv] = await Promise.all([
        fetchJson("/api/autolist/status"),
        fetchJson("/api/mihomo/proxy_group?name=PROXY"),
        loadAutolistPickPingData().catch(() => null),
      ]);
      const cfg = j.config || {};
      if (el("autoGroup")) el("autoGroup").value = cfg.group || "PROXY";
      if (el("autoUrl")) el("autoUrl").value = cfg.url || "";
      if (el("autoIpDirectUrl")) el("autoIpDirectUrl").value = cfg.ip_check_direct_url || cfg.url || "https://api.ipify.org?format=json";
      if (el("autoIpVpnUrl")) el("autoIpVpnUrl").value = cfg.ip_check_vpn_url || cfg.url || "https://api.ipify.org?format=json";
      if (el("autoTimeout")) el("autoTimeout").value = cfg.timeout_ms || 2500;
      if (el("autoCooldown")) el("autoCooldown").value = cfg.cooldown_sec || 900;
      if (el("autoInterval")) el("autoInterval").value = cfg.min_interval_sec || 300;

      const list = (grp.all || []).filter((name) => name !== "DIRECT");
      currentCandidates = (cfg.candidates || []).slice();
      currentHiddenUser = (cfg.hidden_user || []).slice();
      autolistServers = list.slice();
      autolistDelays = srv ? new Map((srv.servers || []).map((item) => [item.name, item.delay])) : new Map();
      renderAutolistServers();

      setText("autolistState", "");
    } catch (e) {
      setText("autolistState", "error: " + e.message);
    }
  }

  async function saveAutolist() {
    setText("autolistState", "");
    try {
      await fetchJson("/api/autolist/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          group: el("autoGroup")?.value || "PROXY",
          url: el("autoUrl")?.value || "http://www.gstatic.com/generate_204",
          ip_check_direct_url: el("autoIpDirectUrl")?.value || "https://api.ipify.org?format=json",
          ip_check_vpn_url: el("autoIpVpnUrl")?.value || "https://api.ipify.org?format=json",
          timeout_ms: Number(el("autoTimeout")?.value || 2500),
          cooldown_sec: Number(el("autoCooldown")?.value || 900),
          min_interval_sec: Number(el("autoInterval")?.value || 300),
          candidates: currentCandidates,
          hidden_user: currentHiddenUser,
        }),
      });
      setText("autolistState", "");
    } catch (e) {
      setText("autolistState", "error: " + e.message);
    }
  }

  async function loadRules() {
    setText("rulesState", "");
    try {
      const j = await fetchJson("/api/rules");
      if (el("rulesText")) el("rulesText").value = j.content || "";
      await loadRulesUpstreamStatus();
      setText("rulesState", "");
    } catch (e) {
      setText("rulesState", "error: " + e.message);
    }
  }

  function formatTs(ts) {
    if (!ts) return "";
    try {
      return new Date(Number(ts) * 1000).toLocaleString();
    } catch (_) {
      return "";
    }
  }

  async function loadRulesUpstreamStatus() {
    try {
      const j = await fetchJson("/api/rules/upstream/status", { cache: "no-store" });
      const state = j.state || {};
      const apply = j.apply || {};
      const tag = state.tag || "не скачивалось";
      const detail = state.detail || "";
      const last = formatTs(state.last_success_at);
      const parts = [tag];
      if (detail) parts.push(detail);
      if (last) parts.push(last);
      if (apply.pending) parts.push("применяется…");
      if (apply.done && apply.done_at) parts.push(`applied ${formatTs(apply.done_at)}`);
      setText("rulesUpstreamInfo", parts.join(" · "));
      return { state, apply };
    } catch (e) {
      setText("rulesUpstreamInfo", "status error: " + e.message);
      return null;
    }
  }

  async function refreshRules(mode) {
    setText("rulesState", "");
    try {
      await fetchJson("/api/rules/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      setText("rulesState", "ok");
    } catch (e) {
      setText("rulesState", "error: " + e.message);
    }
  }

  async function updateAllRules() {
    setText("rulesState", "sync…");
    try {
      const j = await fetchJson("/api/rules/update-all", { method: "POST" });
      const state = j.state || {};
      const status = await loadRulesUpstreamStatus();
      await loadRules();
      if (status && status.apply && status.apply.pending) {
        setText("rulesState", "applying…");
        await waitForRulesApply();
      } else {
        setText("rulesState", j.changed ? `updated ${state.tag || ""}`.trim() : "already latest");
      }
    } catch (e) {
      setText("rulesState", "error: " + e.message);
    }
  }

  async function waitForRulesApply() {
    const startedAt = Date.now();
    while (Date.now() - startedAt < 180000) {
      await new Promise((resolve) => setTimeout(resolve, 3000));
      const status = await loadRulesUpstreamStatus();
      if (!status) return;
      if (status.apply && status.apply.done) {
        setText("rulesState", "applied");
        return;
      }
      setText("rulesState", "applying…");
    }
    setText("rulesState", "apply timeout");
  }

  async function saveRules() {
    setText("rulesState", "");
    try {
      await fetchJson("/api/rules", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: el("rulesText")?.value || "" }),
      });
      setText("rulesState", "");
    } catch (e) {
      setText("rulesState", "error: " + e.message);
    }
  }

  async function loadSelectiveDefault() {
    setText("selectiveState", "");
    try {
      const j = await fetchJson("/api/routing/status");
      const global = j.global || {};
      const sel = (global.selective_default || "DIRECT").toUpperCase();
      const selfMode = (global.self_mode || "GLOBAL").toUpperCase();
      setSelectValue("selectiveDefault", sel, "DIRECT");
      setSelectValue("selfMode", selfMode, "GLOBAL");
      setText("selectiveState", "");
    } catch (e) {
      setText("selectiveState", "error: " + e.message);
    }
  }

  async function saveSelectiveDefault() {
    setText("selectiveState", "");
    try {
      const selDef = el("selectiveDefault")?.value || "DIRECT";
      const selfMode = el("selfMode")?.value || "GLOBAL";
      await fetchJson("/api/routing/global", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selective_default: selDef, self_mode: selfMode }),
      });
      setText("selectiveState", "");
    } catch (e) {
      setText("selectiveState", "error: " + e.message);
    }
  }

  function renderAdminDevices() {
    const wrap = el("adminDevicesWrap");
    if (!wrap) return;
    const list = adminDevicesData || [];
    const lan = list.filter((d) => !isTailscaleIp(d.ip));
    const ts = list.filter((d) => isTailscaleIp(d.ip));
    const lanCount = el("adminDevicesCountLan");
    const tsCount = el("adminDevicesCountTs");
    if (lanCount) lanCount.textContent = String(lan.length);
    if (tsCount) tsCount.textContent = String(ts.length);
    const items = adminDevicesTab === "ts" ? ts : lan;

    if (!items.length) {
      wrap.innerHTML = '<div class="empty">нет активных устройств</div>';
      return;
    }
    wrap.innerHTML = items.map((d) => {
      const mode = d.override ? d.override : "GLOBAL";
      const label = d.name || cleanHostname(d.hostname) || d.ip || "";
      const hasMac = !!(d.mac && d.mac.length);
      const isTs = isTailscaleIp(d.ip);
      const noMacLabel = isTs ? "имя из Tailscale" : "ручное правило (без MAC)";
      return `<div class="device-row">
        <div>
          <div class="device-title">${escapeHtml(label)}</div>
          <div class="muted mono">${escapeHtml(d.ip || "")} · ${escapeHtml(d.mac || "")}</div>
        </div>
        <div class="device-actions">
          ${hasMac ? `<input class="input input--mono" data-admin-name-for="${escapeHtml(d.mac || "")}" value="${escapeHtml(d.name || "")}" placeholder="имя" />`
                   : (isTs ? `<div class="muted">${noMacLabel}</div>`
                           : `<input class="input input--mono" data-admin-name-ip-for="${escapeHtml(d.ip || "")}" value="${escapeHtml(d.name || "")}" placeholder="имя" />`)}
          <div class="row" style="margin-top:8px;">
            <select class="input" data-admin-device="${escapeHtml(d.ip || "")}">
              <option value="GLOBAL" ${mode === "GLOBAL" ? "selected" : ""}>ОБЩИЙ (как глобальный)</option>
              <option value="DIRECT" ${mode === "DIRECT" ? "selected" : ""}>DIRECT</option>
              <option value="VPN" ${mode === "VPN" ? "selected" : ""}>VPN</option>
              <option value="SELECTIVE" ${mode === "SELECTIVE" ? "selected" : ""}>SELECTIVE</option>
            </select>
            ${hasMac ? `<button class="btn" data-admin-save-name="${escapeHtml(d.mac || "")}">Сохранить</button>`
                     : (!isTs ? `<button class="btn" data-admin-save-ip-name="${escapeHtml(d.ip || "")}">Сохранить</button>` : ``)}
          </div>
        </div>
      </div>`;
    }).join("");
  }

  async function loadAdminDevices(refresh) {
    setText("adminDevicesState", "");
    try {
      const url = refresh ? "/api/devices?refresh=1" : "/api/devices";
      const j = await fetchJson(url, { cache: "no-store" });
      adminDevicesData = j.active || [];
      setText("adminDevicesState", "");
      renderAdminDevices();
    } catch (e) {
      setText("adminDevicesState", "error: " + e.message);
    }
  }

  function setAdminDevicesTab(tab) {
    const btnLan = el("adminDevicesTabLan");
    const btnTs = el("adminDevicesTabTs");
    if (!btnLan || !btnTs) return;
    adminDevicesTab = tab === "ts" ? "ts" : "lan";
    const isLan = adminDevicesTab === "lan";
    btnLan.classList.toggle("is-active", isLan);
    btnTs.classList.toggle("is-active", !isLan);
    renderAdminDevices();
  }

  async function saveAdminDeviceName(mac, ip) {
    let input = null;
    if (mac) {
      input = document.querySelector(`input[data-admin-name-for="${CSS.escape(mac)}"]`);
    } else if (ip) {
      input = document.querySelector(`input[data-admin-name-ip-for="${CSS.escape(ip)}"]`);
    }
    const name = input ? input.value.trim() : "";
    try {
      await fetchJson("/api/device/name", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(mac ? { mac, name } : { ip, name }),
      });
      setText("adminDevicesState", "");
    } catch (e) {
      setText("adminDevicesState", "error: " + e.message);
    }
  }

  async function saveAdminDeviceMode(ip, mode) {
    if (!ip || !mode) return;
    try {
      await fetchJson("/api/routing/device", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ip, mode }),
      });
      setText("adminDevicesState", "");
    } catch (e) {
      setText("adminDevicesState", "error: " + e.message);
    }
  }

  function wire() {
    el("autolistSave")?.addEventListener("click", saveAutolist);
    el("autolistRefresh")?.addEventListener("click", loadAutolist);
    el("rulesRefresh")?.addEventListener("click", loadRules);
    el("rulesSave")?.addEventListener("click", saveRules);
    el("selectiveSave")?.addEventListener("click", saveSelectiveDefault);
    el("adminDevicesRefresh")?.addEventListener("click", () => loadAdminDevices(true));
    el("rulesRefresh")?.addEventListener("click", () => refreshRules("small"));
    el("rulesRefreshAll")?.addEventListener("click", updateAllRules);
    el("adminDevicesTabLan")?.addEventListener("click", () => setAdminDevicesTab("lan"));
    el("adminDevicesTabTs")?.addEventListener("click", () => setAdminDevicesTab("ts"));

    document.addEventListener("change", (ev) => {
      const autoBox = ev.target.closest("input[data-auto-candidate]");
      if (autoBox) {
        const name = autoBox.dataset.autoCandidate || "";
        if (!name) return;
        if (autoBox.checked) {
          if (!currentCandidates.includes(name)) currentCandidates.push(name);
        } else {
          currentCandidates = currentCandidates.filter((item) => item !== name);
        }
        return;
      }
      const hiddenBox = ev.target.closest("input[data-auto-hidden]");
      if (hiddenBox) {
        const name = hiddenBox.dataset.autoHidden || "";
        if (!name) return;
        if (hiddenBox.checked) {
          if (!currentHiddenUser.includes(name)) currentHiddenUser.push(name);
        } else {
          currentHiddenUser = currentHiddenUser.filter((item) => item !== name);
        }
      }
    });

    document.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-auto-sort]");
      if (!btn) return;
      const key = btn.dataset.autoSort || "";
      if (!key) return;
      if (autolistSortKey === key) {
        autolistSortDir = autolistSortDir === "asc" ? "desc" : "asc";
      } else {
        autolistSortKey = key;
        autolistSortDir = "asc";
      }
      renderAutolistServers();
    });

    document.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-admin-save-name]");
      if (btn) saveAdminDeviceName(btn.dataset.adminSaveName, "");
    });

    document.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-admin-save-ip-name]");
      if (btn) saveAdminDeviceName("", btn.dataset.adminSaveIpName);
    });

    document.addEventListener("change", (ev) => {
      const sel = ev.target.closest("select[data-admin-device]");
      if (!sel) return;
      const ip = sel.dataset.adminDevice;
      const mode = sel.value;
      saveAdminDeviceMode(ip, mode);
    });

    loadAutolist();
    loadRules();
    loadSelectiveDefault();
    loadAdminDevices(false);
    setAdminDevicesTab(adminDevicesTab);

    document.addEventListener("fwrouter:view", (ev) => {
      if (ev.detail && ev.detail.view === "admin") {
        loadAdminDevices(false);
        loadSelectiveDefault();
      }
    });
  }

  window.addEventListener("DOMContentLoaded", wire);
})();
