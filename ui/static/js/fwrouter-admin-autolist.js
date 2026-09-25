// Admin VPN-auto table rendering helpers.
(function () {
  const t = (key, params) => window.FwrouterI18n?.t(key, params) || key;
  const {
    escapeHtml,
    countryCodeToFlagEmoji,
    flagEmojiToCountryCode,
    stripLeadingFlagEmoji,
  } = window.FwrouterUI;

  function formatPing(delay) {
    if (window.FwrouterPingSelect?.formatPingValue) {
      return window.FwrouterPingSelect.formatPingValue(delay);
    }
    if (typeof delay === "number" && delay > 0) return `${delay} ms`;
    if (delay === 0 || delay === -1) return "timeout";
    return "—";
  }

  function renderPing(delay, status, pending) {
    if (window.FwrouterPingSelect?.renderPingCell) {
      return window.FwrouterPingSelect.renderPingCell({ pending, delay, status });
    }
    if (pending) return '<span class="ping-spinner" aria-hidden="true"></span>';
    return escapeHtml(formatPing(delay));
  }

  function renderEffectiveLatency(delay, status, pending) {
    if (pending) return '<span class="ping-spinner" role="status" aria-label="' + escapeHtml(t("manual_check.loading")) + '"></span>';
    const value = String(status || "unknown").toLowerCase();
    if ((value === "usable" || value === "healthy") && typeof delay === "number" && delay >= 0) {
      return `<span class="ping-status ping-status--value">${escapeHtml(`${delay} ms`)}</span>`;
    }
    const label = value === "failed" || value === "unavailable"
      ? t("admin.autolist.member_unavailable_timeout")
      : t("admin.autolist.latency_unavailable");
    return `<span class="ping-status ping-status--value">${escapeHtml(label)}</span>`;
  }

  function topologyStatusKey(status) {
    const value = String(status || "unknown").toLowerCase();
    return ["healthy", "failed", "unknown", "stale", "unsupported"].includes(value)
      ? `admin.autolist.member_status.${value}`
      : "admin.autolist.member_status.unknown";
  }

  function logicalHealthKey(status) {
    const value = String(status || "unknown").toLowerCase();
    return ["usable", "unavailable", "unknown"].includes(value)
      ? `admin.autolist.logical_health.${value}`
      : "admin.autolist.logical_health.unknown";
  }

  function logicalHealthStatus(status) {
    const value = String(status || "unknown").toLowerCase();
    return ["usable", "unavailable", "unknown"].includes(value) ? value : "unknown";
  }

  function renderTopologySummary(topology) {
    const total = Number(topology?.totalMembers || 0);
    if (total < 1) return "";
    const usable = Number(topology?.usableMembers || 0);
    const status = logicalHealthStatus(topology?.healthStatus);
    const health = t(logicalHealthKey(status));
    const description = t("admin.autolist.logical_health_summary", { health, usable, total });
    return `<span class="admin-server-health admin-server-health--${status}" role="status" aria-label="${escapeHtml(description)}" title="${escapeHtml(description)}">
      <span class="admin-server-health__indicator" aria-hidden="true"></span>
      <span class="admin-server-health__count">${escapeHtml(`${usable}/${total}`)}</span>
    </span>`;
  }

  function renderTopologyMembersHtml(members, pending) {
    const ordered = (Array.isArray(members) ? members : [])
      .filter((member) => member && member.is_active !== false)
      .slice()
      .sort((left, right) => {
        const order = Number(left.member_order || 0) - Number(right.member_order || 0);
        if (order) return order;
        return String(left.member_id || "").localeCompare(String(right.member_id || ""));
      });
    if (!ordered.length) return `<div class="admin-server-members__empty">${escapeHtml(t("admin.autolist.members_empty"))}</div>`;
    const rows = ordered.map((member) => {
      const index = Number(member.presentation_index || Number(member.member_order || 0) + 1);
      const status = String(member.status || "unknown").toLowerCase();
      const latency = status === "healthy" && typeof member.latency_ms === "number" && member.latency_ms >= 0
        ? `${member.latency_ms} ms`
        : (status === "failed" || status === "unavailable"
          ? t("admin.autolist.member_unavailable_timeout")
          : t("admin.autolist.member_no_latency"));
      const latencyHtml = pending
        ? '<span class="ping-spinner" role="status" aria-label="' + escapeHtml(t("manual_check.loading")) + '"></span>'
        : escapeHtml(latency);
      const active = Boolean(member.is_effective_active);
      return `<div class="admin-server-member-row admin-server-member-row--${escapeHtml(status)} ${active ? "is-effective-active" : ""}">
        <span class="admin-server-member-label">${escapeHtml(t("admin.autolist.member_label", { index }))}</span>
        <span class="admin-server-member-active ${active ? "is-active" : ""}" ${active ? `role="img" aria-label="${escapeHtml(t("admin.autolist.member_active"))}" title="${escapeHtml(t("admin.autolist.member_active"))}"` : "aria-hidden=\"true\""}><span aria-hidden="true"></span></span>
        <span class="admin-server-member-latency">${latencyHtml}</span>
        <span class="admin-server-member-health"><span class="admin-server-member-health__indicator" aria-hidden="true"></span>${escapeHtml(t(topologyStatusKey(status)))}</span>
      </div>`;
    }).join("");
    return `<div class="admin-server-members-table" role="table" aria-label="${escapeHtml(t("admin.autolist.members"))}">
      <div class="admin-server-member-row admin-server-member-row--head" role="row">
        <span>${escapeHtml(t("admin.autolist.member_column.node"))}</span>
        <span>${escapeHtml(t("admin.autolist.member_column.active"))}</span>
        <span>${escapeHtml(t("admin.autolist.member_column.latency"))}</span>
        <span>${escapeHtml(t("admin.autolist.member_column.health"))}</span>
      </div>${rows}</div>`;
  }

  function renderAdminServerName(name, meta) {
    const text = String(name || "").trim();
    if (!text) return "—";

    if (/^proxy(?:\s|$|\d)/i.test(text)) {
      return `<span class="picklist__label picklist__label--proxy admin-server-label" title="${escapeHtml(text)}"><span class="picklist__flag picklist__flag--proxy" aria-hidden="true">🔌</span><span class="picklist__label-text">${escapeHtml(text)}</span></span>`;
    }

    const match = text.match(/^([a-z]{2})\s+(.+)$/i);
    const metaCode = String(meta?.countryCode || "").trim().toLowerCase() || flagEmojiToCountryCode(text);
    if (!match && !metaCode) {
      if (window.FwrouterPingSelect?.renderFlaggedName) {
        return window.FwrouterPingSelect.renderFlaggedName(text);
      }
      return escapeHtml(text);
    }

    const code = (match ? match[1] : metaCode).toLowerCase();
    const rest = match ? match[2].trim() : stripLeadingFlagEmoji(text);
    const fallbackFlag = countryCodeToFlagEmoji(code);

    return `<span class="picklist__label admin-server-label" title="${escapeHtml(rest)}">
      <span class="picklist__flag-wrap" aria-hidden="true">
        <img
          class="picklist__flag-img"
          src="/static/flags/${escapeHtml(code)}.svg"
          alt=""
          loading="eager"
          decoding="async"
          onerror="this.style.display='none';if(this.nextElementSibling){this.nextElementSibling.style.display='inline-flex';}"
        />
        <span class="picklist__flag picklist__flag--fallback" style="display:none">${escapeHtml(fallbackFlag)}</span>
      </span>
      <span class="picklist__label-text">${escapeHtml(rest)}</span>
    </span>`;
  }

  function sortHead(label, key, sortKey, sortDir) {
    const active = sortKey === key;
    const arrow = active ? (sortDir === "asc" ? "↑" : "↓") : "";

    return `<button
      type="button"
      class="picklist__sort ${active ? "is-active" : ""}"
      data-auto-sort="${escapeHtml(key)}"
      title="${escapeHtml(t("admin.autolist.sort_title", { label }))}"
    >
      <span class="picklist__sort-label">${escapeHtml(label)}</span>
      <span class="picklist__sort-arrow" aria-hidden="true">${escapeHtml(arrow)}</span>
    </button>`;
  }

  function renderAutolistTableHtml(names, options) {
    const opts = options || {};
    const currentCandidates = Array.isArray(opts.currentCandidates) ? opts.currentCandidates : [];
    const currentHiddenUser = Array.isArray(opts.currentHiddenUser) ? opts.currentHiddenUser : [];
    const currentPriorities = opts.currentPriorities || {};
    const autolistDelays = opts.autolistDelays instanceof Map ? opts.autolistDelays : new Map();
    const autolistStatuses = opts.autolistStatuses instanceof Map ? opts.autolistStatuses : new Map();
    const autolistServerMeta = opts.autolistServerMeta instanceof Map ? opts.autolistServerMeta : new Map();
    const adminCurrentProxy = String(opts.adminCurrentProxy || "");
    const selectedAutolistServerKey = String(opts.selectedAutolistServerKey || "");
    const activatingAutolistServerKey = String(opts.activatingAutolistServerKey || "");
    const pingPending = Boolean(opts.pingPending);

    const rows = (Array.isArray(names) ? names : []).map((name) => {
      const checkedAuto = currentCandidates.includes(name) ? "checked" : "";
      const isVisible = !currentHiddenUser.includes(name);
      const checkedVisible = isVisible ? "checked" : "";
      const delay = autolistDelays.has(name) ? autolistDelays.get(name) : null;
      const pingStatus = autolistStatuses.get(name) || "";
      const priority = Number(currentPriorities[name] ?? 0);
      const meta = autolistServerMeta.get(name) || {};
      const isCurrent = adminCurrentProxy && (name === adminCurrentProxy || meta.label === adminCurrentProxy);
      const isSelected = selectedAutolistServerKey && name === selectedAutolistServerKey;
      const isActivating = activatingAutolistServerKey && name === activatingAutolistServerKey;

      let nameHtml = renderAdminServerName(meta.label || name, meta);
      const topology = meta.topology || {};
      const hasMemberExpansion = topology.totalMembers > 0;
      const topologyHtml = topology.totalMembers > 0
        ? `<div class="admin-server-topology">${renderTopologySummary(topology)}${hasMemberExpansion ? `<button type="button" class="admin-server-members-toggle" data-topology-server="${escapeHtml(name)}" aria-label="${escapeHtml(t("admin.autolist.members"))}" title="${escapeHtml(t("admin.autolist.members"))}" aria-expanded="false"><span aria-hidden="true"></span></button>` : ""}</div>`
        : "";
      const memberExpansionHtml = hasMemberExpansion
        ? `<div class="admin-server-members" data-topology-members="${escapeHtml(name)}" hidden></div>`
        : "";

      if (isCurrent) {
        nameHtml += ` <span class="picklist__badge">${escapeHtml(t("admin.autolist.current"))}</span>`;
      }

      const rowClass = [
        "server-matrix__row",
        "server-table__row",
        isCurrent ? "is-current" : "",
        isSelected ? "is-selected" : "",
        isActivating ? "is-activating" : "",
      ].filter(Boolean).join(" ");

      return `<div class="${rowClass}" data-auto-server-row="${escapeHtml(name)}" title="${escapeHtml(t("admin.autolist.row_title"))}">
        <div class="server-matrix__name server-table__cell" title="${escapeHtml(stripLeadingFlagEmoji(String(meta.label || name).replace(/^([a-z]{2})\s+/i, "").trim() || name))}">
          ${nameHtml}${topologyHtml}
        </div>

        <div class="server-matrix__ping server-table__cell">
          ${renderEffectiveLatency(delay, pingStatus, pingPending)}
        </div>

        <label class="server-switch server-table__cell" title="${escapeHtml(t("admin.autolist.auto_title"))}">
          <input type="checkbox" data-auto-candidate="${escapeHtml(name)}" ${checkedAuto} />
          <span class="server-switch__track"><span class="server-switch__thumb"></span></span>
        </label>

        <label class="server-switch server-table__cell" title="${escapeHtml(t("admin.autolist.visible_title"))}">
          <input type="checkbox" data-auto-visible="${escapeHtml(name)}" ${checkedVisible} />
          <span class="server-switch__track"><span class="server-switch__thumb"></span></span>
        </label>

        <div class="server-matrix__priority server-table__cell" title="${escapeHtml(t("admin.autolist.priority_title"))}">
          <input
            class="input input--mono"
            type="number"
            min="-1"
            max="5"
            step="1"
            value="${escapeHtml(String(priority))}"
            data-auto-priority="${escapeHtml(name)}"
            ${checkedAuto ? "" : "disabled"}
          />
        </div>
        ${memberExpansionHtml}
      </div>`;
    }).join("");

    return `<div class="server-matrix__head server-table__head">
      <div class="server-table__cell server-table__cell--name">${sortHead(t("admin.autolist.server"), "name", opts.sortKey, opts.sortDir)}</div>
      <div class="server-table__cell server-table__cell--ping">${sortHead(t("admin.autolist.ping"), "ping", opts.sortKey, opts.sortDir)}</div>
      <div class="server-table__cell server-table__cell--auto">${sortHead(t("admin.autolist.auto"), "auto", opts.sortKey, opts.sortDir)}</div>
      <div class="server-table__cell server-table__cell--visible">${sortHead(t("admin.autolist.visible"), "visible", opts.sortKey, opts.sortDir)}</div>
      <div class="server-table__cell server-table__cell--priority">${sortHead(t("admin.autolist.priority"), "priority", opts.sortKey, opts.sortDir)}</div>
    </div>
    <div class="server-matrix__body server-table__body">
      ${rows || `<div class="muted" style="padding:12px 0;">${escapeHtml(t("admin.autolist.empty"))}</div>`}
    </div>`;
  }

  window.FwrouterAdminAutolist = {
    renderAdminServerName,
    renderAutolistTableHtml,
    renderTopologyMembersHtml,
  };
})();
