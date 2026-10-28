# Backend prerequisite for manual UI deployment

## Authority and current boundary

This is a separate, owner-authorized controlled backend preparation procedure, **not** an operation of the UI-only wrapper. Merging source does not install it. An installed backend without `native-proof`, or an existing owner without the required native receipts, must block UI deployment. A healthy API alone does not establish native readiness. Public tests exercise contracts and disposable targets; they do not qualify private bytes, Debian host equivalence, or production installation.

No command below is authorized for production by this document. The owner must separately approve the exact backend source, dependency compatibility, installation, API lifecycle, backup/recovery plan and timer decision. A successful prerequisite does not authorize wrapper installation or a UI deployment. No generic automatic bootstrap is added.

## 1. Read-only preparation

Use only independently trusted local source and already-local Git objects. Verify attached clean main, fixed remote, exact remote-main SHA, fresh exact-SHA actual-main mandatory evidence and its offline replay, source/installed manifests and the privileged executable closure. Do not fetch code or execute a downloaded bundle. Confirm the installed interpreter/venv/dependencies support the new backend; `--deploy` deliberately does not install OS dependencies or recreate the venv. A dependency change requires separate qualification, not a hidden pip/apt fallback.

Record current SQLite schema/integrity/FK through supported read-only access, identities/credentials/overrides as private hashes/counts, current (not hardcoded) generation, Core intent/enforcement/native parity, jobs, checkpoints, active units/timers, API/Gateway health, protected files and independent administrative access. Never print credentials, subscription URLs or raw configurations. Stop on drift, active conflicting work, unresolved recovery, source mismatch or unknown compatibility.

This procedure supports **schema27 → schema27 only**. Any other observed schema or new schema requirement is a separate migration scope and STOP. Do not invoke an apply migration here. Before an authorized backend mutation require a consistent verified backup, protected runtime/source manifests and a qualified restore plan. No backup is created merely to review this document.

Prepare two immutable root-owned exports from already-local reviewed source:

- `OLD_EXPORT`: its canonical filtered backend manifest must equal the currently installed backend. Use the qualified old source/lifecycle helper, not the newly merged helper's `stop`: `stop` verifies its own source against installed files. If the exact old export is unavailable, STOP; do not reconstruct a fake checkpoint.
- `NEW_EXPORT`: the reviewed desired source; bind its installer, lifecycle helper, bootstrap and all transitive dependencies to approved hashes. Preserve private `.env` and venv; they are excluded from the content manifest but remain separately protected inputs.

Use the existing lifecycle/native-proof canonical filtered backend manifest, **not a Git SHA substituted for its digest**. Independently record `OLD_MANIFEST`, `NEW_MANIFEST` and the approved immutable local runtime image configuration IDs. No policy approval is inferred from a current-host hash inventory.

## 2. Hold and source transfer (future separately authorized mutation)

The following are command shapes, not an executable rollout script. Replace the named inputs only after independent review. Use a clean fixed environment and trusted interpreters. Preserve each actual helper result as a private receipt and stop on any nonzero exit.

```sh
python3 "$OLD_EXPORT/installer/controlled_backend_lifecycle.py" stop
```

Require `status=stopped`; save its actual `operation_id`. The existing lifecycle helper stops only its allowlisted previously active API dependents/timers and API, refuses active one-shot jobs, and creates its own checkpoint. Do not stop native engines unnecessarily. Recheck that every rollout-owned unit and transient job is stopped; independent admin access must remain available.

Before transferring source ownership, stage and load the reviewed API unit while
all rollout-owned writers are stopped. Backend-only deployment does not install
systemd units. Use the existing host component only after reviewing its complete
source manifest and confirming that the exact host delta is limited to the
explicitly approved `fwrouter-boot-preflight.sh` and API unit changes; stop on
any additional host delta. Keep the lifecycle-owned API, Gateway, and events
services held and the four timers inactive while publishing this pair. Xray
and Mihomo engines remain running;
do not restart runtime units during publication. Then verify the installed
helper and loaded API unit before backend transfer. This is a bounded
source-specific host step, not permission to update host components generally.
It does not authorize production publication or API activation by itself.

```sh
"$NEW_EXPORT/installer/install.sh" --deploy --component host
systemctl daemon-reload
```

`--deploy` neither enables units nor reloads systemd itself. This host step still
runs the installed bootstrap-state script (directory/permission preparation);
review those effects as well. Verify the on-disk API unit hash, loaded
`ExecCondition`/`ExecStart`, and absence of overriding drop-ins before proceeding.
The loaded unit must use `fwrouter_api_maintenance startup-check` and `run-api`.
For the qualified old source without those commands, a restart at this point
must fail closed; it must not fall back to direct uvicorn. No API start is needed
for this staging step, and no timer is enabled. A failed unit publication or
reload is STOP before transfer, with the coherent OLD backend retained.

