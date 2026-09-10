# `/opt/fwrouter-api/fwrouter_api/services/ui_state_common.py`

## Purpose

Shared helpers for UI read-model modules.

## Main Responsibilities

- Normalize inventory roles and external network display system ids.
- Build activity reason keys and localized labels through `ui_text.py`.
- Compute the common client lifecycle projection: `enabled` is user intent,
  `runtime_present` is runtime/config presence, and `online` requires confirmed
  client activity within the 24 hour activity window.
- Load short TTL traffic maps and subscription client maps.
- Cache effective subject state for UI read paths without live dataplane probes.
- Provide Xray subscription grouping and internal-client filtering helpers.

## Runtime Impact

Reads SQLite traffic, subscription, subject, and job state through short TTL
caches. It should not write persistent state.

## Guardrails

- Keep traffic/activity labels in `ui_text.py`; do not add local display strings.
- Do not treat Xray runtime inventory presence or `enabled` as client activity.
  Use confirmed `last_traffic_at` or subscription profile `last_seen_at`.
- Keep `/ui/clients` read paths free of cold live Mihomo/dataplane probes.
- Preserve facade hook compatibility for `_load_traffic_maps` and effective subject loading.
