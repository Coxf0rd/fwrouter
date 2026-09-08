# `/opt/fwrouter-ui/static/js/fwrouter-ui-action.js`

## Purpose

Thin frontend ActionManager exposed as `window.FwrouterUIAction`. It sits on
top of the existing `window.FwrouterUI` helpers and gives page controllers a
single explicit-target lifecycle for user-triggered mutations.

## Important Functions

- `runAction(options)`
  Orchestrates the lifecycle: normalize explicit targets, `startAction`,
  caller `action`, optional `pollJob`, optional caller `confirm`, optional
  caller `refresh`, then `finishAction` or `failAction` with guaranteed pending
  cleanup.
- `createActionTargets(options)`
  Normalizes only caller-provided targets: `button`, `scope`, `resultTarget`,
  `indicator`, `messageTarget`, and `disable`. It does not use `closest()` and
  does not discover broad DOM scopes.
- `startAction(...)`, `finishAction(...)`, `failAction(...)`, `resetAction(...)`
  Low-level helpers for `IDLE`, `RUNNING`, `SUCCESS`, `FAILED`, and `TIMEOUT`.

## External Dependencies

- `window.FwrouterUI.pollJob`
- `window.FwrouterUI.setDynamicStatus` / `setText`
- `window.FwrouterUI.actionMessage` / `translateBackendMessage`
- `window.FwrouterUI.setPendingState` / `setPendingStateMany`
- Existing CSS classes: `is-pending`, `is-pending-scope`,
  `is-success-scope`, `is-error-scope`, `has-result-icon`

## Runtime Relevance

Medium. The file is loaded before page controllers. Current `settings.js`
consumers are:

- subscription actions: `saveVpnSubscriptionUrl()`,
  `refreshVpnSubscription()`
- rules actions: `refreshRules()`, `updateAllRules()`, `saveRules()`
- proxy actions: `createSettingsProxy()`, `deleteSettingsProxy()`
- VLESS/external-client actions: `createSettingsExternalClient()`,
  `deleteSettingsExternalClient()`, `deleteSettingsExternalClientGroup()`
- subject/system actions: `saveSettingsItem()`,
  `deleteSettingsSystemSubject()`, `toggleSettingsAdminVisibility()`,
  `saveSettingsDisplayFromSystems()`
- external connection actions: `submitSettingsExternalSystem()`,
  `saveSettingsConnectionDetails()`, `deleteSettingsExternalSystem()`

These consumers keep backend endpoints and payloads unchanged while moving
pending/success/error feedback to explicit targets.

## Notes

- The target contract is explicit-only: the ActionManager must not apply state
  to all children and must not infer a parent scope with `closest()`.
- `refresh()` may return `{ resultTarget }` so success/error feedback can be
  attached to a fresh DOM node after rerender.
- UI-visible messages are passed as i18n keys. Backend errors still flow
  through the existing `actionMessage` / `translateBackendMessage` helpers.
