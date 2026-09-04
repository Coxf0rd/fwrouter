// Domain-state renderers for Settings routing and diagnostics views.
(function () {
  const { escapeHtml, translateBackendMessage } = window.FwrouterUI;
  const t = (key, params) => window.FwrouterI18n?.t(key, params) || key;
  const {
    settingsModeLabel,
    domainCategoryLabel,
    subjectDomainCategory,
    implementationLabel,
  } = window.FwrouterLabels;

  function statusClass(value) {
    const raw = String(value || "ok").toLowerCase();
    return raw === "failed" ? "error" : raw;
  }

  function routingDestinationFor(subject) {
    const effective = subject?.effective || {};
    const mode = String(effective.mode || subject?.intent?.mode || "").toLowerCase();
    if (effective.selected_server_id) return t("routing.policy.destination.selected_vpn_path");
    if (mode === "vpn" || effective.dataplane_path === "vpn") return t("routing.policy.destination.vpn_path");
    if (mode === "selective" || effective.dataplane_path === "selective") return t("routing.policy.destination.policy_match");
    if (mode === "disabled") return t("routing.policy.destination.disabled");
    return t("routing.policy.destination.direct");
  }

  function routingReasonFor(subject, routing) {
    const reason = subject?.reason || {};
    const code = String(reason.code || reason.mode_source || "").toLowerCase();
    if (code) {
      const key = `routing.policy.reason.${code}`;
      const translated = t(key);
      if (translated !== key) return translated;
    }
    const globalMode = String(routing?.effective?.desired_global_mode || routing?.intent?.mode || "").toLowerCase();
    if (globalMode) return t("routing.policy.reason.global_mode", { mode: settingsModeLabel(globalMode) });
    return t("routing.policy.reason.state_projection");
  }

  function renderRoutingPolicyHtml(payload) {
    const subjects = Array.isArray(payload?.subjects?.items) ? payload.subjects.items : [];
    const routing = payload?.routing?.routing || payload?.routing || {};
    const reconcile = Array.isArray(payload?.reconcile?.entities) ? payload.reconcile.entities : [];
    const driftCount = reconcile.filter((item) => ["drift", "failed"].includes(String(item.reconcile_state || "").toLowerCase())).length;
    const rows = subjects.slice(0, 80).map((subject) => {
      const entity = subject.entity || {};
      const label = subject.identity?.display_name || entity.label || entity.id || t("subject.kind.client");
      const category = domainCategoryLabel(subjectDomainCategory(entity.role || subject.intent?.details?.implementation_kind));
      const implementation = implementationLabel(subject.intent?.details?.implementation_kind);
      const destination = routingDestinationFor(subject);
      const decision = settingsModeLabel(subject.effective?.mode || subject.intent?.mode || "");
      const reason = routingReasonFor(subject, routing);
      return `
        <div class="settings-domain-row">
          <div>
            <div class="settings-domain-row__title">${escapeHtml(label)}</div>
            <div class="muted">${escapeHtml(category)}</div>
          </div>
          <div class="settings-domain-row__arrow" aria-hidden="true">→</div>
          <div>
            <div class="settings-domain-row__title">${escapeHtml(destination)}</div>
            <div class="muted">${escapeHtml(decision)}</div>
          </div>
          <div>
            <span class="pill">${escapeHtml(reason)}</span>
            ${implementation ? `<div class="muted mono">${escapeHtml(t("inventory.info.implementation"))}: ${escapeHtml(implementation)}</div>` : ""}
          </div>
        </div>
      `;
    }).join("");

    return `
      <div class="settings-domain-panel">
        <div class="settings-domain-panel__head">
          <div>
            <div class="label">${escapeHtml(t("routing.policy.title"))}</div>
            <div class="muted">${escapeHtml(t("routing.policy.meta", { count: subjects.length, drift: driftCount }))}</div>
          </div>
          <span class="pill settings-event__level--${escapeHtml(statusClass(routing.projection?.state || routing.reconcile?.state || "ok"))}">
            ${escapeHtml(routing.reconcile?.state || routing.projection?.state || "ok")}
          </span>
        </div>
        <div class="settings-domain-list">
          ${rows || `<div class="settings-events__empty muted">${escapeHtml(t("routing.policy.empty"))}</div>`}
        </div>
      </div>
    `;
  }

  function sectionLabel(name) {
    const key = `diagnostics.section.${String(name || "").toLowerCase()}`;
    const label = t(key);
    return label !== key ? label : String(name || "");
  }

  function problemEntityLabel(problem) {
    const entityType = String(problem?.entity_type || "").toLowerCase();
    if (entityType === "xray") return t("diagnostics.entity.external_client_connection");
    if (entityType === "vpn") return t("diagnostics.entity.vpn_connection");
    if (entityType === "routing") return t("diagnostics.entity.routing_policy");
    if (entityType === "subject") return t("diagnostics.entity.subject");
    return sectionLabel(entityType || "system");
  }

  function problemImplementation(problem) {
    const source = String(problem?.source || "").toLowerCase();
    const details = problem?.details || {};
    return implementationLabel(details.implementation || details.implementation_kind || (source.includes("xray") ? "xray" : ""));
  }

  function renderDiagnosticsHtml(report) {
    const sections = report?.sections && typeof report.sections === "object" ? report.sections : {};
    const problems = Array.isArray(report?.problems) ? report.problems : [];
    const sectionRows = ["database", "subjects", "routing", "vpn", "watchdog", "xray", "events"].map((name) => {
      const section = sections[name] || {};
      const status = String(section.status || "ok").toLowerCase();
      const label = name === "xray" ? t("diagnostics.section.external_integrations") : sectionLabel(name);
      return `
        <div class="settings-domain-row settings-domain-row--compact">
          <div class="settings-domain-row__title">${escapeHtml(label)}</div>
          <span class="settings-event__level settings-event__level--${escapeHtml(statusClass(status))}">${escapeHtml(status)}</span>
        </div>
      `;
    }).join("");
    const problemRows = problems.slice(0, 20).map((problem) => {
      const implementation = problemImplementation(problem);
      return `
        <div class="settings-event-context__detail">
          <div class="settings-event-context__key">${escapeHtml(problemEntityLabel(problem))}</div>
          <div class="settings-event-context__value">
            <strong>${escapeHtml(translateBackendMessage(problem.reason || ""))}</strong>
            <div class="muted mono">${escapeHtml(problem.entity_id || "")}</div>
            ${implementation ? `<div class="muted mono">${escapeHtml(t("inventory.info.implementation"))}: ${escapeHtml(implementation)}</div>` : ""}
          </div>
        </div>
      `;
    }).join("");

    return `
      <div class="settings-domain-panel">
        <div class="settings-domain-panel__head">
          <div>
            <div class="label">${escapeHtml(t("diagnostics.title"))}</div>
            <div class="muted">${escapeHtml(t("diagnostics.generated", { time: report?.generated_at || "" }))}</div>
          </div>
          <span class="settings-event__level settings-event__level--${escapeHtml(statusClass(report?.status || "ok"))}">
            ${escapeHtml(String(report?.status || "ok").toUpperCase())}
          </span>
        </div>
        <div class="settings-domain-list">${sectionRows}</div>
        <div class="settings-event-context__details">
          ${problemRows || `<div class="settings-event-context__empty-detail muted">${escapeHtml(t("diagnostics.problems.none"))}</div>`}
        </div>
      </div>
    `;
  }

  window.FwrouterSettingsDomainState = {
    renderRoutingPolicyHtml,
    renderDiagnosticsHtml,
    routingDestinationFor,
    routingReasonFor,
    problemEntityLabel,
    problemImplementation,
  };
})();
