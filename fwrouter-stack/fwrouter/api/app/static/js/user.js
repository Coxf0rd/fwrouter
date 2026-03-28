// user.js — subscription, server selection, routing, stats (manual refresh)
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

  function extractIp(text) {
    const source = String(text || "");
    const ipv4 = source.match(/\b(?:\d{1,3}\.){3}\d{1,3}\b/);
    if (ipv4) return ipv4[0];
    const ipv6 = source.match(/\b(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{1,4}\b/i);
    if (ipv6) return ipv6[0];
    return "";
  }

  async function loadClientExternalIp(url, targetId) {
    const currentShown = (el(targetId)?.textContent || "").trim();
    try {
      const target = (url || "").trim() || "https://api.ipify.org?format=json";
      const response = await fetch(target, { cache: "no-store" });
      const contentType = (response.headers.get("content-type") || "").toLowerCase();
      let ip = "";
      if (contentType.includes("application/json")) {
        const json = await response.json().catch(() => ({}));
        ip = String(json.ip || json.ipString || json.query || json.origin || json.address || "").trim();
        if (!ip) ip = extractIp(JSON.stringify(json));
      } else {
        const body = await response.text().catch(() => "");
        ip = extractIp(body);
      }
      if (ip) setText(targetId, ip);
      else setText(targetId, currentShown || "");
    } catch (_) {
      setText(targetId, currentShown || "");
    }
  }

  async function loadClientExternalIpPair(cfg) {
    const conf = cfg || {};
    const directUrl = String(conf.ip_check_direct_url || conf.url || "https://api.ipify.org?format=json");
    const vpnUrl = String(conf.ip_check_vpn_url || conf.url || "https://api.ipify.org?format=json");
    await Promise.all([
      loadClientExternalIp(directUrl, "serverCurrentIpDirect"),
      loadClientExternalIp(vpnUrl, "serverCurrentIpVpn"),
    ]);
  }

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

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

  async function loadSubscription() {
    try {
      const j = await fetchJson("/api/subscription");
      if (el("subUrl")) el("subUrl").value = j.url || "";
    } catch (_) {
      // keep quiet
    }
  }

  async function saveSubscription() {
    const url = (el("subUrl")?.value || "").trim();
    try {
      await fetchJson("/api/subscription", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
    } catch (_) {
      // keep quiet
    }
  }

  let preferredServer = null;
  let autoEnabledCached = false;
  let serverPingControl = null;
  let serverPicker = null;
  let userPingConfig = { group: "PROXY", url: "http://www.gstatic.com/generate_204", timeout_ms: 2500 };

  function formatDelayLabel(name, delay) {
    if (delay && delay > 0) return `${name} · ${delay} ms`;
    if (delay === 0 || delay === -1) return `${name} · timeout`;
    return name;
  }

  function setServerCurrentLabel(name) {
    const node = el("serverCurrent");
    if (!node) return;
    const text = String(name || "DIRECT");
    if (window.FwrouterPingSelect?.renderFlaggedName && text !== "DIRECT") {
      node.innerHTML = window.FwrouterPingSelect.renderFlaggedName(text);
    } else {
      node.textContent = text;
    }
  }

  function fillServerSelect(names, autoEnabled, current, delays) {
    if (!serverPicker) return;
    window.FwrouterPingSelect?.preloadFlagsFromNames?.(names);
    const delayMap = {};
    if (Array.isArray(delays)) {
      delays.forEach((d) => { delayMap[d.name] = d.delay; });
    }
    const selected = serverPicker.getValue();
    const items = [{
      value: "VPN-AUTO",
      primary: autoEnabled ? "vpn-auto (включен)" : "vpn-auto",
      secondary: "auto",
      triggerLabel: autoEnabled ? "vpn-auto (включен)" : "vpn-auto",
      sort: { name: "vpn-auto", ping: -1 },
      cells: [
        escapeHtml(autoEnabled ? "vpn-auto (включен)" : "vpn-auto"),
        `<span class="picklist__badge">AUTO</span>`,
      ],
    }, ...names.map((name) => ({
      value: name,
      primary: name,
      secondary: formatDelayLabel(name, delayMap[name]),
      triggerLabel: name,
      sort: { name, ping: (typeof delayMap[name] === "number" && delayMap[name] > 0) ? delayMap[name] : 999999 },
      cells: [
        window.FwrouterPingSelect?.renderFlaggedName ? window.FwrouterPingSelect.renderFlaggedName(name) : escapeHtml(name),
        escapeHtml((delayMap[name] && delayMap[name] > 0) ? `${delayMap[name]} ms` : "timeout"),
      ],
    }))];
    const targetValues = items.map((item) => item.value);
    serverPicker.setItems(items);
    const hasPreferred = preferredServer && (preferredServer === "VPN-AUTO" || names.includes(preferredServer));
    if (hasPreferred) {
      serverPicker.setValue(preferredServer);
    } else if (selected && targetValues.includes(selected)) {
      serverPicker.setValue(selected);
    } else if (autoEnabled) {
      serverPicker.setValue("VPN-AUTO");
      preferredServer = "VPN-AUTO";
    } else if (current) {
      serverPicker.setValue(current);
      preferredServer = current;
    }
  }

  function getUserPingRequest(optionCount, autoCfg) {
    const cfg = (autoCfg && autoCfg.config) ? autoCfg.config : {};
    const group = cfg.group || "PROXY";
    const url = cfg.url || "http://www.gstatic.com/generate_204";
    const timeoutMs = Number(cfg.timeout_ms || 2500);
    const maxTests = Math.max(1, optionCount || 1);
    const budgetMs = Math.max(timeoutMs * maxTests + 3000, 5000);
    return { group, url, timeoutMs, maxTests, budgetMs };
  }

  function filterVisibleServers(names, hiddenUser, currentName, candidates) {
    const hidden = new Set((hiddenUser || []).map((name) => String(name)));
    const required = new Set((candidates || []).map((name) => String(name)));
    return (names || []).filter((name) => (
      !hidden.has(name)
      || required.has(name)
      || name === currentName
      || name === preferredServer
    ));
  }

  function getUserPingCacheKey() {
    const optionCount = Math.max(1, serverPicker?.getCount() ? serverPicker.getCount() - 1 : 1);
    const group = userPingConfig.group || "PROXY";
    const url = userPingConfig.url || "http://www.gstatic.com/generate_204";
    const timeoutMs = Number(userPingConfig.timeout_ms || 2500);
    return ["mihomo-servers", group, url, timeoutMs, optionCount].join("|");
  }

  async function loadServersBasic() {
    try {
      const grp = await fetchJson("/api/mihomo/proxy_group?name=PROXY");
      const auto = await fetchJson("/api/autolist/status");
      const rawList = (grp.all || []).filter((name) => name !== "DIRECT");
      const now = grp.now || "";
      const autoEnabled = !!(auto.config && auto.config.enabled);
      autoEnabledCached = autoEnabled;
      userPingConfig = auto.config || userPingConfig;
      const list = filterVisibleServers(
        rawList,
        auto.config && auto.config.hidden_user,
        now,
        auto.config && auto.config.candidates
      );

      fillServerSelect(list, autoEnabled, now, null);
      serverPingControl?.reset();
      setServerCurrentLabel(now || "DIRECT");
      loadClientExternalIpPair(userPingConfig);
    } catch (e) {
      setText("serversState", "error: " + e.message);
    }
  }

  async function loadServersWithPingData() {
    setText("serversState", "");
    try {
      setText("serversState", "измерение…");
      const optionCount = Math.max(1, serverPicker?.getCount() ? serverPicker.getCount() - 1 : 1);
      const auto = await fetchJson("/api/autolist/status");
      userPingConfig = auto.config || userPingConfig;
      const req = getUserPingRequest(optionCount, auto);
      const params = new URLSearchParams({
        group: req.group,
        url: req.url,
        timeout_ms: String(req.timeoutMs),
        measure: "1",
        max_tests: String(req.maxTests),
        budget_ms: String(req.budgetMs),
      });
      const srv = await fetchJson(`/api/mihomo/servers?${params.toString()}`, { cache: "no-store" });
      return { srv, auto };
    } catch (e) {
      setText("serversState", "error: " + e.message);
      throw e;
    }
  }

  async function loadServersWithPing() {
    const data = await loadServersWithPingData();
    applyServerPingData(data);
    return data;
  }

  function applyServerPingData(data) {
    const srv = data && data.srv ? data.srv : {};
    const auto = data && data.auto ? data.auto : {};
    const rawList = (srv.servers || []).map((s) => ({
      name: s.name,
      delay: s.delay,
    }));
    const now = srv.now || "";
    const autoEnabled = !!(auto.config && auto.config.enabled);
    autoEnabledCached = autoEnabled;
    const hiddenUser = auto.config && auto.config.hidden_user;
    const candidates = auto.config && auto.config.candidates;
    const list = filterVisibleServers(rawList.map((item) => item.name), hiddenUser, now, candidates)
      .map((name) => rawList.find((item) => item.name === name))
      .filter(Boolean);

    fillServerSelect(list.map((x) => x.name), autoEnabled, now, list);
    setServerCurrentLabel(now || "DIRECT");
    loadClientExternalIpPair(userPingConfig);
    setText("serversState", "");
  }

  async function selectServer(name) {
    preferredServer = name;
    if (name === "VPN-AUTO") {
      return setAuto(true);
    }
    setText("serversState", "");
    try {
      await fetchJson("/api/mihomo/select", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ group: "PROXY", target: name }),
      });
      await fetchJson("/api/autolist/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: false }),
      });
      autoEnabledCached = false;
      await loadServersWithPing();
    } catch (e) {
      setText("serversState", "error: " + e.message);
    }
  }

  async function setAuto(enable) {
    if (enable) preferredServer = "VPN-AUTO";
    setText("serversState", "");
    try {
      await fetchJson("/api/autolist/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !!enable }),
      });
      await loadServersBasic();
      setText("serversState", "");
    } catch (e) {
      setText("serversState", "error: " + e.message);
    }
  }

  async function loadRouting() {
    try {
      const [route, devices, who] = await Promise.all([
        fetchJson("/api/routing/status"),
        fetchJson("/api/devices", { cache: "no-store" }),
        fetchJson("/api/whoami"),
      ]);

      const global = route.global || { enabled: "false", mode: "DIRECT" };
      setSelectValue("globalMode", global.mode, "DIRECT");

      const list = devices.active || [];
      const clientIp = (who && who.ip) ? String(who.ip).trim() : "";
      const filtered = clientIp ? list.filter((d) => d.ip === clientIp) : [];
      const wrap = el("devicesWrap");
      if (wrap) {
        if (!clientIp) {
          wrap.innerHTML = '<div class="empty">не удалось определить IP устройства</div>';
        } else if (!filtered.length) {
          wrap.innerHTML = `<div class="empty">устройство ${escapeHtml(clientIp)} не найдено в активных</div>`;
        } else {
          wrap.innerHTML = filtered.map(d => {
            const mode = d.override ? d.override : "GLOBAL";
            const label = d.name || cleanHostname(d.hostname) || d.ip || "";
            return `<div class="device-row">
              <div>
                <div class="device-title">${escapeHtml(label)}</div>
                <div class="muted mono">${escapeHtml(d.ip || "")} · ${escapeHtml(d.mac || "")}</div>
              </div>
              <div class="device-actions">
                <div class="row" style="margin-top:8px;">
                  <select class="input" data-device="${escapeHtml(d.ip || "")}">
                    <option value="GLOBAL" ${mode === "GLOBAL" ? "selected" : ""}>ОБЩИЙ (как глобальный)</option>
                    <option value="DIRECT" ${mode === "DIRECT" ? "selected" : ""}>DIRECT</option>
                    <option value="VPN" ${mode === "VPN" ? "selected" : ""}>VPN</option>
                    <option value="SELECTIVE" ${mode === "SELECTIVE" ? "selected" : ""}>SELECTIVE</option>
                  </select>
                </div>
              </div>
            </div>`;
          }).join("");
        }
      }
    } catch (e) {
      setText("routingState", "error: " + e.message);
    }
  }

  async function saveGlobalMode() {
    const mode = el("globalMode")?.value || "DIRECT";
    try {
      await fetchJson("/api/routing/global", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: "true", mode }),
      });
      setText("routingState", "");
    } catch (e) {
      setText("routingState", "error: " + e.message);
    }
  }

  async function saveDeviceMode(ip, mode) {
    try {
      await fetchJson("/api/routing/device", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ip, mode }),
      });
      setText("routingState", "");
    } catch (e) {
      setText("routingState", "error: " + e.message);
    }
  }


  async function loadStats() {
    try {
      const j = await fetchJson("/api/stats");
      const series = (j.vpn && j.vpn.series) ? j.vpn.series : [];
      renderChart(series);
      const up = j.vpn ? j.vpn.up || 0 : 0;
      const down = j.vpn ? j.vpn.down || 0 : 0;
      const now = el("statsNow");
      if (now) now.textContent = `↓ ${fmtRate(down)}  ↑ ${fmtRate(up)}`;
    } catch (_) {
      // keep quiet
    }
  }

  function fmtRate(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return `${n} B/s`;
    const kb = n / 1024;
    if (kb < 1024) return `${kb.toFixed(1)} KB/s`;
    const mb = kb / 1024;
    if (mb < 1024) return `${mb.toFixed(1)} MB/s`;
    const gb = mb / 1024;
    return `${gb.toFixed(2)} GB/s`;
  }

  let statsTimer = null;
  let statsRunning = false;

  function startStatsLoop() {
    if (statsTimer) clearInterval(statsTimer);
    if (!statsRunning || document.visibilityState !== "visible") return;
    statsTimer = setInterval(loadStats, 1000);
  }

  function stopStatsLoop() {
    if (statsTimer) clearInterval(statsTimer);
    statsTimer = null;
  }

  function toggleStats() {
    statsRunning = !statsRunning;
    const btn = el("statsToggle");
    if (btn) btn.textContent = statsRunning ? "Остановить" : "Запустить";
    if (statsRunning) {
      loadStats();
      startStatsLoop();
    } else {
      stopStatsLoop();
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      startStatsLoop();
    } else {
      stopStatsLoop();
    }
  });

  function renderChart(series) {
    const canvas = el("vpnChart");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, "rgba(244, 176, 106, 0.45)");
    grad.addColorStop(1, "rgba(244, 176, 106, 0.05)");

    ctx.strokeStyle = "rgba(255,255,255,0.12)";
    for (let i = 1; i <= 4; i++) {
      const y = (h / 5) * i;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }

    if (!series.length) {
      ctx.fillStyle = "rgba(255,255,255,0.6)";
      ctx.font = "12px sans-serif";
      ctx.fillText("Нет данных", 12, h / 2);
      return;
    }

    const max = Math.max(...series.map(p => p.value || 0), 1);
    const pts = series.map((p, i) => ({
      x: (i / (series.length - 1 || 1)) * (w - 20) + 10,
      y: h - 10 - (p.value / max) * (h - 20),
    }));

    ctx.beginPath();
    pts.forEach((p, i) => {
      if (i === 0) ctx.moveTo(p.x, p.y);
      else ctx.lineTo(p.x, p.y);
    });
    ctx.strokeStyle = "#f4b06a";
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.lineTo(pts[pts.length - 1].x, h - 10);
    ctx.lineTo(pts[0].x, h - 10);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();
  }

  function wire() {
    el("subSave")?.addEventListener("click", saveSubscription);
    el("serverApply")?.addEventListener("click", () => {
      const v = serverPicker?.getValue() || "";
      if (v) selectServer(v);
    });
    const serverSelect = el("serverSelect");
    if (serverSelect && window.FwrouterPingSelect) {
      serverPicker = window.FwrouterPingSelect.createTablePicker({
        root: serverSelect,
        placeholder: "Выберите сервер",
        columns: [
          { key: "name", label: "Сервер", className: "picklist__cell--name", sortable: true },
          { key: "ping", label: "Пинг", className: "picklist__cell--ping", sortable: true },
        ],
      });
      serverPingControl = window.FwrouterPingSelect.bindLazyPingSelect({
        target: serverSelect,
        cooldownMs: 180000,
        getCacheKey: getUserPingCacheKey,
        loadData: loadServersWithPingData,
        applyData: applyServerPingData,
      });
    }
    el("routingSave")?.addEventListener("click", saveGlobalMode);
    el("statsToggle")?.addEventListener("click", toggleStats);

    document.addEventListener("change", (ev) => {
      const sel = ev.target.closest("select[data-device]");
      if (sel) {
        const ip = sel.dataset.device;
        const mode = sel.value;
        if (ip && mode) saveDeviceMode(ip, mode);
      }
    });

    loadSubscription();
    loadServersBasic();
    loadRouting();
  }

  window.addEventListener("DOMContentLoaded", wire);
})();
