# `backend/fwrouter_api/services/xray_admission_config_proof.py`

## Responsibility

Read-only, fail-closed equivalence checks for the private Xray and Mihomo
candidates used by legacy proof-only admission. The coordinator decides when
these checks are applicable; this module cannot authorize publication.

Xray retains the bounded regular-file reader, strict JSON parsing and the
narrow reordering allowance for contiguous disjoint single-user rules. Other
routing anchors and all non-rule fields remain exact. Mihomo retains the
bounded shared YAML parser and exact provenance-aware inbound sanitization
comparison. Neither comparator applies a configuration or contacts a runtime.

## Dependencies and compatibility

Dependencies point to existing format/adapter constants and Mihomo parser,
inbound and comparison helpers. There is no import of the Xray publication
coordinator. Its historical comparator names remain available through the
coordinator for supported callers and patches.

## Verification

`test_xray_generation_contracts.py` characterizes the compatibility boundary;
`test_proof_only_legacy_admission.py` covers strict positive/negative config
vectors. These are disposable source tests, not physical runtime recovery or
production acceptance.
