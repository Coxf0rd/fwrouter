"""Fresh-process backend import contracts under the existing safety bootstrap."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[2]
_GATE_SPEC = importlib.util.spec_from_file_location(
    "_backend_import_contract_gate", ROOT / "tests/gates/gate.py",
)
assert _GATE_SPEC is not None and _GATE_SPEC.loader is not None
_GATE = importlib.util.module_from_spec(_GATE_SPEC)
_GATE_SPEC.loader.exec_module(_GATE)

_CHILD = textwrap.dedent("""\
    import importlib
    import importlib.util
    import json
    from pathlib import Path
    import sys

    # Production paths, processes and network are denied before app imports.
    spec = importlib.util.spec_from_file_location(
        '_backend_import_contract_bootstrap', Path('tests/_isolation_bootstrap.py'),
    )
    bootstrap = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bootstrap
    spec.loader.exec_module(bootstrap)
    bootstrap.configure_test_process()
    from fwrouter_api.core.config import Settings
    Settings.model_config['env_file'] = None

    names = json.loads(sys.argv[1])
    for index, name in enumerate(names):
        importlib.import_module('fwrouter_api.services.' + name)
        if index == 0 and name == 'mihomo_reconcile':
            assert 'fwrouter_api.services.mihomo_config' not in sys.modules

    if names[0].startswith('mihomo_'):
        from fwrouter_api.services import mihomo_config as facade
        from fwrouter_api.services import mihomo_reconcile as implementation
        for name in (
            'promote_mihomo_candidate_config', 'reconcile_mihomo_runtime',
            'reconcile_mihomo_selective_default_fast',
            'validate_and_promote_mihomo_candidate_config',
        ):
            assert getattr(facade, name) is getattr(implementation, name), name
        fallback = facade.reconcile_mihomo_selective_default_fast(job_id='import-contract')
        assert fallback['ok'] is False
        assert fallback['fallback_required'] is True
        assert fallback['reconcile_action'] == 'none'
    print(json.dumps({'status': 'passed', 'order': names}))
""")


@pytest.mark.parametrize("order", [
    ("mihomo_config", "mihomo_reconcile"),
    ("mihomo_reconcile", "mihomo_config"),
])
def test_fresh_import_orders_and_export_identity(tmp_path, order):
    suite, _reports = _GATE.create_suite_root(tmp_path, "backend-import-contract")
    env = _GATE.clean_test_environment(suite)
    env["PYTHONPATH"] = str(ROOT / "backend")
    env["FWROUTER_PYTEST_COORDINATOR_ROOT"] = str(suite)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("FWROUTER_GATE_NODE_REPORT", None)
    result = subprocess.run(
        [sys.executable, "-B", "-c", _CHILD, json.dumps(order)],
        cwd=ROOT / "backend", env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr[-4096:]
    assert json.loads(result.stdout) == {"status": "passed", "order": list(order)}
