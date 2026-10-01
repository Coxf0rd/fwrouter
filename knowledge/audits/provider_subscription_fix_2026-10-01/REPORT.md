# Selected provider source and StealthSurf subscription import fix — 2026-10-01

Implementation commit: `c461089`. Standard backend/UI/docs deploy completed, deployed loader preflight passed, API restarted, all four FWRouter units active, Health healthy. Five changed backend/UI deployment files match source. No startup traceback, config validation or migration failure markers. Operator credentials preserved; no provider API mutation or binding enable.

## Causes and correction

Provider placeholders were projected for all saved subscriptions rather than recognized sources. Composition now recognizes HTTPS StealthSurf connect links locally, rejects new unrelated bindings before I/O/storage, and leaves existing explicit bindings represented. Settings selects the exact source, shows its safe label, clears stale action identity/messages, locks the selector during a provider job, and disables provider refresh until enabled.

The same saved connect URL returned a usable Hysteria2 Clash YAML for the legacy client and recognized but unparseable Xray JSON for Happ. Format rank previously selected that JSON and returned `SUBSCRIPTION_SERVERS_EMPTY`. Download selection now prefers actual parse success before existing format/count rank and reuses the parse result. All-failed diagnostics retain their earlier ranking. No new JSON protocol support was added.

## Tests and live verification

314 targeted backend tests passed across provider storage/client/operations/recovery, ordinary subscription/targeted refresh, selector and watchdog. The final parser lifecycle rerun passed 93 tests. Five targeted Node suites passed, including executing the real renderer with distinct provider bindings and an ordinary source in RU/EN. Diff and installer clean-surface checks passed.

Read-only saved connect URL fetch/parse passed with one Hysteria2 server. Live browser switched all three sources in RU/EN at 1440/390 px: provider panel hidden for both ordinary subscriptions and shown for the exact StealthSurf source; no page errors or horizontal overflow; no mutating browser requests. Provider API request metrics remained empty.

The existing saved failed source was then refreshed through its standard targeted job, `c8f63f49-5d58-47f6-9e71-6811db7ce5a6`: success, promoted/applied/runtime_verified true, native config validation passed. The source now has success status, one server and no saved error. Other sources remain successful with 19 and 8 servers. Generated Mihomo contains 78 proxies including one Hysteria2 proxy; generated and active mounted hashes match. Common refresh restarted managed containers through its existing apply path. Provider intent remains disabled, with no provider API requests. This is ordinary subscription import/application evidence, not provider-managed binding acceptance or switch/recovery acceptance.

## Boundaries

Full Protocol Adapter coverage and explicit provider-managed binding/apply acceptance remain open. Existing Xray lifecycle and UI ux-presentation baseline failures documented in the preceding foundation deploy report were not changed by this correction. One installer attempt encountered a disappearing SQLite WAL shared-memory sidecar during chmod; repeating the same standard command succeeded. Installer race hardening is an independent follow-up.
