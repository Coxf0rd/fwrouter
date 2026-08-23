# `/opt/fwrouter-api/fwrouter_api/services/ui_state_clients.py`

## Purpose

Owns UI client list DTOs and panel/client count helpers.

## Main Responsibilities

- Build `/api/v2/ui/clients` rows for LAN, external network, and Xray/Vless clients.
- Aggregate Xray subscription profile subjects into `xray-subscription:*` UI rows.
- Apply display settings filtering for hidden, inactive, internal, and disabled systems.
- Build client presence summaries and workspace counts.

## Runtime Impact

Reads subjects, subject details, traffic summaries, subscription metadata, and
effective state caches. It does not write routing intent.

## Guardrails

- Hide service Xray subjects such as `vpn-auto-*` from user-facing client lists.
- Keep synthetic subscription rows UI-only; do not persist them as real subjects.
- Preserve cheap effective-state reads for UI polling.
