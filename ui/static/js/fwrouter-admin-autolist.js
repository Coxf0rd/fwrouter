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
    if (typeof delay === "number" && delay > 0) return `${delay} ms`;
    if (delay === 0 || delay === -1) return "timeout";
    return "—";
  }

  function renderAdminServerName(name, meta) {
    const text = String(name || "").trim();
    if (!text) return "—";

    if (/^proxy(?:\s|$|\d)/i.test(text)) {
      return `<span class="picklist__label picklist__label--proxy"><span class="picklist__flag picklist__flag--proxy" aria-hidden="true">🔌</span><span class="picklist__label-text">${escapeHtml(text)}</span></span>`;
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

    return `<span class="picklist__label">
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
    const autolistServerMeta = opts.autolistServerMeta instanceof Map ? opts.autolistServerMeta : new Map();
    const adminCurrentProxy = String(opts.adminCurrentProxy || "");
    const selectedAutolistServerKey = String(opts.selectedAutolistServerKey || "");
    const activatingAutolistServerKey = String(opts.activatingAutolistServerKey || "");

    const rows = (Array.isArray(names) ? names : []).map((name) => {
      const checkedAuto = currentCandidates.includes(name) ? "checked" : "";
      const isVisible = !currentHiddenUser.includes(name);
      const checkedVisible = isVisible ? "checked" : "";
      const delay = autolistDelays.has(name) ? autolistDelays.get(name) : null;
      const priority = Number(currentPriorities[name] ?? 0);
      const isCurrent = adminCurrentProxy && name === adminCurrentProxy;
      const isSelected = selectedAutolistServerKey && name === selectedAutolistServerKey;
      const isActivating = activatingAutolistServerKey && name === activatingAutolistServerKey;

      const meta = autolistServerMeta.get(name) || {};
      let nameHtml = renderAdminServerName(name, meta);

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
        <div class="server-matrix__name server-table__cell" title="${escapeHtml(stripLeadingFlagEmoji(String(name || "").replace(/^([a-z]{2})\s+/i, "").trim() || name))}">
          ${nameHtml}
        </div>

        <div class="server-matrix__ping server-table__cell">
          ${escapeHtml(formatPing(delay))}
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
  };
})();
