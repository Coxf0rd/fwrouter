# `/opt/fwrouter-ui/static_js_mode_switching.md`

## Purpose

Generated code-index entry for `/opt/fwrouter-ui/static_js_mode_switching.md`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file is part of the FWRouter source/runtime surface. Keep this card synchronized when the file responsibility, runtime side effects, boot relevance, or risk profile changes.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- Server country codes are internal metadata for selecting flag assets and parsing provider names. User-facing labels in user/admin server lists must show the flag and cleaned server name, not an extra `de`/`az` style prefix or alphabetic fallback.
- User view must mirror the backend user-override gate: admin committed mode other than `global` disables user `Direct/Selective/VPN` controls; admin `direct`/`disabled` also disables the hero power/connect button.
- User view mode segment has four states. `Direct/Selective/VPN` create a 7-day user mode override through `POST /subjects/{subject_id}/mode` with `actor_scope=user`; `Global` calls `DELETE /subjects/{subject_id}/mode` to clear the override and inherit router-wide mode again. Active `Global` means no manual client mode override and does not control VPN server selection.
- Admin global block: top `Direct/Selective/VPN` segmented control changes whole-router mode through `/routing/global`. The `FWRouter traffic` field is read-only `DIRECT`; `fwrouter:global` represents router control-plane traffic and must not send subject mode mutations.
- Admin Devices loads lightweight settings inventory by `inventory_role` for Lan, external network clients, Vless clients, Docker, and Host; optional tabs use i18n labels and are hidden after the first inventory load when the corresponding role has no rows or is disabled by `system_visibility`.
- The admin external-network tab respects both generic `system_visibility.external_network_source` and backend-provided concrete `display_system_id` on inventory items; hiding a concrete source in `Connections` must hide its admin tab/rows regardless of the implementation.
- Settings inventory calls `/ui/settings/inventory?role=...` with `include_inactive=true`, so inactive Vless subscription groups and other manageable objects remain visible in Settings even when Admin hides inactive rows.
- Settings journal has a top-level `Watchdog` tab. Technical events with `component=watchdog` are filtered out of `System` and shown there together with watchdog operational warnings/errors. Operational `vpn_auto_server_switched` rows are also shown in `Watchdog` only when the backend summary marks them `category=watchdog` from raw watchdog `requested_by`/`reason`; manual/external switches remain server events.
- Mobile startup ignores a stored `fwrouter:view=settings` on viewports up to 760px unless `?view=settings` is explicit. This lets phones recover to user view if a heavy settings screen previously made the browser tab unstable.
- Settings `Connections` dialog lets the operator choose external role plus data delivery: `api_push`, `http_poll`, `command_probe`, or `file_read`, and refresh timing `on_change`, `manual`, or `interval`. The collector config is one JSON textarea; create/delete uses `/ui/external-connections/{system_id}` so the backend validates and normalizes the record instead of the browser rewriting the whole display settings blob. Visible labels live in `fwrouter-i18n.js`.
- Settings `Connections` rows are compact cards; detailed fields, an editable form for custom records and `customizable` discovered external network sources, settings JSON, and contract JSON open in a modal detail view on click/Enter/Space. Edits are saved through `PATCH /ui/external-connections/{system_id}`; for discovered sources this creates a custom override with the same `system_id`. Connection type and replacement target stay read-only. The modal includes actions for toggling display and deleting custom external connections; visibility/delete/copy buttons must not trigger row details.
- Visible UI strings belong in `static/js/fwrouter-i18n.js` with matching `ru` and `en` keys. Settings/watchdog journal details use stable detail keys and translate them in the renderer; ordinary UI JS/HTML should not introduce hardcoded Russian text outside the i18n dictionary.
