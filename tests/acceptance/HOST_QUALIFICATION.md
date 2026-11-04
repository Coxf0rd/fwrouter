# Host qualification controller contract

`host_qualification.py` is an opt-in controller API for a future independently
qualified GitHub-hosted Linux runner. No gate, workflow, profile selection, or
current PR/main/L6 policy invokes it. A caller must provide a `VerifiedRun`
obtained from an external GitHub run verifier, along with SHA-256-pinned
`qemu-system-x86_64`, `qemu-img`, and Debian guest inputs. This record is an
integration boundary, not a self-authenticating proof; matching process
environment values and the local checkout SHA do not authenticate GitHub run
metadata.

`admit()` checks the verified source/repository/workflow/event/ref/run/attempt
against actual GitHub-owned process context, requires the source SHA to equal
the checked-out `HEAD`, requires clean tracked/untracked source and tracked
controller entrypoints, checks hosted Linux runner class/image/sudo/capability
and externally measured resource headroom, and verifies the binary/image
digests for both QEMU executables and the guest image. The guest must be a
standalone raw disk image; backing chains and external data files are rejected.
Its declared virtual size must fit the configured overlay disk bound. It
rejects missing or mismatched prerequisites before creating a
temporary directory or starting a process.

`qualify()` creates a private ephemeral workspace and a bounded QCOW2 overlay,
then starts pinned QEMU with TCG, bounded vCPU/RAM/disk, a restricted QEMU user
network with no forwarding, no host mounts/devices, and a guest-agent channel.
Guest vCPU/memory/virtual-disk allocations have explicit controller bounds;
admission reserves additional CPU/memory/disk headroom using values reported by
the external verifier. The receipt marks these as verifier-reported and not
independently verified by this controller; it does not measure host resources
or QEMU overhead. It requires systemd as PID 1 with `systemctl is-system-running`
equal to `running` before and after a clean `systemctl reboot`, reconnects
through the controller-owned guest-agent socket, and requires a changed UUID
boot identity. The outer controller attempts SIGTERM then SIGKILL against only
its QEMU process group within one bounded cleanup deadline. A run passes cleanup
only when the process group is gone and its temporary state is removed. If
process-group cleanup cannot be proven, the controller reports failure and
preserves the owned work directory rather than unlinking files still possibly
in use.

The receipt binds source/run/attempt/runner/image/nonce and input digests, but
always sets `requires_external_provenance_verification=true` and
`deploy_eligibility=false`. It is capability-smoke evidence only. It does not
qualify FWRouter daemon/component fault matrices, Docker daemon loss/recovery,
native parity, destructive recovery, or production behavior. Receipts include
verifier-reported resource/capability facts, guest limits, boot IDs, and
controller cleanup outcome; they do not represent local host measurements or
console/journal capture. Any required guest diagnostics must be bound by a
future producer. Synthetic guest
factories in unit tests exercise controller failure and cleanup paths; they
cannot produce host-qualification evidence. Full qualification remains blocked
until an authorized GitHub-hosted run has the verified pins and measured
headroom. This production-server checkout must never be used as the runner.
