# Stage 4B live deployment evidence

Date: 2026-10-05. Deployed source commit: `9a4dc036768662e11a9372549df021167432710d`.

The standard installer deployed only backend and docs:

```bash
/srv/fwrouter/installer/install.sh --deploy --component backend --component docs
systemctl restart fwrouter-api.service
```

The API service alone was explicitly restarted. Mihomo and Xray containers/services were not restarted. The process stayed on the deployed commit; no source edits followed it. Before restart, the installer was followed by two read-only captures. The second capture confirmed schema 24, selection revision 37, unchanged provenance/exclusive settings, current routing selection, provider binding policy/member rows, native configuration hashes, and runtime incarnations. There were no queued/running jobs or in-progress apply versions. A final job check was repeated immediately before restart.

The protected predeploy backup is outside Git at `/var/lib/fwrouter-v2/backups/stage4b-read-retention-9a4dc036-predeploy-20261005` (directory mode `0700`, files `0600`). It contains a SQLite online backup, used `.env`, exact source and deployed backend/docs archives, native/generated config snapshots, and predeploy state/runtime metadata. The database was not restored or otherwise written by this rollout.

## Live checks

- `GET /api/v2/health` returned HTTP 200, healthy, schema 24. `GET /api/v2/system/summary` returned HTTP 200 and `ok=true`.
- `GET /api/v2/selector/vpn-auto/state` returned HTTP 200. Global mode remains selective, server mode auto, active server and target valid, and the active selection matches persisted routing state. The enabled and auto-selectable candidate counts were each 1.
- Persistent checks stayed stable across the API restart: schema 24, selection revision 37, provenance/exclusive setting hashes, routing selection, provider bindings/member rows, account/subscription state, and logical-server membership. The provider binding's automatic-member-switch policy remains false. No queued/running jobs or active apply versions were present at the final readback.
- Native config SHA-256 values for current nftables, its manifest, Mihomo config, and Xray config matched the predeploy values. Mihomo/Xray container IDs and `StartedAt` matched; the Xray process start time also remained unchanged. No apply receipt, job, or operational event appeared after API startup.
- The generated `/var/lib/fwrouter-v2/xray/fwrouter-bindings.json` sidecar changed raw and canonical JSON hashes. A field-level comparison found only its root `generated_at` and each binding's `applied_at` timestamp changed. All 78 binding identities, selected server/source/runtime targets, subject/server identities, statuses (`applied`), handoff/proxy fields, and all 10 listener identities/targets/ports/digests matched. This is a generated receipt timestamp update; the mounted native Xray config and running Xray process were unchanged. A separate bounded pre-restart comparison found only observed probe/health telemetry timestamps/counters/evidence changed; logical-server identities and member topology, provider state, routing intent, and active selection stayed stable.

The same Chromium CDP harness captured one postdeploy User → Admin → Settings flow in [UI_POSTDEPLOY_REFERENCE.json](UI_POSTDEPLOY_REFERENCE.json), alongside the predeploy run [UI_PREDEPLOY_REFERENCE.json](UI_PREDEPLOY_REFERENCE.json). Both used the same harness ID and disabled browser HTTP cache. Postdeploy usable content was observed at 4.293 s for User, 0.776 s for Admin, and 0.859 s for Settings; full phase times were 4.551 s, 1.047 s, and 1.117 s. User showed its picker/current label, Admin showed 31 server rows and 4 inventory rows, and Settings showed 64 inventory rows. Visible alert/status errors were zero in all phases. The harness recorded 16 GET API requests, all HTTP 200, blocked the external-IP probe before dispatch, observed zero non-read attempts, and restored local UI preferences. It recorded one `console.error` kind associated with the intentionally blocked external-IP probe and no page-level JavaScript exception.

Predeploy totals were User 3.500 s, Admin 5.270 s, and Settings 0.684 s. The single workspace request TTFB was 4,835.3 ms before and 601.9 ms after; the single User `/servers` TTFB was 137.6 ms before and 158.3 ms after, while the Admin `/servers` TTFB was 287.4 ms before and 310.6 ms after. The flows reuse the same browser session and application data store, so these are point observations with cache/state effects and observer-poll uncertainty. They do not establish a matched cold/warm performance change, a broad UI speedup, or that a 5.65-second `/servers` burst is fixed. Backend Health and state checks are separate from UI readiness. Postdeploy Mihomo/Xray `docker stats` was a single point sample (2.72% / 111.9 MiB and 0.27% / 40.77 MiB); it is not a before/after comparison. Process RSS/CPU observations around restart are not attributed to this change.

The normalized before/after checklist and aggregate results are in [LIVE.json](LIVE.json). Earlier isolated regression and baseline-failure evidence remains in [READ_RETENTION.md](READ_RETENTION.md). This live checkpoint does not change those test verdicts or claim the full suite is green.
