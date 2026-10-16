# Installer

Source-tree installer and validation tools.

## Files

- `install.sh` - installs selected components from this monorepo into live system paths. Use `--deploy` on an already prepared host when only copying selected local source components to live targets; it skips apt dependencies, backend venv setup, unit enable/daemon-reload, Docker network creation, and sysctl reload.
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

The helper does not install files, start the API, restore a database, or change networking. If `stop` exits partway through, keep the checkpoint and rerun `stop` before installing; it continues the same saved operation. If `resume` exits partway through, keep the checkpoint and rerun `resume` after API readiness; completed unit starts are safe to repeat. Do not delete a pending checkpoint to bypass a failed health or source-export check.

Persistent timers are stopped when they were active before rollout. By default `resume` restores long-running dependents and leaves those timers listed as pending. To allow systemd's `Persistent=true` catch-up behavior, review the timer consequences and explicitly run:

```bash
python3 "$FWROUTER_TRUSTED_SOURCE/installer/controlled_backend_lifecycle.py" resume --allow-timer-catchup
```

That option applies only to timers that were active in the saved checkpoint. It does not directly invoke their one-shot services.
