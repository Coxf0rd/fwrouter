// Admin devices/VLESS rendering helpers.
(function () {
  const {
    escapeHtml,
    trafficMetricLabel,
    formatTrafficBytes,
  } = window.FwrouterUI;
  const {
    compactModeLabel: modeLabel,
    compactSourceLabel: sourceLabel,
  } = window.FwrouterLabels;

  function isTailscaleIp(ip) {
    if (!ip) return false;
    return String(ip).startsWith("100.64.");
  }

  function renderTrafficMetricPair(metrics) {
    const items = Array.isArray(metrics) ? metrics : [];
    if (!items.length) {
      return `
        <div class="device-row__traffic-grid">
          <div class="device-row__traffic-item">
            <span class="device-row__traffic-label">Traffic</span>
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

  function renderDeviceIcon(isTs) {
    if (isTs) {
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

  function getVlessClientId(item) {
    return String(item?.id || item?.uuid || item?.client_id || item?.email || "").trim();
  }

  function renderAdminVlessClientsHtml(clients) {
    const items = Array.isArray(clients) ? clients : [];
    if (!items.length) return '<div class="empty">Нет VLESS клиентов</div>';

    return items.map((client) => {
      const id = getVlessClientId(client);
      const label = client.local_name || client.name || client.email || id || "VLESS клиент";
      const displayId = client.email || client.uuid || id;
      const trafficHtml = renderTrafficMetricPair(client.traffic_panel_metrics);
      const enabledLabel = client.enabled ? "активен" : "отключён";
      const lastSeen = client.last_seen ? ` · ${escapeHtml(client.last_seen)}` : "";
      const aggregateControls = client.is_aggregate
        ? '<div class="muted">Группа профилей подписки</div>'
        : `
              <input
                class="input input--mono"
                data-admin-vless-name-for="${escapeHtml(id)}"
                value="${escapeHtml(client.local_name || client.name || "")}"
                placeholder="Локальное имя клиента"
              />

              <button class="btn" data-admin-save-vless-name="${escapeHtml(id)}" type="button">
                Сохранить
              </button>

              <button class="btn btn--danger device-row__delete" data-admin-delete-vless="${escapeHtml(id)}" type="button">
                Удалить
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

  function settingsKindVisible(kind, displaySettings) {
    const settings = displaySettings || {};
    if (kind === "lan") return Boolean(settings.show_lan);
    if (kind === "tailscale") return Boolean(settings.show_tailscale);
    if (kind === "xray") return Boolean(settings.show_xray);
    return true;
  }

  function splitDevices(devices, displaySettings) {
    const list = (Array.isArray(devices) ? devices : [])
      .filter((d) => settingsKindVisible(d.subject_type === "tailscale" ? "tailscale" : "lan", displaySettings));
    return {
      lan: list.filter((d) => !isTailscaleIp(d.ip)),
      ts: list.filter((d) => isTailscaleIp(d.ip)),
    };
  }

  function renderAdminDeviceRows(devices, cleanHostname) {
    const items = Array.isArray(devices) ? devices : [];
    if (!items.length) return '<div class="empty">Нет активных устройств</div>';

    return items.map((d) => {
      const mode = d.override ? d.override : "GLOBAL";
      const label = d.name || cleanHostname(d.hostname) || d.ip || "";
      const hasMac = !!(d.mac && d.mac.length);
      const isTs = isTailscaleIp(d.ip);
      const subjectId = String(d.id || "");

      const metaParts = [];
      if (d.ip) metaParts.push(escapeHtml(d.ip));
      if (d.mac) metaParts.push(escapeHtml(d.mac));
      const meta = metaParts.join(" · ");
      const trafficHtml = renderTrafficMetricPair(d.traffic_panel_metrics);

      const iconClass = isTs
        ? "device-row__icon device-row__icon--ts"
        : "device-row__icon device-row__icon--lan";

      const nameControl = hasMac
        ? `<input class="input input--mono" data-admin-alias-for="${escapeHtml(subjectId)}" data-initial-value="${escapeHtml(String(d.name || ""))}" value="${escapeHtml(d.name || "")}" placeholder="Имя устройства" />`
        : (
            isTs
              ? `<div class="muted device-row__readonly">Имя берётся из Tailscale</div>`
              : `<input class="input input--mono" data-admin-alias-for="${escapeHtml(subjectId)}" data-initial-value="${escapeHtml(String(d.name || ""))}" value="${escapeHtml(d.name || "")}" placeholder="Имя устройства" />`
          );

      return `
        <div class="device-row" data-admin-device-row="${escapeHtml(subjectId)}">
          <div class="${iconClass}" aria-hidden="true">
            ${renderDeviceIcon(isTs)}
          </div>

          <div class="device-row__main">
            <div class="device-row__head">
              <div class="device-title">${escapeHtml(label)}</div>
              <div class="muted mono device-row__meta">${meta}</div>
            </div>

            ${trafficHtml}

            <div class="muted settings-client-row__foot">
              Политика: ${escapeHtml(modeLabel(d.desired_mode || mode))} · Сейчас: ${escapeHtml(modeLabel(d.effective_mode || mode))} · ${escapeHtml(sourceLabel(d.mode_source || "GLOBAL"))}
            </div>

            <div class="device-actions">
              ${nameControl}

              <select class="input" data-admin-device="${escapeHtml(subjectId)}" data-initial-value="${escapeHtml(mode)}">
                <option value="GLOBAL" ${mode === "GLOBAL" ? "selected" : ""}>Global</option>
                <option value="DIRECT" ${mode === "DIRECT" ? "selected" : ""}>Direct</option>
                <option value="VPN" ${mode === "VPN" ? "selected" : ""}>VPN</option>
                <option value="SELECTIVE" ${mode === "SELECTIVE" ? "selected" : ""}>Selective</option>
              </select>

              <button class="btn" type="button" data-admin-save-device="${escapeHtml(subjectId)}" disabled>Сохранить</button>
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
