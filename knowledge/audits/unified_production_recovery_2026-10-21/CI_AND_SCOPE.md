# CI and scope gate

Baseline main e464a50dee2929558a1ef1e109d8ebafa63e179f reverified through GitHub; exact main run39503760067 completed SUCCESS. Branch `fix/production-recovery-large-mihomo` starts at that SHA; previous tracked tree equals baseline and all server-only audits/pending patches were preserved.

User explicitly approved one Draft PR before live qualification because mandatory `Affected L0-L5 with hosted profiles` runs only for pull_request or actual main push. It has no workflow_dispatch. Fast branch push or manual L6 is not substituted. The final commit must request the existing ci:full-acceptance policy.

Workflow inspection: pull_request (not pull_request_target), GitHub-hosted ubuntu-24.04, contents:read, checkout persist-credentials:false, current immutable PR head, same-repository trust checks and hashed/digest-bound test dependencies. No production environment, production secrets, SSH/deployment steps or production runner. Disposable native/packet environments and their required receipts/cleanup remain mandatory. No CI gate/timeout/resource limit is changed by this recovery.

One Draft PR will contain only necessary source/tests/docs; private raw production evidence is not staged. Full mandatory CI on its exact final HEAD must be launched once implementation/targeted tests/QA are ready. A >=10minute check requires immediate agent stop after recording run ID/URL/HEAD/checkpoint and confirming launch; no polling until user resumes. The Draft remains Draft until separately authorized live qualification is completed.

Production writes, checkpoint transfer/resume, admission retry, native apply/restart, normal startup and worker/timer restoration remain NOT AUTHORIZED during this source task. No Stage5, PR split, automatic merge or direct-main push.
