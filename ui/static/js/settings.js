// settings.js — settings panel: admin event journal + routing rules
(function () {
  const el = (id) => document.getElementById(id);
  const t = (key, params) => window.FwrouterI18n?.t(key, params) || key;
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
  let settingsSystemVisibility = {};
  let settingsInventoryRequestSeq = 0;
  let settingsInventoryAbortController = null;
  let settingsLogSearchTimer = null;
  let settingsAutoRefreshBusy = false;
  let settingsAutoRefreshLastAt = 0;
  let lastRulesValidationMessage = "";

  const DEV_VPN_SUBSCRIPTION_URL_KEY = "fwrouter.dev.vpnSubscriptionUrl";
  const {
    fetchJson,
    fetchApiV2,
    actionMessage,
    pollJob,
    escapeHtml,
    setText,
    setDynamicStatus,
    clearDynamicStatus,
    setPendingState,
    setPendingStateMany,
    createPendingHelpers,
    translateBackendMessage,
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
    toTypedEvent,
    toUnixSeconds,
    isJournalTab,
    matchesJournalTab,
    groupRepeatedEvents,
  } = window.FwrouterSettingsEvents;
  const {
    settingsModeLabel: modeLabel,
    subjectDomainCategory,
  } = window.FwrouterLabels;
  const {
    TRAFFIC_METRIC_KEYS,
    normalizeTrafficPreferences,
    renderSettingsClientsHtml,
    renderSettingsCounts,
    settingsClientActionAdapter,
  } = window.FwrouterSettingsInventory;
  const {
    renderSelectedEventContextHtml,
    renderRulesContextHtml,
    renderEventsHtml,
  } = window.FwrouterSettingsJournal;
  const {
    renderRoutingPolicyHtml,
    renderDiagnosticsHtml,
  } = window.FwrouterSettingsDomainState;

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

    const text = levelFilter ? levelLabel(levelFilter) : t("settings.level.all");

    label.textContent = text;
    trigger.setAttribute("aria-expanded", root.classList.contains("is-open") ? "true" : "false");
    menu.hidden = !root.classList.contains("is-open");

    menu.querySelectorAll("[data-level-value]").forEach((btn) => {
      btn.classList.toggle("is-active", (btn.dataset.levelValue || "") === levelFilter);
    });
  }

  function getEventSourceIndex(item) {
    const explicit = Number(item?.source_index);
    if (Number.isFinite(explicit) && explicit >= 0) return explicit;
    const idx = loadedEvents.indexOf(item);
    return idx >= 0 ? idx : -1;
  }

  function renderSelectedEventContext() {
    const card = document.querySelector("#settings-top .settings-summary-card");
    const title = document.querySelector("#settings-top .settings-summary-card .panel__head .label");
    const body = document.querySelector("#settings-top .settings-summary-card__body");

    if (!card || !body) return;

    if (title) {
      title.textContent = t("settings.details.event");
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
      title.textContent = t("settings.details.rules");
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
      hint.textContent = t("settings.subscription.active");
      return;
    }

    hint.textContent = vpnSubscriptionSavedOnServer ? t("settings.subscription.saved") : t("settings.subscription.not_set");
  }

  function setCheckbox(id, value) {
    const node = el(id);
    if (!node) return;
    node.checked = Boolean(value);
  }

  function slugifySystemId(value) {
    return String(value || "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9а-яё_]+/gi, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 64);
  }

  function systemVisibilityFromSettings(settings) {
    const result = {
      local_client: true,
      external_client: true,
      service: true,
      infrastructure: true,
      lan: true,
      external_network_source: true,
      vless_client: true,
      vpn_runtime: true,
      docker: true,
      host: true,
    };
    const source = settings && typeof settings.system_visibility === "object"
      ? settings.system_visibility
      : {};
    Object.entries(source).forEach(([key, value]) => {
      const systemId = slugifySystemId(key);
      if (systemId) result[systemId] = Boolean(value);
    });
    return result;
  }

  function systemVisible(systemId, settings) {
    const visibility = Object.keys(settingsSystemVisibility).length
      ? settingsSystemVisibility
      : systemVisibilityFromSettings(settings || {});
    const normalized = slugifySystemId(systemId);
    return normalized ? visibility[normalized] !== false : true;
  }

  function renderSettingsConnections() {
    const wrap = el("settingsClientsWrap");
    if (!wrap) return;
    const systems = Array.isArray(settingsWorkspace?.display_systems)
      ? settingsWorkspace.display_systems.filter((system) => system?.show_in_connections !== false)
      : [];
    syncSettingsClientTabs();
    if (!systems.length) {
      wrap.innerHTML = `
        <div class="settings-events__empty muted">
          <button class="btn btn--secondary" type="button" data-settings-add-external>${escapeHtml(t("settings.connections.add"))}</button>
        </div>
      `;
      return;
    }
    const kindLabel = {
      core: "core",
      managed: "managed",
      external: "external",
      inventory: "inventory",
    };
    wrap.innerHTML = `
      <div class="settings-connections-toolbar">
        <button class="btn btn--secondary settings-connections-add" type="button" data-settings-add-external>${escapeHtml(t("settings.connections.add"))}</button>
      </div>
      <div class="settings-systems__list">
        ${systems.map((system) => {
      const systemKey = settingsConnectionKey(system);
      const visible = systemVisible(systemKey, settingsWorkspace?.display_settings);
      const count = Number(system.count || 0);
      const custom = Boolean(system.custom);
      const kind = String(system.kind || system.lifecycle_mode || "external").toLowerCase();
      const connectionType = String(system.connection_type || "").toLowerCase();
      const description = settingsSystemDescription(system);
      const readiness = system.readiness && typeof system.readiness === "object" ? system.readiness : null;
      const readinessDetails = readiness && readiness.details && typeof readiness.details === "object" ? readiness.details : {};
      const missingFields = readiness && Array.isArray(readiness.missing_fields) ? readiness.missing_fields : [];
      const infoItems = [
        system.connection_id ? ["connection_id", system.connection_id] : null,
        system.integration_mode ? [t("settings.connections.info.integration"), integrationModeLabel(system.integration_mode)] : null,
        system.refresh_mode ? [t("settings.connections.info.refresh"), refreshModeLabel(system.refresh_mode)] : null,
        system.replacement_target ? [t("settings.connections.info.replaces"), replacementTargetLabel(system.replacement_target)] : null,
        system.runtime_type ? ["Runtime", system.runtime_type] : null,
        system.location ? [t("settings.connections.info.location"), connectionLocationLabel(system.location)] : null,
        system.address ? [t("settings.connections.info.address"), system.address] : null,
        readiness?.state ? [t("settings.connections.info.readiness"), readinessLabel(readiness.state)] : null,
        readinessDetails.active_as_runtime_adapter ? ["Runtime adapter", t("runtime.active")] : null,
        readinessDetails.runtime_adapter_role ? [t("settings.connections.info.role"), readinessDetails.runtime_adapter_role] : null,
        readinessDetails.tcp_redir_port_present === false ? ["TCP redir", t("settings.connections.not_set")] : null,
        readinessDetails.udp_tproxy_port_present === false ? ["UDP TProxy", t("settings.connections.not_set")] : null,
        missingFields.length ? [t("settings.connections.info.missing"), missingFields.join(", ")] : null,
        system.last_seen_at ? [t("settings.connections.info.last_seen"), formatTs(system.last_seen_at)] : null,
        system.last_action ? [t("settings.connections.info.action"), system.last_action] : null,
        system.channel ? [t("settings.connections.info.channel"), system.channel] : null,
      ].filter(Boolean);
      return `
        <div
          class="settings-client-row settings-system-row${visible ? " is-visible" : " is-hidden"}"
          data-settings-system-row="${escapeHtml(systemKey)}"
          data-settings-system-open="${escapeHtml(systemKey)}"
          tabindex="0"
          role="button"
          title="${escapeHtml(t("settings.connections.open_details"))}"
        >
          <div class="settings-system-row__main">
            <div class="settings-system-row__title">${escapeHtml(settingsSystemLabel(system))}</div>
            <div class="settings-system-row__meta muted">${escapeHtml(description || t("settings.connections.display_meta"))}</div>
            ${infoItems.length ? `
              <div class="settings-system-row__info">
                ${infoItems.map(([label, value]) => `
                  <div class="settings-system-row__info-item">
                    <span>${escapeHtml(label)}</span>
                    <strong>${escapeHtml(value || "—")}</strong>
                  </div>
                `).join("")}
              </div>
            ` : ""}
          </div>
          <div class="settings-system-row__badges">
            <span class="pill settings-system-row__kind">${escapeHtml(kindLabel[kind] || kind)}</span>
            ${connectionType ? `<span class="pill settings-system-row__kind">${escapeHtml(connectionTypeLabel(connectionType))}</span>` : ""}
            ${count ? `<span class="pill mono">${escapeHtml(String(count))}</span>` : ""}
            <button
              class="pill settings-system-row__toggle${visible ? " is-shown" : " is-hidden"}"
              type="button"
              data-settings-system-toggle="${escapeHtml(systemKey)}"
              aria-pressed="${visible ? "true" : "false"}"
              title="${escapeHtml(t("settings.connections.show_title"))}"
            >${escapeHtml(visible ? t("settings.connections.show") : t("settings.connections.hidden"))}</button>
            ${custom ? `
              <button
                class="pill settings-system-row__delete"
                type="button"
                data-settings-system-delete="${escapeHtml(systemKey)}"
                title="${escapeHtml(t("settings.connections.delete_title"))}"
              >${escapeHtml(t("inventory.delete"))}</button>
            ` : ""}
          </div>
        </div>
      `;
        }).join("")}
      </div>
    `;
  }

  function settingsConnectionById(systemId) {
    const normalized = slugifySystemId(systemId);
    const systems = Array.isArray(settingsWorkspace?.display_systems)
      ? settingsWorkspace.display_systems
      : [];
    return systems.find((item) => settingsConnectionKey(item) === normalized) || null;
  }

  function settingsConnectionKey(system) {
    if (system?.custom) return slugifySystemId(system?.connection_id);
    return slugifySystemId(system?.system_id || system?.id);
  }

  function settingsSystemI18nKey(system, field) {
    const systemId = String(system?.system_id || "").trim();
    const builtinIds = new Set(["lan", "external_network_source", "vless_client", "vpn_runtime", "docker", "host"]);
    if (!builtinIds.has(systemId)) return "";
    return `display.system.${field}.${systemId}`;
  }

  function settingsSystemLabel(system) {
    const key = settingsSystemI18nKey(system, "title");
    if (key) {
      const label = t(key);
      if (label !== key) return label;
    }
    return String(system?.label || system?.system_id || "").trim();
  }

  function settingsSystemDescription(system) {
    const key = settingsSystemI18nKey(system, "description");
    if (key) {
      const description = t(key);
      if (description !== key) return description;
    }
    return String(system?.description || system?.status_text || "").trim();
  }

  function connectionTypeLabel(value) {
    const raw = String(value || "").toLowerCase();
    if (raw === "external_management") return t("settings.connections.type.external_management");
    if (raw === "external_vpn_module") return t("settings.connections.type.external_vpn_module");
    if (raw === "external_network_source" || raw === "external_network") return t("settings.connections.type.external_network_source");
    if (raw === "display_only") return t("settings.connections.type.display_only");
    return raw || "external";
  }

  function integrationModeLabel(value) {
    const raw = String(value || "").toLowerCase();
    if (raw === "api_push") return t("settings.connections.integration.api_push");
    if (raw === "http_poll") return t("settings.connections.integration.http_poll");
    if (raw === "command_probe") return t("settings.connections.integration.command_probe");
    if (raw === "file_read") return t("settings.connections.integration.file_read");
    return raw || "—";
  }

  function refreshModeLabel(value) {
    const raw = String(value || "").toLowerCase();
    if (raw === "on_change") return t("settings.connections.refresh.on_change");
    if (raw === "manual") return t("settings.connections.refresh.manual");
    if (raw === "interval") return t("settings.connections.refresh.interval");
    return raw || "—";
  }

  function readinessLabel(value) {
    const raw = String(value || "").toLowerCase();
    if (raw === "active") return t("settings.connections.readiness.active");
    if (raw === "ready") return t("settings.connections.readiness.ready");
    if (raw === "seen") return t("settings.connections.readiness.seen");
    if (raw === "incomplete") return t("settings.connections.readiness.incomplete");
    return raw || "—";
  }

  function replacementTargetLabel(value) {
    const raw = String(value || "").toLowerCase();
    if (raw === "mihomo") return t("settings.connections.replacement_vpn_dataplane");
    if (raw === "xray") return t("settings.connections.replacement_external_client");
    return raw || "—";
  }

  function connectionLocationLabel(value) {
    const raw = String(value || "").toLowerCase();
    if (raw === "docker") return "Docker";
    if (raw === "host") return "Host";
    if (raw === "ip") return "IP / hostname";
    return "Manual";
  }

  function renderExternalConnectionGuide(system) {
    const guideJson = connectionGuideJson(system);
    return `
      <div class="settings-system-guide">
        <div class="settings-system-guide__head">
          <strong>${escapeHtml(t("settings.connections.json_title"))}</strong>
          <button class="settings-system-guide__copy" type="button" data-settings-copy-guide="${escapeHtml(settingsConnectionKey(system))}">${escapeHtml(t("settings.connections.copy"))}</button>
        </div>
        <pre class="settings-system-guide__json"><code>${escapeHtml(guideJson)}</code></pre>
      </div>
    `;
  }

  function connectionGuideJson(system) {
    const guide = system && system.api_guide && typeof system.api_guide === "object" ? system.api_guide : {};
    const connectionType = String(system.connection_type || "external_management").toLowerCase();
    const fallbackGuide = {
      connection_type: connectionType,
      configure: {
        connection_id: settingsConnectionKey(system),
        system_id: slugifySystemId(system.system_id || settingsConnectionKey(system)),
        label: system.label || "",
        location: system.location || "manual",
        address: system.address || "",
      },
    };
    return JSON.stringify(Object.keys(guide).length ? guide : fallbackGuide, null, 2);
  }

  function connectionSettingsJson(system) {
    const settings = {
      connection_id: system.connection_id || "",
      system_id: system.system_id || "",
      label: system.label || "",
      kind: system.kind || "",
      lifecycle_mode: system.lifecycle_mode || "",
      connection_type: system.connection_type || "",
      location: system.location || "",
      address: system.address || "",
      runtime_type: system.runtime_type || "",
      replacement_target: system.replacement_target || "",
      integration_mode: system.integration_mode || "api_push",
      refresh_mode: system.refresh_mode || "on_change",
      endpoints: system.endpoints || {},
      capabilities: system.capabilities || {},
      collector_config: system.collector_config || {},
      readiness: system.readiness || {},
    };
    return JSON.stringify(settings, null, 2);
  }

  function keyValueLine(value) {
    const data = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    return Object.entries(data)
      .filter(([, val]) => String(val || "").trim())
      .map(([key, val]) => `${key}=${val}`)
      .join(", ");
  }

  function renderSettingsConnectionDetailInfo(system) {
    const readiness = system.readiness && typeof system.readiness === "object" ? system.readiness : {};
    const details = readiness.details && typeof readiness.details === "object" ? readiness.details : {};
    const missing = Array.isArray(readiness.missing_fields) ? readiness.missing_fields : [];
    const rows = [
      ["connection_id", system.connection_id || ""],
      ["requested_by", system.requested_by || ""],
      ["collector", system.collector || ""],
      [t("settings.connections.info.role"), connectionTypeLabel(system.connection_type)],
      [t("settings.connections.info.integration"), integrationModeLabel(system.integration_mode)],
      [t("settings.connections.info.refresh"), refreshModeLabel(system.refresh_mode)],
      [t("settings.connections.info.replaces"), replacementTargetLabel(system.replacement_target)],
      ["Runtime", system.runtime_type || ""],
      [t("settings.connections.info.location"), connectionLocationLabel(system.location)],
      [t("settings.connections.info.address"), system.address || ""],
      [t("settings.connections.info.readiness"), readinessLabel(readiness.state)],
      [t("settings.connections.info.missing"), missing.join(", ")],
      ["Runtime adapter", details.active_as_runtime_adapter ? t("runtime.active") : ""],
      [t("settings.connections.info.last_seen"), system.last_seen_at ? formatTs(system.last_seen_at) : ""],
    ].filter(([, value]) => String(value || "").trim());
    return `
      <div class="settings-connection-detail__info">
        ${rows.map(([label, value]) => `
          <div class="settings-system-row__info-item">
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(value || "—")}</strong>
          </div>
        `).join("")}
      </div>
    `;
  }

  function openSettingsConnectionDetails(systemId) {
    const system = settingsConnectionById(systemId);
    if (!system) return;
    closeSettingsConnectionDetails();
    const dialog = document.createElement("div");
    dialog.className = "settings-connection-dialog settings-connection-detail";
    dialog.dataset.settingsConnectionDetailSystem = settingsConnectionKey(system);
    const title = String(settingsSystemLabel(system) || t("settings.connections.details")).trim();
    dialog.innerHTML = `
      <div class="settings-connection-dialog__backdrop" data-settings-connection-detail-close></div>
      <section class="settings-connection-dialog__panel settings-connection-detail__panel" role="dialog" aria-modal="true" aria-label="${escapeHtml(title)}">
        <div class="settings-connection-dialog__head">
          <div>
            <h3>${escapeHtml(title)}</h3>
            <div class="settings-connection-detail__subtitle">${escapeHtml(connectionTypeLabel(system.connection_type))}</div>
          </div>
          <button class="settings-connection-dialog__close" type="button" data-settings-connection-detail-close aria-label="${escapeHtml(t("settings.connections.close"))}">×</button>
        </div>
        ${renderSettingsConnectionDetailInfo(system)}
        ${renderSettingsConnectionDetailActions(system)}
        ${renderSettingsConnectionEditForm(system)}
        <div class="settings-connection-detail__sections">
          <section class="settings-connection-detail__section">
            <div class="settings-connection-detail__section-title">${escapeHtml(t("settings.connections.settings_json"))}</div>
            <pre class="settings-system-guide__json"><code>${escapeHtml(connectionSettingsJson(system))}</code></pre>
          </section>
          <section class="settings-connection-detail__section">
            <div class="settings-connection-detail__section-title">
              <span>${escapeHtml(t("settings.connections.contract_json"))}</span>
              <button class="settings-system-guide__copy" type="button" data-settings-copy-guide="${escapeHtml(settingsConnectionKey(system))}">${escapeHtml(t("settings.connections.copy"))}</button>
            </div>
            <pre class="settings-system-guide__json"><code>${escapeHtml(connectionGuideJson(system))}</code></pre>
          </section>
        </div>
      </section>
    `;
    document.body.appendChild(dialog);
    dialog.querySelector("[data-settings-connection-detail-close]")?.focus?.();
  }

  function closeSettingsConnectionDetails() {
    document.querySelector(".settings-connection-detail")?.remove();
  }

  function renderSettingsConnectionDetailActions(system) {
    const systemId = settingsConnectionKey(system);
    const visible = systemVisible(systemId, settingsWorkspace?.display_settings);
    const custom = Boolean(system.custom);
    return `
      <div class="settings-connection-detail__actions" aria-label="${escapeHtml(t("settings.connections.actions"))}">
        <button
          class="settings-connection-detail__action settings-connection-detail__action--toggle${visible ? " is-shown" : " is-hidden"}"
          type="button"
          data-settings-system-toggle="${escapeHtml(systemId)}"
          aria-pressed="${visible ? "true" : "false"}"
        >${escapeHtml(visible ? t("settings.connections.disable_display") : t("settings.connections.enable_display"))}</button>
        ${custom ? `
          <button
            class="settings-connection-detail__action settings-connection-detail__action--delete"
            type="button"
            data-settings-system-delete="${escapeHtml(systemId)}"
          >${escapeHtml(t("settings.connections.delete"))}</button>
        ` : `
          <button
            class="settings-connection-detail__action settings-connection-detail__action--unavailable"
            type="button"
            disabled
            title="${escapeHtml(t("settings.connections.auto_discovered_title"))}"
          >${escapeHtml(t("settings.connections.delete_unavailable"))}</button>
        `}
      </div>
    `;
  }

  function renderSettingsConnectionEditForm(system) {
    if (!(system?.custom || system?.customizable)) return "";
    const systemId = settingsConnectionKey(system);
    const connectionType = String(system.connection_type || "external_management");
    const integrationMode = normalizeIntegrationMode(system.integration_mode);
    const refreshMode = normalizeRefreshMode(system.refresh_mode, integrationMode);
    const endpoints = keyValueLine(system.endpoints || {});
    const collectorConfig = JSON.stringify(system.collector_config || JSON.parse(externalCollectorPlaceholder(integrationMode, refreshMode)), null, 2);
    const showRuntime = connectionType !== "external_management";
    return `
      <form
        class="settings-connection-detail__edit"
        data-settings-connection-edit="${escapeHtml(systemId)}"
        data-settings-connection-edit-type="${escapeHtml(connectionType)}"
      >
        <div class="settings-connection-detail__section-title">${escapeHtml(t("settings.connections.edit"))}</div>
        <div class="settings-connection-detail__edit-grid">
          <label class="field">
            <span>${escapeHtml(t("html.settings.name"))}</span>
            <input class="input" name="label" autocomplete="off" value="${escapeHtml(system.label || "")}" />
          </label>
          <label class="field">
            <span>${escapeHtml(t("html.settings.type"))}</span>
            <input class="input" value="${escapeHtml(connectionTypeLabel(connectionType))}" disabled />
          </label>
          <label class="field">
            <span>${escapeHtml(t("settings.connections.info.replaces"))}</span>
            <input class="input" value="${escapeHtml(replacementTargetLabel(system.replacement_target))}" disabled />
          </label>
          <label class="field">
            <span>${escapeHtml(t("settings.connections.location"))}</span>
            <select class="input" name="location">
              ${["docker", "host", "ip", "manual"].map((value) => `
                <option value="${escapeHtml(value)}"${String(system.location || "manual") === value ? " selected" : ""}>${escapeHtml(connectionLocationLabel(value))}</option>
              `).join("")}
            </select>
          </label>
          <label class="field">
            <span>${escapeHtml(t("html.settings.address"))}</span>
            <input class="input" name="address" autocomplete="off" value="${escapeHtml(system.address || "")}" />
          </label>
          ${showRuntime ? `
            <label class="field">
              <span>${escapeHtml(t("settings.connections.runtime_type"))}</span>
              <input class="input" name="runtime_type" autocomplete="off" value="${escapeHtml(system.runtime_type || "")}" />
            </label>
          ` : ""}
          <label class="field">
            <span>${escapeHtml(t("settings.connections.integration_mode"))}</span>
            <select class="input" name="integration_mode" data-settings-connection-edit-integration>
              ${["api_push", "http_poll", "command_probe", "file_read"].map((value) => `
                <option value="${escapeHtml(value)}"${integrationMode === value ? " selected" : ""}>${escapeHtml(integrationModeLabel(value))}</option>
              `).join("")}
            </select>
          </label>
          <label class="field">
            <span>${escapeHtml(t("settings.connections.refresh_mode"))}</span>
            <select class="input" name="refresh_mode" data-settings-connection-edit-refresh${integrationMode === "api_push" ? " disabled" : ""}>
              ${["on_change", "manual", "interval"].map((value) => `
                <option value="${escapeHtml(value)}"${refreshMode === value ? " selected" : ""}${integrationMode !== "api_push" && value === "on_change" ? " disabled" : ""}>${escapeHtml(refreshModeLabel(value))}</option>
              `).join("")}
            </select>
          </label>
          ${showRuntime ? `
            <label class="field settings-connection-detail__wide">
              <span>${escapeHtml(t("settings.connections.endpoints"))}</span>
              <textarea class="input" name="endpoints" rows="3" spellcheck="false">${escapeHtml(endpoints)}</textarea>
            </label>
          ` : ""}
          <label class="field settings-connection-detail__wide">
            <span>${escapeHtml(t("settings.connections.collector_config"))}</span>
            <textarea class="input" name="collector_config" rows="6" spellcheck="false">${escapeHtml(collectorConfig)}</textarea>
          </label>
        </div>
        <div class="settings-connection-detail__edit-actions">
          <button class="btn btn--primary" type="submit">${escapeHtml(t("inventory.save"))}</button>
        </div>
      </form>
    `;
  }

  function getSettingsDisplayPayload() {
    const current = (settingsWorkspace && settingsWorkspace.display_settings) || {};
    const checkedOrCurrent = (id, key, fallback = true) => {
      const node = el(id);
      if (node) return Boolean(node.checked);
      if (typeof current[key] === "boolean") return current[key];
      return fallback;
    };
    const systemVisibility = {
      ...systemVisibilityFromSettings(current),
      ...settingsSystemVisibility,
    };
    document.querySelectorAll("[data-settings-system-toggle]").forEach((node) => {
      const displayKey = slugifySystemId(node.dataset.settingsSystemToggle);
      if (displayKey) systemVisibility[displayKey] = node.getAttribute("aria-pressed") !== "false";
    });

    return {
      system_visibility: systemVisibility,
      show_inactive: checkedOrCurrent("settingsShowInactive", "show_inactive", false),
      show_internal_vless: Boolean(current.show_internal_vless),
      hidden_subject_ids: Array.from(settingsHiddenSubjectIds),
      subject_traffic_preferences: settingsTrafficPreferences,
    };
  }

  function renderSubscriptionMeta() {
    const meta = el("vpnSubscriptionMeta");
    if (!meta) return;

    const subscription = (settingsWorkspace && settingsWorkspace.subscription) || {};
    const statusLabels = {
      success: t("settings.subscription.status.success"),
      failed: t("settings.subscription.status.failed"),
      running: t("settings.subscription.status.running"),
      idle: t("settings.subscription.status.idle"),
      not_configured: t("settings.subscription.status.not_configured"),
    };
    const parts = [];
    if (subscription.status) parts.push(t("settings.subscription.meta.status", { status: statusLabels[String(subscription.status)] || subscription.status }));
    if (subscription.url_saved) parts.push(t("settings.subscription.meta.url_saved"));
    if (subscription.last_refresh_at) parts.push(t("settings.subscription.meta.updated", { time: formatTs(subscription.last_refresh_at) }));
    if (subscription.last_success_at) parts.push(t("settings.subscription.meta.success", { time: formatTs(subscription.last_success_at) }));
    if (subscription.error_message) parts.push(translateBackendMessage(subscription.error_message));
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
    settingsSystemVisibility = systemVisibilityFromSettings(settings);
    setCheckbox("settingsShowInactive", settings.show_inactive);
    if (settingsClientsTab === "connections") renderSettingsConnections();
  }

  function syncSettingsClientTabs() {
    const tabSystems = {
      local_client: "lan",
      external_network_source: "external_network_source",
      external_client: "vless_client",
      service: "service",
      infrastructure: "router_core",
    };
    const counts = settingsWorkspace?.counts || {};
    const countForTab = (value) => {
      if (value === "local_client") return Number(counts.local_client ?? counts.lan_client ?? 0);
      if (value === "external_network_source") return Number(counts.external_network_source ?? 0);
      if (value === "external_client") return Number(counts.external_client ?? counts.vless_client ?? 0);
      if (value === "service") return Number(counts.service ?? Number(counts.docker_runtime ?? counts.docker ?? 0) + Number(counts.host_runtime ?? counts.host ?? 0));
      if (value === "infrastructure") return Number(counts.infrastructure ?? counts.router_core ?? 0);
      return Number(counts?.[value] || 0);
    };
    const optionalHasItems = (value) => countForTab(value) > 0;
    const domainTabVisible = (value) => {
      if (value === "service") {
        return systemVisible("docker", settingsWorkspace?.display_settings) || systemVisible("host", settingsWorkspace?.display_settings);
      }
      if (value === "infrastructure") {
        return systemVisible("router_core", settingsWorkspace?.display_settings) || systemVisible("infrastructure", settingsWorkspace?.display_settings);
      }
      return systemVisible(tabSystems[value] || value, settingsWorkspace?.display_settings);
    };
    const tabAvailable = (value) => (
      value === "local_client"
        ? domainTabVisible(value)
        : domainTabVisible(value) && optionalHasItems(value)
    );
    const visibleTabs = ["local_client", "external_client", "external_network_source", "service", "infrastructure"]
      .filter((value) => tabAvailable(value));
    if (!["all", "connections"].includes(settingsClientsTab) && !visibleTabs.includes(settingsClientsTab)) {
      settingsClientsTab = "all";
    }
    [["settingsClientsTabAll", "all"], ["settingsClientsTabLan", "local_client"], ["settingsClientsTabVless", "external_client"], ["settingsClientsTabExternalNetwork", "external_network_source"], ["settingsClientsTabDocker", "service"], ["settingsClientsTabHost", "infrastructure"], ["settingsClientsTabConnections", "connections"]]
      .forEach(([id, value]) => {
        const node = el(id);
        if (!node) return;
        node.hidden = !["all", "connections"].includes(value) && !tabAvailable(value);
        node.classList.toggle("is-active", settingsClientsTab === value);
      });
  }

  function renderSettingsClients() {
    const wrap = el("settingsClientsWrap");
    const meta = el("settingsClientsMeta");
    if (!wrap) return;

    if (settingsClientsTab === "connections") {
      if (meta) meta.textContent = t("settings.connections.meta");
      renderSettingsConnections();
      return;
    }

    const items = Array.isArray(settingsInventoryItems) ? settingsInventoryItems : [];
    const counts = settingsWorkspace?.counts || {};

    if (meta) {
      meta.textContent = renderSettingsCounts(counts);
    }

    if (!items.length) {
      wrap.innerHTML = `<div class="settings-events__empty muted">${escapeHtml(t("settings.clients.empty"))}</div>`;
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

  function inventoryRolesForDomainTab(tab) {
    return ({
      all: ["all"],
      local_client: ["lan_client"],
      external_client: ["vless_client"],
      external_network_source: ["external_network_source"],
      service: ["docker_runtime", "host_runtime"],
      infrastructure: ["router_core"],
    }[tab] || ["all"]);
  }

  async function loadSettingsInventory() {
    const seq = ++settingsInventoryRequestSeq;
    if (settingsInventoryAbortController) {
      settingsInventoryAbortController.abort();
    }
    if (settingsClientsTab === "connections") {
      settingsInventoryItems = [];
      renderSettingsConnections();
      clearDynamicStatus("settingsClientsState");
      return;
    }
    settingsInventoryAbortController = new AbortController();
    syncSettingsClientTabs();
    setDynamicStatus("settingsClientsState", "status.loading");

    try {
      const roles = inventoryRolesForDomainTab(settingsClientsTab);
      const responses = await Promise.all(roles.map((roleParam) => fetchApiV2(
        `/ui/settings/inventory?role=${encodeURIComponent(roleParam)}&limit=200&include_inactive=true`,
        { cache: "no-store", signal: settingsInventoryAbortController.signal }
      )));
      if (seq !== settingsInventoryRequestSeq) return;
      settingsInventoryItems = responses
        .flatMap((data) => (Array.isArray(data.items) ? data.items : []))
        .filter((item) => settingsClientsTab === "all" || subjectDomainCategory(item) === settingsClientsTab);
      renderSettingsClients();
      clearSettingsClientsDirty();
      clearDynamicStatus("settingsClientsState");
    } catch (e) {
      if (e?.name === "AbortError") return;
      if (seq !== settingsInventoryRequestSeq) return;
      settingsInventoryItems = [];
      renderSettingsClients();
      setText("settingsClientsState", t("status.error_prefix", { message: actionMessage(e) }));
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
      setText("settingsClientsState", t("status.error_prefix", { message: e.message }));
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
      } else if (settingsTab === "diagnostics") {
        await loadDiagnostics();
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
      wrap.innerHTML = `<div class="settings-proxy-list__empty">${escapeHtml(t("settings.proxy.empty"))}</div>`;
      return;
    }

    wrap.innerHTML = custom.map((server) => {
      const proxy = server.custom_proxy || {};
      const meta = [
        proxy.proxy_type ? String(proxy.proxy_type).toUpperCase() : "",
        proxy.host,
        proxy.port,
        server.preferences?.vpn_auto ? t("settings.proxy.in_auto") : "",
      ].filter(Boolean).join(" · ");

      return `
        <div class="settings-proxy-item">
          <div class="settings-proxy-item__top">
            <div class="settings-proxy-item__name">${escapeHtml(server.server_name || server.server_id || t("settings.proxy.default_name"))}</div>
            <button class="btn btn--danger" type="button" data-settings-delete-proxy="${escapeHtml(String(server.server_id || ""))}">${escapeHtml(t("inventory.delete"))}</button>
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
    const diagnosticsPane = el("settingsDiagnosticsPane");
    const controlsPane = el("settingsControlsPane");
    const logActions = el("settingsLogActions");
    const meta = el("settingsWorkspaceMeta");
    const summaryCard = document.querySelector("#settings-top .settings-summary-card");
    const journal = isJournalTab(settingsTab);

    if (eventsWrap) eventsWrap.hidden = !journal;
    if (rulesPane) rulesPane.hidden = settingsTab !== "rules";
    if (diagnosticsPane) diagnosticsPane.hidden = settingsTab !== "diagnostics";
    if (controlsPane) controlsPane.hidden = settingsTab !== "controls";
    if (logActions) logActions.hidden = !journal;
    if (summaryCard) summaryCard.hidden = settingsTab === "controls" || settingsTab === "diagnostics";

    if (meta) {
      if (journal) meta.textContent = t("settings.meta.journal", { category: categoryLabel(settingsTab) });
      else if (settingsTab === "rules") meta.textContent = t("settings.meta.rules");
      else if (settingsTab === "diagnostics") meta.textContent = t("settings.meta.diagnostics");
      else meta.textContent = t("settings.meta.controls");
    }

    if (settingsTab === "rules") {
      renderRulesContext(null);
      return;
    }

    if (settingsTab === "controls") {
      return;
    }

    if (settingsTab === "diagnostics") {
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
        translateBackendMessage(item.message),
        item.actor,
        item.category,
        item.event_class,
        item.severity,
        item.entity_type,
        item.entity_id,
        item.subject_id,
        item.connection_id,
        item.job_id,
        item.apply_id,
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
          ${escapeHtml(hasFilters ? t("settings.events.empty.filtered") : t("settings.events.empty.recent"))}
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
      clearDynamicStatus("adminLogsState");
      return;
    }

    if (!opts.silent) setDynamicStatus("adminLogsState", "status.loading");

    try {
      let auditItems = [];
      let operationalItems = [];
      let diagnosticItems = [];

      try {
        const typedData = await fetchJson("/api/v2/events/recent?limit=300", { cache: "no-store" });
        auditItems = (Array.isArray(typedData.audit) ? typedData.audit : []).map((event) => toTypedEvent(event, "audit"));
        operationalItems = (Array.isArray(typedData.operational) ? typedData.operational : []).map((event) => toTypedEvent(event, "operational"));
        diagnosticItems = (Array.isArray(typedData.diagnostic) ? typedData.diagnostic : []).map((event) => toTypedEvent(event, "diagnostic"));
      } catch (_) {
        const [operationalData, technicalData] = await Promise.all([
          fetchApiV2(`/logs/operational?limit=300&locale=${encodeURIComponent(window.FwrouterI18n?.locale?.() || "ru")}`, { cache: "no-store" }),
          fetchApiV2(`/logs/technical?limit=300&locale=${encodeURIComponent(window.FwrouterI18n?.locale?.() || "ru")}`, { cache: "no-store" }),
        ]);
        operationalItems = (Array.isArray(operationalData.events) ? operationalData.events : []).map(toLegacyEvent);
        diagnosticItems = (Array.isArray(technicalData.events) ? technicalData.events : []).map(toLegacyTechnicalEvent);
      }

      loadedEvents = groupRepeatedEvents([...auditItems, ...operationalItems, ...diagnosticItems]
        .filter((item) => matchesJournalTab(item, source))
        .sort((a, b) => (toUnixSeconds(b.ts) || 0) - (toUnixSeconds(a.ts) || 0)));

      if (selectedEventIndex >= loadedEvents.length) {
        selectedEventIndex = -1;
      }

      renderEvents(loadedEvents);

      setText(
        "settingsWorkspaceMeta",
        t("settings.logs.meta", { category: categoryLabel(source), days: 30 })
      );

      clearDynamicStatus("adminLogsState");
    } catch (e) {
      loadedEvents = [];
      selectedEventIndex = -1;
      renderEvents(loadedEvents);

      const wrap = el("adminEventsList");
      if (wrap) {
        wrap.innerHTML = `<div class="settings-events__empty">${escapeHtml(t("settings.logs.load_error", { message: translateBackendMessage(e.message) }))}</div>`;
      }

      renderSelectedEventContext();
      setText("adminLogsState", t("status.error"));
    }
  }

  function renderRulesStatus(rules) {
      const state = rules.state || {};
      const manual = rules.manual || {};
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
        : (configuredVpn.length ? t("settings.rules.source.vpn_list") : t("settings.rules.source.not_set"));
      const apply = {
        pending: ["running", "pending", "applying"].includes(String(state.status || "").toLowerCase()),
        done: Boolean(state.last_apply_job_id || state.last_update_job_id),
        done_at: toUnixSeconds(state.last_success_at || state.updated_at),
      };

      const statusLabel = {
        success: t("settings.rules.status.success"),
        clean: t("settings.rules.status.clean"),
        idle: t("settings.rules.status.idle"),
        running: t("settings.rules.status.running"),
        pending: t("settings.rules.status.pending"),
        applying: t("settings.rules.status.applying"),
        failed: t("settings.rules.status.failed"),
        not_configured: t("settings.rules.status.not_configured"),
      }[String(state.status || "").toLowerCase()] || String(state.status || t("settings.rules.status.unknown"));
      const totalCount = Number(effectiveCounts.total || 0);
      const vpnCount = Number(effectiveCounts.vpn || sourceCounts.big_vpn || 0);
      const detailParts = [];
      const draftValidationMessage = rulesValidationMessage({ data: { rules: { manual } } });
      lastRulesValidationMessage = draftValidationMessage;

      if (totalCount) detailParts.push(t("settings.rules.detail.rule_count", { count: totalCount.toLocaleString("ru-RU") }));
      else if (vpnCount) detailParts.push(t("settings.rules.detail.vpn_rule_count", { count: vpnCount.toLocaleString("ru-RU") }));
      if (draftValidationMessage) detailParts.push(t("settings.rules.detail.validation_error", { message: draftValidationMessage }));
      else if (bigVpnMeta.last_error_message) detailParts.push(t("settings.rules.detail.last_error", { message: translateBackendMessage(bigVpnMeta.last_error_message) }));
      else if (state.error_message) detailParts.push(translateBackendMessage(state.error_message));
      if (!detailParts.length && lastEffective.fetch_summary && Object.keys(lastEffective.fetch_summary).length) {
        detailParts.push(t("settings.rules.detail.has_metadata"));
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

  function renderRulesPolicy(payload) {
    const wrap = el("rulesPolicyView");
    if (!wrap) return;
    wrap.innerHTML = renderRoutingPolicyHtml(payload || {});
  }

  async function loadRoutingPolicyProjection(rulesSummary) {
    let rulesState = {};
    let subjectsState = {};
    let routingState = {};
    let reconcileState = {};
    try {
      [rulesState, subjectsState, routingState, reconcileState] = await Promise.all([
        fetchApiV2("/state/rules", { cache: "no-store" }),
        fetchApiV2("/state/subjects?limit=500", { cache: "no-store" }),
        fetchApiV2("/state/routing", { cache: "no-store" }),
        fetchJson("/api/v2/reconcile", { cache: "no-store" }),
      ]);
    } catch (_) {
      rulesState = rulesSummary ? { rules: { legacy: { raw: rulesSummary } } } : {};
    }
    return {
      rules: rulesState || {},
      rulesSummary: rulesSummary || rulesState?.rules?.legacy?.raw || {},
      subjects: subjectsState?.subjects || {},
      routing: routingState || {},
      reconcile: reconcileState || {},
    };
  }

  async function loadDiagnostics() {
    const wrap = el("settingsDiagnosticsView");
    if (!wrap) return;
    setDynamicStatus("adminLogsState", "status.loading");
    try {
      let report;
      try {
        report = await fetchJson("/api/v2/diagnose", { cache: "no-store" });
      } catch (_) {
        const [rulesData, operationalData, technicalData] = await Promise.all([
          fetchApiV2("/rules/summary", { cache: "no-store" }),
          fetchApiV2("/logs/operational?limit=20", { cache: "no-store" }),
          fetchApiV2("/logs/technical?limit=20", { cache: "no-store" }),
        ]);
        const ruleStatus = String(rulesData?.rules?.state?.status || "unknown").toLowerCase();
        const problemEvents = [
          ...(Array.isArray(operationalData.events) ? operationalData.events : []),
          ...(Array.isArray(technicalData.events) ? technicalData.events : []),
        ].filter((event) => ["warning", "error", "failed"].includes(String(event.level || "").toLowerCase()));
        report = {
          status: problemEvents.length || !["clean", "success", "idle"].includes(ruleStatus) ? "warning" : "ok",
          generated_at: new Date().toISOString(),
          sections: {
            database: { status: "ok" },
            subjects: { status: "warning" },
            connections: { status: "warning" },
            routing: { status: ruleStatus === "success" ? "ok" : "warning" },
            vpn: { status: "warning" },
            watchdog: { status: problemEvents.some((event) => String(event.component || event.category || "").toLowerCase() === "watchdog") ? "warning" : "ok" },
            events: { status: problemEvents.length ? "warning" : "ok" },
          },
          problems: problemEvents.slice(0, 10).map((event) => ({
            entity_type: String(event.component || event.category || "system").toLowerCase(),
            entity_id: event.subject_id || event.event_type || "system",
            severity: String(event.level || "warning").toLowerCase(),
            reason: event.message || event.event_type || "",
            source: "legacy_logs_compat",
            details: event.details || {},
          })),
        };
      }
      wrap.innerHTML = renderDiagnosticsHtml(report || {});
      clearDynamicStatus("adminLogsState");
    } catch (e) {
      wrap.innerHTML = `<div class="settings-events__empty muted">${escapeHtml(t("settings.logs.load_error", { message: translateBackendMessage(e.message) }))}</div>`;
      setText("adminLogsState", t("status.error"));
    }
  }

  function firstRulesValidationError(source) {
    const payload = source?.payload || source || {};
    const candidates = [
      payload?.error?.errors,
      payload?.data?.validation?.errors,
      payload?.data?.rules?.manual?.draft_validation?.errors,
      payload?.data?.job?.result?.validation?.errors,
      payload?.data?.job?.result?.mutation?.details?.validation?.errors,
      payload?.validation?.errors,
      payload?.rules?.manual?.draft_validation?.errors,
      payload?.manual?.draft_validation?.errors,
    ];

    for (const candidate of candidates) {
      if (Array.isArray(candidate) && candidate.length) return candidate[0];
    }

    return null;
  }

  function rulesValidationMessage(source) {
    const error = firstRulesValidationError(source);
    if (!error) return "";

    const line = Number(error.line || 0);
    const text = String(error.text || error.value || "").trim();
    const rawMessage = translateBackendMessage(error.message || error.code || "");
    const message = String(error.code || "") === "INVALID_FORMAT"
      ? t("settings.rules.validation.invalid_format")
      : rawMessage;

    if (line && text) return t("settings.rules.validation.line_text", { line, message, text });
    if (line) return t("settings.rules.validation.line", { line, message });
    if (text) return t("settings.rules.validation.text", { message, text });
    return message;
  }

  function rulesActionMessage(error) {
    return rulesValidationMessage(error) || lastRulesValidationMessage || actionMessage(error);
  }

  async function loadRules() {
    clearDynamicStatus("rulesState");

    try {
      const j = await fetchApiV2("/rules/summary", { cache: "no-store" });
      const policyPayload = await loadRoutingPolicyProjection(j.rules || {});
      const rules = j.rules || {};

      if (el("rulesText")) {
        el("rulesText").value = String(rules?.manual?.draft_text || rules?.manual?.active_text || "");
      }

      renderRulesPolicy(policyPayload);
      renderRulesStatus(rules);
      clearDynamicStatus("rulesState");
    } catch (e) {
      setText("rulesState", t("status.error_prefix", { message: rulesActionMessage(e) }));
    }
  }

  async function loadRulesUpstreamStatus() {
    try {
      const data = await fetchApiV2("/rules/summary", { cache: "no-store" });
      const policyPayload = await loadRoutingPolicyProjection(data.rules || {});
      renderRulesPolicy(policyPayload);
      return renderRulesStatus(data.rules || {});
    } catch (e) {
      setText("rulesState", t("status.error_prefix", { message: rulesActionMessage(e) }));

      return null;
    }
  }

  async function refreshRules(mode) {
    clearDynamicStatus("rulesState");

    try {
      await fetchApiV2("/rules/manual/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          requested_by: "ui",
          run_now: true,
        }),
      });

      setText("rulesState", t("status.ok"));
      await loadRules();
    } catch (e) {
      await loadRulesUpstreamStatus();
      setText("rulesState", t("status.error_prefix", { message: rulesActionMessage(e) }));
    }
  }

  async function updateAllRules() {
    setDynamicStatus("rulesState", "status.refreshing");

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
        setText("rulesState", t("settings.rules.result.already_current"));
        return;
      }

      setText("rulesState", t("settings.rules.result.updated"));
    } catch (e) {
      await loadRulesUpstreamStatus();
      setText("rulesState", t("status.error_prefix", { message: rulesActionMessage(e) }));
    }
  }

  async function saveRules() {
    clearDynamicStatus("rulesState");

    try {
      await fetchApiV2("/rules/manual", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: el("rulesText")?.value || "" }),
      });

      clearDynamicStatus("rulesState");
      await loadRulesUpstreamStatus();
    } catch (e) {
      renderRulesStatus(e?.payload?.data?.rules || {});
      setText("rulesState", t("status.error_prefix", { message: rulesActionMessage(e) }));
    }
  }

  async function saveVpnSubscriptionUrl() {
    const input = el("vpnSubscriptionUrl");
    if (!input) return;

    const url = String(input.value || "").trim();

    setDynamicStatus("vpnSubscriptionState", "status.saving");

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
      setText("vpnSubscriptionState", t("status.ready"));
      syncVpnSubscriptionHint();
      await loadSettingsWorkspace();
    } catch (_) {
      setDevVpnSubscriptionUrl(url);
      vpnSubscriptionSavedOnServer = Boolean(url);
      setText("vpnSubscriptionState", t("status.local"));
      syncVpnSubscriptionHint();
    }
  }

  async function refreshVpnSubscription() {
    setDynamicStatus("vpnSubscriptionState", "status.updating");

    try {
      await fetchApiV2("/subscription/refresh", { method: "POST" });
      setText("vpnSubscriptionState", t("status.ready"));
      await loadSettingsWorkspace();
    } catch (e) {
      setText("vpnSubscriptionState", t("status.error_prefix", { message: e.message }));
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

    setDynamicStatus("settingsProxyState", "status.saving");

    try {
      await fetchApiV2("/servers/custom/proxy", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setText("settingsProxyState", t("status.ready"));
      ["settingsProxyName", "settingsProxyHost", "settingsProxyPort", "settingsProxyUsername", "settingsProxyPassword"]
        .forEach((id) => {
          const node = el(id);
          if (node) node.value = "";
        });
      setSettingsProxyType("http");
      await loadSettingsProxyServers(true);
    } catch (e) {
      setText("settingsProxyState", t("status.error_prefix", { message: actionMessage(e) }));
    }
  }

  async function deleteSettingsProxy(serverId) {
    const normalized = String(serverId || "").trim();
    if (!normalized) return;

    setDynamicStatus("settingsProxyState", "status.deleting");

    try {
      await fetchApiV2(`/servers/custom/proxy/${encodeURIComponent(normalized)}?requested_by=ui`, {
        method: "DELETE",
      });
      await loadSettingsProxyServers(true);
      setText("settingsProxyState", t("status.ready"));
    } catch (e) {
      setText("settingsProxyState", t("status.error_prefix", { message: actionMessage(e) }));
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
      setText("settingsClientsState", t("status.error_prefix", { message: t("settings.traffic.pick_two") }));
      return;
    }

    setDynamicStatus("settingsClientsState", "status.saving");
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
      const actionAdapter = settingsClientActionAdapter(client);
      if (actionAdapter?.action === "xray_client") {
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
            setDynamicStatus("settingsClientsState", status === "queued" ? "status.queued" : "status.applying");
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
      settingsSystemVisibility = systemVisibilityFromSettings(settingsWorkspace.display_settings);
      settingsTrafficPreferences = normalizeTrafficPreferences(settingsWorkspace.display_settings.subject_traffic_preferences);
      document.dispatchEvent(new CustomEvent("fwrouter:display-settings-updated", {
        detail: { display_settings: settingsWorkspace.display_settings },
      }));
      await loadSettingsWorkspace();
      clearSettingsClientsDirty();
      setText("settingsClientsState", t("status.ok"));
      const freshRow = getSettingsClientRow(normalized);
      const freshModeSelect = document.querySelector(`[data-settings-mode-for="${CSS.escape(normalized)}"]`);
      const freshSaveButton = document.querySelector(`[data-settings-save-item="${CSS.escape(normalized)}"]`);
      flashScopeResult(freshRow || freshSaveButton || freshModeSelect || triggerNode || saveButton || modeSelect || aliasInput, "success");
    } catch (e) {
      setText("settingsClientsState", t("status.error_prefix", { message: actionMessage(e) }));
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

  async function deleteSettingsExternalClient(clientId) {
    const normalized = String(clientId || "").trim();
    if (!normalized) return;

    setDynamicStatus("settingsClientsState", "status.deleting");

    try {
      await fetchApiV2(`/xray/clients/${encodeURIComponent(normalized)}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ requested_by: "ui" }),
      });
      await loadSettingsWorkspace();
      setText("settingsClientsState", t("status.ok"));
    } catch (e) {
      setText("settingsClientsState", t("status.error_prefix", { message: actionMessage(e) }));
    }
  }

  async function deleteSettingsSystemSubject(subjectId) {
    const normalized = String(subjectId || "").trim();
    if (!normalized) return;

    setDynamicStatus("settingsClientsState", "status.deleting");

    try {
      await fetchApiV2(`/system-subjects/${encodeURIComponent(normalized)}?requested_by=ui`, {
        method: "DELETE",
      });
      await loadSettingsWorkspace();
      setText("settingsClientsState", t("status.ok"));
    } catch (e) {
      setText("settingsClientsState", t("status.error_prefix", { message: actionMessage(e) }));
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
      setText("settingsClientsState", t("settings.traffic.max_two"));
      return;
    }

    button.classList.toggle("is-selected", !isSelected);
    button.setAttribute("aria-pressed", !isSelected ? "true" : "false");

    const nextSelected = buttons
      .filter((item) => item.classList.contains("is-selected"))
      .map((item) => String(item.dataset.metric || "").trim())
      .filter((metric) => TRAFFIC_METRIC_KEYS.includes(metric));
    settingsTrafficPreferences[subjectId] = nextSelected;
    setText("settingsClientsState", nextSelected.length === 2 ? "" : t("settings.traffic.pick_two"));
  }

  function markSettingsClientsDirty() {
    const state = el("settingsClientsState");
    if (!state) return;
    state.textContent = t("settings.unsaved");
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
    if (!button) return "";
    const subjectId = String(button.dataset.settingsPowerToggle || "").trim();
    if (!subjectId) return "";

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
    button.textContent = nextEnabled ? t("inventory.power_on") : t("inventory.power_off");
    markSettingsClientsDirty();
    markSettingsRowDirty(subjectId);
    return subjectId;
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
      settingsSystemVisibility = systemVisibilityFromSettings(settingsWorkspace.display_settings);
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
      setText("settingsClientsState", t("status.error_prefix", { message: e.message }));
      flashScopeResult(getSettingsClientRow(subjectId) || row || button, "error");
    } finally {
      setPendingScope(getSettingsClientRow(subjectId) || row || button, false);
    }
  }

  async function saveSettingsDisplayFromSystems(triggerNode) {
    const payload = getSettingsDisplayPayload();
    setPendingScope(triggerNode, true);
    try {
      const j = await fetchApiV2("/ui/settings/display", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      settingsWorkspace = settingsWorkspace || {};
      settingsWorkspace.display_settings = j.display_settings || payload;
      settingsSystemVisibility = systemVisibilityFromSettings(settingsWorkspace.display_settings);
      applyDisplaySettings();
      await loadSettingsWorkspace();
      document.dispatchEvent(new CustomEvent("fwrouter:display-settings-updated", {
        detail: { display_settings: settingsWorkspace.display_settings },
      }));
      setText("settingsClientsState", t("status.ok"));
      flashScopeResult(triggerNode, "success");
    } catch (e) {
      setText("settingsClientsState", t("status.error_prefix", { message: actionMessage(e) }));
      flashScopeResult(triggerNode, "error");
    } finally {
      setPendingScope(triggerNode, false);
    }
  }

  function toggleSettingsSystemVisibility(button) {
    const displayKey = slugifySystemId(button?.dataset.settingsSystemToggle);
    if (!displayKey) return;
    const openDetailKey = slugifySystemId(document.querySelector(".settings-connection-detail")?.dataset.settingsConnectionDetailSystem);
    const nextVisible = !(settingsSystemVisibility[displayKey] !== false);
    settingsSystemVisibility[displayKey] = nextVisible;
    renderSettingsConnections();
    if (openDetailKey === displayKey) {
      openSettingsConnectionDetails(displayKey);
    }
    saveSettingsDisplayFromSystems(getSettingsSystemRow(displayKey) || button);
  }

  function getSettingsSystemRow(systemId) {
    const normalized = slugifySystemId(systemId);
    return normalized ? document.querySelector(`[data-settings-system-row="${CSS.escape(normalized)}"]`) : null;
  }

  function addSettingsExternalSystem() {
    openSettingsExternalSystemDialog();
  }

  function openSettingsExternalSystemDialog() {
    closeSettingsExternalSystemDialog();
    const dialog = document.createElement("div");
    dialog.className = "settings-connection-dialog";
    dialog.innerHTML = `
      <div class="settings-connection-dialog__backdrop" data-settings-connection-close></div>
      <form class="settings-connection-dialog__panel" data-settings-connection-form>
        <div class="settings-connection-dialog__head">
          <div>
            <h3>${escapeHtml(t("settings.connections.add"))}</h3>
          </div>
          <button class="settings-connection-dialog__close" type="button" data-settings-connection-close aria-label="${escapeHtml(t("settings.connections.close"))}">×</button>
        </div>
        <div class="settings-connection-dialog__grid">
          <label class="field settings-connection-dialog__wide">
            <span>${escapeHtml(t("html.settings.type"))}</span>
            <select
              class="input settings-connection-type-select"
              name="connection_type"
              data-settings-connection-type
              title="${escapeHtml(t("settings.connections.type_title"))}"
            >
              <option value="external_management">${escapeHtml(t("settings.connections.type_option.external_management"))}</option>
              <option value="external_vpn_module" selected>${escapeHtml(t("settings.connections.type_option.external_vpn_module"))}</option>
              <option value="external_network_source">${escapeHtml(t("settings.connections.type_option.external_network_source"))}</option>
            </select>
            <small class="settings-connection-type-hint" data-settings-connection-type-hint></small>
          </label>
          <label class="field">
            <span>${escapeHtml(t("html.settings.name"))}</span>
            <input class="input" name="label" autocomplete="off" placeholder="${escapeHtml(t("settings.connections.name_placeholder"))}" required />
          </label>
          <label class="field">
            <span>${escapeHtml(t("settings.connections.location"))}</span>
            <select class="input" name="location">
              <option value="docker">Docker</option>
              <option value="host">Host</option>
              <option value="ip">IP / hostname</option>
              <option value="manual">Manual</option>
            </select>
          </label>
          <label class="field">
            <span>${escapeHtml(t("html.settings.address"))}</span>
            <input class="input" name="address" autocomplete="off" placeholder="${escapeHtml(t("settings.connections.address_placeholder"))}" />
          </label>
          <label class="field" data-settings-runtime-field>
            <span>${escapeHtml(t("settings.connections.runtime_type"))}</span>
            <input class="input" name="runtime_type" autocomplete="off" value="generic" placeholder="sing-box, mihomo-compatible, wireguard, api" />
          </label>
          <label class="field" data-settings-replacement-field>
            <span>${escapeHtml(t("settings.connections.info.replaces"))}</span>
            <select class="input" name="replacement_target" title="${escapeHtml(t("settings.connections.replacement_title"))}">
              <option value="">${escapeHtml(t("settings.connections.replacement_none"))}</option>
              <option value="mihomo" selected>${escapeHtml(t("settings.connections.replacement_vpn_dataplane"))}</option>
              <option value="xray">${escapeHtml(t("settings.connections.replacement_external_client"))}</option>
            </select>
          </label>
          <label class="field settings-connection-dialog__wide" data-settings-endpoints-field>
            <span>${escapeHtml(t("settings.connections.endpoints"))}</span>
            <textarea class="input" name="endpoints" rows="4" spellcheck="false"></textarea>
          </label>
          <label class="field">
            <span>${escapeHtml(t("settings.connections.integration_mode"))}</span>
            <select class="input" name="integration_mode" data-settings-integration-mode title="${escapeHtml(t("settings.connections.integration_title"))}">
              <option value="api_push" selected>${escapeHtml(t("settings.connections.integration.api_push"))}</option>
              <option value="http_poll">${escapeHtml(t("settings.connections.integration.http_poll"))}</option>
              <option value="command_probe">${escapeHtml(t("settings.connections.integration.command_probe"))}</option>
              <option value="file_read">${escapeHtml(t("settings.connections.integration.file_read"))}</option>
            </select>
          </label>
          <label class="field">
            <span>${escapeHtml(t("settings.connections.refresh_mode"))}</span>
            <select class="input" name="refresh_mode" data-settings-refresh-mode title="${escapeHtml(t("settings.connections.refresh_title"))}">
              <option value="on_change" selected>${escapeHtml(t("settings.connections.refresh.on_change"))}</option>
              <option value="manual">${escapeHtml(t("settings.connections.refresh.manual"))}</option>
              <option value="interval">${escapeHtml(t("settings.connections.refresh.interval"))}</option>
            </select>
          </label>
          <label class="field settings-connection-dialog__wide" data-settings-collector-field>
            <span>${escapeHtml(t("settings.connections.collector_config"))}</span>
            <textarea class="input" name="collector_config" rows="7" spellcheck="false"></textarea>
            <small class="settings-connection-type-hint" data-settings-collector-hint></small>
          </label>
        </div>
        <div class="settings-connection-dialog__actions">
          <button class="btn btn--secondary" type="button" data-settings-connection-close>${escapeHtml(t("settings.connections.cancel"))}</button>
          <button class="btn btn--primary" type="submit">${escapeHtml(t("settings.connections.add"))}</button>
        </div>
      </form>
    `;
    document.body.appendChild(dialog);
    syncSettingsConnectionDialog(dialog);
    dialog.querySelector("[name='label']")?.focus();
  }

  function closeSettingsExternalSystemDialog() {
    document.querySelector(".settings-connection-dialog")?.remove();
  }

  function syncSettingsConnectionDialog(dialog) {
    const root = dialog || document.querySelector(".settings-connection-dialog");
    if (!root) return;
    const connectionType = String(root.querySelector("[name='connection_type']")?.value || "external_vpn_module");
    const hint = root.querySelector("[data-settings-connection-type-hint]");
    if (hint) hint.textContent = externalConnectionTypeHint(connectionType);
    const runtimeField = root.querySelector("[data-settings-runtime-field]");
    const replacementField = root.querySelector("[data-settings-replacement-field]");
    const endpointsField = root.querySelector("[data-settings-endpoints-field]");
    const endpointsInput = root.querySelector("[name='endpoints']");
    const integrationMode = String(root.querySelector("[name='integration_mode']")?.value || "api_push");
    const refreshModeInput = root.querySelector("[name='refresh_mode']");
    const collectorInput = root.querySelector("[name='collector_config']");
    const collectorHint = root.querySelector("[data-settings-collector-hint]");
    const showRuntime = connectionType !== "external_management";
    if (runtimeField) runtimeField.hidden = !showRuntime;
    if (replacementField) replacementField.hidden = connectionType === "external_management";
    if (endpointsField) endpointsField.hidden = connectionType === "external_management";
    if (endpointsInput && !endpointsInput.dataset.userEdited) {
      endpointsInput.value = externalConnectionEndpointPlaceholder(connectionType);
    }
    if (refreshModeInput && integrationMode === "api_push") {
      refreshModeInput.value = "on_change";
    } else if (refreshModeInput && refreshModeInput.value === "on_change") {
      refreshModeInput.value = "manual";
    }
    if (collectorInput && !collectorInput.dataset.userEdited) {
      collectorInput.value = externalCollectorPlaceholder(integrationMode, refreshModeInput?.value || "manual");
    }
    if (collectorHint) {
      collectorHint.textContent = externalCollectorHint(integrationMode);
    }
  }

  function buildSettingsExternalConnectionPayload(form) {
    const formData = new FormData(form);
    const connectionType = String(formData.get("connection_type") || "external_vpn_module");
    const label = String(formData.get("label") || "").trim();
    if (!label) return null;
    const rawLocation = String(formData.get("location") || "manual").trim().toLowerCase();
    const location = ["docker", "host", "ip", "manual"].includes(rawLocation) ? rawLocation : "manual";
    const address = String(formData.get("address") || "").trim();
    const runtimeType = connectionType === "external_management"
      ? ""
      : String(formData.get("runtime_type") || "generic").trim();
    const rawReplacementTarget = String(formData.get("replacement_target") || "").trim().toLowerCase();
    const replacementTarget = ["mihomo", "xray"].includes(rawReplacementTarget) ? rawReplacementTarget : "";
    const endpoints = connectionType === "external_management"
      ? {}
      : parseKeyValueList(String(formData.get("endpoints") || ""));
    const integrationMode = normalizeIntegrationMode(formData.get("integration_mode"));
    const refreshMode = normalizeRefreshMode(formData.get("refresh_mode"), integrationMode);
    const collectorConfig = parseCollectorConfig(String(formData.get("collector_config") || ""), integrationMode, refreshMode);
    const capabilities = inferExternalConnectionCapabilities(connectionType, endpoints);
    return {
      label,
      connection_type: connectionType,
      location,
      address,
      runtime_type: runtimeType,
      replacement_target: replacementTarget,
      endpoints,
      capabilities,
      integration_mode: integrationMode,
      refresh_mode: refreshMode,
      collector_config: collectorConfig,
      description: externalConnectionDescription(connectionType),
    };
  }

  async function submitSettingsExternalSystem(form) {
    let payload;
    try {
      payload = buildSettingsExternalConnectionPayload(form);
    } catch (e) {
      setText("settingsClientsState", t("status.error_prefix", { message: actionMessage(e) }));
      flashScopeResult(form, "error");
      return;
    }
    if (!payload) return;
    setDynamicStatus("settingsClientsState", "status.saving");
    setPendingScope(form, true);
    try {
      const response = await fetchApiV2("/ui/external-connections", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      settingsWorkspace = settingsWorkspace || {};
      settingsWorkspace.display_settings = response.display_settings || settingsWorkspace.display_settings || {};
      settingsSystemVisibility = { ...(settingsWorkspace.display_settings.system_visibility || {}) };
      renderSettingsConnections();
      closeSettingsExternalSystemDialog();
      await loadSettingsWorkspace();
      settingsClientsTab = "connections";
      renderSettingsConnections();
      setText("settingsClientsState", t("status.ok"));
    } catch (e) {
      setText("settingsClientsState", t("status.error_prefix", { message: actionMessage(e) }));
      flashScopeResult(form, "error");
    } finally {
      setPendingScope(form, false);
    }
  }

  function buildSettingsConnectionPatchPayload(form) {
    const formData = new FormData(form);
    const connectionType = String(form.dataset.settingsConnectionEditType || "external_management");
    const integrationMode = normalizeIntegrationMode(formData.get("integration_mode"));
    const refreshMode = normalizeRefreshMode(formData.get("refresh_mode"), integrationMode);
    const endpoints = connectionType === "external_management"
      ? {}
      : parseKeyValueList(String(formData.get("endpoints") || ""));
    return {
      label: String(formData.get("label") || "").trim(),
      location: String(formData.get("location") || "manual").trim().toLowerCase(),
      address: String(formData.get("address") || "").trim(),
      runtime_type: connectionType === "external_management"
        ? ""
        : String(formData.get("runtime_type") || "").trim(),
      endpoints,
      capabilities: inferExternalConnectionCapabilities(connectionType, endpoints),
      integration_mode: integrationMode,
      refresh_mode: refreshMode,
      collector_config: parseCollectorConfig(String(formData.get("collector_config") || ""), integrationMode, refreshMode),
    };
  }

  async function saveSettingsConnectionDetails(form) {
    const connectionId = slugifySystemId(form?.dataset.settingsConnectionEdit);
    if (!connectionId) return;
    let payload;
    try {
      payload = buildSettingsConnectionPatchPayload(form);
    } catch (e) {
      setText("settingsClientsState", t("status.error_prefix", { message: actionMessage(e) }));
      flashScopeResult(form, "error");
      return;
    }
    setDynamicStatus("settingsClientsState", "status.saving");
    setPendingScope(form, true);
    try {
      const response = await fetchApiV2(`/ui/external-connections/${encodeURIComponent(connectionId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      settingsWorkspace = settingsWorkspace || {};
      settingsWorkspace.display_settings = response.display_settings || settingsWorkspace.display_settings || {};
      settingsSystemVisibility = { ...(settingsWorkspace.display_settings.system_visibility || {}) };
      await loadSettingsWorkspace();
      settingsClientsTab = "connections";
      renderSettingsConnections();
      setText("settingsClientsState", t("status.ok"));
      openSettingsConnectionDetails(connectionId);
    } catch (e) {
      setText("settingsClientsState", t("status.error_prefix", { message: actionMessage(e) }));
      flashScopeResult(form, "error");
    } finally {
      setPendingScope(form, false);
    }
  }

  function syncSettingsConnectionEditForm(root) {
    const form = root?.closest?.("[data-settings-connection-edit]") || root;
    if (!form) return;
    const integrationInput = form.querySelector("[data-settings-connection-edit-integration]");
    const refreshInput = form.querySelector("[data-settings-connection-edit-refresh]");
    const integrationMode = normalizeIntegrationMode(integrationInput?.value || "api_push");
    if (!refreshInput) return;
    refreshInput.disabled = integrationMode === "api_push";
    Array.from(refreshInput.options || []).forEach((option) => {
      option.disabled = integrationMode !== "api_push" && option.value === "on_change";
    });
    if (integrationMode === "api_push") {
      refreshInput.value = "on_change";
    } else if (refreshInput.value === "on_change") {
      refreshInput.value = "manual";
    }
  }

  function externalConnectionTypeHint(connectionType) {
    if (connectionType === "external_management") {
      return t("settings.connections.hint.external_management");
    }
    if (connectionType === "external_network_source") {
      return t("settings.connections.hint.external_network_source");
    }
    return t("settings.connections.hint.external_vpn_module");
  }

  function externalConnectionEndpointPlaceholder(connectionType) {
    if (connectionType === "external_vpn_module") {
      return "http_proxy_url=http://127.0.0.1:7890, socks_proxy_url=socks5://127.0.0.1:7891, tcp_redir_port=7892, udp_tproxy_port=7893, controller_url=http://127.0.0.1:9090, healthcheck_url=http://127.0.0.1:9090/version";
    }
    if (connectionType === "external_network_source") {
      return "client_inventory_url=http://127.0.0.1:8080/clients, interface_name=wg0, client_cidr=100.64.0.0/10, healthcheck_url=http://127.0.0.1:8080/health";
    }
    return "";
  }

  function externalCollectorPlaceholder(integrationMode, refreshMode) {
    const base = {
      timeout_seconds: 5,
      apply_traffic: false,
    };
    if (refreshMode === "interval") {
      base.interval_seconds = 300;
      base.trigger = "poll_interval";
    } else if (refreshMode === "manual") {
      base.trigger = "manual_refresh";
    }
    if (integrationMode === "http_poll") {
      return JSON.stringify({ ...base, url: "http://127.0.0.1:8080/status" }, null, 2);
    }
    if (integrationMode === "command_probe") {
      return JSON.stringify({ ...base, script_id: "", extra_args: [] }, null, 2);
    }
    if (integrationMode === "file_read") {
      return JSON.stringify({ ...base, path: "/var/lib/fwrouter-v2/external-collectors/status.json" }, null, 2);
    }
    return JSON.stringify({
      interval_seconds: 300,
      timeout_seconds: 5,
      apply_traffic: false,
      trigger: "external_system_pushes_on_change",
    }, null, 2);
  }

  function externalCollectorHint(integrationMode) {
    if (integrationMode === "http_poll") return t("settings.connections.collector_hint.http_poll");
    if (integrationMode === "command_probe") return t("settings.connections.collector_hint.command_probe");
    if (integrationMode === "file_read") return t("settings.connections.collector_hint.file_read");
    return t("settings.connections.collector_hint.api_push");
  }

  function normalizeIntegrationMode(value) {
    const raw = String(value || "").trim().toLowerCase();
    return ["api_push", "http_poll", "command_probe", "file_read"].includes(raw) ? raw : "api_push";
  }

  function normalizeRefreshMode(value, integrationMode) {
    const raw = String(value || "").trim().toLowerCase();
    if (integrationMode === "api_push") return "on_change";
    return ["manual", "interval"].includes(raw) ? raw : "manual";
  }

  function parseCollectorConfig(value, integrationMode, refreshMode) {
    const fallback = JSON.parse(externalCollectorPlaceholder(integrationMode, refreshMode));
    const raw = String(value || "").trim();
    if (!raw) return fallback;
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
    } catch (e) {
      throw new Error(t("settings.connections.invalid_collector_json"));
    }
    throw new Error(t("settings.connections.invalid_collector_json"));
  }

  function externalConnectionDescription(connectionType) {
    if (connectionType === "external_vpn_module") {
      return "External VPN core: user-managed runtime with proxy/transparent endpoints.";
    }
    if (connectionType === "external_network_source") {
      return "External network source: user-managed provider of client inventory or network ranges.";
    }
    return "External management client: calls FWRouter API, not a routing target.";
  }

  function parseKeyValueList(value) {
    const result = {};
    String(value || "")
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean)
      .forEach((part) => {
        const index = part.indexOf("=");
        if (index <= 0) return;
        const key = part.slice(0, index).trim();
        const val = part.slice(index + 1).trim();
        if (key && val) result[key] = val;
      });
    return result;
  }

  function inferExternalConnectionCapabilities(connectionType, endpoints) {
    const data = endpoints && typeof endpoints === "object" ? endpoints : {};
    if (connectionType === "external_vpn_module") {
      return {
        supports_tcp: Boolean(data.http_proxy_url || data.socks_proxy_url || data.tcp_redir_port),
        supports_udp: Boolean(data.udp_tproxy_port),
        supports_http_proxy: Boolean(data.http_proxy_url),
        supports_socks_proxy: Boolean(data.socks_proxy_url),
        supports_transparent_proxy: Boolean(data.tcp_redir_port || data.udp_tproxy_port),
        supports_selector_api: Boolean(data.controller_url),
        supports_client_api: Boolean(data.client_api_url || data.client_inventory_url),
        supports_subscription_api: Boolean(data.subscription_base_url),
        supports_traffic_stats: Boolean(data.traffic_stats_url),
        supports_reload: Boolean(data.reload_url),
      };
    }
    if (connectionType === "external_network_source") {
      return {
        supports_client_inventory: Boolean(data.client_inventory_url || data.interface_name || data.client_cidr),
      };
    }
    return {};
  }

  function guideJsonForSystemId(systemId) {
    const normalized = slugifySystemId(systemId);
    const systems = Array.isArray(settingsWorkspace?.display_systems)
      ? settingsWorkspace.display_systems
      : [];
    const system = systems.find((item) => settingsConnectionKey(item) === normalized);
    return system ? connectionGuideJson(system) : "";
  }

  async function copySettingsConnectionGuide(button) {
    const systemId = slugifySystemId(button?.dataset.settingsCopyGuide);
    const text = guideJsonForSystemId(systemId);
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      const previous = button.textContent;
      button.textContent = t("settings.connections.copied");
      window.setTimeout(() => {
        button.textContent = previous || t("settings.connections.copy");
      }, 1200);
    } catch (_) {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
  }

  async function copySettingsValue(button) {
    const text = String(button?.dataset.settingsCopyValue || "").trim();
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      const previous = button.textContent;
      button.textContent = t("settings.connections.copied");
      window.setTimeout(() => {
        button.textContent = previous || t("settings.connections.copy");
      }, 1200);
    } catch (_) {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
  }

  async function deleteSettingsExternalSystem(button) {
    const connectionId = slugifySystemId(button?.dataset.settingsSystemDelete);
    if (!connectionId) return;
    const openDetailKey = slugifySystemId(document.querySelector(".settings-connection-detail")?.dataset.settingsConnectionDetailSystem);
    setDynamicStatus("settingsClientsState", "status.deleting");
    setPendingScope(getSettingsSystemRow(connectionId) || button, true);
    try {
      const response = await fetchApiV2(`/ui/external-connections/${encodeURIComponent(connectionId)}`, {
        method: "DELETE",
      });
      settingsWorkspace = settingsWorkspace || {};
      settingsWorkspace.display_settings = response.display_settings || settingsWorkspace.display_settings || {};
      settingsSystemVisibility = { ...(settingsWorkspace.display_settings.system_visibility || {}) };
      if (openDetailKey === connectionId) {
        closeSettingsConnectionDetails();
      }
      await loadSettingsWorkspace();
      settingsClientsTab = "connections";
      renderSettingsConnections();
      setText("settingsClientsState", t("status.ok"));
    } catch (e) {
      setText("settingsClientsState", t("status.error_prefix", { message: actionMessage(e) }));
      flashScopeResult(getSettingsSystemRow(connectionId) || button, "error");
    } finally {
      setPendingScope(getSettingsSystemRow(connectionId) || button, false);
    }
  }

  function wire() {
    if (settingsBootstrapped) return;
    settingsBootstrapped = true;

    el("adminLogsRefresh")?.addEventListener("click", () => {
      if (settingsTab === "rules") {
        loadRules();
        return;
      }
      if (settingsTab === "diagnostics") {
        loadDiagnostics();
        return;
      }
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

        if (source === "diagnostics") {
          settingsTab = source;
          syncSettingsTabs();
          loadDiagnostics();
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
    [["settingsClientsTabAll", "all"], ["settingsClientsTabLan", "local_client"], ["settingsClientsTabVless", "external_client"], ["settingsClientsTabExternalNetwork", "external_network_source"], ["settingsClientsTabDocker", "service"], ["settingsClientsTabHost", "infrastructure"], ["settingsClientsTabConnections", "connections"]]
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
        const subjectId = toggleSettingsPower(powerToggle);
        if (subjectId) {
          const modeSelect = document.querySelector(`[data-settings-mode-for="${CSS.escape(subjectId)}"]`);
          saveSettingsItem(subjectId, modeSelect?.value || undefined, powerToggle);
        }
        return;
      }

      const adminVisibilityToggle = ev.target.closest("[data-settings-admin-visibility]");
      if (adminVisibilityToggle) {
        toggleSettingsAdminVisibility(adminVisibilityToggle);
        return;
      }

      const systemVisibilityToggle = ev.target.closest("[data-settings-system-toggle]");
      if (systemVisibilityToggle) {
        toggleSettingsSystemVisibility(systemVisibilityToggle);
        return;
      }

      const systemDelete = ev.target.closest("[data-settings-system-delete]");
      if (systemDelete) {
        deleteSettingsExternalSystem(systemDelete);
        return;
      }

      const addExternalSystem = ev.target.closest("[data-settings-add-external]");
      if (addExternalSystem) {
        addSettingsExternalSystem();
        return;
      }

      const copyGuide = ev.target.closest("[data-settings-copy-guide]");
      if (copyGuide) {
        ev.preventDefault();
        ev.stopPropagation();
        copySettingsConnectionGuide(copyGuide);
        return;
      }

      const copyValue = ev.target.closest("[data-settings-copy-value]");
      if (copyValue) {
        ev.preventDefault();
        ev.stopPropagation();
        copySettingsValue(copyValue);
        return;
      }

      const closeConnectionDetail = ev.target.closest("[data-settings-connection-detail-close]");
      if (closeConnectionDetail) {
        closeSettingsConnectionDetails();
        return;
      }

      const closeConnectionDialog = ev.target.closest("[data-settings-connection-close]");
      if (closeConnectionDialog) {
        closeSettingsExternalSystemDialog();
        return;
      }

      const connectionRow = ev.target.closest("[data-settings-system-open]");
      if (connectionRow && !ev.target.closest("button, a, input, select, textarea, summary, details")) {
        openSettingsConnectionDetails(connectionRow.dataset.settingsSystemOpen);
        return;
      }

      const connectionsTab = ev.target.closest("#settingsClientsTabConnections");
      if (connectionsTab) {
        settingsClientsTab = "connections";
        syncSettingsClientTabs();
        loadSettingsInventory();
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
        if (kind === "xray_client" && id) deleteSettingsExternalClient(id);
        if (kind === "system_subject" && id) deleteSettingsSystemSubject(id);
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

    document.addEventListener("submit", (ev) => {
      const editForm = ev.target.closest?.("[data-settings-connection-edit]");
      if (editForm) {
        ev.preventDefault();
        saveSettingsConnectionDetails(editForm);
        return;
      }
      const form = ev.target.closest?.("[data-settings-connection-form]");
      if (!form) return;
      ev.preventDefault();
      submitSettingsExternalSystem(form);
    });

    document.addEventListener("change", (ev) => {
      if (ev.target.closest?.("[data-settings-connection-edit-integration], [data-settings-connection-edit-refresh]")) {
        syncSettingsConnectionEditForm(ev.target);
        return;
      }
      if (!ev.target.closest?.("[data-settings-connection-type], [data-settings-integration-mode], [data-settings-refresh-mode]")) return;
      const dialog = ev.target.closest(".settings-connection-dialog");
      const endpointsInput = dialog?.querySelector("[name='endpoints']");
      const collectorInput = dialog?.querySelector("[name='collector_config']");
      if (ev.target.closest?.("[data-settings-connection-type]") && endpointsInput) delete endpointsInput.dataset.userEdited;
      if (ev.target.closest?.("[data-settings-integration-mode], [data-settings-refresh-mode]") && collectorInput) {
        delete collectorInput.dataset.userEdited;
      }
      syncSettingsConnectionDialog(dialog);
    });

    document.addEventListener("input", (ev) => {
      const endpointsInput = ev.target.closest?.(".settings-connection-dialog [name='endpoints']");
      if (endpointsInput) endpointsInput.dataset.userEdited = "1";
      const collectorInput = ev.target.closest?.(".settings-connection-dialog [name='collector_config']");
      if (collectorInput) collectorInput.dataset.userEdited = "1";
    });

    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && document.querySelector(".settings-connection-detail")) {
        closeSettingsConnectionDetails();
        return;
      }
      if (ev.key === "Escape" && document.querySelector(".settings-connection-dialog")) {
        closeSettingsExternalSystemDialog();
        return;
      }
      if ((ev.key === "Enter" || ev.key === " ") && ev.target?.closest?.("[data-settings-system-open]")) {
        if (ev.target.closest("button, a, input, select, textarea, summary, details")) return;
        ev.preventDefault();
        openSettingsConnectionDetails(ev.target.closest("[data-settings-system-open]").dataset.settingsSystemOpen);
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

  document.addEventListener("fwrouter:locale", () => {
    if ((document.documentElement.dataset.view || "") !== "settings") return;
    applyDisplaySettings();
    renderSubscriptionMeta();
    renderProxyList();
    renderSettingsClients();
    renderSettingsConnections();
    if (isJournalTab(settingsTab)) {
      loadSettingsLogs({ source: settingsTab, silent: true });
    } else if (settingsTab === "diagnostics") {
      loadDiagnostics();
    } else {
      renderSelectedEventContext();
    }
    syncVpnSubscriptionHint();
  });
})();
