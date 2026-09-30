// Settings journal/context rendering helpers.
(function () {
  const { escapeHtml, translateBackendMessage } = window.FwrouterUI;
  const t = (key, params) => window.FwrouterI18n?.t(key, params) || key;
  const {
    formatTs,
    categoryLabel,
    levelLabel,
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

  function renderAdvancedValue(value) {
    if (value == null || value === "") return "";
    if (typeof value === "object") {
      try {
        return `<pre class="settings-advanced-details__json">${escapeHtml(JSON.stringify(value, null, 2))}</pre>`;
      } catch (_) {
        return "";
      }
    }
    return `<span class="mono">${renderContextValue(value)}</span>`;
  }

  function eventEntityIdentity(item) {
    const entityType = String(item?.entity_type || "").trim();
    const entityId = String(item?.entity_id || "").trim();
    if (entityId && entityType && entityId.startsWith(`${entityType}:`)) return entityId;
    if (entityType || entityId) return [entityType, entityId].filter(Boolean).join(":");
    return item?.subject_id || item?.connection_id || "";
  }

  function eventEntityLabel(item) {
    const label = String(item?.entity_label || "").trim();
    return label && label.length <= 120 ? label : "";
  }

  function safeSummaryIsInTitle(item) {
    const title = String(item?.title || item?.message || "");
    const summary = String(item?.safe_summary || "");
    if (!summary) return false;
    if (title.includes(summary)) return true;
    const separator = summary.indexOf(": ");
    const compact = separator > 0 && !summary.includes(" · ") ? summary.slice(separator + 2) : "";
    return Boolean(compact && title.includes(compact));
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

  function sourceLabel(source) {
    const value = String(source || "").trim();
    if (!value) return "—";
    const key = `journal.source.${value}`;
    const translated = t(key);
    return translated === key ? t("journal.source.system") : translated;
  }

  function ordinaryDetailValue(key, value) {
    if (key === "actor_attribution") {
      const attribution = value === "caller_supplied_unverified" ? "caller_supplied" : value;
      const label = t(`events.actor_attribution.${attribution}`);
      return label.startsWith("events.actor_attribution.") ? "" : escapeHtml(label);
    }
    if (key === "changed_fields" && Array.isArray(value)) {
      const fields = {
        alias: "events.field.client_alias",
        desired_mode: "events.field.client_mode",
        vpn_auto: "events.field.vpn_auto",
        vpn_auto_priority: "events.field.vpn_auto_priority",
        global_list: "events.field.global_list",
        server_mode: "events.field.server_mode",
        selective_default: "events.field.selective_default",
        name: "events.field.name",
        description: "events.field.description",
        enabled: "events.field.enabled",
      };
      const labels = value.map((field) => fields[field] ? t(fields[field]) : "").filter(Boolean);
      return labels.length ? escapeHtml(labels.join(", ")) : "";
    }
    if (["old_status", "new_status"].includes(key)) {
      return escapeHtml(t(`events.state.${value}`));
    }
    if (["objects_added", "objects_removed"].includes(key) && Array.isArray(value)) {
      const names = value.filter((name) => typeof name === "string" && name.trim() && name.length <= 120
        && !/(?:https?:\/\/|\/|\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b[a-f0-9]{20,}\b|^[a-z0-9_-]{32,}$|^(?:subject|server|entity|connection|request|job|apply|module|client|user|uuid|id|hash|sha256)[:_-])/i.test(name));
      return names.length ? escapeHtml(names.join(", ")) : "";
    }
    if (["checked_at", "last_observation_at"].includes(key)) {
      return typeof value === "string" && /^\d{4}-\d\d-\d\d[T ]\d\d:\d\d/.test(value)
        ? escapeHtml(formatTs(value, { absolute: true }) || value) : "";
    }
    if (key === "objects_label_source") {
      const label = t(`events.label_source.${value}`);
      return label.startsWith("events.label_source.") ? "" : escapeHtml(label);
    }
    if (["added_count", "removed_count", "member_number"].includes(key)) {
      return Number.isSafeInteger(value) && value >= 0 ? escapeHtml(String(value)) : "";
    }
    if (key === "logical_server_label") {
      return typeof value === "string" && value.length <= 120 ? escapeHtml(value) : "";
    }
    if (key === "evidence_source") {
      const label = t(`events.evidence_source.${value}`);
      return label.startsWith("events.evidence_source.") ? "" : escapeHtml(label);
    }
    return "";
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

    const category = String(item.journal_category || item.category || "system").toLowerCase();
    const level = String(item.level || "info").toLowerCase();

    const ordinaryDetailKeys = new Set([
      "actor_attribution", "changed_fields", "old_status",
      "new_status", "checked_at", "last_observation_at", "evidence_source",
      "logical_server_label", "member_number", "added_count",
      "removed_count", "objects_added", "objects_removed",
      "objects_label_source",
    ]);
    const details = Object.entries(item.details || {}).filter(([key, value]) => {
      if (!ordinaryDetailKeys.has(key)) return false;
      if (value == null) return false;
      if (typeof value === "string") {
        if (!value.trim() || value.length > 180) return false;
        if (["old_status", "new_status"].includes(key) && !["healthy", "failed", "stale", "unknown"].includes(value)) return false;
      } else if (typeof value === "number") {
        if (!Number.isFinite(value)) return false;
      } else if (typeof value === "boolean") {
        return false;
      } else if (Array.isArray(value)) {
        if (!value.length || value.length > 100 || !value.every((entry) => typeof entry === "string" && entry.length <= 120)) return false;
      } else {
        return false;
      }
      return Boolean(ordinaryDetailValue(key, value));
    });

    const rawDetails = item.details && typeof item.details === "object" ? item.details : {};
    const advancedEvent = item.advanced_event && typeof item.advanced_event === "object" ? item.advanced_event : null;
    const advancedDetails = advancedEvent?.details && typeof advancedEvent.details === "object" ? advancedEvent.details : {};
    const advancedEvidence = {};
    for (const key of [
      "phase", "workflow_id", "correlation_id", "causation_id", "recovery_attempt_id",
      "error_code", "error_message", "apply_state", "runtime_state", "reconcile_state",
      "implementation", "implementation_kind", "adapter", "provider", "evidence", "stack",
    ]) {
      if (advancedDetails[key] != null && advancedDetails[key] !== "") advancedEvidence[key] = advancedDetails[key];
    }
    const detailValue = (...keys) => {
      for (const key of keys) {
        if (rawDetails[key] != null && String(rawDetails[key]).trim()) return rawDetails[key];
      }
      return "";
    };
    const jobType = String(item.job_type || rawDetails.job_type || "").toLowerCase();
    const jobTypeKey = `events.job_type.${jobType}`;
    const jobTypeLabel = t(jobTypeKey) === jobTypeKey ? t("events.job_type.unknown") : t(jobTypeKey);
    const advancedSections = [
      {
        title: "journal.advanced.identity",
        rows: [
          ["journal.field.event_id", item.event_id],
          ["journal.field.subject_id", item.subject_id],
          ["journal.field.connection_id", item.connection_id],
          ["journal.field.entity", eventEntityIdentity(item)],
          ["journal.field.request_id", item.request_id],
        ],
      },
      {
        title: "journal.advanced.execution",
        rows: [
          ["journal.field.job_id", item.job_id],
          ["journal.field.apply_id", item.apply_id],
          ["journal.detail.status", detailValue("apply_state", "Состояние применения", "status", "Статус")],
        ],
      },
      {
        title: "journal.advanced.observation",
        rows: [
          ["journal.field.state", detailValue("runtime_state", "Live-режим", "Live-состояние не менялось")],
          ["journal.detail.traffic_snapshot", detailValue("observed_at", "Снимок трафика")],
        ],
      },
      {
        title: "journal.advanced.reconcile",
        rows: [
          ["journal.field.state", detailValue("reconcile_state", "confirmation", "Подтверждение")],
        ],
      },
      {
        title: "journal.advanced.implementation",
        rows: [
          ["inventory.info.implementation", detailValue("implementation", "implementation_kind", "adapter", "provider")],
          ["journal.field.type", item.event_code || item.event_type || item.type],
        ],
      },
      {
        title: "journal.advanced.errors",
        rows: [
          ["journal.detail.code", detailValue("error_code", "Код")],
          ["journal.detail.status", detailValue("error_message", "message", "Сообщение")],
          ["journal.detail.job_type", advancedEvent?.job_type || item.job_type || advancedDetails.job_type],
          ["journal.advanced.evidence", advancedEvidence],
        ],
      },
    ].map((section) => {
      const rows = section.rows
        .filter(([, value]) => {
          if (value == null) return false;
          if (typeof value === "string" && !value.trim()) return false;
          if (Array.isArray(value) && !value.length) return false;
          if (typeof value === "object" && !Array.isArray(value) && !Object.keys(value).length) return false;
          return true;
        })
        .map(([labelKey, value]) => `
          <div class="settings-advanced-details__row">
            <div class="settings-advanced-details__key">${escapeHtml(t(labelKey))}</div>
            <div class="settings-advanced-details__value">${renderAdvancedValue(value)}</div>
          </div>
        `).join("");
      if (!rows) return "";
      return `
        <section class="settings-advanced-details__section">
          <div class="settings-advanced-details__title">${escapeHtml(t(section.title))}</div>
          ${rows}
        </section>
      `;
    }).join("");

    const detailRows = details.length
      ? details.map(([key, value]) => `
        <div class="settings-event-context__detail">
          <div class="settings-event-context__key">${escapeHtml(detailKeyLabel(key))}</div>
          <div class="settings-event-context__value">${ordinaryDetailValue(key, value)}</div>
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

        ${item.message && item.message !== (item.title || "") ? `
          <div class="settings-event-context__message">
            ${escapeHtml(item.message)}
          </div>
        ` : ""}

        ${item.safe_summary && item.safe_summary !== item.reason && !safeSummaryIsInTitle(item) ? `
          <div class="settings-event-context__message">${escapeHtml(item.safe_summary)}</div>
        ` : ""}

        ${item.reason ? `
          <div class="settings-event-context__message">
            <strong>${escapeHtml(t("journal.field.reason"))}:</strong>
            ${escapeHtml(item.reason)}
          </div>
        ` : ""}

        ${item.recommendation ? `
          <div class="settings-event-context__message muted">
            <strong>${escapeHtml(t("journal.field.recommended_action"))}:</strong>
            ${escapeHtml(item.recommendation)}
          </div>
        ` : ""}

        <div class="settings-event-context__grid">
          <div class="settings-event-context__field">
            <span>${escapeHtml(t("journal.column.time"))}</span>
            <strong class="mono">${escapeHtml(formatTs(item.ts, { absolute: true })) || "—"}</strong>
          </div>

          <div class="settings-event-context__field">
            <span>${escapeHtml(t("journal.field.actor"))}</span>
            <strong>${escapeHtml(actorDisplay(item))}</strong>
          </div>
          ${item.actor_attribution && !rawDetails.actor_attribution ? `<div class="settings-event-context__field"><span>${escapeHtml(t("journal.detail.actor_attribution"))}</span><strong>${escapeHtml(ordinaryDetailValue("actor_attribution", item.actor_attribution))}</strong></div>` : ""}

          <div class="settings-event-context__field">
            <span>${escapeHtml(t("journal.field.source"))}</span>
            <strong>${escapeHtml(sourceLabel(item.source || item.log_source))}</strong>
          </div>

          ${item.result ? `
            <div class="settings-event-context__field">
              <span>${escapeHtml(t("journal.field.result"))}</span>
              <strong>${escapeHtml(t(`events.result.${item.result}`))}</strong>
            </div>
          ` : ""}
          ${jobType ? `<div class="settings-event-context__field"><span>${escapeHtml(t("journal.detail.job_type"))}</span><strong>${escapeHtml(jobTypeLabel)}</strong></div>` : ""}

          ${eventEntityLabel(item) && !String(item.title || item.message || "").includes(eventEntityLabel(item)) ? `
            <div class="settings-event-context__field">
              <span>${escapeHtml(t("journal.field.entity"))}</span>
              <strong>${escapeHtml(eventEntityLabel(item))}</strong>
            </div>
          ` : ""}
          ${item.entity_label_source === "current" && eventEntityLabel(item) ? `<div class="settings-event-context__field"><span>${escapeHtml(t("journal.field.entity_provenance"))}</span><strong>${escapeHtml(t("journal.entity_label.current"))}</strong></div>` : ""}

        </div>

        ${details.length ? `<div class="settings-event-context__details-visible settings-event-context__details">${detailRows}</div>` : ""}

        ${item.event_id ? `<details class="settings-event-context__details admin-advanced settings-advanced-collapse" data-event-full-disclosure>
          <summary class="admin-advanced__summary settings-advanced-collapse__summary">${escapeHtml(t("journal.advanced_details"))}</summary>
          <div class="settings-advanced-collapse__content">
            ${item.advanced_event ? `<div class="muted">${escapeHtml(t("journal.full.record_source"))}: ${escapeHtml(t(`journal.record_source.${item.advanced_event.details?.record_source || "unknown"}`))}</div>` : ""}
            ${item.advanced_error ? `<div class="muted">${escapeHtml(t("journal.full.unavailable"))} <button type="button" data-retry-full-event>${escapeHtml(t("journal.full.retry"))}</button></div>` : item.advanced_event ? "" : `<div class="muted">${escapeHtml(item.advanced_loading ? t("journal.full.loading") : t("journal.full.open"))}</div>`}
            <div class="settings-advanced-details">${advancedSections}</div>
          </div>
        </details>` : `<div class="settings-event-context__details admin-advanced settings-advanced-collapse"><span class="muted">${escapeHtml(t("journal.full.lookup_unsupported"))}</span><div class="settings-advanced-details">${advancedSections}</div></div>`}
      </div>
    `;
  }

  function actorDisplay(item) {
    const attribution = String(item?.actor_attribution || item?.details?.actor_attribution || "");
    const actor = String(item?.actor || "").trim();
    const opaque = actor.length > 80 || /(?:https?:\/\/|\/|\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b[a-f0-9]{20,}\b|^[a-z0-9_-]{32,}$|^(?:subject|server|entity|connection|request|job|apply|module|client|user|uuid|id|hash|sha256)[:_-])/i.test(actor);
    if (actor && actor !== "system" && !opaque) return actor;
    if (actor === "system" || attribution === "system") return t("events.actor_attribution.system");
    if (["caller_supplied", "caller_supplied_unverified"].includes(attribution)) return t("journal.actor.caller_supplied");
    if (attribution === "internal") return t("events.actor_attribution.internal");
    return t("journal.actor.unknown");
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
        ? (apply.outcome === "failed" ? t("journal.rules.failed") : t("journal.rules.last_attempt_complete"))
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
            <div class="settings-event-context__value">${escapeHtml(state.active_status || detail)}</div>
          </div>

          <div class="settings-event-context__detail">
            <div class="settings-event-context__key">${escapeHtml(t("journal.rules.latest_attempt"))}</div>
            <div class="settings-event-context__value">${escapeHtml(state.latest_attempt_status || "—")}</div>
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
          ${state.problem ? `<div class="settings-event-context__detail"><div class="settings-event-context__key">${escapeHtml(t("diagnostics.field.problem"))}</div><div class="settings-event-context__value">${escapeHtml(state.problem)}</div></div>` : ""}
          ${state.action ? `<div class="settings-event-context__detail"><div class="settings-event-context__key">${escapeHtml(t("diagnostics.field.action"))}</div><div class="settings-event-context__value">${escapeHtml(state.action)}</div></div>` : ""}
        </div>
      </div>
    `;
  }

  function renderEventsHtml(items, selectedEventIndex, getEventSourceIndex) {
    const rows = (Array.isArray(items) ? items : []).map((item) => {
      const sourceIndex = getEventSourceIndex(item);
      const category = String(item.journal_category || item.category || "system").toLowerCase();
      const level = String(item.level || "info").toLowerCase();
      const selected = sourceIndex === selectedEventIndex;

      return `
        <div
          class="settings-event-row settings-event-row--summary settings-event-row--${escapeHtml(level)} ${selected ? "is-selected" : ""}"
          data-event-row="${sourceIndex}"
        >
          <div class="settings-event-row__main" role="button" tabindex="0" aria-expanded="false" data-event-toggle>
            <span class="settings-event__time mono">${escapeHtml(formatTs(item.ts, { absolute: true }))}</span>
            <span class="settings-event__badge settings-event__badge--${escapeHtml(category)}">${escapeHtml(categoryLabel(category))}</span>
            <span class="settings-event__message">
              ${escapeHtml(item.message || item.title || t("events.type.default"))}
              ${item.entity_label && !String(item.message || item.title || "").includes(item.entity_label) ? `<span class="settings-event__entity"> · ${escapeHtml(item.entity_label)}${item.entity_label_source === "current" ? ` (${escapeHtml(t("journal.entity_label.current_short"))})` : ""}</span>` : ""}
            </span>
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
