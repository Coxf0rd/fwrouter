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

  function toLegacyEvent(event) {
    return {
      id: String(event.event_id || ""),
      ts: String(event.created_at || ""),
      category: eventCategory(event),
      level: String(event.level || "info"),
      event_type: String(event.event_type || ""),
      type: String(event.event_type || ""),
      actor: String(event.subject_id || "system"),
      title: translateBackendMessage(event.message || event.event_type || t("events.type.default")),
      message: translateBackendMessage(event.message || ""),
      created_at: String(event.created_at || ""),
      details: event.details || {},
      subject_id: event.subject_id || null,
    };
  }

  function toLegacyTechnicalEvent(event) {
    const component = String(event.component || "").toLowerCase();
    const type = String(event.event_type || "").toLowerCase();
    const category = component === "watchdog" || type.includes("watchdog") ? "watchdog" : "system";

    return {
      id: String(event.timestamp || event.event_type || ""),
      ts: String(event.timestamp || ""),
      category,
      level: String(event.level || "info"),
      event_type: String(event.event_type || ""),
      type: String(event.event_type || ""),
      actor: String(event.component || "system"),
      title: translateBackendMessage(event.message || event.event_type || t("events.type.technical_default")),
      message: translateBackendMessage(event.message || ""),
      created_at: String(event.timestamp || ""),
      details: event.details || {},
      subject_id: null,
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
    toLegacyEvent,
    toLegacyTechnicalEvent,
    toUnixSeconds,
    isJournalTab,
  };
})();
