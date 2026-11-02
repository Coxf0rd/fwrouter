"""Fresh-process backend import contracts under the existing safety bootstrap."""
from __future__ import annotations

import ast
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
    import threading

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
    before_threads = {thread.ident for thread in threading.enumerate()}
    db_path = Settings().paths.db_path
    assert not db_path.exists()
    for index, name in enumerate(names):
        importlib.import_module('fwrouter_api.services.' + name)
        if index == 0 and name == 'mihomo_reconcile':
            assert 'fwrouter_api.services.mihomo_config' not in sys.modules
        if index == 0 and name == 'apply_orchestrator_public':
            assert 'fwrouter_api.services.apply_orchestrator' not in sys.modules

    assert {thread.ident for thread in threading.enumerate()} == before_threads
    assert not db_path.exists()

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
    else:
        from fwrouter_api.services import apply_orchestrator as facade
        from fwrouter_api.services import apply_orchestrator_public as implementation
        from fwrouter_api.jobs import manager as manager_owner
        for name in (
            'ApplyOrchestrator', 'set_global_mode', 'apply_global_mode_immediately',
            'submit_apply_mutation', 'run_apply_mutation',
        ):
            assert getattr(facade, name) is getattr(implementation, name), name
        class Manager:
            def __init__(self):
                self.started = []
            def create(self, job_type, **kwargs):
                assert job_type == facade.JOB_TYPE_APPLY_MUTATION
                assert kwargs['input_data'] == {'intent': facade.INTENT_SET_GLOBAL_MODE, 'payload': {'mode': 'direct'}}
                return {'job_id': 'cold-import-contract'}
            def start_job(self, job_id):
                self.started.append(job_id)
                return {'job_id': job_id, 'status': 'queued'}
        manager = Manager()
        previous = facade.get_default_job_manager
        owner_factory = manager_owner.get_default_job_manager
        try:
            facade.get_default_job_manager = lambda: manager
            job = implementation.submit_apply_mutation(
                intent=facade.INTENT_SET_GLOBAL_MODE, payload={'mode': 'direct'}, run_now=False,
            )
            assert job == {'job_id': 'cold-import-contract', 'status': 'queued'}
            assert manager.started == ['cold-import-contract']
            assert manager_owner.get_default_job_manager is owner_factory
        finally:
            facade.get_default_job_manager = previous
        assert implementation.get_default_job_manager is previous
        assert not db_path.exists()
    print(json.dumps({'status': 'passed', 'order': names}))
""")


@pytest.mark.parametrize("order", [
    ("mihomo_config", "mihomo_reconcile"),
    ("mihomo_reconcile", "mihomo_config"),
    ("apply_orchestrator", "apply_orchestrator_public"),
    ("apply_orchestrator_public", "apply_orchestrator"),
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


def test_mihomo_implementation_has_no_facade_or_operation_view_dependency():
    services = ROOT / "backend/fwrouter_api/services"
    for name in ("mihomo_config_builder", "mihomo_config_validation", "mihomo_reconcile"):
        tree = ast.parse((services / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "fwrouter_api.services.mihomo_config", name
                if node.module in {None, "fwrouter_api.services"}:
                    assert all(alias.name != "mihomo_config" for alias in node.names), name
            if isinstance(node, ast.Import):
                assert all(alias.name != "fwrouter_api.services.mihomo_config"
                           for alias in node.names), name
        for function in (node for node in tree.body if isinstance(node, ast.FunctionDef)):
            assert not any(isinstance(node, ast.Attribute)
                           and isinstance(node.value, ast.Name) and node.value.id == "config"
                           for node in ast.walk(function)), (name, function.name)


_XRAY_LEAF_CHILD = textwrap.dedent("""\
    import importlib
    import importlib.util
    import json
    from pathlib import Path
    import sys
    import threading

    spec = importlib.util.spec_from_file_location(
        '_xray_leaf_contract_bootstrap', Path('tests/_isolation_bootstrap.py'),
    )
    bootstrap = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bootstrap
    spec.loader.exec_module(bootstrap)
    bootstrap.configure_test_process()
    from fwrouter_api.core.config import Settings
    Settings.model_config['env_file'] = None
    db_path = Settings().paths.db_path
    before_threads = {thread.ident for thread in threading.enumerate()}
    assert not db_path.exists()
    prefix = 'fwrouter_api.services.'
    leaf_first = json.loads(sys.argv[1])
    if not leaf_first:
        importlib.import_module(prefix + 'xray_subscription_service')
    leaves = {name: importlib.import_module(prefix + name) for name in (
        'xray_admission_config_proof', 'xray_generation_checkpoint',
        'xray_generation_fingerprint',
    )}
    if leaf_first:
        assert prefix + 'xray_subscription_service' not in sys.modules
    assert not db_path.exists()
    assert {thread.ident for thread in threading.enumerate()} == before_threads
    owner = importlib.import_module(prefix + 'xray_subscription_service')
    for name in ('_legacy_xray_candidate_matches_active', '_legacy_mihomo_candidate_matches'):
        assert getattr(owner, name) is getattr(leaves['xray_admission_config_proof'], name)
    for name in (
        '_xray_generation_checkpoint_path', '_legacy_admission_checkpoint_payload',
        '_checkpoint_operation_mode', '_checkpoint_selection_for_recovery',
        '_AdmissionFenceView',
    ):
        assert getattr(owner, name) is getattr(leaves['xray_generation_checkpoint'], name)
    assert owner._identity_projection_reference_sha256 is leaves[
        'xray_generation_fingerprint'].identity_projection_reference_sha256
    assert not db_path.exists()
    assert {thread.ident for thread in threading.enumerate()} == before_threads
    print(json.dumps({'status': 'passed', 'leaf_first': leaf_first}))
