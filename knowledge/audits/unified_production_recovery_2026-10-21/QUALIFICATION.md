# Unified recovery qualification log

Current local gates: source/tests/review PASS; full mandatory PR CI pending; production unchanged and live qualification not authorized. Historical iterations below remain evidence, not current PASS claims.

## Rollout checkpoint transfer package

Independent QA **PASSED** the frozen installer helper/tests/README. It reviewed schema1 compatibility, schema2 transition chain, operation/current/new manifest CAS, pending status, installed old-tree equality, stopped-unit checks before/after hashing, atomic replace/fsync, exact replay and timer-state preservation. Reviewer executed29targeted tests in0.24s and diff whitespace check. Writer ran49installer tests in1.16s with PYTHONPATH=backend, Ruff and compilation checks. Initial invocation without required backend import path had48PASS/1FAIL; corrected invocation passed. That initial failure is retained as an invocation issue, not an erased source PASS.

Source freeze SHA256: helper6f2a71edada1e99db9ac56276682d35eeffa3ed75ec81b9f3c94daee8a41db77; test69e460784cc8fd7ec869ecb516d4b1a66e565c930bd71fa6d6e56e55a3120e2b; README d908453e7915a025055a339ba96ec56a1d8ad15dce97da0c13790817e9ec3f8d.

The helper does not serialize unrelated installers/systemd callers or external database writers. Exact immutable export, single-operator window and separate writer quiescence are required. Tests are synthetic; no production checkpoint transition has been executed.

## Other gates

Backend large-config source package, affected/native qualification, full-branch independent review, exact-HEAD mandatory CI and branch live deployment remain pending until their actual results are appended. Nothing here implies production admission succeeded. Admission checkpoint remains proof_started and marker absent.

## Backend diagnostic checkpoint

A genuine synthetic >20.8MB builder→strict matcher test passed in a fresh process constrained to1.5GiB address space and480CPU seconds:21.59swall,20.94suser+0.56ssystem,492484KiBpeakRSS. This is component-process evidence, not native/production acceptance. Exact fixture bytes and final source hashes will be bound in the final result.

An affected6module invocation initially returned200PASS/2FAIL in45.7s. The two performance tests constructed MihomoHttpAdapter with DEFAULT_CONFIG_PATH pointing at production; the test isolation guard correctly refused that path. A clean git-archive e464a50control reproduced both failures identically. Their fixture-only correction uses temporary synthetic config/secret and keeps native request-count, concurrency upper/lower bound and ordering assertions. No runtime change or isolation bypass is justified. The intermediate200PASS/2deselected run is NOT a full qualifying PASS; the corrected complete group must pass before promotion.

## Independent backend review iteration 1 — CHANGES_REQUIRED

QA found remaining unbounded active-Mihomo reads in legacy native proof hashing/recheck and runtime fences. A bounded comparator alone does not bound the admission path before comparison. Root traced the related native-health/policy reader and receipt active-config hash as well; the grouped correction must use the same stable bounded input contract through those paths and bind the new parser in the existing source fingerprint. This is a required correction, not a waiver. No production operation occurred and no full CI was launched on this rejected package.

Root persisted v1JUnit/resource evidence: backend202PASS/0FAIL/0SKIP,46.616swall,41.251suser+2.660ssystem,631268KiBpeakRSS under1.5GiB address-space/480CPU seconds. Installer49PASS/0FAIL separately in0.82s. These results qualify only v1, not the pending correction. A mistakenly combined backend+installer pytest invocation produced218PASS/33FAIL because the backend safety bootstrap intentionally prohibits subprocess/system operations used by synthetic installer tests. Corrected separate execution profiles preserve the guard; no isolation relaxation or source change was made to pass that invalid combination. Failed receipts remain archived privately.

The large fixture is **synthetic component composition**: real builder/serialization/bounded reader/comparator with deterministic rule projections, while source materialization, proxy/selector generation and handoff collection are test doubles. It is not full owner/native lifecycle evidence. Existing native profiles and eventual separately approved live proof remain distinct gates.

## Final frozen correction — local PASS, independent source QA PASSED

