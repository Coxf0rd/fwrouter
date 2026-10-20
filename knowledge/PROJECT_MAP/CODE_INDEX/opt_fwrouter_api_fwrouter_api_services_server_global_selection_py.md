# `/opt/fwrouter-api/fwrouter_api/services/server_global_selection.py`

## Purpose

Owns global fixed-server and auto-server selection apply flows.

## Main Responsibilities

- Validate servers that can be selected for global VPN routing.
- Persist and clear global fixed-server intent.
- Apply fixed server targets to Mihomo and update routing state.
- Restore global auto mode and apply the `vpn-auto` selector path.

## Runtime Impact

Can call Mihomo selector APIs, reconcile routing state, and write operational
logs for global fixed/auto changes.

## Guardrails

- Keep persistent `server_id` separate from the Mihomo selector target.
- Restore auto mode with the canonical persisted auto `server_id`; runtime names remain observations.
- Restore previous state only with current owner/CAS and native preimage proof; uncertain or superseded effects must not claim rollback or success.
- Keep Xray VPN-auto virtual server handling explicit.

## Audit behavior (2026-09-27)

Administrative fixed-server intent writes `routing.global_fixed_server_changed` atomically with the SQLite mutation. It records only a hashed server reference; repeated same-selection and already-auto clear operations emit no audit event.


## Fixed generation/fencing source contract (2026-10-19)

Fixed targets remain selectable outside auto/global-list preferences. Their
native membership is prepared by the existing Mihomo generation owner before
probing/selecting them. Fixed/auto native effects share the writer guard with
generic mode operations and revision-fenced TTL/recovery paths. Completion must
reject changed intent, source, runtime incarnation or native proof. Preparation
receipts do not prove final routing, and no missing inventory marker is bypassed.

## Unified fixed workflow (implementation under qualification)

The fixed owner coordinates existing Mihomo generation/selection, Core apply and
Xray staged generation. Its intent and workflow revision share one SQLite
transaction in the existing Xray pending record. A verified intermediate Mihomo
selection can be clean while unified convergence remains pending. Only final
fresh owner proofs and CAS close the workflow and permit job SUCCESS.

Core must already be enabled and managed by explicit operator intent. Its
threaded apply runs outside an outer Xray writer guard and checks expected Core
and selection revisions. Exact current Xray proof avoids generation; unknown
proof or identity inconsistency blocks it. Generic pending recovery delegates to
the fixed owner and must not replay older selection or bypass pending ownership.
This source work is not yet a native/CI qualification or deployment claim.

Pending fixed convergence excludes generic Core mode apply and preference/pool
mutations with `FIXED_WORKFLOW_PENDING`; it never silently rebases another
writer's intent. Generic Xray reconcile preserves the marker and delegates to
the fixed owner. Explicit new fixed intent, auto clear and existing TTL expiry
supersede with their routing/selection transaction. Emergency Direct/bypass
remains a separate existing owner.
