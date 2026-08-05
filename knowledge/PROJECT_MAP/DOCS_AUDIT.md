# Docs Audit

## Scope

This file tracks documentation hygiene for the in-repo FWRouter knowledge map.

## Current Contract

- Tracked project documentation and knowledge-base content in `/srv/fwrouter` must be written in English.
- Local non-English notes may live outside the git project in the owner-local decisions tree.
- Keep documentation updates narrow and tied to the changed behavior.
- Do not rewrite the whole project map unless the architecture actually changed broadly.

## Required Reads For Agents

Before non-trivial changes, read:

- `knowledge/README.md`
- `knowledge/QUICK_START_FOR_AGENTS.md`
- `knowledge/PROJECT_MAP/QUICK_START_FOR_AGENTS.md`
- relevant files under `knowledge/PROJECT_MAP/CODE_INDEX/`
- relevant architecture files under `knowledge/PROJECT_MAP/`

## Audit Checklist

- Code/config/systemd/dataplane/API/boot changes update affected knowledge files.
- Source docs and the owner-local decisions tree stay synchronized where the change affects both audiences.
- Generated code-index cards stay concise and point agents to the real source file.
- ADRs record durable architecture decisions, not transient debugging notes.
- Secrets, runtime state, logs, DB files, caches, and AI-local metadata stay out of git.

## Known Split

The in-repo knowledge base is English. The owner-local decisions tree outside the repo can keep non-English operator notes.
