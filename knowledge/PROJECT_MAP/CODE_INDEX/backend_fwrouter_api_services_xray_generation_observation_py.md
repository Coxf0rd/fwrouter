# `backend/fwrouter_api/services/xray_generation_observation.py`

## Responsibility

Generation and native-state observations. Owns source/Core/fixed/selection read projections and comparison predicates. Borrowed connections remain borrowed; optional connections retain their separate existing `db_session` windows. Native probes may refresh the existing short-lived readback cache. Read-only refers to domain authority, not a promise that connection initialization has no filesystem bookkeeping.

## Dependency and authority boundary

Existing fingerprint, Core, selection and native readback owners. No views/staging/coordinator dependency or durable writer.

Canonical functions are imported directly. Historical coordinator private names remain reexports; an explicit compatibility-only attribute alias table mirrors supported setattr/monkeypatch bindings. Components do not resolve collaborators through that facade. Direct dictionary writes and concurrent hot-patching are not runtime configuration mechanisms.

## Verification

Disposable planning/observation characterization, generation/publication/recovery suites and cold import/identity/patch-restore contracts. These checks do not claim production native or external E2E acceptance. Source fingerprinting covers the extracted executable bytes.
