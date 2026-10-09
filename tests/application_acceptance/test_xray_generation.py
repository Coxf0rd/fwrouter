from __future__ import annotations

import json
import threading
from queue import Queue

from .http_support import http_json
from .xray_support import (
    active_identities,
    assert_mihomo_launch_matches_active,
    assert_native_matches_active,
    await_job,
    loaded_identities,
    read_subscription_rows,
    assert_applied_bindings,
)


def test_email_client_generation_persists_and_matches_native_after_worker_restart(acceptance_stack):
    """Email client creation must persist profile intent and converge generated/native state."""
    stack = acceptance_stack
    token = "phase-c-profile@acceptance.invalid"
    active_path = stack["state"] / "xray" / "config.json"
    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "phase-c-profile", "email": token, "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and response.get("ok") is True, response
    job = await_job(stack["api"], response)
    assert job.get("status") == "success", job
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    client = (result.get("xray_client") or {}).get("client") or {}
    client_id = str(client.get("client_id") or "")
    assert client_id and client.get("email") == token, job

    rows = read_subscription_rows(stack["state"], token)
    assert len(rows["accounts"]) == 1 and rows["accounts"][0]["enabled"] == 1, rows
    assert len(rows["clients"]) == 1 and rows["clients"][0]["enabled"] == 1, rows
    assert rows["snapshots"] and rows["snapshots"][0]["runtime_verified_at"], rows
    persisted_nodes = json.loads(rows["snapshots"][0]["nodes_json"])
    assert isinstance(persisted_nodes, list) and persisted_nodes, rows
    generated_pairs = {
        (str(node.get("client_uuid") or ""), str(node.get("client_email") or ""))
        for node in persisted_nodes if isinstance(node, dict)
    }
    assert generated_pairs and generated_pairs.issubset(set(active_identities(active_path))), {
        "generated": sorted(generated_pairs), "active": active_identities(active_path),
    }
    assert generated_pairs.issubset(set(loaded_identities(stack["native"])))
    assert_applied_bindings(stack["state"], generated_pairs)
    assert_native_matches_active(stack["native"], active_path)
    assert_mihomo_launch_matches_active(stack["native"], stack["state"] / "generated" / "mihomo" / "config.yaml")

    # A repeated API request resolves the same email identity and must not queue
    # a second create or change the generated/native generation.
    before = active_path.read_bytes()
    reload_count_before_repeat = stack["native"].reload_calls
    again_code, again = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "phase-c-profile", "email": token, "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert again_code == 200 and again.get("ok") is True, again
    assert (again.get("data", {}).get("xray_client") or {}).get("stage") == "existing", again
    assert active_path.read_bytes() == before
    assert stack["native"].reload_calls == reload_count_before_repeat

    stack["worker"].terminate()
    stack["worker"].wait(timeout=5)
    restarted_worker, restarted_api = stack["start_worker"]()
    stack["worker"], stack["api"] = restarted_worker, restarted_api
    reload_code, reload_response = http_json(
        f"{restarted_api}/xray/reload", method="POST", payload={"requested_by": "hosted-acceptance"},
    )
    assert reload_code == 200 and reload_response.get("ok") is True, reload_response
    assert_native_matches_active(stack["native"], active_path)


def test_email_generation_reload_failure_keeps_desired_candidate_and_last_good_loaded_identity(acceptance_stack):
    """A reload fault remains failed while desired bytes and loaded bytes stay distinguishable."""
    stack = acceptance_stack
    active_path = stack["state"] / "xray" / "config.json"
    before = active_path.read_bytes()
    from .xray_support import loaded_identities
    before_loaded = loaded_identities(stack["native"])
    stack["native"].fail_next_reload()
    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "phase-c-reload-failure", "email": "reload-failure@acceptance.invalid",
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    job_ref = response.get("data", {}).get("job") if isinstance(response.get("data"), dict) else None
    assert code == 200 and isinstance(job_ref, dict) and bool(str(job_ref.get("job_id") or "")), response
    job = await_job(stack["api"], response)
    assert job.get("status") == "failed", job
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    client_result = result.get("xray_client") if isinstance(result.get("xray_client"), dict) else {}
    assert client_result.get("ok") is False, job
    desired = json.loads(active_path.read_text(encoding="utf-8"))
    desired_clients = next(item for item in desired["inbounds"] if item.get("tag") == "vless-ws")["settings"]["clients"]
    assert any(item.get("email") == "reload-failure@acceptance.invalid" for item in desired_clients), {
        "before_sha256": __import__("hashlib").sha256(before).hexdigest(),
        "desired_clients": desired_clients,
    }
    assert loaded_identities(stack["native"]) == before_loaded, "failed generation changed actual HandlerService identities"
    assert (token_uuid(job), "reload-failure@acceptance.invalid") not in set(loaded_identities(stack["native"])), job


