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

The runtime pins are immutable local Docker image configuration IDs (`docker inspect .Image`), not registry `RepoDigests` or mutable tags. The backend identity uses the installer's canonical filtered content manifest rooted at the resolved `__file__` of the executing `native_proof` module; `FWROUTER_BACKEND_ROOT` cannot redirect the attested tree. It is not an invented Git commit identity. The external operator policy must bind those hashes to reviewed source and approved runtime inputs. A public CI result alone cannot approve private wrapper bytes or these pins.

## Consistency boundary

For the supported `ui` scope, the native proof deliberately does not hash the UI release or its active pointer. `installer/ui_publication.py` owns that separate contract: it prepares and verifies the complete content-addressed tree, validates backend compatibility, then atomically exchanges the managed pointer while retaining the prior release. Native proof binds the executing backend and managed runtime evidence. A deployment preflight must validate both results independently; switching a UI release does not change backend, SQLite, Core, Xray or Mihomo identity, and a native proof result does not attest UI tree integrity. The UI pointer switch is atomic for server-side path resolution, not across a browser session spanning multiple requests.

The collector acquires a nonblocking shared lease on the existing Xray writer lock without creating or changing the lock. An absent, busy, unsafe or replaced lock is insufficient evidence. Existing mutation paths retain their exclusive owner; no parallel lifecycle or generation owner is introduced.

Two independent SQLite read-only/query-only transactions bracket the native capture. A context-local read-only scope also covers existing nested projection helpers: their normal database connector cannot chmod/create files, enable WAL or perform SQL writes during proof. The scope resets before the existing owner performs its separate promotion write and does not change other threads or tasks. SQLite WAL readers may use existing shared-memory read bookkeeping; this is a guarantee of no application/durable mutation, not a bit-for-bit freeze of live WAL/SHM activity. They compare schema/integrity/FK, verified inventory generation, source fingerprint, selection/Core/apply revisions, routing/modules, active jobs and checkpoint facts. Native readers verify durable Xray identity/binding/mode projections against loaded users and mounted configuration; Mihomo generated semantics, native rules, selector membership and selected targets; and Core applied artifacts, complete owned nftables semantics, policy rules and routing facts. Runtime/configuration and source identities are fenced. A final cross-component native recheck plus closing durable snapshots rejects changes that occur after an earlier reader completes.

PASS describes evidence consistent during this bounded capture window, not atomicity across SQLite and independent engines or a guarantee that no future mutation can occur after return. Missing data is never treated as agreement. The collector does not defend against a malicious privileged peer that can rewrite trusted code, native runtimes and owner receipts together.

## Existing-owner receipts and legacy readiness

The existing successful Core apply owner can record the complete applied owned-table semantics and relevant IP-rule/table100 state, bound to its apply ID, intent revision and artifacts. Normalized nftables evidence ignores only handles/counter values and elements of the two explicitly dynamic DNS timeout sets; it preserves rule order, static set contents and dynamic-set declarations. Named counters retain their names, declarations and references in the canonical table; only their volatile packet/byte values and handles are excluded. Duplicate or malformed named counter declarations and foreign ownership fail closed. Tables without named counters retain their existing canonical representation. Unknown objects fail closed.

The existing successful subscription-generation owner can attach a compact native proof tuple to its verified marker under its existing exclusive writer lease and promotion CAS. Receipt capture is an additional capability: missing legacy evidence does not reject an otherwise valid established apply or generation promotion and never preserves a stale proof tuple as current.

The external collector is strict. Existing verified markers without the required owner evidence yield INCOMPLETE. It never manufactures receipts or performs an owner operation to establish readiness. Initial capability establishment, if needed, is a separately qualified normal owner operation with separate production authority. There is no automatic migration or recovery fallback.

Core's applied-manifest receipt uses a streamed, stable file hash with a dedicated 64 MiB cap. The proof reads the exact manifest bytes under the same cap and binds the parsed object to those bytes; its closing read must match. The limit accommodates the observed roughly 24 MB generated manifest while bounding resource use and rejecting larger artifacts. It does not change limits for other native artifacts.

Mihomo's generated YAML is read using its existing no-follow, stable-file reader and 32 MiB YAML envelope. The proof parses exactly that read's UTF-8 text and hashes those same bytes; the closing stable hash must still match. This keeps parsing and identity bound to one proof-owned snapshot rather than alternate adapter-supplied text, without widening the generic 16 MiB artifact limit.

