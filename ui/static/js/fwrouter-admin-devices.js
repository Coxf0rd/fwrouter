// Admin devices/Vless rendering helpers.
(function () {
  const t = (key, params) => window.FwrouterI18n?.t(key, params) || key;
  const {
    escapeHtml,
    trafficMetricLabel,
    formatTrafficBytes,
  } = window.FwrouterUI;
  const {
    compactModeLabel: modeLabel,
    compactSourceLabel: sourceLabel,
  } = window.FwrouterLabels;

  function renderTrafficMetricPair(metrics) {
    const items = Array.isArray(metrics) ? metrics : [];
    if (!items.length) {
      return `
        <div class="device-row__traffic-grid">
          <div class="device-row__traffic-item">
            <span class="device-row__traffic-label">${escapeHtml(t("traffic.generic"))}</span>
            <strong class="mono">0 B</strong>
          </div>
        </div>
      `;
    }

    return `
      <div class="device-row__traffic-grid">
        ${items.map((item) => `
          <div class="device-row__traffic-item">
            <span class="device-row__traffic-label">${escapeHtml(item?.label || trafficMetricLabel(item?.key))}</span>
            <strong class="mono">${escapeHtml(formatTrafficBytes(item?.bytes || 0))}</strong>
          </div>
        `).join("")}
      </div>
    `;
  }

  function renderDeviceIcon(isExternalNetwork) {
    if (isExternalNetwork) {
      return `
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <circle cx="8" cy="8" r="2.3" fill="currentColor"></circle>
          <circle cx="15.8" cy="6.2" r="2.3" fill="currentColor"></circle>
          <circle cx="16" cy="15.8" r="2.3" fill="currentColor"></circle>
          <circle cx="8.2" cy="17.6" r="2.3" fill="currentColor"></circle>
          <path d="M9.8 8.2l3.8-.9M15.9 8.5v5M9.7 16.9l4-1.1M9.2 10l-1 5.1" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"></path>
        </svg>
      `;
    }

    return `
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <rect x="4.5" y="5.5" width="15" height="10" rx="2.4" fill="none" stroke="currentColor" stroke-width="1.7"></rect>
        <path d="M9 18.5h6M10.6 15.7v2.8M13.4 15.7v2.8" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"></path>
      </svg>
    `;
  }

  function renderVlessIcon() {
    return `
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <path d="M7.4 5.2h9.2a2.2 2.2 0 0 1 2.2 2.2v9.2a2.2 2.2 0 0 1-2.2 2.2H7.4a2.2 2.2 0 0 1-2.2-2.2V7.4a2.2 2.2 0 0 1 2.2-2.2Z" fill="none" stroke="currentColor" stroke-width="1.7"></path>
        <path d="M8.4 9.2h7.2M8.4 12h7.2M8.4 14.8h4.8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"></path>
      </svg>
    `;
  }

  function renderSystemIcon(kind) {
    if (kind === "docker") {
      return `
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <rect x="4.5" y="9" width="15" height="8.5" rx="2.2" fill="none" stroke="currentColor" stroke-width="1.7"></rect>
          <path d="M7.2 9V6.4h3V9M11.4 9V5.4h3V9M15.6 9V7.2h2.2V9" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"></path>
        </svg>
      `;
    }
    if (kind === "host") {
      return `
        <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <rect x="5.5" y="4.5" width="13" height="15" rx="2.2" fill="none" stroke="currentColor" stroke-width="1.7"></rect>
          <path d="M8.5 8.5h7M8.5 12h7M8.5 15.5h3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"></path>
        </svg>
      `;
    }
    return renderDeviceIcon(false);
  }

  function getVlessClientId(item) {
    return String(item?.id || item?.uuid || item?.client_id || item?.email || "").trim();
  }

  function renderAdminVlessClientsHtml(clients) {
    const items = Array.isArray(clients) ? clients : [];
    if (!items.length) return `<div class="empty">${escapeHtml(t("admin.devices.no_vless"))}</div>`;

    return items.map((client) => {
      const id = getVlessClientId(client);
      const label = client.local_name || client.name || client.email || id || t("admin.devices.vless_client");
      const displayId = client.email || client.uuid || id;
      const trafficHtml = renderTrafficMetricPair(client.traffic_panel_metrics);
      const enabledLabel = client.enabled ? t("admin.devices.enabled") : t("admin.devices.disabled");
      const lastSeen = client.last_seen ? ` · ${escapeHtml(client.last_seen)}` : "";
      const aggregateControls = client.is_aggregate
        ? `<div class="muted">${escapeHtml(t("admin.devices.subscription_group"))}</div>`
        : `
              <input
                class="input input--mono"
                data-admin-vless-name-for="${escapeHtml(id)}"
                value="${escapeHtml(client.local_name || client.name || "")}"
                placeholder="${escapeHtml(t("admin.devices.client_local_name"))}"
              />

              <button class="btn" data-admin-save-vless-name="${escapeHtml(id)}" type="button">
                ${escapeHtml(t("inventory.save"))}
              </button>

              <button class="btn btn--danger device-row__delete" data-admin-delete-vless="${escapeHtml(id)}" type="button">
                ${escapeHtml(t("inventory.delete"))}
              </button>
            `;

      return `
        <div class="device-row device-row--vless" data-vless-client="${escapeHtml(id)}">
          <div class="device-row__icon device-row__icon--vless" aria-hidden="true">
            ${renderVlessIcon()}
          </div>

          <div class="device-row__main">
            <div class="device-row__head">
              <div class="device-title">${escapeHtml(label)}</div>
              <div class="muted mono device-row__meta">
                ${escapeHtml(displayId)} · ${escapeHtml(enabledLabel)}${lastSeen}
              </div>
            </div>

            ${trafficHtml}

            <div class="device-actions device-actions--vless">
              ${aggregateControls}
            </div>
          </div>
        </div>
      `;
    }).join("");
  }

  function settingsVisibilityValue(visibility, key) {
    if (key && Object.prototype.hasOwnProperty.call(visibility, key)) {
      return Boolean(visibility[key]);
    }
    return true;
  }

  function settingsSlug(value) {
    return String(value || "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 64);
  }

  function externalNetworkSystemId(device) {
    const explicit = settingsSlug(device?.display_system_id);
    if (explicit) return explicit;
    const implementation = String(device?.implementation_kind || device?.subject_type || "").trim().toLowerCase();
    const slug = settingsSlug(implementation || "external");
    return slug ? `external-network-${slug}` : "";
  }

  function settingsItemVisible(device, displaySettings) {
    const settings = displaySettings || {};
    const visibility = settings.system_visibility && typeof settings.system_visibility === "object"
      ? settings.system_visibility
      : {};
    const kind = String(device?.inventory_role || "");
    const visibilityKey = ({
      lan_client: "lan",
      external_network_source: "external_network_source",
      vless_client: "vless_client",
      docker_runtime: "docker",
      host_runtime: "host",
    }[kind] || kind);
    if (!settingsVisibilityValue(visibility, visibilityKey)) {
      return false;
    }
    if (kind === "external_network_source") {
      const concreteKey = externalNetworkSystemId(device);
      return settingsVisibilityValue(visibility, concreteKey);
    }
    return true;
  }

  function splitDevices(devices, displaySettings) {
    const list = (Array.isArray(devices) ? devices : [])
      .filter((d) => settingsItemVisible(d, displaySettings));
    return {
      lan: list.filter((d) => String(d.inventory_role || "") === "lan_client"),
      externalNetwork: list.filter((d) => String(d.inventory_role || "") === "external_network_source"),
      docker: list.filter((d) => String(d.inventory_role || "") === "docker_runtime"),
      host: list.filter((d) => String(d.inventory_role || "") === "host_runtime"),
    };
  }

  function renderAdminDeviceRows(devices, cleanHostname) {
    const items = Array.isArray(devices) ? devices : [];
    if (!items.length) return `<div class="empty">${escapeHtml(t("admin.devices.no_active"))}</div>`;

    return items.map((d) => {
      const mode = d.override ? d.override : "GLOBAL";
      const label = d.name || cleanHostname(d.hostname) || d.ip || "";
      const hasMac = !!(d.mac && d.mac.length);
      const isExternalNetwork = String(d.inventory_role || "") === "external_network_source";
      const subjectType = String(d.subject_type || (isExternalNetwork ? "tailscale" : "lan")).toLowerCase();
      const isSystem = subjectType === "docker" || subjectType === "host";
      const subjectId = String(d.id || "");

      const metaParts = [];
      if (d.ip) metaParts.push(escapeHtml(d.ip));
      if (d.mac) metaParts.push(escapeHtml(d.mac));
      if (isSystem && d.hostname) metaParts.push(escapeHtml(d.hostname));
      const meta = metaParts.join(" · ");
      const trafficHtml = renderTrafficMetricPair(d.traffic_panel_metrics);

      const iconClass = isSystem
        ? `device-row__icon device-row__icon--${escapeHtml(subjectType)}`
        : isExternalNetwork
        ? "device-row__icon device-row__icon--external-network"
        : "device-row__icon device-row__icon--lan";

      const nameControl = hasMac
        ? `<input class="input input--mono" data-admin-alias-for="${escapeHtml(subjectId)}" data-initial-value="${escapeHtml(String(d.name || ""))}" value="${escapeHtml(d.name || "")}" placeholder="${escapeHtml(t("admin.devices.device_name"))}" />`
        : (
            isExternalNetwork
              ? `<div class="muted device-row__readonly">${escapeHtml(t("admin.devices.external_network_name"))}</div>`
              : `<input class="input input--mono" data-admin-alias-for="${escapeHtml(subjectId)}" data-initial-value="${escapeHtml(String(d.name || ""))}" value="${escapeHtml(d.name || "")}" placeholder="${escapeHtml(t("admin.devices.device_name"))}" />`
          );

      return `
        <div class="device-row" data-admin-device-row="${escapeHtml(subjectId)}">
          <div class="${iconClass}" aria-hidden="true">
            ${isSystem ? renderSystemIcon(subjectType) : renderDeviceIcon(isExternalNetwork)}
          </div>

          <div class="device-row__main">
            <div class="device-row__head">
              <div class="device-title">${escapeHtml(label)}</div>
              <div class="muted mono device-row__meta">${meta}</div>
            </div>

            ${trafficHtml}

            <div class="muted settings-client-row__foot">
              ${escapeHtml(t("admin.devices.policy_line", {
                policy: modeLabel(d.desired_mode || mode),
                current: modeLabel(d.effective_mode || mode),
                source: sourceLabel(d.mode_source || "GLOBAL"),
              }))}
            </div>

            <div class="device-actions">
              ${nameControl}

              <select class="input" data-admin-device="${escapeHtml(subjectId)}" data-initial-value="${escapeHtml(mode)}">
                <option value="GLOBAL" ${mode === "GLOBAL" ? "selected" : ""}>Global</option>
                <option value="DIRECT" ${mode === "DIRECT" ? "selected" : ""}>Direct</option>
                <option value="VPN" ${mode === "VPN" ? "selected" : ""}>VPN</option>
                <option value="SELECTIVE" ${mode === "SELECTIVE" ? "selected" : ""}>Selective</option>
              </select>

              <button class="btn" type="button" data-admin-save-device="${escapeHtml(subjectId)}" disabled>${escapeHtml(t("inventory.save"))}</button>
            </div>
          </div>
        </div>
      `;
    }).join("");
  }

  window.FwrouterAdminDevices = {
    splitDevices,
    renderAdminDeviceRows,
    renderAdminVlessClientsHtml,
  };
})();
