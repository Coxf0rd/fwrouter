# UI

## Role

The UI is a static frontend served by the backend. It exposes operator controls for servers, routing modes, rules, subject inventory, Xray subscriptions, runtime status, jobs, and diagnostics.

## Source And Deploy Paths

- source: `/srv/fwrouter/ui`
- live target: `/opt/fwrouter-ui`
- serving route: backend UI route under `fwrouter_api/routes/ui.py`

## Contracts

- UI calls backend API routes and should not infer routing state from partial client-side data.
- Runtime server latency in user/admin tables comes from canonical `/servers`
  topology data: `topology.effective_latency_ms`, tied to the runtime-effective
  active member and canonical member health evidence. Missing runtime latency is
  rendered from evidence status: healthy/usable plus numeric latency shows `N ms`,
  fresh failure shows localized `Unavailable / Timeout`, and unknown, stale, or
  absent evidence shows `No data`. A forced refresh replaces latency with a
  spinner only in the checked user scope (`user_vpn_auto` or `user_global`);
  Admin marks all groups and expanded members as checking. Completion invalidates
  the cached server response and reloads canonical latency, health, active-member,
  and freshness fields. Its localized
  group/member/error summary occupies a reserved row below the controls. There
  is no manual result lane or separate manual result column.
- Logical health and runtime latency are separate presentation axes. `usable`,
  `unknown`, and `unavailable` describe member evidence; `timeout` belongs to
  the ping column and must not be presented as logical health. Server rows use
  a compact localized health indicator plus the usable/total member count;
  they do not repeat a sentence-form availability summary.
- Logical servers with members expand into a bounded-height mini-table ordered
  by canonical `member_order` with a stable identity tie-breaker. Rows use
  localized presentation labels such as `Node 1`, mark the Mihomo-observed
  effective member with a compact marker, and show member health and latency.
  Raw `member_id` and `sub:...` values are not primary user labels. The compact
  chevron beside the member count is the only expansion control; selecting a
  row does not expand or apply it. Expanded logical-server IDs persist across
  list, health, and sort renders. The mini-table spans the full server row and
  long groups scroll vertically without horizontal page overflow.
- Runtime status must distinguish desired state, live dataplane state, module state, scoped egress status, and watchdog state.
- Subject displays use domain categories (`local_client`, `external_client`, `external_network_source`, `service`, `infrastructure`) as user-facing concepts. Technical implementations such as Xray/VLESS, Tailscale, Docker, Host, and Mihomo stay in details/advanced context or adapter code.
- Settings journal uses typed `/api/v2/events/recent` when the live backend exposes it: the main journal shows Audit and Operational events, while Diagnostic events are isolated in the advanced diagnostic-events tab. During source/live version skew, legacy `/logs/operational` and `/logs/technical` are compatibility fallbacks; legacy technical records are routed to the diagnostic tab and must not become the primary operational journal. Event grouping must use typed fields such as `event_class`, `severity`, `entity_type`, `entity_id`, `subject_id`, and `connection_id`, not runtime/component substring matching.
- UI state presentation is normalized through shared UX states: Healthy, Warning, Degraded, Failed, Inactive, Disabled. Raw `apply_state`, `runtime_state`, `reconcile_state`, implementation names, and backend `error_code` values are not primary user text; they belong in details/advanced/debug context.
- Event wording answers what happened, to which domain entity, why, and with what result. Diagnostic/probe events are not promoted to user-visible warning/error unless there is typed user impact, and repeated presentation events are grouped without changing stored/raw events.
- Settings Rules shows a domain policy view first (`Subject -> Destination -> Decision/Reason`) from `/state/rules`, `/state/subjects`, `/state/routing`, and `/reconcile` when available. The current manual rule write/status path still uses `/rules/summary`, which also supplies active rule source counts and manual rule rows until the new read contract contains the full rule inventory. The raw rules DSL stays in the advanced editor.
- Settings Diagnostics reads `/api/v2/diagnose` when available and presents one domain health summary; older live backends use a read-only compatibility summary from rule/log endpoints. Implementation names are metadata/details, not section titles.
- Settings read-only tabs keep a lightweight browser cache for Journal, Rules,
  Diagnostics/Health and Inventory. Reopening an already loaded tab renders the
  cached payload immediately; stale cached data remains visible while a
  background refresh runs. Manual refresh bypasses this cache. Locale changes
  rerender cached payloads and must not trigger backend refetch by themselves.
- Settings inventory has a first-level Connections tab (`html.settings.connections`) backed by the generic systems list from backend state. `managed` means FWRouter owns lifecycle, `external` means user-managed service, `inventory` means view-only discovered host/container objects, and UI visibility means show/hide in admin only.
- Custom external connections in settings are created through the Add connection dialog (`connections.add`); the backend generates immutable `connection_id`, and the browser uses the returned ID for later update/delete/contract/collect actions. Records store purpose (`external_management`, `external_vpn_module`, `external_network_source`), location, address, optional runtime type, `replacement_target`, endpoints and capabilities. The UI shows identity (`connection_id`, legacy/display `system_id`, `requested_by`, `collector`), readiness, and one copyable JSON mounting/API contract for the selected purpose.
- Custom external connections are registration/display records. They do not create routing targets, health probes, systemd units, Docker containers, restart controls, or a working dataplane adapter until corresponding backend adapter support is implemented.
- `fwrouter:global` must not appear as a normal user-facing scoped VPN candidate.
- User-facing UI labels and backend-message translations should go through `static/js/fwrouter-i18n.js`; English is the dictionary fallback and Russian remains an explicitly selected locale.
- Static HTML text and attributes should use `data-i18n`, `data-i18n-placeholder`, `data-i18n-title`, or `data-i18n-aria-label`.
- Active runtime status pills such as measuring/loading/saving/applying/deleting should store semantic i18n keys through `FwrouterUI.setDynamicStatus()` and rerender on `fwrouter:locale`; store plain text only for terminal results, warnings, errors, or raw backend diagnostics.
- Source identifiers and comments are English; comments are short and only explain non-obvious behavior.

## Risks

- Showing stale `not_configured` while live dataplane is enforced creates operator confusion.
- Treating Xray runtime implementation subjects as visible user clients creates duplicate/noisy UI rows.
- Directly translating runtime strings without updating tests can break UI assertions; user-facing localization should be handled deliberately.
- Leaving new visible text inline in controllers makes future locale switching and consistency checks harder.
