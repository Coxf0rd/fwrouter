// Settings inventory rendering and traffic preference helpers.
(function () {
  const t = (key, params) => window.FwrouterI18n?.t(key, params) || key;
  const TRAFFIC_METRIC_KEYS = ["direct_rx_bytes", "direct_tx_bytes", "vpn_rx_bytes", "vpn_tx_bytes"];

  const {
    escapeHtml,
    trafficMetricLabel,
    formatTrafficBytes,
  } = window.FwrouterUI;
  const {
    settingsModeLabel: modeLabel,
    settingsSourceLabel: sourceLabel,
    presentationState,
    presentationLevelClass,
    settingsModeOptions,
    defaultEnabledModeFor,
    subjectDomainCategory,
    domainCategoryLabel,
    implementationLabel,
  } = window.FwrouterLabels;
  const { freshnessFor } = window.FwrouterSettingsEvents;

  function normalizeTrafficPreferences(preferences) {
    const normalized = {};
    if (!preferences || typeof preferences !== "object") return normalized;
    Object.entries(preferences).forEach(([subjectId, metrics]) => {
      if (!Array.isArray(metrics)) return;
      const uniq = [];
      metrics.forEach((metric) => {
        const key = String(metric || "").trim();
        if (!TRAFFIC_METRIC_KEYS.includes(key)) return;
        if (uniq.includes(key)) return;
        uniq.push(key);
      });
      if (uniq.length >= 2) {
        normalized[String(subjectId)] = uniq.slice(0, 2);
      }
    });
    return normalized;
  }

  function trafficMetricBytes(client, key) {
    const month = client && client.traffic_month && typeof client.traffic_month === "object"
      ? client.traffic_month
      : {};
    return Number(month[key] || 0);
  }

  function metricPreferenceForClient(client, trafficPreferences) {
    const subjectId = String(client?.subject_id || "");
    const preferences = trafficPreferences || {};
    const preferred = Array.isArray(preferences[subjectId])
      ? preferences[subjectId]
      : (Array.isArray(client?.traffic_panel_metric_keys) ? client.traffic_panel_metric_keys : ["vpn_rx_bytes", "vpn_tx_bytes"]);
    const uniq = [];
    preferred.forEach((metric) => {
      const key = String(metric || "").trim();
      if (!TRAFFIC_METRIC_KEYS.includes(key)) return;
      if (uniq.includes(key)) return;
      uniq.push(key);
    });
    TRAFFIC_METRIC_KEYS.forEach((key) => {
      if (uniq.length >= 2) return;
      if (!uniq.includes(key)) uniq.push(key);
    });
    return uniq.slice(0, 2);
  }

  function renderTrafficMetricPicker(client, selectedKeys) {
    const selected = new Set(selectedKeys);
    const subjectId = String(client.subject_id || "");
    return `
      <div
        class="settings-client-row__traffic-grid settings-client-row__traffic-grid--picker"
        data-settings-traffic-picker="${escapeHtml(subjectId)}"
        aria-label="${escapeHtml(t("inventory.traffic_metrics_aria"))}"
      >
        ${TRAFFIC_METRIC_KEYS.map((key) => {
          const active = selected.has(key);
          return `
            <button
              class="settings-client-row__traffic-item settings-client-row__traffic-choice${active ? " is-selected" : ""}"
              type="button"
              data-settings-traffic-choice="${escapeHtml(subjectId)}"
              data-metric="${escapeHtml(key)}"
              aria-pressed="${active ? "true" : "false"}"
              title="${escapeHtml(t("inventory.show_in_admin_title"))}"
            >
              <span>${escapeHtml(trafficMetricLabel(key))}</span>
              <strong class="mono">${escapeHtml(formatTrafficBytes(trafficMetricBytes(client, key)))}</strong>
            </button>
          `;
        }).join("")}
      </div>
    `;
  }

  function renderSettingsModeSelect(client) {
    const rawCurrent = String(client.desired_mode || client.applied_mode || "").toLowerCase();
    const subjectId = String(client.subject_id || "");
    const isXrayProfile = subjectDomainCategory(client) === "external_client" && String(client.implementation_kind || "").toLowerCase() === "xray";
    const modeSupportState = String(client.mode_support_state || "");
    const current = modeSupportState === "mixed"
      ? "mixed"
      : isXrayProfile && rawCurrent === "enabled" ? "vpn" : (rawCurrent || defaultEnabledModeFor(client));
    const supported = Array.isArray(client.supported_admin_modes) ? client.supported_admin_modes.map((x) => String(x).toLowerCase()) : null;
    const configured = supported ? supported.slice() : settingsModeOptions(client);
    const options = isXrayProfile ? configured.filter((mode) => ["vpn", "disabled"].includes(mode)) : configured;
    if (isXrayProfile && !options.length) options.push("vpn", "disabled");
    const legacyMode = isXrayProfile && ["direct", "selective"].includes(current);
    const legacyDirectSupported = current === "direct" && modeSupportState === "legacy_supported_direct";
    const unsupportedLegacy = current !== "mixed" && ((legacyMode && !legacyDirectSupported)
      || (modeSupportState === "unsupported_legacy" && !["vpn", "disabled"].includes(current))
      || (Boolean(supported) && current && !options.includes(current) && !legacyDirectSupported));
    const currentLabelBase = current === "mixed" ? t("inventory.mode_mixed") : modeLabel(current);
    const currentSuffix = unsupportedLegacy
      ? t("inventory.mode_unsupported_legacy")
      : legacyDirectSupported ? t("inventory.mode_legacy_preserved") : "";
    const currentLabel = `${currentLabelBase}${currentSuffix ? ` · ${currentSuffix}` : ""}`;
    const storedOptions = [...options];
    if (unsupportedLegacy && current && !storedOptions.includes(current)) storedOptions.unshift(current);
    if (legacyDirectSupported && !storedOptions.includes(current)) storedOptions.unshift(current);
    if (current === "mixed" && !storedOptions.includes(current)) storedOptions.unshift(current);
    return `
      <div class="settings-level-select settings-mode-select" data-settings-mode-root="${escapeHtml(subjectId)}" aria-label="${escapeHtml(t("inventory.mode_aria"))}">
        <select class="settings-mode-select__native" data-settings-mode-for="${escapeHtml(subjectId)}" data-settings-mode-original="${escapeHtml(rawCurrent)}" tabindex="-1" aria-hidden="true">
          ${storedOptions.map((mode) => `
          <option value="${escapeHtml(mode)}" ${current === mode ? "selected" : ""} ${(unsupportedLegacy || legacyDirectSupported || mode === "mixed") && mode === current ? `disabled data-legacy-current="true"` : ""}>${escapeHtml(mode === "mixed" ? t("inventory.mode_mixed") : modeLabel(mode))}${mode === current && currentSuffix ? ` · ${escapeHtml(currentSuffix)}` : ""}</option>
          `).join("")}
        </select>

        <button class="settings-level-select__trigger" type="button" data-settings-mode-trigger="${escapeHtml(subjectId)}" aria-haspopup="listbox" aria-expanded="false">
          <span class="settings-level-select__label" data-settings-mode-label="${escapeHtml(subjectId)}">${escapeHtml(currentLabel)}</span>
          <span class="settings-level-select__arrow" aria-hidden="true">▾</span>
        </button>

        <div class="settings-level-select__menu" role="listbox" hidden>
          ${options.map((mode) => `
            <button
              class="settings-level-select__option${current === mode ? " is-active" : ""}"
              type="button"
              role="option"
              data-settings-mode-value="${escapeHtml(subjectId)}"
              data-mode="${escapeHtml(mode)}"
              aria-selected="${current === mode ? "true" : "false"}"
            >${escapeHtml(modeLabel(mode))}</button>
          `).join("")}
        </div>
      </div>
    `;
  }

  function settingsClientActionAdapter(client) {
    const category = subjectDomainCategory(client);
    const implementation = String(client?.implementation_kind || "").toLowerCase();
    if (
      category === "external_client"
      && client?.is_aggregate
      && String(client?.aggregate_kind || "") === "xray_subscription"
      && Array.isArray(client?.subject_ids)
      && client.subject_ids.length
      && client?.can_delete
    ) {
      const subjectId = String(client.subject_id || "").trim();
      return subjectId ? { action: "xray_client_group", domain_category: category, id: subjectId } : null;
    }
    if (category === "external_client" && (implementation === "xray" || String(client?.inventory_role || "") === "vless_client")) {
      const clientId = String(client.client_id || client.client_uuid || client.subject_id || "").trim();
      return clientId ? { action: "xray_client", domain_category: category, id: clientId } : null;
    }
    if ((category === "service" || category === "infrastructure") && client?.can_delete) {
      const subjectId = String(client.subject_id || "").trim();
      return subjectId ? { action: "system_subject", domain_category: category, id: subjectId } : null;
    }
    return null;
  }

  function settingsDeleteAction(client) {
    return settingsClientActionAdapter(client);
  }

  function activityReasonLabel(client) {
    const reason = String(client?.activity_reason || "").trim();
    if (reason) {
      const key = `inventory.activity.${reason}`;
      const translated = t(key);
      if (translated !== key) return translated;
    }
    return String(client?.activity_reason_label || "").trim();
  }

  function renderSettingsClient(client, options) {
    const opts = options || {};
    const hiddenSubjectIds = opts.hiddenSubjectIds || new Set();
    const trafficPreferences = opts.trafficPreferences || {};
    const subjectId = String(client.subject_id || "");
    const hiddenInAdmin = hiddenSubjectIds.has(subjectId);
    const domainCategory = subjectDomainCategory(client);
    const subscriptionUrl = String(client.subscription_url || "").trim();
    const connectionUri = String(client.connection_uri || "").trim();
    const secondary = domainCategory === "external_client"
      ? (subscriptionUrl || connectionUri || client.subscription_path || subjectId)
      : [
          client.ip_address,
          client.mac_address,
          client.email,
          client.hostname,
          client.user_name,
        ].filter(Boolean).join(" · ");

    const trafficPref = metricPreferenceForClient(client, trafficPreferences);
    const deleteAction = settingsDeleteAction(client);
    const storedMode = String(client.desired_mode || client.applied_mode || "").toLowerCase();
    const currentMode = domainCategory === "external_client" && String(client.implementation_kind || "").toLowerCase() === "xray" && storedMode === "enabled" ? "vpn" : storedMode;
    const disabledByMode = currentMode === "disabled";
    const restoreMode = domainCategory === "external_client" && String(client.implementation_kind || "").toLowerCase() === "xray"
      ? "vpn"
      : defaultEnabledModeFor(client);
    const activityLabel = activityReasonLabel(client);
    const implementation = implementationLabel(client);
    const subscriptionStatus = client.aggregate_kind === "xray_subscription"
      && typeof client.subscription_enabled === "boolean"
      ? t(client.subscription_enabled ? "inventory.subscription.enabled" : "inventory.subscription.disabled")
      : "";
    const health = client.health && typeof client.health === "object" ? client.health : {};
    const evidencePending = health.reason === "HEALTH_EVIDENCE_NOT_LOADED"
      && currentMode !== "disabled"
      && client.is_active !== false;
    const uxState = evidencePending
      ? { state: "pending", severity: "info", label: t("inventory.health.loading"), summary: t("inventory.health.loading"), action: "" }
      : health.reason === "HEALTH_EVIDENCE_UNAVAILABLE"
        ? { state: "unknown", severity: "info", label: t("inventory.health.unavailable"), summary: t("inventory.health.unavailable"), action: "" }
      : presentationState({
        ...health,
        desired_mode: currentMode,
        is_active: client.is_active,
        entity_type: domainCategory === "external_client" ? "xray" : domainCategory,
      });
    const stateClass = presentationLevelClass(uxState);
    const observation = client.observation && typeof client.observation === "object" ? client.observation : {};
    const activityTimestamp = client.last_activity_at || client.last_seen_at;
    const isExternalClient = domainCategory === "external_client";
    const lastSeenFreshness = activityTimestamp ? freshnessFor(activityTimestamp, {
      absolute: isExternalClient,
      seconds: isExternalClient,
      stale: Boolean(observation.stale) || String(client.activity_reason || "") === "stale_seen",
      stale_after: observation.stale_after,
    }) : (isExternalClient ? freshnessFor(null) : null);
    const infoItems = [
      [t("inventory.info.type"), domainCategoryLabel(domainCategory)],
      implementation ? [t("inventory.info.implementation"), implementation] : null,
      subscriptionStatus ? [t("inventory.info.subscription"), subscriptionStatus] : null,
      [t("inventory.info.effective"), displayXrayModeLabel(client, client.effective_mode || client.applied_mode || client.desired_mode)],
      [t("inventory.info.policy"), displayXrayModeLabel(client, client.committed_desired_mode || client.desired_mode)],
      [t("inventory.info.source"), sourceLabel(client.mode_source)],
      [t("inventory.info.state"), uxState.summary, "", "", false, "health_summary"],
      [t("journal.field.recommended_action"), uxState.action, "", "", !uxState.action, "health_action"],
      [t("inventory.info.activity"), activityLabel || t("inventory.activity.unknown"), "", "", !activityLabel, "activity_label"],
      lastSeenFreshness ? [t(isExternalClient ? "inventory.info.last_activity" : "inventory.info.last_seen"), lastSeenFreshness.text, lastSeenFreshness.state, isExternalClient ? (activityTimestamp || "") : "", false, isExternalClient ? "activity_time" : ""] : null,
      client.is_internal ? [t("inventory.info.system"), t("inventory.yes")] : null,
    ].filter(Boolean);

    return `
      <div class="settings-client-row settings-client-row--${escapeHtml(domainCategory)}" data-settings-client-row="${escapeHtml(subjectId)}" data-health-state="${escapeHtml(uxState.state)}">
        <div class="settings-client-row__main">
          <div class="settings-client-row__head">
            <div class="settings-client-row__title-wrap">
              <div class="settings-client-row__title">${escapeHtml(client.display_name || subjectId || t("inventory.client"))}</div>
              <div class="settings-client-row__meta-wrap">
                <div class="settings-client-row__meta muted mono" title="${escapeHtml(secondary || subjectId || "—")}">${escapeHtml(secondary || subjectId || "—")}</div>
                ${deleteAction && domainCategory === "external_client" ? `
                  <button class="btn btn--danger settings-client-row__delete-near-link" type="button" data-settings-delete-kind="${escapeHtml(deleteAction.action)}" data-settings-delete-id="${escapeHtml(deleteAction.id)}">${escapeHtml(t("inventory.delete"))}</button>
                ` : ""}
              </div>
            </div>
            <div class="settings-client-row__badges">
              <span class="pill">${escapeHtml(domainCategoryLabel(domainCategory))}</span>
              <span
                class="pill settings-client-row__status settings-event__level--${escapeHtml(stateClass)} ${uxState.state === "healthy" ? " is-active" : " is-inactive"}"
                title="${escapeHtml(activityLabel || t("inventory.availability_title"))}"
              >${escapeHtml(uxState.label)}</span>
              <button
                class="pill settings-client-row__admin-visibility${hiddenInAdmin ? " is-hidden" : " is-shown"}"
                type="button"
                data-settings-admin-visibility="${escapeHtml(subjectId)}"
                aria-pressed="${hiddenInAdmin ? "false" : "true"}"
                title="${escapeHtml(t("inventory.visibility_title"))}"
              >${escapeHtml(hiddenInAdmin ? t("inventory.hidden") : t("inventory.in_admin"))}</button>
              <button
                class="pill settings-client-row__power${disabledByMode ? " is-off" : " is-on"}"
                type="button"
                data-settings-power-toggle="${escapeHtml(subjectId)}"
                data-enabled="${disabledByMode ? "0" : "1"}"
                data-restore-mode="${escapeHtml(currentMode && currentMode !== "disabled" ? currentMode : restoreMode)}"
                aria-pressed="${disabledByMode ? "false" : "true"}"
                title="${escapeHtml(t("inventory.power_title"))}"
              >${escapeHtml(disabledByMode ? t("inventory.power_off") : t("inventory.power_on"))}</button>
            </div>
          </div>

          <div class="settings-client-row__info">
            ${infoItems.map(([label, value, freshnessState, semanticTime, hidden, field]) => `
              <div class="settings-client-row__info-item"${field ? ` data-settings-${escapeHtml(field)}=""` : ""}${hidden ? " hidden" : ""}>
                <span>${escapeHtml(label)}</span>
                <strong ${freshnessState ? `class="settings-freshness settings-freshness--${escapeHtml(freshnessState)}"` : ""}>${field === "activity_time" ? `<time data-settings-activity-time datetime="${escapeHtml(semanticTime)}">${escapeHtml(value || "—")}</time>` : escapeHtml(value || "—")}</strong>
              </div>
            `).join("")}
          </div>

          ${renderTrafficMetricPicker(client, trafficPref)}

          <div class="settings-client-row__actions">
            <input
              class="input input--mono settings-client-row__alias"
              data-settings-alias-for="${escapeHtml(subjectId)}"
              value="${escapeHtml(String(client.alias || client.display_name || ""))}"
              placeholder="${escapeHtml(t("inventory.local_name"))}"
            />

            ${renderSettingsModeSelect(client)}

            <div class="settings-client-row__buttons">
              ${deleteAction && domainCategory !== "external_client" ? `
                <button
                  class="btn btn--danger"
                  type="button"
                  data-settings-delete-kind="${escapeHtml(deleteAction.action)}"
                  data-settings-delete-id="${escapeHtml(deleteAction.id)}"
                >${escapeHtml(t("inventory.delete"))}</button>
              ` : ""}
              <button class="btn btn--primary" type="button" data-settings-save-item="${escapeHtml(subjectId)}">${escapeHtml(t("inventory.save"))}</button>
            </div>
          </div>

        </div>
      </div>
    `;
  }

  function displayXrayMode(client, value) {
    const mode = String(value || "").toLowerCase();
    return subjectDomainCategory(client) === "external_client"
      && String(client?.implementation_kind || "").toLowerCase() === "xray"
      && mode === "enabled"
      ? "vpn"
      : mode;
  }

  function displayXrayModeLabel(client, value) {
    if (String(client?.mode_support_state || "") === "mixed") return t("inventory.mode_mixed");
    return modeLabel(displayXrayMode(client, value));
  }

  function renderSettingsClientsHtml(items, options) {
    return (Array.isArray(items) ? items : [])
      .map((client) => renderSettingsClient(client, options))
      .join("");
  }

  function mergeHealthItemsBySubjectId(targetItems, sourceItems) {
    const sourceById = new Map((Array.isArray(sourceItems) ? sourceItems : [])
      .map((item) => [String(item?.subject_id || ""), item])
      .filter(([id]) => Boolean(id)));
    let changed = false;
    (Array.isArray(targetItems) ? targetItems : []).forEach((target) => {
      const source = sourceById.get(String(target?.subject_id || ""));
      if (!source) return;
      ["health", "presence", "observation", "projection", "reconcile", "live_state", "online", "activity_reason", "activity_reason_label", "last_activity_at", "last_seen_at"].forEach((key) => {
        if (source[key] !== undefined) target[key] = source[key];
      });
      changed = true;
    });
    return changed;
  }

  function updateSettingsClientHealthRow(row, client) {
    if (!row || !client) return;
    const health = client.health && typeof client.health === "object" ? client.health : {};
    const pending = health.reason === "HEALTH_EVIDENCE_NOT_LOADED"
      && String(client.desired_mode || client.applied_mode || "").toLowerCase() !== "disabled"
      && client.is_active !== false;
    const ux = pending
      ? { state: "pending", severity: "info", label: t("inventory.health.loading"), summary: t("inventory.health.loading"), action: "" }
      : health.reason === "HEALTH_EVIDENCE_UNAVAILABLE"
        ? { state: "unknown", severity: "info", label: t("inventory.health.unavailable"), summary: t("inventory.health.unavailable"), action: "" }
      : presentationState({
        ...health,
        desired_mode: client.desired_mode || client.applied_mode,
        is_active: client.is_active,
        entity_type: client.entity_type || client.domain_category,
      });
    row.dataset.healthState = ux.state;
    const badge = row.querySelector(".settings-client-row__status");
    if (badge) {
      badge.textContent = ux.label;
      badge.className = `pill settings-client-row__status settings-event__level--${presentationLevelClass(ux)}${ux.state === "healthy" ? " is-active" : " is-inactive"}`;
    }
    const summary = row.querySelector("[data-settings-health_summary] strong");
    if (summary) summary.textContent = ux.summary;
    const action = row.querySelector("[data-settings-health_action]");
    if (action) {
      action.hidden = !ux.action;
      const value = action.querySelector("strong");
      if (value) value.textContent = ux.action;
    }
    const activityLabel = row.querySelector("[data-settings-activity_label]");
    if (activityLabel) {
      const reason = String(client.activity_reason || "").trim();
      const translated = reason ? t(`inventory.activity.${reason}`) : "";
      const label = translated && translated !== `inventory.activity.${reason}`
        ? translated
        : String(client.activity_reason_label || "").trim();
      activityLabel.hidden = !label;
      const value = activityLabel.querySelector("strong");
      if (value) value.textContent = label || t("inventory.activity.unknown");
    }
    const activityTime = row.querySelector("[data-settings-activity-time]");
    if (activityTime) {
      const timestamp = client.last_activity_at || client.last_seen_at;
      const freshness = timestamp
        ? freshnessFor(timestamp, { absolute: true, seconds: true, stale: Boolean(client.observation?.stale) || client.activity_reason === "stale_seen", stale_after: client.observation?.stale_after })
        : freshnessFor(null);
      activityTime.dateTime = timestamp || "";
      activityTime.textContent = freshness.text;
      activityTime.className = `settings-freshness settings-freshness--${freshness.state}`;
    }
  }

  function renderSettingsCounts(counts) {
    const safe = counts || {};
    return t("inventory.counts", {
      all: safe.all || 0,
      lan: safe.lan_client ?? 0,
      external_network_source: safe.external_network_source ?? 0,
      external_client: safe.external_client ?? safe.vless_client ?? 0,
      service: safe.service ?? Number(safe.docker_runtime ?? safe.docker ?? 0) + Number(safe.host_runtime ?? safe.host ?? 0),
      infrastructure: safe.infrastructure ?? safe.router_core ?? 0,
    });
  }

  window.FwrouterSettingsInventory = {
    TRAFFIC_METRIC_KEYS,
    normalizeTrafficPreferences,
    metricPreferenceForClient,
    trafficMetricBytes,
    settingsClientActionAdapter,
    renderSettingsClientsHtml,
    mergeHealthItemsBySubjectId,
    updateSettingsClientHealthRow,
    renderSettingsCounts,
  };
})();
