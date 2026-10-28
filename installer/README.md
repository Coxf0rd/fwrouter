# Installer

Source-tree installer and validation tools.

## Files

- `install.sh` - installs selected components from this monorepo into live system paths. Use `--deploy` on an already prepared host when only copying selected local source components to live targets; it skips apt dependencies, backend venv setup, unit enable/daemon-reload, Docker network creation, and sysctl reload.
- `component_install_lock.py` - serializes cooperating backend/UI installer operations and direct UI publication for one target.
- `safe_recovery.py` - prepares, verifies, and isolated-stages hosted-qualified backend recovery artifacts; it is not a live restore or rollback tool.
- `install-host-dependencies.sh` - installs component-scoped apt packages required by FWRouter on apt-based systems; `--dry-run` prints the selected package set without mutating apt state.
- `test-install.sh` - static installer tests for component copy boundaries and dependency dry-runs.
- `check-clean-tree-surface.sh` - validates that the source tree has the expected deployable files and excludes runtime/secrets.

## Component Names

- `backend`
- `ui`
- `mihomo`
- `xray`
- `host`
- `docs`
- `homeassistant`
- `all`

`backend` installs the FastAPI tree and minimal Python/SQLite dependency set. `host` installs dataplane/systemd/sysctl/iproute2 files and host networking tools. `mihomo` and `xray` install managed Docker runtime trees and Docker/TUN-related dependencies. `ui` has no system runtime dependency set.

`homeassistant` copies only the FWRouter package and action script into `/app/config/homeassistant`. It is opt-in and excluded from `all`, because Home Assistant is an external service. It does not restart or reload Home Assistant; validate configuration and restart HA separately after deployment.

## Selective host publication

`--deploy --component host --host-plan PLAN.json` is a separate, narrow path for publishing a nonempty subset of exactly two allowlisted files: `host/libexec/fwrouter/fwrouter-boot-preflight.sh` and `host/systemd/fwrouter-api.service`. If both are listed, they must appear in that order. The plan must use schema `fwrouter-host-publication-plan/v1` and provide each entry's exact source and target paths, the source SHA-256, the expected old target SHA-256 (or `null` when absent), and the fixed mode (`493` / `0755` for the helper, `420` / `0644` for the unit). Duplicate JSON keys, extra fields, other paths, wrong modes, writable plan files, symlinks, non-regular files, and stale target hashes fail closed. The source bytes and modes are rechecked against the plan before any target write. If bytes already match but the target mode needs normalization, the plan must set the expected old digest to that same digest; the publisher then atomically replaces the file with the fixed mode. A target already matching both digest and mode is an idempotent no-op.

The selective command takes the common installer lock and an exclusive maintenance lease for the full operation. On a live `/opt` target, the existing lease requires the backend writer units to be quiescent. It publishes the helper first and the API unit second. Each file replacement is atomic and fsynced with readback; the pair is not a multi-file transaction. A failure after the first replacement leaves that file published and returns failure. Re-running the same exact plan is safe: a target already matching that plan's source digest and mode is fsynced and accepted, while every remaining target must still match its expected old digest or absence. A changed target fails without overwrite.

The lock serializes cooperating FWRouter installers and their maintenance lease. Privileged writes made outside that contract are not serialized by it.

This path bypasses the general host tree copy and exits before bootstrap, `.env` permission changes, unit daemon-reload/enable, service actions, sysctl, or any other host file copy. It does not establish that the API unit is compatible with the installed backend, stop or activate services, or perform service readiness checks. Qualify backend/unit compatibility and service lifecycle separately. Plain `--deploy --component host` retains the existing full host installation behavior.

## Controlled API Rollout Lifecycle

For a planned source rollout that must stop the API, use the same trusted, clean source export for the helper and backend installer. Run these steps as root, replacing the path with the export used for this rollout:

```bash
FWROUTER_TRUSTED_SOURCE=/path/to/trusted-clean-fwrouter-source
python3 "$FWROUTER_TRUSTED_SOURCE/installer/controlled_backend_lifecycle.py" stop
"$FWROUTER_TRUSTED_SOURCE/installer/install.sh" --deploy --component backend
systemctl start fwrouter-api.service
python3 "$FWROUTER_TRUSTED_SOURCE/installer/controlled_backend_lifecycle.py" resume
```

Before writing a checkpoint or stopping any unit, `stop` compares the installed backend's filtered path set with the clean source export. Extra paths, including `.ruff_cache`, or non-excluded symlinks block the operation; missing paths and old contents at expected paths are allowed because the installer replaces files by overlay. The helper never deletes extras. Inspect and back up the host state, then resolve the extras through an explicit operator procedure before retrying.

After preflight, `stop` writes a root-owned, mode-`0700` durable checkpoint under `/var/lib/fwrouter-v2/state/controlled-rollout` before it stops only previously active allowlisted API dependents and the API. It refuses to interrupt active one-shot jobs. The checkpoint binds resume to the backend source export captured before stop; `resume` checks the same trusted export and requires the filtered `/opt/fwrouter-api` file manifest to match exactly, then requires the API health response to report both the API and database healthy. It restores only units recorded active in that checkpoint. Gateway restoration also waits for its configured listener at `172.18.0.1:5055`.

If a controlled rollout is already pending and the reviewed branch source changes, do not edit or remove `pending.json`. The same lifecycle owner provides an explicit, root-only `transfer` command. It requires the exact saved operation ID, current checkpoint manifest digest, and new trusted-export manifest digest; it verifies the installed backend still exactly matches the old checkpoint and that every rollout-owned API, dependent, timer, and one-shot unit is inactive. It then atomically binds the existing operation to the new manifest, preserving the original active-unit set and timer gate while recording the old/new digest transition. The command never starts or stops services. It rechecks unit inactivity after hashing the installed tree. Run it within a single-operator maintenance window: the helper lock serializes lifecycle commands, but it does not lock out an unrelated installer or systemd operator. An exact replay after checkpoint publication is idempotent while the installed backend matches either side of that last recorded transition; a later source revision requires another explicit CAS transfer after the prior revision is installed. At most 16 transitions are retained. A missing external-writer check is not implied by unit quiescence: the operator must separately establish that no out-of-band database writers remain before transfer or deployment.

Example (fill every value from the protected checkpoint and the reviewed clean export manifest; do not copy placeholder hashes):

```bash
python3 "$FWROUTER_TRUSTED_SOURCE/installer/controlled_backend_lifecycle.py" transfer \
  --expected-operation-id '<pending operation UUID>' \
  --expected-current-manifest-sha256 '<checkpoint backend_manifest_sha256>' \
  --expected-new-manifest-sha256 '<reviewed trusted export digest>'
```

The new source digest must match the helper's trusted source export. After transfer, install that same export, then use the normal health-gated `resume` path only when the separately authorized rollout phase permits it. Legacy schema-1 checkpoints remain readable; transfer upgrades their checkpoint format atomically while preserving their operation ownership.

The helper does not install files, start the API, restore a database, or change networking. If `stop` exits partway through, keep the checkpoint and rerun `stop` before installing; it continues the same saved operation. If `resume` exits partway through, keep the checkpoint and rerun `resume` after API readiness; completed unit starts are safe to repeat. Do not delete a pending checkpoint to bypass a failed health or source-export check.

Persistent timers are stopped when they were active before rollout. By default `resume` restores long-running dependents and leaves those timers listed as pending. To allow systemd's `Persistent=true` catch-up behavior, review the timer consequences and explicitly run:

```bash
python3 "$FWROUTER_TRUSTED_SOURCE/installer/controlled_backend_lifecycle.py" resume --allow-timer-catchup
```

That option applies only to timers that were active in the saved checkpoint. It does not directly invoke their one-shot services.

## Atomic UI publication

