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

At boot, `systemd-sysctl.service` applies host configuration before FWRouter
services. The API, Mihomo, and Xray preflight only read the four FWRouter-owned
effective IPv4 values with individual `sysctl -n` queries. A missing tool/key,
unreadable value, or mismatch rejects startup before the preflight creates
directories, bootstraps state, or rewrites the FWRouter routing-table
declaration. It does not repair kernel state or replay the host's `sysctl.d`
configuration.

The persistent fragment is not a runtime-admission input. Correct effective
values can pass startup when the fragment is absent, but that does not prove the
values will survive reboot; persistence must be checked separately. The
`default.rp_filter=0` value supplies the existing default for future interfaces.
The checks of `all.rp_filter` and `default.rp_filter` do not inspect every
interface: Linux uses the maximum of `conf.all` and the interface value when
applying reverse-path filtering. No per-interface admission contract is added.

The installer retains its separate existing `sysctl --system` behavior for
the normal install path when explicitly selected; deploy mode skips it.

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

Linux documents forwarding, mark-aware reverse-path lookup, and the
`max(conf.all, conf.interface)` behavior for `rp_filter` in the [IPv4 sysctl
reference for kernel 6.1](https://origin.kernel.org/doc/html/v6.1/networking/ip-sysctl.html).
`systemd-sysctl.service` applies host kernel parameters during early boot and
can process all configured files when invoked without arguments; see the
[systemd service documentation](https://www.freedesktop.org/software/systemd/man/latest/systemd-sysctl.service.html).
