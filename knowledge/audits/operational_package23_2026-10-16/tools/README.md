# Synthetic reproduction

Use Python3.11.2 and project test dependencies in an isolated environment. Inspect runners before execution: paths are private synthetic state only. Freeze baseline using `git show 3f96e1e:backend/fwrouter_api/services/live_probe_cache.py` and the equivalent db/connection.py. Cache runner selects frozen module via CACHE_MODULE_PATH; candidate imports source backend. Run with empty inherited environment, explicit PYTHONPATH and no production settings. Separate five untraced repeat sets from tracemalloc diagnostics. Permission runner compares frozen module and temporary conditional-permission pilot recorded in its receipts; no pilot source is shipped. Never point these tools at live DB,configs,backups,providers or credentials.

Artifacts are curated synthetic numerical receipts only. No production state or secret fixture is included. Absolute private paths identify original measurement provenance, not requirements to use a production interpreter or data.
