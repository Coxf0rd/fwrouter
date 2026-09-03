from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence

from fwrouter_api.services.reconcile import ReconcileResult, build_reconcile_response


def _count(results: list[ReconcileResult], entity_type: str, state: str | None = None) -> int:
    return sum(
        1
        for result in results
        if result.entity_type == entity_type and (state is None or result.reconcile_state == state)
    )


def _is_xray_result(result: ReconcileResult) -> bool:
    return result.entity_type == "xray" or (
        result.entity_type == "subject"
        and result.details.get("implementation_kind") == "xray"
    )


def _count_xray(results: list[ReconcileResult], state: str | None = None) -> int:
    return sum(
        1
        for result in results
        if _is_xray_result(result) and (state is None or result.reconcile_state == state)
    )


def _print_reconcile_check() -> int:
    response = build_reconcile_response()
    results = response.entities
    summary = response.summary
    system_ok = summary["drift"] == 0 and summary["stale"] == 0 and summary["failed"] == 0
    print("SYSTEM OK" if system_ok else "SYSTEM DRIFT")
    print()

    print("XRay:")
    print(f"  {_count_xray(results)} checked")
    print(f"  {_count_xray(results, 'drift')} drift")
    print(f"  {_count_xray(results, 'stale')} stale")
    print()

    routing = next((result for result in results if result.entity_type == "routing"), None)
    routing_state = "OK" if routing and routing.reconcile_state == "in_sync" else "DRIFT"
    dataplane_state = routing_state
    print("Routing:")
    print(f"  rules={routing_state}")
    print(f"  dataplane={dataplane_state}")

    extra = Counter(result.reconcile_state for result in results)
    if not system_ok:
        print()
        print("Summary:")
        for state in ("drift", "stale", "failed", "unknown"):
            count = extra.get(state, 0)
            if count:
                print(f"  {state}={count}")
    return 0 if system_ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fwrouter")
    subparsers = parser.add_subparsers(dest="command")
    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_subparsers = reconcile_parser.add_subparsers(dest="reconcile_command")
    reconcile_subparsers.add_parser("check")
    args = parser.parse_args(argv)

    if args.command == "reconcile" and args.reconcile_command == "check":
        return _print_reconcile_check()
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