def test_xray_generation_fence_rejects_replaced_native_incarnation(acceptance_stack):
    """A process replacement after a durable generation fence cannot pass publication."""
    stack = acceptance_stack
    native = stack["native"]
    from .test_core_provider_mihomo import (
        _configure_provider, _enable_owned_mihomo_runtime_state, _seed_source,
    )
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _configure_provider(stack["api"], source_ref)
    token = "incarnation-checkpoint@acceptance.invalid"
    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "phase-c-incarnation-fence", "email": token,
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and response.get("ok") is True, response
    seeded = await_job(stack["api"], response)
    assert seeded.get("status") == "success", seeded
    checkpoint = stack["state"] / "xray" / ".generation" / "generation-checkpoint.json"
    native.hold_checkpoint_phase("xray_applied")
    enable_code, enable = http_json(
        f"{stack['api']}/subscription/sources/{source_ref}/provider",
        method="POST", payload={"action": "enable"}, timeout=15,
    )
    assert enable_code == 200 and enable.get("ok") is True and enable["data"].get("accepted") is True, enable
    assert native.checkpoint_entered.wait(25), "generation did not durably reach xray_applied checkpoint"
    checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
    expected_incarnation = str(checkpoint_data.get("xray_runtime_incarnation_after") or "")
    assert checkpoint_data.get("phase") == "xray_applied" and expected_incarnation, checkpoint_data
    prior_pid = native.process.pid if native.process is not None else None
    try:
        native.restart_xray_child()
        assert native.process is not None and native.process.pid != prior_pid
        current_incarnation = native.rpc("runtime_inspect", {"container_id": f"{native.process.pid:x}"})
        assert current_incarnation.get("ok") is True, current_incarnation
        observed_incarnation = str((current_incarnation.get("details") or {}).get("stdout") or "")
        assert observed_incarnation and observed_incarnation != expected_incarnation
    finally:
        native.release_checkpoint()
    job = await_job(stack["api"], enable)
    assert job.get("status") == "failed", job
    assert "XRAY_GENERATION_STALE_BEFORE_PUBLICATION" in json.dumps(job), job
    assert_native_matches_active(native, stack["state"] / "xray" / "config.json")
    assert_mihomo_launch_matches_active(native, stack["state"] / "generated" / "mihomo" / "config.yaml")


