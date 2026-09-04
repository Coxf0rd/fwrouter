// Domain-state renderers for Settings routing and diagnostics views.
(function () {
  const { escapeHtml, translateBackendMessage } = window.FwrouterUI;
  const t = (key, params) => window.FwrouterI18n?.t(key, params) || key;
  const {
    settingsModeLabel,
    domainCategoryLabel,
    subjectDomainCategory,
    implementationLabel,
    presentationState,
    presentationLevelClass,
  } = window.FwrouterLabels;

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

  function rulesSummaryFromPayload(payload) {
    return payload?.rulesSummary || payload?.rules?.legacy?.raw || payload?.rules?.rules?.legacy?.raw || {};
  }

  function sourceLabel(source) {
    const key = `routing.rules.source.${String(source || "").toLowerCase()}`;
    const label = t(key);
    return label !== key ? label : String(source || t("routing.rules.source.unknown"));
  }

  function ruleDestination(rule) {
    const value = String(rule?.value || "").trim();
    if (!value) return t("routing.rules.destination.all");
    const kind = String(rule?.kind || rule?.match || "").toLowerCase();
    if (kind.includes("domain_suffix")) return t("routing.rules.destination.domain_suffix", { value });
    if (kind.includes("domain")) return t("routing.rules.destination.domain", { value });
    if (kind.includes("cidr")) return t("routing.rules.destination.network", { value });
    return value;
  }

  function ruleReason(rule) {
    const parts = [
      sourceLabel(rule?.source),
      rule?.line ? t("routing.rules.line", { line: rule.line }) : "",
      rule?.match ? String(rule.match) : "",
    ].filter(Boolean);
    return parts.join(" · ");
  }

  function ruleRowsFromSummary(summary) {
    const rows = [];
    const manualRules = summary?.manual?.active_validation?.rules || summary?.manual?.draft_validation?.rules || [];
    manualRules.forEach((rule) => rows.push({ ...rule, source: rule.source || "manual" }));

    const metadata = Array.isArray(summary?.metadata) ? summary.metadata : [];
    metadata.forEach((item) => {
      const type = String(item.ruleset_type || item.ruleset_id || "").toLowerCase();
      if (!type || type === "manual" || type === "effective") return;
      const count = Number(item.metadata_json?.count || item.metadata_json?.effective_counts?.total || 0);
      rows.push({
        source: type,
        value: count ? t("routing.rules.destination.count", { count: count.toLocaleString("ru-RU") }) : "",
        action: type.includes("vpn") ? "VPN" : "DIRECT",
        kind: "ruleset",
        match: "ruleset",
        count,
      });
    });

    const effectiveCounts = summary?.metadata
      ?.find?.((item) => String(item.ruleset_type || "") === "effective")
      ?.metadata_json?.effective_counts || summary?.manual?.effective?.effective_counts || {};
    const protectedCount = Number(effectiveCounts.protected || 0);
    if (protectedCount && !rows.some((row) => String(row.source) === "protected")) {
      rows.unshift({
        source: "protected",
        value: t("routing.rules.destination.count", { count: protectedCount.toLocaleString("ru-RU") }),
        action: "DIRECT",
        kind: "ruleset",
        match: "protected",
        count: protectedCount,
      });
    }

    const defaultAction = String(summary?.state?.selective_default || summary?.manual?.effective?.default_action || "").toUpperCase();
    if (defaultAction) {
      rows.push({
        source: "selective_default",
        value: t("routing.rules.destination.unmatched"),
        action: defaultAction,
        kind: "default",
        match: "default",
      });
    }
    return rows;
  }

  function renderRoutingPolicyHtml(payload) {
    const subjects = Array.isArray(payload?.subjects?.items) ? payload.subjects.items : [];
    const routing = payload?.routing?.routing || payload?.routing || {};
    const reconcile = Array.isArray(payload?.reconcile?.entities) ? payload.reconcile.entities : [];
    const driftCount = reconcile.filter((item) => ["drift", "failed"].includes(String(item.reconcile_state || "").toLowerCase())).length;
    const routingState = presentationState(driftCount ? "drift" : (routing.projection?.state || routing.reconcile?.state || "ok"));
    const summary = rulesSummaryFromPayload(payload);
    const ruleRows = ruleRowsFromSummary(summary);
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
    const ruleRowsHtml = ruleRows.map((rule) => `
      <div class="settings-domain-row settings-domain-row--rule">
        <div>
          <div class="settings-domain-row__title">${escapeHtml(sourceLabel(rule.source))}</div>
          <div class="muted">${escapeHtml(t("routing.rules.scope"))}</div>
        </div>
        <div class="settings-domain-row__arrow" aria-hidden="true">→</div>
        <div>
          <div class="settings-domain-row__title">${escapeHtml(ruleDestination(rule))}</div>
          <div class="muted">${escapeHtml(String(rule.kind || ""))}</div>
        </div>
        <div>
          <span class="pill">${escapeHtml(settingsModeLabel(rule.action || ""))}</span>
          <div class="muted">${escapeHtml(ruleReason(rule))}</div>
        </div>
      </div>
    `).join("");
    const totalRules = Number(
      summary?.metadata?.find?.((item) => String(item.ruleset_type || "") === "effective")?.metadata_json?.effective_counts?.total
      || summary?.manual?.effective?.effective_counts?.total
      || ruleRows.length
      || 0
    );

    return `
      <div class="settings-domain-panel">
        <div class="settings-domain-panel__head">
          <div>
            <div class="label">${escapeHtml(t("routing.policy.title"))}</div>
            <div class="muted">${escapeHtml(t("routing.policy.meta", { count: subjects.length, drift: driftCount }))}</div>
            <div class="muted">${escapeHtml(t("routing.rules.meta", { count: totalRules.toLocaleString("ru-RU") }))}</div>
          </div>
          <span class="pill settings-event__level--${escapeHtml(presentationLevelClass(routingState))}">
            ${escapeHtml(routingState.label)}
          </span>
        </div>
        <div class="settings-domain-list">
          <div class="label">${escapeHtml(t("routing.rules.title"))}</div>
          ${ruleRowsHtml || `<div class="settings-events__empty muted">${escapeHtml(t("routing.rules.empty"))}</div>`}
        </div>
        <div class="settings-domain-list">
          <div class="label">${escapeHtml(t("routing.policy.subjects_title"))}</div>
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
    const reportState = presentationState(report?.status || "ok");
    const sectionRows = ["database", "subjects", "connections", "routing", "vpn", "watchdog", "xray", "events"].map((name) => {
      const section = sections[name] || {};
      const uxState = presentationState(section.status || "ok");
      const label = name === "xray" ? t("diagnostics.section.external_integrations") : sectionLabel(name);
      return `
        <div class="settings-domain-row settings-domain-row--compact">
          <div class="settings-domain-row__title">${escapeHtml(label)}</div>
          <span class="settings-event__level settings-event__level--${escapeHtml(presentationLevelClass(uxState))}">${escapeHtml(uxState.label)}</span>
        </div>
      `;
    }).join("");
    const problemRows = problems.slice(0, 20).map((problem) => {
      const uxState = presentationState(problem);
      const implementation = problemImplementation(problem);
      const action = uxState.action || t("ux.action.check_diagnostics");
      return `
        <div class="settings-event-context__detail">
          <div class="settings-event-context__key">${escapeHtml(problemEntityLabel(problem))}</div>
          <div class="settings-event-context__value">
            <strong>${escapeHtml(translateBackendMessage(problem.reason || ""))}</strong>
            <div class="muted">${escapeHtml(t("journal.field.recommended_action"))}: ${escapeHtml(action)}</div>
            <details class="admin-advanced">
              <summary>${escapeHtml(t("journal.advanced_details"))}</summary>
              <div class="muted mono">${escapeHtml(problem.entity_id || "")}</div>
              <div class="muted mono">${escapeHtml(t("journal.field.source"))}: ${escapeHtml(problem.source || "")}</div>
              ${implementation ? `<div class="muted mono">${escapeHtml(t("inventory.info.implementation"))}: ${escapeHtml(implementation)}</div>` : ""}
            </details>
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
          <span class="settings-event__level settings-event__level--${escapeHtml(presentationLevelClass(reportState))}">
            ${escapeHtml(reportState.label)}
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
    ruleRowsFromSummary,
  };
})();
