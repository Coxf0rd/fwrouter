# Python Static Analysis

FWRouter uses Ruff 0.17.0 for a bounded, fail-closed static check in the existing L0 gate. The same entry point runs before routine test selection in the push fast lane and the required affected PR/main workflow; it does not create a separate CI job.

The policy has two scopes:

- All tracked maintained Python files are checked for the complete selected Ruff `F` and `E9` rules, including unused imports/locals, undefined names and invalid export/local references. The 38 tracked `knowledge/**` audit/history scripts are excluded because they are evidence tooling, not maintained application/test source.
- Every changed Python file is checked in full for Ruff `F` and `E9` rules. Inline `noqa` cannot suppress the gate. This also covers deliberately changed historical scripts. The maintained-source baseline is now clean, so an unused import/local cannot return unnoticed in an untouched maintained file.

The gate pins Ruff through the hash-locked `tests/gates/requirements-ci.txt`, checks the exact executable version, supplies the root [`ruff.toml`](../ruff.toml) explicitly, and uses bounded path count, timeout, and diagnostic output. This prevents nested project configuration or `noqa` comments from silently weakening CI. Ruff detects undefined names and unused imports/variables; it does not prove that dynamically loaded modules, runtime attributes, or application-level contracts resolve correctly. Type-checking is deferred based on the measured feasibility results in the [static-analysis foundation report](audits/static_analysis_foundation_2026-10-18/REPORT.md); no type-check gate is implied.

Run the same committed-source and local-change gate from the repository root:

```sh
# Provision the backend dev environment from its committed lockfile, then use its Ruff executable.
uv sync --project backend --extra dev --locked
export PATH="$PWD/backend/.venv/bin:$PATH"
BASE="$(git merge-base HEAD refs/remotes/origin/main)"
python3 tests/gates/gate.py lint-changed --base "$BASE"
```

CI uses the separately hash-locked `tests/gates/requirements-ci.txt`; both paths require Ruff 0.17.0. The backend lock was refreshed with uv 0.13.1: it moves lock-format revision 3 to 5 and changes only the Ruff package/specifier, with no other package-version changes. Use uv 0.13.1 or a compatible newer version and verify with `uv lock --check --project backend`.

The command includes Python changes committed since `BASE` and staged/unstaged changes to tracked files. A new local Python file must be indexed before it is checked; use `git add -N -- path/to/file.py` for an intent-to-add entry or stage the file. Untracked and ignored files are not recursively scanned. The global check reads only Git-indexed Python paths, filters the documented `knowledge/**` history, and never follows symlinks or paths outside the repository.

To inspect a particular touched file with the same strict changed-file rules:

```sh
ruff check --config ruff.toml --select F,E9 --target-version py311 --ignore-noqa --no-cache -- path/to/file.py
```

The Ruff completion package establishes a clean global maintained-source `F/E9` baseline; it does not enable broad style/format rules or recursive checks of historical/private evidence. Earlier audit counts remain historical evidence. Retained module exports use explicit, tested contracts rather than blanket ignores.

The safe-debt cleanup package records its source-only before/after counts and deferred ownership review in the [Package1 report](audits/ruff_debt_safe_package1_2026-10-18/REPORT.md). Historical baseline reports remain unchanged; a reduced finding count does not imply all remaining imports are unnecessary.

The [complex import/definition contract report](audits/ruff_complex_contracts_2026-10-18/REPORT.md) and [remaining findings register](audits/ruff_complex_contracts_2026-10-18/REMAINING_FINDINGS.md) supersede earlier debt counts in the same PR while preserving their historical evidence. Explicit facade exports preserve the baseline namespace; they are not blanket lint exemptions.

The [completion and startup investigation report](audits/ruff_completion_startup_2026-10-18/REPORT.md) records the final baseline and source/benchmark boundaries. Hosted startup diagnostics do not authorize production operations or prove performance gains from unrelated CI snapshots.