```sh
python3 "$NEW_EXPORT/installer/controlled_backend_lifecycle.py" transfer \
  --expected-operation-id "$OPERATION_ID" \
  --expected-current-manifest-sha256 "$OLD_MANIFEST" \
  --expected-new-manifest-sha256 "$NEW_MANIFEST"
"$NEW_EXPORT/installer/install.sh" --deploy --component backend
```

Transfer must succeed against the existing owner checkpoint and old installed tree before overlay begins. It supports only its documented exact CAS replay; do not edit checkpoint/source markers. The installer owns the common backend/UI component lock, source copy, obsolete-source retirement and bootstrap. Backend installation additionally holds the existing maintenance lease exclusively and requires the existing writer/unit allowlist to be quiescent; UI-only publication keeps its shared maintenance lease. The API holds its shared lease through startup and process lifetime, excluding backend overlay. Both sides of the source tar stream must succeed; an extraction success alone is not installer success. Backend copy is not an atomic source publication: keep API/workers stopped until the complete export is independently verified. The installer also performs bootstrap filesystem side effects; it is not a byte-only copy. Do not add `--all`, host/native components, undocumented skips or manual source copying.

For a pending ordinary rollout, startup admission compares the executing installed backend manifest with the checkpoint target digest. It repeats this check under the shared lease before boot preflight. After transfer, OLD or partial source fails admission; a complete matching NEW tree may start for the existing readiness-before-resume sequence. This is a coherent-source guard, not an installer-completion, schema, native-proof or authorization receipt. It cannot prove that bootstrap finished if interruption occurred after all source bytes were copied. Keep that failure at STOP until the explicit post-install checks succeed.

If the installer fails/interruption occurs, retain compatible held state and diagnostics. A partially copied tree is not runnable. Do not blindly rerun `stop` against changed source, start the partial backend, roll back schema or restore a database. Re-enter only the existing reviewed source-transfer/installer recovery contract after inspecting actual checkpoint and installed manifest. A partial installed tree is intentionally not an admissible `transfer` replay: retain the already committed transition, revalidate its operation/source identities, and complete only the exact reviewed NEW installer under quiescence. Do not invent another operation, edit the checkpoint or overlay OLD onto NEW.

## 3. Installed export, schema and readiness

Before activation verify exact installed filtered manifest equals `NEW_MANIFEST`, protected inputs and credentials/identities/overrides are preserved, dependency closure remains approved and SQLite is still schema27 with integrity/FK PASS. The installed qualified CLI supports the same-schema read-only command:

```sh
/opt/fwrouter-api/.venv/bin/fwrouter database upgrade --expected-schema 27
```

Require `already_current` and `database_changed=false`; **never add `--apply`** in this prerequisite. This command does not prove that API/native state is ready.

Only after source/schema compatibility is proven, explicitly start the API as separately authorized. The lifecycle helper's `resume` does not start API for you. Check normal API health and database health. Startup may execute normal owner work; it must be qualified and observed, not assumed read-only. If the owner explicitly accepts non-destructive live-host qualification, record that distinct authority and remaining NOT RUN staging scenarios; never extend it to destructive tests. Stop on unexpected generation, routing, credential or ownership changes; do not silently reconcile them.

Collect a fresh proof through the installed qualified executable with independently approved hashes:

```sh
/opt/fwrouter-api/.venv/bin/fwrouter native-proof --scope ui \
  --expected-backend-manifest-sha256 "$NEW_MANIFEST" \
  --expected-xray-image-digest "$APPROVED_XRAY_IMAGE_ID" \
  --expected-mihomo-image-digest "$APPROVED_MIHOMO_IMAGE_ID"
```

Image IDs are Docker `.Image` configuration IDs (`sha256:...`), not mutable tags or registry RepoDigests. Validate exit status **and** the complete existing proof schema/checklist/source identities/generation/fences/native evidence using the independently pinned consumer. `FAIL`, `STALE`, `INCOMPLETE`, missing receipts or wrong identity means STOP. Do not grep for PASS, accept a cached projection, run legacy admission, synthesize a receipt, or call apply/refresh/recovery to manufacture readiness.

Missing old owner capabilities require a **separate explicitly authorized and qualified normal existing owner operation** (Core apply and/or subscription generation as indicated by the real failure). It may mutate native state and is not automatically covered by backend preparation. This prerequisite cannot promise that installing code alone creates those receipts. Record the exact missing capability and stop at that boundary.

## 4. Resume, idempotency and final readback

