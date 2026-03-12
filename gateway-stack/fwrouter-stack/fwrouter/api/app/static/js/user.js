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

  async function loadExternalIp() {
    const controller = new AbortController();
    const t = setTimeout(() => controller.abort(), 2500);
    try {
      const r = await fetch("https://api.ipify.org?format=json", {
        cache: "no-store",
        signal: controller.signal,
      });
      const j = await r.json().catch(() => ({}));
      setText("serverCurrentIp", j.ip || "");
    } catch (_) {
      setText("serverCurrentIp", "");
    } finally {
      clearTimeout(t);
    }
  }

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  const TS_SUFFIX = window.FWROUTER_TS_SUFFIX || ".vpn.example.com";
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

  function fillServerSelect(names, autoEnabled, current, delays) {
    const select = el("serverSelect");
    if (!select) return;
    const delayMap = {};
    if (Array.isArray(delays)) {
      delays.forEach((d) => { delayMap[d.name] = d.delay; });
    }
    select.innerHTML = "";
    const autoOpt = document.createElement("option");
    autoOpt.value = "VPN-AUTO";
    autoOpt.textContent = autoEnabled ? "vpn-auto (включен)" : "vpn-auto";
    select.appendChild(autoOpt);

    names.forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      const d = delayMap[name];
      opt.textContent = d ? `${name} · ${d}` : name;
      select.appendChild(opt);
    });
    const hasPreferred = preferredServer && (preferredServer === "VPN-AUTO" || names.includes(preferredServer));
    if (hasPreferred) {
      select.value = preferredServer;
    } else if (autoEnabled) {
      select.value = "VPN-AUTO";
      preferredServer = "VPN-AUTO";
    } else if (current) {
      select.value = current;
      preferredServer = current;
    }
  }

  async function loadServersBasic() {
    try {
      const grp = await fetchJson("/api/mihomo/proxy_group?name=PROXY");
      const auto = await fetchJson("/api/autolist/status");
      const list = (grp.all || []).filter((name) => name !== "DIRECT");
      const now = grp.now || "";
      const autoEnabled = !!(auto.config && auto.config.enabled);
      autoEnabledCached = autoEnabled;

      fillServerSelect(list, autoEnabled, now, null);
      setText("serverCurrent", now || "DIRECT");
      loadExternalIp();
    } catch (e) {
      setText("serversState", "error: " + e.message);
    }
  }

  async function loadServersWithPing() {
    setText("serversState", "");
    try {
      const srv = await fetchJson("/api/mihomo/servers?group=PROXY&measure=0");
      const auto = await fetchJson("/api/autolist/status");
      const list = (srv.servers || []).map((s) => ({
        name: s.name,
        delay: (s.delay && s.delay > 0) ? `${s.delay} ms` : "timeout",
      }));
      const now = srv.now || "";
      const autoEnabled = !!(auto.config && auto.config.enabled);
      autoEnabledCached = autoEnabled;

      fillServerSelect(list.map((x) => x.name), autoEnabled, now, list);
      setText("serverCurrent", now || "DIRECT");
      loadExternalIp();
      setText("serversState", "");
    } catch (e) {
      setText("serversState", "error: " + e.message);
    }
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
    el("serversRefresh")?.addEventListener("click", loadServersWithPing);
    el("serverApply")?.addEventListener("click", () => {
      const v = el("serverSelect")?.value || "";
      if (v) selectServer(v);
    });
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
