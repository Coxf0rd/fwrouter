// Shared UI labels for mode/source/runtime/kind values.
(function () {
  const t = (key) => window.FwrouterI18n?.t(key) || key;

  function settingsSubjectKindLabel(kind) {
    const value = String(kind || "").toLowerCase();
    return ({
      lan: t("subject.kind.lan"),
      lan_client: t("subject.kind.lan"),
      external_network_source: t("subject.kind.external_network_source"),
      vless_client: t("subject.kind.vless_client"),
      docker_runtime: t("subject.kind.docker"),
      host_runtime: t("subject.kind.host"),
    }[value] || value || t("subject.kind.client"));
  }

  function settingsModeLabel(mode) {
    const value = String(mode || "").toLowerCase();
    return ({
      global: t("mode.global"),
      direct: t("mode.direct"),
      selective: t("mode.selective"),
      vpn: t("mode.vpn"),
      disabled: t("mode.disabled"),
      enabled: t("mode.enabled"),
      forced_vpn: t("mode.forced_vpn"),
    }[value] || value || "-");
  }

  function compactModeLabel(mode) {
    const value = String(mode || "").toUpperCase();
    if (value === "GLOBAL") return t("mode.global");
    if (value === "DIRECT") return t("mode.direct");
    if (value === "VPN") return t("mode.vpn");
    if (value === "DISABLED") return t("mode.compact.disabled");
    if (value === "ENABLED") return t("mode.compact.enabled");
    return t("mode.selective");
  }

  function settingsSourceLabel(source) {
    const value = String(source || "").toLowerCase();
    return ({
      global: t("source.global"),
      admin_override: t("source.admin_override"),
      user_override: t("source.user_override"),
      vless_forced_vpn: t("source.vless_forced_vpn"),
      inherited: t("source.inherited"),
    }[value] || value || "-");
  }

  function compactSourceLabel(source) {
    const value = String(source || "").trim().toLowerCase();

    if (value === "vpn-auto") return "VPN-auto";
    if (value === "global") return t("source.compact.global");
    if (value === "manual") return t("source.compact.manual");
    if (value === "admin_locked" || value === "admin_override") return t("source.compact.admin");
    if (value === "user_override") return t("source.compact.user");
    if (value === "inherited") return t("source.compact.inherited");

    return value ? value : t("source.compact.global");
  }

  function runtimeLabel(value) {
    const normalized = String(value || "").toLowerCase();
    return ({
      active: t("runtime.active"),
      inactive: t("runtime.inactive"),
      running: t("runtime.running"),
      stopped: t("runtime.stopped"),
      failed: t("runtime.failed"),
      degraded: t("runtime.degraded"),
      missing: t("runtime.missing"),
      not_configured: t("runtime.not_configured"),
    }[normalized] || normalized || "-");
  }

  function settingsModeOptions(client) {
    const role = String(client?.inventory_role || "");
    if (role === "vless_client") return ["direct", "selective", "vpn", "disabled"];
    if (role === "docker_runtime" || role === "host_runtime") return ["direct", "vpn", "disabled"];
    return ["global", "direct", "selective", "vpn", "disabled"];
  }

  function defaultEnabledModeFor(client) {
    const role = String(client?.inventory_role || "").toLowerCase();
    if (role === "vless_client") return "direct";
    if (role === "docker_runtime" || role === "host_runtime") return "direct";
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
