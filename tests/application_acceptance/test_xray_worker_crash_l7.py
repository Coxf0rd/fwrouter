from __future__ import annotations

import json
import os
import signal
import threading
import time
from queue import Queue

import pytest

from .http_support import http_json
from .xray_support import (
    assert_mihomo_launch_matches_active, assert_native_matches_active, await_job, loaded_identities, read_subscription_rows,
)


@pytest.mark.l7
def test_api_worker_sigkill_during_native_reload_preserves_owned_process_state(acceptance_stack):
    """Kill the actual uvicorn worker at an apply barrier; no checkpoint recovery is claimed."""
    stack = acceptance_stack
    worker = stack["worker"]
    stack["native"].hold_next_reload()
    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "phase-c-kill-barrier", "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and response.get("ok") is True, response
    assert stack["native"].reload_entered.wait(20), "actual Xray reload did not reach the bounded pause barrier"
    try:
        os.kill(worker.pid, signal.SIGKILL)
        worker.wait(timeout=5)
        assert worker.returncode == -signal.SIGKILL
    finally:
        stack["native"].release_reload()

    deadline = time.monotonic() + 12
    loaded: list[tuple[str, str]] = []
    active_pairs: list[tuple[str, str]] = []
    poll_wait = threading.Event()
    while time.monotonic() < deadline:
        try:
            loaded = loaded_identities(stack["native"])
            active = json.loads((stack["state"] / "xray" / "config.json").read_text(encoding="utf-8"))
            active_clients = active["inbounds"][1]["settings"]["clients"]
            active_pairs = sorted((str(item.get("id") or ""), str(item.get("email") or "")) for item in active_clients)
            if loaded == active_pairs:
                break
        except Exception:
            poll_wait.wait(0.1)
    assert loaded == active_pairs and loaded, {"loaded": loaded, "active": active_pairs}

    restarted_worker, restarted_api = stack["start_worker"]()
    stack["worker"], stack["api"] = restarted_worker, restarted_api
    code, listing = http_json(f"{restarted_api}/xray/clients")
    assert code == 200 and listing.get("ok") is True, listing
    listed_pairs = sorted((str(item.get("client_id") or ""), str(item.get("email") or ""))
                          for item in listing.get("data", {}).get("clients", []))
    assert set(active_pairs).issubset(set(listed_pairs)), {"active": active_pairs, "listed": listed_pairs}


@pytest.mark.parametrize(
    "phase",
    ["prepared", "transition_applied", "xray_applied", "runtime_applied",
     "inventory_synced", "bindings_written", "projections_cleaned", "selection_verified"],
    ids=["checkpoint-prepared", "mihomo-transition-applied", "xray-applied",
         "runtime-applied", "inventory-persisted", "bindings-persisted", "projections-cleaned",
         "selection-verified"],
)
@pytest.mark.l7
def test_worker_sigkill_at_generated_checkpoint_phase_recovers_profile_and_native_parity(acceptance_stack, phase):
    """Crash after the real atomic checkpoint write at a named generation phase."""
    stack = acceptance_stack
    token = "phase-c-crash-profile@acceptance.invalid"
    checkpoint = stack["state"] / "xray" / ".generation" / "generation-checkpoint.json"
    active = stack["state"] / "xray" / "config.json"
    from .test_core_provider_mihomo import (
        _configure_provider, _enable_owned_mihomo_runtime_state, _seed_source,
    )
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _configure_provider(stack["api"], source_ref)
    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "phase-c-crash-profile", "email": token,
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and response.get("ok") is True, response
    seeded = await_job(stack["api"], response)
    assert seeded.get("status") == "success", seeded
    stack["native"].hold_checkpoint_phase(phase)
    # Enabling the synthetic, real HTTP-backed provider enters the ordinary
    # subscription pipeline, including Core selection and its genuine
    # prepublication verification callback.
    code, enable = http_json(
        f"{stack['api']}/subscription/sources/{source_ref}/provider",
        method="POST", payload={"action": "enable"}, timeout=15,
    )
    assert code == 200 and enable.get("ok") is True and enable["data"].get("accepted") is True, enable
    assert stack["native"].checkpoint_entered.wait(25), f"generation did not durably reach checkpoint phase {phase}"
    assert checkpoint.is_file(), "generation checkpoint was not durable before native Xray reload"
    checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert checkpoint_data.get("phase") == phase, checkpoint_data

    worker = stack["worker"]
    try:
        os.kill(worker.pid, signal.SIGKILL)
        worker.wait(timeout=5)
        assert worker.returncode == -signal.SIGKILL
    finally:
        stack["native"].release_checkpoint()

    # The parent releases the worker's post-write checkpoint barrier. A new API
    # worker then invokes the normal profile reconcile path, which consumes the
    # real checkpoint and performs bounded recovery.
    started = time.monotonic()
    while time.monotonic() - started < 12:
        try:
            identities = loaded_identities(stack["native"])
            if any(email == token for _, email in identities):
                break
        except Exception:
            threading.Event().wait(0.1)
    restarted_worker, restarted_api = stack["start_worker"]()
    stack["worker"], stack["api"] = restarted_worker, restarted_api
    recovery_code, recovery = http_json(
        f"{restarted_api.rsplit('/api/v2', 1)[0]}/api/v2/__acceptance/xray/reconcile",
        method="POST", payload={"token": token}, timeout=45,
    )
    assert recovery_code == 200 and recovery.get("ok") is True, recovery
    assert not checkpoint.exists(), "successful crash recovery must durably consume the generation checkpoint"
    from .xray_support import assert_native_matches_active
    assert_native_matches_active(stack["native"], active)
    assert_mihomo_launch_matches_active(stack["native"], stack["state"] / "generated" / "mihomo" / "config.yaml")
    assert any(email == token for _, email in loaded_identities(stack["native"]))

    from .xray_support import assert_applied_bindings, read_subscription_rows
    rows = read_subscription_rows(stack["state"], token)
    assert len(rows["accounts"]) == 1 and rows["accounts"][0]["enabled"] == 1, rows
    assert len(rows["clients"]) == 1 and rows["clients"][0]["enabled"] == 1, rows
    assert rows["snapshots"] and rows["snapshots"][0]["runtime_verified_at"], rows
    persisted_nodes = json.loads(rows["snapshots"][0]["nodes_json"])
    generated_pairs = {
        (str(node.get("client_uuid") or ""), str(node.get("client_email") or ""))
        for node in persisted_nodes if isinstance(node, dict)
    }
    assert generated_pairs and generated_pairs.issubset(set(loaded_identities(stack["native"]))), rows
    assert_applied_bindings(stack["state"], generated_pairs)
    first_recovered_config = active.read_bytes()
    first_recovered_reload_count = stack["native"].reload_calls
    repeated_code, repeated = http_json(
        f"{restarted_api.rsplit('/api/v2', 1)[0]}/api/v2/__acceptance/xray/reconcile",
        method="POST", payload={"token": token}, timeout=45,
    )
    assert repeated_code == 200 and repeated.get("ok") is True, repeated
    assert active.read_bytes() == first_recovered_config, "repeated reconciliation changed committed generation bytes"
    assert stack["native"].reload_calls == first_recovered_reload_count, "repeated reconciliation reloaded an unchanged generation"


