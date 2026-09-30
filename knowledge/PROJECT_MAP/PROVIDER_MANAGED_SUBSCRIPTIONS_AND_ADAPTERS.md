# Provider-managed subscriptions and adapter architecture

Status: approved target architecture / planned implementation, reconciled 2026-10-01. This document extends the existing subscription lifecycle and watchdog contracts; it does not claim implemented provider execution or module extraction.

## Ownership and integration boundary

At most one saved subscription may enable provider-managed mode. It remains one top-level logical subscription/server group. Its internal provider members are distinct domain/storage entities, never ordinary logical servers: logical_server_id != member_id; member identity != presentation label. Discovery is observation, not user intent. Only the user disables the mode.

Provider Adapter → provider member/config data → Protocol Adapter → canonical normalized contract → common validation/persistence pipeline → inventory/runtime/Health/API/UI.

A Provider Adapter owns interaction with a service; a Protocol Adapter owns parsing and normalization of a protocol/config. Core and the common pipeline do not know concrete provider names or contain protocol-specific parsing. A provider may expose several protocols, each dispatched to the appropriate adapter. Xray/Mihomo runtime code must not contain provider-specific branches. Extend the existing pipeline, selector, Health, events and writer guard rather than introduce replacements.

## Provider Adapter contract

StealthSurf is the first concrete implementation. Pluggable adapters expose capabilities for current provider/member state, available-member discovery, provider-side member switching, provider config/settings updates and integration with targeted subscription refresh. Map provider IDs, names and protocol identifiers inside the adapter. Return typed normalized results; do not write directly to canonical DB/domain tables.

Normalized provider results include stable member ID, source_ref, safe display/location, provider availability (including unknown/down/busy), capabilities, discovery timestamp and bounded non-secret metadata. Persist local health/latency, user eligibility/priority and last-selected evidence through the common persistence owner, separately from provider availability. Maintain one uniform provider result contract and one uniform protocol/server result contract; these are distinct typed contracts, not interchangeable entities.

## Credentials and existing configuration

Use FWRouter's existing operator-maintained general backend configuration file, `/opt/fwrouter-api/.env`, and its source template `backend/.env.example`; do not create a StealthSurf-specific secret store. During implementation extend that existing template with an empty documented provider API-key field (for example `FWROUTER_STEALTHSURF_API_KEY=`), and load it only in the backend adapter. Preserve existing root-only 0600 access and installer treatment of operator values. This is a planned, explicitly approved extension of the older documentation that describes this file as ordinary deployment configuration, not an assertion that provider credentials are currently implemented.

Never commit a real key, return it through UI/public DTO/diagnostics, persist it in jobs input, or log it in Journal, application logs, exceptions or provider response evidence. Redact authorization and sensitive config fields before any diagnostic projection. This documentation-only checkpoint does not change the template, live file or secret storage.

## Candidate pool and common selection

Project provider members into the existing subscription group/server tables: current member and available alternatives use the same Auto, priority, latency, status and selection controls. No second mini-interface or selector. The backend maintains separate identities; UI shares normalized candidate presentation.

The existing selector uses eligibility, priority, local canonical Health/latency and existing tie-break/fallback rules. Only genuinely available provider members enter the selectable pool; DOWN/unavailable/busy members do not receive speculative switch attempts. Unknown provider availability is not proof of availability. Preserve last-known inventory when discovery fails, with freshness/unknown evidence; stale evidence does not independently prove a confirmed failure or justify a provider switch.

Ordinary candidate → existing runtime apply. Provider member candidate → Provider Adapter switch using the subscription's current protocol → targeted refresh → validate/reconcile → exact logical/effective readback → verified publication. Report actual change, verified no-op, partial or failed/unconfirmed through existing outcomes; do not change real-switch provenance on a no-op.

## API call budget and refresh boundary

No continuous provider API polling. Calls are allowed only when enabling provider-managed mode, manually updating servers, confirmed watchdog recovery, and after provider switch/config update when new config/state is required. Ping is a local runtime check and never calls the provider API. API failure preserves last-known member inventory and last-good runtime/public generation.

