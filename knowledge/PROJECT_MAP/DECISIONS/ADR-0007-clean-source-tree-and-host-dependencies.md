# 0007: Clean Source Tree And Host Dependencies

## Status

Accepted.

## Context

FWRouter deployment includes backend, UI, runtimes, systemd units, libexec helpers, timer wrappers, sysctl, and policy-routing fragments. The git source tree must not capture runtime state, secrets, databases, logs, caches, or historical scratch files.

## Decision

Use `/srv/fwrouter` as the source root. Keep `/opt`, `/etc`, `/usr/local`, `/var/lib`, `/var/log`, and `/run` as deployment/runtime targets. Validate clean source surface before commits and deploys.

## Consequences

- New Debian/Ubuntu-like servers can install base dependencies automatically.
- `.venv`, `.env`, SQLite DBs, generated configs, logs, backups, and runtime state stay out of git.
- Non-apt distributions need separate package mapping.
- Run clean-tree checks before git or deploy operations.

## Related Files

- `/srv/fwrouter/installer/check-clean-tree-surface.sh`
- `/srv/fwrouter/installer/install-host-dependencies.sh`
- `/srv/fwrouter/installer/install.sh`
- `/srv/fwrouter/knowledge/`
