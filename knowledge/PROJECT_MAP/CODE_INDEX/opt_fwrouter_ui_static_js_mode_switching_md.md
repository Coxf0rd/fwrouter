# `/opt/fwrouter-ui/index.html` and `/opt/fwrouter-ui/static/js/*.js`

## Purpose

Frontend page controllers and shared UI helpers for FWRouter user, admin, and
settings views. This area owns browser-side rendering, user action wiring,
mutation feedback, job polling calls, and post-mutation read-model refreshes.

## Important Files

- `index.html`
  Loads shared helpers before page controllers. `fwrouter-ui-action.js` must be
  loaded after `fwrouter-common.js` and `fwrouter-i18n.js`, and before
  `admin.js`, `user.js`, and `settings.js`.
- `fwrouter-common.js`
  Shared browser helper layer exposed as `window.FwrouterUI`: API wrappers,
  backend message translation, job polling, applied-state waiting,
  pending/highlight primitives, escaping, byte formatting, flag helpers, and
  normalization of API error payloads from JSON envelopes, FastAPI validation
  details, plain text responses, network failures, and job failures. It also
  exposes `window.FwrouterDataStore`, a short-TTL shared request cache with
  promise dedupe for common read endpoints such as `whoami`, server inventory,
  router summary, Settings workspace/display/inventory, and external IP.
- `fwrouter-ui-action.js`
  Shared explicit-target ActionManager exposed as `window.FwrouterUIAction`.
  Settings, Admin, and User mutation handlers use it for pending/success/error
  lifecycle instead of duplicating manual `try/catch/finally` flows.
- `settings.js`
  Settings controller. It owns settings journal loading, rules editor actions,
  subscription actions, proxy actions, external connection actions, VLESS client
  creation/deletion, subject item edits, display visibility mutations, cache
  invalidation, post-mutation workspace/inventory refresh, and persistence of
  the last selected Settings tab so the Controls pane can restore proxy data
  after reload. Heavy rules projections, reconcile, and diagnostics remain lazy
  and are not loaded by the normal Settings bootstrap.
- `admin.js`
  Admin controller for global mode, selective defaults, VPN-auto server
  selection, server preference autosave, and device/VLESS management. Admin
  fixed-target selection accepts both regular `vpn_server` rows and
  `custom_https_proxy` rows when `global_list` is enabled.
- `user.js`
  User controller for current subject state, self-service mode switching,
  server override toggling, server lists, and current/VPN IP refresh. User
  actions report server-apply and mode errors into explicit `serversState` and
  `routingState` message targets. User bootstrap is driven only by the
  `fwrouter:view` event for the active User view, so Admin/Settings startup does
  not trigger User-only reads.
- Renderer/helper modules
  `fwrouter-labels.js`, `fwrouter-settings-events.js`,
  `fwrouter-settings-inventory.js`, `fwrouter-settings-journal.js`,
  `fwrouter-settings-domain-state.js`, `fwrouter-admin-devices.js`,
  `fwrouter-admin-autolist.js`, `fwrouter-user-servers.js`,
  `fwrouter-ip-check.js`, and `ping-select.js` keep rendering and shared UI
  behavior out of the large page controllers. `fwrouter-admin-devices.js`
  renders Admin device and external-client rows, including short `/s/<alias>`
  public subscription paths in visible external-client metadata while keeping
  full URLs in tooltips. `fwrouter-user-servers.js` and
  `fwrouter-admin-autolist.js` preserve proxy row presentation so
  `custom_https_proxy` rows render with a stable proxy marker, readable
  ellipsis, and title tooltip without disturbing regular country flag rows.
- UI contract tests
  `ui/tests/admin-user-ui-action-integration.test.js`,
  `ui/tests/settings-ui-action-integration.test.js`, and
  `ui/tests/ui-action.test.js` assert the explicit ActionManager lifecycle,
  target scopes, backend endpoints, job/apply waits, and absence of manual
  mutation pending helpers. `ui/tests/common-error.test.js` covers frontend API
  error normalization for validation details, top-level messages, plain text
  failures, network failures, and job failures. `ui/tests/vless-create-error-lifecycle.test.js`
  verifies VLESS/external-client create error feedback, and
  `ui/tests/admin-client-presentation.test.js`,
  `ui/tests/user-server-list-presentation.test.js`, and
  `ui/tests/admin-server-list-presentation.test.js` protect custom proxy server
  list rendering, Admin external-client short-link presentation, and mobile
  layout contracts for the Admin server matrix, late responsive CSS overrides,
  and Settings proxy/tabs areas.
  `ui/tests/data-loading-performance.test.js` covers active-view bootstrap
  ordering, DataStore request dedupe/TTL/invalidation, and normal Settings
  startup staying off heavy state/reconcile/diagnostics endpoints.

