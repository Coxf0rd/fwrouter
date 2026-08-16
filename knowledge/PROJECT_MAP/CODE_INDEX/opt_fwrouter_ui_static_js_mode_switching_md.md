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
- Admin global block: top `Direct/Selective/VPN` segmented control changes whole-router mode through `/routing/global`. The `FWRouter traffic` field is read-only `DIRECT`; `fwrouter:global` represents router control-plane traffic and must not send subject mode mutations.
- Admin Devices loads lightweight settings inventory by `inventory_role` for Lan, external network clients, Vless clients, Docker, and Host; optional tabs use i18n labels and are hidden after the first inventory load when the corresponding role has no rows or is disabled by `system_visibility`.
- Settings inventory calls `/ui/settings/inventory?role=...` with `include_inactive=true`, so inactive Vless subscription groups and other manageable objects remain visible in Settings even when Admin hides inactive rows.
- Settings journal has a top-level `Watchdog` tab. Technical events with `component=watchdog` are filtered out of `System` and shown there together with watchdog operational warnings/errors.
- Settings `Connections` dialog lets the operator choose external role plus data delivery: `api_push`, `http_poll`, `command_probe`, or `file_read`, and refresh timing `on_change`, `manual`, or `interval`. The collector config is one JSON textarea; visible labels live in `fwrouter-i18n.js`.
- Settings `Connections` rows are compact cards; detailed fields, settings JSON, and contract JSON open in a modal detail view on click/Enter/Space. The modal includes actions for toggling display and deleting custom external connections; visibility/delete/copy buttons must not trigger row details.
- Visible UI strings belong in `static/js/fwrouter-i18n.js` with matching `ru` and `en` keys. Settings/watchdog journal details use stable detail keys and translate them in the renderer; ordinary UI JS/HTML should not introduce hardcoded Russian text outside the i18n dictionary.
