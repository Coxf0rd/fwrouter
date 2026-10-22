# Manual verified local deployment

## Status and authority

This repository contains **public documentation and isolated contract tests**, not the privileged deployment wrapper. The owner-selected wrapper lives outside Git, proposed at `/root/fwrouter-deploy/deploy.sh`, with independently root-controlled policy/configuration. It must never be installed, updated or invoked by an application PR, GitHub workflow, merge, git pull, timer or agent. No GitHub-to-production SSH/root/VPN credential is required or permitted.

Private implementation is in progress outside Git. Private staging qualification and independent security QA remain OPEN until actual hash-bound results are recorded. Publication of this document or a green public CI run does not install/activate that implementation or prove its private bytes were tested. A separate hash-bound private qualification receipt and independent QA are required. First installation/update and first real execution each need owner authorization. No production execution was performed for this PR.

Documentation reconciliation is a separate PR, [#34](https://github.com/Coxf0rd/fwrouter/pull/34). This PR deliberately does not edit the shared roadmap before that PR is user-merged. The architecture supersedes the researched automatic CD/poller/root-broker direction. CI V2 remains qualification only.

## Initial operation boundary

The initial candidate supports a conservative UI-only operation and read-only check. Backend activation, schema migration, systemd/host/native/runtime/network changes, recovery, refresh and reconcile are outside this wrapper's initial scope and must STOP with a reference to the existing controlled manual procedure. This is not a full-release automation wrapper.

Even `installer/install.sh --deploy --component ui` is not copy-only: it invokes the **installed backend bootstrap script**, which creates/touches state/log/run directories and `.gitkeep` files and chmods private directories/database/backup files. A qualified wrapper must pin the complete execution chain, require the installed backend/bootstrap match approved local source, require safe existing permissions and make those limited filesystem side effects explicit. No schema/identity/credential/native mutation is authorized by selecting UI. The installer overlays UI files in place; an interrupted/failed copy may leave a partial UI. Record FAIL and require manual resolution, not blind rollback or a success stamp.

## Verification contract

1. Root only; fixed trusted repository and external policy locations; controlled environment and system binary identities. No user-provided shell, paths, remote, target, component or environment command overrides.
2. Validate every critical parent directory, checkout, Git administration and external wrapper/policy ownership/modes. Reject unsafe linked worktrees, alternates, replacement refs, hooks/config execution paths, submodules, symlinked deployment input and unreviewed Git mechanisms. Reject unsafe concurrent source ownership; another trusted root administrator remains a concurrency assumption, not something clean Git status can defeat.
3. Require local branch main, attached HEAD, clean index/tracked tree and no unsafe untracked/ignored files in the deployment surface. Inspect deployment inputs, not only normal git status.
4. Obtain exact current remote main from the fixed HTTPS GitHub repository identity. Compare to local full40-hex SHA; stale origin/main is insufficient. A mismatch/unavailable/ambiguous response means NO DEPLOY; never fetch, pull, checkout, reset, clone or download deployment source.
5. Require exact-main push CI for the approved workflow identity, run ID/attempt and required jobs. Use the reviewed, independently pinned existing CI V2 aggregate contract on evidence data, not a second permissive interpretation. Reject failed/cancelled/skipped/missing required jobs/artifacts or wrong SHA/run/attempt. Downloaded evidence is never executed or used as installed source. Artifact API authentication may need a separate external read-only credential; never use a repository token file or broad owner/admin credential.
6. Take locks compatible with existing lifecycle ownership; validate installed manifest/schema/integrity/FK, generation verified/native parity, absence of unresolved checkpoints/jobs/drift, service/timer readiness and backup prerequisites. Do not initiate recovery or fix a failed gate.
7. Capture an immutable private root-owned snapshot from already-local verified Git objects and compare its bytes/modes to approved hashes. Deploy only from this snapshot, with independently pinned installer, installed bootstrap and system command chain. Recheck local identity, remote main and live safety immediately before mutation. No candidate code/artifact from GitHub becomes an executable verifier.
8. Invoke the existing installer only for the explicitly approved scope, without arbitrary restart/reload or lifecycle mutation. Persistent started/failed/interrupted operations cannot be treated as successful repeat deployments.
9. Postflight must prove installed UI manifest, API/UI/core/runtime readiness and schema/identity/credential/override/generation/checkpoint preservation, subscriptions, timers, failed units and relevant OOM/errors. Installer exit0 with failed postflight means overall FAIL. A same-manifest repeat should be a checked no-op, not another bootstrap/copy.

A production/root compromise, compromised trusted system binary or another root process intentionally rewriting protected files is outside the unprivileged-source-tampering guarantee. Root ownership alone is not proof application code is benign; human authorization and fixed external policy remain essential.

## Private logs and audit

Each authorized root invocation creates an unpredictable exclusive mode0600 log beneath root-owned0700 `/var/tmp/fwrouter-deploy/`, before ordinary preflight failure paths. Do not log API bodies, tokens, client configuration, SQL rows or raw secret-bearing subprocess diagnostics. If safe logging is impossible, fail before deployment; a non-root caller cannot be promised write access to a root-only log directory.

Temporary execution logs have seven-day retention through an owner-installed `systemd-tmpfiles` rule covering only that directory. Do not install a new cleanup daemon or a deployment timer. Permanent operation/audit receipts and backups are outside this cleanup scope. Failed/interrupted receipt state survives log expiry and blocks unsafe replay. Configuration/retention installation is separate from this PR.

## Tests and qualification boundaries

`installer/tests/fixtures/manual-deployment-contract.json` declares the34 required private safety scenarios. `installer/tests/test_manual_deployment_contract.py` exercises only public configuration/summary invariants with synthetic data: no external wrapper import, production path access, network, installer, Docker, systemd or SQLite. It does **not** qualify the private root implementation. Private tests must exercise real private logic with synthetic repositories/SQL state and mocked transport/host boundaries; failure injection uses disposable targets only. The private report must bind implementation/config/test hashes, exact results and independent QA.

Public CI must retain its existing required check, native/hosted/packet profile contracts and strict receipts. No workflow/CD trigger is added. A public test result cannot substitute for a successful main-event CI on the final post-merge SHA, an operator-approved external trust configuration, installation GO or real production postflight.

## Owner sequence before first use

1. Review/merge documentation PR34 using Squash and Merge, then review this separate Draft PR. Only the user merges. Review the private wrapper package and its independent QA/hash-bound test report separately through the existing secure administrative channel.
2. After user merge, verify mandatory main CI on the actual squash SHA. Manually bring the trusted local checkout to that main through the owner's separate Git workflow; the wrapper never does this.
3. Explicitly authorize installation of the reviewed private bytes/policy and seven-day tmpfiles rule. Validate root-only paths, complete chain pins, read-only GitHub credential scope and rollback/hold instructions. Do not copy private code/configuration into this public repository.
4. Run the read-only check first through the externally installed entrypoint; a failed gate is a STOP, not a request to weaken policy. Review its private log and current production baseline.
5. Obtain separate first-use deployment GO; invoke only the documented explicit UI operation at the exact expected SHA. For any unsupported scope use the existing controlled manual full-release procedure and its separate authorization.

The precise private installation commands, hashes and CLI are delivered in the private staging package and final operator handoff. Public paths are conventional installation locations, not a self-install mechanism. Nothing in this document authorizes a current deployment.
