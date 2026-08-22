# `/opt/fwrouter-api/fwrouter_api_services_apply_orchestrator_handlers.py`

## Purpose

Generated code-index entry for `/opt/fwrouter-api/fwrouter_api_services_apply_orchestrator_handlers.py`.

## Review Notes

Read the source file directly before changing related behavior. Check adjacent service, route, adapter, script, or systemd documentation as applicable.

## Runtime Impact

This file holds apply mutation handlers. For VPN/selective mutations, Mihomo reconcile is skipped when a ready `external_vpn_module` owns VPN egress; the handler then proceeds to the nft/dataplane apply path using the external contour from preflight. Managed Mihomo remains the default path when no ready external VPN module exists.

## Guardrails

- Keep FWRouter core as the authority for classification and policy routing.
- Keep Mihomo as a VPN egress adapter, not the network policy engine.
- Preserve direct-safe behavior for host/control-plane traffic unless an explicit scoped contour says otherwise.
- `set_selective_default` must keep `routing_global_state`, `rules_state`, and active `effective-rules.*` artifacts synchronized. The apply pipeline receives a `rules_effective` artifact with the newly requested fallback so preflight/profile generation cannot keep an old default.
- When `set_selective_default` needs a live dataplane apply, it should call cheap `mihomo_runtime_satisfies_routing(...)` first, then try `reconcile_mihomo_selective_default_fast(...)` for fallback-only Mihomo changes, and run full `reconcile_mihomo_runtime()` only when the narrow patch is not structurally safe.
- Non-Xray subject server override mutations should switch the stable `fwrouter-subject-*` Mihomo selector first. Full Mihomo reconcile is only a fallback when the selector is missing and needs initial materialization.
- Non-Xray subject server override set/clear returns after the selector switch/status sync. It must not run the full dataplane apply pipeline because the generated nft/Mihomo contours are unchanged.
- Non-Xray subject server override clear must still delete the DB override when the runtime selector is missing. A missing `fwrouter-subject-*` selector can mean the subject is no longer in a materialized VPN path.
- User-scope subject server override set is blocked when the admin committed subject mode is `direct` or `disabled`; admin-scope writes may still persist pending overrides for operator workflows.
- `clear_subject_user_mode` clears `subject_user_overrides` through the apply mutation path and, for global-following subjects, passes a `fast_subject_apply` hint so returning to global inheritance remains a subject-scoped operation.
- The non-Xray subject server override hot path must stay single-subject. Do not call `_load_subjects_with_overrides()` just to set or clear one subject selector; that builds live effective state for every client and makes UI-triggered server selection slow.
