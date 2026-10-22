# Manual deployment contract qualification status

The privileged implementation is deliberately **outside this repository**. This PR publishes operator documentation, a declarative safety contract and isolated synthetic contract tests only. It does not distribute or auto-update the root wrapper.

Current public contract checks:33 isolated cases passed locally; the test catalog includes the new file. Private implementation and independent security qualification are separate hash-bound gates; their final status is recorded in the PR handoff after execution, not inferred from public CI. No production rollout, activation, migration, service restart, database/client change, GitHub settings change or automatic CD is part of this PR.

Roadmap publication belongs to PR34 first; this independent PR avoids editing that roadmap while PR34 remains open. A future chronological roadmap checkpoint can link the actual final qualification result after owner review/merge, preserving production d313dcf and formal PARTIALLY VERIFIED external acceptance.

Further isolated qualification executes exact installer/bootstrap, verifies a disposable chroot root target, and confirms interrupted UI overlay is non-atomic. The safe wrapper policy refuses changed-UI publication. Backup/restore and real namespace UI/Gateway/native readback evidence remain private, separately hash-bound and independently reviewed. Public CI does not execute those private tests. Production invocation stays blocked; installation readiness is not inferred from this Draft PR.
