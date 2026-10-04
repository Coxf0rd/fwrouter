# Xray generation recovery live verification — 2026-10-04

## Scope and outcome

This checkpoint verifies the deployed recovery correction on the production FWRouter host. The work used the standard backend/docs installer and restarted only `fwrouter-api.service`; no manual Xray/Mihomo restart, checkpoint edit, provider PATCH, provider member switch, or deliberate provider API request was made. The final refresh was an ordinary scoped subscription refresh. It returned `success`, `runtime_verified=true`, and `input_fingerprint_unchanged`; it was a no-op and did not force a new changed-input generation. The already materialized 78-identity generation was recovered and verified by the owned pipeline during API startup.

Source commits in the corrective chain: `001c6e9`, `95f54c1`, `5728d85`, and `a139e66ed4f72efc631a1d6ae2fba609cd4f0d7e`. The final source change imports `atomic_write_text` in the deferred staged-publication branch of the guarded Xray profile reconciliation function; it is not an import in the checkpoint recovery finalizer. The source gate reported 1,322 passed, the same 52 baseline failures, and 1 skipped; the relevant cohort reported 603 passed with the same 23 baseline failures. These are source/test results, distinct from live evidence below.

## Recovery attempts

At the start of this sequence the pending checkpoint was fenced: the service refused an unsafe restore, while current-state proof did not reach terminal closure, leaving the checkpoint in place and ordinary generation blocked. Read-only diagnosis of the first authorized attempt established that the current handoff artifact and canonical projection had the same association members but different list ordering. The source correction compares those association arrays semantically while retaining strict checks for membership, duplicates, endpoint/port/target fields, and other artifact fields. This establishes the observed current blocking condition; it does not establish the cause of the original October 2 exception.

| Job | UTC interval | Result | Runtime effect |
|---|---|---|---|
| `d63182d4-edcc-438c-a624-f7d795bae83d` | 22:08:22–22:08:50 | `XRAY_GENERATION_RECOVERY_FAILED`; `bindings_artifact_mismatch` | No config application. RO comparison showed the handoff membership lists contained the same members but in different order. |
| `55d1761a-ab87-4a1e-bf3c-052ceefca25b` | 22:37:42–22:38:04 | `XRAY_GENERATION_RECOVERY_FAILED`; `NameError` (`time`) | Failed before application. Active config, binding artifact, checkpoint, and Xray incarnation remained unchanged. |
| `8d28727c-d320-4bf0-a41a-003008a81a16` | 22:51:05–22:51:48 | `XRAY_VPN_AUTO_RECONCILE_EXCEPTION`; undefined `atomic_write_text` | Xray `StartedAt` was 22:50:19 UTC, before this job began at 22:51:05. The 78-identity materialization is therefore attributed to the startup-plus-refresh sequence, not solely to this job. The job failed during deferred publication after the observed checkpoint reached `projections_cleaned`; the projection was still pending after the job. No manual rollback or retry was done. |
| `2fc53731-12f5-4ae1-84de-dace0f251c75` | 23:08:05–23:08:45 | `success`, stage `verify`, `runtime_verified=true`, `input_fingerprint_unchanged` | No-op refresh after startup-owned recovery had closed the pending checkpoint. This does not claim a forced changed-input generation. |

The failed job result included `last_good_retained=false`; this was not treated as proof of lost runtime state. Read-only checks after the third job showed a current 78-identity projection, but do not isolate which portion of the startup-plus-refresh sequence materialized it. The final API startup recovery verified that generation before closing its checkpoint. The original October 2 exception's historical cause remains unproven.

## Backups

Before each deploy, a mode `0700` backup directory was created with an online SQLite backup (`PRAGMA quick_check=ok`) and mode `0600` payloads/manifest. A timestamped adjacent Xray config backup was also created with mode `0600` and matching owner/mode. Paths:

- `/var/lib/fwrouter-v2/backups/xray-generation-predeploy-20261003T220240Z`
- `/var/lib/fwrouter-v2/backups/xray-generation-preretry-20261003T223552Z`
- `/var/lib/fwrouter-v2/backups/xray-generation-preretry-20261003T224920Z`
- `/var/lib/fwrouter-v2/backups/xray-generation-finalpredeploy-20261003T230550Z`

