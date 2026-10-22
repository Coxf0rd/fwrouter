# Atomic UI publication qualification

## Defect and contract

The installer previously overlaid the active static tree through tar. Interruption could leave old HTML with new assets or a partial file set. The correction prepares and hashes a complete safe immutable release on the destination filesystem before a Linux atomic exchange of the active pointer. The previous complete tree remains retained as a verified content-addressed release. A supported explicit restoration mode uses the same publication lock and atomic exchange, requires matching trusted UI source and installed-backend compatibility, and retains the current release. Missing/corrupt releases and incompatible source fail closed. An interrupted initial exchange is archived by the same publisher on its next invocation without replacing the active version. Errors before exchange preserve the active tree; errors after exchange report failure without blind rollback.

UI-only installation requires exact canonical installed/source backend manifest compatibility, checked under the UI publication lock before preparation and again before exchange. Full installation still copies backend first and executes the existing installed bootstrap afterward. No engine restart, networking change or alternate lifecycle owner is introduced.

## Local evidence

- 32 regression cases passed as root and independently under an unprivileged UID.
- Real `install.sh --deploy --component ui --target` processes were interrupted with SIGTERM/SIGKILL during preparation and SIGTERM after pointer exchange. Complete old/new active and retained previous trees were asserted.
- Pinned-helper child tests exercised interruption after exchange, archive and during postflight; these instrumentation tests are separate from externally signalled installer processes.
- Exact installer execution in private mount namespaces hit actual ENOSPC on a 2 MiB tmpfs and EROFS on a read-only target: nonzero exit, prior UI intact, no active partial tree.
- An isolated root source chroot executed the exact installer twice with synthetic backend/UI/bootstrap/state. All 110 UI files were present and synthetic database, environment and protected-unit hashes were preserved. It did not qualify actual systemd/container activation.
- Existing installer staging smoke, clean-surface check, Python compilation and diff checks passed.

Private raw receipts bind executable source hashes and retain exact commands. They contain local operational metadata and are not published. Mandatory CI and independent frozen-source review are separate PR evidence; local checks alone do not imply their success.

## Limits and integration

Atomicity is per server-side pointer lookup, not a transaction spanning browser requests or caches. `renameat2(RENAME_EXCHANGE)`/same-filesystem support is mandatory; no non-atomic fallback exists. Root/trusted owner peers remain able to modify their own files; this is not protection against a malicious root peer. Previous releases are retained without automatic destructive cleanup.

Final review later reproduced a check-to-switch race: a separately invoked backend installer could write during the UI publisher's last compatibility check and atomic exchange because the prior UI-only lock did not cover backend installation. The installer now holds a shared target lock across backend/UI writes, bootstrap and UI publication; direct publisher calls take the same lock before their UI-specific lock. A disposable regression pauses at the real exchange call and starts the real competing backend installer. This is cooperative serialization for supported FWRouter installer paths; privileged writes outside those paths remain outside the fence. The focused atomic UI test file passes 34 cases with this correction. A fresh disposable chroot repeated UI-only installation twice with the exact installer, publisher and lock-helper hashes; all 110 UI files were present and synthetic DB/environment/protected-file hashes remained unchanged. Installer smoke, shell syntax, py_compile, Ruff and diff checks pass. The correction still requires independent review and a new full CI run at its final source SHA.

This source correction does not deploy production or authorize private wrapper activation. The private wrapper must separately qualify the helper/import/bootstrap execution closure and managed UI symlink handling at exact final source pins. PR35 remains Draft and production-blocked. Only the user may merge.

Independent review of the first candidate requested a supported restoration path for the initial tree; that correction and six regressions are included in this candidate. The intermediate first-candidate CI remains historical only; the corrected HEAD requires its own complete CI.
