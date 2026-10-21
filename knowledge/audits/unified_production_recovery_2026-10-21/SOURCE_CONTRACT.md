# Recovery source contract

## Confirmed incompatibility

The accepted e464a50base selected configs only below4MiB and strict admission semantic comparison only below1MiB. A managed active config around20.8MB was therefore skipped for generation and rejected for semantic proof after YAML serialization changed byte hashes. Fallback sanitization provenance then introduced a second independent comparison barrier. The original refusal remains correct fail-closed behavior, not production acceptance.

The coordinated correction must use the current stable active base within one supported envelope, fail closed rather than silently prefer stale fallback when the current file is unsupported, and apply the same bounded/unambiguous YAML rules to reconciliation and admission. All non-provenance fields remain strict. Provenance is derived from exact active sanitization; it is not a blanket fwrouter metadata exemption. Byte equality must not bypass input safety validation.

The envelope is a managed-configuration input contract, not a changed API startup deadline, native apply timeout or CI resource allowance. Bound bytes, structural nodes and depth; reject alias expansion, duplicate/non-string mapping keys, unsupported types and changed/symlink inputs. Synthetic measurement must report actual peak RSS, CPU and wall time. Finite input/work bounds do not constitute a universal startup performance guarantee.

## Pending owner states

Backend rollout source transfer is an explicit command of the existing lifecycle owner, not a parallel rollout manager. Require old installed content proof, exact old/new manifests and operation ID, inactive rollout units and separate external-writer quiescence. Keep durable atomic provenance and original restore/timer gates. Source transfer does not grant admission or permission to restore units.

The existing proof_only_legacy_admission/proof_started checkpoint stays owned by the existing generation owner. Its source fingerprint binds durable source/intent, selection, Core revision/apply state and runtime context; it is not the backend Git commit. A source-only fix can resume the exact checkpoint only after unchanged fences, approved digest and fresh strict native proof. Do not change checkpoint fields manually, manufacture a marker, run generic recovery, refresh inventory or repair drift through admission.

## Preservation

No schema migration is planned (schema26). No identity/credential regeneration, policy/selection change, implicitDIRECT, new routing owner or native recreation is part of the source fix. Original78identities/76durable rows plus all additional records are future live invariants, never test fixtures. Rescue, DNS, Tailscale, SSH and unrelated services remain independent. Source/CI results do not authorize or prove production deployment.

## Readback boundary correction

The same stable32MiB reader/hash also covers admission's active-config snapshots/rechecks/fences and the existing native-health/receipt input paths. Node/alias/depth checks run before native controller proof consumes parsed policy. Unsafe config reports configuration unavailable/DEGRADED, not runtime DOWN or verified policy; no controller request establishes false health on that path. Existing missing/ambiguous native evidence stays blocking. The native input source fingerprint includes the shared parser and adapter so a code revision cannot silently reuse its input-fingerprint cache as unchanged. Fresh receipt publication remains with the existing owner, never a new bypass writer.
