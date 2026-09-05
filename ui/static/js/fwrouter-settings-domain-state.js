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
  const freshnessFor = (...args) => window.FwrouterSettingsEvents?.freshnessFor?.(...args) || {
    state: "unknown",
    text: "",
    title: "",
  };

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

  function formatLocaleNumber(value) {
    return Number(value || 0).toLocaleString(window.FwrouterI18n?.locale?.() || "ru-RU");
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
    if (Array.isArray(rule?.actions) && rule.actions.length) {
      return rule.actions.map((action) => settingsModeLabel(action)).join(" · ");
    }
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
    if (manualRules.length) {
      const actions = [...new Set(manualRules.map((rule) => String(rule.action || "").toUpperCase()).filter(Boolean))];
      rows.push({
        source: "manual",
        value: t("routing.rules.destination.count", { count: formatLocaleNumber(manualRules.length) }),
        action: actions.length === 1 ? actions[0] : t("routing.rules.action.mixed"),
        actions,
        kind: "ruleset",
        match: t("routing.rules.manual.scope"),
        count: manualRules.length,
      });
    }

    const metadata = Array.isArray(summary?.metadata) ? summary.metadata : [];
    metadata.forEach((item) => {
      const type = String(item.ruleset_type || item.ruleset_id || "").toLowerCase();
      if (!type || type === "manual" || type === "effective") return;
      const count = Number(item.metadata_json?.count || item.metadata_json?.effective_counts?.total || 0);
      rows.push({
        source: type,
        value: count ? t("routing.rules.destination.count", { count: formatLocaleNumber(count) }) : "",
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
        value: t("routing.rules.destination.count", { count: formatLocaleNumber(protectedCount) }),
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

  function ruleStatus(rule) {
    if (Number(rule?.count || 0) > 0 || String(rule?.kind || "") === "default") return presentationState("healthy");
    return presentationState("unknown");
  }

  function ruleSourceClass(source) {
    return `settings-domain-row--source-${String(source || "unknown").toLowerCase().replace(/[^a-z0-9_-]+/g, "-")}`;
  }

  function diagnosticReasonKey(reason) {
    const value = String(reason || "").trim().toLowerCase();
    if (!value) return "";
    if (value.includes("legacy database references")) return "diagnostics.reason.legacy_database_references";
    if (value.includes("client or source observation is stale") || value.includes("subject observation is stale")) {
      return "diagnostics.reason.stale_subject_observation";
    }
    if (value.includes("external integration observation missing") || value.includes("external integration has no recent observation")) {
      return "diagnostics.reason.external_integration_observation_missing";
    }
    if (value.includes("routing dataplane does not fully match intent")) return "diagnostics.reason.routing_drift";
    if (value.includes("vpn runtime path does not fully match intent")) return "diagnostics.reason.vpn_drift";
    if (value.includes("watchdog last observation is stale")) return "diagnostics.reason.watchdog_stale";
    return "";
  }

  function diagnosticReasonText(reason, fallback) {
    const key = diagnosticReasonKey(reason);
    if (key) return t(key);
    return fallback || t("diagnostics.reason.none");
  }

  function diagnosticMeaningText(reason, state) {
    const key = diagnosticReasonKey(reason);
    if (key) {
      const meaningKey = `${key}.meaning`;
      const meaning = t(meaningKey);
      if (meaning !== meaningKey) return meaning;
    }
    const uxState = presentationState(state || "unknown");
    return uxState.summary || t("diagnostics.meaning.unknown");
  }

  function renderRoutingPolicyHtml(payload) {
    const subjects = Array.isArray(payload?.subjects?.items) ? payload.subjects.items : [];
    const routing = payload?.routing?.routing || payload?.routing || {};
    const reconcile = Array.isArray(payload?.reconcile?.entities) ? payload.reconcile.entities : [];
    const policyReconcile = reconcile.filter((item) => {
      const type = String(item?.entity_type || "").toLowerCase();
      return type === "routing" || type === "rules" || type === "subject";
    });
    const driftCount = policyReconcile.filter((item) => ["drift", "failed"].includes(String(item.reconcile_state || "").toLowerCase())).length;
    const routingState = presentationState(driftCount ? "drift" : (routing.projection?.state || routing.reconcile?.state || "ok"));
    const summary = rulesSummaryFromPayload(payload);
    const ruleRows = ruleRowsFromSummary(summary);
    const rows = subjects.slice(0, 80).map((subject) => {
      const entity = subject.entity || {};
      const label = subject.identity?.display_name || entity.label || entity.id || t("subject.kind.client");
      const category = domainCategoryLabel(subjectDomainCategory(entity.role || subject.intent?.details?.implementation_kind));
      const destination = routingDestinationFor(subject);
      const decision = settingsModeLabel(subject.effective?.mode || subject.intent?.mode || "");
      const reason = routingReasonFor(subject, routing);
      const state = presentationState(subject.projection?.state || subject.reconcile?.state || "unknown");
      return `
        <div class="settings-domain-row settings-domain-row--subject">
          <div class="settings-domain-cell" title="${escapeHtml(label)}">
            <div class="settings-domain-row__title">${escapeHtml(label)}</div>
            <div class="muted">${escapeHtml(category)}</div>
          </div>
          <div class="settings-domain-cell" title="${escapeHtml(destination)}">
            <div class="settings-domain-row__title">${escapeHtml(destination)}</div>
            <div class="muted">${escapeHtml(t("routing.rules.destination_label"))}</div>
          </div>
          <div class="settings-domain-cell">
            <span class="pill">${escapeHtml(decision)}</span>
          </div>
          <div class="settings-domain-cell" title="${escapeHtml(reason)}">
            <div class="muted settings-domain-row__reason">${escapeHtml(reason)}</div>
          </div>
          <div class="settings-domain-cell">
            <span class="settings-event__level settings-event__level--${escapeHtml(presentationLevelClass(state))}">${escapeHtml(state.label)}</span>
          </div>
        </div>
      `;
    }).join("");
    const headerHtml = `
      <div class="settings-domain-row settings-domain-row--header">
        <div>${escapeHtml(t("routing.rules.column.source_scope"))}</div>
        <div>${escapeHtml(t("routing.rules.column.destination"))}</div>
        <div>${escapeHtml(t("routing.rules.column.decision"))}</div>
        <div>${escapeHtml(t("routing.rules.column.reason"))}</div>
        <div>${escapeHtml(t("routing.rules.column.status"))}</div>
      </div>
    `;
    const ruleRowsHtml = ruleRows.map((rule) => {
      const destination = ruleDestination(rule);
      const reason = ruleReason(rule);
      const state = ruleStatus(rule);
      return `
      <div class="settings-domain-row settings-domain-row--rule ${escapeHtml(ruleSourceClass(rule.source))}">
        <div class="settings-domain-cell" title="${escapeHtml(sourceLabel(rule.source))}">
          <div class="settings-domain-row__title">${escapeHtml(sourceLabel(rule.source))}</div>
          <div class="muted">${escapeHtml(t("routing.rules.scope"))}</div>
        </div>
        <div class="settings-domain-cell" title="${escapeHtml(destination)}">
          <div class="settings-domain-row__title">${escapeHtml(destination)}</div>
          <div class="muted">${escapeHtml(String(rule.kind || ""))}</div>
        </div>
        <div class="settings-domain-cell">
          <span class="pill">${escapeHtml(settingsModeLabel(rule.action || ""))}</span>
        </div>
        <div class="settings-domain-cell" title="${escapeHtml(reason)}">
          <div class="muted settings-domain-row__reason">${escapeHtml(reason)}</div>
        </div>
        <div class="settings-domain-cell">
          <span class="settings-event__level settings-event__level--${escapeHtml(presentationLevelClass(state))}">${escapeHtml(state.label)}</span>
        </div>
      </div>
    `;
    }).join("");
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
            <div class="muted">${escapeHtml(t("routing.rules.meta", { count: formatLocaleNumber(totalRules) }))}</div>
          </div>
          <span class="pill settings-event__level--${escapeHtml(presentationLevelClass(routingState))}">
            ${escapeHtml(routingState.label)}
          </span>
        </div>
        <div class="settings-domain-list">
          <div class="label">${escapeHtml(t("routing.rules.title"))}</div>
          ${ruleRowsHtml ? `${headerHtml}${ruleRowsHtml}` : `<div class="settings-events__empty muted">${escapeHtml(t("routing.rules.empty"))}</div>`}
        </div>
        <details class="admin-advanced settings-advanced-collapse settings-policy-decisions">
          <summary class="admin-advanced__summary settings-advanced-collapse__summary">${escapeHtml(t("routing.policy.subjects_title"))}</summary>
          <div class="settings-domain-list">
            ${rows || `<div class="settings-events__empty muted">${escapeHtml(t("routing.policy.empty"))}</div>`}
          </div>
        </details>
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
    const reportState = presentationState(report?.status || "unknown");
    const activeWarningCount = ["database", "routing", "vpn", "subjects", "connections", "watchdog"].filter((name) => {
      const section = sections[name] || {};
      const state = String(section.status || "").toLowerCase();
      if (section.overall_impact === false && state === "warning") return false;
      return ["warning", "degraded", "failed"].includes(state);
    }).length;
    const sectionRows = ["database", "routing", "vpn", "subjects", "connections", "watchdog"].map((name) => {
      const section = sections[name] || {};
      const uxState = presentationState(section.status || "unknown");
      const label = name === "connections" ? t("diagnostics.section.external_integrations") : sectionLabel(name);
      const reason = section.reason || section.reconcile?.reason || "";
      const affected = section.affected_entity_count ?? section.drift_count ?? section.failed ?? 0;
      const observed = section.last_observation || section.observation?.observed_at || "";
      const freshness = freshnessFor(observed, {
        stale: section.observation?.stale,
        stale_after: section.observation?.stale_after,
      });
      const reasonText = diagnosticReasonText(reason, uxState.summary);
      const meaningText = diagnosticMeaningText(reason, section.status);
      const action = uxState.action || (uxState.state === "healthy" ? "" : t("ux.action.check_diagnostics"));
      return `
        <details class="settings-diagnostics-section-card">
          <summary class="settings-diagnostics-section-card__summary">
            <span class="settings-diagnostics-section-card__title">${escapeHtml(label)}</span>
            <strong class="settings-event__level settings-event__level--${escapeHtml(presentationLevelClass(uxState))}">${escapeHtml(uxState.label)}</strong>
            <span class="settings-diagnostics-section-card__affected">${escapeHtml(String(affected || 0))}</span>
            <span class="settings-freshness settings-freshness--${escapeHtml(freshness.state)}" title="${escapeHtml(freshness.title)}">${escapeHtml(freshness.text || "-")}</span>
            <span class="settings-diagnostics-section-card__chevron" aria-hidden="true">▾</span>
          </summary>
          <div class="settings-diagnostics-section-card__expanded">
            <div class="settings-diagnostics-section-card__field">
              <span class="muted">${escapeHtml(t("diagnostics.field.reason"))}</span>
              <strong>${escapeHtml(reasonText)}</strong>
            </div>
            <div class="settings-diagnostics-section-card__field">
              <span class="muted">${escapeHtml(t("diagnostics.field.affected_entities"))}</span>
              <strong>${escapeHtml(String(affected || 0))}</strong>
            </div>
            <div class="settings-diagnostics-section-card__field">
              <span class="muted">${escapeHtml(t("diagnostics.field.last_observation"))}</span>
              <strong class="settings-freshness settings-freshness--${escapeHtml(freshness.state)}">${escapeHtml(freshness.text || "-")}</strong>
            </div>
            <div class="settings-diagnostics-section-card__field">
              <span class="muted">${escapeHtml(t("diagnostics.field.meaning"))}</span>
              <strong>${escapeHtml(meaningText)}</strong>
            </div>
            ${action ? `
              <div class="settings-diagnostics-section-card__field">
                <span class="muted">${escapeHtml(t("journal.field.recommended_action"))}</span>
                <strong>${escapeHtml(action)}</strong>
              </div>
            ` : ""}
            <details class="admin-advanced settings-advanced-collapse">
              <summary class="admin-advanced__summary settings-advanced-collapse__summary">${escapeHtml(t("journal.advanced_details"))}</summary>
              <pre class="settings-event-context__json">${escapeHtml(JSON.stringify(section, null, 2))}</pre>
            </details>
          </div>
        </details>
      `;
    }).join("");

    return `
      <div class="settings-domain-panel">
        <div class="settings-domain-panel__head settings-diagnostics-overall">
          <div>
            <div class="label">${escapeHtml(t("diagnostics.title"))}</div>
            <div class="muted">${escapeHtml(t("diagnostics.generated", { time: freshnessFor(report?.generated_at).text || "" }))}</div>
            <div class="muted">${escapeHtml(t("diagnostics.active_warnings", { count: activeWarningCount }))}</div>
          </div>
          <span class="settings-event__level settings-event__level--${escapeHtml(presentationLevelClass(reportState))}">
            ${escapeHtml(reportState.label)}
          </span>
        </div>
        <div class="settings-event-context__grid settings-diagnostics-section-grid">${sectionRows}</div>
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
