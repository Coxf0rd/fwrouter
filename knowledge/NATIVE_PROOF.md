# Read-only fenced native proof

## Supported scope and authority

`fwrouter native-proof` is a local CLI contract for the managed UI deployment preflight. It proves the installed backend and current managed native state; it does not approve a backend, schema, host, networking or recovery deployment. It creates no generation, checkpoint, records, configuration or runtime reload and never invokes admission, refresh, reconcile or apply. There is no new HTTP endpoint, agent or privileged service.

The caller supplies independently approved installed-backend and runtime identity pins:

```sh
fwrouter native-proof --scope ui \
  --expected-backend-manifest-sha256 APPROVED_BACKEND_SHA256 \
  --expected-xray-image-digest sha256:APPROVED_XRAY_IMAGE_ID \
  --expected-mihomo-image-digest sha256:APPROVED_MIHOMO_IMAGE_ID
```

The runtime pins are immutable local Docker image configuration IDs (`docker inspect .Image`), not registry `RepoDigests` or mutable tags. The backend identity uses the installer's canonical filtered content manifest; it is not an invented Git commit identity. The external operator policy must bind those hashes to reviewed source and approved runtime inputs. A public CI result alone cannot approve private wrapper bytes or these pins.

## Consistency boundary

The collector acquires a nonblocking shared lease on the existing Xray writer lock without creating or changing the lock. An absent, busy, unsafe or replaced lock is insufficient evidence. Existing mutation paths retain their exclusive owner; no parallel lifecycle or generation owner is introduced.

Two independent SQLite read-only/query-only transactions bracket the native capture. A context-local read-only scope also covers existing nested projection helpers: their normal database connector cannot chmod/create files, enable WAL or perform SQL writes during proof. The scope resets before the existing owner performs its separate promotion write and does not change other threads or tasks. SQLite WAL readers may use existing shared-memory read bookkeeping; this is a guarantee of no application/durable mutation, not a bit-for-bit freeze of live WAL/SHM activity. They compare schema/integrity/FK, verified inventory generation, source fingerprint, selection/Core/apply revisions, routing/modules, active jobs and checkpoint facts. Native readers verify durable Xray identity/binding/mode projections against loaded users and mounted configuration; Mihomo generated semantics, native rules, selector membership and selected targets; and Core applied artifacts, complete owned nftables semantics, policy rules and routing facts. Runtime/configuration and source identities are fenced. A final cross-component native recheck plus closing durable snapshots rejects changes that occur after an earlier reader completes.

PASS describes evidence consistent during this bounded capture window, not atomicity across SQLite and independent engines or a guarantee that no future mutation can occur after return. Missing data is never treated as agreement. The collector does not defend against a malicious privileged peer that can rewrite trusted code, native runtimes and owner receipts together.

## Existing-owner receipts and legacy readiness

The existing successful Core apply owner can record the complete applied owned-table semantics and relevant IP-rule/table100 state, bound to its apply ID, intent revision and artifacts. Normalized nftables evidence ignores only handles/counter values and elements of the two explicitly dynamic DNS timeout sets; it preserves rule order, static set contents and dynamic-set declarations. Unknown objects fail closed.

The existing successful subscription-generation owner can attach a compact native proof tuple to its verified marker under its existing exclusive writer lease and promotion CAS. Receipt capture is an additional capability: missing legacy evidence does not reject an otherwise valid established apply or generation promotion and never preserves a stale proof tuple as current.

The external collector is strict. Existing verified markers without the required owner evidence yield INCOMPLETE. It never manufactures receipts or performs an owner operation to establish readiness. Initial capability establishment, if needed, is a separately qualified normal owner operation with separate production authority. There is no automatic migration or recovery fallback.

## Result and failures

Output is structured JSON with proof schema, installed content identity, performed checks, safe counts/hashes, owner fences and component evidence. Raw configuration, client UUIDs, credentials and arbitrary exception messages are excluded. Exit0 is reserved for PASS; STALE, INCOMPLETE and FAIL return nonzero. Unsupported scope, missing or malformed pins, unavailable runtime/manifest, unresolved checkpoint, active mutation and source drift block approval.

Consumers must validate the result schema, exact expected identities and all required checks, not merely grep for the word PASS. A proof cannot authorize deployment if another preflight, source-integrity, backup, compatibility or lifecycle gate fails.

## Qualification and integration

Routine synthetic SQLite/reader tests cover fences, receipt promotion, missing legacy capability, secret-safe failures, lock races and preservation. Qualified native profiles exercise real pinned Mihomo APIs and actual Xray HandlerService/mounted-config readback. Fixture injection used for durable projection or approved runtime identity is explicit; a simulated altered rule response is distinct from a real selector change through the disposable controller API. These do not assert external client connectivity.

Actual network-namespace tests exercise complete nftables hashing and IPv4/IPv6 policy reading without touching host networking. Qualified hosted/native/packet receipts and independent final review remain separate gates, bound to the exact candidate SHA. No source test installs or activates the external private manual wrapper, changes production or enables automatic CD.