def test_xray_create_job_cannot_succeed_before_actual_native_readback_completes(acceptance_stack):
    """Hold the real HandlerService readback and inspect the still-running API job."""
    stack = acceptance_stack
    native = stack["native"]
    token = "readback-barrier@acceptance.invalid"
    native.hold_action("api_inbound_users")
    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "readback-barrier", "email": token,
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and response.get("ok") is True, response
    assert native.action_entered.wait(25), "create job did not reach actual HandlerService readback"
    job_id = str(((response.get("data") or {}).get("job") or {}).get("job_id") or "")
    assert job_id, response
    try:
        pending_code, pending = http_json(f"{stack['api']}/jobs/{job_id}")
        assert pending_code == 200 and pending.get("ok") is True, pending
        pending_job = (pending.get("data") or {}).get("job") or {}
        assert pending_job.get("status") not in {"success", "failed", "cancelled"}, pending_job
        duplicate_code, duplicate = http_json(
            f"{stack['api']}/xray/clients", method="POST",
            payload={"alias": "readback-barrier", "email": token,
                     "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
        )
        assert duplicate_code == 200 and duplicate.get("ok") is True, duplicate
        duplicate_job = (duplicate.get("data") or {}).get("job") or {}
        assert duplicate_job.get("job_id") == job_id, duplicate
    finally:
        native.release_action()
    job = await_job(stack["api"], response)
    assert job.get("status") == "success", job
    assert (token_uuid(job), token) in set(loaded_identities(native))
    assert_native_matches_active(native, stack["state"] / "xray" / "config.json")
    assert_mihomo_launch_matches_active(native, stack["state"] / "generated" / "mihomo" / "config.yaml")


def test_xray_create_job_fails_on_native_readback_transport_error(acceptance_stack):
    """A real HandlerService readback transport error cannot be reported as success."""
    stack = acceptance_stack
    native = stack["native"]
    native.fail_next_action("api_inbound_users")
    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "readback-error", "email": "readback-error@acceptance.invalid",
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and response.get("ok") is True, response
    job = await_job(stack["api"], response)
    assert job.get("status") == "failed", job
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    client_result = result.get("xray_client") if isinstance(result.get("xray_client"), dict) else {}
    assert client_result.get("ok") is False, job
    assert_native_matches_active(native, stack["state"] / "xray" / "config.json")
    assert_mihomo_launch_matches_active(native, stack["state"] / "generated" / "mihomo" / "config.yaml")


def test_real_core_selection_cas_miss_reconciles_after_native_readback_without_provider_retry(acceptance_stack):
    """Advance Core's real fence after a real runtime switch and verify fresh reconciliation."""
    stack = acceptance_stack
    api, native, bridge = stack["api"], stack["native"], stack["provider_bridge"]
    from .test_core_provider_mihomo import (
        _configure_provider, _enable_owned_mihomo_runtime_state, _seed_source,
    )
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _configure_provider(api, source_ref)
    token = "selection-cas-miss@acceptance.invalid"
    code, created = http_json(
        f"{api}/xray/clients", method="POST",
        payload={"alias": "selection-cas-miss", "email": token,
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and created.get("ok") is True, created
    assert await_job(api, created).get("status") == "success"

    code, accepted = http_json(
        f"{api}/subscription/sources/{source_ref}/provider", method="POST",
        payload={"action": "enable"}, timeout=15,
    )
    assert code == 200 and accepted.get("ok") is True and accepted["data"].get("accepted") is True, accepted
    enabled = await_job(api, accepted)
    assert enabled.get("status") == "success", enabled

    fence_code, fence_before_fault = http_json(f"{api}/__acceptance/selection/fence")
    assert fence_code == 200 and fence_before_fault.get("ok") is True, fence_before_fault
    direct_code, direct = http_json(
        f"{api}/__acceptance/selection/runtime-direct", method="POST", payload={},
    )
    assert direct_code == 200 and direct.get("ok") is True, direct
    assert direct.get("selector") == "vpn-auto" and direct.get("selected_target") == "DIRECT", direct
    assert direct.get("membership_confirmed") is True and direct.get("readback_confirmed") is True, direct
    fence_code, fence_before_switch = http_json(f"{api}/__acceptance/selection/fence")
    assert fence_code == 200 and fence_before_switch.get("ok") is True, fence_before_switch
    assert fence_before_switch["fence"] == fence_before_fault["fence"], {
        "before_fault": fence_before_fault, "after_runtime_fault": fence_before_switch,
    }

    native.hold_action("selection_commit_barrier")
    switch_result: Queue = Queue(maxsize=1)

    def switch_vpn_auto() -> None:
        try:
            switch_result.put(http_json(
                f"{api}/selector/vpn-auto/switch", method="POST",
                payload={
                    "confirm_switch": True, "exclude_active": False, "update_ping_state": False,
                    "limit": 20, "timeout_ms": 1000, "reason": "acceptance_selection_cas_miss",
                    "requested_by": "external_client",
                    "management_context": {"client_name": "hosted-acceptance",
                                           "action": "selection_cas_miss_reconcile"},
                },
                timeout=90,
            ))
        except BaseException as exc:
            switch_result.put(exc)

    switch_thread = threading.Thread(target=switch_vpn_auto, daemon=True)
    switch_thread.start()
    try:
        assert native.action_entered.wait(30), "real selector did not reach its persistent CAS after native apply/readback"
        commit = native.last_selection_commit
        actual_probe = native.last_mihomo_probe_result
        assert commit and commit.get("server_id"), commit
        assert actual_probe and actual_probe.get("probe_ok") is True, actual_probe
        assert str(actual_probe.get("logical_runtime_target") or ""), actual_probe
        pause_fence = fence_before_switch["fence"]
        assert commit.get("expected_revision") == pause_fence["revision"], {"commit": commit, "fence": pause_fence}

        # This explicit test fault uses the application's monotonic revision helper
        # in its real transaction; it does not invent a selector result or edit the
        # active selection/provenance fields.
        advance_code, advanced = http_json(
            f"{api}/__acceptance/selection/revision/advance", method="POST",
            payload={"expected_revision": commit["expected_revision"]},
        )
        assert advance_code == 200 and advanced.get("ok") is True, advanced
        assert advanced["revision"] == commit["expected_revision"] + 1, advanced
        assert advanced["fence"]["active_server_id"] == pause_fence["active_server_id"], advanced
        assert advanced["fence"]["decision_id"] == pause_fence["decision_id"], advanced
        provider_calls_at_cas = bridge.snapshot_calls()
        native.release_action()
    finally:
        native.release_action()
        switch_thread.join(timeout=95)

    assert not switch_thread.is_alive(), "public Core selector request did not finish after CAS barrier release"
    switched_result = switch_result.get_nowait()
    if isinstance(switched_result, BaseException):
        raise switched_result
    switched_code, switched = switched_result

    assert switched_code == 200 and switched.get("ok") is True, switched
    selector_result = switched.get("data", {}).get("selector", {})
    assert selector_result.get("applied") is True, selector_result
    assert selector_result.get("apply_result", {}).get("ok") is True, selector_result
    assert selector_result.get("active_after_runtime_target") == selector_result.get("selected_runtime_target"), selector_result
    assert selector_result.get("selector_readback_matches") is True, selector_result

    job = switched

    def nested_dicts(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from nested_dicts(child)
        elif isinstance(value, list):
            for child in value:
                yield from nested_dicts(child)

    reconcile_proof = next((item for item in nested_dicts(job)
                            if item.get("reconciled_after_cas_miss") is True
                            and item.get("reconciliation_after_cas_miss", {}).get("outcome") == "repaired"), None)
    assert reconcile_proof is not None, job
    assert reconcile_proof.get("selection_outcome") == "selected", reconcile_proof
    assert reconcile_proof.get("error_code") is None, reconcile_proof
    assert bridge.snapshot_calls() == provider_calls_at_cas, "CAS repair issued a second provider HTTP request"

    fence_code, final_fence = http_json(f"{api}/__acceptance/selection/fence")
    assert fence_code == 200 and final_fence.get("ok") is True, final_fence
    fence = final_fence["fence"]
    assert fence["revision"] == advanced["revision"] + 1, fence
    assert fence["active_server_id"] == commit["server_id"], {"commit": commit, "fence": fence}
    state_code, state = http_json(f"{api}/state/vpn")
    assert state_code == 200 and state.get("ok") is True, state
    actual_active = state["data"]["vpn"]["effective"]["server_health"]["active"]["server_id"]
    assert actual_active == commit["server_id"], {"expected": commit["server_id"], "state": state}
    assert_native_matches_active(native, stack["state"] / "xray" / "config.json")
    assert_mihomo_launch_matches_active(native, stack["state"] / "generated" / "mihomo" / "config.yaml")


def token_uuid(job: dict) -> str:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    client = (result.get("xray_client") or {}).get("client") or {}
    return str(client.get("client_id") or "")


def test_concurrent_xray_create_alias_update_and_delete_preserve_committed_clients(acceptance_stack):
    """Concurrent public CRUD intents serialize through the real Xray writer guard."""
    stack = acceptance_stack
    native = stack["native"]
    first_email = "crud-existing@acceptance.invalid"
    first_code, first_response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "crud-existing", "email": first_email,
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert first_code == 200 and first_response.get("ok") is True, first_response
    first_job = await_job(stack["api"], first_response)
    assert first_job.get("status") == "success", first_job
    first_id = token_uuid(first_job)
    assert first_id

    second_email = "crud-concurrent@acceptance.invalid"
    native.hold_next_reload()
    create_code, create_response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "crud-concurrent", "email": second_email,
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert create_code == 200 and create_response.get("ok") is True, create_response
    assert native.reload_entered.wait(25), "concurrent create did not reach actual Xray reload"
    staged_second_id = next((client_id for client_id, email in active_identities(
        stack["state"] / "xray" / "config.json") if email == second_email), "")
    assert staged_second_id, "active generation did not expose the real pending client identity"

    update_result: Queue = Queue(maxsize=1)

    def update_existing_alias() -> None:
        try:
            update_result.put(http_json(
                f"{stack['api']}/xray/clients/{first_id}", method="PATCH",
                payload={"alias": "crud-existing-updated", "requested_by": "hosted-acceptance"}, timeout=45,
            ))
        except BaseException as exc:
            update_result.put(exc)

    updater = threading.Thread(target=update_existing_alias, daemon=True)
    updater.start()
    delete_code, delete_response = http_json(
        f"{stack['api']}/xray/clients/{staged_second_id}",
        method="DELETE", payload={"requested_by": "hosted-acceptance"}, timeout=15,
    )
    native.release_reload()
    create_job = await_job(stack["api"], create_response)
    assert create_job.get("status") == "success", create_job
    created_id = token_uuid(create_job)
    assert created_id
    assert delete_code == 200 and delete_response.get("ok") is True, delete_response
    delete_job = await_job(stack["api"], delete_response)
    assert delete_job.get("status") == "success", delete_job
    updater.join(timeout=10)
    assert not updater.is_alive(), "alias update remained blocked after Xray create completed"
    update_reply = update_result.get_nowait()
    assert not isinstance(update_reply, BaseException), repr(update_reply)
    update_code, update_response = update_reply
    assert update_code == 200 and update_response.get("ok") is True, update_response

    list_code, listing = http_json(f"{stack['api']}/xray/clients")
    assert list_code == 200 and listing.get("ok") is True, listing
    clients = (listing.get("data") or {}).get("clients") or []
    by_id = {str(item.get("client_id") or item.get("id") or ""): item for item in clients if isinstance(item, dict)}
    assert first_id in by_id and by_id[first_id].get("alias") == "crud-existing-updated", by_id
    assert created_id not in by_id, by_id
    identities = set(loaded_identities(native))
    assert any(email == first_email for _client_id, email in identities), identities
    assert all(email != second_email for _client_id, email in identities), identities
    first_identity = next(pair for pair in identities if pair[1] == first_email)
    assert_applied_bindings(stack["state"], {first_identity})
    assert_native_matches_active(native, stack["state"] / "xray" / "config.json")
    assert_mihomo_launch_matches_active(native, stack["state"] / "generated" / "mihomo" / "config.yaml")


def test_xray_forced_reload_failure_restores_last_good_native_and_active_config(acceptance_stack):
    """An application-requested reload failure must restore and re-read last-good state."""
    stack = acceptance_stack
    active_path = stack["state"] / "xray" / "config.json"
    before = active_path.read_bytes()
    before_loaded = loaded_identities(stack["native"])
    stack["native"].fail_next_reload()
    code, response = http_json(
        f"{stack['api']}/xray/reload", method="POST", payload={"requested_by": "hosted-acceptance"},
    )
    assert code == 200 and response.get("ok") is False, response
    assert active_path.read_bytes() == before
    assert loaded_identities(stack["native"]) == before_loaded
    assert_native_matches_active(stack["native"], active_path)
