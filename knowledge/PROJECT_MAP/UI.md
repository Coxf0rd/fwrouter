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
  active member and canonical member health evidence. Latency cells contain only
  `N ms`, localized `No data`, or a spinner while that scope is being checked.
  User rows retain exactly `Server` and `Latency`; a compact localized health dot
  sits beside each server name. Admin group rows retain their existing health dot
  and usable/total count beside the name. Expanded Admin members keep a separate
  health cell; failed member evidence is presented as localized Timeout or Error
  based on existing error fields without exposing raw diagnostics. A forced refresh replaces latency with a
  spinner only in the checked user scope (`user_vpn_auto` or `user_global`);
  Admin marks all groups and expanded members as checking. Completion invalidates
  the cached server response and reloads canonical latency, health, active-member,
  and freshness fields. Its localized
  group/member/error summary occupies a reserved row below the controls. There
  is no manual result lane or separate manual result column.
- User health dots use available, unavailable, and neutral unknown/stale colors
  with localized accessible labels. User group rows do not expose runtime IDs,
  member IDs, error codes, or raw probe messages.
- Logical health and runtime latency are separate presentation axes. `usable`,
  `unknown`, and `unavailable` describe member evidence; timeout/error labels
  belong to the member health cell and never to latency. Server rows use
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
- Settings journal reads compact typed `/api/v2/events/recent?view=summary` and makes a parallel bounded `type=audit` request in the same cache; results merge/dedupe by event ID so recent routine events cannot starve the Audit tab. Diagnostic events remain isolated in the advanced diagnostic-events tab. Each source event stays a separate row. During source/live version skew, legacy `/logs/operational` and `/logs/technical` remain compatibility fallbacks.
- Absolute journal timestamps use numeric `DD.MM.YY HH:mm` in the configured application time zone for both locales. External-client activity in Settings and Admin uses its own absolute localized `DD.MM.YY HH:mm:ss` second line with a semantic `<time datetime>` value; missing or invalid timestamps render localized no-observation text. Primary rows may add only an allowlisted safe entity label and safe localized transitions already present in stored fields; unknown legacy messages stay in advanced details under a generic localized title. Raw IDs, URLs, tokens, and alias values are not fabricated or promoted.
- UI state presentation is normalized through shared UX states: Healthy, Warning, Degraded, Failed, Inactive, Disabled. Raw `apply_state`, `runtime_state`, `reconcile_state`, implementation names, and backend `error_code` values are not primary user text; they belong in details/advanced/debug context.
- Admin Global fixed → auto reset matches the current row by canonical server ID, uses the existing DELETE action, and confirms authoritative `server_mode=AUTO` after a forced refresh. Journal summaries show safe object labels and localized mode/preference transitions; VPN member labels never expose runtime member IDs.
- Event wording answers what happened, to which domain entity, why, and with what result. Diagnostic/probe events are not promoted to user-visible warning/error unless there is typed user impact.
- Journal primary titles append safe known transitions to localized event/action labels. VPN-auto selection distinguishes initial selection, automatic switching, and API/external switching using the existing reason/source fields; logical server names remain the only primary identity. Legacy `result` and `source` values are read from the summary `details` projection when top-level fields are absent. Failed and partial outcomes raise displayed severity to error and warning respectively, keeping the error filter aligned with the operation result. Ordinary details show actor, source, result, reason, and timestamp; the collapsed technical disclosure is limited to useful identifiers and evidence and does not repeat those fields.
- Settings Rules shows a domain policy view first (`Subject -> Destination -> Decision/Reason`) from `/state/rules`, `/state/subjects`, `/state/routing`, and `/reconcile` when available. The current manual rule write/status path still uses `/rules/summary`, which also supplies active rule source counts and manual rule rows until the new read contract contains the full rule inventory. The raw rules DSL stays in the advanced editor.
- Settings Diagnostics reads compact `/api/v2/diagnose?view=summary` and presents current backend health. When diagnose is unavailable, health is unconfirmed `unknown`; old logs never become current health. Section reasons resolve by stable `reason_code` through RU/EN dictionaries. Rules distinguish configured rulesets from successful apply and apply failure.
- Event journal source checkpoint (2026-09-29; not yet deployed): audit entity enrichment is bounded and uses stable event codes plus structured identity, never message parsing. Alias snapshots retain only labels accepted by shared `safe_human_label`; unsafe old/new aliases are omitted. Known events use localized object-specific titles where safe, otherwise a localized action/category title without an invented entity. The compact endpoint omits `event_class`, so the existing reader's resolved category must also drive safe transition formatting. Ordinary disclosure contains safe localized transitions and actor/source/result; arbitrary nested values, IDs, and technical payload remain advanced-only. Older boolean-only alias audit records cannot recover historical names.
- Deployment checkpoint (2026-09-29, commit `804f1ca`): backend/UI were deployed and only `fwrouter-api.service` restarted; a later UI-only deploy followed the resolved-category regression fix, with no service restart. HTTP ingress `127.0.0.1:5500` served the expected cache-busted scripts matching source hashes. RU/EN browser fixtures passed with non-GET requests blocked. Live audit API returned 10 sampled rows with 10 entity types and 6 safe labels; historical alias rows still lack old/new alias snapshots. Xray remained 85/85 applied and its pre/post identity and routing binding projections matched; only per-binding apply timestamps were rewritten on API startup. Exact historical cause of 86→85 remains unproven.
- Bounded journal/subscription follow-up (2026-09-29): navigating the actual `All` journal tab over `127.0.0.1:5500` rendered 28 rows; recent and audit event GETs were HTTP 200, with no generic `Событие`/`Event`, UUID/credential patterns in visible text, failed requests, or blocked writes. The accumulated browser console history was not scoped to this navigation and is not treated as current-page evidence. Read-only Xray client inventory exposed 85 subscription paths (path-set SHA-256 `dcaba840f784ecf9053cbb38f5cacb0af9fd80720c7260e457e413504538e409`). One enabled user profile's public `/s/{token}` Clash endpoint returned HTTP 200, 11 VLESS proxy entries (4,434 bytes; body SHA-256 `144b194fafdba4892da025d67ec7f5bf9374f1cf1d67cc26a15e43bc21a11e25`); names matched its 11-node verified snapshot, and its snapshot client identities matched the current runtime-exportable profile subset (11/11). The single VPN-auto virtual server identity maps to a concrete runtime binding, so its virtual/logical server ID differs from that binding's selected target. This verifies one profile, not all 85 outputs. No refresh or Xray mutation was run.
- Settings read-only tabs keep a lightweight browser cache for Journal, Rules,
  Diagnostics/Health and Inventory. Reopening an already loaded tab renders the
  cached payload immediately; stale cached data remains visible while a
  background refresh runs. Manual refresh bypasses this cache. Locale changes
  rerender cached payloads and must not trigger backend refetch by themselves.
