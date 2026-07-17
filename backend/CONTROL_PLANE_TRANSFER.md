# Control-Plane Transfer

This backend supports local control-plane snapshot export and later restore on a Linux host.

The intended workflow is selective and incremental:

- do not treat snapshot import as "copy the whole local machine";
- import control-plane state only when that exact state is needed on the host;
- keep code rollout, state import and runtime verification as separate operator steps.

The transfer workflow is intentionally split into five steps:

1. export local state into a snapshot file;
2. copy the snapshot file into the Linux server transfer directory;
3. inspect available snapshot files on the server;
4. run validation and dry-run import planning;
5. import the snapshot, then run Linux-side apply/verify.

## Scope

The snapshot is designed for control-plane migration, not runtime cloning.

Included:

- `settings`
- `modules`
- `routing_global_state`
- `subjects` with detail rows
- `subject_user_overrides`
- `subject_server_overrides`
- `servers`
- `server_preferences`
- `server_ping_state`
- `subscription_state`
- `rules_state`
- `rules_metadata`
- rules text/effective artifacts

Not carried as authoritative runtime:

- live nftables state
- live Mihomo runtime
- live Xray runtime
- job history
- operational logs
- traffic accounting snapshots

By default the import normalizes runtime/apply state so that the Linux host does a fresh apply instead of inheriting local runtime claims.

## API

All routes live under `/api/v2`.

### Export

`GET /transfer/control-plane/export`

Query flags:

- `include_secrets=false|true`
- `write_file=false|true`

Typical local export:

```http
GET /api/v2/transfer/control-plane/export?include_secrets=false&write_file=true
```

Notes:

- The saved file goes into `state/transfer/control-plane-snapshot.<timestamp>.json`.
- `subscription_state.url` is redacted unless `include_secrets=true`.

### List Snapshot Files

`GET /transfer/control-plane/files`

This returns the transfer directory and known snapshot files with:

- path
- size
- modified time
- snapshot version
- exported time
- object counts

### Validate

`POST /transfer/control-plane/validate`

Allowed inputs:

- inline `snapshot`
- `file_path` pointing to a file inside the transfer directory

Example:

```json
{
  "file_path": "control-plane-snapshot.20260514T120000Z.json"
}
```

### Plan

`POST /transfer/control-plane/plan`

This is the recommended pre-import step. It does not mutate the database.

It returns:

- snapshot validation result
- simulated post-import expectations
- scoped egress diagnostics/readiness
- tables/files that would be restored

Recommended request:

```json
{
  "file_path": "control-plane-snapshot.20260514T120000Z.json",
  "normalize_runtime_state": true
}
```

### Import

`POST /transfer/control-plane/import`

Recommended request:

```json
{
  "file_path": "control-plane-snapshot.20260514T120000Z.json",
  "normalize_runtime_state": true
}
```

After success:

- database control-plane tables are replaced by snapshot state;
- rules files are restored from the snapshot bundle;
- runtime/apply state is reset to a fresh host baseline when normalization is enabled.

## Linux Playbook

Recommended server-side flow:

1. Export locally with `write_file=true`.
2. Copy the snapshot JSON into the Linux host transfer directory under `/var/lib/fwrouter-v2/transfer/`.
3. On the Linux host call `GET /api/v2/transfer/control-plane/files`.
4. Run `POST /api/v2/transfer/control-plane/plan` for the chosen file.
5. Check:
   - validation is `ok=true`
   - `scoped_egress.readiness.state` is understood
   - warnings are acceptable
6. Run `POST /api/v2/transfer/control-plane/import`.
7. After import, run the normal Linux apply/verify workflow for routing, rules, scoped egress and watchdog.

## Selective Import Strategy

For real rollout, prefer importing snapshots in support of a concrete server-side task.

Typical examples:

- routing/rules test:
  - import snapshot
  - verify `rules`, `routing_global_state`, `servers`
  - run apply/verify only for global direct/vpn/selective work
- scoped egress test:
  - import snapshot
  - verify `subjects`, `subject_server_overrides`, `servers`
  - inspect `GET /runtime/scoped-egress`
  - test one subject class at a time
- Xray/backend test:
  - import snapshot
  - verify `subjects`, `subject_user_overrides`, `subject_server_overrides`
  - inspect Xray status/export APIs

If a snapshot contains more than you want to exercise on the host right now, keep the extra state imported but only enable/apply the feature slice you are currently validating.

## What To Check Before Import

Before server-side import, confirm:

- the target host already has the intended code/scripts copied into place;
- `.env` matches the feature you want to validate;
- the snapshot `plan` matches the intended slice of work;
- warnings in `plan` are understood, especially:
  - `scoped_egress`
  - `core bypass`
  - rules source/readiness
  - missing runtime prerequisites

## Guardrails

- `file_path` is restricted to the backend transfer directory.
- Snapshot import is not a replacement for Linux runtime verification.
- `normalize_runtime_state=true` should remain the default for cross-host migration.
- If scoped egress readiness is `blocked` or `degraded`, that is a deployment signal, not a silent success.
