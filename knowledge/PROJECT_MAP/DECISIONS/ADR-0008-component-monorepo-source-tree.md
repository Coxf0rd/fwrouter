# 0008: Component Monorepo Source Tree

## Status

Accepted.

## Context

FWRouter has several related but distinct surfaces: backend control plane, static UI, Mihomo runtime wrapper, Xray runtime wrapper, host integration, installer tooling, and persistent knowledge. The live layout is useful for Linux FHS and systemd but unsafe as a git root.

## Decision

Use `/srv/fwrouter` as a component monorepo source tree with `backend/`, `ui/`, `runtimes/`, `host/`, `installer/`, `docs/`, and `knowledge/`.

## Consequences

- Each component can be read and deployed independently.
- Installer can deploy focused components through `--component`.
- Git must not live over `/opt` or `/`.
- Any host/runtime/boot behavior change must update affected files in `knowledge/`.

## Related Files

- `/srv/fwrouter/backend/`
- `/srv/fwrouter/ui/`
- `/srv/fwrouter/runtimes/`
- `/srv/fwrouter/host/`
- `/srv/fwrouter/installer/`
- `/srv/fwrouter/knowledge/`
