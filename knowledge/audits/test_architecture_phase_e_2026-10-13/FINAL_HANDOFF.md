# Test Architecture & CI Stabilization — final PR handoff

Branch: `stage/test-architecture-ci-stabilization`; base `eaecab5` (`origin/main`).
PR: https://github.com/Coxf0rd/fwrouter/pull/5

Status: **PRE-MERGE ACCEPTED at f416738 / READY FOR USER MERGE after the latest protected PR HEAD check succeeds**. Actual PR38347054113 passed and exact receipts were inspected. Final reporting cleanup/docs follow that proof; GitHub must enforce and pass its check on the latest commit before merge. No merge/deploy authority is inferred. Post-merge main verification remains pending.

## Phase status and acceptance boundary

| Phase | Status |
|---|---|
| A | Comprehensive audit completed;38345d4 |
| B | Refactoring/review accepted;f50d7e6 |
| C | Source coverage complete; executed in agreed PhaseD hosted scope |
| D | ACCEPTED;38305978196 |
| E Technical | Audit/corrections complete |
| E PR/required checks | ACCEPTED at actual PR38347054113; latest HEAD check required |
| Overall milestone | READY FOR USER MERGE subject to latest protected check; main event remains post-merge |
| CD | DEFERRED |
| Production | UNCHANGED |

## Complete branch review

The review covers the entire milestone diff from current main, not only PhaseE. Independent source and CI reviewers found no critical remaining production-code or affected-aggregate defect. The final GHCR review identified and removed temporary stage-marker package-write authorization; reviewed manual acknowledgment/exact-SHA and trusted main dependency-change modes remain. The optional stage anonymous consumer has no publish authority. No extra bypass, workflow permissions or deployment access was added.

The complete delta is large primarily because exact PhaseA matrices, inventory ledgers, traceability and historical receipts are retained. These are bounded source/test engineering evidence, not raw native logs/captures. Raw evidence remains outside Git. Canonical documents under `/решения/roadmap/fwrouter` are external authorities; English mirror, project contracts and exact history snapshot are version controlled. Necessary documentation consolidation was committed0b88c16; publication cleanupf416738. The final aggregate-label fix distinguishes selected QCP suites from truly unexecuted ones; validated native proof and eligibility are unchanged.

## Production-source inventory and compatibility

[PRODUCTION_SOURCE_INVENTORY.csv](PRODUCTION_SOURCE_INVENTORY.csv) enumerates35 backend production paths and two installer implementation/script paths. Tests in installer are classified separately. No DB schema/migration file changed.

| Production change group | Contract / regression proof | Future deploy risk |
|---|---|---|
| Core/dataplane full/selective routing | Mandatory VPN fail-closed; empty pool/non-endpoint REJECT is not healthy/selectable VPN; explicit/protected DIRECT and confirmed Emergency Direct preserved | Deliberate rejection replaces prior implicit DIRECT; staged supported config and independent rescue/rollback required |
| VLESS/provider/Mihomo UDP normalization | Declared supported/unsupported/unknown distinct from native transport ability and actual remote evidence; unknown may attempt compatible VPN, never proves DOWN | Old subscriptions lacking fields remain unknown; fixed protocol/transport compatibility must be retained |
| Native config/readback/reconcile/Xray | Exact config/runtime proof, revision/incarnation/source/CAS fences, generation-scoped publication and owned rollback | Config-generation/readback semantics require pinned native and compatible deployed runtime; not host/reboot proof |
| Cache/background/projection/read-path | In-flight object identity fences stale publication/removal; matching/global invalidation, deterministic prewarm fixtures, watchdog loads read-only | Shared in-process concurrency freshness; no new persisted schema or background cadence claim |
| Selector/provider/subject state | Non-server selector targets excluded, scoped credentials/source eligibility; typed provider evidence and zero normal polling | API/eligibility compatibility; missing API evidence is not remote DOWN |
| Installer | Correct isolated/optional dependency paths and install checks | Host distro/root/service boot acceptance remains separate |

Proof: PhaseD38305978196 includes1429/0/0, qualified26+1, ordinary26/Provider11/packet1 twice, historical seven7/7. Stability38305595288 is300PASS×5. PhaseE38313177638 validates selected ordinary26+Provider11; wheel38314460619 verifies247 checkout Python files. These source/profile-bound results are not claimed as production live acceptance.

## CI, network and GHCR

Fast push selects L0–L1; PR stable aggregate selects affected L0–L5 with required native/browser/QCP/packet dependencies; main adds strict native smoke/build. Shared/unknown changes broaden admission. Missing/failed/skipped mandatory proof, source/profile mismatch, incorrect nodes and cleanup failures make the aggregate ineligible. L6 nightly/manual and L7 separately authorized release policy remain distinct; neither was run by this closure.

The single reused Compose testbed runs Debian12 userland on GitHub-hosted Ubuntu kernel, real Xray/Mihomo/Chromium and confined network namespaces. Real nftables, TPROXY, policy routing, DIRECT/VPN/DNS packet endpoints, fail-closed/empty pool/Emergency Direct/protected DIRECT captures/counters/readback have accepted receipts. No host networking/uncontrolled privileged operation/real paid Provider API or production configuration is required. Docker kernel testbed is not Debian physical-host systemd/reboot acceptance.

