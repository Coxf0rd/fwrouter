# Provider-managed explicit intent and canonical Admin correction

Date: 2026-10-01. Scope: source implementation and isolated tests only.

## Result

Every saved ordinary subscription exposes explicit management intent. Provider configuration is per-source; private keys are write-only and resource discovery is explicit. One config is auto-selected; multiple configs require selection. Settings contains configuration only. The existing canonical Admin renderer projects Provider vpn groups, locations and members with standard current/effective, Auto/priority, latency and Health semantics.

Initialization uses the current config material and eligible current member from the selected subscription, the existing selector, targeted validation/apply and exact logical/member readback. It performs no implicit provider PATCH. Intent-only, partial, failed and unconfirmed states are distinct from verified runtime success; last-good remains protected. Schema 23 migrates existing bindings without creating intent from ordinary URLs; environment credentials are migration bootstrap only.

## Verification

- Expanded backend regression run: 390 passed, covering provider credentials/config/discovery/projection/enable/recovery, migrations, ordinary subscription lifecycle/targeted refresh/delete/outcomes, logical topology, selector and watchdog.
- Final backend rerun after cache invalidation: 53 passed.
- Seven targeted Node suites passed: Settings provider controls, Admin provider members, Admin server presentation, Settings source delete, action integration, lazy reads and Health UI.
- Source-preview browser fixtures: RU/EN at 1440/390 pixels, Settings source switching and canonical Admin location/member rendering; no horizontal overflow or JavaScript errors. No browser write requests. This is fixture evidence, not production acceptance.
- Final diff whitespace and installer clean-surface checks passed.

No deploy, restart, live schema migration, provider switch/protocol change, provider PATCH, outage or destructive subscription mutation was performed. Previously identified Xray lifecycle and UX-presentation baseline failures are outside this correction and are not claimed fixed. Deployment/migration and live acceptance remain separate pending work. The canonical roadmap/specification and full Protocol Adapter stage are unchanged.
