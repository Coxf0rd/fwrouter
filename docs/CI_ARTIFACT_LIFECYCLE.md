# CI artifact lifecycle

## Dependency and retention model

The `test-gates.yml` workflow preserves the stable required check `Affected L0-L5 with hosted profiles`. It does not grant `actions: write`, add a cleanup workflow, or change the strict aggregate's eligibility rules.

| Level | Data | Producer to consumer | Retention / lifetime | Purpose |
| --- | --- | --- | --- | --- |
| Runner-local | Logs, caches, downloaded runtimes, disposable test state, raw packet captures and intermediate build directories | One job only | Runner lifetime | Keep internal work on its runner; do not upload it merely for retention. |
| Cross-job handoff | Planner/build outputs; manifest-owned routine shard results and their validated canonical unit report; selected hosted/provider/packet/QCP/Xray receipts and runtime-input manifests, including sibling archived `profile.json` files required by QCP and Docker-Xray validators, each with a producer envelope | Planner/build/unit/profile jobs to dependent jobs and the `affected` strict aggregate | GitHub artifact: 1 day | Transfer only allowlisted immutable validator inputs between isolated runners. Worker logs, raw hosted profiles, captures, containers and disposable state stay on their producer runner. Bounded measured timing/resource facts may be retained in validator receipts and summaries; this is not permission to upload raw runtime dumps. Current run and attempt names remain mandatory. |
| Successful result | Strict aggregate report, required-result, plan, build validation inputs, selected validator receipts/runtime manifests, original producer envelopes, safe-recovery receipts and source archive when required, source/verifier/manifest files, content hashes | `affected` job to reviewers/auditors | 7 days | Preserve a bounded PR review bundle, or an actual-main bundle named `fwrouter-main-evidence-<SHA>-<run_id>-<attempt>`. |
| Failed/incomplete diagnostics | Redacted diagnostic projections of bounded aggregate/required reports, producer identity/outcome and allowlisted JSON receipts | `affected` job to maintainers | 3 days | Explain failure without retaining raw logs, packet captures, profiles, credentials, raw configs or disposable state. These projections are explicitly not original validated receipts and cannot establish qualification. |

The producer handoff is projected before upload; the successful bundle is a **bounded projection**, not a copy of all producer payloads. It preserves each handoff's sealed producer inventory and the validator inputs needed by the existing strict aggregate; it omits operational logs and non-validator payloads. The final uploader includes hidden files only for this allowlisted generated bundle because the offline verifier source includes `.github/workflows/test-gates.yml`; producer/raw directories are never uploaded through this exception. The aggregate remains authoritative and the final bundle build replays it before upload. Failure to build, verify or publish the bundle after an eligible aggregate makes the stable required job fail.

For a successful PR run, the bundle is review evidence only. Only a completed `push` event for `refs/heads/main`, with the immutable main smoke requirement met, receives the `fwrouter-main-evidence-...` name. A PR run cannot be interpreted as actual-main qualification.

## Trust and expiry boundaries

The bundle content manifest detects missing, extra or modified files relative to its manifest, and offline verification rechecks the strict aggregate using a clean, independently trusted checkout at the exact source SHA. Bundled code is data: the verifier never executes code from the archive. A checksum manifest is not a signature and does not prove GitHub origin. A consumer must independently establish the repository, workflow, required-check, run ID/attempt, event/ref, exact SHA and successful mandatory check through trusted GitHub metadata. In particular, this archive cannot authenticate its own run metadata.

Artifact existence and retention are also external GitHub facts. A deployment consumer must verify through GitHub that the exact main artifact still exists, belongs to the required successful run/attempt and has not expired. A missing or expired artifact is **NO DEPLOY**; a local cached ZIP or green badge alone is not a fallback. The offline CLI reports evidence verification, never deployment authorization.

The one-day producer artifacts support immediate workflow handoff only. If they expire before a complete qualifying run finishes, the run cannot be partially resumed from stale artifacts; rerun the complete required workflow. A GitHub cancellation that prevents the final `affected` job from executing cannot promise a three-day diagnostic bundle. In that case, retain only available Actions logs and producer artifacts until their configured expiry.

## Performance and cleanup policy

No post-run deletion is used. GitHub's short retention expires cross-job artifacts automatically; successful and failed packages use the minimum duration needed for review and diagnosis. Existing historical artifacts are not changed by this workflow update. Do not bulk-delete old artifacts based only on age or name; historical qualification evidence requires separate dependency review and exact object identification.

Future tasks that create disposable resources should follow: **temporary resource ownership → verification → cleanup → cleanup receipt**. This lifecycle change adds no privileged cleanup service and does not alter the mandatory check or branch protection.

CI V3 extends the producer inventory with deterministic routine shard provenance and separate QCP/Xray owners. The final bundle must retain the original sealed shard evidence and validate the complete disjoint partition before replaying strict qualification; a merged summary alone is insufficient. See [CI_V3.md](CI_V3.md) for coverage and execution-count semantics.

## Bounded Docker-Xray failure diagnostics

The qualified Docker-Xray receipt retains a failure-only diagnostic projection:
exact-node JUnit failure/error text, a pytest log tail, the failing harness phase,
and the cleanup operation/error class. Container state and configured resource
limits are projected only after the existing owner-label, run-nonce and exact-name
checks. No container environment, mount paths, config, or Docker log stream is
published. Successful receipts omit these diagnostic fields.

The existing native diagnostic redactor processes the complete pytest log before
retaining at most 8 KiB; logs at or above the 4 MiB safe input cap are omitted.
JUnit traceback/message and cleanup stderr have separate 4 KiB/1 KiB/2 KiB caps.
Cleanup command output uses bounded pipe drains; potentially clipped or timed-out
stderr is omitted rather than redacted without its original context. The receipt
uses the existing one-day handoff and three-day redacted failure projection.
Raw JUnit/log files remain runner-local. This does not expand the final-bundle
allowlist, increase retention, or change qualification and cleanup requirements.

Diagnostics explain failure; they never satisfy native proof or replace a missing
successful receipt. A prior runner's missing traceback or unconfirmed cleanup
cannot be reconstructed from a later successful run. Preserve original artifacts
before expiry and distinguish historical unknown causes from reproduced defects.
