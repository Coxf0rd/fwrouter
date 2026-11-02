# `backend/fwrouter_api/services/xray_generation_staging.py`

## Responsibility

Private native candidate preparation and validation. Prepares and validates normal/proof-only candidates and validates a private Mihomo preimage. This component has bounded staging effects: private file creation/chmod/write, local/native validators and existing technical diagnostics. It is not a pure or byte-read-only module.

## Dependency and authority boundary

Planning, observation, views and existing native adapters. The coordinator supplies/owns admission and held writer guard. No activation, restart, native apply, checkpoint transitions, CAS, lease acquisition, publication, rollback decision or recovery settlement.

Canonical functions are imported directly. Historical coordinator private names remain reexports; an explicit compatibility-only attribute alias table mirrors supported setattr/monkeypatch bindings. Components do not resolve collaborators through that facade. Direct dictionary writes and concurrent hot-patching are not runtime configuration mechanisms.

## Verification

Disposable planning/observation characterization, generation/publication/recovery suites and cold import/identity/patch-restore contracts. These checks do not claim production native or external E2E acceptance. Source fingerprinting covers the extracted executable bytes.
