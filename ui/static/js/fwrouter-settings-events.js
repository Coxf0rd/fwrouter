// Settings journal helpers. Pure data shaping/labels for settings.js.
(function () {
  const { translateBackendMessage } = window.FwrouterUI;
  const t = (key) => window.FwrouterI18n?.t(key) || key;
  const APP_TIME_ZONE = "Asia/Krasnoyarsk";
  const DATE_TIME_FORMAT = new Intl.DateTimeFormat("ru-RU", {
    timeZone: APP_TIME_ZONE,
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });

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

  function formatTs(ts) {
    if (!ts) return "";

    try {
      const parsed = parseBackendTs(ts);
      if (!parsed || Number.isNaN(parsed.getTime())) return String(ts || "");
      return DATE_TIME_FORMAT.format(parsed);
    } catch (_) {
      return String(ts || "");
    }
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
    const eventClass = String(event?.event_class || event?.classification || event?.type || "").toLowerCase();
    const raw = String(event?.severity || event?.level || (event?.result === "failure" ? "error" : "info")).toLowerCase();
    const eventType = String(event?.event_type || event?.action || "").toLowerCase();
    const userImpact = Boolean(event?.entity_type || event?.entity_id || event?.subject_id || event?.connection_id);

    if (eventClass === "diagnostic" && !userImpact) return "info";
    if (raw === "critical") return "critical";
    if (["failed", "failure", "error"].includes(raw) || eventType.endsWith("_failed") || eventType === "runtime_failed") {
      return eventClass === "diagnostic" && !userImpact ? "info" : "error";
    }
    if (["warning", "degraded", "drift", "stale"].includes(raw) || eventType === "reconcile_drift") return "warning";
    return "info";
  }

  function eventTypeLabel(type) {
    const value = String(type || "").trim();

    const label = t(`events.type.${value}`);
    return label !== `events.type.${value}` ? label : (value || t("events.type.default"));
  }

  function eventCategory(event) {
    const explicit = String(event?.category || "").toLowerCase();
    if (explicit) return explicit;

    const eventClass = String(event?.event_class || event?.classification || "").toLowerCase();
    if (eventClass === "diagnostic") return "diagnostic";
    const entityType = String(event?.entity_type || "").toLowerCase();
    if (entityType === "watchdog") return "watchdog";
    if (entityType === "routing" || entityType === "rules") return "routing";
    if (entityType === "vpn" || entityType === "server" || entityType === "connection") return "server";
    if (entityType === "subject" || event?.subject_id) return "user";
    if (entityType === "module" || entityType === "system" || entityType === "database") return "system";
    if (eventClass === "audit") return "audit";
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
    return category === value;
  }

  function eventDisplayMessage(event, fallbackKey) {
    const raw = String(event?.message || "").trim();
    const translated = translateBackendMessage(raw || event?.event_type || t(fallbackKey));
    const typeLabel = eventTypeLabel(event?.event_type);
    const typeRaw = String(event?.event_type || "");
    const wantsNonRussian = (window.FwrouterI18n?.locale?.() || "ru") !== "ru";
    if (wantsNonRussian && (!raw || raw === typeRaw) && typeLabel && typeLabel !== typeRaw) {
      return typeLabel;
    }
    return translated;
  }

  function domainEventMessage(event) {
    const eventClass = String(event?.event_class || "").toLowerCase();
    const entityType = String(event?.entity_type || "").toLowerCase();
    const eventType = String(event?.event_type || event?.action || "").toLowerCase();
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
    const rawReason = String(event?.reason || details.reason || details.reason_code || details.error || "").trim();
    const eventType = String(event?.event_type || event?.action || "").toLowerCase();
    if (eventType === "vpn_auto_server_switched") return t("events.reason.vpn_quality_degraded");
    if (eventType === "reconcile_drift") return t("events.reason.reconcile_drift");
    if (rawReason) return translateBackendMessage(rawReason);
    return "";
  }

  function recommendedActionForEvent(event) {
    const severity = String(event?.severity || event?.level || "").toLowerCase();
    const entityType = String(event?.entity_type || "").toLowerCase();
    const eventType = String(event?.event_type || "").toLowerCase();
    if (!["warning", "error", "critical"].includes(severity)) return "";
    if (eventType.includes("stale")) return t("ux.action.refresh_diagnostics");
    if (entityType === "vpn") return t("ux.action.check_vpn");
    if (entityType === "xray") return t("ux.action.wait_reconnect");
    return t("ux.action.check_diagnostics");
  }

  function toLegacyEvent(event) {
    const message = eventDisplayMessage(event, "events.type.default");
    return {
      id: String(event.event_id || ""),
      ts: String(event.created_at || ""),
      category: eventCategory(event),
      journal_category: journalCategory(event),
      level: String(event.level || "info"),
      event_type: String(event.event_type || ""),
      type: String(event.event_type || ""),
      actor: String(event.subject_id || "system"),
      title: message,
      message,
      created_at: String(event.created_at || ""),
      details: event.details || {},
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
      message: event?.message || event?.action || type,
    };
    const message = domainEventMessage(normalizedForMessage)
      || eventDisplayMessage(normalizedForMessage, "events.type.default");
    const reason = domainEventReason(normalizedForMessage);
    const recommendation = recommendedActionForEvent({ ...normalizedForMessage, severity });
    return {
      id: String(event.event_id || ""),
      ts: String(event.timestamp || event.created_at || ""),
      category: eventCategory({ ...event, event_class: resolvedClass, severity }),
      journal_category: journalCategory({ ...event, event_class: resolvedClass, severity }),
      level: severity === "failed" ? "error" : severity,
      severity,
      event_class: resolvedClass,
      event_type: type,
      type,
      actor: String(event.actor || event.source || event.entity_type || "system"),
      title: message,
      message,
      reason,
      recommendation,
      created_at: String(event.timestamp || event.created_at || ""),
      details: event.details || {},
      subject_id: event.subject_id || null,
      entity_type: event.entity_type || null,
      entity_id: event.entity_id || null,
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
      event?.event_type || event?.type,
      event?.entity_type,
      event?.entity_id,
      event?.subject_id,
      event?.connection_id,
      event?.message || event?.title,
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
    const component = String(event.component || "").toLowerCase();
    const type = String(event.event_type || "").toLowerCase();
    const category = component === "watchdog" || type.includes("watchdog") ? "watchdog" : "system";

    const message = eventDisplayMessage(event, "events.type.technical_default");

    return {
      id: String(event.timestamp || event.event_type || ""),
      ts: String(event.timestamp || ""),
      category: String(event.category || category).toLowerCase(),
      journal_category: journalCategory({
        ...event,
        actor: event.component,
        category: String(event.category || category).toLowerCase(),
        log_source: "technical",
      }),
      level: String(event.level || "info"),
      event_type: String(event.event_type || ""),
      type: String(event.event_type || ""),
      actor: String(event.component || "system"),
      title: message,
      message,
      created_at: String(event.timestamp || ""),
      details: event.details || {},
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
    categoryLabel,
    levelLabel,
    eventTypeLabel,
    normalizeEventSeverity,
    eventCategory,
    journalCategory,
    matchesJournalTab,
    toLegacyEvent,
    toLegacyTechnicalEvent,
    toTypedEvent,
    groupRepeatedEvents,
    toUnixSeconds,
    isJournalTab,
  };
})();
