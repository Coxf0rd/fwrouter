# Read-only fenced native proof correction

## Root cause and scope

The verified inventory marker did not bind a complete current Xray/Mihomo/Core native tuple. Existing admission/staging collectors can mutate lifecycle ownership, so they cannot serve as a deployment preflight. Independent GET results and internally consistent files cannot substitute for owner-bound parity.

The new local CLI supports only the conservative managed UI scope. It binds canonical installed backend content and explicitly approved immutable runtime image IDs, verified inventory, native Xray identities/bindings/configuration, Mihomo semantic generation/rules/current selectors, and Core applied artifacts/complete owned-table/IP policy evidence. It uses the existing Xray writer barrier, two read-only durable snapshots and a final cross-component recheck. STALE, INCOMPLETE, unavailable or malformed evidence never yields PASS.

Existing successful Core and generation owners can publish compact receipts under their established ownership and CAS. Missing legacy capability does not break established normal apply/promotion, but cannot satisfy external strict proof. The collector creates no receipt, generation, record, checkpoint, apply, refresh, reconcile or runtime reload. Initial owner capability establishment remains a separately qualified operation, not automatic recovery.

See [operator and result contract](../../NATIVE_PROOF.md).

## Defects found during qualification

- Independent review found that `FWROUTER_BACKEND_ROOT` could point the source-identity check at a matching decoy tree while the CLI executed an imported module elsewhere. Attestation now derives the manifest root from the resolved executing module path, and a CLI-handler regression sets the override to a decoy whose hash is used as the approved pin; the result reports the executing package hash and returns `STALE` before reading runtime or database state.
- Nested existing projection helpers used the ordinary DB connector, which chmods files and configures WAL. A context-local read-only mode in the existing connection owner now covers the entire proof projection chain without changing independent threads or the later owner promotion write. Real SQLite tests reject attempted TTL mutation and verify preserved durable bytes, permissions and timestamps.
- Actual `iproute2` output can use configured table aliases, IPv6 default spelling and numeric RTN_LOCAL. The proof requests numeric names and handles the documented local route forms; unicast remains rejected. Real IPv4/IPv6 network-namespace tests exercise the corrected reader.
- Per-component fences alone missed a restart after an earlier reader returned. Final cross-component readback now rejects this window.
- Arbitrary exception text is not an allowed public diagnostic. Only fixed safe reason codes are returned.

## Evidence boundaries

Routine regressions cover read-only SQLite/lock/source fences, legacy/incomplete state, generation receipt promotion, durable/native mismatch, runtime identity pins, concurrency, exception redaction and optional receipt failures. Actual isolated pinned Mihomo APIs exercise the production collector and reject a real selector change. The altered-rule response negative is explicitly synthetic. Actual isolated nftables tests cover dynamic timeout-element normalization, extra accept rules, static-element drift and real IPv4/IPv6 policy facts.

The registered qualified child profile includes the new Mihomo collector node; the existing isolated Xray Docker profile additionally invokes the new Xray collector against actual HandlerService and mounted configuration. Its narrowly injected identity seam is bound to the profile's already-validated pinned image/version. Local skips do not qualify these profiles. Full mandatory CI and independent frozen-source QA are recorded separately at the exact PR HEAD; this document does not turn pending or skipped checks into PASS.

No external client connectivity, arbitrary host configuration, privileged malicious-peer resistance or full browser/session acceptance is claimed. The proof describes a bounded stable capture window, not atomicity across independent engines or future immutability. SQLite WAL readers may perform shared-memory read bookkeeping; no application/durable mutation is allowed.

## Sequential integration after Atomic UI merge (2026-10-23)

The owner merged PR36 by squash into main `765a355e57c70935fd54699b04ee01f2fb82709e`; actual-main CI [39845388758](https://github.com/Coxf0rd/fwrouter/actions/runs/39845388758) succeeded. PR37 integration is based on that main. Cross-contract tests execute the real UI publisher for initial directory-to-pointer publication and a subsequent managed-pointer switch, checking that retained previous releases remain complete. The fail-closed case omits the generation owner's native tuple and remains `INCOMPLETE`; it asserts native readers are not called. A separate explicitly synthetic case supplies deterministic Xray/Mihomo/Core reader dictionaries and an owner-shaped tuple to the actual collector, which exercises its `PASS` path before and after both UI transitions. It verifies all native readers and both durable snapshots run each time, and that backend identity, durable facts, component fingerprints and the native proof digest remain unchanged. That synthetic `PASS` validates the collector contract only; it is not actual runtime evidence or a live proof result.

Local integration checks: native-proof regressions 28 passed; full atomic UI installer suite including both cross-contract cases 36 passed; safe recovery 13 passed; backend bootstrap 36 passed; installer backend lifecycle 29 passed; CI acceptance contracts 175 passed (four unknown-browser-marker warnings); manifest validation covers182 files; exact QCP inventory is43 node IDs with existing per-file counts; Ruff, whitespace and clean-source-surface checks passed. These local checks do not replace strict full CI V2, exact integrated HEAD receipts or independent integrated review. No production or private-wrapper operation occurred.

## Integration and production boundary

The atomic UI publication correction is an independent feature PR from the same qualified base. Their catalog/roadmap additions occupy separate insertion points. Both must be integrated by the owner and the resulting actual main must pass full main-event CI before combined-source qualification is asserted. No synthesized combined tree is a qualified production release.

The private wrapper remains outside Git, uninstalled and execution-blocked. Its snapshot/import/bootstrap closure, managed UI pointer policy, proof parser and exact policy pins still require separate private qualification. PR35 stays Draft. No production deployment, restart, migration, native operation, merge or automatic CD is performed by this source task.