@pytest.mark.l7
def test_generation_recovery_declines_checkpoint_after_newer_core_routing_intent(acceptance_stack):
    """A newer public Core routing intent changes the generation source fence."""
    stack = acceptance_stack
    from .test_core_provider_mihomo import (
        _configure_provider, _enable_owned_mihomo_runtime_state, _seed_source,
    )
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _configure_provider(stack["api"], source_ref)
    token = "phase-c-stale-checkpoint@acceptance.invalid"
    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "phase-c-stale-checkpoint", "email": token,
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and response.get("ok") is True, response
    seeded = await_job(stack["api"], response)
    assert seeded.get("status") == "success", seeded

    checkpoint = stack["state"] / "xray" / ".generation" / "generation-checkpoint.json"
    stack["native"].hold_checkpoint_phase("prepared")
    outcome: Queue = Queue(maxsize=1)

    def enable_provider() -> None:
        try:
            outcome.put(http_json(
                f"{stack['api']}/subscription/sources/{source_ref}/provider",
                method="POST", payload={"action": "enable"}, timeout=45,
            ))
        except BaseException as exc:
            outcome.put(exc)

    caller = threading.Thread(target=enable_provider, daemon=True)
    caller.start()
    assert stack["native"].checkpoint_entered.wait(25), "real provider generation did not durably reach prepared checkpoint"
    before_fingerprint = json.loads(checkpoint.read_text(encoding="utf-8"))["source_fingerprint"]

    # Change Core-owned routing intent via its public API while generation is
    # stopped after its durable checkpoint. Recovery must see the new source
    # fingerprint and must not roll the newer intent back.
    mode_code, mode_response = http_json(
        f"{stack['api']}/routing/global", method="POST",
        payload={"mode": "direct", "requested_by": "hosted-acceptance", "run_now": True}, timeout=15,
    )
    assert mode_code == 200 and mode_response.get("ok") is True, mode_response
    mode_job = await_job(stack["api"], mode_response)
    assert mode_job.get("status") == "success", mode_job
    route_code, route_state = http_json(f"{stack['api']}/routing/global")
    assert route_code == 200 and route_state.get("ok") is True, route_state
    routing = (route_state.get("data") or {}).get("routing") or {}
    assert routing.get("desired_mode") == "direct", routing
    after_fingerprint = json.loads(checkpoint.read_text(encoding="utf-8"))["source_fingerprint"]
    assert before_fingerprint == after_fingerprint, "durable checkpoint should retain the old source fingerprint"

    worker = stack["worker"]
    try:
        os.kill(worker.pid, signal.SIGKILL)
        worker.wait(timeout=5)
        assert worker.returncode == -signal.SIGKILL
    finally:
        stack["native"].release_checkpoint()
    caller.join(timeout=5)
    assert not caller.is_alive(), "provider generation caller remained blocked after worker SIGKILL"

    restarted_worker, restarted_api = stack["start_worker"]()
    stack["worker"], stack["api"] = restarted_worker, restarted_api
    recovery_code, recovery = http_json(
        f"{restarted_api.rsplit('/api/v2', 1)[0]}/api/v2/__acceptance/xray/reconcile",
        method="POST", payload={"token": token}, timeout=45,
    )
    assert recovery_code == 200, recovery
    assert recovery.get("ok") is False, "stale checkpoint recovery must not claim success"
    recovery_details = recovery.get("details") if isinstance(recovery.get("details"), dict) else {}
    assert recovery_details.get("reason") == "generation_source_changed", recovery
    route_code, final_state = http_json(f"{restarted_api}/routing/global")
    assert route_code == 200 and final_state.get("ok") is True, final_state
    final_routing = (final_state.get("data") or {}).get("routing") or {}
    assert final_routing.get("desired_mode") == "direct", final_routing
    assert_native_matches_active(stack["native"], stack["state"] / "xray" / "config.json")
    assert_mihomo_launch_matches_active(stack["native"], stack["state"] / "generated" / "mihomo" / "config.yaml")


