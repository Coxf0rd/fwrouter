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
- Subject displays should separate VPN-capable subjects, tracked-only subjects, and direct-safe control-plane subjects.
- Settings inventory has a first-level Connections tab (`html.settings.connections`) backed by the generic systems list from backend state. `managed` means FWRouter owns lifecycle, `external` means user-managed service, `inventory` means view-only discovered host/container objects, and UI visibility means show/hide in admin only.
- Custom external connections in settings are created through the Add connection dialog (`connections.add`) and store purpose (`external_management`, `external_vpn_module`, `external_network_source`), location, address, optional runtime type, `replacement_target`, endpoints and capabilities. The UI shows identity (`external_system_id`, `requested_by`, `collector`), readiness, and one copyable JSON mounting/API contract for the selected purpose.
- Custom external connections are registration/display records. They do not create routing targets, health probes, systemd units, Docker containers, restart controls, or a working dataplane adapter until corresponding backend adapter support is implemented.
- `fwrouter:global` must not appear as a normal user-facing scoped VPN candidate.
- User-facing UI labels and backend-message translations should go through `static/js/fwrouter-i18n.js`.
- Static HTML text and attributes should use `data-i18n`, `data-i18n-placeholder`, `data-i18n-title`, or `data-i18n-aria-label`.
- Source identifiers and comments are English; comments are short and only explain non-obvious behavior.

## Risks

- Showing stale `not_configured` while live dataplane is enforced creates operator confusion.
- Treating Xray runtime implementation subjects as visible user clients creates duplicate/noisy UI rows.
- Directly translating runtime strings without updating tests can break UI assertions; user-facing localization should be handled deliberately.
- Leaving new visible text inline in controllers makes future locale switching and consistency checks harder.
