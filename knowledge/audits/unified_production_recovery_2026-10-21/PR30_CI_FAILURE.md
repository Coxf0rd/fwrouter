# PR #30 first mandatory CI failure

## Proven diagnosis

Run [39520719706](https://github.com/Coxf0rd/fwrouter/actions/runs/39520719706), source `b740beda4587a6c88455fd9c81da8ce15a1a1c64`, failed the routine unit gate. Artifact `12310314675` contains the redacted unit log and JSON. The blocked suite is `backend/tests/test_configured_metadata_efficiency.py`, specifically `test_concurrent_atomic_replacements_return_complete_per_read_configs`.

The suite completed in 1.749 seconds, exit code 1, with 9 PASS and 1 FAIL. The traceback is an uncaught `MihomoYamlInvalidError: file_changed_before_read` at the strict reader's path-before/opened-descriptor identity check. It is not a timeout, OOM, crash or missing-result failure. The aggregate correctly refused eligibility; hosted native/packet provisioning and receipt aggregation were NOT RUN after this failure.

The fixture deliberately publishes complete files through `os.replace()` while configured metadata is read. The new shared strict reader rejects replacement between `lstat(path)` and `open(path)`. That refusal is required for fresh native proof but conflicts with the existing observational metadata contract, which accepts one complete old or new version per read. Replacing the configured-metadata reader with a proof reader introduced the regression. No runner-load explanation is needed for this deterministic operation-order mechanism.

## Reproduction contract

CI used Python 3.11.2, pytest 8.3.5, one serial suite, a 30-second process deadline, 262144-byte output limit and 1048576-byte artifact limit. The local pinned virtualenv has matching Python/pytest/PyYAML versions. Reproduction uses the existing gate clean environment, ownership/isolation bootstrap and gate plugin. Local hardware is not the hosted runner; no claim of identical host CPU/memory conditions is made. The original runner reported 4 CPUs and approximately 15 GiB RAM. No production inputs are fixtures.

## Correction boundary

Configured metadata needs a bounded, no-follow descriptor snapshot, not proof that the pathname still denotes that snapshot. Strict health/native policy, admission, semantic-comparison, digest and source-fingerprint paths must retain their existing path-stability checks. No retries, stale cache, timeout relaxation, assertion relaxation or native-proof waiver are authorized by this correction.

Local reproduction, final regression/QA and the next exact-HEAD CI receipt will be appended after they actually run. Production writes, admission resume, checkpoint changes, normal startup and restoration remain NOT AUTHORIZED/NOT RUN.

## Local before/after evidence

The unchanged failing suite reproduced independently of full-suite order: 9 PASS/1 FAIL in 0.85s, same `file_changed_before_read`. After correction the exact isolated gate environment completed 16 PASS/0 FAIL, pytest1.10s (wrapper1.755s), exit0, no output truncation. The existing concurrent-atomic-publication assertion is unchanged. Six added regressions cover replacement before open, replacement during descriptor read, in-place modification refusal, symlink/size refusal, strict-path refusal and explicit strict health-reader dispatch. The existing strict hash/path-replacement test also passed (other nodes deselected for that narrow check, not counted as whole-suite qualification).

Private retained records include the initial artifact, before log, final metadata stdout/JUnit/node report, pinned tool versions, exact gate reproducer and SHA256 of the three modified Python files. The initial baseline and corrected uncommitted files are distinguished explicitly. Public reports contain no production configuration or credentials.

## Expanded regression

Root-owned frozen-code regression across nine affected backend modules completed **268 PASS/0 FAIL/0 SKIP** in48.76s; wrapper wall49.340s, userCPU43.266s, systemCPU2.941s, maxRSS631840KiB. Same1.5GiB address-space/480CPU-second envelope as the previous recovery qualification, with a540s outer safety cap (expected under one minute). This includes the >20.8MB synthetic component case, adapter/native-proof refusal, admission and serialization/performance regressions. It is not actual hosted native or production acceptance. Pinned Ruff, whitespace,179-file catalog and clean deployment surface passed; no resource/test/CI limits changed. Installer source and its prior49PASS package are unchanged by this correction.

Next gate: independent source QA, coherent commit in the existing PR #30 branch, then mandatory full PR CI on that new exact commit. Draft status and separate live-qualification approval remain mandatory.

Independent frozen-source QA **PASSED**: reviewed both source files, caller boundaries, regressions and documentation; independently ran the isolated metadata module,16PASS in0.91s, and whitespace checks. Admission/native tests were not independently repeated by QA. Non-blocking limitation: controller authentication obtains its token through the scalar scanner and may observe one old/new token during atomic publication; HTTP failure remains checked and no proof is published by this path. A dedicated authentication-during-replacement test is NOT RUN. The snapshot is not an integrity attestation against adversarial compound mutation/unlink.
