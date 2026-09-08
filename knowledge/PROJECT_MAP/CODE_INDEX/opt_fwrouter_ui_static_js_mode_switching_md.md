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
  pending/highlight primitives, escaping, byte formatting, and flag helpers.
- `fwrouter-ui-action.js`
  Shared explicit-target ActionManager exposed as `window.FwrouterUIAction`.
  Settings mutation handlers now use it for pending/success/error lifecycle
  instead of duplicating manual `try/catch/finally` flows.
- `settings.js`
  Settings controller. It owns settings journal loading, rules editor actions,
  subscription actions, proxy actions, external connection actions, VLESS client
  creation/deletion, subject item edits, display visibility mutations, cache
  invalidation, and post-mutation workspace/inventory refresh.
- `admin.js`
  Admin controller for global mode, selective defaults, VPN-auto server
  selection, server preference autosave, and device/VLESS management.
- `user.js`
  User controller for current subject state, self-service mode switching,
  server override toggling, server lists, and current/VPN IP refresh.
- Renderer/helper modules
  `fwrouter-labels.js`, `fwrouter-settings-events.js`,
  `fwrouter-settings-inventory.js`, `fwrouter-settings-journal.js`,
  `fwrouter-settings-domain-state.js`, `fwrouter-admin-devices.js`,
  `fwrouter-admin-autolist.js`, `fwrouter-user-servers.js`,
  `fwrouter-ip-check.js`, and `ping-select.js` keep rendering and shared UI
  behavior out of the large page controllers.

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
- Background/lazy operations such as ping sweeps and read-model loaders should
  not be migrated mechanically to mutation lifecycle.