`--component backend`, `--component ui`, `--all`, and selective host publication with `--host-plan` use the common `/opt/.fwrouter-component-install.lock` (under the selected `--target` root when disposable). It is held from before backend/UI/selected host writes through installer bootstrap and, when selected, the final UI compatibility check and pointer switch. This prevents cooperating FWRouter installer invocations from changing the backend in the check-to-switch interval. The lock runner validates the target path and lock file, passes its open descriptor through exec, and the installer checks that the inherited descriptor identifies the exact target lock and still owns it. Other component-only installs do not take this lock. A privileged process that edits files outside the installer contract is outside this serialization guarantee.

`--component ui` publishes `/opt/fwrouter-ui` from a prepared immutable release below `/opt/.fwrouter-ui-releases`. It takes the common component lock before the UI publisher's own lock. A direct invocation of `ui_publication.py`, including explicit retained-release restoration, acquires the same common lock itself before checking backend compatibility; installer calls reuse the already-held validated descriptor. The source and installed backend must have identical canonical filtered manifests from `controlled_backend_lifecycle.py`; `--all` installs backend first and then applies this compatibility check. For live deployment the checkout and installed tree must be root-owned; disposable targets must be owned by the invoking identity. UI files and directories must be regular, non-symlink entries with safe ownership and modes that let the static-file server read files and traverse directories. Entry count and total/per-file byte limits are enforced. Their bytes, file modes, and directory modes are hashed and verified before and after publication. The installer fsyncs prepared files/directories and the containing directories, then uses Linux `renameat2(RENAME_EXCHANGE)` to replace an initial real UI directory without an absent-path interval. Unsupported syscall/filesystem behavior fails closed; there is no non-atomic fallback.

`safe_recovery.py` packages the exact tracked backend source plus its supported backend installer execution closure: `install.sh`, `obsolete-backend-paths.txt`, and `component_install_lock.py`. Preparation and verification require all three installer inputs in the source manifest and archive. The isolated stage extracts those verified bytes together and invokes the exact included installer against its private `--target`. This keeps the disposable recovery rehearsal aligned with the current installer contract without making it a live recovery path.

Existing active UI trees remain whole immutable releases. The initial real directory is exchanged with a symlink and archived as `release-<content-sha256>`. If the process is killed in the short interval after exchange and before archive, the next publisher verifies and completes that archival under the same lock. Identical pre-existing releases are verified and never overwritten; an identical exchanged tree is retained at its recovery path. A failed postflight reports failure and retains both the active and prior releases without blind automatic rollback. Managed active symlinks must point directly to a content-addressed release below the managed root. The existing Nginx document root can follow this pointer atomically for each filesystem lookup. This guarantees server-side pointer atomicity only; it does not make a browser session or HTML/JavaScript requests across cache and multiple requests one atomic version.

An operator may restore a retained UI release only from a trusted local source tree whose UI digest exactly matches that release and whose canonical backend manifest matches the installed backend. The same publisher validates the release and switches the pointer atomically, retaining the current active release. It never restores or modifies the backend. From a reviewed checkout with the intended UI source and compatible backend, the explicit helper form is:

```bash
python3 installer/ui_publication.py \
  --source "$PWD/ui" \
  --target /opt/fwrouter-ui \
  --backend-source "$PWD/backend" \
  --backend-installed /opt/fwrouter-api \
  --restore-release '<64-character UI content SHA-256>' \
  --require-root
```

The source digest must equal the requested release digest. Missing/corrupt releases, a different UI source, or backend-manifest mismatch fail closed before pointer change. The command does not decide whether a rollback is operationally appropriate; it supplies only the same atomic UI publication primitive under the existing deployment lifecycle owner.

The controlled backend lifecycle CLI uses its separate durable rollout lock only for the duration of that CLI operation; that lock is released before the installer is invoked. Do not treat it as nested or held during installer execution. The UI publication helper does not install or stop services, mutate host networking, reset bootstrap state, or download source.
