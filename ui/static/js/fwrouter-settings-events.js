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

  function eventTypeLabel(type) {
    const value = String(type || "").trim();

    const label = t(`events.type.${value}`);
    return label !== `events.type.${value}` ? label : (value || t("events.type.default"));
  }

  function eventCategory(event) {
    const explicit = String(event?.category || "").toLowerCase();
    if (explicit) return explicit;

    const type = String(event?.event_type || "").toLowerCase();
    if (type.includes("rule")) return "routing";
    if (type.includes("watchdog")) return "watchdog";
    if (type.includes("server") || type.includes("vpn_auto") || type.includes("mihomo")) return "server";
    if (type.includes("routing") || type.includes("subject_mode")) return "routing";
    if (type.includes("subscription") || type.includes("settings")) return "settings";
    if (String(event?.level || "").toLowerCase() === "error") return "error";
    if (event?.subject_id) return "user";
    return "system";
  }

  function eventSearchText(event) {
    const details = event?.details;
    const detailText = details && typeof details === "object"
      ? Object.entries(details).map(([key, value]) => `${key} ${String(value || "")}`).join(" ")
      : "";

    return [
      event?.level,
      event?.event_type,
      event?.type,
      event?.component,
      event?.actor,
      event?.message,
      event?.title,
      event?.subject_id,
      detailText,
    ].join(" ").toLowerCase();
  }

  function isWarningOrError(event) {
    return ["warning", "error"].includes(String(event?.level || "").toLowerCase());
  }

  function isWatchdogEvent(event) {
    return eventSearchText(event).includes("watchdog");
  }

  function isRoutingEvent(event) {
    const text = eventSearchText(event);
    return [
      "routing",
      "route",
      "apply",
      "rules",
      "rule",
      "dataplane",
      "nft",
      "dnsmasq",
      "subject_mode",
      "global_mode",
      "selective",
      "core_bypass",
    ].some((needle) => text.includes(needle));
  }

  function isServerEvent(event) {
    const text = eventSearchText(event);
    return [
      "selector",
      "server",
      "vpn-auto",
      "vpn_auto",
      "custom_server",
      "custom-https",
      "mihomo",
      "subscription",
      "proxy",
    ].some((needle) => text.includes(needle));
  }

  function isSystemEvent(event) {
    if (isWatchdogEvent(event)) return false;

    const text = eventSearchText(event);
    return String(event?.log_source || "").toLowerCase() === "technical"
      || ["maintenance", "scheduler", "runtime_convergence", "convergence", "startup", "bootstrap", "traffic_accounting"]
        .some((needle) => text.includes(needle));
  }

  function journalCategory(event) {
    if (isWatchdogEvent(event)) return "watchdog";
    if (isRoutingEvent(event)) return "routing";
    if (isServerEvent(event)) return "server";
    if (isSystemEvent(event)) return "system";
    if (isWarningOrError(event)) return "error";
    return eventCategory(event);
  }

  function matchesJournalTab(event, tab) {
    const value = String(tab || "all").toLowerCase();
    if (value === "all") return true;
    if (value === "error") return isWarningOrError(event);
    if (value === "watchdog") return isWatchdogEvent(event);
    if (value === "routing") return isRoutingEvent(event);
    if (value === "server") return isServerEvent(event);
    if (value === "system") return isSystemEvent(event);
    return journalCategory(event) === value;
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
    return !["rules", "controls"].includes(String(tab || "").toLowerCase());
  }

  window.FwrouterSettingsEvents = {
    parseBackendTs,
    formatTs,
    categoryLabel,
    levelLabel,
    eventTypeLabel,
    eventCategory,
    journalCategory,
    matchesJournalTab,
    toLegacyEvent,
    toLegacyTechnicalEvent,
    toUnixSeconds,
    isJournalTab,
  };
})();
