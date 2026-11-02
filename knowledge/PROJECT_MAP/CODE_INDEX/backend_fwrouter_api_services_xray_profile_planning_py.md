# `backend/fwrouter_api/services/xray_profile_planning.py`

## Responsibility

Generation input and prospective binding planning. Reads the existing VPN-auto eligibility and binding projections, preserving SQL ordering, read windows, fallback and handoff semantics. `compile_generation_inputs` builds desired-client/email/prefix values without generating credentials or changing identities. `GenerationInputs` is shallow-frozen; contained dictionaries and lists retain existing value semantics.

## Dependency and authority boundary

Existing identity, inventory, binding and handoff owners. No coordinator import, credential mutation, lease or publication.

Canonical functions are imported directly. Historical coordinator private names remain reexports; an explicit compatibility-only attribute alias table mirrors supported setattr/monkeypatch bindings. Components do not resolve collaborators through that facade. Direct dictionary writes and concurrent hot-patching are not runtime configuration mechanisms.

## Verification

Disposable planning/observation characterization, generation/publication/recovery suites and cold import/identity/patch-restore contracts. These checks do not claim production native or external E2E acceptance. Source fingerprinting covers the extracted executable bytes.
