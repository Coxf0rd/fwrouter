# Local deployment command

`installer/local_deploy.py` is installed by the explicit `deploy` component as
`/usr/local/libexec/fwrouter/local_deploy.py`, with the public shell entrypoint
`/usr/local/sbin/fwrouter-deploy`. The root-controlled local config records one
root-owned clean Git checkout path and its full commit. The command does not
fetch source, accept a production target override, or introduce another source
or runtime state owner.

`--check` reads source identity, installed backend/UI/host state, lifecycle
checkpoint and API health. Default apply uses `controlled_backend_lifecycle.py`,
the shared installer component lock, `install.sh --deploy`, the existing
selective host publisher for its exact two-file allowlist, and the atomic UI
publisher. A matching pending lifecycle checkpoint is resumed forward only
when its backend manifest matches the selected source. Timer catch-up remains
explicit. This local command does not qualify recovery, refresh native
generation, provision a host, or grant deployment authority elsewhere.

Tests: `installer/tests/test_local_deployment.py`.
