// Shared UI labels for mode/source/runtime/kind values.
(function () {
  const t = (key) => window.FwrouterI18n?.t(key) || key;

  function subjectDomainCategory(value) {
    const raw = typeof value === "object" && value !== null
      ? String(value.domain_category || value.subject_role || value.inventory_role || value.implementation_kind || "").toLowerCase()
      : String(value || "").toLowerCase();
    return ({
      local_client: "local_client",
      lan: "local_client",
      lan_client: "local_client",
      external_client: "external_client",
      explicit_external_client: "external_client",
      vless_client: "external_client",
      xray: "external_client",
      external_network_source: "external_network_source",
      external_network_client: "external_network_source",
      tailscale: "external_network_source",
      tailscale_node: "external_network_source",
      service: "service",
      docker: "service",
      docker_runtime: "service",
      host: "service",
      host_runtime: "service",
      infrastructure: "infrastructure",
      fwrouter: "infrastructure",
      router_core: "infrastructure",
    }[raw] || "local_client");
  }

  function domainCategoryLabel(category) {
    const value = subjectDomainCategory(category);
    return ({
      local_client: t("domain.category.local_client"),
      external_client: t("domain.category.external_client"),
      external_network_source: t("domain.category.external_network_source"),
      service: t("domain.category.service"),
      infrastructure: t("domain.category.infrastructure"),
    }[value] || t("subject.kind.client"));
  }

  function implementationLabel(value) {
    const raw = typeof value === "object" && value !== null
      ? String(value.implementation_label || value.implementation_kind || value.subject_type || "").toLowerCase()
      : String(value || "").toLowerCase();
    return ({
      lan: "LAN",
      xray: "Xray/VLESS",
      explicit_external_client: "Xray/VLESS",
      tailscale: "Tailscale",
      tailscale_node: "Tailscale",
      docker: "Docker",
      host: "Host",
      fwrouter: "FWRouter",
      mihomo: "Mihomo",
    }[raw] || String(value?.implementation_label || value || "").trim());
  }

  function settingsSubjectKindLabel(kind) {
    return domainCategoryLabel(subjectDomainCategory(kind));
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

  function presentationState(input) {
    const raw = typeof input === "object" && input !== null ? input : { state: input };
    const desiredMode = String(raw.desired_mode || raw.intent_mode || raw.mode || "").toLowerCase();
    const reconcile = String(raw.reconcile_state || raw.reconcile?.state || "").toLowerCase();
    const projection = String(raw.projection_state || raw.projection?.state || raw.status || raw.state || "").toLowerCase();
    const runtime = String(raw.runtime_state || raw.observed_state || raw.health || "").toLowerCase();
    const severity = String(raw.severity || raw.level || "").toLowerCase();
    const errorCode = String(raw.error_code || raw.reason_code || raw.reason || "").toLowerCase();
    const activeKnown = raw.is_active !== undefined || raw.active !== undefined;
    const isActive = raw.is_active !== undefined ? Boolean(raw.is_active) : Boolean(raw.active);
    const candidates = [severity, reconcile, projection, runtime, errorCode].filter(Boolean);

    if (desiredMode === "disabled" || candidates.includes("disabled")) {
      return {
        state: "disabled",
        severity: "inactive",
        label: t("ux.state.disabled"),
        summary: t("ux.state.disabled.summary"),
        action: "",
      };
    }

    if ((activeKnown && !isActive) || candidates.includes("inactive") || candidates.includes("stopped") || candidates.includes("not_configured")) {
      return {
        state: "inactive",
        severity: "inactive",
        label: t("ux.state.inactive"),
        summary: t("ux.state.inactive.summary"),
        action: t("ux.action.wait_reconnect"),
      };
    }

    if (candidates.some((value) => ["failed", "failure", "error", "unavailable", "runtime_failed", "critical"].includes(value))) {
      return {
        state: "failed",
        severity: "error",
        label: t("ux.state.failed"),
        summary: t("ux.state.failed.summary"),
        action: raw.entity_type === "vpn" ? t("ux.action.check_vpn") : t("ux.action.check_diagnostics"),
      };
    }

    if (candidates.some((value) => ["drift", "degraded", "missing", "runtime_drift", "failed_adapter"].includes(value))) {
      return {
        state: "degraded",
        severity: "warning",
        label: t("ux.state.degraded"),
        summary: t("ux.state.degraded.summary"),
        action: t("ux.action.check_diagnostics"),
      };
    }

    if (candidates.some((value) => ["stale", "unknown", "warning", "pending", "observation_stale", "intent_newer_than_runtime"].includes(value))) {
      return {
        state: "warning",
        severity: "warning",
        label: t("ux.state.warning"),
        summary: t("ux.state.warning.summary"),
        action: t("ux.action.refresh_diagnostics"),
      };
    }

    return {
      state: "healthy",
      severity: "info",
      label: t("ux.state.healthy"),
      summary: t("ux.state.healthy.summary"),
      action: "",
    };
  }

  function presentationLevelClass(state) {
    const ux = typeof state === "object" && state !== null ? state : presentationState(state);
    return ({
      healthy: "info",
      warning: "warning",
      degraded: "warning",
      failed: "error",
      inactive: "inactive",
      disabled: "inactive",
    }[ux.state] || "info");
  }

  function settingsModeOptions(client) {
    const category = subjectDomainCategory(client);
    if (category === "external_client") return ["direct", "selective", "vpn", "disabled"];
    if (category === "service" || category === "infrastructure") return ["direct", "vpn", "disabled"];
    return ["global", "direct", "selective", "vpn", "disabled"];
  }

  function defaultEnabledModeFor(client) {
    const category = subjectDomainCategory(client);
    if (category === "external_client" || category === "service" || category === "infrastructure") return "direct";
    return "global";
  }

  window.FwrouterLabels = {
    subjectDomainCategory,
    domainCategoryLabel,
    implementationLabel,
    settingsSubjectKindLabel,
    settingsModeLabel,
    compactModeLabel,
    settingsSourceLabel,
    compactSourceLabel,
    runtimeLabel,
    presentationState,
    presentationLevelClass,
    settingsModeOptions,
    defaultEnabledModeFor,
  };
})();
