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

  function renderCandidates(candidates) {
    const wrap = el("autoCandidateList");
    if (!wrap) return;
    if (!candidates.length) {
      wrap.innerHTML = "<span class=\"muted\">нет выбранных</span>";
      return;
    }
    wrap.innerHTML = candidates.map((name) => (
      `<span class="tag"><span class="mono">${escapeHtml(name)}</span> <button class="btn btn--danger" data-remove="${escapeHtml(name)}">×</button></span>`
    )).join(" ");
  }

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }

  let adminDevicesTab = "lan";
  let adminDevicesData = [];
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

  function isTailscaleIp(ip) {
    if (!ip) return false;
    return ip.startsWith("100.64.");
  }

  let currentCandidates = [];

  async function loadAutolist() {
    setText("autolistState", "");
    try {
      const [j, grp] = await Promise.all([
        fetchJson("/api/autolist/status"),
        fetchJson("/api/mihomo/proxy_group?name=PROXY"),
      ]);
      const cfg = j.config || {};
      if (el("autoGroup")) el("autoGroup").value = cfg.group || "PROXY";
      if (el("autoUrl")) el("autoUrl").value = cfg.url || "";
      if (el("autoTimeout")) el("autoTimeout").value = cfg.timeout_ms || 2500;
      if (el("autoCooldown")) el("autoCooldown").value = cfg.cooldown_sec || 900;
      if (el("autoInterval")) el("autoInterval").value = cfg.min_interval_sec || 300;

      const list = (grp.all || []).filter((name) => name !== "DIRECT");
      const pick = el("autoCandidatePick");
      if (pick) {
        pick.innerHTML = "";
        list.forEach((name) => {
          const opt = document.createElement("option");
          opt.value = name;
          opt.textContent = name;
          pick.appendChild(opt);
        });
      }

      currentCandidates = (cfg.candidates || []).slice();
      renderCandidates(currentCandidates);

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
          timeout_ms: Number(el("autoTimeout")?.value || 2500),
          cooldown_sec: Number(el("autoCooldown")?.value || 900),
          min_interval_sec: Number(el("autoInterval")?.value || 300),
          candidates: currentCandidates,
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
      setText("rulesState", "");
    } catch (e) {
      setText("rulesState", "error: " + e.message);
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
      setSelectValue("selectiveDefault", sel, "DIRECT");
      setText("selectiveState", "");
    } catch (e) {
      setText("selectiveState", "error: " + e.message);
    }
  }

  async function saveSelectiveDefault() {
    setText("selectiveState", "");
    try {
      const j = await fetchJson("/api/routing/status");
      const global = j.global || {};
      const mode = (global.mode || "DIRECT").toUpperCase();
      const selDef = el("selectiveDefault")?.value || "DIRECT";
      await fetchJson("/api/routing/global", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: "true", mode, selective_default: selDef }),
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
    el("rulesRefreshAll")?.addEventListener("click", () => refreshRules("all"));
    el("adminDevicesTabLan")?.addEventListener("click", () => setAdminDevicesTab("lan"));
    el("adminDevicesTabTs")?.addEventListener("click", () => setAdminDevicesTab("ts"));

    el("autoCandidateAdd")?.addEventListener("click", () => {
      const pick = el("autoCandidatePick");
      if (!pick) return;
      const name = pick.value;
      if (!name) return;
      if (!currentCandidates.includes(name)) currentCandidates.push(name);
      renderCandidates(currentCandidates);
    });

    document.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-remove]");
      if (!btn) return;
      const name = btn.dataset.remove;
      currentCandidates = currentCandidates.filter((x) => x !== name);
      renderCandidates(currentCandidates);
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