Final root-owned backend receipt: **252PASS/0FAIL/0ERROR/0SKIP** across8affected modules, JUnit retained with every exact node ID. Wall47.984s, userCPU42.204s, systemCPU2.708s, maxRSS631864KiB (~617MiB). Process limits:1.5GiB address space,480CPU seconds; outer harness540s, expected~1minute. These are test-process resource measurements, not aggregate container/native/host memory or production startup measurements. No CI limits/startup deadline were changed. Installer profile remains separately49PASS/0FAIL,0.82s, with JUnit; no source changes to that package since its independent review.

Affected backend nodes are from test_mihomo_config_runtime, test_mihomo_staged_candidate, test_proof_only_legacy_admission, test_mihomo_status, test_mihomo_serialization, test_performance_sol_scope, test_mihomo_adapter and test_mihomo_runtime. The large positive explicitly asserts each active/candidate YAML exceeds20,834,224bytes and uses95000synthetic domain entries with three policy projections (~570k rule scalars). The exact generated files were cleaned by test lifecycle; separate exact byte counts were not exported. No claim of native/real Xray handoff composition is made from that fixture.

Grouped QA correction also covers bounded stable hashes through admission and native receipt fences, bounded adapter config read/policy parse, no controller proof on unsafe input, and parser/adapter source binding. Test expectations were strengthened to stable safe error code/no cause/no secret path and constructor non-execution; an initial assertion incorrectly expected suppressed exception context for a direct preflight rejection. Native proof predicates, policy inequality refusals and all concurrency assertions remain enforced.

Independent final source review **PASSED**, including callers, input/cache/receipt interaction, no source-fingerprint/admission bootstrap cycle, provenance, schema1→2rollout transfer and draft plan. It inspected final source and reported receipts; this final review does not claim an independently executed full backend run. Earlier installer review executed29targeted tests; earlier backend review executed194tests and correctly required the grouped bounded-read correction. No review executed production actions.

Ruff changed backend/installer files, whitespace checks, test catalog validation (179files) and clean deployment-surface check PASS. Mandatory full PR CI/native/packet/source-plan-profile receipts are still **NOT RUN/PENDING** at source-documentation freeze. Production branch deployment, admission resume, normal startup and restoration are **NOT AUTHORIZED/NOT RUN**. One Draft PR is the approved exception; it must remain Draft until separately approved live qualification and final gates.

## Final lint compatibility correction

The mandatory pinned Ruff gate uses `--ignore-noqa` and rejected the compatibility-only `yaml` import despite the earlier ordinary Ruff check. It is now an explicit `import yaml as yaml` re-export, preserving existing loader patch points without a suppression. The pinned changed-source gate passed. Both C-loader and SafeLoader fallback compatibility tests passed (2PASS,0.70s). This does not change parser behavior, limits or proof predicates. Full hosted qualification remains pending on the final commit.

## PR #30 first CI refusal and bounded correction

Run39520719706 onb740beda failed a configured-metadata atomic-replacement test:9PASS/1FAIL, exception `file_changed_before_read`, not timeout/OOM. Hosted native profiles were consequently NOT RUN. See [the retained failure analysis](PR30_CI_FAILURE.md). The observation/proof reader boundary was corrected without changing strict proof consumers or the existing concurrency assertion. Exact gate-environment metadata suite16PASS; expanded affected backend268PASS/0FAIL/0SKIP,48.76s,631840KiBpeakRSS under the same local process envelope. Full CI must run again on the new committed correction; the earlier failed run remains historical FAIL.

## Hosted packet input-binding correction

Run39522895146 ona58e154 passed routine unit nodes and the other hosted profiles but packet acceptance returned1FAIL/3PASS. [Packet failure analysis](PR30_PACKET_FAILURE.md) establishes parent-process versus API-worker adapter config-path mismatch, not a proven runtime outage. The test-local real adapter now reads the owned synthetic config before all parent selector observations. Strict runtime/proof code, timeouts and packet assertions are unchanged; explicit RUNNING is additionally required alongside vpn_auto_no_candidates. Targeted regression1PASS; complete contract169PASS, independently169PASS in14.143s; source QA PASSED. Hosted packet must be requalified on the new exact HEAD; no local contract result is counted as native acceptance. Production and both pending checkpoints remain unchanged.
