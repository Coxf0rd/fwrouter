// Settings journal/context rendering helpers.
(function () {
  const { escapeHtml, translateBackendMessage } = window.FwrouterUI;
  const t = (key, params) => window.FwrouterI18n?.t(key, params) || key;
  const {
    formatTs,
    categoryLabel,
    levelLabel,
    eventTypeLabel,
  } = window.FwrouterSettingsEvents;

  function renderContextValue(value) {
    if (value == null) return "—";

    if (typeof value === "boolean") {
      return escapeHtml(t(value ? "common.yes" : "common.no"));
    }

    if (typeof value === "object") {
      try {
        return escapeHtml(JSON.stringify(value, null, 2));
      } catch (_) {
        return "—";
      }
    }

    const text = String(value || "").trim();
    const aliasKey = `common.alias.${text}`;
    const alias = t(aliasKey);
    if (alias !== aliasKey) return escapeHtml(alias);
    return text ? escapeHtml(translateBackendMessage(text)) : "—";
  }

  function detailKeyLabel(key) {
    const raw = String(key || "").trim();
    if (!raw) return "";

    const aliasKey = `journal.detail.alias.${raw}`;
    const alias = t(aliasKey);
    if (alias !== aliasKey) return alias;
    const normalized = raw;
    const keyName = `journal.detail.${normalized}`;
    const label = t(keyName);
    return label !== keyName ? label : raw;
  }

  function renderEmptyEventContextHtml() {
    return `
      <div class="settings-event-context settings-event-context--empty">
        <div class="settings-event-context__title">${escapeHtml(t("journal.empty.title"))}</div>
        <div class="settings-event-context__text muted">
          ${escapeHtml(t("journal.empty.text"))}
        </div>
      </div>
    `;
  }

  function renderSelectedEventContextHtml(item) {
    if (!item) return renderEmptyEventContextHtml();

    const category = String(item.category || "system").toLowerCase();
    const level = String(item.level || "info").toLowerCase();

    const details = Object.entries(item.details || {}).filter(([, value]) => {
      if (value == null) return false;
      if (typeof value === "string" && !value.trim()) return false;
      if (Array.isArray(value) && !value.length) return false;
      if (typeof value === "object" && !Array.isArray(value) && !Object.keys(value).length) return false;
      return true;
    });

    const detailRows = details.length
      ? details.map(([key, value]) => `
        <div class="settings-event-context__detail">
          <div class="settings-event-context__key">${escapeHtml(detailKeyLabel(key))}</div>
          <div class="settings-event-context__value mono">${renderContextValue(value)}</div>
        </div>
      `).join("")
      : `<div class="settings-event-context__empty-detail muted">${escapeHtml(t("journal.empty_details"))}</div>`;

    return `
      <div class="settings-event-context">
        <div class="settings-event-context__top">
          <span class="settings-event__badge settings-event__badge--${escapeHtml(category)}">
            ${escapeHtml(categoryLabel(category))}
          </span>

          <span class="settings-event__level settings-event__level--${escapeHtml(level)}">
            ${escapeHtml(levelLabel(level))}
          </span>
        </div>

        <div class="settings-event-context__title">
          ${escapeHtml(item.title || item.message || t("events.type.default"))}
        </div>

        ${item.message ? `
          <div class="settings-event-context__message">
            ${escapeHtml(item.message)}
          </div>
        ` : ""}

        <div class="settings-event-context__grid">
          <div class="settings-event-context__field">
            <span>${escapeHtml(t("journal.column.time"))}</span>
            <strong class="mono">${escapeHtml(formatTs(item.ts)) || "—"}</strong>
          </div>

          <div class="settings-event-context__field">
            <span>${escapeHtml(t("journal.field.source"))}</span>
            <strong>${escapeHtml(item.actor || "—")}</strong>
          </div>

          <div class="settings-event-context__field">
            <span>${escapeHtml(t("journal.field.type"))}</span>
            <strong>${escapeHtml(eventTypeLabel(item.type) || "—")}</strong>
          </div>
        </div>

        <div class="settings-event-context__details">
          ${detailRows}
        </div>
      </div>
    `;
  }

  function renderRulesContextHtml(status) {
    const state = (status && status.state) || {};
    const apply = (status && status.apply) || {};

    const tag = state.tag || t("journal.rules.not_configured");
    const detail = state.detail || "—";

    const lastSuccess = state.last_success_at
      ? formatTs(new Date(Number(state.last_success_at) * 1000).toISOString())
      : "—";

    const appliedAt = apply.done_at
      ? formatTs(new Date(Number(apply.done_at) * 1000).toISOString())
      : "—";

    const applyStatus = apply.pending
      ? t("journal.rules.applying")
      : apply.done
        ? t("journal.rules.applied")
        : "—";

    return `
      <div class="settings-event-context settings-rules-context">
        <div class="settings-event-context__top">
          <span class="settings-event__badge">Re-filter</span>
          <span class="settings-event__level settings-event__level--info">
            ${escapeHtml(applyStatus)}
          </span>
        </div>

        <div class="settings-event-context__title">
          ${escapeHtml(t("journal.rules.title"))}
        </div>

        <div class="settings-event-context__message">
          ${escapeHtml(t("journal.rules.text"))}
        </div>

        <div class="settings-event-context__details">
          <div class="settings-event-context__detail">
            <div class="settings-event-context__key">${escapeHtml(t("journal.field.source"))}</div>
            <div class="settings-event-context__value mono">${escapeHtml(tag)}</div>
          </div>

          <div class="settings-event-context__detail">
            <div class="settings-event-context__key">${escapeHtml(t("journal.field.state"))}</div>
            <div class="settings-event-context__value mono">${escapeHtml(detail)}</div>
          </div>

          <div class="settings-event-context__detail">
            <div class="settings-event-context__key">${escapeHtml(t("journal.field.last_success"))}</div>
            <div class="settings-event-context__value mono">${escapeHtml(lastSuccess)}</div>
          </div>

          <div class="settings-event-context__detail">
            <div class="settings-event-context__key">${escapeHtml(t("journal.field.apply"))}</div>
            <div class="settings-event-context__value mono">${escapeHtml(applyStatus)}</div>
          </div>

          <div class="settings-event-context__detail">
            <div class="settings-event-context__key">${escapeHtml(t("journal.field.applied"))}</div>
            <div class="settings-event-context__value mono">${escapeHtml(appliedAt)}</div>
          </div>
        </div>
      </div>
    `;
  }

  function renderEventsHtml(items, selectedEventIndex, getEventSourceIndex) {
    const rows = (Array.isArray(items) ? items : []).map((item) => {
      const sourceIndex = getEventSourceIndex(item);
      const category = String(item.category || "system").toLowerCase();
      const level = String(item.level || "info").toLowerCase();
      const selected = sourceIndex === selectedEventIndex;

      return `
        <div
          class="settings-event-row settings-event-row--summary settings-event-row--${escapeHtml(level)} ${selected ? "is-selected" : ""}"
          data-event-row="${sourceIndex}"
        >
          <div class="settings-event-row__main" role="button" tabindex="0" aria-expanded="false" data-event-toggle>
            <span class="settings-event__time mono">${escapeHtml(formatTs(item.ts))}</span>
            <span class="settings-event__badge settings-event__badge--${escapeHtml(category)}">${escapeHtml(categoryLabel(category))}</span>
            <span class="settings-event__message">${escapeHtml(item.message || item.title || t("events.type.default"))}</span>
            <span class="settings-event__level settings-event__level--${escapeHtml(level)}">${escapeHtml(levelLabel(level))}</span>
          </div>
        </div>
      `;
    }).join("");

    return `
      <div class="settings-events-table" role="table" aria-label="${escapeHtml(t("journal.table.label"))}">
        <div class="settings-events-table__head" role="row">
          <span class="settings-events-table__cell settings-events-table__cell--time">${escapeHtml(t("journal.column.time"))}</span>
          <span class="settings-events-table__cell settings-events-table__cell--category">${escapeHtml(t("journal.column.category"))}</span>
          <span class="settings-events-table__cell settings-events-table__cell--message">${escapeHtml(t("journal.column.message"))}</span>
          <span class="settings-events-table__cell settings-events-table__cell--level">${escapeHtml(t("journal.column.level"))}</span>
        </div>
        <div class="settings-events-table__body">${rows}</div>
      </div>
    `;
  }

  window.FwrouterSettingsJournal = {
    renderSelectedEventContextHtml,
    renderRulesContextHtml,
    renderEventsHtml,
  };
})();