Dependency base is immutable and source-free; anonymous consumers use the pinned digest and overlay this checked code. Publisher alone has packages-write; PR jobs have contents-read and no production credentials. Base image2,616,513,753B; measured cold35.462s versus same-job warm0.154s. Application job runner freshness means layer downloads may recur. No measured alternative warrants another image or complex cache system.

Measured PhaseE affected job wall8m33s; ordinary244.382s/Provider158.566s. These are not product performance comparisons. Accepted D process RSS high-water below194000KiB and temp storage bounded15.76/8.98/2.99MB per receipt are not aggregate CPU/RAM peaks. Whole-run CPU/RAM/disk/cache-hit measurements are incomplete and explicitly unclaimed.

## Actual PR evidence

[38347054113](https://github.com/Coxf0rd/fwrouter/actions/runs/38347054113), pull_request, checked headf416738: unit1429/0/0; qualified26+1/0/0; ordinary26/0/0; Provider11/0/0; packet1/0/0; historical seven7/7. Strict aggregate eligible with no blocked/missing levels; distinct owned receipts, pinned digest/source/plan and cleanup. See PR_CHECKPOINT_38347054113.json. Latest protected results remain available in [PR Checks](https://github.com/Coxf0rd/fwrouter/pull/5/checks); after final reporting changes, those latest results must pass before user merge.

## GitHub settings / required context

Repository: allow_squash_merge=true; allow_merge_commit=false; allow_rebase_merge=false; delete_branch_on_merge=true. Existing Main ruleset24678746 remains active, default branch only, no bypass actors, non-fast-forward guard and PR/thread-resolution requirements preserved. Allowed merge methods reduced to squash only. Added strict up-to-date required status `Affected L0-L5 with hosted profiles`, bound to GitHub Actions integration15368. Existing approval count0 is preserved; final merge remains the user's action. No unrelated access/secret/environment setting was modified.

Actual PR required context passed at f416738 and PR became CLEAN/MERGEABLE. Pending/failure on any subsequent head blocks merge. Optional branch diagnostic/publisher skipped checks are not required or counted native acceptance. Documentation-only selection remains cheap and mandatory aggregate still executes; no workflow path filter hides the required job. No workflow performs production deployment.

## Security / documentation review

All282 changed tracked files at audit (~13.08MB) were scanned for high-confidence private-key/GitHub-token formats. The sole candidate is intentional dummy PRIVATE KEY material in a redaction test; no actual key/token was identified. This scan is not exhaustive credential proof. No .env/SQLite/archive/PCAP/private production config is in the tracked delta. Public fixtures are synthetic and diagnostics bounded/redacted.

4045 untracked and2041 ignored file entries were inventoried without printing values: retained raw evidence/CI diagnostics and Python/pytest caches. These were neither removed nor staged. Only explicit reviewed source/docs paths were committed. External canonical/mirror owning requirement blocks and preserved snapshots were compared; Stage5–10 dependencies, future configuration/security/DNS/rescue/public docs/server cleanup/SDK and independent live/host gates retained. `/AGENTS.md` unchanged.

## Release/manual gates and production safety

L6/L7 remain independent policy gates; L7 needs explicit authorization. Supported configuration matrix, actual Debian host systemd/boot/reboot/physical interface/production-kernel parity, release candidate verification and safe production-live acceptance remain open planned work. New main/manual publisher event verification follows availability on the default branch; existing publisher/consumer receipts and negative authorization contracts are narrower proof.

Before any future network-impacting deployment: supported config/native versions, safe baseline, verified Rescue/SSH independent management, confidential backups and tested independent rollback. Deployment only by separately approved existing installer, with explicitly necessary managed restarts. Preserve newer SQLite intent/data; no blind rollback/replay. No production fault injection, Provider PATCH or administrative network mutation was performed here.

## Recommended squash commit

Title: `feat(ci): stabilize test architecture and GitHub Actions`

Body:

- Standardize isolated L0–L7 tests, deterministic fixtures, affected selection and baseline evidence.
- Add GitHub-hosted application/native/browser and real kernel packet acceptance with digest-pinned Debian dependencies and fresh source.
- Preserve Core/fencing/readback while correcting mandatory VPN UDP/empty-pool fail-closed and cache ownership/read-path regressions.
- Enforce trustworthy CI aggregation, source wheel identity and trusted GHCR publication.
- Consolidate roadmap/contracts/history and document independent release/host/live gates; no production deploy or CD.

## Post-merge sequence — user merge only

1. User performs Squash and Merge after required PASS/review; agent never merges.
2. Fetch main; verify one resulting single-parent squash commit matches approved PR contents. Do not require individual diagnostic commits to be ancestors of a squash commit.
3. Inspect real main-push integration/native smoke/build/affected results and artifacts. Main proof cannot exist before this merge.
4. Confirm automatic remote head cleanup. Verify no uncommitted changes and main content inclusion before local completed-branch cleanup; unfinished operational branch untouched.
5. Confirm no automatic production deployment; finalize post-merge report/milestone.
6. Only then consider separately authorized next work. Production deployment/release remains separately gated.