- Settings inventory first renders the lightweight `live_observations=false` response, then automatically hydrates the same inventory API with `live_observations=true` when the backend marks missing cached evidence as `health.reason=HEALTH_EVIDENCE_NOT_LOADED`. Health and live presence/activity are merged by stable `subject_id` into existing rows and the existing cache, preserving drafts, focus, selection, persisted lifecycle intent and deletion references. Obsolete sequence responses are ignored. A failed or incomplete hydrate is shown as unavailable evidence, not as offline or a confirmed unknown; explicit refresh retries it. Known disabled/inactive intent remains visible during the pending phase.
- Xray client mode controls offer only explicit VPN and Disabled choices. Legacy Direct, Selective and mixed profiles retain their current value without silently rewriting intent; Direct is identified as preserved only when backend capability reports `legacy_supported_direct`, while unsupported Selective remains visible but is not offered as a new choice. The legacy `enabled` shorthand is displayed as VPN. An explicit VPN action is an intentional profile normalization; plain reads, alias edits and profile enable lifecycle preserve existing member modes. Transparent `external_network_source` modes remain separate.
- User/Admin current routing reads share the existing `/ui/router-summary` DataStore request and refresh every 15 seconds only while a relevant view is visible; visibility return triggers an immediate read and hidden views pause. The refresh patches current labels/status/provenance without replacing editable tables or form state; Admin also moves the current-server row highlight and badge in place. Current server labels use safe human names only; missing or hash-like names use a localized unavailable label. Auto-selection provenance is shown only for the matching auto target and localized allowlisted reason/origin; caller-supplied actor attribution is explicitly unverified. Subject-fixed and displayed DIRECT routing do not inherit stale auto provenance.
- User UI omits the Proxy GET action while preserving the shared action-status area used by other operations. FWRouter's own traffic is rendered as static DIRECT. External-client Delete stays adjacent to its subscription link while retaining the existing confirmation and canonical deletion reference.
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
