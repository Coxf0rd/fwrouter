# FWRouter engineering roadmap mirror

Updated 2026-10-01. This English engineering summary is not a second authoritative active plan. The sole canonical active roadmap is [/решения/roadmap/fwrouter/ROADMAP.md](/решения/roadmap/fwrouter/ROADMAP.md); the old root filename is navigation-only. Full unedited historical checkpoints and superseded wording are preserved in [/решения/roadmap/fwrouter/history/README.md](/решения/roadmap/fwrouter/history/README.md). Local documentation is maintained outside this Git repository.

## Confirmed milestones and evidence boundaries

The source tree was clean at ed102a3 when canonicalization began. Completed milestones include typed audit foundation, External Clients cleanup/identity (not External Connections functional CRUD), original subscription lifecycle, LAN freshness/collector evidence, routing reset, individual Health and lazy diagnostics/event details, explicit VPN/Disabled and durable 180-second manual vpn-auto debounce.

| Milestone | Commit | Source/tests | Deploy/live checkpoint |
|---|---|---|---|
| Verified Xray/Mihomo/public generation and REALITY foundation | 3991039 | Implemented; 233 targeted tests passed | Deployed; identities/bindings/public/runtime verification recorded |
| HA consumer, timestamp and server-row fixes | 304acf4 | Implemented; targeted and browser smoke passed | Deployed; safe HA label and RU/EN wide/narrow verified |
| Journal/Health projections and Admin alignment | 818c8c6 | Implemented; 42 backend tests plus UI checks passed | Deployed; affected labels/counts, lazy details and geometry verified |
| Source-scoped delete, ownership/current selection safety, Settings actions/results and User grid | 5abe524 | Implemented; targeted tests passed | Deployed; read-only UI/runtime verification; no live subscription deleted |
| Canonical targeted/full refresh and HA/UI consistency | ed102a3 | Implemented; 298 backend/HA tests, 29 localization tests, 8 Node suites passed; additional recovery/Xray checks passed | Deployed; 42 file hashes matched, Health healthy, 70 identities/bindings, 7 consistent snapshots, generated/active configs matched, safe HA state/options, RU/EN 1440/390px with no fresh JS errors/overlap, flags HTTP 200 |

ed102a3 covers saved intent versus verified runtime, per-source full/targeted outcomes, writer guard, last-good retention, current auto/fixed safety, Admin canonical full refresh job, safe source labels, one primary User/Admin state with stale secondary evidence, HA unique safe options/action-boundary mapping, truthful watchdog/no-op outcomes, immediate effective latency after one probe, External Client destructive action-row and eu/ae/ar assets.

Numbers are checkpoint observations, not permanent inventory counts. Failure/race/delete/no-alternative/switch cases have targeted coverage; destructive production checks were intentionally not run. A new natural post-deploy scheduled refresh was not observed in the verification window and is not an implementation blocker. Existing API/runtime source contracts and historical checkpoints remain the evidence sources; this documentation work did not run tests, deploy, inspect/change live state or modify code/configuration.

## Canonical execution order

1. Roadmap canonicalization — completed by this documentation checkpoint.
2. Provider-managed subscriptions and Provider Adapter architecture — next/planned, extension of Stage 2.
3. Protocol Adapter normalization and full supported protocol coverage — planned; capability matrix co-designed with provider work, full validation required before that block is accepted.
4. Stage 4 Performance audit, after adapter contract stabilization.
5. Performance fixes for measured bottlenecks.
6. Stage 5 early configuration contract analysis.
7. Stage 6 evidence-gated cleanup / Stage 6A measured internal execution improvements.
8. Stage 8 Core/module contracts.
9. Stage 7 module extraction; original numbering retained, execution 8 → 7.
10. Post-extraction functional/architecture audit.
11. Final hardening/configuration/deployment/runtime stabilization.
12. Isolated Final UI redesign, parity and approved cutover.
13. Production Documentation for final architecture/UI.
14. Stage 9 failure/recovery validation in test/staging, findings fed back to hardening/docs.
15. Stage 10 final release audit and initial supported production baseline acceptance.

## Next architecture block

See [Provider-managed subscriptions and adapters](PROJECT_MAP/PROVIDER_MANAGED_SUBSCRIPTIONS_AND_ADAPTERS.md) and [current Protocol Integration Contract](PROJECT_MAP/PROTOCOL_INTEGRATIONS.md). Both distinguish implemented foundations from planned execution.

At most one provider-managed subscription, one top-level logical group/server and separate internal members; shared server tables/controls and the existing selector. StealthSurf is the first provider adapter; Core is provider-agnostic. Provider Adapter → member/config data → protocol-specific adapter → common normalized contract → one validation/persistence pipeline → inventory/runtime/Health/API/UI. Available candidates only; exclude unavailable/busy members. Allowed provider calls: enable, manual update, confirmed recovery and after member/protocol/config update; never ordinary ping or continuous polling.