The implemented canonical operation is refresh_subscription(source_ref); use that stable identity rather than introduce another subscription-ID scheme. It fetches only the selected source and preserves peers. refresh_all_subscriptions() remains the existing full orchestration operation. Both use fetch → parse/validate → normalized inventory → ownership/memberships → vpn-auto consistency → validated runtime generation → apply/readback → verified public state → Health/API/UI/events. The provider adapter supplies data at this boundary; it does not own a second reconcile or persistence path.

## Subscription-level protocol

One protocol is chosen for the entire provider-managed subscription; all internal members use it. Normal member switching does not change protocol. If provider requests require a protocol parameter, the adapter supplies the existing subscription choice without creating a new user decision.

Settings, adjacent to the subscription/provider-managed controls, exposes only the intersection of provider availability, FWRouter Protocol Adapter support and current runtime capability. Unsupported protocols cannot be selected. Provider-specific names/IDs stay behind adapter mapping.

Manual change: saved user protocol intent → Provider Adapter applies provider-side protocol/config change → targeted refresh of this source → selected Protocol Adapter → validation → normalized contract → common persistence → reconcile → verified runtime apply/readback. Persistent intent, provider observation and verified applied state remain distinct; failure must not claim an applied protocol or destroy last-good. Recovery of a provider-side partial mutation uses existing typed outcomes and targeted reconcile, without silently rewriting user intent.

## Protocol Adapter implementation scope

Do not build one universal parser. Each protocol adapter owns parse, protocol-specific validation, transport/security handling and normalization, returning the same versioned canonical server/protocol contract. Declare import/export, Mihomo/Xray projection and health/latency capabilities explicitly; do not promise unsupported native Xray egress or lossless exports. After this boundary only the common pipeline writes canonical domain/DB/inventory state.

The next provider/protocol implementation covers all current StealthSurf protocol types actually available through its API/subscription formats and supportable by the pinned FWRouter runtime, including Hysteria2 and other eligible types. It is not restricted to VLESS/REALITY. Before implementation verify exact current identifiers against official provider API/documentation and pinned runtime capabilities, then record the supported intersection and rejection reasons. No provider protocol list or runtime support is asserted by this documentation-only update. Migrate existing protocol integrations to the same contract without spreading new hardcode through generic storage/reconcile/UI; Hysteria/Hysteria2/TUIC and other protocols remain capability-gated rather than assumed supported.

## Watchdog recovery

Keep the existing failure-confirmation policy and watchdog authority. Provider status is evidence, not the sole truth of Health. Do not add an aggressive internal retry loop or change ordinary watchdog cadence.

1. Confirmation #1: targeted refresh of the current provider-managed subscription; wait for the normal watchdog verification cycle.
2. Confirmation #2: obtain provider status evidence when supported. If current member is DOWN, the existing selector chooses another eligible available member; Provider Adapter switch → targeted refresh → validate/reconcile. If the provider reports UP, one more targeted refresh is allowed instead of a pointless switch. Unknown/unavailable status must not be presented as confirmed DOWN. Wait for the next normal cycle.
3. Confirmation #3, connectivity still absent: existing Emergency Direct. Journal, Health and UI show the reason and recommend checking the subscription/provider or explicitly disabling provider-managed mode.

Recovered connectivity closes the incident and resets the attempt counter. Emergency Direct never automatically disables provider-managed mode. No eligible alternative or unavailable status produces an honest recovery outcome; it does not bypass failure confirmation.

## Invariants and acceptance

Validation before publication; unsupported/invalid candidates never become usable; runtime/bindings/public snapshots share one verified generation; last-good is preserved on validation/apply/readback failure. Refresh/delete/provider changes share the existing writer guard and revision protection. Preserve actual user intent, ordinary sources and custom/local ownership. Safe labels are presentation, never identity.

Acceptance requires targeted tests for provider call budget, available/busy/down filtering, subscription protocol stability/change/capability intersection, credential redaction, single persistence ownership, targeted isolation, selector and watchdog confirmed cycles, typed no-op/failure/partial outcomes, last-good and logical/effective readback. Failure tests belong in isolated test/staging, not destructive production smoke. Minimum functional controls may be added now; visual redesign remains the late Final UI stage.

## Dependencies

The ed102a3 lifecycle/refresh foundation is implemented. Provider execution and full protocol migration remain planned and precede Performance audit. Their normalized boundaries feed Stage 8 module contracts; Stage 7 extraction is a later independent task. See the canonical active roadmap and current Protocol Integration Contract for implemented-versus-planned distinctions.
