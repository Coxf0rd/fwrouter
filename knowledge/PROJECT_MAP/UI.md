# UI

## Role

The UI is a static frontend served by the backend. It exposes operator controls for servers, routing modes, rules, subject inventory, Xray subscriptions, runtime status, jobs, and diagnostics.

## Source And Deploy Paths

- source: `/srv/fwrouter/ui`
- live target: `/opt/fwrouter-ui`
- serving route: backend UI route under `fwrouter_api/routes/ui.py`

## Contracts

- UI calls backend API routes and should not infer routing state from partial client-side data.
- Server ping values in user/admin views come from canonical backend `/servers` data backed by `server_ping_state`; live UI measurements only update backend state and notify other views to refresh it.
- Runtime status must distinguish desired state, live dataplane state, module state, scoped egress status, and watchdog state.
- Subject displays use domain categories (`local_client`, `external_client`, `external_network_source`, `service`, `infrastructure`) as user-facing concepts. Technical implementations such as Xray/VLESS, Tailscale, Docker, Host, and Mihomo stay in details/advanced context or adapter code.
- Settings journal uses typed `/api/v2/events/recent`: the main journal shows Audit and Operational events, while Diagnostic events are isolated in the advanced diagnostic-events tab. Event grouping must use typed fields such as `event_class`, `severity`, `entity_type`, `entity_id`, `subject_id`, and `connection_id`, not runtime/component substring matching.
- UI state presentation is normalized through shared UX states: Healthy, Warning, Degraded, Failed, Inactive, Disabled. Raw `apply_state`, `runtime_state`, `reconcile_state`, implementation names, and backend `error_code` values are not primary user text; they belong in details/advanced/debug context.
- Event wording answers what happened, to which domain entity, why, and with what result. Diagnostic/probe events are not promoted to user-visible warning/error unless there is typed user impact, and repeated presentation events are grouped without changing stored/raw events.
- Settings Rules shows a domain policy view first (`Subject -> Destination -> Decision/Reason`) from `/state/rules`, `/state/subjects`, `/state/routing`, and `/reconcile`; the raw rules DSL stays in the advanced editor because it is still the write path for manual rule changes.
- Settings Diagnostics reads `/api/v2/diagnose` and presents domain health sections. Implementation names are metadata/details, not section titles.
- Settings inventory has a first-level Connections tab (`html.settings.connections`) backed by the generic systems list from backend state. `managed` means FWRouter owns lifecycle, `external` means user-managed service, `inventory` means view-only discovered host/container objects, and UI visibility means show/hide in admin only.
- Custom external connections in settings are created through the Add connection dialog (`connections.add`); the backend generates immutable `connection_id`, and the browser uses the returned ID for later update/delete/contract/collect actions. Records store purpose (`external_management`, `external_vpn_module`, `external_network_source`), location, address, optional runtime type, `replacement_target`, endpoints and capabilities. The UI shows identity (`connection_id`, legacy/display `system_id`, `requested_by`, `collector`), readiness, and one copyable JSON mounting/API contract for the selected purpose.
- Custom external connections are registration/display records. They do not create routing targets, health probes, systemd units, Docker containers, restart controls, or a working dataplane adapter until corresponding backend adapter support is implemented.
- `fwrouter:global` must not appear as a normal user-facing scoped VPN candidate.
- User-facing UI labels and backend-message translations should go through `static/js/fwrouter-i18n.js`.
- Static HTML text and attributes should use `data-i18n`, `data-i18n-placeholder`, `data-i18n-title`, or `data-i18n-aria-label`.
- Active runtime status pills such as measuring/loading/saving/applying/deleting should store semantic i18n keys through `FwrouterUI.setDynamicStatus()` and rerender on `fwrouter:locale`; store plain text only for terminal results, warnings, errors, or raw backend diagnostics.
- Source identifiers and comments are English; comments are short and only explain non-obvious behavior.

## Risks

- Showing stale `not_configured` while live dataplane is enforced creates operator confusion.
- Treating Xray runtime implementation subjects as visible user clients creates duplicate/noisy UI rows.
- Directly translating runtime strings without updating tests can break UI assertions; user-facing localization should be handled deliberately.
- Leaving new visible text inline in controllers makes future locale switching and consistency checks harder.