## Data Loading Lifecycle

`ui.js` owns initial view selection and dispatches `fwrouter:view` after applying
the active view to the document. Page controllers listen for that event and
bootstrap once for their own view only. They no longer self-bootstrap from
`DOMContentLoaded`, which prevents the User controller from issuing User-only
API calls while opening Admin or Settings.

`window.FwrouterDataStore` dedupes concurrent reads and keeps short-lived cache
entries for shared read models. Mutations must invalidate affected keys before
post-mutation refreshes. Apply/job state and confirmation polling should use
forced reads rather than cached state.

## Settings Action Lifecycle

All user-triggered Settings mutation actions are routed through
`FwrouterUIAction.runAction(...)`:

- subscription: `saveVpnSubscriptionUrl()`, `refreshVpnSubscription()`
- rules: `refreshRules()`, `updateAllRules()`, `saveRules()`
- proxy: `createSettingsProxy()`, `deleteSettingsProxy()`
- VLESS/external clients: `createSettingsExternalClient()`,
  `deleteSettingsExternalClient()`, `deleteSettingsExternalClientGroup()`
- subject/system items: `saveSettingsItem()`, `deleteSettingsSystemSubject()`,
  `toggleSettingsAdminVisibility()`, `saveSettingsDisplayFromSystems()`
- external connections: `submitSettingsExternalSystem()`,
  `saveSettingsConnectionDetails()`, `deleteSettingsExternalSystem()`

Settings action scopes are deliberately narrow: subscription actions target the
`vpnSubscriptionActions` row, rules actions target `settingsRulesActions`, proxy
create targets the proxy form/actions instead of the proxy list, and validation
flashes target the concrete input or message node rather than broad cards.

## Admin Action Lifecycle

Admin mutation actions routed through `FwrouterUIAction.runAction(...)`:

- global mode: `saveAdminGlobalMode()`
- selective default: `saveSelectiveDefault()`
- VPN-auto fixed server: `activateAutolistServer()`,
  `resetAutolistManualServer()`
- device edits: `saveAdminDevice()`
- VLESS/external clients: `saveAdminVlessClientName()`,
  `deleteAdminVlessClient()`

## User Action Lifecycle

User mutation actions routed through `FwrouterUIAction.runAction(...)`:

- power/server override: `onPowerClick()` with `applyTarget()`
- self-service routing mode: `switchUserMode()` / `saveUserMode()`
- return to inherited global mode: `resetUserModeToGlobal()`

User action scopes stay narrow: the power button for server override actions,
the mode selector for routing-mode actions, and the concrete action block when
a row-level action exists.

Allowed non-ActionManager paths in `settings.js`:

- GET/read loaders such as inventory, logs, diagnostics, rules summaries, and
  routing projections.
- Local UI-only actions such as tab switches, dropdowns, clipboard copy, field
  validation, dirty markers, and local optimistic state before a mutation
  action is submitted.
- Validation side effects before `runAction(...)` starts.

## Runtime Relevance

- `pollJob()` timeout and progress handling affect perceived correctness for
  mode/apply operations. Long-running backend jobs may complete after UI
  polling unless timeouts are aligned with backend apply windows.
- After a mutation rerenders a row, `refresh()` can return a fresh
  `resultTarget` so success/error feedback is attached to the current DOM node.
- Refresh-on-return is event-driven and should not interfere with pending
  mutation scopes.

## Notes

- UI-visible strings should live in `static/js/fwrouter-i18n.js`; page
  controllers should pass stable i18n keys rather than hardcoded labels.
- Action state must be applied only to explicitly supplied targets. Do not infer
  lifecycle scope with `closest()` and do not use broad section-level targets
  when a form, row, or control group is available.
- Action success/error markers use a roughly 30-second default lifetime
  (`30000ms`), the color flash remains short, and old markers are cleared when a
  new action starts.
- Background/lazy operations such as ping sweeps and read-model loaders should
  not be migrated mechanically to mutation lifecycle.
