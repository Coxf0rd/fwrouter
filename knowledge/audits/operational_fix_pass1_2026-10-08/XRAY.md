# Operational Performance Fix Pass 1 — Xray client lifecycle

Baseline source: `4bb23916fbe346d59e42f91eb6883903e7b59316` (`4bb2391`). This note records the Xray lifecycle investigation only. No application source/runtime change, client mutation, provider request, deploy, or commit was made in this workstream.

## Call path and ownership

External no-email create first runs `RealXrayAdapter.create_client`: load active config, append the raw client identity, write a private candidate, native-test it, write active config, and reload Xray. `services/xray_clients.create_xray_client` then syncs inventory and alias metadata, calls `_materialize_xray_runtime_bindings`, and finally verifies client runtime convergence. Delete follows the corresponding adapter removal/reload, then syncs inventory and cleans the client projection, materializes bindings, and verifies absence. Alias edit remains local metadata and does not reload Xray.

The second lifecycle stage is semantically broader than the raw client mutation. `materialize_xray_runtime_bindings` prepares Mihomo handoff, collects canonical bindings and client modes, and calls `RealXrayAdapter.materialize_client_bindings`. That adapter can add binding metadata to clients and update managed egress/routing. It native-tests changed candidates and reloads; the service then checks active-file binding and mode projection, records applied state, and lifecycle code checks client presence/absence. The stages therefore cannot be merged by dropping materialization while retaining current contracts.

## No-op finding

`RealXrayAdapter.materialize_client_bindings` already returns `stage=unchanged` and skips native test/reload when the generated candidate text exactly matches the active config text and `force_reload` is false. The service still checks active binding and mode projections after that branch. The existing `test_materialize_client_bindings_skips_reload_when_config_unchanged` covers a repeated identical materialization call.

The 2026-10-07 audit records 12 fake reloads over 3 creates and 3 deletes, attributing one adapter mutation plus one binding materialization per lifecycle. Its artifact does not retain per-step candidate hashes or prove whether the materialization no-op branch was eligible. The fake runner's aggregate action count is not proof that these lifecycle operations repeated identical native/runtime work. The audit also does not record exact Xray runtime config digest/readback for the binding projection. Thus this evidence does not authorize suppressing a create/delete reload. A safe new skip would need per-step candidate equality plus a current healthy runtime/readback proving the applied config and all binding/routing semantics; config-file equality alone is insufficient.

## Validation

Focused isolated L1 adapter tests passed at baseline using the existing virtualenv and a private pytest basetemp:

```text
test_materialize_client_bindings_skips_reload_when_config_unchanged
test_create_client_adds_uuid_to_config
test_delete_client_removes_only_selected
test_create_client_writes_atomically
4 passed in 0.66s
```

No source changed, so L0 syntax and application before/after performance comparisons are not applicable. The prescribed gate's L0 check passed, but its generated domain-only manual subset applied `level(value='None')` because the caller omitted `--level`; pytest collected zero tests (exit 5) for each selected Xray file. This is a caller invocation error, not a CI or application defect, and leaves the broad L0–L5 evidence incomplete. Direct focused tests establish only the four named adapter contracts. No L6/L7 or native Xray validation was run.

## Before/after and disposition

No change was applied, so there is no before/after improvement claim. Existing fake lifecycle latency (create 260.6–286.9 ms; delete 262.5–272.6 ms), aggregate fake reload count (12), and resource samples belong to the prior audit and include its 25 ms sampler overhead; they are not comparable to an optimized run. No per-lifecycle SQL counts or candidate/config hashes were captured there.

**Pass 1 disposition:** retain both lifecycle stages and their validation/readback. Record candidate-hash/runtime-incarnation/readback instrumentation and a potential consolidated create/delete apply as a Pass 2 architecture candidate. Do not claim Xray reload work was removed.
