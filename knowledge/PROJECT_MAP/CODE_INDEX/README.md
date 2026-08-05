# Code Index

This directory contains generated code-index cards for the FWRouter source and live deployment surfaces.

Use these cards as navigation aids only. Before changing behavior, read the real source file, the relevant architecture document, and the matching tests.

Regeneration rules:

- Keep entries in English.
- Keep cards concise and operationally useful.
- Update a card when the file responsibility, runtime side effects, boot relevance, or risk profile changes.
- Do not store secrets, runtime state, logs, or local AI scratch data here.
- `opt_fwrouter_api_tests_conftest_py.md` documents pytest isolation from live dataplane/runtime state.
