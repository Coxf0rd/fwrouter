# Sysctl

## Contract

FWRouter requires host kernel settings that support forwarding, marked routing, and transparent proxying.

The source fragment is `host/sysctl.d/99-fwrouter-routing.conf` and deploys to `/etc/sysctl.d/99-fwrouter-routing.conf`.

## Required Areas

- IPv4 forwarding for router behavior.
- Mark-aware routing semantics through `src_valid_mark`.
- Reverse-path filtering settings compatible with marked transparent traffic.
- IPv6 forwarding or disablement behavior consistent with the selected FWRouter network model.

## Apply Flow

At install time, the installer can run `sysctl --system` when deploying to `/` and unit enablement is enabled.

At boot time, systemd/sysctl applies persistent fragments before FWRouter services depend on the resulting kernel behavior.

## Checks

```bash
sysctl net.ipv4.ip_forward
sysctl net.ipv4.conf.all.src_valid_mark
sysctl net.ipv4.conf.default.rp_filter
sysctl net.ipv4.conf.all.rp_filter
```

## Risks

- Missing `src_valid_mark=1` breaks policy routing for marked packets.
- Strict reverse-path filtering can drop transparent proxy traffic.
- Treating sysctl state as runtime-only causes reboot drift.
