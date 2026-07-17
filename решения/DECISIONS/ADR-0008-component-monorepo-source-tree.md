# ADR-0008: Component Monorepo Source Tree

## Status

Accepted.

## Context

FWRouter состоит из нескольких связанных, но разных участков:

- backend control-plane
- static UI
- Mihomo runtime wrapper
- Xray runtime wrapper
- host-level systemd/libexec/sysctl/policy-routing integration
- installer/deploy tooling
- persistent project knowledge map

Live layout на сервере (`/opt`, `/etc`, `/usr/local`, `/var/lib`) удобен для systemd и Linux FHS, но плох как git root: в нем легко смешать source, secrets, venv, generated state, logs, backups and runtime artifacts.

## Decision

Использовать `/srv/fwrouter` как основной git/source root и хранить проект как component monorepo:

- `backend/`
- `ui/`
- `runtimes/mihomo/`
- `runtimes/xray/`
- `host/`
- `installer/`
- `docs/`
- `решения/`

Live paths остаются deployment targets:

- `backend/` -> `/opt/fwrouter-api`
- `ui/` -> `/opt/fwrouter-ui`
- `runtimes/mihomo/` -> `/opt/fwrouter-mihomo`
- `runtimes/xray/` -> `/opt/fwrouter-xray`
- `host/systemd/` -> `/etc/systemd/system`
- `host/libexec/fwrouter/` -> `/usr/local/libexec/fwrouter`
- `host/sbin/` -> `/usr/local/sbin`

## Consequences

- Пользователь может скачать/читать отдельный участок проекта без обхода live-style tree.
- Installer может ставить выбранные компоненты через `--component`.
- Git не должен жить поверх `/opt` или `/`.
- `.env`, `.venv`, SQLite DB, generated configs, logs, backups и runtime state не входят в source tree.
- Любая правка host/runtime boot behavior требует обновления соответствующих docs в `решения/`.
