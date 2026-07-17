# Host Integration

Privileged host integration files for FWRouter.

## Owns

- `systemd/` - FWRouter services and timers installed into `/etc/systemd/system`
- `libexec/fwrouter/` - privileged dataplane, traffic and gateway helpers installed into `/usr/local/libexec/fwrouter`
- `sbin/` - scheduled job wrappers installed into `/usr/local/sbin`
- `sysctl.d/` - routing sysctl fragment installed into `/etc/sysctl.d`
- `iproute2/` - routing-table fragment installed into `/etc/iproute2`

## Contract

These files are the boundary between backend intent and host runtime state. Changes here can affect boot behavior, packet routing, firewall state and service ordering.