After healthy API and owner/native readiness, use the same new source's existing `resume` command. By default persistent timers remain pending; this is not full completion. Reactivation with `resume --allow-timer-catchup` requires a distinct owner decision because persistent timers may immediately run missed work. Require the checkpoint's real completed status and verify originally active dependents/timers and subscriptions. Preserve originally inactive services as inactive.

After any authorized timer catch-up has settled, repeat fresh native proof/readback and final health/preservation checks. Do not assert a fixed generation number across legitimate scheduler activity: prove current verified revisions and consistency. Active mutations or changed fences must invalidate the proof and require a new stable read, not fabricated equality.

Repeated prerequisite review is read-only. If installed source, readiness and approved identities already match, do not reinstall or rerun owner operations: repeat fresh proof and remaining UI-only preflight. Interrupted stop/transfer/resume is idempotent only within the existing operation-ID/source CAS contract, not by blindly retrying the entire procedure. Old/incomplete receipts never become successful receipts. Backup restore and source rollback always remain separate reviewed recovery decisions; no automatic rollback is introduced.

## Qualification required before use

The normal API launcher runs the host boot preflight before it execs Uvicorn.
That preflight reads only the four existing FWRouter-owned IPv4 routing values
(`ip_forward=1`, `all.rp_filter=0`, `default.rp_filter=0`, and
`all.src_valid_mark=1`) and fails closed on a missing reader/key or a mismatched
value, before its bootstrap and routing-table-file effects. It does not write
kernel values or run `sysctl --system`, so unrelated host sysctl configuration
is not replayed on each API start. The Mihomo and Xray units call the same
preflight.

Boot-time persistence remains the responsibility of the host's
`systemd-sysctl.service` and the installed sysctl fragment. Runtime preflight
does not require or create that fragment: correct effective values may pass
without it, while their persistence across reboot remains **NOT VERIFIED**.
Likewise, checking `all.rp_filter` and `default.rp_filter` does not prove that
every existing interface is permissive; Linux applies the maximum of the
global and interface values. These are the pre-existing managed values, not a
new per-interface admission rule. See the [kernel IPv4 sysctl
reference](https://origin.kernel.org/doc/html/v6.1/networking/ip-sysctl.html)
and [systemd-sysctl service documentation](https://www.freedesktop.org/software/systemd/man/latest/systemd-sysctl.service.html).

Source qualification must cover the existing stop, source-CAS transfer, installer/startup barrier, lease, schema27 preservation, backup and recovery contracts in isolated tests and exact-source CI. Real installed Docker/systemd/native behavior, dependency and worker compatibility, genuine proof, controlled resume and external-client acceptance remain separate host/live gates.

For the current Stage5 source-closure scope, the owner has accepted moving those non-destructive installed-host checks to a separately authorized controlled production rollout and live acceptance; creating a disposable host is not a source prerequisite. This decision does not label unperformed staging E2E as PASS or authorize deployment, native apply, registry cutover or the private UI wrapper. When destructive interruption/reboot/corruption testing lacks a qualified isolated environment, record NOT RUN and its residual risk; do not perform it on production to satisfy a checkbox. A future owner-approved operation must first establish independent administrative access, a genuine protected WAL-safe backup, exact OLD/NEW source and trusted inputs, bounded startup effects, STOP conditions and safe forward completion.


## Evidence establishment after preparation

Preparation may finish with healthy API/Gateway but native readiness still INCOMPLETE. Preserve that result and the actual performed-check list; it is not successful UI readiness. In particular, `native_generation_receipt_missing_or_invalid` is a missing owner capability, not permission for the observer to synthesize a tuple. A later Core reader can independently fail on unsupported nft objects; fixing its parser in source is neither a live patch nor a receipt migration.

Before any separately approved owner operation, require the reviewed parser implementation, exact installed source pin, current native image pins, no conflicting writer/recovery and an explicit plan for existing state. Preserve old receipts as history and reject mismatched semantic hashes. No same-value routing request, arbitrary refresh or hidden legacy admission is part of this procedure. Timers held by an incomplete lifecycle remain subject to a separate restoration/catch-up decision.

## Qualification limits of the source-coherence barrier

The pending-manifest and exclusive-lease guards address partial source startup
and concurrent installation. They do not make the backend overlay atomic or
supply a missing Debian/systemd/native qualification. Host reboot and unattended
persistent-timer reactivation are not proved by process-level crash fixtures;
`systemctl stop` does not itself disable enabled timers across a reboot. Keep
those scenarios outside a qualified claim until actual evidence establishes
their hold/recovery behavior; without qualified isolated staging, destructive
scenarios remain NOT RUN under the owner-approved source/live separation. Do not use a manual catch-up or native
apply to manufacture acceptance. No production procedure is authorized here.
