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

`deploy` installs the updater at `/usr/local/sbin/fwrouter-deploy` and its Python module under `/usr/local/libexec/fwrouter`. The deploy-only installer uses the common component lock and atomically publishes the module. It preserves the existing wrapper when its type, ownership, mode and bytes match the selected source.

```sh
sudo fwrouter-deploy --stable
sudo fwrouter-deploy --rollback
sudo fwrouter-deploy --dev --acknowledge-stable-replacement
sudo fwrouter-deploy --dev --source /path/to/fwrouter
sudo fwrouter-deploy --check
```

`--stable` uses the fixed FWRouter GitHub origin for Main and the Actions API for the exact Main push, run attempt, jobs and sealed evidence. It requires working Git and `gh` access. The strict offline verifier runs against the exact temporary source after checking the downloaded archive's GitHub SHA-256. An unqualified current Main blocks deployment. Source and CI staging live in a private directory under `/tmp` and are cleaned up on exit. The durable admission record carries the exact previously qualified SHA and evidence identity for retry, so recovery can fetch that SHA and rebuild its source after the staging directory is gone. The original application and lifecycle owners retain custody of installed files and checkpoints. The updater uses the D1 module-only bootstrap when needed. A wrapper change requires the compatible installer procedure.

#### First D2 bootstrap from an installed D1 updater

After merge, wait for the successful **Main** Release Acceptance run and its sealed evidence. D1 requires the exact current Main SHA, run, attempt, artifact ID, and artifact digest. For this one-time bootstrap, use a root-owned, full-history temporary Git checkout. Preserve the existing checkouts and record the path and SHA from `/etc/fwrouter/deploy-source.json` before selecting the temporary source.

```bash
FWROUTER_PRIOR_SOURCE='<source path recorded in deploy-source.json>'
FWROUTER_PRIOR_SHA='<source SHA recorded in deploy-source.json>'
FWROUTER_MAIN_SHA='<40-character merged Main SHA>'
FWROUTER_MAIN_RUN_ID='<successful Main run ID>'
FWROUTER_MAIN_RUN_ATTEMPT='<successful attempt number>'
FWROUTER_MAIN_ARTIFACT_ID='<sealed evidence artifact ID>'
FWROUTER_MAIN_ARTIFACT_DIGEST='sha256:<artifact digest>'
FWROUTER_D1_MODULE_SHA256="$(sha256sum /usr/local/libexec/fwrouter/local_deploy.py | cut -d ' ' -f 1)"
FWROUTER_BOOTSTRAP_ROOT="$(sudo mktemp -d /tmp/fwrouter-d2-bootstrap.XXXXXXXX)"
printf 'Bootstrap checkout: %s/source\n' "$FWROUTER_BOOTSTRAP_ROOT"

FWROUTER_UPGRADE_RESULT="$(
  set -euo pipefail
  [[ "$FWROUTER_BOOTSTRAP_ROOT" =~ ^/tmp/fwrouter-d2-bootstrap\.[a-zA-Z0-9]{8}$ ]]
  source_dir="$FWROUTER_BOOTSTRAP_ROOT/source"
  sudo git clone --quiet git@github.com:Coxf0rd/fwrouter.git "$source_dir"
  sudo git -C "$source_dir" checkout --quiet --detach "$FWROUTER_MAIN_SHA"
  test "$(sudo git -C "$source_dir" rev-parse HEAD)" = "$FWROUTER_MAIN_SHA"
  test -z "$(sudo git -C "$source_dir" status --porcelain --ignored=matching)"
  sudo cmp -s "$source_dir/host/sbin/fwrouter-deploy" /usr/local/sbin/fwrouter-deploy
  sudo /usr/local/sbin/fwrouter-deploy --configure-source "$source_dir" >/dev/null
  sudo /usr/local/sbin/fwrouter-deploy --self-upgrade --json \
    --main-sha "$FWROUTER_MAIN_SHA" \
    --confirm-main-sha "$FWROUTER_MAIN_SHA" \
    --run-id "$FWROUTER_MAIN_RUN_ID" \
    --run-attempt "$FWROUTER_MAIN_RUN_ATTEMPT" \
    --artifact-id "$FWROUTER_MAIN_ARTIFACT_ID" \
    --artifact-digest "$FWROUTER_MAIN_ARTIFACT_DIGEST" \
    --expected-installed-module-sha256 "$FWROUTER_D1_MODULE_SHA256"
)"
printf '%s\n' "$FWROUTER_UPGRADE_RESULT"
```

