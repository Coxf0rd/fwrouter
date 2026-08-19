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
- `all`

`backend` installs the FastAPI tree and minimal Python/SQLite dependency set. `host` installs dataplane/systemd/sysctl/iproute2 files and host networking tools. `mihomo` and `xray` install managed Docker runtime trees and Docker/TUN-related dependencies. `ui` has no system runtime dependency set.