One subscription-level protocol applies to all members. Normal member changes retain it. Settings permits only provider/adapter/runtime supported intersection. Manual protocol change applies provider config, targeted refreshes one source, validates and reconciles to exact verified readback. Use existing operator config/template for an empty documented provider key during implementation; no new secret store and no keys in repo/public DTO/UI/jobs/events/diagnostics/logs. Templates/live configuration are unchanged now.

Preserve watchdog confirmation policy: first confirmation targeted refresh; second uses status evidence, DOWN switches via existing selector, UP may refresh once more; third persistent connectivity failure uses Emergency Direct. Wait normal verification cycles, no internal aggressive retries. Recovery closes incident/resets counter; only user disables mode. Provider status is evidence, not a replacement Health truth.

Each protocol has its own parse/validate/normalize adapter, not a universal parser; only the common pipeline writes canonical storage. Before implementation verify exact current StealthSurf API protocol identifiers/formats and pinned runtime capability. Cover all actually available and supportable types, including Hysteria2; Hysteria/TUIC and others are evidence-gated. No current provider list or native Xray egress support is assumed.

## Remaining architecture/release work

- Stage 4: reproducible cold/warm UI/API baseline, meaningful p50/p95, payload/requests/SQL/adapter/serialization cost, RAM/CPU/growth and background overhead; measured fixes only.
- Stage 5: env versus persistent intent versus generated artifacts; schema/API/runtime/installer baseline; clean install/current-supported restore/rollback/future upgrades. Stage 6: dependency-verified dead code/obsolete integrations/aliases/formats cleanup. Stage 6A: measured redundant calculations/DB calls/blocking execution, using Stage 4 baseline.
- Stage 8: ownership/API/events/config/lifecycle/Health boundaries; Stage 7: Xray/Mihomo/Tailscale extraction preserving Core intent/state authority. Then post-extraction parity/architecture audit and final hardening.
- Final UI: copied isolated frontend/preview, protect existing production UI until parity and approved cutover, bounded rollback then cleanup. Until then only functional UX regressions/minimal capability controls; no standalone redesign. Production Documentation follows extraction/hardening/Final UI.
- Stage 9 isolated Internet/runtime/config/apply/provider failures, restart/restore/rollback and recovery observability; Stage 10 release artifacts/docs/contracts and all acceptance evidence. No production release is declared here.

## Independent open follow-ups

- Settings inventory tab/locale loading race: needs confirmation, not a confirmed defect.
- External Connections functional CRUD/lifecycle verification in tests/staging; read-only inventory inspection is not CRUD coverage.
- Remaining administrative typed audit coverage, especially Rules/External Connections; caller-supplied identity stays unverified. No parallel event subsystem.
- LAN activity TTL measurement and possible roughly one-hour policy; do not extend automatically to Xray/external/service clients. Already corrected LAN stale projection is distinct.
- Tailscale peer admission/inventory/per-subject policy requires a decision based on routed evidence or managed intent. An offline peer is an operational condition, not automatically a FWRouter defect.
- Direct manual vpn-auto membership removal of current effective target and later fixed transition: scoped confirmation; refresh/source-delete safety does not prove every manual path.
- Historical Xray 86→85 identity root cause remains unconfirmed; do not restore counts without identity-level baseline.
- Existing SQLite projection commit/postimage-fsync crash window: operator investigation may be required; pending marker prevents false-ready/publication. Review in Stage 9.
- Reconfirm historical profile token/slug in jobs input and subscription_client.token in Settings/inventory; source-ref redaction does not close every profile path. Classify historical logs before redaction; define backup retention/access/restore policy.
- Post-release Xray subscription/client metadata compatibility/security research: document provenance and supported clients before any implementation; do not fabricate usage/quota/expiry.

## Initial production baseline / legacy policy

After canonical architecture acceptance, remove superseded legacy files, obsolete integrations, historical-only formats/migrations and compatibility layers. Preserve history in documentation, not production code. Removal needs proof no supported current consumer/runtime/installer/DB/API dependency and targeted regression coverage; preserve actual user data/intent. Transitional extraction paths are temporary, not permanent compatibility obligations. After first released baseline, breaking changes require explicit migration/deprecation contracts.

## Documentation checkpoint — 2026-10-01

The previous local roadmap was archived byte-for-byte; the active plan and history rule were canonicalized, statuses reconciled and provider/protocol architecture extended. Original stage numbering and all history retained. This Git commit changes documentation only; production code, templates, UI, runtime, DB, tests and deploy are untouched. The authoritative local documents and archive remain outside Git; this file is their English engineering summary.