IPv6 policy proof supports active IPv6 and a host with IPv6 disabled. Disabled mode is accepted only when a bounded snapshot matches the complete `ip link` name/index inventory to `/proc/sys/net/ipv6/conf`, `disable_ipv6` is exactly `1` for `all`, `default`, `lo`, and every interface, and all-interface IPv6 address and all-table route readbacks are empty. These sysctl/interface/address/route facts enter the Core semantic hash and are sampled again across the existing owner/native fence. Address and route expiry countdowns are normalized to `finite` versus `forever` so normal timer progression does not invalidate otherwise stable topology; address/route identity and expiry mode remain bound. Any missing, malformed, unexpected, or changing fact fails closed. Active or partially enabled IPv6 continues to require both IPv6 mark rules and the owned local default route in table 100. The check reports an absent `-6_local_default_route` as false in disabled mode, alongside separate `-6_disabled_mode_proven` and `-6_policy_enforced` facts; it does not claim a route exists when it does not. The proof never enables or configures IPv6.

IPv6 link/address/route JSON is read through a selector-drained `ip` subprocess boundary: stdout is capped at 4 MiB, stderr at 64 KiB, and the command is killed/reaped after three seconds. Nonzero exit, invalid JSON, overflow, timeout, or unavailable process execution fails closed. The cap bounds bytes retained by this collector; it does not claim a bound on kernel pipe buffers or the external command's own memory.

## Result and failures

Output is structured JSON with proof schema, installed content identity, performed checks, safe counts/hashes, owner fences and component evidence. Raw configuration, client UUIDs, credentials and arbitrary exception messages are excluded. Exit0 is reserved for PASS; STALE, INCOMPLETE and FAIL return nonzero. Unsupported scope, missing or malformed pins, unavailable runtime/manifest, unresolved checkpoint, active mutation and source drift block approval.

Consumers must validate the result schema, exact expected identities and all required checks, not merely grep for the word PASS. A proof cannot authorize deployment if another preflight, source-integrity, backup, compatibility or lifecycle gate fails.

## Qualification and integration

Routine synthetic SQLite/reader tests cover fences, receipt promotion, missing legacy capability, secret-safe failures, lock races and preservation. Qualified native profiles exercise real pinned Mihomo APIs and actual Xray HandlerService/mounted-config readback. Fixture injection used for durable projection or approved runtime identity is explicit; a simulated altered rule response is distinct from a real selector change through the disposable controller API. These do not assert external client connectivity.

Actual network-namespace tests exercise complete nftables hashing and IPv4/IPv6 policy reading without touching host networking. Qualified hosted/native/packet receipts and independent final review remain separate gates, bound to the exact candidate SHA. No source test installs or activates the external private manual wrapper, changes production or enables automatic CD.


## Missing receipts after backend preparation

Installing the CLI establishes a reader, not the owner evidence it reads. A verified generation may predate this capability and still lack the required native tuple. A healthy API, unchanged generation, successful subscription response or kernel packet probe must not substitute for that tuple. If the collector returns `native_generation_receipt_missing_or_invalid`, later component checks have not necessarily executed.

The existing operator-confirmed `admit-legacy` operation has the plan action
`reprove_verified` for this exact verified-generation case. Its plan remains
read-only. By default, execution preserves the established legacy contract: it
rechecks the existing runtime/source fences and refreshes the Mihomo receipt,
without requiring or rewriting the newer Native Generation tuple. An operator
may explicitly pass `--require-native-proof` to use the existing generation
promoter with an exact digest/revision capability; that mode requires a
complete strict-reader-compatible tuple before updating the marker. It does
not bump the verified generation revision or change inventory, identities,
credentials or runtime configuration. Incomplete capture or a changed fence
returns blocked and retains the checkpoint; an existing good proof remains
untouched. The strict option is rejected before checkpoint creation for
non-verified generations. Ordinary pending-generation promotion keeps its
established optional-receipt behavior. Generic startup recovery does not invoke
this explicit reproof operation.

Named-counter parsing is a prerequisite to both Core receipt capture and verification. Receipt capture remains owned by the existing successful apply/generation lifecycle; supporting counter declarations does not itself create or backfill any receipt. Existing receipts without a matching semantic digest are not grandfathered. A later source installation or genuine existing-owner operation needs its own exact-source qualification and operation authority. Do not trigger apply, refresh or admission solely because this document or a public CI run is green.
