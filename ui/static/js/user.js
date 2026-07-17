(function () {
  const el = (id) => document.getElementById(id);
  const RUNTIME_REFRESH_MIN_INTERVAL_MS = 2000;

  const {
    fetchJson,
    fetchApiV2,
    actionMessage,
    pollJob,
    waitForAppliedState,
    escapeHtml,
    setPendingState,
    setPendingStateMany,
    createPendingHelpers,
  } = window.FwrouterUI;
  const {
    setPendingScope,
    flashScopeResult,
  } = createPendingHelpers([
    ".user-hero",
    ".user-mode",
    ".user-panel",
    ".panel",
    "[data-section]",
  ]);
  const {
    parseCurrentServerName,
    renderServerListName,
    renderCurrentServerTitle,
    preloadCurrentServerFlag,
  } = window.FwrouterUserServers;
  const { loadClientExternalIpPair } = window.FwrouterIpCheck;

  function setText(id, txt) {
    const node = el(id);
    if (!node) return;
    const value = txt || "";
    node.textContent = value;
    if (node.classList.contains("pill")) node.hidden = !value;
  }

  function setSelectValue(id, value, fallback) {
    const node = el(id);
    if (!node) return;
    const next = String(value || fallback || "").toUpperCase();
    node.value = next || (fallback || "");
    if (node.value !== next && fallback) node.value = fallback;
    node.dispatchEvent(new Event("change", { bubbles: true }));
  }

  let serverPicker = null;
  let allServersPicker = null;
  let serverPingControl = null;

  let userPingConfig = {
    group: "PROXY",
    url: "https://cp.cloudflare.com/generate_204",
    ip_check_direct_url: "https://api.ipify.org?format=json",
    ip_check_vpn_url: "https://api.ipify.org?format=json",
    timeout_ms: 2500,
  };

  let currentClientIp = "";
  let currentSubjectId = "";
  let currentSubject = null;
  let currentServerName = "DIRECT";
  let currentUserMode = "SELECTIVE";
  let currentUserModeSource = "GLOBAL";
  let userServerOverride = "VPN-AUTO";
  let preferredServer = "";
  let lastAutoPreferred = "";
  let powerInView = true;
  let powerApplyInFlight = false;

  let autoNames = [];
  let allRows = [];
  let activeSource = "auto";
  let activeAutoValue = "";
  let activeAllValue = "";
  let proxyAllNames = [];
  let pingLoading = false;
  let runtimePollBusy = false;
  let runtimeRefreshLastAt = 0;
  let userBootstrapped = false;
  let knownServers = [];

  const DEV_ADMIN_CURRENT_PROXY_KEY = "fwrouter.dev.adminCurrentProxy";
  const UI_AUTOLIST_CONFIG_KEY = "fwrouter.ui.autolistConfig.v1";

  function getUserPingConfig() {
    const fallback = {
      group: "PROXY",
      url: "https://cp.cloudflare.com/generate_204",
      ip_check_direct_url: "https://api.ipify.org?format=json",
      ip_check_vpn_url: "https://api.ipify.org?format=json",
      timeout_ms: 2500,
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

  function getDevAdminCurrentProxy() {
    try {
      return String(window.localStorage.getItem(DEV_ADMIN_CURRENT_PROXY_KEY) || "").trim();
    } catch (_) {
      return "";
    }
  }

  function isAutoOverride(value) {
    return String(value || "").startsWith("VPN-AUTO");
  }

  function isManualPowerActive() {
    const isAuto = isAutoOverride(userServerOverride || "VPN-AUTO");
    const hasServer = Boolean(currentServerName && currentServerName !== "DIRECT");
    return Boolean(!isAuto && hasServer);
  }

  function setStatusChip(id, text, tone) {
    const node = el(id);
    if (!node) return;

    node.textContent = text || "—";
    node.classList.remove("is-neutral", "is-blue", "is-green", "is-violet", "is-amber");

    if (tone) node.classList.add(`is-${tone}`);
  }

  function rememberAutoTarget(name) {
    const safe = String(name || "").trim();
    if (!safe || safe === "VPN-AUTO" || safe === "__empty__") return;
    if (proxyAllNames.length && !proxyAllNames.includes(safe)) return;
    lastAutoPreferred = safe;
  }

  function getRememberedAutoTarget() {
    const candidates = [
      activeAutoValue,
      preferredServer,
      lastAutoPreferred,
      (isAutoOverride(userServerOverride) ? currentServerName : ""),
    ].map((x) => String(x || "").trim());

    for (const value of candidates) {
      if (!value || value === "VPN-AUTO" || value === "__empty__") continue;
      if (proxyAllNames.length && !proxyAllNames.includes(value)) continue;
      return value;
    }

    return "";
  }

  function resolveUserMode(mode) {
    const up = String(mode || "SELECTIVE").toUpperCase();
    if (up === "DIRECT" || up === "SELECTIVE" || up === "VPN") return up;
    return "SELECTIVE";
  }

  function modeLabel(mode) {
    const safe = resolveUserMode(mode);
    if (safe === "DIRECT") return "Direct";
    if (safe === "VPN") return "VPN";
    return "Selective";
  }

  function modeSourceLabel(source) {
    const value = String(source || "").trim().toUpperCase();
    if (value === "GLOBAL") return "Global";
    if (value === "ADMIN_LOCKED" || value === "ADMIN_OVERRIDE") return "Админ";
    if (value === "USER_OVERRIDE") return "Пользователь";
    if (value === "XRAY_FORCED_VPN") return "Xray";
    return value ? value : "Global";
  }

  function modeStatusLabel() {
    const source = modeSourceLabel(currentUserModeSource);
    const mode = modeLabel(currentUserMode);
    return source === "Global" ? `Global (${mode})` : mode;
  }

  function modeSourceTone() {
    const value = String(currentUserModeSource || "").trim().toUpperCase();
    if (value === "GLOBAL") return "blue";
    if (value === "ADMIN_LOCKED" || value === "ADMIN_OVERRIDE") return "amber";
    if (value === "USER_OVERRIDE") return "violet";
    return "neutral";
  }

  function pingCell(delay) {
    if (pingLoading) return '<span class="ping-spinner" aria-hidden="true"></span>';
    return (typeof delay === "number" && delay > 0) ? `${delay} ms` : "n/a";
  }

  function syncSelectionStateFromOverride() {
    const current = String(userServerOverride || "VPN-AUTO");

    if (current === "VPN-AUTO") {
      activeSource = "auto";
      activeAutoValue = lastAutoPreferred || preferredServer || currentServerName || "";
      activeAllValue = "";
      return;
    }

    if (current.startsWith("VPN-AUTO:")) {
      const preferred = current.split(":", 2)[1] || "";
      activeSource = "auto";
      activeAutoValue = preferred;
      activeAllValue = "";
      rememberAutoTarget(preferred);
      return;
    }

    activeSource = "all";
    activeAutoValue = lastAutoPreferred || preferredServer || currentServerName || "";
    activeAllValue = current;
  }

  function syncCurrentHighlights() {
    const current = String(currentServerName || "");

    if (serverPicker) {
      const autoCurrent = autoNames.includes(current)
        ? current
        : "";
      serverPicker.setCurrentValue(autoCurrent);
    }

    if (allServersPicker) {
      const manualCurrent = allRows.some((row) => row.name === current)
        ? current
        : "";
      allServersPicker.setCurrentValue(manualCurrent);
    }
  }

  function repaintLists() {
    if (!serverPicker && !allServersPicker) return;
    fillAutoPicker(autoNames, allRows);
    fillAllPicker(allRows);
    syncCurrentHighlights();
  }

  function updatePowerWorkingState() {
    const power = el("powerConnect");
    if (!power) return;

    const isWorking = isManualPowerActive();
    const watching = Boolean(isWorking && !document.hidden && powerInView);

    power.classList.toggle("is-working", isWorking);
    power.classList.toggle("is-watching", watching);
  }

  function updatePowerModeTone() {
    const power = el("powerConnect");
    if (!power) return;

    const isAuto = isAutoOverride(userServerOverride || "VPN-AUTO");
    const hasServer = Boolean(currentServerName && currentServerName !== "DIRECT");
    const isManual = Boolean(!isAuto && hasServer);

    power.classList.toggle("is-auto-mode", false);
    power.classList.toggle("is-manual-mode", isManual);

    power.setAttribute(
      "title",
      isManual
        ? "Подключен конкретный сервер"
        : hasServer
          ? "Источник: VPN-auto"
          : "Ожидание подключения"
    );

    updatePowerWorkingState();
  }

  function updateUserStatus() {
    const node = el("userStatus");
    if (!node) return;

    const isAuto = isAutoOverride(userServerOverride || "VPN-AUTO");
    const hasServer = Boolean(currentServerName && currentServerName !== "DIRECT");
    const parsed = parseCurrentServerName(currentServerName);
    const mode = modeStatusLabel();
    const serverSource = isAuto ? "VPN-auto" : "Manual";

    let statusText = "Отключено";

    if (hasServer) {
      statusText = `Подключен: ${parsed.full} · Сервер: ${serverSource} · Режим: ${mode}`;
    } else {
      statusText = `Отключено · Сервер: ${serverSource} · Режим: ${mode}`;
    }

    node.textContent = statusText;

    setStatusChip("userStatusState", hasServer ? "Активен" : "Отключен", hasServer ? "green" : "neutral");
    setStatusChip("userStatusSource", modeSourceLabel(currentUserModeSource), modeSourceTone());

    if (currentUserMode === "DIRECT") {
      setStatusChip("userStatusMode", "Direct", "amber");
    } else if (currentUserMode === "VPN") {
      setStatusChip("userStatusMode", "VPN", "violet");
    } else {
      setStatusChip("userStatusMode", "Selective", "green");
    }
  }

  function setServerCurrentLabel(name) {
    const legacyNode = el("serverCurrent");
    const titleNode = el("serverCurrentName");
    const protoNode = el("serverCurrentProto");

    const text = String(name || "DIRECT");
    currentServerName = text;

    if (text && text !== "DIRECT") {
      rememberAutoTarget(text);
    }

    const parsed = parseCurrentServerName(text);

    if (legacyNode) {
      legacyNode.textContent = text;
    }

    if (titleNode) {
      titleNode.innerHTML = renderCurrentServerTitle(parsed);
      preloadCurrentServerFlag(parsed);
    }

    if (protoNode) {
      protoNode.textContent = parsed.protocol;
    }

    updatePowerModeTone();
    updateUserStatus();
    syncCurrentHighlights();
  }

  function syncModeSegment() {
    const mode = resolveUserMode(el("globalMode")?.value || currentUserMode);
    el("modeDirectBtn")?.classList.toggle("is-active", mode === "DIRECT");
    el("modeSelectiveBtn")?.classList.toggle("is-active", mode === "SELECTIVE");
    el("modeTunnelBtn")?.classList.toggle("is-active", mode === "VPN");
    currentUserMode = mode;
    updateUserStatus();
  }

  function fillAutoPicker(names, delays) {
    if (!serverPicker) return;

    autoNames = Array.isArray(names) ? names.slice() : [];
    const delayMap = {};
    (delays || []).forEach((d) => { delayMap[d.name] = d.delay; });

    if (currentServerName && autoNames.includes(currentServerName)) {
      rememberAutoTarget(currentServerName);
    }

    const preferredTop = String(
      serverPicker?.getValue()
      || activeAutoValue
      || preferredServer
      || currentServerName
      || ""
    ).trim();

    if (preferredTop && autoNames.includes(preferredTop)) {
      autoNames = [preferredTop].concat(autoNames.filter((name) => name !== preferredTop));
    }

    window.FwrouterPingSelect?.preloadFlagsFromNames?.(autoNames);

    const items = autoNames.map((name) => ({
      value: name,
      primary: name,
      secondary: pingCell(delayMap[name]),
      triggerLabel: name,
      sort: {
        name,
        ping: (typeof delayMap[name] === "number" && delayMap[name] > 0) ? delayMap[name] : 999999,
      },
      cells: [
        renderServerListName(name),
        pingLoading
          ? '<span class="ping-spinner" aria-hidden="true"></span>'
          : escapeHtml(pingCell(delayMap[name])),
      ],
    }));

    if (!items.length) {
      serverPicker.setItems([{
        value: "__empty__",
        primary: "Нет серверов",
        secondary: "—",
        triggerLabel: "Нет серверов",
        sort: { name: "Нет серверов", ping: 999999 },
        cells: ["Нет серверов", "—"],
      }]);
      serverPicker.setValue("__empty__");
      activeAutoValue = "";
      return;
    }

    serverPicker.setItems(items);

    const preferred = preferredServer || lastAutoPreferred || currentServerName || activeAutoValue;
    const next = items.some((item) => item.value === preferred)
      ? preferred
      : items[0].value;

    serverPicker.setValue(next);
    activeAutoValue = next;
  }

  function fillAllPicker(rows) {
    if (!allServersPicker) return;

    allRows = Array.isArray(rows) ? rows.slice() : [];

    if (!allRows.length) {
      allServersPicker.setItems([{
        value: "__empty__",
        primary: "Нет серверов",
        secondary: "—",
        triggerLabel: "Нет серверов",
        sort: { name: "Нет серверов", ping: 999999 },
        cells: ["Нет серверов", "—"],
      }]);
      allServersPicker.setValue("__empty__");
      allServersPicker.setCurrentValue("");
      activeAllValue = "";
      return;
    }

    const items = allRows.map((row) => {
      const ping = pingCell(row.delay);

      return {
        value: row.name,
        primary: row.name,
        secondary: ping,
        triggerLabel: row.name,
        sort: {
          name: row.name,
          ping: (typeof row.delay === "number" && row.delay > 0) ? row.delay : 999999,
        },
        cells: [
          renderServerListName(row),
          pingLoading
            ? '<span class="ping-spinner" aria-hidden="true"></span>'
            : escapeHtml(ping),
        ],
      };
    });

    allServersPicker.setItems(items);

    const next = items.some((item) => item.value === activeAllValue) ? activeAllValue : "";
    allServersPicker.setValue(next);
    activeAllValue = next;
  }

  function getUserPingCacheKey() {
    const optionCount = Math.max(
      1,
      allRows.length || proxyAllNames.length || (serverPicker?.getCount() ? serverPicker.getCount() : 1)
    );
    const group = userPingConfig.group || "PROXY";
    const url = userPingConfig.url || "https://cp.cloudflare.com/generate_204";
    const timeoutMs = Number(userPingConfig.timeout_ms || 2500);

    return ["mihomo-servers", group, url, timeoutMs, optionCount].join("|");
  }

  function filterHidden(names, hiddenUser) {
    const hidden = new Set((hiddenUser || []).map((n) => String(n)));
    return (names || []).filter((name) => !hidden.has(name));
  }

  function getKnownServerByName(name) {
    const normalized = String(name || "").trim();
    if (!normalized) return null;
    return knownServers.find((server) => {
      const serverName = String(server?.server_name || server?.name || "").trim();
      const serverId = String(server?.server_id || "").trim();
      return serverName === normalized || serverId === normalized;
    }) || null;
  }

  function getKnownServerName(serverId) {
    const normalized = String(serverId || "").trim();
    if (!normalized) return "";
    const match = knownServers.find((server) => String(server?.server_id || "").trim() === normalized);
    return String(match?.server_name || normalized).trim();
  }

  async function loadCurrentWhoami() {
    const data = await fetchApiV2("/ui/whoami", { cache: "no-store" });
    const whoami = data.whoami || {};
    const subject = whoami.subject || null;
    const detail = subject && typeof subject.detail === "object" ? subject.detail : {};

    currentSubject = subject;
    currentClientIp = String(detail.ip_address || whoami.client_ip || currentClientIp || "").trim();
    currentSubjectId = String(subject?.subject_id || currentSubjectId || "").trim();

    return { whoami, subject };
  }

  async function loadCurrentUiClient() {
    if (currentSubject) return currentSubject;
    const { subject } = await loadCurrentWhoami();
    return subject || null;
  }

  function effectiveStateOf(subject) {
    return subject && typeof subject.effective_state === "object" ? subject.effective_state : {};
  }

  async function loadUserServerOverride() {
    try {
      const { subject } = await loadCurrentWhoami();
      const uiClient = subject || await loadCurrentUiClient();
      const [routerData, overrideData] = await Promise.all([
        fetchApiV2("/ui/router-summary", { cache: "no-store" }),
        currentSubjectId
          ? fetchApiV2(`/subjects/${encodeURIComponent(currentSubjectId)}/server-override`, { cache: "no-store" })
          : Promise.resolve({}),
      ]);

      const override = overrideData.server_override || null;
      const overrideId = String(override?.selected_server_id || "").trim();
      const overrideName = overrideId ? getKnownServerName(overrideId) : "";
      const effectiveState = effectiveStateOf(uiClient);
      const appliedMode = String(uiClient?.applied_mode || subject?.applied_mode || uiClient?.desired_mode || subject?.desired_mode || "GLOBAL").toUpperCase();
      const effectiveMode = String(uiClient?.effective_mode || effectiveState.effective_mode || subject?.effective_mode || "").toUpperCase();
      currentUserModeSource = String(uiClient?.mode_source || effectiveState.mode_source || subject?.mode_source || (appliedMode === "GLOBAL" ? "GLOBAL" : "USER_OVERRIDE")).toUpperCase();
      currentUserMode = resolveUserMode(appliedMode === "GLOBAL" ? (effectiveMode || "SELECTIVE") : (appliedMode === "DISABLED" ? "DIRECT" : appliedMode));
      const router = routerData.router || {};
      const routerTarget = String(router.current_server_name || router.active_auto_server_id || "").trim();
      const effectiveTarget = overrideName || (appliedMode === "DIRECT" ? "DIRECT" : (routerTarget || "DIRECT"));

      userServerOverride = overrideName || "VPN-AUTO";

      const nextPreferred = effectiveTarget && effectiveTarget !== "DIRECT" ? String(effectiveTarget).trim() : "";
      if (nextPreferred) {
        preferredServer = nextPreferred;
        rememberAutoTarget(nextPreferred);
      } else if (isAutoOverride(userServerOverride)) {
        preferredServer = lastAutoPreferred || "";
      } else {
        preferredServer = "";
      }

      syncSelectionStateFromOverride();

      const devGlobalTarget = getDevAdminCurrentProxy();

      if (isAutoOverride(userServerOverride || "VPN-AUTO") && devGlobalTarget) {
        setServerCurrentLabel(devGlobalTarget);
      } else if (effectiveTarget) {
        setServerCurrentLabel(effectiveTarget);
      }

      updatePowerModeTone();
      updateUserStatus();
      repaintLists();

      return {
        ip: currentClientIp,
        subject_id: currentSubjectId,
        override: userServerOverride,
        preferred: preferredServer,
        effective: {
          target: effectiveTarget,
        },
      };
    } catch (e) {
      setText("serversState", "error: " + e.message);
      return null;
    }
  }

  function applyServerPingData(data) {
    const srv = (data && data.srv) ? data.srv : {};
    const auto = (data && data.auto) ? data.auto : {};

    const rawRows = (srv.servers || []).map((s) => ({ name: s.name, delay: s.delay }));
    const allNames = rawRows.map((r) => r.name);
    proxyAllNames = allNames.slice();

    const hiddenUser = (auto.config && auto.config.hidden_user) || [];
    const candidates = (auto.config && auto.config.candidates) || [];

    const visibleAll = filterHidden(allNames, hiddenUser);
    const visibleAllRows = visibleAll.map((name) => rawRows.find((r) => r.name === name)).filter(Boolean);

    const visibleAuto = filterHidden(candidates, hiddenUser).filter((name) => allNames.includes(name));
    const autoRows = visibleAuto.map((name) => rawRows.find((r) => r.name === name)).filter(Boolean);

    fillAutoPicker(autoRows.map((r) => r.name), visibleAllRows);
    fillAllPicker(visibleAllRows);

    if (!currentServerName || currentServerName === "DIRECT") {
      const devGlobalTarget = getDevAdminCurrentProxy();
      setServerCurrentLabel(devGlobalTarget || srv.now || "DIRECT");
    } else {
      syncCurrentHighlights();
    }

    loadClientExternalIpPair(userPingConfig, {
      useBackendFallback: true,
      preferBackend: true,
    });
    setText("serversState", "");
  }

  async function loadServersWithPingData(liveMeasure = false) {
    setText("serversState", "");
    pingLoading = true;
    repaintLists();

    try {
      setText("serversState", liveMeasure ? "измерение…" : "");

      const limit = liveMeasure ? Math.max(1, Math.min(serverPicker?.getCount() || 10, 20)) : 20;
      const [serversData, sweepData] = await Promise.all([
        fetchApiV2("/servers?inventory_state=active&limit=1000", { cache: "no-store" }),
        liveMeasure
          ? fetchApiV2("/server-ping/sweep", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              checked_by: "ui",
              timeout_ms: Number(userPingConfig.timeout_ms || 2500),
              limit,
            }),
          }).catch(() => ({}))
          : Promise.resolve({}),
      ]);

      const servers = Array.isArray(serversData.servers) ? serversData.servers : [];
      knownServers = servers.slice();
      const sweep = sweepData.sweep || {};
      const resultMap = new Map((Array.isArray(sweep.results) ? sweep.results : []).map((item) => [String(item.server_id || ""), item]));

      const visibleServers = servers
        .filter((server) => server && String(server.server_id || "").trim())
        .filter((server) => !String(server.server_id || "").startsWith("virtual:"))
        .filter((server) => Boolean(server?.preferences?.global_list) !== false);

      const srv = {
        now: currentServerName,
        servers: visibleServers.map((server) => {
          const result = resultMap.get(String(server.server_id || ""));
          const delay = typeof result?.delay_ms === "number"
            ? result.delay_ms
            : (typeof server?.ping?.last_ping_ms === "number" ? server.ping.last_ping_ms : null);
          return {
            name: String(server.server_name || server.server_id || ""),
            delay,
            status: String(result?.status || server?.ping?.status || "unknown"),
            server_id: String(server.server_id || ""),
            kind: String(server.kind || ""),
          };
        }),
      };

      const auto = {
        config: {
          candidates: visibleServers
            .filter((server) => Boolean(server?.preferences?.vpn_auto))
            .map((server) => String(server.server_name || server.server_id || "")),
          hidden_user: [],
        },
      };

      return { srv, auto };
    } catch (e) {
      setText("serversState", "error: " + e.message);
      throw e;
    } finally {
      pingLoading = false;
      repaintLists();
    }
  }

  async function loadServersBasic(opts) {
    const options = opts || {};

    try {
      const serversData = await fetchApiV2("/servers?inventory_state=active&limit=1000", { cache: "no-store" });
      const servers = Array.isArray(serversData.servers) ? serversData.servers : [];
      knownServers = servers.slice();

      const visibleServers = servers
        .filter((server) => server && String(server.server_id || "").trim())
        .filter((server) => !String(server.server_id || "").startsWith("virtual:"))
        .filter((server) => Boolean(server?.preferences?.global_list) !== false);
      const rawAll = visibleServers.map((server) => String(server.server_name || server.server_id || ""));
      proxyAllNames = rawAll.slice();

      const hiddenUser = [];
      const candidates = visibleServers
        .filter((server) => Boolean(server?.preferences?.vpn_auto))
        .map((server) => String(server.server_name || server.server_id || ""));

      const allNames = filterHidden(rawAll, hiddenUser);
      const autoNamesLocal = filterHidden(candidates, hiddenUser).filter((name) => rawAll.includes(name));

      fillAutoPicker(autoNamesLocal, []);
      fillAllPicker(allNames.map((name) => ({ name, delay: null })));

      await loadUserServerOverride();

      if (!options.skipIpRefresh) {
        await loadClientExternalIpPair(userPingConfig, {
          cacheBust: Boolean(options.cacheBustIps),
          keepCurrentOnFail: true,
          useBackendFallback: true,
          preferBackend: true,
        });
      }

      try {
        const cachedPingData = await loadServersWithPingData(false);
        applyServerPingData(cachedPingData);
        await loadUserServerOverride();
      } catch (_) {
        // Keep the basic list even if cached delay fetch fails.
      }

      repaintLists();
    } catch (e) {
      setText("serversState", "error: " + e.message);
    }
  }

  async function loadServersWithPing(liveMeasure = true) {
    const data = await loadServersWithPingData(liveMeasure);
    applyServerPingData(data);
    await loadUserServerOverride();
    return data;
  }

  function currentSelectionToTarget() {
    const autoSelectedNow = String(serverPicker?.getValue() || activeAutoValue || "");
    const allSelectedNow = String(allServersPicker?.getValue() || activeAllValue || "");

    activeAutoValue = autoSelectedNow;
    activeAllValue = allSelectedNow;

    if (activeSource === "all") {
      const selected = String(allSelectedNow || "");
      if (!selected || selected === "__empty__") return "";
      return selected;
    }

    const selected = String(autoSelectedNow || "");
    if (!selected || selected === "__empty__") return "";
    if (!proxyAllNames.includes(selected)) return "";

    rememberAutoTarget(selected);

    return selected;
  }

  async function applyTarget(target) {
    await loadCurrentWhoami();

    if (!currentSubjectId) {
      setText("serversState", "error: не удалось определить устройство");
      return false;
    }

    const normalizedTarget = String(target || "").trim();

    if (normalizedTarget === "VPN-AUTO") {
      const response = await fetchApiV2(`/subjects/${encodeURIComponent(currentSubjectId)}/server-override`, {
        method: "DELETE",
      });
      const jobId = String(response?.job?.job_id || "").trim();
      if (jobId) {
        await pollJob(jobId, {
          onProgress(status) {
            setText("serversState", status === "queued" ? "в очереди…" : "применение…");
          },
        });
      }
      await waitForAppliedState(loadUserServerOverride, () => isAutoOverride(userServerOverride || "VPN-AUTO"));
    } else {
      const server = getKnownServerByName(target);
      if (!server || !server.server_id) {
        setText("serversState", "error: сервер не найден");
        return false;
      }

      const response = await fetchApiV2(`/subjects/${encodeURIComponent(currentSubjectId)}/server-override`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          server_id: String(server.server_id),
          requested_by: "ui",
          run_now: false,
        }),
      });
      const jobId = String(response?.job?.job_id || "").trim();
      if (jobId) {
        await pollJob(jobId, {
          onProgress(status) {
            setText("serversState", status === "queued" ? "в очереди…" : "применение…");
          },
        });
      }
      await waitForAppliedState(
        loadUserServerOverride,
        () => !isAutoOverride(userServerOverride || "") && String(userServerOverride || "") === String(server.server_name || target || "")
      );
      rememberAutoTarget(String(server.server_name || target || ""));
    }

    setText("serversState", "");

    return true;
  }

  async function forceRefreshIpsAfterSwitch() {
    const maxAttempts = 4;
    const sleepMs = 1000;
    let last = { directIp: "", vpnIp: "" };

    setText("serverCurrentIpDirect", "");
    setText("serverCurrentIpVpn", "");

    for (let i = 0; i < maxAttempts; i += 1) {
      const lastTry = i === maxAttempts - 1;

      setText("serversState", "обновление IP…");

      last = await loadClientExternalIpPair(userPingConfig, {
        cacheBust: true,
        keepCurrentOnFail: false,
        useBackendFallback: false,
      });

      const hasDirect = Boolean(last.directIp);
      const hasVpn = Boolean(last.vpnIp);

      if (hasDirect && hasVpn) {
        setText("serversState", "");
        return true;
      }

      if (!lastTry) {
        await new Promise((resolve) => window.setTimeout(resolve, sleepMs));
      }
    }

    setText("serversState", "warning: не удалось обновить оба IP, повторите через пару секунд");
    return false;
  }

  async function onPowerClick() {
    if (powerApplyInFlight) return;

    powerApplyInFlight = true;
    setText("serversState", "");

    const power = el("powerConnect");
    setPendingState(power, true);
    setPendingScope(power, true);

    try {
      setText("serversState", "применение…");

      const candidate = currentSelectionToTarget();
      const currentOverride = String(userServerOverride || "");
      const hasManualOverride = Boolean(currentOverride && !isAutoOverride(currentOverride));

      let target = "";

      if (hasManualOverride) {
        target = "VPN-AUTO";
      } else {
        target = candidate || getRememberedAutoTarget() || currentServerName || activeAllValue || activeAutoValue;
      }

      if (!target || target === "__empty__" || (target !== "VPN-AUTO" && !proxyAllNames.includes(target))) {
        setText("serversState", "error: не выбран доступный сервер");
        return;
      }

      const ok = await applyTarget(target);

      if (!ok) return;

      await forceRefreshIpsAfterSwitch();

      power?.classList.remove("is-pressing");

      if (power) {
        window.requestAnimationFrame(() => power.classList.add("is-pressing"));
        window.setTimeout(() => power.classList.remove("is-pressing"), 620);
      }

      await loadServersBasic({ skipIpRefresh: true });
      flashScopeResult(power, "success");
    } catch (e) {
      setText("serversState", "error: " + e.message);
      flashScopeResult(power, "error");
    } finally {
      powerApplyInFlight = false;
      setPendingState(power, false);
      setPendingScope(power, false);
    }
  }

  async function loadRouting() {
    try {
      const { subject } = await loadCurrentWhoami();
      const uiClient = subject || await loadCurrentUiClient();
      const effectiveState = effectiveStateOf(uiClient);
      const appliedMode = String(uiClient?.applied_mode || uiClient?.desired_mode || "GLOBAL").toUpperCase();
      const effectiveMode = String(uiClient?.effective_mode || effectiveState.effective_mode || "").toUpperCase();
      currentUserModeSource = String(uiClient?.mode_source || effectiveState.mode_source || (appliedMode === "GLOBAL" ? "GLOBAL" : "USER_OVERRIDE")).toUpperCase();
      const displayMode = appliedMode === "GLOBAL"
        ? (effectiveMode || "SELECTIVE")
        : (appliedMode === "DISABLED" ? "DIRECT" : appliedMode);
      currentUserMode = resolveUserMode(displayMode);
      setSelectValue("globalMode", currentUserMode, "SELECTIVE");
      syncModeSegment();
      setText("routingState", "");
    } catch (e) {
      setText("routingState", "error: " + e.message);
    }
  }

  async function saveUserMode(mode) {
    const safe = resolveUserMode(mode || el("globalMode")?.value || currentUserMode);
    const controls = [
      el("modeDirectBtn"),
      el("modeSelectiveBtn"),
      el("modeTunnelBtn"),
    ];
    const activeControl = controls.find((node) => String(node?.dataset?.mode || "").toUpperCase() === safe) || null;
    const scopeNode = el("modeSeg") || activeControl;

    await loadCurrentWhoami();

    if (!currentSubjectId) {
      setText("routingState", "error: не удалось определить устройство");
      return;
    }

    try {
      setPendingStateMany(controls, true);
      setPendingScope(scopeNode, true);
      if (activeControl) activeControl.classList.add("is-pending-target");
      const action = await fetchApiV2(`/subjects/${encodeURIComponent(currentSubjectId)}/mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode: safe.toLowerCase(),
          actor_scope: "user",
          requested_by: "ui",
          run_now: false,
        }),
      });

      const jobId = String(action?.job?.job_id || "").trim();
      if (jobId) {
        await pollJob(jobId, {
          onProgress(status) {
            setText("routingState", status === "queued" ? "в очереди…" : "применение…");
          },
        });
      }
      await waitForAppliedState(loadRouting, () => currentUserMode === safe);

      setText("routingState", "");
      flashScopeResult(scopeNode, "success");
    } catch (e) {
      setText("routingState", "error: " + actionMessage(e));
      flashScopeResult(scopeNode, "error");
    } finally {
      if (activeControl) activeControl.classList.remove("is-pending-target");
      setPendingStateMany(controls, false);
      setPendingScope(scopeNode, false);
    }
  }

  async function switchUserMode(mode) {
    const safe = resolveUserMode(mode);
    const select = el("globalMode");

    if (!select) return;

    if (select.value !== safe) {
      select.value = safe;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }

    await saveUserMode(safe);

    try {
      setText("serversState", "обновление IP…");

      await loadClientExternalIpPair(userPingConfig, {
        cacheBust: true,
        keepCurrentOnFail: false,
        useBackendFallback: false,
      });

      setText("serversState", "");
    } catch (_) {
      setText("serversState", "warning: IP не обновился сразу после смены режима");
    }
  }

  async function refreshRuntimeOnReturn() {
    if (document.hidden || runtimePollBusy) return;
    if ((document.documentElement.dataset.view || "") !== "user") return;
    if (document.querySelector("#user-top .is-pending-scope, #user-top .is-pending")) return;
    const now = Date.now();
    if (now - runtimeRefreshLastAt < RUNTIME_REFRESH_MIN_INTERVAL_MS) return;

    runtimePollBusy = true;
    runtimeRefreshLastAt = now;
    try {
      await Promise.allSettled([
        loadRouting(),
        loadUserServerOverride(),
      ]);
    } finally {
      runtimePollBusy = false;
    }
  }

  function bindRuntimeRefreshOnReturn() {
    window.addEventListener("focus", refreshRuntimeOnReturn);
    window.addEventListener("pageshow", refreshRuntimeOnReturn);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshRuntimeOnReturn();
    });
  }

  function wire() {
    if (userBootstrapped) return;
    userBootstrapped = true;
    userPingConfig = getUserPingConfig();

    const serverSelect = el("serverSelect");

    if (serverSelect && window.FwrouterPingSelect) {
      serverPicker = window.FwrouterPingSelect.createTablePicker({
        root: serverSelect,
        placeholder: "VPN-auto",
        alwaysOpen: true,
        columns: [
          { key: "name", label: "Сервер", className: "picklist__cell--name", sortable: true },
          { key: "ping", label: "Пинг", className: "picklist__cell--ping", sortable: true },
        ],
      });

      serverPingControl = window.FwrouterPingSelect.bindLazyPingSelect({
        target: serverSelect,
        cooldownMs: 180000,
        getCacheKey: getUserPingCacheKey,
        loadData: () => loadServersWithPingData(true),
        applyData: applyServerPingData,
        autoTrigger: false,
      });

      serverSelect.addEventListener("change", () => {
        activeSource = "auto";
        activeAutoValue = serverPicker?.getValue() || "";

        if (activeAutoValue && activeAutoValue !== "__empty__") {
          rememberAutoTarget(activeAutoValue);
          preferredServer = activeAutoValue;
        }

        activeAllValue = "";
        allServersPicker?.setValue("");
        syncCurrentHighlights();
      });
    }

    const allServersSelect = el("allServersSelect");

    if (allServersSelect && window.FwrouterPingSelect) {
      allServersPicker = window.FwrouterPingSelect.createTablePicker({
        root: allServersSelect,
        placeholder: "Все серверы",
        alwaysOpen: true,
        columns: [
          { key: "name", label: "Сервер", className: "picklist__cell--name", sortable: true },
          { key: "ping", label: "Пинг", className: "picklist__cell--ping", sortable: true },
        ],
      });

      allServersSelect.addEventListener("change", () => {
        activeSource = "all";
        activeAllValue = allServersPicker?.getValue() || "";
        activeAutoValue = lastAutoPreferred || preferredServer || currentServerName || "";

        if (activeAutoValue) serverPicker?.setValue(activeAutoValue);

        syncCurrentHighlights();
      });
    }

    el("modeDirectBtn")?.addEventListener("click", () => switchUserMode("DIRECT"));
    el("modeSelectiveBtn")?.addEventListener("click", () => switchUserMode("SELECTIVE"));
    el("modeTunnelBtn")?.addEventListener("click", () => switchUserMode("VPN"));
    el("globalMode")?.addEventListener("change", syncModeSegment);

    el("powerConnect")?.addEventListener("click", onPowerClick);

    const powerBtn = el("powerConnect");

    if (powerBtn && window.IntersectionObserver) {
      const observer = new IntersectionObserver((entries) => {
        const hit = entries && entries[0];
        powerInView = Boolean(hit && hit.isIntersecting && hit.intersectionRatio > 0.25);
        updatePowerWorkingState();
      }, { threshold: [0, 0.25, 0.5, 1] });

      observer.observe(powerBtn);
    }

    document.addEventListener("visibilitychange", updatePowerWorkingState);

    const bindAccordionAction = (id, handler) => {
      const btn = el(id);
      if (!btn) return;

      btn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        handler();
      });
    };

    bindAccordionAction("vpnAutoReloadBtn", () => {
      loadServersBasic();
    });

    bindAccordionAction("allServersReloadBtn", () => {
      loadServersBasic();
    });

    bindAccordionAction("vpnAutoPingBtn", () => {
      const runner = serverPingControl?.trigger?.(true);

      if (runner && typeof runner.catch === "function") {
        runner.catch((e) => setText("serversState", "error: " + e.message));
      } else {
        loadServersWithPing(true).catch((e) => setText("serversState", "error: " + e.message));
      }
    });

    bindAccordionAction("allServersPingBtn", () => {
      const runner = serverPingControl?.trigger?.(true);

      if (runner && typeof runner.catch === "function") {
        runner.catch((e) => setText("serversState", "error: " + e.message));
      } else {
        loadServersWithPing(true).catch((e) => setText("serversState", "error: " + e.message));
      }
    });

    Promise.allSettled([
      loadRouting(),
      loadServersBasic(),
    ]);

    syncModeSegment();
    updateUserStatus();
    updatePowerModeTone();
    bindRuntimeRefreshOnReturn();
  }

  window.addEventListener("DOMContentLoaded", () => {
    if ((document.documentElement.dataset.view || "user") === "user") {
      wire();
    }
  });

  document.addEventListener("fwrouter:view", (event) => {
    const view = event && event.detail ? event.detail.view : "";
    if (view === "user") wire();
  });
})();
