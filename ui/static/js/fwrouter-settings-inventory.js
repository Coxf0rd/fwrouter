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
    settingsSubjectKindLabel: subjectKindLabel,
    settingsModeLabel: modeLabel,
    settingsSourceLabel: sourceLabel,
    runtimeLabel,
    settingsModeOptions,
    defaultEnabledModeFor,
  } = window.FwrouterLabels;
  const { formatTs } = window.FwrouterSettingsEvents;

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
    const options = settingsModeOptions(client);
    const current = options.includes(rawCurrent) ? rawCurrent : defaultEnabledModeFor(client);
    const currentLabel = modeLabel(current);
    return `
      <div class="settings-level-select settings-mode-select" data-settings-mode-root="${escapeHtml(subjectId)}" aria-label="${escapeHtml(t("inventory.mode_aria"))}">
        <select class="settings-mode-select__native" data-settings-mode-for="${escapeHtml(subjectId)}" tabindex="-1" aria-hidden="true">
          ${options.map((mode) => `
          <option value="${escapeHtml(mode)}" ${current === mode ? "selected" : ""}>${escapeHtml(modeLabel(mode))}</option>
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

  function settingsDeleteAction(client) {
    const kind = String(client.kind || "").toLowerCase();
    if (client.is_aggregate) return null;
    if (kind === "xray") {
      const clientId = String(client.client_id || client.client_uuid || client.subject_id || "").trim();
      return clientId ? { kind: "xray", id: clientId } : null;
    }
    if ((kind === "docker" || kind === "host") && client.can_delete) {
      const subjectId = String(client.subject_id || "").trim();
      return subjectId ? { kind: "system", id: subjectId } : null;
    }
    return null;
  }

  function renderSettingsClient(client, options) {
    const opts = options || {};
    const hiddenSubjectIds = opts.hiddenSubjectIds || new Set();
    const trafficPreferences = opts.trafficPreferences || {};
    const subjectId = String(client.subject_id || "");
    const hiddenInAdmin = hiddenSubjectIds.has(subjectId);
    const secondary = [
      client.ip_address,
      client.mac_address,
      client.email,
      client.hostname,
      client.user_name,
    ].filter(Boolean).join(" · ");

    const trafficPref = metricPreferenceForClient(client, trafficPreferences);
    const deleteAction = settingsDeleteAction(client);
    const currentMode = String(client.desired_mode || client.applied_mode || "").toLowerCase();
    const disabledByMode = currentMode === "disabled";
    const available = Boolean(client.is_active);
    const infoItems = [
      [t("inventory.info.type"), subjectKindLabel(client.kind)],
      [t("inventory.info.effective"), modeLabel(client.effective_mode || client.applied_mode || client.desired_mode)],
      [t("inventory.info.policy"), modeLabel(client.committed_desired_mode || client.desired_mode)],
      [t("inventory.info.source"), sourceLabel(client.mode_source)],
      [t("inventory.info.state"), runtimeLabel(client.runtime_state || (client.is_active ? "active" : "inactive"))],
      client.activity_reason_label ? [t("inventory.info.activity"), client.activity_reason_label] : null,
      client.last_seen_at ? [t("inventory.info.last_seen"), formatTs(client.last_seen_at)] : null,
      client.is_internal ? [t("inventory.info.system"), t("inventory.yes")] : null,
    ].filter(Boolean);

    return `
      <div class="settings-client-row settings-client-row--${escapeHtml(String(client.kind || "unknown"))}" data-settings-client-row="${escapeHtml(subjectId)}">
        <div class="settings-client-row__main">
          <div class="settings-client-row__head">
            <div class="settings-client-row__title-wrap">
              <div class="settings-client-row__title">${escapeHtml(client.display_name || subjectId || t("inventory.client"))}</div>
              <div class="settings-client-row__meta muted mono">${escapeHtml(secondary || subjectId || "—")}</div>
            </div>
            <div class="settings-client-row__badges">
              <span class="pill">${escapeHtml(subjectKindLabel(client.kind))}</span>
              <span
                class="pill settings-client-row__status${available ? " is-active" : " is-inactive"}"
                title="${escapeHtml(client.activity_reason_label || t("inventory.availability_title"))}"
              >${escapeHtml(available ? t("inventory.active") : t("inventory.inactive"))}</span>
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
                data-restore-mode="${escapeHtml(currentMode && currentMode !== "disabled" ? currentMode : defaultEnabledModeFor(client))}"
                aria-pressed="${disabledByMode ? "false" : "true"}"
                title="${escapeHtml(t("inventory.power_title"))}"
              >${escapeHtml(disabledByMode ? t("inventory.power_off") : t("inventory.power_on"))}</button>
            </div>
          </div>

          <div class="settings-client-row__info">
            ${infoItems.map(([label, value]) => `
              <div class="settings-client-row__info-item">
                <span>${escapeHtml(label)}</span>
                <strong>${escapeHtml(value || "—")}</strong>
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
              <button class="btn" type="button" data-settings-save-item="${escapeHtml(subjectId)}">${escapeHtml(t("inventory.save"))}</button>
              ${deleteAction ? `
                <button
                  class="btn btn--danger"
                  type="button"
                  data-settings-delete-kind="${escapeHtml(deleteAction.kind)}"
                  data-settings-delete-id="${escapeHtml(deleteAction.id)}"
                >${escapeHtml(t("inventory.delete"))}</button>
              ` : ""}
            </div>
          </div>

        </div>
      </div>
    `;
  }

  function renderSettingsClientsHtml(items, options) {
    return (Array.isArray(items) ? items : [])
      .map((client) => renderSettingsClient(client, options))
      .join("");
  }

  function renderSettingsCounts(counts) {
    const safe = counts || {};
    return t("inventory.counts", {
      all: safe.all || 0,
      lan: safe.lan || 0,
      tailscale: safe.tailscale || 0,
      xray: safe.xray || 0,
      docker: safe.docker || 0,
      host: safe.host || 0,
    });
  }

  window.FwrouterSettingsInventory = {
    TRAFFIC_METRIC_KEYS,
    normalizeTrafficPreferences,
    metricPreferenceForClient,
    trafficMetricBytes,
    renderSettingsClientsHtml,
    renderSettingsCounts,
  };
})();