""")


@pytest.mark.parametrize("leaf_first", [True, False])
def test_xray_read_contract_imports_and_compatibility_identity(tmp_path, leaf_first):
    suite, _reports = _GATE.create_suite_root(tmp_path, "xray-read-contract")
    env = _GATE.clean_test_environment(suite)
    env["PYTHONPATH"] = str(ROOT / "backend")
    env["FWROUTER_PYTEST_COORDINATOR_ROOT"] = str(suite)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("FWROUTER_GATE_NODE_REPORT", None)
    result = subprocess.run(
        [sys.executable, "-B", "-c", _XRAY_LEAF_CHILD, json.dumps(leaf_first)],
        cwd=ROOT / "backend", env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr[-4096:]
    assert json.loads(result.stdout) == {"status": "passed", "leaf_first": leaf_first}


def test_xray_read_contracts_have_no_reverse_coordinator_import():
    services = ROOT / "backend/fwrouter_api/services"
    for name in (
        "xray_admission_config_proof", "xray_generation_checkpoint",
        "xray_generation_fingerprint",
    ):
        tree = ast.parse((services / f"{name}.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any("xray_subscription_service" in alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert "xray_subscription_service" not in (node.module or "")
                assert not any("xray_subscription_service" in alias.name for alias in node.names)


_XRAY_D2_IMPORT_CHILD = textwrap.dedent("""\
    import importlib
    import importlib.util
    import json
    from pathlib import Path
    import sys
    import threading

    spec = importlib.util.spec_from_file_location(
        '_xray_d2_contract_bootstrap', Path('tests/_isolation_bootstrap.py'),
    )
    bootstrap = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bootstrap
    spec.loader.exec_module(bootstrap)
    bootstrap.configure_test_process()
    from fwrouter_api.core.config import Settings
    Settings.model_config['env_file'] = None
    db_path = Settings().paths.db_path
    before_threads = {thread.ident for thread in threading.enumerate()}
    assert not db_path.exists()

    prefix = 'fwrouter_api.services.'
    order = json.loads(sys.argv[1])
    expected = {
        'xray_profile_planning', 'xray_generation_observation',
        'xray_generation_views', 'xray_generation_staging',
    }
    assert set(order) == expected
    for name in order:
        assert prefix + 'xray_subscription_service' not in sys.modules
        importlib.import_module(prefix + name)

    assert prefix + 'xray_subscription_service' not in sys.modules
    owner = importlib.import_module(prefix + 'xray_subscription_service')
    modules = {
        name: importlib.import_module(prefix + name)
        for name in expected
    }
    assert owner._prospective_profile_bindings is modules[
        'xray_profile_planning'
    ].prepare_profile_bindings
    assert owner._capture_generation_auto_selection is modules[
        'xray_generation_observation'
    ].capture_generation_selection
    assert owner._current_xray_projection_snapshot is modules[
        'xray_generation_views'
    ].capture_current_projection
    assert owner._stage_profile_native_candidates is modules[
        'xray_generation_staging'
    ].prepare_native_candidates
    assert not db_path.exists()
    assert {thread.ident for thread in threading.enumerate()} == before_threads
    print(json.dumps({'status': 'passed', 'order': order}))
""")


@pytest.mark.parametrize("order", [
    (
        "xray_profile_planning", "xray_generation_observation",
        "xray_generation_views", "xray_generation_staging",
    ),
    (
        "xray_generation_staging", "xray_generation_views",
        "xray_generation_observation", "xray_profile_planning",
    ),
])
def test_xray_d2_components_cold_import_in_both_orders(tmp_path, order):
    suite, _reports = _GATE.create_suite_root(tmp_path, "xray-d2-import-contract")
    env = _GATE.clean_test_environment(suite)
    env["PYTHONPATH"] = str(ROOT / "backend")
    env["FWROUTER_PYTEST_COORDINATOR_ROOT"] = str(suite)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("FWROUTER_GATE_NODE_REPORT", None)
    result = subprocess.run(
        [sys.executable, "-B", "-c", _XRAY_D2_IMPORT_CHILD, json.dumps(order)],
        cwd=ROOT / "backend", env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr[-4096:]
    assert json.loads(result.stdout) == {"status": "passed", "order": list(order)}
