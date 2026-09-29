// Settings journal helpers. Pure data shaping/labels for settings.js.
(function () {
  const { translateBackendMessage } = window.FwrouterUI;
  const t = (key, params) => window.FwrouterI18n?.t(key, params) || key;
  const APP_TIME_ZONE = "Asia/Krasnoyarsk";
  const OLD_DATA_DAYS = 7;

  function localeCode() {
    return window.FwrouterI18n?.locale?.() || document.documentElement?.dataset?.locale || "ru";
  }

  function absoluteTimeFormatter() {
    return new Intl.DateTimeFormat(localeCode() === "en" ? "en-US" : "ru-RU", {
      timeZone: APP_TIME_ZONE,
      day: "2-digit",
      month: "2-digit",
      year: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  function formatAbsoluteTime(date) {
    const parts = Object.fromEntries(absoluteTimeFormatter().formatToParts(date).map(({ type, value }) => [type, value]));
    return `${parts.day}.${parts.month}.${parts.year} ${parts.hour}:${parts.minute}`;
  }

  function parseBackendTs(ts) {
    if (ts instanceof Date) return ts;
    if (typeof ts === "number") return new Date(ts);

    const raw = String(ts || "").trim();
    if (!raw) return null;

    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(raw)) {
      return new Date(`${raw.replace(" ", "T")}Z`);
    }

    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(raw)) {
      return new Date(`${raw}Z`);
    }

    return new Date(raw);
  }

  function formatTs(ts, options = {}) {
    if (!ts) return "";

    try {
      const parsed = parseBackendTs(ts);
      if (!parsed || Number.isNaN(parsed.getTime())) return String(ts || "");
      if (options.absolute) return formatAbsoluteTime(parsed);
      const now = options.now instanceof Date ? options.now : new Date();
      const ageMs = now.getTime() - parsed.getTime();
      const ageSec = Math.max(0, Math.floor(ageMs / 1000));
      if (ageSec < 60) return t("time.just_now");
      if (ageSec < 3600) return t("time.minutes_ago", { count: Math.max(1, Math.floor(ageSec / 60)) });
      if (ageSec < 86400) return t("time.hours_ago", { count: Math.max(1, Math.floor(ageSec / 3600)) });
      if (ageSec < OLD_DATA_DAYS * 86400) return t("time.days_ago", { count: Math.max(1, Math.floor(ageSec / 86400)) });
      return formatAbsoluteTime(parsed);
    } catch (_) {
      return String(ts || "");
    }
  }

  function freshnessFor(ts, options = {}) {
    const parsed = parseBackendTs(ts);
    if (!parsed || Number.isNaN(parsed.getTime())) {
      return {
        state: "unknown",
        text: t("time.no_observation"),
        title: String(ts || ""),
      };
    }
    const staleAfter = parseBackendTs(options.stale_after);
    const staleByProjection = Boolean(options.stale)
      || (staleAfter && !Number.isNaN(staleAfter.getTime()) && Date.now() > staleAfter.getTime());
    const state = staleByProjection ? "stale" : options.historical ? "historical" : "fresh";
    const prefix = state === "stale" ? t("time.stale") : state === "historical" ? t("time.historical") : "";
    const rendered = formatTs(parsed, options);
    return {
      state,
      text: prefix ? `${prefix}: ${rendered}` : rendered,
      title: parsed.toISOString(),
    };
  }

  function categoryLabel(category) {
    const value = String(category || "").toLowerCase();

    const label = t(`events.category.${value}`);
    return label !== `events.category.${value}` ? label : (value || t("events.category.default"));
  }

  function levelLabel(level) {
    const value = String(level || "info").toLowerCase();

    const label = t(`events.level.${value}`);
    return label !== `events.level.${value}` ? label : value;
  }

  function normalizeEventSeverity(event) {
    const eventClass = String(event?.event_class || event?.classification || event?.category || "").toLowerCase();
    if (eventClass === "audit") return "info";
    const raw = String(event?.severity || event?.level || (event?.result === "failure" ? "error" : "info")).toLowerCase();
    const eventType = String(event?.event_type || event?.action || "").toLowerCase();
    const eventCode = String(event?.event_code || "").toLowerCase();
    const legacyType = !eventCode || event?.details?.event_code_compatibility === "legacy_event_type";
    if (raw === "critical") return "critical";
    if (["failed", "failure", "error"].includes(raw) || (legacyType && (eventType.endsWith("_failed") || eventType === "runtime_failed"))) {
      return "error";
    }
    if (["warning", "degraded", "drift", "stale"].includes(raw) || (legacyType && eventType === "reconcile_drift")) return "warning";
    return "info";
  }

  function isLegacyEventCode(event) {
    const code = String(event?.event_code || "").trim();
    return !code || event?.details?.event_code_compatibility === "legacy_event_type";
  }

  function eventTypeLabel(type) {
    const value = String(type || "").trim();

    const label = t(`events.type.${value}`);
    return label !== `events.type.${value}` ? label : (value || t("events.type.default"));
  }

  function safeHumanLabel(value) {
    const label = String(value || "").trim();
    if (!label || label.length > 120 || /(?:https?:\/\/|\/|\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b[a-f0-9]{20,}\b|^[a-z0-9_-]{32,}$|^(?:subject|server|entity|connection|request|job|apply|module|client|user|uuid|id|hash|sha256)[:_-])/i.test(label)) return "";
    return label;
  }

  function eventCategory(event) {
    const explicit = String(event?.category || "").toLowerCase();
    if (explicit) return explicit;

    const eventClass = String(event?.event_class || event?.classification || "").toLowerCase();
    if (eventClass === "audit") return "audit";
    if (eventClass === "diagnostic") return "diagnostic";
    const entityType = String(event?.entity_type || "").toLowerCase();
    if (entityType === "watchdog") return "watchdog";
    if (entityType === "routing" || entityType === "rules") return "routing";
    if (entityType === "vpn" || entityType === "server" || entityType === "connection") return "server";
    if (entityType === "subject" || event?.subject_id) return "user";
    if (entityType === "module" || entityType === "system" || entityType === "database") return "system";
    if (String(event?.severity || event?.level || "").toLowerCase() === "error") return "error";
    return "system";
  }

  function eventSearchText(event) {
    const details = event?.details;
    const detailText = details && typeof details === "object"
      ? Object.entries(details).map(([key, value]) => `${key} ${String(value || "")}`).join(" ")
      : "";

    return [
      event?.event_class,
      event?.severity,
      event?.level,
      event?.event_type,
      event?.type,
      event?.entity_type,
      event?.entity_id,
      event?.actor,
      event?.action,
      event?.message,
      event?.title,
      event?.subject_id,
      event?.connection_id,
      detailText,
    ].join(" ").toLowerCase();
  }

  function isWarningOrError(event) {
    return ["warning", "error", "critical", "failed"].includes(String(event?.severity || event?.level || "").toLowerCase());
  }

  function journalCategory(event) {
    return eventCategory(event);
  }

  function matchesJournalTab(event, tab) {
    const value = String(tab || "all").toLowerCase();
    const category = journalCategory(event);
    if (value === "all") return category !== "diagnostic";
    if (value === "diagnostic") return category === "diagnostic";
    if (category === "diagnostic") return false;
    if (value === "error") return isWarningOrError(event);
    if (category === value) return true;
    if (category !== "audit") return false;

    // Audit remains the event's canonical category while entity-specific
    // journal tabs continue to include the audited object/action.
    const entityType = String(event?.entity_type || "").toLowerCase();
    if (entityType === "routing" || entityType === "rules") return value === "routing";
    if (["vpn", "server", "server_assignment", "connection"].includes(entityType)) return value === "server";
    if (["subject", "external_client", "client"].includes(entityType) || event?.subject_id) return value === "user";
    if (["module", "system", "database"].includes(entityType)) return value === "system";
    return false;
  }

  function mergeAuditEvents(payload, auditPayload) {
    const merged = { ...(payload || {}) };
    const byId = new Map();
    for (const event of [
      ...(Array.isArray(payload?.audit) ? payload.audit : []),
      ...(Array.isArray(auditPayload?.audit) ? auditPayload.audit : []),
    ]) {
      if (!event || typeof event !== "object") continue;
      const id = String(event.event_id || "");
      if (!id) continue;
      byId.set(id, event);
    }
    merged.audit = Array.from(byId.values()).sort((left, right) => {
      const leftTime = Date.parse(String(left.timestamp || left.created_at || "")) || 0;
      const rightTime = Date.parse(String(right.timestamp || right.created_at || "")) || 0;
      return rightTime - leftTime;
    });
    return merged;
  }

  function eventDisplayMessage(event, fallbackKey) {
    const raw = String(event?.message || "").trim();
    const typeLabel = eventTypeLabel(event?.event_type);
    const typeRaw = String(event?.event_type || "");
    if ((!raw || raw === typeRaw) && typeLabel && typeLabel !== typeRaw) {
      return typeLabel;
    }
    const translated = translateBackendMessage(raw);
    return translated && translated !== raw ? translated : t(fallbackKey);
  }

  function domainEventMessage(event) {
    const eventCode = String(event?.event_code || "").trim();
    const eventType = String(event?.event_type || event?.action || "").toLowerCase();
    const details = event?.details && typeof event.details === "object" ? event.details : {};
    if (eventType === "job_handler_exception") {
      const jobType = String(event.job_type || details.job_type || "").toLowerCase();
      const jobKey = `events.job_type.${jobType}`;
      const operation = t(jobKey) === jobKey ? t("events.job_type.unknown") : t(jobKey);
      return t("events.job.failure", { operation });
    }
    if (eventType === "watchdog_scheduler_failed") return t("events.job.watchdog_failure");
    if (eventCode) {
      for (const key of [`events.code.${eventCode}`, `events.type.${eventCode}`]) {
        const codeLabel = t(key);
        if (codeLabel !== key) {
          const objectLabel = String(event?.entity_label || "").trim();
          const objectKey = `${key}.object`;
          const objectTemplate = t(objectKey);
          if (objectLabel && objectTemplate !== objectKey) {
            return t(objectKey, { object: objectLabel });
          }
          return codeLabel;
        }
      }
      if (!isLegacyEventCode(event)) return "";
    }
    const eventClass = String(event?.event_class || "").toLowerCase();
    const entityType = String(event?.entity_type || "").toLowerCase();
    const severity = String(event?.severity || event?.level || "").toLowerCase();
    if (eventClass === "audit") {
      const auditKey = `events.audit.${eventType}`;
      const label = t(auditKey);
      if (label !== auditKey) return label;
    }
    if (entityType === "xray") {
      if (severity === "error" || severity === "failed" || eventType === "runtime_failed") {
        return t("events.domain.external_client_connection_failed");
      }
      if (eventType === "reconcile_drift") return t("events.domain.external_client_drift");
      if (eventType.includes("binding") || eventType.includes("materialized")) return t("events.domain.external_client_route_updated");
      return t("events.domain.external_client_connection_changed");
    }
    if (entityType === "routing" || entityType === "rules") {
      if (eventType === "reconcile_drift") return t("events.domain.routing_drift");
      if (severity === "error" || severity === "failed") return t("events.domain.routing_failed");
      return t("events.domain.routing_changed");
    }
    if (entityType === "vpn") {
      if (eventType === "vpn_auto_server_switched") return t("events.domain.vpn_server_changed_auto");
      if (severity === "error" || severity === "failed" || eventType === "runtime_failed") {
        return t("events.domain.vpn_connection_failed");
      }
      return t("events.domain.vpn_connection_changed");
    }
    return "";
  }

  function domainEventReason(event) {
    const details = event?.details && typeof event.details === "object" ? event.details : {};
    const reasonCode = String(event?.error_code || event?.reason_code || details.error_code || details.reason_code || "").trim();
    if (reasonCode) {
      const reasonLabel = t(`events.reason_code.${reasonCode}`);
      if (reasonLabel !== `events.reason_code.${reasonCode}`) return reasonLabel;
    }
    if (event?.event_code && !isLegacyEventCode(event)) return "";
    const rawReason = String(event?.reason || details.reason || details.reason_code || details.error || "").trim();
    const eventType = String(event?.event_type || event?.action || "").toLowerCase();
    if (eventType === "vpn_auto_server_switched") return "";
    if (eventType === "reconcile_drift") return t("events.reason.reconcile_drift");
    if (rawReason) {
      const translated = translateBackendMessage(rawReason);
      return translated !== rawReason ? translated : "";
    }
    return "";
  }

  function eventSafeSummary(event) {
    const eventCode = String(event?.event_code || "");
    const eventType = String(event?.event_type || "");
    if ([
      "HEALTH_MEMBER_STATE_CHANGED",
      "HEALTH_MEMBER_RECOVERED",
    ].includes(eventCode) || eventType === "logical_member_health_transition") {
      const details = event?.details && typeof event.details === "object" ? event.details : {};
      const oldStatus = String(details.old_status || "");
      const newStatus = String(details.new_status || "");
      const safeStatuses = new Set(["healthy", "failed", "stale", "unknown"]);
      if (!safeStatuses.has(oldStatus) || !safeStatuses.has(newStatus)) return "";
      const statusLabel = (status) => t(`events.state.${status}`);
      return t("events.detail.member_transition", {
        old: statusLabel(oldStatus),
        next: statusLabel(newStatus),
      });
    }
    if (eventType === "vpn_auto_server_switched" || eventCode === "vpn_auto_server_switched") {
      const details = event?.details && typeof event.details === "object" ? event.details : {};
      const previous = details.previous_value && typeof details.previous_value === "object" ? details.previous_value : {};
      const next = details.new_value && typeof details.new_value === "object" ? details.new_value : {};
      const oldLabel = safeHumanLabel(previous.server_label) || t("events.target.unknown_previous");
      const newLabel = safeHumanLabel(next.server_label) || t("events.target.unknown_current");
      const reasonCode = String(details.reason_code || event.reason_code || "").toLowerCase();
      const reasonKey = `events.selection_reason.${reasonCode}`;
      const reasonLabel = reasonCode && t(reasonKey) !== reasonKey ? t(reasonKey) : "";
      return [t("events.detail.field_transition", { field: t("events.field.server"), old: oldLabel, next: newLabel }), reasonLabel].filter(Boolean).join(" · ");
    }
    const eventClass = String(event?.event_class || "").toLowerCase();
    if (eventClass !== "audit") return "";
    const details = event?.details && typeof event.details === "object" ? event.details : {};
    const auditCode = String(event?.event_code || "");
    if (![
      "client.alias_changed",
      "client.mode_changed",
      "server.preferences_changed",
      "server.vpn_auto_membership_changed",
      "routing.global_mode_changed",
      "routing.server_mode_changed",
      "routing.global_fixed_server_changed",
      "server.assignment_changed",
      "routing.selective_default_changed",
      "module.desired_state_changed",
      "module.lifecycle_changed",
      "subscription.configuration_changed",
    ].includes(auditCode)) return "";
    const previous = details.previous_value && typeof details.previous_value === "object" ? details.previous_value : {};
    const next = details.new_value && typeof details.new_value === "object" ? details.new_value : {};
    if (auditCode === "server.vpn_auto_membership_changed") {
      const added = Array.isArray(details.objects_added) ? details.objects_added.map(safeHumanLabel).filter(Boolean).slice(0, 20) : [];
      const removed = Array.isArray(details.objects_removed) ? details.objects_removed.map(safeHumanLabel).filter(Boolean).slice(0, 20) : [];
      const addedCount = Number.isInteger(details.added_count) ? details.added_count : added.length;
      const removedCount = Number.isInteger(details.removed_count) ? details.removed_count : removed.length;
      return [
        addedCount ? t("events.detail.servers_added_count", { count: addedCount }) : "",
        added.length ? t("events.detail.servers_added", { objects: added.join(", ") }) : "",
        removedCount ? t("events.detail.servers_removed_count", { count: removedCount }) : "",
        removed.length ? t("events.detail.servers_removed", { objects: removed.join(", ") }) : "",
      ].filter(Boolean).join(" · ");
    }
    if (auditCode === "subscription.configuration_changed") {
      const changed = Array.isArray(details.changed_fields) ? details.changed_fields : [];
      const fieldKeys = { name: "events.field.name", description: "events.field.description", enabled: "events.field.enabled" };
      const labels = changed.map((field) => fieldKeys[field] ? t(fieldKeys[field]) : "").filter(Boolean);
      return labels.length ? t("events.detail.fields_changed", { fields: labels.join(", ") }) : "";
    }
    if (auditCode === "client.alias_changed") {
      const oldAlias = typeof previous.alias_label === "string" ? previous.alias_label : "";
      const newAlias = typeof next.alias_label === "string" ? next.alias_label : "";
      if (oldAlias && newAlias && oldAlias !== newAlias) {
        return t("events.detail.field_transition", {
          field: t("events.field.client_alias"), old: oldAlias, next: newAlias,
        });
      }
      if (typeof previous.alias_present !== "boolean" || typeof next.alias_present !== "boolean") return "";
      return t("events.detail.field_transition", {
        field: t("events.field.alias_present"),
        old: t(previous.alias_present ? "common.yes" : "common.no"),
        next: t(next.alias_present ? "common.yes" : "common.no"),
      });
    }
    const labels = {
      desired_mode: "events.field.client_mode",
      selective_default: "events.field.selective_default",
      vpn_auto: "events.field.vpn_auto",
      vpn_auto_priority: "events.field.vpn_auto_priority",
      global_list: "events.field.global_list",
      desired_state: "events.field.desired_state",
      lifecycle_mode: "events.field.lifecycle_mode",
    };
    const safeEnum = new Set([
      "enabled", "disabled", "managed", "external", "none", "inventory",
      "direct", "selective", "vpn", "global", "auto", "fixed",
    ]);
    const values = [];
    const safeServerTarget = (value, mode, previous = false) => {
      const serverLabel = safeHumanLabel(value?.server_label);
      if (serverLabel) return serverLabel;
      const normalizedMode = String(mode || value?.server_mode || value?.mode || "").toLowerCase();
      if (["auto", "global"].includes(normalizedMode)) return t(`events.mode.${normalizedMode}`);
      return t(previous ? "events.target.unknown_previous" : "events.target.unknown_current");
    };
    const selectionEvent = ["routing.global_fixed_server_changed", "server.assignment_changed"].includes(auditCode);
    for (const key of ["server_mode", "mode"]) {
      if (previous[key] !== undefined && next[key] !== undefined && previous[key] !== next[key]) {
        values.push(t("events.detail.field_transition", {
          field: t(key === "server_mode" ? "events.field.server_mode" : "events.field.mode"),
          old: safeEnum.has(String(previous[key]).toLowerCase()) ? t(`events.mode.${String(previous[key]).toLowerCase()}`) : "",
          next: safeEnum.has(String(next[key]).toLowerCase()) ? t(`events.mode.${String(next[key]).toLowerCase()}`) : "",
        }));
      }
    }
    if (selectionEvent) {
      values.push(t("events.detail.field_transition", {
        field: t("events.field.server"),
        old: safeServerTarget(previous, previous.server_mode, true),
        next: safeServerTarget(next, next.server_mode, false),
      }));
    }
    for (const key of Object.keys(labels)) {
      if (!(key in previous) || !(key in next)) continue;
      const oldValue = previous[key];
      const newValue = next[key];
      const safeValue = (value) => {
        if (typeof value === "boolean") return t(value ? "common.yes" : "common.no");
        if (typeof value === "number" && Number.isFinite(value)) return String(value);
        if (typeof value === "string" && safeEnum.has(value.toLowerCase())) {
          const normalized = value.toLowerCase();
          return ["direct", "selective", "vpn", "global", "auto", "fixed"].includes(normalized)
            ? t(`events.mode.${normalized}`)
            : t(`events.state.${normalized}`);
        }
        return "";
      };
      const oldLabel = safeValue(oldValue);
      const newLabel = safeValue(newValue);
      if (oldLabel && newLabel && oldValue !== newValue) {
        values.push(t("events.detail.field_transition", {
          field: t(labels[key]), old: oldLabel, next: newLabel,
        }));
      }
    }
    return values.join(" · ");
  }

  function recommendedActionForEvent(event) {
    const severity = String(event?.severity || event?.level || "").toLowerCase();
    const entityType = String(event?.entity_type || "").toLowerCase();
    const eventType = String(event?.event_type || "").toLowerCase();
    if (!["warning", "error", "critical"].includes(severity)) return "";
    if (["job_handler_exception", "watchdog_scheduler_failed"].includes(eventType)) return t("events.job.check_logs");
    if (isLegacyEventCode(event) && eventType.includes("stale")) return t("ux.action.refresh_diagnostics");
    if (entityType === "vpn") return t("ux.action.check_vpn");
    if (entityType === "xray") return t("ux.action.wait_reconnect");
    return t("ux.action.check_diagnostics");
  }

  function toLegacyEvent(event) {
    const rawMessage = String(event?.message || "").trim();
    const type = String(event?.event_type || "");
    const typeLabel = eventTypeLabel(type);
    const translatedMessage = translateBackendMessage(rawMessage);
    const message = typeLabel !== type
      ? typeLabel
      : translatedMessage && translatedMessage !== rawMessage
        ? translatedMessage
        : t("events.type.default");
    const details = { ...(event.details || {}) };
    if (message === t("events.type.default") && rawMessage && rawMessage !== message) {
      details.legacy_raw_message = rawMessage;
    }
    return {
      id: String(event.event_id || ""),
      event_id: String(event.event_id || ""),
      ts: String(event.created_at || ""),
      category: eventCategory(event),
      journal_category: journalCategory(event),
      level: String(event.level || "info"),
      event_type: String(event.event_type || ""),
      event_code: String(event.event_code || event.details?.event_code || ""),
      type: String(event.event_type || ""),
      actor: String(event.actor || ""),
      actor_attribution: String(details.actor_attribution || ""),
      entity_label_source: "missing",
      title: message,
      message,
      created_at: String(event.created_at || ""),
      details,
      subject_id: event.subject_id || null,
      log_source: "operational",
    };
  }

  function toTypedEvent(event, eventClass) {
    const resolvedClass = String(eventClass || event?.event_class || event?.type || "operational").toLowerCase();
    const severity = normalizeEventSeverity({ ...event, event_class: resolvedClass });
    const type = String(event?.event_type || event?.action || "").trim();
    const normalizedForMessage = {
      ...event,
      event_class: resolvedClass,
      level: severity,
      event_type: type,
      event_code: String(event?.event_code || event?.details?.event_code || ""),
      message: event?.message || event?.action || type,
    };
    const eventDetails = { ...(event.details || {}) };
    let entityLabel = safeHumanLabel(event.entity_label);
    if (String(type) === "logical_member_health_transition") {
      const serverLabel = String(eventDetails.logical_server_label || "").trim();
      const memberNumber = Number(eventDetails.member_number);
      if (serverLabel && Number.isInteger(memberNumber) && memberNumber > 0) {
        entityLabel = `${serverLabel} · ${t("events.entity.vpn_member", { number: memberNumber })}`;
      } else if (!entityLabel) {
        entityLabel = t("events.entity.vpn_member_unavailable");
      }
    }
    if (!entityLabel && event.entity_type) {
      const entityType = String(event.entity_type).toLowerCase();
      entityLabel = entityType === "server_assignment" && String(event.entity_id || "") === "vpn-auto"
        ? t("events.entity.vpn_auto_list")
        : entityType === "routing" && String(event.entity_id || "") === "global"
        ? t("events.entity.routing_global")
        : entityType === "module" && /^[a-z][a-z0-9_-]{0,31}$/i.test(String(event.entity_id || ""))
          ? t("events.entity.module", { name: String(event.entity_id) })
          : "";
    }
    normalizedForMessage.entity_label = entityLabel;
    const translatedType = eventTypeLabel(type);
    const explicitUnknown = normalizedForMessage.event_code && !isLegacyEventCode(normalizedForMessage);
    const rawMessage = String(event?.message || "").trim();
    const translatedMessage = translateBackendMessage(rawMessage);
    const unknownTitleKey = resolvedClass === "audit" ? "events.type.audit_change"
      : resolvedClass === "diagnostic" ? "events.type.diagnostic_update" : "events.type.operational_update";
    const message = domainEventMessage({ ...normalizedForMessage, entity_label: normalizedForMessage.entity_label_source === "current" ? "" : entityLabel })
      || (explicitUnknown ? t(unknownTitleKey)
        : translatedType !== type ? translatedType
          : translatedMessage && translatedMessage !== rawMessage
            ? translatedMessage
            : t("events.type.default"));
    if ((explicitUnknown || message === t("events.type.default")) && rawMessage && rawMessage !== message) {
      eventDetails.legacy_raw_message = rawMessage;
    }
    const reason = domainEventReason(normalizedForMessage);
    const recommendation = recommendedActionForEvent({ ...normalizedForMessage, severity });
    const details = eventDetails;
    return {
      id: String(event.event_id || ""),
      event_id: String(event.event_id || ""),
      ts: String(event.timestamp || event.created_at || ""),
      category: eventCategory({ ...event, event_class: resolvedClass, severity }),
      journal_category: journalCategory({ ...event, event_class: resolvedClass, severity }),
      level: severity === "failed" ? "error" : severity,
      severity,
      event_class: resolvedClass,
      event_type: type,
      event_code: normalizedForMessage.event_code,
      type,
      actor: String(event.actor || ""),
      actor_attribution: String(event.actor_attribution || eventDetails.actor_attribution || ""),
      job_type: String(event.job_type || eventDetails.job_type || ""),
      entity_label_source: String(event.entity_label_source || "missing"),
      result: ["success", "failed", "partial", "pending"].includes(String(event.result || eventDetails.result || "").toLowerCase())
        ? String(event.result || eventDetails.result).toLowerCase() : "",
      title: message,
      message,
      reason,
      recommendation,
      created_at: String(event.timestamp || event.created_at || ""),
      details: eventDetails,
      safe_summary: eventSafeSummary({ ...normalizedForMessage, event_class: resolvedClass, details: eventDetails }),
      source: String(event.source || eventDetails.source || ""),
      subject_id: event.subject_id || null,
      entity_type: event.entity_type || null,
      entity_id: event.entity_id || null,
      entity_label: entityLabel,
      connection_id: event.connection_id || null,
      request_id: event.request_id || null,
      job_id: event.job_id || null,
      apply_id: event.apply_id || null,
      log_source: resolvedClass,
    };
  }

  function eventGroupKey(event) {
    return [
      event?.event_class,
      event?.severity || event?.level,
      event?.event_code || event?.event_type || event?.type,
      event?.entity_type,
      event?.entity_id,
      event?.subject_id,
      event?.connection_id,
      event?.outcome || event?.details?.reason_code || "",
      isLegacyEventCode(event) ? (event?.message || event?.title) : "",
    ].map((part) => String(part || "")).join("|");
  }

  function groupRepeatedEvents(items) {
    const grouped = [];
    const byKey = new Map();
    (Array.isArray(items) ? items : []).forEach((item) => {
      const key = eventGroupKey(item);
      const existing = byKey.get(key);
      if (!existing) {
        const clone = { ...item, repeat_count: 1, first_ts: item.ts, last_ts: item.ts, group_ids: [item.id].filter(Boolean) };
        byKey.set(key, clone);
        grouped.push(clone);
        return;
      }
      existing.repeat_count += 1;
      existing.group_ids = [...(existing.group_ids || []), item.id].filter(Boolean);
      existing.first_ts = item.ts || existing.first_ts;
      existing.last_ts = existing.last_ts || item.ts;
    });
    return grouped.map((item, index) => ({ ...item, source_index: index }));
  }

  function toLegacyTechnicalEvent(event) {
    const rawMessage = String(event?.message || "").trim();
    const type = String(event?.event_type || "");
    const typeLabel = eventTypeLabel(type);
    const translatedMessage = translateBackendMessage(rawMessage);
    const message = typeLabel !== type
      ? typeLabel
      : translatedMessage && translatedMessage !== rawMessage
        ? translatedMessage
        : t("events.type.technical_default");
    const details = { ...(event.details || {}) };
    if (message === t("events.type.technical_default") && rawMessage) {
      details.legacy_raw_message = rawMessage;
    }

    return {
      id: String(event.timestamp || event.event_type || ""),
      event_id: String(event.event_id || ""),
      ts: String(event.timestamp || ""),
      category: "diagnostic",
      journal_category: "diagnostic",
      level: String(event.level || "info"),
      event_type: String(event.event_type || ""),
      type: String(event.event_type || ""),
      actor: String(event.component || "system"),
      title: message,
      message,
      created_at: String(event.timestamp || ""),
      details,
      subject_id: null,
      log_source: "technical",
    };
  }

  function toUnixSeconds(value) {
    const ts = Date.parse(String(value || ""));
    return Number.isFinite(ts) ? Math.floor(ts / 1000) : null;
  }

  function isJournalTab(tab) {
    return !["rules", "controls", "diagnostics"].includes(String(tab || "").toLowerCase());
  }

  window.FwrouterSettingsEvents = {
    parseBackendTs,
    formatTs,
    freshnessFor,
    categoryLabel,
    levelLabel,
    eventTypeLabel,
    normalizeEventSeverity,
    eventCategory,
    journalCategory,
    matchesJournalTab,
    mergeAuditEvents,
    toLegacyEvent,
    toLegacyTechnicalEvent,
    toTypedEvent,
    groupRepeatedEvents,
    toUnixSeconds,
    isJournalTab,
  };
})();