The last predeploy snapshot contained the then-current 78-identity DB, Xray config/bindings/checkpoint, and candidate artifacts. Its adjacent config copy was `/var/lib/fwrouter-v2/xray/config.json.pre-generation-20261003T230550Z.bak`.

## Final recovery and runtime evidence

After deploying `a139e66`, the normal API startup recovery closed checkpoint SHA `9d27054f7850dbcd2f02c6ce2d1db048c662d8dd11aba5b11250e0cb944fd3fc`. The matching receipt is `superseded_verified`: 78 expected identities, 78 expected bindings, 78 loaded identities, and 78 binding readbacks verified; zero client-mode directives. The checkpoint file is absent. No checkpoint or receipt was manually edited or cleared.

Read-only canonical projection checks passed: 78 DB identities, 78 runtime bindings with an exact identity-set match, public projection valid, and binding artifact parity true. Native Xray API readback returned 78 inbound users; the in-memory UUID/email set matched the active DB set exactly. The host Xray config contains 78 client identities, 78/78 bindings are applied, and there are 10 handoff groups. The Xray native config test and Mihomo native config test both exited 0.

Final host/mounted hashes match:

- Xray config: `ddd71d3e4f54b81a9df171a06bbdce0ff6112aca312c47efd3db898c7ac46c58`
- Mihomo config: `170740fbf729f6c014ce9f5de2d6abddf8e1db8479437f456fa069923092d32c`
- Xray bindings artifact: `f8124fcabfeca593ec291143f7b61d29aec20700fbcdeafc01398ec6cdb3e922`

The old 70 active identities are a subset of the current 78. All 70 retain the same selected server ID/source and handoff/proxy target as before the third attempt; none were removed. The eight added DB subjects are `explicit_external_client` / `vless_client`. The schema does not expose a source-ref field on these subjects, so no source attribution is claimed.

The final predeploy DB and post-refresh DB agree on routing mode/active server, selection provenance, exclusive source, and enabled provider binding/member/revision. The owned selection revision advanced from 11 to 15 during the owned operation sequence; active server and provenance remained unchanged. The enabled provider binding remained current/applied member and revision 4/4. API-process provider request/mutation counters were zero in the measured post-restart window. These process counters reset at restart; the pre-restart cumulative value of one request is not attributable from the retained evidence, so this is not an external-provider API audit.

Both API health endpoints checked (`127.0.0.1:5000/api/v2/health` and canonical `127.0.0.1:5500/health`) returned 200. Direct HTTPS and an HTTPS GET through the existing loopback Mihomo mixed listener returned HTTP 204 from `https://www.gstatic.com/generate_204` (0.271 s direct; 0.227 s via Mihomo). Xray and Mihomo containers remained stable during seven samples over 90 seconds: revision 15, matching active/provenance, no active jobs, healthy endpoints, unchanged container incarnations, and zero provider request/mutation counters. A final two-sample check also remained stable.

Final health was healthy/HTTP 200, schema 23, `problem_count=0`. The four canonical units were healthy: API and Xray subscription gateway running, Mihomo and Xray active, and both runtime containers running. One historical failed unit, `fwrouter-subscription-refresh.service`, has a state-change timestamp of 2026-10-04 03:04:30 +07 (2026-10-03 20:04:30 UTC), before these deployments; it was not modified. The next scheduled unit run was not observed and remains open, so this report does not claim zero failed units or a successful scheduled refresh.

The current public projection has seven profiles and 70 unique identities, all contained in the native-loaded set of 78; no public identity was absent. Read-only `/servers?vpn_auto=true&include_provider_legacy=true` showed the active Provider vpn row as auto-eligible/selectable with the exclusive source reference present.

## Boundary

Live verification establishes that the pending 78-identity projection is loaded, matches the current DB/public/bindings state, and remains stable after the no-op refresh. The no-op refresh did not exercise a new changed-input generation. Changed-input recovery remains supported by the source gate and will require a naturally changed authorized input for separate live acceptance.

If rollback is needed, the approved boundary is code-only: preserve the current database intent and runtime projection. Do not restore the predeploy database, checkpoint, or artifact backup over the verified current generation, and do not blindly replay an older checkpoint's runtime writes.