Keep the temporary checkout and the original pins if the transition is interrupted. Resume only the `--self-upgrade` command with those pins and the same configured checkout, saving its JSON response in `FWROUTER_UPGRADE_RESULT`. Keep the original old-module SHA even if replacement already occurred. Once the upgrade reports success, verify the previous source SHA before restoring its selection. The read-only check may return 3 while the app identity remains `UNKNOWN`. Remove the task-owned temporary checkout only after the check verifies the updater and wrapper.

```bash
(
  set -euo pipefail
  jq -e '.status == "UPDATER_UPGRADED" or .status == "UPDATER_ALREADY_INSTALLED"' \
    <<<"$FWROUTER_UPGRADE_RESULT" >/dev/null
  test "$(sudo git -C "$FWROUTER_PRIOR_SOURCE" rev-parse HEAD)" = "$FWROUTER_PRIOR_SHA"
  sudo /usr/local/sbin/fwrouter-deploy --configure-source "$FWROUTER_PRIOR_SOURCE"
  check_code=0
  check_result="$(sudo /usr/local/sbin/fwrouter-deploy --stable --check --json)" || check_code=$?
  printf '%s\n' "$check_result"
  [[ "$check_code" == 0 || "$check_code" == 3 ]]
  jq -e '.stages.Updater.status == "PASS"' <<<"$check_result" >/dev/null
  [[ "$FWROUTER_BOOTSTRAP_ROOT" =~ ^/tmp/fwrouter-d2-bootstrap\.[a-zA-Z0-9]{8}$ ]]
  sudo rm -r -- "$FWROUTER_BOOTSTRAP_ROOT"
)
```

The bootstrap leaves the existing wrapper, database and application files in place. The read-only check inspects the installed updater and available local health evidence. Main qualification and application acceptance are separate.

Deployment itself launches no tests or CI. For post-merge verification, inspect `sudo fwrouter-deploy --stable --check --json`. A subsequent `sudo fwrouter-deploy --stable` requires separate live-deployment authorization. To deploy Development, use `sudo fwrouter-deploy --dev`; add `--acknowledge-stable-replacement` when the installed identity is Stable. DEV requires `/srv/fwrouter` to contain a lifecycle owner with the D2 timer restoration contract. If the checkout is behind or dirty in ways that make that contract unavailable, D2 stops at preflight. Preserve its existing changes when preparing compatible source.

`--dev` uses `/srv/fwrouter` or the checkout selected by `--source`. It captures the working contents of tracked and nonignored untracked deployable files, verifies that the source stayed consistent during capture, and reports the base Git SHA, dirty state and content digest. DEV uses the same installation, services and database as Stable. Use `--acknowledge-stable-replacement` when replacing Stable or an installation with unknown provenance. Subsequent DEV deployments can use the same command without the acknowledgement. DEV uses the local snapshot and bypasses GitHub qualification. The deployment CLI runs no test suites.

DEV admission requires compatible installer and lifecycle owners and the same strict SQLite schema and recovery protections as Stable. The admitted snapshot remains available in private deployment state while an operation is pending or DEV is installed. Recovery uses that snapshot even if the original checkout has changed or disappeared. The deployment owner retires superseded snapshots once their operation and installed-side references have been cleared. DEV leaves the accepted Stable pins available for a qualified return.

