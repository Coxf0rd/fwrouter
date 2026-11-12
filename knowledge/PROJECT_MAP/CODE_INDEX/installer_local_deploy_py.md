# Local deployment command

`installer/local_deploy.py` is installed by the explicit `deploy` component as
`/usr/local/libexec/fwrouter/local_deploy.py`, with the existing shell entrypoint
`/usr/local/sbin/fwrouter-deploy`. Requested source remains in the root-controlled
local config: one clean standard Git checkout and full commit. D1 adds no
Stable/Development selector, repository fetch, rollback command or audit CLI.

The single D1 deployment record is `deployment-identity.json` in the existing
controlled-rollout state directory. It distinguishes requested source, the last
content-verified installation, per-component facts/provenance and an incomplete
operation. Missing legacy metadata is UNKNOWN. Existing lifecycle checkpoints
remain the lifecycle authority; identity does not replace their admission.
Installation content verification is distinct from release/native acceptance,
which D1 reports NOT_VERIFIED rather than manufacturing a proof.

`--check` reads source, backend/UI/host content, lifecycle and identity state,
and existing readiness checks; it neither publishes metadata nor contacts GitHub.
Default apply reuses the lifecycle owner, common component/maintenance locks,
existing installer, exact selective host allowlist and atomic UI publisher.
Timer catch-up remains explicit. Identity publication must follow the existing
postflight and retain source/operation/content fences; failed or pending work
must not become a new accepted installation.

`--self-upgrade` is a separate root-authorized, exact-pin operation for the single
updater Python module. It verifies current-Main CI/artifact provenance through
fixed-host GitHub API metadata and the exact-source trusted offline verifier,
not code from the downloaded bundle. Candidate bytes are bound to Git objects.
The wrapper stays unchanged; incompatible wrapper changes fail closed. The
pending/complete updater transition shares the deployment record, preserves
application provenance, and uses existing locks and atomic durable publication.
An explicit interrupted-operation resume retains its original pins. The legacy
first bootstrap uses the separately qualified existing installer `deploy`
component; no production activation is implied by installing source or passing CI.

See [deployment contracts](../../INSTALL_AND_DEPLOY.md) and
[installer contracts](../../../installer/README.md). Tests:
`installer/tests/test_local_deployment.py`.
