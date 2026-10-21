# Manual verified local deployment decision

Status: owner-approved architecture direction; implementation and activation are separate gates.

The owner rejected automatic GitHub Actions CD and the previously investigated pull-agent/root-broker design. Those local design documents remain historical research, not implementation requirements. No agent, broker, deployment timer, production credential in GitHub or CD trigger is authorized.

The owner will authenticate to the host and explicitly run a separately administered root-owned verifier outside the FWRouter Git repository. Neither code nor local trust configuration is published or updated by application PRs. Proposed installation is `/root/fwrouter-deploy/deploy.sh`, with root-only temporary execution logs retained seven days and separate permanent audit receipts; installation requires a later GO. This PR installs nothing.

The public repository may document the operation and contain synthetic, isolated deployment-contract tests. Such CI cannot be represented as testing private implementation bytes it never received. Private implementation tests and independent review must bind the tested wrapper hash. No production databases or credentials may be test fixtures.

Remote GitHub is metadata/evidence only. The verifier must require an exact trusted local main checkout matching the current remote main and successful exact-main CI V2 with complete required evidence. It must never fetch, pull, reset, checkout, clone or execute downloaded deployment code. Deployment is from a private immutable snapshot of verified already-local objects, with separate pins for the complete privileged execution chain and controlled environment. Another root process remains a trusted concurrency boundary; a clean-tree check alone cannot prevent source replacement.

Use the existing installer and lifecycle/recovery contracts. Current UI-scoped deploy still invokes installed backend bootstrap; this is not a copy-only operation. An initial conservative scope must validate/pin that chain and document its side effects, or STOP. Schema changes, backend activation, native changes, routing/firewall and recovery require the existing separately qualified controlled manual procedure. CI success alone grants none of these permissions.

Publication sequence: this documentation PR first; a separate Draft PR for public operator documentation and isolated contract tests follows. That second PR must not re-edit this roadmap before the first PR is merged by the user. No autonomous merge or production operation. Full implementation readiness depends on actual private tests, independent QA and applicable CI, not this decision record.

Production remains the qualified d313dcf baseline. Post-rollout acceptance is PARTIALLY VERIFIED: owner-reported external client success is recorded separately from absent comprehensive DNS-leak and fresh authenticated external SSH/Rescue/SFTP evidence. Raw operational evidence is private.
