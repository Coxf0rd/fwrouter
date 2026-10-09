# Phase D workflow and evidence matrix

| Lane | Event | Required work | Execution boundary | Promotion |
|---|---|---|---|---|
| Push fast | non-main push | L0 + routine affected L1 | disposable hosted runner, fail-closed Python bootstrap | Fast feedback only; profile suites explicitly deferred |
| PR affected | pull_request to main | affected L0–L5 including required profiles | no secrets, read-only token, fork cache writes disabled | exact source/plan receipts, no skipped required suite |
| Main | main push | affected plus independent native/application/browser smoke and wheel build | hosted Compose internal network | no deployment |
| L6 | nightly/manual | complete non-L7 manifest | same qualified profile adapters | separate policy gate, no blanket allowlist |
| L7 | manual, main, exact SHA and typed acknowledgement | 11 owned-process crash/recovery scenarios | unprivileged isolated acceptance container | unsupported host-native scenarios remain NOT RUN |

The temporary branch-validation workflow exercises the same gate/helper implementations without creating a PR. It is removed before delivery; its historical runs remain evidence. Event-specific PR/main behavior is source-reviewed rather than falsely reported as actual PR/main execution.

## Isolation tiers

- Routine unit suites: no real Provider API, native processes, production paths or privileged host operations.
- Functional application profile: actual Xray/Mihomo/Chromium children in a read-only, uid10001, capability-free container; CPU2/RAM2GiB/PID256/tmpfs512MiB; internal network, no host ports or Docker socket.
- Qualified child process cohort: 26 exact legacy cases in the same restricted container envelope. Only this cohort has executable bounded temporary files because its traffic-script test creates synthetic executable tools; no host networking or capabilities are added.
- Docker Xray legacy case: outer GitHub runner owns the ephemeral daemon orchestration, immutable scratch image from pinned Xray bytes, one uid65534 network-none cap-drop container. Authority is restricted to one exact node/name/image/owned temp root and never passes into the application container.

## Remaining policy boundaries

Source workflow definitions do not prove an actual remote PASS. Required-check repository rules must be bound to verified stable job contexts separately; current Main ruleset requires PR but has no required-status-check rule. No production deployment, host systemd/nftables/reboot or production lifecycle parity is inferred from process-backed acceptance.
