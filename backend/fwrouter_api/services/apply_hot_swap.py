from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fwrouter_api.adapters.dataplane import (
    DataplaneOperation,
    DataplanePlan,
    DataplaneResult,
)
from fwrouter_api.services.apply_manifest import (
    _manifest_requests_core_bypass,
    _runtime_mode_from_manifest,
)
from fwrouter_api.services.subject_taxonomy import subject_follows_global_mode


_FAST_SUBJECT_APPLY_MODES = {"direct", "selective", "vpn"}
_GLOBAL_MODE_HOT_SWAP_INTENTS = {"set_global_mode"}
_NFT_COMMENT_PATTERN = re.compile(r'comment "([^"]+)"')


def _fast_subject_apply_context(
    *,
    input_data: dict[str, Any] | None,
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(input_data, dict):
        return None
    fast_apply = input_data.get("fast_subject_apply")
    if not isinstance(fast_apply, dict) or not bool(fast_apply.get("enabled")):
        return None

    global_mode = _runtime_mode_from_manifest(manifest).strip().lower()
    if global_mode != "direct":
        return None

    subject_id = str(fast_apply.get("subject_id") or "").strip()
    subject_type = str(fast_apply.get("subject_type") or "").strip().lower()
    target_mode = str(fast_apply.get("target_mode") or "").strip().lower()
    if not subject_id or not subject_follows_global_mode(subject_type) or target_mode not in _FAST_SUBJECT_APPLY_MODES:
        return None

    subjects = manifest.get("subjects") if isinstance(manifest, dict) else None
    if not isinstance(subjects, list):
        return None
    manifest_subject = next(
        (
            subject
            for subject in subjects
            if isinstance(subject, dict) and str(subject.get("subject_id") or "") == subject_id
        ),
        None,
    )
    if not isinstance(manifest_subject, dict):
        return None
    expected_path = "vpn" if target_mode == "vpn" else target_mode
    manifest_path = str(manifest_subject.get("dataplane_path") or "").strip().lower()
    if manifest_path != expected_path:
        return None

    return {
        "subject_id": subject_id,
        "subject_type": subject_type,
        "target_mode": target_mode,
        "manifest_subject": manifest_subject,
        "global_mode": global_mode,
    }


def _verify_fast_subject_apply(context: dict[str, Any]) -> dict[str, Any]:
    subject_id = str(context.get("subject_id") or "")
    target_mode = str(context.get("target_mode") or "")
    manifest_subject = context.get("manifest_subject") if isinstance(context.get("manifest_subject"), dict) else {}
    scoped_runtime = manifest_subject.get("scoped_runtime") if isinstance(manifest_subject, dict) else {}
    matcher = scoped_runtime.get("matcher") if isinstance(scoped_runtime, dict) else {}
    family = str(matcher.get("family") or "").strip().lower() if isinstance(matcher, dict) else ""

    try:
        completed = subprocess.run(
            ["nft", "list", "chain", "inet", "fwrouter_v2", "fwrouter_classify"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "error_code": "NFT_NOT_AVAILABLE",
            "error_message": str(exc),
            "subject_id": subject_id,
            "target_mode": target_mode,
            "raw_chain": "",
        }
    except subprocess.CalledProcessError as exc:
        return {
            "ok": False,
            "error_code": "NFT_CHAIN_READ_FAILED",
            "error_message": (exc.stderr or exc.stdout or str(exc)).strip(),
            "subject_id": subject_id,
            "target_mode": target_mode,
            "raw_chain": exc.stdout or "",
        }

    raw_chain = completed.stdout
    if target_mode == "direct":
        direct_marker = f"scoped direct override: {subject_id}"
        stale_subject_markers = (
            f"scoped vpn override: {subject_id}",
            f"scoped selective direct IPv4: {subject_id}",
            f"scoped selective vpn IPv4: {subject_id}",
            f"scoped selective dns direct IPv4: {subject_id}",
            f"scoped selective dns vpn IPv4: {subject_id}",
            f"scoped selective direct IPv6: {subject_id}",
            f"scoped selective vpn IPv6: {subject_id}",
            f"scoped selective default direct: {subject_id}",
            f"scoped selective default vpn: {subject_id}",
            f"scoped selective degraded default direct: {subject_id}",
        )
        ok = direct_marker in raw_chain or (
            "global direct v1" in raw_chain
            and not any(marker in raw_chain for marker in stale_subject_markers)
        )
    elif target_mode == "vpn":
        ok = f'scoped vpn override: {subject_id}' in raw_chain
    else:
        if family == "ipv6":
            direct_branch = f'scoped selective direct IPv6: {subject_id}'
            vpn_branch = (
                f'scoped selective vpn IPv6: {subject_id}' in raw_chain
                or f'scoped selective degraded block VPN IPv6: {subject_id}' in raw_chain
            )
        else:
            direct_branch = f'scoped selective direct IPv4: {subject_id}'
            vpn_branch = (
                f'scoped selective vpn IPv4: {subject_id}' in raw_chain
                or f'scoped selective degraded block VPN IPv4: {subject_id}' in raw_chain
            )
        default_branch = (
            f'scoped selective default ' in raw_chain and subject_id in raw_chain
        ) or f'scoped selective degraded default direct: {subject_id}' in raw_chain
        ok = direct_branch in raw_chain and vpn_branch and default_branch

    return {
        "ok": ok,
        "error_code": None if ok else "FAST_SUBJECT_APPLY_VERIFY_FAILED",
        "error_message": None if ok else f"Live classify chain is missing expected subject rule for {subject_id}.",
        "subject_id": subject_id,
        "target_mode": target_mode,
        "raw_chain": raw_chain,
    }


def _extract_classify_rules(candidate_path: str | None) -> list[str]:
    if not candidate_path:
        return []
    path = Path(candidate_path)
    if not path.exists():
        return []

    rules: list[str] = []
    in_chain = False
    depth = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not in_chain:
            if stripped == "chain fwrouter_classify {":
                in_chain = True
                depth = 1
            continue

        depth += stripped.count("{")
        depth -= stripped.count("}")
        if depth <= 0:
            break
        if stripped:
            rules.append(stripped)
    return rules


def _global_mode_hot_swap_context(
    *,
    input_data: dict[str, Any] | None,
    manifest: dict[str, Any],
    check_details: dict[str, Any],
    preflight: dict[str, Any],
    candidate_path: str | None,
) -> dict[str, Any] | None:
    if not isinstance(input_data, dict):
        return None
    if str(input_data.get("intent") or "").strip() not in _GLOBAL_MODE_HOT_SWAP_INTENTS:
        return None
    if _manifest_requests_core_bypass(manifest):
        return None

    required_chains = check_details.get("required_chains")
    if not bool(check_details.get("table_exists")):
        return None
    if not isinstance(required_chains, dict) or not all(bool(value) for value in required_chains.values()):
        return None

    if bool((manifest.get("summary") or {}).get("requires_vpn_policy_routing")):
        if not bool(check_details.get("vpn_external_path_verified")):
            return None

    if not bool(preflight.get("can_enforce_global_direct")):
        return None

    rules = _extract_classify_rules(candidate_path)
    if not rules:
        return None

    return {
        "target_mode": _runtime_mode_from_manifest(manifest),
        "hot_swap_kind": "global_mode",
        "rules": rules,
        "candidate_path": candidate_path,
    }


def _subject_mode_hot_swap_context(
    *,
    fast_subject_apply: dict[str, Any] | None,
    manifest: dict[str, Any],
    check_details: dict[str, Any],
    preflight: dict[str, Any],
    candidate_path: str | None,
) -> dict[str, Any] | None:
    if fast_subject_apply is None:
        return None
    if _manifest_requests_core_bypass(manifest):
        return None

    required_chains = check_details.get("required_chains")
    if not bool(check_details.get("table_exists")):
        return None
    if not isinstance(required_chains, dict) or not all(bool(value) for value in required_chains.values()):
        return None

    if bool((manifest.get("summary") or {}).get("requires_vpn_policy_routing")):
        if not bool(check_details.get("vpn_external_path_verified")):
            return None

    if not bool(preflight.get("can_enforce_global_direct")):
        return None

    rules = _extract_classify_rules(candidate_path)
    if not rules:
        return None

    return {
        **fast_subject_apply,
        "hot_swap_kind": "subject_mode",
        "target_mode": str(fast_subject_apply.get("target_mode") or ""),
        "rules": rules,
        "candidate_path": candidate_path,
    }


def _apply_global_mode_hot_swap(
    *,
    context: dict[str, Any],
    plan: DataplanePlan,
    check_details: dict[str, Any],
) -> DataplaneResult:
    rules = [str(rule) for rule in context.get("rules") or [] if str(rule).strip()]
    commands = ["flush chain inet fwrouter_v2 fwrouter_classify"]
    commands.extend(
        f"add rule inet fwrouter_v2 fwrouter_classify {rule}"
        for rule in rules
    )
    payload = "\n".join(commands) + "\n"

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="fwrouter-global-hot-swap-",
        suffix=".nft",
        delete=False,
    ) as handle:
        handle.write(payload)
        command_path = handle.name

    def _run_nft_payload() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["nft", "-f", command_path],
            check=False,
            capture_output=True,
            text=True,
        )

    def _verify_live_comments() -> dict[str, Any]:
        expected_comments = _NFT_COMMENT_PATTERN.findall(payload)
        try:
            live = subprocess.run(
                ["nft", "list", "chain", "inet", "fwrouter_v2", "fwrouter_classify"],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            return {
                "ok": False,
                "error_code": "NFT_NOT_AVAILABLE",
                "error_message": str(exc),
                "missing_comments": expected_comments,
            }
        if live.returncode != 0:
            return {
                "ok": False,
                "error_code": "NFT_CHAIN_READ_FAILED",
                "error_message": (live.stderr or live.stdout or "Failed to read live classify chain.").strip(),
                "missing_comments": expected_comments,
            }
        raw_chain = live.stdout
        missing = [comment for comment in expected_comments if comment not in raw_chain]
        return {
            "ok": not missing,
            "error_code": None if not missing else "NFT_GLOBAL_MODE_HOT_SWAP_VERIFY_FAILED",
            "error_message": None if not missing else "Live classify chain is missing hot-swapped rule markers.",
            "missing_comments": missing,
            "raw_chain": raw_chain,
        }

    try:
        completed = _run_nft_payload()
        hot_swap_verify = _verify_live_comments() if completed.returncode == 0 else {
            "ok": False,
            "error_code": "NFT_GLOBAL_MODE_HOT_SWAP_FAILED",
            "error_message": (completed.stderr or completed.stdout or "nft hot-swap failed.").strip(),
            "missing_comments": [],
        }
        retried = False
        if completed.returncode == 0 and not bool(hot_swap_verify.get("ok")):
            retried = True
            completed = _run_nft_payload()
            hot_swap_verify = _verify_live_comments() if completed.returncode == 0 else hot_swap_verify
    finally:
        try:
            Path(command_path).unlink()
        except FileNotFoundError:
            pass

    details = {
        **check_details,
        "adapter": "nft-owned-table",
        "operation": DataplaneOperation.APPLY.value,
        "stage": "verify" if completed.returncode == 0 else "apply",
        "hot_swap": True,
        "hot_swap_kind": context.get("hot_swap_kind") or "global_mode",
        "hot_swap_scope": "fwrouter_classify",
        "hot_swap_rules_count": len(rules),
        "hot_swap_verify": hot_swap_verify,
        "hot_swap_retried": retried,
        "candidate_path": context.get("candidate_path"),
        "script": {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    }
    ok = completed.returncode == 0 and bool(hot_swap_verify.get("ok"))
    return DataplaneResult(
        ok=ok,
        operation=DataplaneOperation.APPLY,
        message=(
            "FWRouter global mode classify chain hot-swapped."
            if ok
            else "FWRouter global mode classify chain hot-swap failed."
        ),
        details=details,
        error_code=None if ok else str(hot_swap_verify.get("error_code") or "NFT_GLOBAL_MODE_HOT_SWAP_FAILED"),
        error_message=None if ok else str(
            hot_swap_verify.get("error_message")
            or completed.stderr
            or completed.stdout
            or "nft hot-swap failed."
        ).strip(),
    )
