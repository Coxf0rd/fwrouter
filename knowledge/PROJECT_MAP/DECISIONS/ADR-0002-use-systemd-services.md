# 0002: Use Systemd For Boot Orchestration

## Status

Accepted.

## Context

FWRouter needs deterministic startup after reboot and ordered startup for Docker runtimes, API, helper services, and timers.

## Decision

Use root-level systemd units and timers under `/etc/systemd/system`.

## Consequences

- Systemd provides boot persistence, ordering, restart policy, and timers.
- Dependencies and readiness checks must remain explicit.
- `network-online.target` alone is not enough without preflight and wait-port helpers.

## Related Files

- `/etc/systemd/system/fwrouter-*.service`
- `/etc/systemd/system/fwrouter-*.timer`
- `/usr/local/libexec/fwrouter/fwrouter-boot-preflight.sh`
- `/usr/local/libexec/fwrouter/fwrouter-wait-port.sh`
