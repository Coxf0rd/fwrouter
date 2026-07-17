// settings.js — settings panel: admin event journal + routing rules
(function () {
  const el = (id) => document.getElementById(id);
  const AUTO_REFRESH_MIN_INTERVAL_MS = 2000;

  let settingsTab = "all";
  let loadedEvents = [];
  let searchQuery = "";
  let levelFilter = "";
  let selectedEventIndex = -1;
  let vpnSubscriptionSavedOnServer = false;
  let settingsBootstrapped = false;
  let settingsWorkspace = null;
  let settingsClientsTab = "all";
  let settingsServers = [];
  let settingsInventoryItems = [];
  let settingsHiddenSubjectIds = new Set();
  let settingsTrafficPreferences = {};
  let settingsInventoryRequestSeq = 0;
  let settingsInventoryAbortController = null;
  let settingsLogSearchTimer = null;
  let settingsAutoRefreshBusy = false;
  let settingsAutoRefreshLastAt = 0;

  const DEV_VPN_SUBSCRIPTION_URL_KEY = "fwrouter.dev.vpnSubscriptionUrl";
  const {
    fetchApiV2,
    actionMessage,
    pollJob,
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
    ".settings-client-row",
    ".settings-card",
    ".field",
    ".device-row",
    "[data-section]",
  ]);

  const {
    formatTs,
    categoryLabel,
    levelLabel,
    toLegacyEvent,
    toLegacyTechnicalEvent,
    toUnixSeconds,
    isJournalTab,
  } = window.FwrouterSettingsEvents;
  const { settingsModeLabel: modeLabel } = window.FwrouterLabels;
  const {
    TRAFFIC_METRIC_KEYS,
    normalizeTrafficPreferences,
    renderSettingsClientsHtml,
    renderSettingsCounts,
  } = window.FwrouterSettingsInventory;
  const {
    renderSelectedEventContextHtml,
    renderRulesContextHtml,
    renderEventsHtml,
  } = window.FwrouterSettingsJournal;

  function getDevVpnSubscriptionUrl() {
    try {
      return String(window.localStorage.getItem(DEV_VPN_SUBSCRIPTION_URL_KEY) || "").trim();
    } catch (_) {
      return "";
    }
  }

  function setDevVpnSubscriptionUrl(url) {
    try {
      const value = String(url || "").trim();

      if (value) {
        window.localStorage.setItem(DEV_VPN_SUBSCRIPTION_URL_KEY, value);
      } else {
        window.localStorage.removeItem(DEV_VPN_SUBSCRIPTION_URL_KEY);
      }
    } catch (_) {
      // ignore localStorage errors
    }
  }

  function normalizeSubscriptionPayload(j) {
    return String(
      (j && (
        j.url ||
        j.subscription_url ||
        j.subscriptionUrl ||
        j.value
      )) || ""
    ).trim();
  }

  function syncLevelDropdown() {
    const root = el("adminEventsLevel");
    const trigger = el("adminEventsLevelTrigger");
    const menu = el("adminEventsLevelMenu");
    const label = el("adminEventsLevelLabel");

    if (!root || !trigger || !menu || !label) return;

    const text = levelFilter ? levelLabel(levelFilter) : "Все уровни";

    label.textContent = text;
    trigger.setAttribute("aria-expanded", root.classList.contains("is-open") ? "true" : "false");
    menu.hidden = !root.classList.contains("is-open");

    menu.querySelectorAll("[data-level-value]").forEach((btn) => {
      btn.classList.toggle("is-active", (btn.dataset.levelValue || "") === levelFilter);
    });
  }

  function getEventSourceIndex(item) {
    const idx = loadedEvents.indexOf(item);
    return idx >= 0 ? idx : -1;
  }

  function renderSelectedEventContext() {
    const card = document.querySelector("#settings-top .settings-summary-card");
    const title = document.querySelector("#settings-top .settings-summary-card .panel__head .label");
    const body = document.querySelector("#settings-top .settings-summary-card__body");

    if (!card || !body) return;

    if (title) {
      title.textContent = "Детали события";
    }

    const item = loadedEvents[selectedEventIndex];
    card.classList.toggle("has-selected-event", Boolean(item));
    body.innerHTML = renderSelectedEventContextHtml(item);
  }

  function renderRulesContext(status) {
    const card = document.querySelector("#settings-top .settings-summary-card");
    const title = document.querySelector("#settings-top .settings-summary-card .panel__head .label");
    const body = document.querySelector("#settings-top .settings-summary-card__body");

    if (!card || !body) return;

    if (title) {
      title.textContent = "Детали правил";
    }

    body.innerHTML = renderRulesContextHtml(status);
  }

  function syncSelectedEventRows() {
    document.querySelectorAll("#settings-top [data-event-row]").forEach((row) => {
      const idx = Number(row.dataset.eventRow);
      row.classList.toggle("is-selected", idx === selectedEventIndex);
    });
  }

  function selectSettingsEvent(index) {
    const next = Number(index);

    if (!Number.isFinite(next) || next < 0 || !loadedEvents[next]) {
      selectedEventIndex = -1;
    } else {
      selectedEventIndex = next;
    }

    syncSelectedEventRows();
    renderSelectedEventContext();
  }

  function syncVpnSubscriptionHint() {
    const input = el("vpnSubscriptionUrl");
    const hint = el("vpnSubscriptionHint");

    if (!input || !hint) return;

    const url = String(input.value || "").trim();
    const active = Boolean(url) || vpnSubscriptionSavedOnServer;

    hint.classList.toggle("is-active", active);
    hint.classList.toggle("is-empty", !active);

    if (url) {
      hint.textContent = "Подписка активна";
      return;
    }

    hint.textContent = vpnSubscriptionSavedOnServer ? "Подписка сохранена на сервере" : "Подписка не задана";
  }

  function setCheckbox(id, value) {
    const node = el(id);
    if (!node) return;
    node.checked = Boolean(value);
  }

  function getSettingsDisplayPayload() {
    const current = (settingsWorkspace && settingsWorkspace.display_settings) || {};
    const checkedOrCurrent = (id, key, fallback = true) => {
      const node = el(id);
      if (node) return Boolean(node.checked);
      if (typeof current[key] === "boolean") return current[key];
      return fallback;
    };

    return {
      show_lan: checkedOrCurrent("settingsShowLan", "show_lan"),
      show_tailscale: checkedOrCurrent("settingsShowTailscale", "show_tailscale"),
      show_xray: checkedOrCurrent("settingsShowXray", "show_xray"),
      show_docker: checkedOrCurrent("settingsShowDocker", "show_docker"),
      show_host: checkedOrCurrent("settingsShowHost", "show_host"),
      show_inactive: checkedOrCurrent("settingsShowInactive", "show_inactive", false),
      show_internal_xray: checkedOrCurrent("settingsShowInternalXray", "show_internal_xray", false),
      hidden_subject_ids: Array.from(settingsHiddenSubjectIds),
      subject_traffic_preferences: settingsTrafficPreferences,
    };
  }

  function renderSubscriptionMeta() {
    const meta = el("vpnSubscriptionMeta");
    if (!meta) return;

    const subscription = (settingsWorkspace && settingsWorkspace.subscription) || {};
    const statusLabels = {
      success: "успешно",
      failed: "ошибка",
      running: "обновляется",
      idle: "ожидание",
      not_configured: "не настроена",
    };
    const parts = [];
    if (subscription.status) parts.push(`Статус: ${statusLabels[String(subscription.status)] || subscription.status}`);
    if (subscription.url_saved) parts.push("ссылка сохранена");
    if (subscription.last_refresh_at) parts.push(`обновлено: ${formatTs(subscription.last_refresh_at)}`);
    if (subscription.last_success_at) parts.push(`успешно: ${formatTs(subscription.last_success_at)}`);
    if (subscription.error_message) parts.push(subscription.error_message);
    meta.textContent = parts.join(" · ");
  }

  function applyDisplaySettings() {
    const settings = (settingsWorkspace && settingsWorkspace.display_settings) || {};
    settingsHiddenSubjectIds = new Set(
      Array.isArray(settings.hidden_subject_ids)
        ? settings.hidden_subject_ids.map((item) => String(item || "").trim()).filter(Boolean)
        : []
    );
    settingsTrafficPreferences = normalizeTrafficPreferences(settings.subject_traffic_preferences);
    setCheckbox("settingsShowLan", settings.show_lan);
    setCheckbox("settingsShowTailscale", settings.show_tailscale);
    setCheckbox("settingsShowXray", settings.show_xray);
    setCheckbox("settingsShowDocker", settings.show_docker);
    setCheckbox("settingsShowHost", settings.show_host);
    setCheckbox("settingsShowInactive", settings.show_inactive);
    setCheckbox("settingsShowInternalXray", settings.show_internal_xray);
  }

  function syncSettingsClientTabs() {
    [["settingsClientsTabAll", "all"], ["settingsClientsTabLan", "lan"], ["settingsClientsTabTs", "tailscale"], ["settingsClientsTabXray", "xray"], ["settingsClientsTabDocker", "docker"], ["settingsClientsTabHost", "host"]]
      .forEach(([id, value]) => {
        el(id)?.classList.toggle("is-active", settingsClientsTab === value);
      });
  }

  function renderSettingsClients() {
    const wrap = el("settingsClientsWrap");
    const meta = el("settingsClientsMeta");
    if (!wrap) return;

    const items = Array.isArray(settingsInventoryItems) ? settingsInventoryItems : [];
    const counts = settingsWorkspace?.counts || {};

    if (meta) {
      meta.textContent = renderSettingsCounts(counts);
    }

    if (!items.length) {
      wrap.innerHTML = '<div class="settings-events__empty muted">Клиенты не найдены</div>';
      syncSettingsClientTabs();
      return;
    }

    wrap.innerHTML = renderSettingsClientsHtml(items, {
      hiddenSubjectIds: settingsHiddenSubjectIds,
      trafficPreferences: settingsTrafficPreferences,
    });

    syncSettingsClientTabs();
  }

  function getSettingsClientRow(subjectId) {
    return document.querySelector(`[data-settings-client-row="${CSS.escape(String(subjectId || ""))}"]`);
  }

  function closeSettingsModeDropdowns(exceptRoot) {
    document.querySelectorAll("#settings-top [data-settings-mode-root]").forEach((root) => {
      if (exceptRoot && root === exceptRoot) return;
      root.classList.remove("is-open", "is-drop-up");
      root.querySelector(".settings-level-select__trigger")?.setAttribute("aria-expanded", "false");
      const menu = root.querySelector(".settings-level-select__menu");
      if (menu) menu.hidden = true;
    });
  }

  function setSettingsModeValue(subjectId, mode) {
    const normalized = String(subjectId || "").trim();
    const nextMode = String(mode || "").trim().toLowerCase();
    if (!normalized || !nextMode) return;

    const root = document.querySelector(`[data-settings-mode-root="${CSS.escape(normalized)}"]`);
    const select = document.querySelector(`[data-settings-mode-for="${CSS.escape(normalized)}"]`);
    const label = document.querySelector(`[data-settings-mode-label="${CSS.escape(normalized)}"]`);
    if (select) {
      select.value = nextMode;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    }
    if (label) label.textContent = modeLabel(nextMode);
    root?.querySelectorAll("[data-settings-mode-value]").forEach((option) => {
      const active = String(option.dataset.mode || "").toLowerCase() === nextMode;
      option.classList.toggle("is-active", active);
      option.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  function toggleSettingsModeDropdown(trigger) {
    const subjectId = String(trigger?.dataset.settingsModeTrigger || "").trim();
    if (!subjectId) return;
    const root = document.querySelector(`[data-settings-mode-root="${CSS.escape(subjectId)}"]`);
    if (!root) return;
    const menu = root.querySelector(".settings-level-select__menu");
    const open = !root.classList.contains("is-open");
    closeSettingsModeDropdowns(root);
    root.classList.toggle("is-open", open);
    root.classList.remove("is-drop-up");
    trigger.setAttribute("aria-expanded", open ? "true" : "false");
    if (menu) {
      menu.hidden = !open;
      if (open) {
        const rect = trigger.getBoundingClientRect();
        const menuHeight = Math.min(menu.scrollHeight || 240, 260);
        const below = window.innerHeight - rect.bottom;
        const above = rect.top;
        root.classList.toggle("is-drop-up", below < menuHeight + 12 && above > below);
      }
    }
  }

  function closeSettingsProxyTypeDropdown() {
    const root = el("settingsProxyTypeSelect");
    if (!root) return;
    root.classList.remove("is-open", "is-drop-up");
    el("settingsProxyTypeTrigger")?.setAttribute("aria-expanded", "false");
    const menu = el("settingsProxyTypeMenu");
    if (menu) menu.hidden = true;
  }

  function setSettingsProxyType(value) {
    const normalized = String(value || "http").trim().toLowerCase() === "socks5" ? "socks5" : "http";
    const labelText = normalized === "socks5" ? "SOCKS5" : "HTTP CONNECT";
    const input = el("settingsProxyType");
    const label = el("settingsProxyTypeLabel");
    if (input) input.value = normalized;
    if (label) label.textContent = labelText;
    document.querySelectorAll("#settingsProxyTypeMenu [data-proxy-type-value]").forEach((option) => {
      const active = String(option.dataset.proxyTypeValue || "").toLowerCase() === normalized;
      option.classList.toggle("is-active", active);
      option.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  function toggleSettingsProxyTypeDropdown() {
    const root = el("settingsProxyTypeSelect");
    const trigger = el("settingsProxyTypeTrigger");
    const menu = el("settingsProxyTypeMenu");
    if (!root || !trigger || !menu) return;
    const open = !root.classList.contains("is-open");
    closeSettingsModeDropdowns();
    root.classList.toggle("is-open", open);
    root.classList.remove("is-drop-up");
    trigger.setAttribute("aria-expanded", open ? "true" : "false");
    menu.hidden = !open;
    if (open) {
      const rect = trigger.getBoundingClientRect();
      const menuHeight = Math.min(menu.scrollHeight || 120, 180);
      const below = window.innerHeight - rect.bottom;
      const above = rect.top;
      root.classList.toggle("is-drop-up", below < menuHeight + 12 && above > below);
    }
  }

  function chooseSettingsMode(option) {
    const subjectId = String(option?.dataset.settingsModeValue || "").trim();
    const mode = String(option?.dataset.mode || "").trim().toLowerCase();
    if (!subjectId || !mode) return;
    setSettingsModeValue(subjectId, mode);
    closeSettingsModeDropdowns();
    markSettingsClientsDirty();
  }

  async function loadSettingsInventory() {
    const seq = ++settingsInventoryRequestSeq;
    if (settingsInventoryAbortController) {
      settingsInventoryAbortController.abort();
    }
    settingsInventoryAbortController = new AbortController();
    syncSettingsClientTabs();
    setText("settingsClientsState", "загрузка…");

    try {
      const data = await fetchApiV2(
        `/ui/settings/inventory?kind=${encodeURIComponent(settingsClientsTab)}&limit=200`,
        { cache: "no-store", signal: settingsInventoryAbortController.signal }
      );
      if (seq !== settingsInventoryRequestSeq) return;
      settingsInventoryItems = Array.isArray(data.items) ? data.items : [];
      renderSettingsClients();
      clearSettingsClientsDirty();
      setText("settingsClientsState", "");
    } catch (e) {
      if (e?.name === "AbortError") return;
      if (seq !== settingsInventoryRequestSeq) return;
      settingsInventoryItems = [];
      renderSettingsClients();
      setText("settingsClientsState", "error: " + actionMessage(e));
    } finally {
      if (seq === settingsInventoryRequestSeq) {
        settingsInventoryAbortController = null;
      }
    }
  }

  async function loadSettingsWorkspace() {
    try {
      const j = await fetchApiV2("/ui/settings/workspace", { cache: "no-store" });
      settingsWorkspace = j.workspace || {};
      const subscription = settingsWorkspace.subscription || {};
      const backendUrl = normalizeSubscriptionPayload(subscription);

      vpnSubscriptionSavedOnServer = Boolean(subscription.url_saved || backendUrl);
      if (el("vpnSubscriptionUrl")) {
        el("vpnSubscriptionUrl").value = backendUrl || getDevVpnSubscriptionUrl() || "";
      }

      syncVpnSubscriptionHint();
      renderSubscriptionMeta();
      applyDisplaySettings();
      const followUps = [loadSettingsInventory()];
      if (settingsTab === "controls") {
        followUps.push(loadSettingsProxyServers());
      }
      await Promise.allSettled(followUps);
    } catch (e) {
      setText("settingsClientsState", "error: " + e.message);
    }
  }

  async function loadSettingsProxyServers(force) {
    if (settingsServers.length && !force) {
      renderProxyList();
      return;
    }

    try {
      const data = await fetchApiV2("/servers?inventory_state=active&limit=1000", { cache: "no-store" });
      settingsServers = Array.isArray(data.servers) ? data.servers : [];
      renderProxyList();
    } catch (_) {
      settingsServers = [];
      renderProxyList();
    }
  }

  function settingsHasPendingUi() {
    return Boolean(document.querySelector("#settings-top .is-pending-scope, #settings-top .is-pending"));
  }

  async function refreshSettingsOnReturn() {
    if (document.hidden || (document.documentElement.dataset.view || "") !== "settings") return;
    if (settingsAutoRefreshBusy || settingsHasPendingUi()) return;
    const now = Date.now();
    if (now - settingsAutoRefreshLastAt < AUTO_REFRESH_MIN_INTERVAL_MS) return;

    settingsAutoRefreshBusy = true;
    settingsAutoRefreshLastAt = now;
    try {
      await loadSettingsWorkspace();
      if (isJournalTab(settingsTab)) {
        await loadSettingsLogs({ source: settingsTab, silent: true });
      } else if (settingsTab === "rules") {
      await loadRules();
      } else if (settingsTab === "controls") {
        await loadSettingsProxyServers();
      }
    } finally {
      settingsAutoRefreshBusy = false;
    }
  }

  function bindSettingsRefreshOnReturn() {
    window.addEventListener("focus", refreshSettingsOnReturn);
    window.addEventListener("pageshow", refreshSettingsOnReturn);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshSettingsOnReturn();
    });
  }

  function renderProxyList() {
    const wrap = el("settingsProxyList");
    if (!wrap) return;

    const custom = (settingsServers || []).filter((server) => String(server.kind || "") === "custom_https_proxy");
    if (!custom.length) {
      wrap.innerHTML = '<div class="settings-proxy-list__empty">Прокси-серверы пока не добавлены.</div>';
      return;
    }

    wrap.innerHTML = custom.map((server) => {
      const proxy = server.custom_proxy || {};
      const meta = [
        proxy.proxy_type ? String(proxy.proxy_type).toUpperCase() : "",
        proxy.host,
        proxy.port,
        server.preferences?.vpn_auto ? "в авто-списке" : "",
      ].filter(Boolean).join(" · ");

      return `
        <div class="settings-proxy-item">
          <div class="settings-proxy-item__top">
            <div class="settings-proxy-item__name">${escapeHtml(server.server_name || server.server_id || "Прокси")}</div>
            <button class="btn btn--danger" type="button" data-settings-delete-proxy="${escapeHtml(String(server.server_id || ""))}">Удалить</button>
          </div>
          <div class="settings-proxy-item__meta muted">${escapeHtml(meta || "—")}</div>
        </div>
      `;
    }).join("");
  }

  function syncSettingsTabs() {
    document.querySelectorAll("#settingsSourceTabs [data-log-source]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.logSource === settingsTab);
    });

    const eventsWrap = el("adminEventsList");
    const rulesPane = el("settingsRulesPane");
    const controlsPane = el("settingsControlsPane");
    const logActions = el("settingsLogActions");
    const meta = el("settingsWorkspaceMeta");
    const summaryCard = document.querySelector("#settings-top .settings-summary-card");
    const journal = isJournalTab(settingsTab);

    if (eventsWrap) eventsWrap.hidden = !journal;
    if (rulesPane) rulesPane.hidden = settingsTab !== "rules";
    if (controlsPane) controlsPane.hidden = settingsTab !== "controls";
    if (logActions) logActions.hidden = !journal;
    if (summaryCard) summaryCard.hidden = settingsTab === "controls";

    if (meta) {
      if (journal) meta.textContent = `Журнал событий fwrouter: ${categoryLabel(settingsTab)}`;
      else if (settingsTab === "rules") meta.textContent = "Локальные правила маршрутизации и их состояние применяются прямо из этого экрана.";
      else meta.textContent = "VPN-подписка и прокси доступны в этом экране.";
    }

    if (settingsTab === "rules") {
      renderRulesContext(null);
      return;
    }

    if (settingsTab === "controls") {
      return;
    }

    renderSelectedEventContext();

    syncLevelDropdown();
  }

  function applyEventFilters(items) {
    const query = String(searchQuery || "").trim().toLowerCase();
    const level = String(levelFilter || "").trim().toLowerCase();

    return (items || []).filter((item) => {
      if (level && String(item.level || "").toLowerCase() !== level) return false;

      if (!query) return true;

      const haystack = [
        item.ts,
        item.title,
        item.message,
        item.actor,
        item.category,
        item.type,
      ].join("\n").toLowerCase();

      return haystack.includes(query);
    });
  }

  function renderEvents(items) {
    const wrap = el("adminEventsList");
    if (!wrap) return;

    const filtered = applyEventFilters(items);

    if (!Array.isArray(filtered) || !filtered.length) {
      const hasFilters = Boolean(String(searchQuery || "").trim() || String(levelFilter || "").trim());

      wrap.innerHTML = `
        <div class="settings-events__empty muted">
          ${hasFilters ? "По текущим фильтрам ничего не найдено" : "За последние 7 дней событий нет"}
        </div>
      `;

      selectedEventIndex = -1;
      renderSelectedEventContext();
      return;
    }

    if (selectedEventIndex >= 0 && !filtered.includes(loadedEvents[selectedEventIndex])) {
      selectedEventIndex = getEventSourceIndex(filtered[0]);
    }

    if (selectedEventIndex < 0) {
      selectedEventIndex = getEventSourceIndex(filtered[0]);
    }

    wrap.innerHTML = renderEventsHtml(filtered, selectedEventIndex, getEventSourceIndex);

    syncSelectedEventRows();
    renderSelectedEventContext();
  }

  async function loadSettingsLogs(options) {
    const opts = options || {};
    const source = String(opts.source || settingsTab || "all");

    settingsTab = source;
    syncSettingsTabs();

    if (!isJournalTab(source)) {
      setText("adminLogsState", "");
      return;
    }

    if (!opts.silent) setText("adminLogsState", "загрузка…");

    try {
      if (source === "system") {
        const data = await fetchApiV2("/logs/technical?limit=180", { cache: "no-store" });
        loadedEvents = (Array.isArray(data.events) ? data.events : []).map(toLegacyTechnicalEvent);
      } else {
        const data = await fetchApiV2("/logs/operational?limit=180", { cache: "no-store" });
        const allItems = (Array.isArray(data.events) ? data.events : []).map(toLegacyEvent);
        loadedEvents = source === "all" ? allItems : allItems.filter((item) => item.category === source);
      }

      if (selectedEventIndex >= loadedEvents.length) {
        selectedEventIndex = -1;
      }

      renderEvents(loadedEvents);

      setText(
        "settingsWorkspaceMeta",
        `Журнал событий: ${categoryLabel(source)} · хранение ${source === "system" ? 30 : 7} дней`
      );

      setText("adminLogsState", "");
    } catch (e) {
      loadedEvents = [];
      selectedEventIndex = -1;
      renderEvents(loadedEvents);

      const wrap = el("adminEventsList");
      if (wrap) {
        wrap.innerHTML = `<div class="settings-events__empty">Ошибка загрузки событий: ${escapeHtml(e.message)}</div>`;
      }

      renderSelectedEventContext();
      setText("adminLogsState", "error");
    }
  }

  function renderRulesStatus(rules) {
      const state = rules.state || {};
      const metadata = Array.isArray(rules.metadata) ? rules.metadata : [];
      const sources = rules.sources || {};
      const configured = sources.configured || {};
      const lastEffective = sources.last_effective || {};
      const effectiveMeta = metadata.find((item) => String(item.ruleset_type || item.ruleset_id || "") === "effective") || {};
      const bigVpnMeta = metadata.find((item) => String(item.ruleset_type || item.ruleset_id || "") === "big_vpn") || {};
      const effectiveCounts = effectiveMeta.metadata_json?.effective_counts || {};
      const sourceCounts = effectiveMeta.metadata_json?.source_counts || {};
      const configuredVpn = Array.isArray(configured.big_vpn) ? configured.big_vpn : [];
      const sourceLabel = configuredVpn.some((url) => String(url || "").includes("Re-filter"))
        ? "Re-filter"
        : (configuredVpn.length ? "VPN-список" : "не задан");
      const apply = {
        pending: ["running", "pending", "applying"].includes(String(state.status || "").toLowerCase()),
        done: Boolean(state.last_apply_job_id || state.last_update_job_id),
        done_at: toUnixSeconds(state.updated_at || state.last_success_at),
      };

      const statusLabel = {
        success: "готово",
        clean: "актуально",
        idle: "ожидает",
        running: "обновляется…",
        pending: "ожидает применения",
        applying: "применяется…",
        failed: "ошибка",
        not_configured: "не настроено",
      }[String(state.status || "").toLowerCase()] || String(state.status || "неизвестно");
      const totalCount = Number(effectiveCounts.total || 0);
      const vpnCount = Number(effectiveCounts.vpn || sourceCounts.big_vpn || 0);
      const detailParts = [];

      if (totalCount) detailParts.push(`${totalCount.toLocaleString("ru-RU")} правил`);
      else if (vpnCount) detailParts.push(`${vpnCount.toLocaleString("ru-RU")} VPN правил`);
      if (bigVpnMeta.last_error_message) detailParts.push(`последняя ошибка: ${bigVpnMeta.last_error_message}`);
      else if (state.error_message) detailParts.push(state.error_message);
      if (!detailParts.length && lastEffective.fetch_summary && Object.keys(lastEffective.fetch_summary).length) {
        detailParts.push("есть metadata последней сборки");
      }

      const detail = detailParts.join(" · ");

      const status = {
        state: {
          tag: sourceLabel,
          detail: detail || statusLabel,
          last_success_at: toUnixSeconds(state.last_success_at),
        },
        apply,
      };

      if (settingsTab === "rules") {
        renderRulesContext(status);
      }

      return status;
  }

  async function loadRules() {
    setText("rulesState", "");

    try {
      const j = await fetchApiV2("/rules/summary", { cache: "no-store" });
      const rules = j.rules || {};

      if (el("rulesText")) {
        el("rulesText").value = String(rules?.manual?.draft_text || rules?.manual?.active_text || "");
      }

      renderRulesStatus(rules);
      setText("rulesState", "");
    } catch (e) {
      setText("rulesState", "error: " + e.message);
    }
  }

  async function loadRulesUpstreamStatus() {
    try {
      const data = await fetchApiV2("/rules/summary", { cache: "no-store" });
      return renderRulesStatus(data.rules || {});
    } catch (e) {
      setText("rulesState", "status error: " + e.message);

      return null;
    }
  }

  async function refreshRules(mode) {
    setText("rulesState", "");

    try {
      await fetchApiV2("/rules/manual/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          requested_by: "ui",
          run_now: true,
        }),
      });

      setText("rulesState", "ok");
      await loadRules();
    } catch (e) {
      setText("rulesState", "error: " + e.message);
    }
  }

  async function updateAllRules() {
    setText("rulesState", "refresh…");

    try {
      const j = await fetchApiV2("/rules/full-update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          requested_by: "ui",
          run_now: true,
        }),
      });
      const changed = Boolean((j.job || {}).result?.changed ?? j.changed);
      const stage = String((j.job || {}).result?.stage || j.stage || "");

      await loadRules();
      await loadSettingsWorkspace();

      if (stage === "noop" || !changed) {
        setText("rulesState", "already current");
        return;
      }

      setText("rulesState", "updated");
    } catch (e) {
      setText("rulesState", "error: " + actionMessage(e));
    }
  }

  async function saveRules() {
    setText("rulesState", "");

    try {
      await fetchApiV2("/rules/manual", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: el("rulesText")?.value || "" }),
      });

      setText("rulesState", "");
      await loadRulesUpstreamStatus();
    } catch (e) {
      setText("rulesState", "error: " + e.message);
    }
  }

  async function saveVpnSubscriptionUrl() {
    const input = el("vpnSubscriptionUrl");
    if (!input) return;

    const url = String(input.value || "").trim();

    setText("vpnSubscriptionState", "сохранение…");

    try {
      const data = await fetchApiV2("/subscription", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url,
        }),
      });

      setDevVpnSubscriptionUrl("");
      vpnSubscriptionSavedOnServer = Boolean(data?.subscription?.url_saved || url);
      setText("vpnSubscriptionState", "готово");
      syncVpnSubscriptionHint();
      await loadSettingsWorkspace();
    } catch (_) {
      setDevVpnSubscriptionUrl(url);
      vpnSubscriptionSavedOnServer = Boolean(url);
      setText("vpnSubscriptionState", "локально");
      syncVpnSubscriptionHint();
    }
  }

  async function refreshVpnSubscription() {
    setText("vpnSubscriptionState", "обновление…");

    try {
      await fetchApiV2("/subscription/refresh", { method: "POST" });
      setText("vpnSubscriptionState", "готово");
      await loadSettingsWorkspace();
    } catch (e) {
      setText("vpnSubscriptionState", "ошибка: " + e.message);
    }
  }

  async function createSettingsProxy() {
    const payload = {
      server_name: String(el("settingsProxyName")?.value || "").trim(),
      proxy_type: String(el("settingsProxyType")?.value || "http").trim(),
      host: String(el("settingsProxyHost")?.value || "").trim(),
      port: Number(el("settingsProxyPort")?.value || 0),
      username: String(el("settingsProxyUsername")?.value || "").trim() || null,
      password: String(el("settingsProxyPassword")?.value || "").trim() || null,
      requested_by: "ui",
      tls: true,
      global_list: true,
      vpn_auto: true,
    };

    setText("settingsProxyState", "сохранение…");

    try {
      await fetchApiV2("/servers/custom/proxy", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setText("settingsProxyState", "готово");
      ["settingsProxyName", "settingsProxyHost", "settingsProxyPort", "settingsProxyUsername", "settingsProxyPassword"]
        .forEach((id) => {
          const node = el(id);
          if (node) node.value = "";
        });
      setSettingsProxyType("http");
      await loadSettingsProxyServers(true);
    } catch (e) {
      setText("settingsProxyState", "ошибка: " + actionMessage(e));
    }
  }

  async function deleteSettingsProxy(serverId) {
    const normalized = String(serverId || "").trim();
    if (!normalized) return;

    setText("settingsProxyState", "удаление…");

    try {
      await fetchApiV2(`/servers/custom/proxy/${encodeURIComponent(normalized)}?requested_by=ui`, {
        method: "DELETE",
      });
      await loadSettingsProxyServers(true);
      setText("settingsProxyState", "готово");
    } catch (e) {
      setText("settingsProxyState", "ошибка: " + actionMessage(e));
    }
  }

  async function saveSettingsItem(subjectId, forcedMode, triggerNode) {
    const normalized = String(subjectId || "").trim();
    if (!normalized) return;

    const items = Array.isArray(settingsInventoryItems) ? settingsInventoryItems : [];
    const client = items.find((item) => String(item.subject_id || "") === normalized);
    if (!client) return;

    const aliasInput = document.querySelector(`[data-settings-alias-for="${CSS.escape(normalized)}"]`);
    const modeSelect = document.querySelector(`[data-settings-mode-for="${CSS.escape(normalized)}"]`);
    const powerToggle = document.querySelector(`[data-settings-power-toggle="${CSS.escape(normalized)}"]`);
    const trafficChoiceButtons = Array.from(document.querySelectorAll(`[data-settings-traffic-choice="${CSS.escape(normalized)}"]`));
    const saveButton = document.querySelector(`[data-settings-save-item="${CSS.escape(normalized)}"]`);
    const quickButtons = Array.from(document.querySelectorAll(`[data-settings-quick-mode="${CSS.escape(normalized)}"]`));
    const alias = aliasInput ? String(aliasInput.value || "").trim() : "";
    const mode = String(forcedMode || (modeSelect ? modeSelect.value : "") || "").trim().toLowerCase();
    const selectedTraffic = trafficChoiceButtons
      .filter((button) => button.classList.contains("is-selected"))
      .map((button) => String(button.dataset.metric || "").trim())
      .filter((metric) => TRAFFIC_METRIC_KEYS.includes(metric));

    if (selectedTraffic.length !== 2) {
      setText("settingsClientsState", "error: выбери 2 показателя трафика");
      return;
    }

    setText("settingsClientsState", "сохранение…");
    clearSettingsClientsDirty();
    setPendingStateMany([
      aliasInput,
      modeSelect,
      powerToggle,
      ...trafficChoiceButtons,
      saveButton,
      triggerNode,
      ...quickButtons,
    ], true);
    setPendingScope(triggerNode || saveButton || modeSelect || aliasInput, true);

    try {
      if (String(client.kind || "") === "xray") {
        const clientId = String(client.client_id || client.client_uuid || "").trim();
        if (clientId) {
          await fetchApiV2(`/xray/clients/${encodeURIComponent(clientId)}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              alias: alias || null,
              requested_by: "ui",
            }),
          });
        }
      } else {
        await fetchApiV2(`/subjects/${encodeURIComponent(normalized)}/alias`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ alias: alias || null }),
        });
      }

      const modeAction = await fetchApiV2(`/subjects/${encodeURIComponent(normalized)}/mode`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode,
          actor_scope: "admin",
          requested_by: "ui",
          run_now: false,
        }),
      });
      const jobId = String(modeAction?.job?.job_id || "").trim();
      if (jobId) {
        await pollJob(jobId, {
          onProgress(status) {
            setText("settingsClientsState", status === "queued" ? "в очереди…" : "применение…");
          },
        });
      }

      settingsTrafficPreferences[normalized] = selectedTraffic;

      const payload = getSettingsDisplayPayload();
      const j = await fetchApiV2("/ui/settings/display", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      settingsWorkspace = settingsWorkspace || {};
      settingsWorkspace.display_settings = j.display_settings || payload;
      settingsTrafficPreferences = normalizeTrafficPreferences(settingsWorkspace.display_settings.subject_traffic_preferences);
      document.dispatchEvent(new CustomEvent("fwrouter:display-settings-updated", {
        detail: { display_settings: settingsWorkspace.display_settings },
      }));
      await loadSettingsWorkspace();
      clearSettingsClientsDirty();
      setText("settingsClientsState", "ok");
      const freshRow = getSettingsClientRow(normalized);
      const freshModeSelect = document.querySelector(`[data-settings-mode-for="${CSS.escape(normalized)}"]`);
      const freshSaveButton = document.querySelector(`[data-settings-save-item="${CSS.escape(normalized)}"]`);
      flashScopeResult(freshRow || freshSaveButton || freshModeSelect || triggerNode || saveButton || modeSelect || aliasInput, "success");
    } catch (e) {
      setText("settingsClientsState", "error: " + actionMessage(e));
      flashScopeResult(triggerNode || saveButton || modeSelect || aliasInput, "error");
    } finally {
      setPendingStateMany([
      aliasInput,
      modeSelect,
      powerToggle,
      ...trafficChoiceButtons,
      saveButton,
      triggerNode,
      ...quickButtons,
      ], false);
      setPendingScope(triggerNode || saveButton || modeSelect || aliasInput, false);
    }
  }

  async function deleteSettingsXray(clientId) {
    const normalized = String(clientId || "").trim();
    if (!normalized) return;

    setText("settingsClientsState", "удаление…");

    try {
      await fetchApiV2(`/xray/clients/${encodeURIComponent(normalized)}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ requested_by: "ui" }),
      });
      await loadSettingsWorkspace();
      setText("settingsClientsState", "ok");
    } catch (e) {
      setText("settingsClientsState", "error: " + actionMessage(e));
    }
  }

  async function deleteSettingsSystemSubject(subjectId) {
    const normalized = String(subjectId || "").trim();
    if (!normalized) return;

    setText("settingsClientsState", "удаление…");

    try {
      await fetchApiV2(`/system-subjects/${encodeURIComponent(normalized)}?requested_by=ui`, {
        method: "DELETE",
      });
      await loadSettingsWorkspace();
      setText("settingsClientsState", "ok");
    } catch (e) {
      setText("settingsClientsState", "error: " + actionMessage(e));
    }
  }

  function toggleSettingsTrafficChoice(button) {
    if (!button) return;
    const subjectId = String(button.dataset.settingsTrafficChoice || "").trim();
    if (!subjectId) return;

    const row = getSettingsClientRow(subjectId);
    const buttons = Array.from(row?.querySelectorAll(`[data-settings-traffic-choice="${CSS.escape(subjectId)}"]`) || []);
    const selected = buttons.filter((item) => item.classList.contains("is-selected"));
    const isSelected = button.classList.contains("is-selected");

    if (!isSelected && selected.length >= 2) {
      button.classList.add("is-rejected");
      window.setTimeout(() => button.classList.remove("is-rejected"), 220);
      setText("settingsClientsState", "для админ-панели можно выбрать только 2 показателя");
      return;
    }

    button.classList.toggle("is-selected", !isSelected);
    button.setAttribute("aria-pressed", !isSelected ? "true" : "false");

    const nextSelected = buttons
      .filter((item) => item.classList.contains("is-selected"))
      .map((item) => String(item.dataset.metric || "").trim())
      .filter((metric) => TRAFFIC_METRIC_KEYS.includes(metric));
    settingsTrafficPreferences[subjectId] = nextSelected;
    setText("settingsClientsState", nextSelected.length === 2 ? "" : "выбери 2 показателя трафика");
  }

  function markSettingsClientsDirty() {
    const state = el("settingsClientsState");
    if (!state) return;
    state.textContent = "Не сохранено";
    state.classList.add("is-unsaved");
  }

  function markSettingsRowDirty(subjectId) {
    const normalized = String(subjectId || "").trim();
    if (!normalized) return;
    const row = getSettingsClientRow(normalized);
    row?.classList.add("is-local-dirty");
    row?.querySelector(`[data-settings-save-item="${CSS.escape(normalized)}"]`)?.classList.add("is-attention");
  }

  function clearSettingsClientsDirty() {
    el("settingsClientsState")?.classList.remove("is-unsaved");
  }

  function toggleSettingsPower(button) {
    if (!button) return;
    const subjectId = String(button.dataset.settingsPowerToggle || "").trim();
    if (!subjectId) return;

    const modeSelect = document.querySelector(`[data-settings-mode-for="${CSS.escape(subjectId)}"]`);
    const enabled = String(button.dataset.enabled || "1") !== "0";
    const nextEnabled = !enabled;
    const restoreMode = String(button.dataset.restoreMode || "").trim().toLowerCase() || "global";
    const nextMode = nextEnabled ? restoreMode : "disabled";

    if (modeSelect) {
      if (!nextEnabled && modeSelect.value !== "disabled") {
        button.dataset.restoreMode = String(modeSelect.value || restoreMode).toLowerCase();
      }
      setSettingsModeValue(subjectId, nextMode);
    }

    button.dataset.enabled = nextEnabled ? "1" : "0";
    button.setAttribute("aria-pressed", nextEnabled ? "true" : "false");
    button.classList.toggle("is-on", nextEnabled);
    button.classList.toggle("is-off", !nextEnabled);
    button.textContent = nextEnabled ? "Включен" : "Выключен";
    markSettingsClientsDirty();
    markSettingsRowDirty(subjectId);
  }

  async function toggleSettingsAdminVisibility(button) {
    if (!button) return;
    const subjectId = String(button.dataset.settingsAdminVisibility || "").trim();
    if (!subjectId) return;

    const nextHidden = !settingsHiddenSubjectIds.has(subjectId);
    if (nextHidden) {
      settingsHiddenSubjectIds.add(subjectId);
    } else {
      settingsHiddenSubjectIds.delete(subjectId);
    }

    renderSettingsClients();
    const row = getSettingsClientRow(subjectId);
    setPendingScope(row || button, true);
    try {
      const payload = getSettingsDisplayPayload();
      const j = await fetchApiV2("/ui/settings/display", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      settingsWorkspace = settingsWorkspace || {};
      settingsWorkspace.display_settings = j.display_settings || payload;
      settingsHiddenSubjectIds = new Set(
        Array.isArray(settingsWorkspace.display_settings.hidden_subject_ids)
          ? settingsWorkspace.display_settings.hidden_subject_ids.map((item) => String(item || "").trim()).filter(Boolean)
          : []
      );
      applyDisplaySettings();
      renderSettingsClients();
      document.dispatchEvent(new CustomEvent("fwrouter:display-settings-updated", {
        detail: { display_settings: settingsWorkspace.display_settings },
      }));
      flashScopeResult(getSettingsClientRow(subjectId) || row || button, "success");
    } catch (e) {
      if (nextHidden) {
        settingsHiddenSubjectIds.delete(subjectId);
      } else {
        settingsHiddenSubjectIds.add(subjectId);
      }
      renderSettingsClients();
      setText("settingsClientsState", "error: " + e.message);
      flashScopeResult(getSettingsClientRow(subjectId) || row || button, "error");
    } finally {
      setPendingScope(getSettingsClientRow(subjectId) || row || button, false);
    }
  }

  function wire() {
    if (settingsBootstrapped) return;
    settingsBootstrapped = true;

    el("adminLogsRefresh")?.addEventListener("click", () => {
      loadSettingsLogs({ source: settingsTab });
    });

    el("adminEventsSearch")?.addEventListener("input", (ev) => {
      searchQuery = ev.target.value || "";
      clearTimeout(settingsLogSearchTimer);
      settingsLogSearchTimer = window.setTimeout(() => {
        renderEvents(loadedEvents);
      }, 120);
    });

    el("adminEventsLevelTrigger")?.addEventListener("click", () => {
      const root = el("adminEventsLevel");
      if (!root) return;

      root.classList.toggle("is-open");
      syncLevelDropdown();
    });

    el("adminEventsLevelMenu")?.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-level-value]");
      if (!btn) return;

      levelFilter = btn.dataset.levelValue || "";

      const root = el("adminEventsLevel");
      if (root) root.classList.remove("is-open");

      syncLevelDropdown();
      renderEvents(loadedEvents);
    });

    document.addEventListener("click", (ev) => {
      const root = el("adminEventsLevel");
      if (!root) return;

      if (ev.target.closest("#adminEventsLevel")) return;

      root.classList.remove("is-open");
      syncLevelDropdown();
    });

    document.addEventListener("click", (ev) => {
      const toggle = ev.target.closest("#settings-top [data-event-toggle]");
      if (!toggle) return;

      const row = toggle.closest("[data-event-row]");
      if (!row) return;

      const idx = Number(row.dataset.eventRow);
      if (!Number.isFinite(idx)) return;

      selectSettingsEvent(idx);
    });

    document.addEventListener("keydown", (ev) => {
      const toggle = ev.target.closest?.("#settings-top [data-event-toggle]");
      if (!toggle) return;

      if (ev.key !== "Enter" && ev.key !== " ") return;

      ev.preventDefault();

      const row = toggle.closest("[data-event-row]");
      if (!row) return;

      const idx = Number(row.dataset.eventRow);
      if (!Number.isFinite(idx)) return;

      selectSettingsEvent(idx);
    });

    document.querySelectorAll("#settingsSourceTabs [data-log-source]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const source = btn.dataset.logSource || "all";
        if (source === "rules") {
          settingsTab = source;
          syncSettingsTabs();
          loadRules();
          return;
        }

        if (source === "controls") {
          settingsTab = source;
          syncSettingsTabs();
          loadSettingsProxyServers();
          return;
        }

        loadSettingsLogs({ source });
      });
    });

    el("rulesRefresh")?.addEventListener("click", () => refreshRules("rules"));
    el("rulesRefreshAll")?.addEventListener("click", updateAllRules);
    el("rulesSave")?.addEventListener("click", saveRules);

    el("vpnSubscriptionSave")?.addEventListener("click", saveVpnSubscriptionUrl);
    el("vpnSubscriptionRefresh")?.addEventListener("click", refreshVpnSubscription);
    el("settingsProxyCreate")?.addEventListener("click", createSettingsProxy);
    el("settingsClientsRefresh")?.addEventListener("click", loadSettingsWorkspace);

    [["settingsClientsTabAll", "all"], ["settingsClientsTabLan", "lan"], ["settingsClientsTabTs", "tailscale"], ["settingsClientsTabXray", "xray"], ["settingsClientsTabDocker", "docker"], ["settingsClientsTabHost", "host"]]
      .forEach(([id, value]) => {
        el(id)?.addEventListener("click", () => {
          if (settingsClientsTab === value) return;
          settingsClientsTab = value;
          syncSettingsClientTabs();
          loadSettingsInventory();
        });
      });

    el("vpnSubscriptionUrl")?.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter") return;

      ev.preventDefault();
      saveVpnSubscriptionUrl();
    });

    el("vpnSubscriptionUrl")?.addEventListener("input", syncVpnSubscriptionHint);

    document.addEventListener("click", (ev) => {
      const trafficChoice = ev.target.closest("[data-settings-traffic-choice]");
      if (trafficChoice) {
        toggleSettingsTrafficChoice(trafficChoice);
        return;
      }

      const powerToggle = ev.target.closest("[data-settings-power-toggle]");
      if (powerToggle) {
        toggleSettingsPower(powerToggle);
        return;
      }

      const adminVisibilityToggle = ev.target.closest("[data-settings-admin-visibility]");
      if (adminVisibilityToggle) {
        toggleSettingsAdminVisibility(adminVisibilityToggle);
        return;
      }

      const modeTrigger = ev.target.closest("[data-settings-mode-trigger]");
      if (modeTrigger) {
        toggleSettingsModeDropdown(modeTrigger);
        return;
      }

      const modeOption = ev.target.closest("[data-settings-mode-value]");
      if (modeOption) {
        chooseSettingsMode(modeOption);
        return;
      }

      const saveBtn = ev.target.closest("[data-settings-save-item]");
      if (saveBtn) {
        const subjectId = saveBtn.dataset.settingsSaveItem || "";
        if (subjectId) saveSettingsItem(subjectId, undefined, saveBtn);
        return;
      }

      const quickModeBtn = ev.target.closest("[data-settings-quick-mode]");
      if (quickModeBtn) {
        const subjectId = quickModeBtn.dataset.settingsQuickMode || "";
        const mode = quickModeBtn.dataset.mode || "";
        if (subjectId && mode) saveSettingsItem(subjectId, mode, quickModeBtn);
        return;
      }

      const deleteBtn = ev.target.closest("[data-settings-delete-kind]");
      if (deleteBtn) {
        const kind = deleteBtn.dataset.settingsDeleteKind || "";
        const id = deleteBtn.dataset.settingsDeleteId || "";
        if (kind === "xray" && id) deleteSettingsXray(id);
        if (kind === "system" && id) deleteSettingsSystemSubject(id);
        return;
      }

      const deleteProxyBtn = ev.target.closest("[data-settings-delete-proxy]");
      if (deleteProxyBtn) {
        const serverId = deleteProxyBtn.dataset.settingsDeleteProxy || "";
        if (serverId) deleteSettingsProxy(serverId);
        return;
      }

      if (ev.target.closest("#settingsProxyTypeTrigger")) {
        toggleSettingsProxyTypeDropdown();
        return;
      }

      const proxyTypeOption = ev.target.closest("[data-proxy-type-value]");
      if (proxyTypeOption) {
        setSettingsProxyType(proxyTypeOption.dataset.proxyTypeValue || "http");
        closeSettingsProxyTypeDropdown();
        return;
      }

      if (!ev.target.closest("#settings-top [data-settings-mode-root]")) {
        closeSettingsModeDropdowns();
      }
      if (!ev.target.closest("#settingsProxyTypeSelect")) {
        closeSettingsProxyTypeDropdown();
      }
    });

    renderSelectedEventContext();
    syncSettingsTabs();
    loadSettingsWorkspace();
    if (isJournalTab(settingsTab)) {
      loadSettingsLogs({ source: settingsTab });
    }
    bindSettingsRefreshOnReturn();
  }

  window.addEventListener("DOMContentLoaded", () => {
    if ((document.documentElement.dataset.view || "user") === "settings") {
      wire();
    }
  });

  document.addEventListener("fwrouter:view", (event) => {
    const view = event && event.detail ? event.detail.view : "";
    if (view === "settings") wire();
  });
})();