@pytest.mark.l7
def test_recovery_does_not_resurrect_profile_deleted_after_generation_checkpoint(acceptance_stack):
    """A canonical Xray delete after crash changes source rows and remains authoritative."""
    stack = acceptance_stack
    from .test_core_provider_mihomo import (
        _configure_provider, _enable_owned_mihomo_runtime_state, _seed_source,
    )
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _configure_provider(stack["api"], source_ref)
    token = "phase-c-delete-newer-intent@acceptance.invalid"
    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "delete-newer-intent", "email": token,
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and response.get("ok") is True, response
    created = await_job(stack["api"], response)
    assert created.get("status") == "success", created
    client_id = str((((created.get("result") or {}).get("xray_client") or {}).get("client") or {}).get("client_id") or "")
    assert client_id

    checkpoint = stack["state"] / "xray" / ".generation" / "generation-checkpoint.json"
    stack["native"].hold_checkpoint_phase("prepared")
    outcome: Queue = Queue(maxsize=1)

    def enable_provider() -> None:
        try:
            outcome.put(http_json(
                f"{stack['api']}/subscription/sources/{source_ref}/provider",
                method="POST", payload={"action": "enable"}, timeout=45,
            ))
        except BaseException as exc:
            outcome.put(exc)

    caller = threading.Thread(target=enable_provider, daemon=True)
    caller.start()
    assert stack["native"].checkpoint_entered.wait(25), "real provider generation did not durably reach prepared checkpoint"
    checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
    old_source_fingerprint = checkpoint_data.get("source_fingerprint")
    assert checkpoint_data.get("phase") == "prepared" and old_source_fingerprint, checkpoint_data
    worker = stack["worker"]
    try:
        os.kill(worker.pid, signal.SIGKILL)
        worker.wait(timeout=5)
        assert worker.returncode == -signal.SIGKILL
    finally:
        stack["native"].release_checkpoint()
    caller.join(timeout=5)
    assert not caller.is_alive(), "generation caller remained blocked after worker SIGKILL"

    restarted_worker, restarted_api = stack["start_worker"]()
    stack["worker"], stack["api"] = restarted_worker, restarted_api
    delete_code, deletion = http_json(
        f"{restarted_api}/xray/clients/{client_id}", method="DELETE",
        payload={"requested_by": "hosted-acceptance"}, timeout=15,
    )
    assert delete_code == 200 and deletion.get("ok") is True, deletion
    deleted_job = await_job(restarted_api, deletion)
    assert deleted_job.get("status") == "success", deleted_job
    rows = read_subscription_rows(stack["state"], token)
    assert all(email != token for _client_uuid, email in loaded_identities(stack["native"]))
    deleted_active = (stack["state"] / "xray" / "config.json").read_bytes()

    recovery_code, recovery = http_json(
        f"{restarted_api.rsplit('/api/v2', 1)[0]}/api/v2/__acceptance/xray/reconcile",
        method="POST", payload={"token": token}, timeout=45,
    )
    assert recovery_code == 200, recovery
    if recovery.get("ok") is True:
        assert not checkpoint.exists(), recovery
    else:
        details = recovery.get("details") if isinstance(recovery.get("details"), dict) else {}
        assert recovery.get("stage") == "generation_recovery", recovery
        assert recovery.get("error_code") == "XRAY_GENERATION_RECOVERY_FAILED", recovery
        assert details.get("reason") == "generation_source_changed", recovery
    assert (stack["state"] / "xray" / "config.json").read_bytes() == deleted_active
    assert all(email != token for _client_uuid, email in loaded_identities(stack["native"]))
    assert_native_matches_active(stack["native"], stack["state"] / "xray" / "config.json")
    assert_mihomo_launch_matches_active(stack["native"], stack["state"] / "generated" / "mihomo" / "config.yaml")
