// Settings journal helpers. Pure data shaping/labels for settings.js.
(function () {
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

    return ({
      all: "Все",
      user: "Пользователи",
      server: "Серверы",
      watchdog: "Автоматика",
      routing: "Маршруты",
      settings: "Настройки",
      error: "Ошибки",
      rules: "Правила",
      controls: "Управление",
      system: "Система",
    }[value] || value || "События");
  }

  function levelLabel(level) {
    const value = String(level || "info").toLowerCase();

    return ({
      info: "Норма",
      warning: "Внимание",
      error: "Ошибка",
    }[value] || value);
  }

  function eventTypeLabel(type) {
    const value = String(type || "").trim();

    return ({
      mutation_set_global_mode_success: "Режим роутера применен",
      mutation_set_global_mode_failed: "Ошибка режима роутера",
      mutation_set_selective_default_success: "Selective default сохранен",
      mutation_set_selective_default_failed: "Ошибка selective default",
      mutation_set_global_server_mode_success: "Режим сервера применен",
      mutation_set_global_server_mode_failed: "Ошибка режима сервера",
      mutation_set_subject_admin_mode_success: "Режим клиента применен",
      mutation_set_subject_admin_mode_failed: "Ошибка режима клиента",
      mutation_set_subject_user_mode_success: "Пользовательский режим клиента применен",
      mutation_set_subject_user_mode_failed: "Ошибка пользовательского режима",
      mutation_set_subject_server_override_success: "Сервер клиента выбран",
      mutation_set_subject_server_override_failed: "Ошибка выбора сервера клиента",
      mutation_clear_subject_server_override_success: "Сервер клиента сброшен",
      mutation_clear_subject_server_override_failed: "Ошибка сброса сервера клиента",
      mutation_repair_global_direct_runtime_success: "Маршрутизация восстановлена",
      mutation_repair_global_direct_runtime_failed: "Ошибка восстановления маршрутизации",
      mutation_apply_manual_rules_success: "Правила применены",
      mutation_apply_manual_rules_failed: "Ошибка применения правил",
      routing_live_drift_detected: "Несовпадение текущей маршрутизации",
      routing_artifact_drift_detected: "Несовпадение сохраненной конфигурации",
      rules_full_update_succeeded: "Re-filter обновлен",
      rules_full_update_noop: "Re-filter уже актуален",
      rules_full_update_failed: "Ошибка применения Re-filter",
      rules_full_update_fetch_failed: "Ошибка скачивания Re-filter",
      rules_full_update_policy_failed: "Re-filter не прошел проверку",
      rules_full_update_dnsmasq_failed: "Ошибка dnsmasq после Re-filter",
      rules_manual_update_dnsmasq_failed: "Ошибка dnsmasq после правил",
      startup_mihomo_selector_restored: "VPN-сервер восстановлен при запуске",
      startup_live_routing_recovered: "Текущая маршрутизация восстановлена при запуске",
      subscription_refresh_completed: "Подписка обновлена",
      subscription_refresh_failed: "Ошибка обновления подписки",
      manual_rules_apply_completed: "Правила применены",
      manual_rules_apply_failed: "Ошибка применения правил",
      watchdog_repair_completed: "Автоматика восстановила состояние",
      watchdog_repair_failed: "Ошибка автоматики",
      traffic_accounting_completed: "Учет трафика обновлен",
      traffic_accounting_failed: "Ошибка учета трафика",
      core_bypass_enabled: "Обход FWRouter включен",
      core_bypass_disabled: "Обход FWRouter выключен",
    }[value] || value || "Событие");
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
      title: String(event.message || event.event_type || "Событие"),
      message: String(event.message || ""),
      created_at: String(event.created_at || ""),
      details: event.details || {},
      subject_id: event.subject_id || null,
    };
  }

  function toLegacyTechnicalEvent(event) {
    return {
      id: String(event.timestamp || event.event_type || ""),
      ts: String(event.timestamp || ""),
      category: "system",
      level: String(event.level || "info"),
      event_type: String(event.event_type || ""),
      type: String(event.event_type || ""),
      actor: String(event.component || "system"),
      title: String(event.message || event.event_type || "Техническое событие"),
      message: String(event.message || ""),
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
