# Python Static Analysis

FWRouter uses Ruff 0.17.0 for a bounded, fail-closed static check in the existing L0 gate. The same entry point runs before routine test selection in the push fast lane and the required affected PR/main workflow; it does not create a separate CI job.

The policy has two scopes:

- All tracked maintained Python files are checked for undefined names (`F821`) and invalid `__all__`/local-variable references (`F822`, `F823`). The 38 tracked `knowledge/**` audit/history scripts are excluded because they are evidence tooling, not maintained application/test source.
- Every changed Python file is checked in full for Ruff `F` and `E9` rules. Inline `noqa` cannot suppress the gate. This means an edit to a file with old unused-import/local-variable findings must resolve those findings in the touched file; unchanged historical findings are not treated as a clean global `F` baseline.

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

Do not run a repository-wide `F` or broad style/format pass and call the result a clean baseline. Current untouched files contain pre-existing unused-name/style debt; this gate deliberately prevents new debt in changed files while global critical-name checks remain enabled.
