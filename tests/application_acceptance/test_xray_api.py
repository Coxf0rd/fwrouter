from __future__ import annotations

import hashlib
import io
import json
import tarfile

from .http_support import http_json
from .xray_support import await_job, loaded_identities


def test_api_xray_client_create_delete_has_native_loaded_readback(acceptance_stack):
    """Exercise the real API/job/core path and prove the running HandlerService state."""
    stack = acceptance_stack
    api, native = stack["api"], stack["native"]
    before_incarnation = native.rpc("runtime_inspect", {"container_id": f"{native.process.pid:x}"})
    assert before_incarnation.get("ok") is True, before_incarnation

    code, created = http_json(
        f"{api}/xray/clients", method="POST",
        payload={"alias": "phase-c-native-user", "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and created.get("ok") is True, created
    job = await_job(api, created)
    assert job.get("status") == "success", json.dumps(job, sort_keys=True, separators=(",", ":"))
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    client_result = result.get("xray_client") if isinstance(result.get("xray_client"), dict) else {}
    client = client_result.get("client") if isinstance(client_result.get("client"), dict) else {}
    client_id = str(client.get("client_id") or "")
    email = str(client.get("email") or "")
    assert client_id and email, job

    second_code, second_created = http_json(
        f"{api}/xray/clients", method="POST",
        payload={"alias": "phase-c-native-user-two", "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert second_code == 200 and second_created.get("ok") is True, second_created
    second_job = await_job(api, second_created)
    assert second_job.get("status") == "success", second_job
    second_result = second_job.get("result") if isinstance(second_job.get("result"), dict) else {}
    second_client_result = second_result.get("xray_client") if isinstance(second_result.get("xray_client"), dict) else {}
    second_client = second_client_result.get("client") if isinstance(second_client_result.get("client"), dict) else {}
    second_id, second_email = str(second_client.get("client_id") or ""), str(second_client.get("email") or "")
    assert second_id and second_email and second_id != client_id, second_job

    loaded = loaded_identities(native)
    assert {(client_id, email), (second_id, second_email)}.issubset(set(loaded)), {"expected": [client_id, email, second_id, second_email], "loaded": loaded}
    list_code, listing = http_json(f"{api}/xray/clients")
    assert list_code == 200 and listing.get("ok") is True, listing
    clients = listing.get("data", {}).get("clients", [])
    assert any(str(row.get("client_id")) == client_id for row in clients), clients

    patch_code, patched = http_json(
        f"{api}/xray/clients/{client_id}", method="PATCH",
        payload={"alias": "phase-c-native-user-renamed", "requested_by": "hosted-acceptance"},
    )
    assert patch_code == 200 and patched.get("ok") is True, patched
    assert patched.get("data", {}).get("xray_client", {}).get("ok") is True, patched
    stack["worker"].terminate()
    stack["worker"].wait(timeout=5)
    restarted_worker, restarted_api = stack["start_worker"]()
    stack["worker"], stack["api"] = restarted_worker, restarted_api
    list_code, listing = http_json(f"{restarted_api}/xray/clients")
    assert list_code == 200 and listing.get("ok") is True, listing
    clients = listing.get("data", {}).get("clients", [])
    renamed = next((row for row in clients if str(row.get("client_id")) == client_id), None)
    assert renamed and renamed.get("alias") == "phase-c-native-user-renamed", clients
    assert {(client_id, email), (second_id, second_email)}.issubset(set(loaded_identities(native)))

    current_config = stack["state"] / "xray" / "config.json"
    archive = native.rpc("runtime_config_archive", {"container_id": f"{native.process.pid:x}"})
    assert archive.get("ok") is True, archive
    archive_bytes = archive["details"]["archive_bytes"]
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as tar:
        members = tar.getmembers()
        assert len(members) == 1 and members[0].name == "config.json" and members[0].isfile(), members
        stream = tar.extractfile(members[0])
        launched_config = stream.read() if stream is not None else b""
    assert json.loads(launched_config)["inbounds"][1]["settings"]["clients"]
    assert hashlib.sha256(launched_config).hexdigest() == hashlib.sha256(current_config.read_bytes()).hexdigest()
    after_incarnation = native.rpc("runtime_inspect", {"container_id": f"{native.process.pid:x}"})
    assert after_incarnation.get("ok") is True, after_incarnation
    assert after_incarnation["details"]["stdout"] != before_incarnation["details"]["stdout"]

    delete_code, deleted = http_json(
        f"{restarted_api}/xray/clients/{client_id}", method="DELETE", payload={"requested_by": "hosted-acceptance"},
    )
    assert delete_code == 200 and deleted.get("ok") is True, deleted
    delete_job = await_job(restarted_api, deleted)
    assert delete_job.get("status") == "success", delete_job
    delete_second_code, delete_second = http_json(
        f"{restarted_api}/xray/clients/{second_id}", method="DELETE", payload={"requested_by": "hosted-acceptance"},
    )
    assert delete_second_code == 200 and delete_second.get("ok") is True, delete_second
    assert await_job(restarted_api, delete_second).get("status") == "success"
    assert (client_id, email) not in loaded_identities(native) and (second_id, second_email) not in loaded_identities(native)
    list_code, listing = http_json(f"{restarted_api}/xray/clients")
    assert list_code == 200 and listing.get("ok") is True, listing
    assert all(str(row.get("client_id")) != client_id for row in listing.get("data", {}).get("clients", []))


def test_invalid_client_request_creates_no_job_or_native_intent(acceptance_stack):
    stack = acceptance_stack
    api, native = stack["api"], stack["native"]
    active = stack["state"] / "xray" / "config.json"
    initial_bytes = active.read_bytes()
    initial_loaded = loaded_identities(native)
    code, before = http_json(f"{api}/jobs?job_type=xray_client_create")
    assert code == 200 and before.get("ok") is True, before
    before_jobs = before.get("data", {}).get("jobs", [])

    code, invalid = http_json(f"{api}/xray/clients", method="POST", payload={"alias": {"invalid": "type"}})
    assert code == 422, invalid
    after_code, after = http_json(f"{api}/jobs?job_type=xray_client_create")
    assert after_code == 200 and after.get("ok") is True, after
    assert len(after.get("data", {}).get("jobs", [])) == len(before_jobs)
    assert active.read_bytes() == initial_bytes
    assert loaded_identities(native) == initial_loaded


def test_native_candidate_rejection_keeps_active_and_loaded_identity_unchanged(acceptance_stack):
    """The native validator rejects an invalid generated candidate before promotion."""
    stack = acceptance_stack
    active = stack["state"] / "xray" / "config.json"
    prior_active_bytes = active.read_bytes()
    prior_loaded = loaded_identities(stack["native"])
    stack["native"].corrupt_next_candidate()

    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "invalid-generation-must-not-promote", "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and response.get("ok") is True, response
    job = await_job(stack["api"], response)
    assert job.get("status") == "failed", job
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    client_result = result.get("xray_client") if isinstance(result.get("xray_client"), dict) else {}
    assert client_result.get("ok") is False, job
    assert active.read_bytes() == prior_active_bytes
    assert loaded_identities(stack["native"]) == prior_loaded