Stable supports the established backend, UI and selective host deployment scope. Schema verification uses the installed and selected strict schema-27 read-only command. Legacy structural mismatches remain an admission failure. Backend changes retain the existing recovery protection gate. The deployment can reuse an independently approved recovery backup or create a private managed snapshot through the existing SQLite backup owner. Configure the optional root-owned mode-0600 `/etc/fwrouter/deploy-acceptance.json` with these fields when approved by the runtime and recovery operators.

| Field | Value |
| --- | --- |
| `schema` | `fwrouter-deploy-acceptance/v1` |
| `xray_image`, `mihomo_image` | Independently approved immutable Docker image configuration IDs, prefixed with `sha256:` |
| `ui_port`, `ui_path` | Loopback HTTP endpoint for the installed UI index |
| `recovery` | Optional external backup pin with `backup_path`, `backup_sha256` and `installed_backend_sha256` from the approved recovery plan |

The updater verifies schema, SQLite integrity, foreign keys and the content hash before backend publication. The managed snapshot uses SQLite's online backup and is bound to the preceding source and backend manifest. Before changing the backend in DEV, the deployment protects the accepted Stable baseline with an exact verified backup. It reuses an approved or managed backup when available. A new DEV baseline backup is reserved separately so that the previous Stable release keeps its recovery backup. The owner verifies retained recovery data before retiring a backup after accepted return to Stable. UI-only, NOOP and status checks create no database backup. An external backup remains under its owner's retention policy.

Stable records the current and previous **accepted** source independently of the most recent completed file install. `--rollback` selects the previous accepted Stable release when Stable is installed and the most recent accepted Stable release when DEV is installed. It reconstructs that exact SHA and compares its component manifests with the saved acceptance. It uses the normal lifecycle and compatibility gates, preserves the updater, keeps the current database and requires fresh acceptance. It needs access to the approved Git objects even after the temporary checkout is removed. Transitions involving DEV use the pinned installed-side manifest to identify source files safe to retire. Stable-only rollback keeps the installer's existing retirement allowlist. For schema migrations or database restore, use the applicable recovery owner.

Storage preflight checks the filesystems used for staging, publication and recovery before installation. Fresh Main retains the Git ancestry required by the evidence verifier. An accepted rollback stages only the pinned target and installed-side source. Following acceptance, the updater retires catalogued obsolete resources whose exact ownership and references can be checked. Resources with unknown ownership are preserved. A private staging lease lets later accepted runs remove expired abandoned staging while skipping active or unrecognised directories. The lease expires after 24 hours. Reports show active and rollback versions, backup state, space estimates, cleanup results and protected resources.

The updater checks UI availability and calls the installed read-only native proof owner with independently approved runtime pins. Missing pins, an unavailable probe, or missing current owner receipts leave acceptance incomplete. Qualification, file identity, lifecycle, health and native acceptance are reported independently, including on a content NOOP. The default CLI flushes progress to stdout as stages run and sends diagnostic errors to stderr. `--json` reserves stdout for the structured report, and `--verbose` includes diagnostic details. The explicit modes use exit 0 for success or a fully accepted NOOP, 1 for failure, 2 for invalid arguments, 3 for incomplete verification and 130 for interruption. `--check` inspects local state without fetching, cleanup or persistent writes.

During Stable resume, the existing lifecycle owner restores previously active timers after verifying their stock units and discarding missed calendar triggers. The old traffic timer is migrated to a nonpersistent minute schedule because starting its elapsed boot timer could immediately run collection. Enablement is preserved and previously inactive timers stay inactive. The legacy configured-source and explicit timer catch-up commands remain available for D1 operations.

Explicit D1 `--self-upgrade` replaces only the updater module and accepts the original exact root-pinned SHA, run, attempt, artifact, digest and old-module inputs. Stable supplies those facts from its automatic qualification. Both forms use the same component lock, shared maintenance lease and durable updater transition record. An interruption resumes only the originally pinned source and module transition. Legacy missing installation identity remains `UNKNOWN`.

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
