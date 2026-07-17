// Shared UI labels for mode/source/runtime/kind values.
(function () {
  function settingsSubjectKindLabel(kind) {
    const value = String(kind || "").toLowerCase();
    return ({
      lan: "LAN",
      tailscale: "Tailscale",
      tailscale_node: "Tailscale",
      xray: "Xray",
      docker: "Docker",
      host: "Host",
    }[value] || value || "Клиент");
  }

  function settingsModeLabel(mode) {
    const value = String(mode || "").toLowerCase();
    return ({
      global: "Global",
      direct: "Direct",
      selective: "Selective",
      vpn: "VPN",
      disabled: "Отключен",
      enabled: "Включен",
      forced_vpn: "VPN принудительно",
    }[value] || value || "—");
  }

  function compactModeLabel(mode) {
    const value = String(mode || "").toUpperCase();
    if (value === "GLOBAL") return "Global";
    if (value === "DIRECT") return "Direct";
    if (value === "VPN") return "VPN";
    if (value === "DISABLED") return "Откл.";
    if (value === "ENABLED") return "Вкл.";
    return "Selective";
  }

  function settingsSourceLabel(source) {
    const value = String(source || "").toLowerCase();
    return ({
      global: "Глобальный режим",
      admin_override: "Админ-настройка",
      user_override: "Пользователь",
      xray_forced_vpn: "Xray",
      inherited: "Наследуется",
    }[value] || value || "—");
  }

  function compactSourceLabel(source) {
    const value = String(source || "").trim().toLowerCase();

    if (value === "vpn-auto") return "VPN-auto";
    if (value === "global") return "глобально";
    if (value === "manual") return "вручную";
    if (value === "admin_locked" || value === "admin_override") return "админ";
    if (value === "user_override") return "польз.";
    if (value === "inherited") return "наслед.";

    return value ? value : "глобально";
  }

  function runtimeLabel(value) {
    const normalized = String(value || "").toLowerCase();
    return ({
      active: "Активен",
      inactive: "Неактивен",
      running: "Работает",
      stopped: "Остановлен",
      failed: "Ошибка",
      degraded: "Проблема",
      missing: "Не найден",
      not_configured: "Не настроен",
    }[normalized] || normalized || "—");
  }

  function settingsModeOptions(client) {
    const kind = String(client?.kind || "");
    if (kind === "xray") return ["enabled", "direct", "selective", "vpn", "disabled"];
    if (kind === "docker" || kind === "host") return ["direct", "vpn", "disabled"];
    return ["global", "direct", "selective", "vpn", "disabled"];
  }

  function defaultEnabledModeFor(client) {
    const kind = String(client?.kind || "").toLowerCase();
    if (kind === "xray") return "enabled";
    if (kind === "docker" || kind === "host") return "direct";
    return "global";
  }

  window.FwrouterLabels = {
    settingsSubjectKindLabel,
    settingsModeLabel,
    compactModeLabel,
    settingsSourceLabel,
    compactSourceLabel,
    runtimeLabel,
    settingsModeOptions,
    defaultEnabledModeFor,
  };
})();
