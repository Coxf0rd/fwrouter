// admin.js — admin panel: autolist, devices, selective defaults
(function () {
  const el = (id) => document.getElementById(id);
  const t = (key, params) => window.FwrouterI18n?.t(key, params) || key;
  const AUTO_REFRESH_MIN_INTERVAL_MS = 2000;

  const {
    fetchJson,
    fetchApiV2,
    actionMessage,
    pollJob,
    waitForAppliedState,
    escapeHtml,
    setText,
    setPendingState,
    setPendingStateMany,
    createPendingHelpers,
  } = window.FwrouterUI;
  const {
    setPendingScope,
    flashScopeResult,
  } = createPendingHelpers([
    ".device-row",
    ".field",
    ".admin-selective-inline",
    ".admin-card",
    "[data-section]",
  ]);
  const {
    compactModeLabel: modeLabel,
    compactSourceLabel: sourceLabel,
  } = window.FwrouterLabels;
  const {
    splitDevices,
    renderAdminDeviceRows,
    renderAdminVlessClientsHtml,
  } = window.FwrouterAdminDevices;
  const {
    renderAdminServerName,
    renderAutolistTableHtml,
  } = window.FwrouterAdminAutolist;

  function setSelectValue(id, value, fallback) {
    const node = el(id);
    if (!node) return;

    const next = (value || fallback || "").toUpperCase();
    node.value = next || (fallback || "");

    if (node.value !== next && fallback) {
      node.value = fallback;
    }
  }

  function enhanceAdminSelects(root) {
    if (!window.FwrouterLiquidSelect) return;

    const scope = root || el("admin-top") || document;
    window.FwrouterLiquidSelect.refresh(scope);
  }

  function isInteractiveTarget(target) {
    return Boolean(
      target &&
      target.closest(
        [
          "button",
          "input",
          "select",
          "textarea",
          "label",
          "a",
          ".server-switch",
          ".lg-select",
          ".picklist__badge",
        ].join(", ")
      )
    );
  }

  let adminDevicesTab = "lan";
  let adminDevicesData = [];
  let adminVlessClients = [];
  let adminDevicesLoaded = false;
  let adminVlessLoaded = false;
  let adminClientDisplaySettings = {
    system_visibility: {
      lan: true,
      external_network_source: true,
      vless_client: true,
      docker: true,
      host: true,
    },
    show_inactive: false,
    show_internal_vless: false,
    subject_traffic_preferences: {},
  };
  let currentRouterSelfSubjectId = "";
  let adminBootstrapped = false;
  let adminAutoRefreshBusy = false;
  let adminAutoRefreshLastAt = 0;

  const EXTERNAL_NETWORK_HOST_SUFFIX = ".vpn.minisk.ru";
  const DEV_ADMIN_CURRENT_PROXY_KEY = "fwrouter.dev.adminCurrentProxy";
  const devVlessClientsStorageKey = "fwrouter.dev.vlessClients";
  const UI_AUTOLIST_CONFIG_KEY = "fwrouter.ui.autolistConfig.v1";

  function cleanHostname(name) {
    if (!name) return "";

    let n = String(name).trim().replace(/\.$/, "");

    if (n.endsWith(EXTERNAL_NETWORK_HOST_SUFFIX)) {
      return n.slice(0, -EXTERNAL_NETWORK_HOST_SUFFIX.length);
    }

    if (n.endsWith(EXTERNAL_NETWORK_HOST_SUFFIX + ".")) {
      return n.slice(0, -(EXTERNAL_NETWORK_HOST_SUFFIX.length + 1));
    }

    return n;
  }

  let currentCandidates = [];
  let currentHiddenUser = [];
  let currentPriorities = {};
  let autolistServers = [];
  let autolistServerMeta = new Map();
  let autolistDelays = new Map();
  let autolistSortKey = "";
  let autolistSortDir = "asc";
  let adminCurrentMode = "SELECTIVE";
  let adminCurrentProxy = "";
  let adminCurrentSource = "global";
  let autolistSaveTimer = null;

  let selectedAutolistServerKey = "";
  let activatingAutolistServerKey = "";

  function resolveMode(value) {
    const mode = String(value || "").toUpperCase();
    if (mode === "DIRECT" || mode === "SELECTIVE" || mode === "VPN") return mode;
    return "SELECTIVE";
  }

  function getDevAdminCurrentProxy() {
    try {
      return String(window.localStorage.getItem(DEV_ADMIN_CURRENT_PROXY_KEY) || "").trim();
    } catch (_) {
      return "";
    }
  }

  function setDevAdminCurrentProxy(name) {
    try {
      const value = String(name || "").trim();

      if (value) {
        window.localStorage.setItem(DEV_ADMIN_CURRENT_PROXY_KEY, value);
      } else {
        window.localStorage.removeItem(DEV_ADMIN_CURRENT_PROXY_KEY);
      }
    } catch (_) {
      // ignore localStorage errors
    }
  }

  function setDevVlessClients(items) {
    try {
      window.localStorage.setItem(devVlessClientsStorageKey, JSON.stringify(items || []));
    } catch (_) {
      // ignore localStorage errors
    }
  }

  function getUiAutolistConfig() {
    const fallback = {
      group: "PROXY",
      url: "https://cp.cloudflare.com/generate_204",
      ip_check_direct_url: "https://api64.ipify.org?format=json",
      ip_check_vpn_url: "https://api64.ipify.org?format=json",
      timeout_ms: 2500,
      cooldown_sec: 900,
      min_interval_sec: 300,
    };

    try {
      const raw = window.localStorage.getItem(UI_AUTOLIST_CONFIG_KEY);
      const parsed = raw ? JSON.parse(raw) : {};
      return {
        ...fallback,
        ...(parsed && typeof parsed === "object" ? parsed : {}),
      };
    } catch (_) {
      return fallback;
    }
  }

  function setUiAutolistConfig(next) {
    const merged = {
      ...getUiAutolistConfig(),
      ...(next && typeof next === "object" ? next : {}),
    };
    try {
      window.localStorage.setItem(UI_AUTOLIST_CONFIG_KEY, JSON.stringify(merged));
    } catch (_) {
      // ignore storage errors
    }
    return merged;
  }

  function getVlessClientId(item) {
    return String(
      item?.id ||
      item?.uuid ||
      item?.client_id ||
      item?.email ||
      item?.name ||
      ""
    ).trim();
  }

  async function resetAutolistManualServer() {
    if (activatingAutolistServerKey) return;

    const applyButton = el("autolistApplyCurrent");
    activatingAutolistServerKey = selectedAutolistServerKey || adminCurrentProxy || "";
    setAdminStatus(t("admin.status.return_auto"));
    setPendingState(applyButton, true);
    setPendingScope(applyButton, true);
    renderAutolistServers();

    try {
      await fetchApiV2("/routing/global/fixed-server?confirm_switch=true&requested_by=ui", {
        method: "DELETE",
      });

      setDevAdminCurrentProxy("");

      adminCurrentSource = "vpn-auto";
      setAdminStatus("");

      await loadAdminVpnOverview({ silent: true });
      await loadAutolist({ liveMeasure: false, skipOverview: true });
      flashScopeResult(applyButton, "success");
    } catch (e) {
      setAdminStatus("error: " + e.message);
      flashScopeResult(applyButton, "error");
    } finally {
      activatingAutolistServerKey = "";
      setPendingState(applyButton, false);
      setPendingScope(applyButton, false);
      renderAutolistServers();
    }
  }

  function ensureAdminGlobalPills() {
    let node = el("adminGlobalPills");
    if (node) return node;

    const title = el("adminServerCurrent");
    const status = el("adminGlobalStatus");
    if (!title || !status || !title.parentElement) return null;

    node = document.createElement("div");
    node.id = "adminGlobalPills";
    node.className = "admin-global-meta-pills";

    title.parentElement.insertBefore(node, status);
    return node;
  }

  function setAdminStatus(txt) {
    const value = String(txt || "").trim();
    if (value && value.startsWith("error:")) {
      setText("adminVpnState", value);
      return;
    }
    setText("adminVpnState", "");
  }

  function syncAdminModeSeg(mode) {
    const safe = resolveMode(mode || adminCurrentMode);

    el("adminModeDirectBtn")?.classList.toggle("is-active", safe === "DIRECT");
    el("adminModeSelectiveBtn")?.classList.toggle("is-active", safe === "SELECTIVE");
    el("adminModeTunnelBtn")?.classList.toggle("is-active", safe === "VPN");

    adminCurrentMode = safe;
  }

  function updateAdminCurrentView(proxyNow, mode, source) {
    const current = String(proxyNow || "DIRECT");
    const rawSource = String(source || "").trim().toLowerCase();
    const safeSource = sourceLabel(source);
    const safeMode = modeLabel(resolveMode(mode));

    adminCurrentProxy = current;
    adminCurrentSource = rawSource || "global";

    const title = el("adminServerCurrent");
    if (title) {
      title.innerHTML = renderAdminServerName(current);
    }

    const pills = ensureAdminGlobalPills();
    if (pills) {
      const modeClass = resolveMode(mode).toLowerCase();
      pills.innerHTML = `
        <span class="admin-meta-pill admin-meta-pill--success">${escapeHtml(t("admin.status.active"))}</span>
        <span class="admin-meta-pill admin-meta-pill--info">${escapeHtml(safeSource)}</span>
        <span class="admin-meta-pill admin-meta-pill--mode admin-meta-pill--mode-${escapeHtml(modeClass)}">${escapeHtml(safeMode)}</span>
      `;
    }

    setText("adminProxyNow", current);

    const status = el("adminGlobalStatus");
    if (status) {
      status.textContent = t("admin.status.global", { mode: safeMode, source: safeSource });
    }

    renderAutolistServers();
  }

  async function loadAdminVpnOverview(options) {
    const opts = options || {};
    const silent = Boolean(opts.silent);

    if (!silent) setAdminStatus(t("status.updating"));

    try {
      const summaryData = await fetchApiV2("/ui/router-summary", { cache: "no-store" });
      const router = summaryData.router || {};
      const backendProxyNow = String(
        router.current_server_name ||
        router.active_auto_server_id ||
        "DIRECT"
      );
      const devProxyNow = getDevAdminCurrentProxy();

      const proxyNow = devProxyNow || backendProxyNow;
      const mode = resolveMode(String(router.global_mode || "SELECTIVE"));
      const source = devProxyNow ? "manual" : String(router.current_server_source || "vpn-auto");

      updateAdminCurrentView(proxyNow, mode, source);
      syncAdminModeSeg(mode);
      setAdminStatus("");
    } catch (e) {
      setAdminStatus("error: " + e.message);
    }
  }

  async function saveAdminGlobalMode(mode) {
    const next = resolveMode(mode);
    const controls = [
      el("adminModeDirectBtn"),
      el("adminModeSelectiveBtn"),
      el("adminModeTunnelBtn"),
    ];
    const activeControl = controls.find((node) => String(node?.dataset?.mode || "").toUpperCase() === next) || null;
    const scopeNode = el("adminModeSeg") || activeControl;
    setAdminStatus(t("status.saving"));
    setPendingStateMany(controls, true);
    setPendingScope(scopeNode, true);
    if (activeControl) activeControl.classList.add("is-pending-target");

    try {
      const action = await fetchApiV2("/routing/global", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: next.toLowerCase(),
          requested_by: "ui",
          run_now: false,
        }),
      });
      const jobId = String(action?.job?.job_id || "").trim();
      if (jobId) {
        await pollJob(jobId, {
          onProgress(status) {
            setAdminStatus(status === "queued" ? t("status.queued") : t("status.applying"));
          },
        });
      }
      await waitForAppliedState(
        () => loadAdminVpnOverview({ silent: true }),
        () => adminCurrentMode === next
      );

      setAdminStatus("");
      flashScopeResult(scopeNode, "success");
    } catch (e) {
      setAdminStatus("error: " + actionMessage(e));
      flashScopeResult(scopeNode, "error");
    } finally {
      if (activeControl) activeControl.classList.remove("is-pending-target");
      setPendingStateMany(controls, false);
      setPendingScope(scopeNode, false);
    }
  }

  function sortedAutolistServers() {
    const list = autolistServers.slice();
    const priorityOf = (name) => {
      const text = String(name || "").trim().toLowerCase();
      if (text === "proxy6") return -10;
      if (text.startsWith("proxy ")) return -5;
      return 0;
    };

    if (!autolistSortKey) {
      return list.sort((left, right) => {
        const l = priorityOf(left);
        const r = priorityOf(right);
        return l !== r ? l - r : left.localeCompare(right, "ru");
      });
    }

    list.sort((left, right) => {
      const priorityDelta = priorityOf(left) - priorityOf(right);
      if (priorityDelta !== 0) {
        return priorityDelta;
      }

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

        if (l !== r) {
          return autolistSortDir === "asc" ? l - r : r - l;
        }

        return left.localeCompare(right, "ru");
      }

      if (autolistSortKey === "priority") {
        const l = currentCandidates.includes(left) ? Number(currentPriorities[left] ?? 0) : 9999;
        const r = currentCandidates.includes(right) ? Number(currentPriorities[right] ?? 0) : 9999;
        if (l !== r) {
          return autolistSortDir === "asc" ? l - r : r - l;
        }
        return left.localeCompare(right, "ru");
      }

      if (autolistSortKey === "visible") {
        const l = currentHiddenUser.includes(left) ? 0 : 1;
        const r = currentHiddenUser.includes(right) ? 0 : 1;

        if (l !== r) {
          return autolistSortDir === "asc" ? l - r : r - l;
        }

        return left.localeCompare(right, "ru");
      }

      return 0;
    });

    return list;
  }

  function syncAutolistApplyButton() {
    const btn = el("autolistApplyCurrent");
    if (!btn) return;

    const selected = selectedAutolistServerKey || "";
    const hasSelected = Boolean(selected && autolistServers.includes(selected));
    const meta = autolistServerMeta.get(selected) || {};
    const fixedEligible = Boolean(hasSelected && meta.kind === "vpn_server" && meta.globalList !== false);
    const isSelectedCurrent = Boolean(selected && selected === adminCurrentProxy);
    const isManualCurrent = adminCurrentSource === "manual";

    const canApply = Boolean(
      hasSelected &&
      fixedEligible &&
      !activatingAutolistServerKey &&
      (!isSelectedCurrent || isManualCurrent)
    );

    btn.disabled = !canApply;

    btn.classList.toggle("is-reset-action", Boolean(hasSelected && isSelectedCurrent && isManualCurrent));
    btn.classList.toggle("is-reset-mode", Boolean(hasSelected && isSelectedCurrent && isManualCurrent));
    btn.classList.toggle("is-vpn-auto-return", Boolean(hasSelected && isSelectedCurrent && isManualCurrent));

    if (activatingAutolistServerKey) {
      btn.title = t("admin.action.switching");
      btn.setAttribute("aria-label", t("admin.action.switching"));
      return;
    }

    if (!selected) {
      btn.title = t("admin.action.choose_server");
      btn.setAttribute("aria-label", t("admin.action.choose_server"));
      return;
    }

    if (!fixedEligible) {
      btn.title = t("admin.action.not_global_fixed");
      btn.setAttribute("aria-label", t("admin.action.not_global_fixed"));
      return;
    }

    if (isSelectedCurrent && isManualCurrent) {
      btn.title = t("admin.action.return_auto");
      btn.setAttribute("aria-label", t("admin.action.return_auto"));
      return;
    }

    if (isSelectedCurrent) {
      btn.title = t("admin.action.already_current");
      btn.setAttribute("aria-label", t("admin.action.already_current"));
      return;
    }

    btn.title = t("admin.action.make_current", { server: selected });
    btn.setAttribute("aria-label", t("admin.action.make_current", { server: selected }));
  }

  function renderAutolistServers() {
    const wrap = el("autoServerTable");
    if (!wrap) {
      syncAutolistApplyButton();
      return;
    }

    const prevBody = wrap.querySelector(".server-matrix__body");
    const prevScrollTop = prevBody ? prevBody.scrollTop : 0;

    wrap.classList.add("server-table", "server-table--full");

    wrap.innerHTML = renderAutolistTableHtml(sortedAutolistServers(), {
      currentCandidates,
      currentHiddenUser,
      currentPriorities,
      autolistDelays,
      autolistServerMeta,
      adminCurrentProxy,
      selectedAutolistServerKey,
      activatingAutolistServerKey,
      sortKey: autolistSortKey,
      sortDir: autolistSortDir,
    });

    const nextBody = wrap.querySelector(".server-matrix__body");
    if (nextBody) {
      nextBody.scrollTop = prevScrollTop;
    }

    syncAutolistApplyButton();
  }

  function getAutolistPingRequest() {
    const group = el("autoGroup")?.value || "PROXY";
    const url = el("autoUrl")?.value || "http://www.gstatic.com/generate_204";
    const timeoutMs = Number(el("autoTimeout")?.value || 2500);
    const maxTests = Math.max(1, autolistServers.length || 1);
    const budgetMs = Math.max(timeoutMs * maxTests + 3000, 5000);

    return { group, url, timeoutMs, maxTests, budgetMs };
  }

  async function activateAutolistServer(name) {
    const serverName = String(name || "").trim();
    if (!serverName || activatingAutolistServerKey) return;
    const meta = autolistServerMeta.get(serverName) || {};
    if (meta.kind !== "vpn_server" || meta.globalList === false) {
      setAdminStatus(t("status.error_prefix", { message: t("admin.action.not_global_fixed") }));
      syncAutolistApplyButton();
      return;
    }

    const group = el("autoGroup")?.value || "PROXY";

    selectedAutolistServerKey = serverName;
    activatingAutolistServerKey = serverName;
    setAdminStatus(t("admin.status.switching"));
    renderAutolistServers();

    try {
      const serversData = await fetchApiV2("/servers?inventory_state=active&limit=1000", { cache: "no-store" });
      const servers = Array.isArray(serversData.servers) ? serversData.servers : [];
      const match = servers.find((server) => {
        const nameValue = String(server.server_name || "").trim();
        const idValue = String(server.server_id || "").trim();
        return nameValue === serverName || idValue === serverName;
      });
      if (!match || !match.server_id) {
        throw new Error(t("admin.error.server_not_found"));
      }

      await fetchApiV2("/routing/global/fixed-server", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          server_id: String(match.server_id),
          requested_by: "ui",
          confirm_switch: true,
        }),
      });

      setDevAdminCurrentProxy(serverName);
      setAdminStatus("");
      await loadAdminVpnOverview({ silent: true });
      await loadAutolist({ liveMeasure: false, skipOverview: true });
    } catch (e) {
      setAdminStatus("error: " + e.message);
    } finally {
      activatingAutolistServerKey = "";
      renderAutolistServers();
    }
  }

  async function loadAutolistPickPingData() {
    const req = getAutolistPingRequest();

    try {
      setText("autolistState", t("status.measuring"));
      const [serversData, sweepData] = await Promise.all([
        fetchApiV2("/servers?inventory_state=active&limit=1000", { cache: "no-store" }),
        fetchApiV2("/server-ping/sweep", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            checked_by: "ui",
            timeout_ms: Number(req.timeoutMs || 2500),
            limit: Number.isFinite(req.maxTests) ? Math.min(Math.max(req.maxTests, 1), 20) : 10,
          }),
        }),
      ]);

      const servers = Array.isArray(serversData.servers) ? serversData.servers : [];
      const sweep = sweepData.sweep || {};
      const resultMap = new Map((Array.isArray(sweep.results) ? sweep.results : []).map((item) => [String(item.server_id || ""), item]));

      return {
        servers: servers
          .filter((server) => server && String(server.server_id || "").trim())
          .map((server) => ({
            name: String(server.server_name || server.server_id || ""),
            delay: typeof resultMap.get(String(server.server_id || ""))?.delay_ms === "number"
              ? resultMap.get(String(server.server_id || "")).delay_ms
              : (typeof server?.ping?.last_ping_ms === "number" ? server.ping.last_ping_ms : null),
          })),
      };
    } catch (e) {
      setText("autolistState", "error: " + e.message);
      throw e;
    }
  }

  async function loadAutolistHistoryPingData() {
    const serversData = await fetchApiV2("/servers?inventory_state=active&limit=1000", { cache: "no-store" });
    const servers = Array.isArray(serversData.servers) ? serversData.servers : [];
    return {
      servers: servers
        .filter((server) => server && String(server.server_id || "").trim())
        .map((server) => ({
          name: String(server.server_name || server.server_id || ""),
          delay: typeof server?.ping?.last_ping_ms === "number" ? server.ping.last_ping_ms : null,
        })),
    };
  }

  async function loadAutolist(options) {
    const opts = options || {};
    const liveMeasure = Boolean(opts.liveMeasure);
    const skipOverview = Boolean(opts.skipOverview);

    setText("autolistState", "");

    try {
      const [serversData, srv] = await Promise.all([
        fetchApiV2("/servers?inventory_state=active&limit=1000", { cache: "no-store" }),
        (liveMeasure ? loadAutolistPickPingData() : loadAutolistHistoryPingData()).catch(() => null),
      ]);

      const cfg = getUiAutolistConfig();
      const servers = Array.isArray(serversData.servers) ? serversData.servers : [];
      const visibleServers = servers
        .filter((server) => server && String(server.server_id || "").trim())
        .filter((server) => !String(server.server_id || "").startsWith("virtual:"));

      if (el("autoGroup")) el("autoGroup").value = cfg.group || "PROXY";
      if (el("autoUrl")) el("autoUrl").value = cfg.url || "";
      if (el("autoIpDirectUrl")) el("autoIpDirectUrl").value = cfg.ip_check_direct_url || cfg.url || "https://api.ipify.org?format=json";
      if (el("autoIpVpnUrl")) el("autoIpVpnUrl").value = cfg.ip_check_vpn_url || cfg.url || "https://api.ipify.org?format=json";
      if (el("autoTimeout")) el("autoTimeout").value = cfg.timeout_ms || 2500;
      if (el("autoCooldown")) el("autoCooldown").value = cfg.cooldown_sec || 900;
      if (el("autoInterval")) el("autoInterval").value = cfg.min_interval_sec || 300;

      const list = visibleServers.map((server) => String(server.server_name || server.server_id || ""));
      autolistServerMeta = new Map(visibleServers.map((server) => [
        String(server.server_name || server.server_id || ""),
        {
          id: String(server.server_id || ""),
          kind: String(server.kind || ""),
          countryCode: String(server.country_code || ""),
          globalList: Boolean(server?.preferences?.global_list) !== false,
        },
      ]));

      currentCandidates = visibleServers
        .filter((server) => Boolean(server?.preferences?.vpn_auto))
        .map((server) => String(server.server_name || server.server_id || ""));
      currentPriorities = Object.fromEntries(visibleServers.map((server) => [
        String(server.server_name || server.server_id || ""),
        Number(server?.preferences?.vpn_auto_priority ?? 0),
      ]));
      currentHiddenUser = visibleServers
        .filter((server) => Boolean(server?.preferences?.global_list) === false)
        .map((server) => String(server.server_name || server.server_id || ""));
      autolistServers = list.slice();
      autolistDelays = srv ? new Map((srv.servers || []).map((item) => [item.name, item.delay])) : new Map();

      if (selectedAutolistServerKey && !autolistServers.includes(selectedAutolistServerKey)) {
        selectedAutolistServerKey = "";
      }

      renderAutolistServers();

      setText("autolistState", "");

      if (!skipOverview) {
        await loadAdminVpnOverview({ silent: true });
      }
    } catch (e) {
      setText("autolistState", "error: " + e.message);
    } finally {
      syncAutolistApplyButton();
    }
  }

  async function saveAutolist() {
    setText("autolistState", t("status.saving"));

    try {
      const serversData = await fetchApiV2("/servers?inventory_state=active&limit=1000", { cache: "no-store" });
      const servers = (Array.isArray(serversData.servers) ? serversData.servers : [])
        .filter((server) => !String(server?.server_id || "").startsWith("virtual:"));

      for (const server of servers) {
        const serverId = String(server.server_id || "").trim();
        const name = String(server.server_name || serverId).trim();
        if (!serverId || !name) continue;

        const nextVpnAuto = currentCandidates.includes(name);
        const nextVisible = !currentHiddenUser.includes(name);
        const nextPriority = nextVpnAuto ? Number(currentPriorities[name] ?? 0) : Number(server?.preferences?.vpn_auto_priority ?? 0);

        const currentVpnAuto = Boolean(server?.preferences?.vpn_auto);
        const currentVisible = Boolean(server?.preferences?.global_list) !== false;
        const currentPriority = Number(server?.preferences?.vpn_auto_priority ?? 0);

        if (nextVpnAuto === currentVpnAuto && nextVisible === currentVisible && nextPriority === currentPriority) {
          continue;
        }

        await fetchApiV2(`/servers/${encodeURIComponent(serverId)}/preferences`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            vpn_auto: nextVpnAuto,
            vpn_auto_priority: nextPriority,
            global_list: nextVisible,
            requested_by: "ui",
            reconcile_mihomo: false,
          }),
        });
      }

      setUiAutolistConfig({
        group: el("autoGroup")?.value || "PROXY",
        url: el("autoUrl")?.value || "https://cp.cloudflare.com/generate_204",
        ip_check_direct_url: el("autoIpDirectUrl")?.value || "https://api64.ipify.org?format=json",
        ip_check_vpn_url: el("autoIpVpnUrl")?.value || "https://api64.ipify.org?format=json",
        timeout_ms: Number(el("autoTimeout")?.value || 2500),
        cooldown_sec: Number(el("autoCooldown")?.value || 900),
        min_interval_sec: Number(el("autoInterval")?.value || 300),
      });

      setText("autolistState", "");
      await loadAutolist({ liveMeasure: false, skipOverview: true });
    } catch (e) {
      setText("autolistState", "error: " + e.message);
    }
  }

  function scheduleAutolistSave() {
    clearTimeout(autolistSaveTimer);
    autolistSaveTimer = setTimeout(() => {
      saveAutolist();
    }, 450);
  }

  async function loadSelectiveDefault() {
    setText("selectiveState", "");

    try {
      const j = await fetchApiV2("/ui/router-summary", { cache: "no-store" });
      const router = j.router || {};
      const sel = String(router.selective_default || "DIRECT").toUpperCase();
      const selfMode = "DIRECT";
      currentRouterSelfSubjectId = String(router.router_self_subject_id || "");

      setSelectValue("selectiveDefault", sel, "DIRECT");
      setSelectValue("selfMode", selfMode, "DIRECT");
      if (el("selfMode")) el("selfMode").disabled = true;
      setText("selectiveState", "");

      enhanceAdminSelects(el("admin-top"));
    } catch (e) {
      setText("selectiveState", "error: " + e.message);
    }
  }

  async function saveSelectiveDefault() {
    const selectNode = el("selectiveDefault");
    setText("selectiveState", "");
    setPendingState(selectNode, true);
    setPendingScope(selectNode, true);

    try {
      const selDef = selectNode?.value || "DIRECT";
      const action = await fetchApiV2("/routing/global", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          selective_default: String(selDef).toLowerCase(),
          requested_by: "ui",
          run_now: true,
        }),
      });
      const jobId = String(action?.job?.job_id || "").trim();
      if (jobId) {
        await pollJob(jobId, {
          onProgress(status) {
            setText("selectiveState", status === "queued" ? t("status.queued") : t("status.applying"));
          },
        });
      }
      await waitForAppliedState(
        loadSelectiveDefault,
        () => String(el("selectiveDefault")?.value || "").toUpperCase() === String(selDef).toUpperCase()
      );
      await loadAdminVpnOverview({ silent: true });
      setText("selectiveState", "");

      enhanceAdminSelects(el("admin-top"));
      flashScopeResult(selectNode, "success");
    } catch (e) {
      setText("selectiveState", "error: " + actionMessage(e));
      flashScopeResult(selectNode, "error");
    } finally {
      setPendingState(selectNode, false);
      setPendingScope(selectNode, false);
    }
  }

  async function saveRouterSelfMode() {
    const selectNode = el("selfMode");
    if (selectNode) {
      setSelectValue("selfMode", "DIRECT", "DIRECT");
      selectNode.disabled = true;
    }
    return;
  }

  function renderAdminVlessClients() {
    const wrap = el("adminDevicesWrap");
    if (!wrap) return;

    const count = el("adminDevicesCountVless");
    if (count) count.textContent = String(adminVlessClients.length);

    wrap.innerHTML = renderAdminVlessClientsHtml(adminVlessClients);
  }

  function renderAdminDevices() {
    const wrap = el("adminDevicesWrap");
    if (!wrap) return;

    const { lan, externalNetwork, docker, host } = splitDevices(adminDevicesData, adminClientDisplaySettings);

    const lanCount = el("adminDevicesCountLan");
    const externalNetworkCount = el("adminDevicesCountExternalNetwork");
    const vlessCount = el("adminDevicesCountVless");
    const dockerCount = el("adminDevicesCountDocker");
    const hostCount = el("adminDevicesCountHost");

    if (lanCount) lanCount.textContent = String(lan.length);
    if (externalNetworkCount) externalNetworkCount.textContent = String(externalNetwork.length);
    if (vlessCount) vlessCount.textContent = String(adminVlessClients.length);
    if (dockerCount) dockerCount.textContent = String(docker.length);
    if (hostCount) hostCount.textContent = String(host.length);

    syncAdminDeviceTabs();

    if (adminDevicesTab === "vless") {
      renderAdminVlessClients();
      return;
    }

    const items = adminDevicesTab === "external-network"
      ? externalNetwork
      : adminDevicesTab === "docker"
        ? docker
        : adminDevicesTab === "host"
          ? host
          : lan;

    wrap.innerHTML = renderAdminDeviceRows(items, cleanHostname);

    enhanceAdminSelects(wrap);
    syncAllAdminDeviceSaveStates();
  }

  async function loadAdminDevices(refresh) {
    setText("adminDevicesState", "");

    try {
      const [displayData, lanData, externalNetworkData, vlessData, dockerData, hostData] = await Promise.all([
        fetchApiV2("/ui/settings/display", { cache: "no-store" }),
        fetchApiV2("/ui/settings/inventory?role=lan_client&limit=500", { cache: "no-store" }),
        fetchApiV2("/ui/settings/inventory?role=external_network_source&limit=500", { cache: "no-store" }),
        fetchApiV2("/ui/settings/inventory?role=vless_client&limit=500", { cache: "no-store" }),
        fetchApiV2("/ui/settings/inventory?role=docker_runtime&limit=500", { cache: "no-store" }),
        fetchApiV2("/ui/settings/inventory?role=host_runtime&limit=500", { cache: "no-store" }),
      ]);
      adminClientDisplaySettings = displayData.display_settings || adminClientDisplaySettings;
      const hiddenSubjectIds = new Set(
        Array.isArray(adminClientDisplaySettings.hidden_subject_ids)
          ? adminClientDisplaySettings.hidden_subject_ids.map((item) => String(item || "").trim()).filter(Boolean)
          : []
      );
      const clients = [
        ...(Array.isArray(lanData.items) ? lanData.items : []),
        ...(Array.isArray(externalNetworkData.items) ? externalNetworkData.items : []),
        ...(Array.isArray(vlessData.items) ? vlessData.items : []),
        ...(Array.isArray(dockerData.items) ? dockerData.items : []),
        ...(Array.isArray(hostData.items) ? hostData.items : []),
      ].filter((item) => {
        const subjectId = String(item?.subject_id || "").trim();
        if (subjectId && hiddenSubjectIds.has(subjectId)) return false;
        if (!adminClientDisplaySettings.show_inactive && !Boolean(item?.is_active)) return false;
        if (item?.inventory_role === "vless_client" && !adminClientDisplaySettings.show_internal_vless && Boolean(item?.is_internal)) return false;
        return true;
      });
      adminDevicesData = clients
        .filter((item) => {
          const role = String(item.inventory_role || "");
          return (
            role === "lan_client"
            || role === "external_network_source"
            || role === "docker_runtime"
            || role === "host_runtime"
          );
        })
        .map((item) => ({
          id: String(item.subject_id || ""),
          inventory_role: String(item.inventory_role || ""),
          implementation_kind: String(item.implementation_kind || ""),
          display_system_id: String(item.display_system_id || ""),
          ip: String(item.ip_address || ""),
          mac: String(item.mac_address || ""),
          hostname: String(item.hostname || ""),
          name: String(item.display_name || item.alias || item.ip_address || ""),
          override: String(item.committed_desired_mode || item.desired_mode || "GLOBAL"),
          effective_mode: String(item.effective_mode || item.applied_mode || item.desired_mode || "GLOBAL"),
          mode_source: String(item.mode_source || "GLOBAL"),
          desired_mode: String(item.committed_desired_mode || item.desired_mode || "GLOBAL"),
          active: Boolean(item.is_active),
          subject_type: String(item.implementation_kind || ""),
          traffic_total_bytes: Number(item.traffic_total_bytes || 0),
          traffic_month_bytes: Number(item.traffic_month_bytes || 0),
          traffic_panel_metrics: Array.isArray(item.traffic_panel_metrics) ? item.traffic_panel_metrics : [],
        }));
      adminVlessClients = clients
        .filter((item) => item.inventory_role === "vless_client")
        .map((item) => ({
          id: String(item.client_id || item.client_uuid || item.subject_id || ""),
          uuid: String(item.client_uuid || ""),
          email: String(item.email || ""),
          local_name: String(item.alias || item.display_name || ""),
          name: String(item.display_name || item.alias || item.email || ""),
          enabled: item.enabled !== false,
          last_seen: String(item.last_seen_at || ""),
          traffic_month_bytes: Number(item.traffic_month_bytes || item.traffic_total_bytes || 0),
          traffic_panel_metrics: Array.isArray(item.traffic_panel_metrics) ? item.traffic_panel_metrics : [],
          is_internal: Boolean(item.is_internal),
          is_aggregate: Boolean(item.is_aggregate),
          member_count: Number(item.member_count || 0),
        }));
      adminDevicesLoaded = true;
      adminVlessLoaded = true;
      setText("adminDevicesState", "");
      syncAdminDeviceTabs();
      renderAdminDevices();
    } catch (e) {
      setText("adminDevicesState", "error: " + e.message);
    }
  }

  async function loadAdminVlessClients(refresh) {
    await loadAdminDevices(refresh);
  }

  function adminHasPendingUi() {
    return Boolean(document.querySelector("#admin-top .is-pending-scope, #admin-top .is-pending"));
  }

  async function refreshAdminOnReturn() {
    if (document.hidden || (document.documentElement.dataset.view || "") !== "admin") return;
    if (adminAutoRefreshBusy || adminHasPendingUi()) return;
    const now = Date.now();
    if (now - adminAutoRefreshLastAt < AUTO_REFRESH_MIN_INTERVAL_MS) return;

    adminAutoRefreshBusy = true;
    adminAutoRefreshLastAt = now;
    try {
      await Promise.allSettled([
        loadAdminVpnOverview({ silent: true }),
        loadSelectiveDefault(),
        loadAdminDevices(false),
      ]);
    } finally {
      adminAutoRefreshBusy = false;
    }
  }

  function bindAdminRefreshOnReturn() {
    window.addEventListener("focus", refreshAdminOnReturn);
    window.addEventListener("pageshow", refreshAdminOnReturn);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshAdminOnReturn();
    });
  }

  function syncAdminDeviceTabs() {
    const btnLan = el("adminDevicesTabLan");
    const btnExternalNetwork = el("adminDevicesTabExternalNetwork");
    const btnVless = el("adminDevicesTabVless");
    const btnDocker = el("adminDevicesTabDocker");
    const btnHost = el("adminDevicesTabHost");
    if (!btnLan || !btnExternalNetwork || !btnVless || !btnDocker || !btnHost) return;
    const { lan, externalNetwork, docker, host } = splitDevices(adminDevicesData, adminClientDisplaySettings);

    const visibility = adminClientDisplaySettings.system_visibility || {};
    const visible = (kind) => (
      Object.prototype.hasOwnProperty.call(visibility, kind)
        ? Boolean(visibility[kind])
        : true
    );
    const optionalVisible = (kind, count) => (
      visible(kind) && Number(count || 0) > 0
    );

    btnLan.hidden = !visible("lan");
    btnExternalNetwork.hidden = !optionalVisible("external_network_source", externalNetwork.length);
    btnVless.hidden = !optionalVisible("vless_client", adminVlessClients.length);
    btnDocker.hidden = !optionalVisible("docker", docker.length);
    btnHost.hidden = !optionalVisible("host", host.length);

    const visibleTabs = [
      !btnLan.hidden ? "lan" : "",
      !btnExternalNetwork.hidden ? "external-network" : "",
      !btnVless.hidden ? "vless" : "",
      !btnDocker.hidden ? "docker" : "",
      !btnHost.hidden ? "host" : "",
    ].filter(Boolean);

    if (!visibleTabs.includes(adminDevicesTab)) {
      adminDevicesTab = visibleTabs[0] || "lan";
    }

    btnLan.classList.toggle("is-active", adminDevicesTab === "lan");
    btnExternalNetwork.classList.toggle("is-active", adminDevicesTab === "external-network");
    btnVless.classList.toggle("is-active", adminDevicesTab === "vless");
    btnDocker.classList.toggle("is-active", adminDevicesTab === "docker");
    btnHost.classList.toggle("is-active", adminDevicesTab === "host");
  }

  function setAdminDevicesTab(tab) {
    const btnLan = el("adminDevicesTabLan");
    const btnExternalNetwork = el("adminDevicesTabExternalNetwork");
    const btnVless = el("adminDevicesTabVless");
    const btnDocker = el("adminDevicesTabDocker");
    const btnHost = el("adminDevicesTabHost");

    if (!btnLan || !btnExternalNetwork) return;

    adminDevicesTab = ["external-network", "vless", "docker", "host"].includes(tab) ? tab : "lan";

    btnLan.classList.toggle("is-active", adminDevicesTab === "lan");
    btnExternalNetwork.classList.toggle("is-active", adminDevicesTab === "external-network");
    btnVless?.classList.toggle("is-active", adminDevicesTab === "vless");
    btnDocker?.classList.toggle("is-active", adminDevicesTab === "docker");
    btnHost?.classList.toggle("is-active", adminDevicesTab === "host");

    if (adminDevicesTab === "vless" && !adminVlessLoaded) {
      loadAdminVlessClients(false);
      return;
    }

    renderAdminDevices();
  }

  function getAdminDeviceRow(subjectId) {
    return document.querySelector(`[data-admin-device-row="${CSS.escape(String(subjectId || ""))}"]`);
  }

  function syncAdminDeviceSaveState(subjectId) {
    const row = getAdminDeviceRow(subjectId);
    if (!row) return;

    const aliasInput = row.querySelector(`[data-admin-alias-for="${CSS.escape(String(subjectId || ""))}"]`);
    const modeSelect = row.querySelector(`[data-admin-device="${CSS.escape(String(subjectId || ""))}"]`);
    const saveButton = row.querySelector(`[data-admin-save-device="${CSS.escape(String(subjectId || ""))}"]`);
    if (!saveButton) return;

    const aliasDirty = Boolean(aliasInput) && String(aliasInput.value || "").trim() !== String(aliasInput.dataset.initialValue || "").trim();
    const modeDirty = Boolean(modeSelect) && String(modeSelect.value || "").toUpperCase() !== String(modeSelect.dataset.initialValue || "").toUpperCase();
    const isDirty = aliasDirty || modeDirty;

    saveButton.disabled = !isDirty;
    saveButton.classList.toggle("is-dirty", isDirty);
  }

  function syncAllAdminDeviceSaveStates() {
    adminDevicesData.forEach((item) => syncAdminDeviceSaveState(item.id));
  }

  async function saveAdminDevice(subjectId) {
    const normalized = String(subjectId || "").trim();
    if (!normalized) return;
    const match = adminDevicesData.find((item) => String(item.id || "") === normalized);
    const row = getAdminDeviceRow(normalized);
    const aliasInput = row?.querySelector(`[data-admin-alias-for="${CSS.escape(normalized)}"]`) || null;
    const modeSelect = row?.querySelector(`[data-admin-device="${CSS.escape(normalized)}"]`) || null;
    const saveButton = row?.querySelector(`[data-admin-save-device="${CSS.escape(normalized)}"]`) || null;
    const alias = aliasInput ? String(aliasInput.value || "").trim() : "";
    const mode = String(modeSelect?.value || match?.override || "GLOBAL").toUpperCase();

    if (!subjectId) {
      setText("adminDevicesState", t("status.error_prefix", { message: t("admin.error.device_not_found") }));
      return;
    }

    setText("adminDevicesState", "");
    setPendingStateMany([aliasInput, modeSelect, saveButton], true);
    setPendingScope(row || saveButton || modeSelect, true);
    try {
      if (aliasInput) {
        await fetchApiV2(`/subjects/${encodeURIComponent(normalized)}/alias`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ alias: alias || null }),
        });
      }

      const action = await fetchApiV2(`/subjects/${encodeURIComponent(normalized)}/mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: mode === "GLOBAL" ? "global" : String(mode).toLowerCase(),
          actor_scope: "admin",
          requested_by: "ui",
          run_now: false,
        }),
      });
      const jobId = String(action?.job?.job_id || "").trim();
      if (jobId) {
        await pollJob(jobId, {
          onProgress() {},
        });
      }
      await loadAdminDevices(false);
      const settled = adminDevicesData.find((item) => String(item.id || "") === normalized);
      const settledAlias = String(settled?.name || "").trim();
      const settledPolicy = String(settled?.override || settled?.desired_mode || "").toUpperCase();
      const modeAppliedFast = settledPolicy === mode;
      const aliasAppliedFast = !aliasInput || settledAlias === alias;
      if (!(modeAppliedFast && aliasAppliedFast)) {
        await waitForAppliedState(
          () => loadAdminDevices(false),
          () => {
            const updated = adminDevicesData.find((item) => String(item.id || "") === normalized);
            const savedAlias = String(updated?.name || "").trim();
            const savedMode = String(updated?.override || updated?.desired_mode || updated?.effective_mode || "").toUpperCase();
            return savedMode === mode && (!aliasInput || savedAlias === alias);
          },
          { timeoutMs: 30000 }
        );
      }
      setText("adminDevicesState", "");
      const freshRow = getAdminDeviceRow(normalized);
      const freshModeSelect = freshRow?.querySelector(`[data-admin-device="${CSS.escape(normalized)}"]`) || null;
      const freshSaveButton = freshRow?.querySelector(`[data-admin-save-device="${CSS.escape(normalized)}"]`) || null;
      flashScopeResult(freshRow || freshSaveButton || freshModeSelect || row || saveButton || modeSelect, "success");
    } catch (e) {
      setText("adminDevicesState", "error: " + actionMessage(e));
      flashScopeResult(row || saveButton || modeSelect, "error");
    } finally {
      setPendingStateMany([aliasInput, modeSelect, saveButton], false);
      setPendingScope(row || saveButton || modeSelect, false);
    }
  }

  async function saveAdminVlessClientName(id) {
    const clientId = String(id || "").trim();
    if (!clientId) return;

    const input = document.querySelector(`input[data-admin-vless-name-for="${CSS.escape(clientId)}"]`);
    const name = input ? input.value.trim() : "";

    setText("adminDevicesState", t("status.saving"));

    try {
      await fetchApiV2(`/xray/clients/${encodeURIComponent(clientId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          alias: name || null,
          requested_by: "ui",
        }),
      });

      setText("adminDevicesState", "");
      await loadAdminVlessClients(false);
    } catch (e) {
      adminVlessClients = adminVlessClients.map((client) => (
        getVlessClientId(client) === clientId
          ? { ...client, local_name: name }
          : client
      ));

      setDevVlessClients(adminVlessClients);
      setText("adminDevicesState", e && e.status === 404 ? "dev" : "error: " + e.message);
      renderAdminDevices();
    }
  }

  async function deleteAdminVlessClient(id) {
    const clientId = String(id || "").trim();
    if (!clientId) return;

    const ok = window.confirm(t("admin.confirm.delete_vless"));
    if (!ok) return;

    setText("adminDevicesState", t("status.deleting"));

    try {
      await fetchApiV2(`/xray/clients/${encodeURIComponent(clientId)}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ requested_by: "ui" }),
      });

      setText("adminDevicesState", "");
      await loadAdminVlessClients(true);
    } catch (e) {
      adminVlessClients = adminVlessClients.filter((client) => getVlessClientId(client) !== clientId);
      setDevVlessClients(adminVlessClients);

      setText("adminDevicesState", e && e.status === 404 ? "dev" : "error: " + e.message);
      renderAdminDevices();
    }
  }

  function wire() {
    if (adminBootstrapped) return;
    adminBootstrapped = true;

    el("adminModeDirectBtn")?.addEventListener("click", () => saveAdminGlobalMode("DIRECT"));
    el("adminModeSelectiveBtn")?.addEventListener("click", () => saveAdminGlobalMode("SELECTIVE"));
    el("adminModeTunnelBtn")?.addEventListener("click", () => saveAdminGlobalMode("VPN"));

    el("autolistRefresh")?.addEventListener("click", () => loadAutolist({ liveMeasure: false }));
    el("autolistPing")?.addEventListener("click", () => loadAutolist({ liveMeasure: true }));

    el("autolistApplyCurrent")?.addEventListener("click", () => {
      if (!selectedAutolistServerKey) return;

      const isSelectedCurrent = selectedAutolistServerKey === adminCurrentProxy;
      const isManualCurrent = adminCurrentSource === "manual";

      if (isSelectedCurrent && isManualCurrent) {
        resetAutolistManualServer();
        return;
      }

      activateAutolistServer(selectedAutolistServerKey);
    });

    el("selectiveDefault")?.addEventListener("change", saveSelectiveDefault);
    el("selfMode")?.addEventListener("change", saveRouterSelfMode);

    el("adminDevicesRefresh")?.addEventListener("click", () => {
      if (adminDevicesTab === "vless") {
        loadAdminVlessClients(true);
      } else {
        loadAdminDevices(true);
      }
    });

    el("adminDevicesTabLan")?.addEventListener("click", () => setAdminDevicesTab("lan"));
    el("adminDevicesTabExternalNetwork")?.addEventListener("click", () => setAdminDevicesTab("external-network"));
    el("adminDevicesTabVless")?.addEventListener("click", () => setAdminDevicesTab("vless"));
    el("adminDevicesTabDocker")?.addEventListener("click", () => setAdminDevicesTab("docker"));
    el("adminDevicesTabHost")?.addEventListener("click", () => setAdminDevicesTab("host"));

    ["autoGroup", "autoUrl", "autoIpDirectUrl", "autoIpVpnUrl", "autoTimeout", "autoCooldown", "autoInterval"].forEach((id) => {
      el(id)?.addEventListener("change", scheduleAutolistSave);
      el(id)?.addEventListener("input", scheduleAutolistSave);
    });

    document.addEventListener("change", (ev) => {
      const autoBox = ev.target.closest("input[data-auto-candidate]");
      if (autoBox) {
        const name = autoBox.dataset.autoCandidate || "";
        if (!name) return;

        if (autoBox.checked) {
          if (!currentCandidates.includes(name)) currentCandidates.push(name);
          if (currentPriorities[name] == null) currentPriorities[name] = 0;
        } else {
          currentCandidates = currentCandidates.filter((item) => item !== name);
        }

        renderAutolistServers();
        scheduleAutolistSave();
        return;
      }

      const priorityInput = ev.target.closest("input[data-auto-priority]");
      if (priorityInput) {
        const name = priorityInput.dataset.autoPriority || "";
        if (!name || !currentCandidates.includes(name)) return;

        let value = Number(priorityInput.value || 0);
        if (!Number.isFinite(value)) value = 0;
        value = Math.max(-1, Math.min(5, Math.trunc(value)));
        priorityInput.value = String(value);
        currentPriorities[name] = value;
        scheduleAutolistSave();
        return;
      }

      const visibleBox = ev.target.closest("input[data-auto-visible]");
      if (visibleBox) {
        const name = visibleBox.dataset.autoVisible || "";
        if (!name) return;

        if (visibleBox.checked) {
          currentHiddenUser = currentHiddenUser.filter((item) => item !== name);
        } else if (!currentHiddenUser.includes(name)) {
          currentHiddenUser.push(name);
        }

        scheduleAutolistSave();
        return;
      }
    });

    document.addEventListener("input", (ev) => {
      const aliasInput = ev.target.closest("input[data-admin-alias-for]");
      if (!aliasInput) return;
      syncAdminDeviceSaveState(aliasInput.dataset.adminAliasFor || "");
    });

    document.addEventListener("change", (ev) => {
      const modeSelect = ev.target.closest("select[data-admin-device]");
      if (!modeSelect) return;
      syncAdminDeviceSaveState(modeSelect.dataset.adminDevice || "");
    });

    document.addEventListener("click", (ev) => {
      const row = ev.target.closest("[data-auto-server-row]");
      if (!row || isInteractiveTarget(ev.target)) return;

      const name = row.dataset.autoServerRow || "";
      if (!name) return;

      selectedAutolistServerKey = name;
      renderAutolistServers();
    });

    document.addEventListener("dblclick", (ev) => {
      const row = ev.target.closest("[data-auto-server-row]");
      if (!row || isInteractiveTarget(ev.target)) return;

      const name = row.dataset.autoServerRow || "";
      if (!name) return;

      activateAutolistServer(name);
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
        autolistSortDir = key === "name" ? "asc" : "desc";
      }

      renderAutolistServers();
    });

    document.addEventListener("click", (ev) => {
      const saveDeviceBtn = ev.target.closest("button[data-admin-save-device]");
      if (saveDeviceBtn) {
        const subjectId = saveDeviceBtn.dataset.adminSaveDevice || "";
        if (subjectId) saveAdminDevice(subjectId);
        return;
      }

      const saveVlessBtn = ev.target.closest("button[data-admin-save-vless-name]");
      if (saveVlessBtn) {
        const id = saveVlessBtn.dataset.adminSaveVlessName || "";
        if (id) saveAdminVlessClientName(id);
        return;
      }

      const deleteVlessBtn = ev.target.closest("button[data-admin-delete-vless]");
      if (deleteVlessBtn) {
        const id = deleteVlessBtn.dataset.adminDeleteVless || "";
        if (id) deleteAdminVlessClient(id);
      }
    });

    syncAutolistApplyButton();

    loadAutolist({ liveMeasure: false });
    loadAdminVpnOverview({ silent: false });
    loadSelectiveDefault();
    loadAdminDevices(false);
    bindAdminRefreshOnReturn();

    enhanceAdminSelects(el("admin-top"));
  }

  window.addEventListener("DOMContentLoaded", () => {
    if ((document.documentElement.dataset.view || "user") === "admin") {
      wire();
    }
  });

  document.addEventListener("fwrouter:view", (event) => {
    const view = event && event.detail ? event.detail.view : "";
    if (view === "admin") wire();
  });

  document.addEventListener("fwrouter:display-settings-updated", (event) => {
    if ((document.documentElement.dataset.view || "") !== "admin") return;
    const next = event && event.detail ? event.detail.display_settings : null;
    if (!next || typeof next !== "object") return;
    adminClientDisplaySettings = {
      ...adminClientDisplaySettings,
      ...next,
    };
    syncAdminDeviceTabs();
    renderAdminDevices();
  });
})();
