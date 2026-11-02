# `backend/fwrouter_api/services/xray_generation_views.py`

## Responsibility

Publication and recovery read projections. Owns derived-row/current projection reads, artifact parity and public membership validation, recovery selection projection, supersession alias lookup, and successful publication result shaping. Result construction preserves output fields and redaction; it neither promotes a profile nor settles an operation.

## Dependency and authority boundary

Observation and existing binding/identity readers. Recovery selection lives here because it needs the current projection; observation never imports views.

Canonical functions are imported directly. Historical coordinator private names remain reexports; an explicit compatibility-only attribute alias table mirrors supported setattr/monkeypatch bindings. Components do not resolve collaborators through that facade. Direct dictionary writes and concurrent hot-patching are not runtime configuration mechanisms.

## Verification

Disposable planning/observation characterization, generation/publication/recovery suites and cold import/identity/patch-restore contracts. These checks do not claim production native or external E2E acceptance. Source fingerprinting covers the extracted executable bytes.
