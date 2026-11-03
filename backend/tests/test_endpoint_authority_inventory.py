"""Fail-closed endpoint/authority inventory for the shipped FastAPI surface.

This test deliberately expands FastAPI's deferred router includes.  OpenAPI is
checked as an independent projection, but is not the source of completeness:
routes hidden with ``include_in_schema=False`` must still appear here.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
INVENTORY_PATH = REPO_ROOT / "knowledge/audits/stage6_1_package_g_2026-11-03/ENDPOINT_AUTHORITY_MATRIX.json"
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
# An authority edit requires an explicit test/reference review, not just regenerating JSON.
REVIEWED_AUTHORITY_SHA256 = "5fa964e4a8cf612bc88ad25b6fb47688e268a9c10a77718f95c0a41edafe220e"
AUTHORITY_PROJECTION_FIELDS = (
    "order", "path", "methods", "handler", "handler_source_sha256", "include_in_schema",
    "route_kind", "authority_class", "admission_contract", "application_dependencies", "dependencies",
)


class InventoryError(AssertionError):
    pass


def _handler_identity(endpoint: Any) -> str:
    module = getattr(endpoint, "__module__", None)
    qualname = getattr(endpoint, "__qualname__", None)
    if not module or not qualname:
        target = endpoint if inspect.isfunction(endpoint) or inspect.ismethod(endpoint) else type(endpoint)
        module = getattr(target, "__module__", None)
        qualname = getattr(target, "__qualname__", None)
    if not module or not qualname:
        raise InventoryError(f"callable has no stable module/qualname: {endpoint!r}")
    return f"{module}:{qualname}"


def _source_sha256(endpoint: Any) -> str | None:
    # Framework-generated docs handlers are version-owned and not application
    # admission code. Their route path/method/identity remain inventoried.
    if str(getattr(endpoint, "__module__", "")).startswith("fastapi."):
        return None
    try:
        source = inspect.getsource(endpoint)
    except (OSError, TypeError) as exc:
        if not inspect.isfunction(endpoint) and not inspect.ismethod(endpoint):
            try:
                source = inspect.getsource(type(endpoint))
            except (OSError, TypeError) as nested_exc:
                raise InventoryError(
                    f"route handler source is unavailable: {_handler_identity(endpoint)}"
                ) from nested_exc
        else:
            raise InventoryError(f"route handler source is unavailable: {_handler_identity(endpoint)}") from exc
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _callable_record(call: Any) -> dict[str, Any]:
    if call is None:
        raise InventoryError("dependency has no callable target")
    identity = _handler_identity(call)
    return {"call": identity, "source_sha256": _source_sha256(call)}


def _declared_dependencies(dependencies: Any) -> list[dict[str, Any]]:
    if dependencies is None:
        return []
    if not isinstance(dependencies, (list, tuple)):
        raise InventoryError(f"unsupported dependency collection: {type(dependencies).__qualname__}")
    result = []
    for dependency in dependencies:
        target = getattr(dependency, "dependency", None)
        if target is None:
            raise InventoryError(f"dependency declaration has no target: {dependency!r}")
        result.append({
            **_callable_record(target),
            "use_cache": getattr(dependency, "use_cache", None),
            "scope": getattr(dependency, "scope", None),
        })
    return result


def _effective_dependency_graph(dependant: Any) -> list[dict[str, Any]]:
    """Serialize FastAPI's resolved dependency graph, including Annotated Depends."""
    seen: set[int] = set()

    def visit(node: Any) -> dict[str, Any]:
        if id(node) in seen:
            raise InventoryError("cyclic or shared dependency graph is not representable")
        seen.add(id(node))
        call = getattr(node, "call", None)
        record = {
            **_callable_record(call),
            "name": getattr(node, "name", None),
            "use_cache": getattr(node, "use_cache", None),
            "scope": getattr(node, "scope", None),
            "own_oauth_scopes": list(getattr(node, "own_oauth_scopes", None) or []),
            "parent_oauth_scopes": list(getattr(node, "parent_oauth_scopes", None) or []),
            "dependencies": [],
        }
        children = getattr(node, "dependencies", None)
        if not isinstance(children, list):
            raise InventoryError("FastAPI dependency node has an unsupported children shape")
        record["dependencies"] = [visit(child) for child in children]
        seen.remove(id(node))
        return record

    if dependant is None:
        raise InventoryError("FastAPI route has no resolved dependant graph")
    children = getattr(dependant, "dependencies", None)
    if not isinstance(children, list):
        raise InventoryError("FastAPI route dependency graph is not a list")
    return [visit(child) for child in children]


def _normalized_path(path: str) -> str:
    parameters = re.findall(r"\{([^{}]+)\}", path)
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in parameters):
        raise InventoryError(f"unsupported route parameter syntax: {path}")
    remainder = re.sub(r"\{[^{}]+\}", "", path)
    if "{" in remainder or "}" in remainder:
        raise InventoryError(f"malformed route path: {path}")
    shape = re.sub(r"\{[^{}]+\}", "{}", path)
    return shape


def _flatten_routes(app: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Expand top-level and deferred APIRouter entries, failing on new shapes."""
    from fastapi.routing import APIRoute
    from starlette.routing import Route

    rows: list[dict[str, Any]] = []
    includes: list[dict[str, Any]] = []

    app_dependencies = _declared_dependencies(getattr(getattr(app, "router", None), "dependencies", None))

    def append_route(route: Any, prefix: str, include_in_schema: bool, order: str,
                     *, effective: Any = None) -> None:
        if not isinstance(route, (APIRoute, Route)):
            raise InventoryError(f"unsupported route shape {type(route).__module__}.{type(route).__qualname__} at {order}")
        path = getattr(effective, "path", prefix + route.path)
        _normalized_path(path)
        methods = sorted(set(getattr(effective, "methods", None) or getattr(route, "methods", None) or ()))
        if not methods or not set(methods) <= HTTP_METHODS:
            raise InventoryError(f"unsupported or empty HTTP method set at {path}: {methods!r}")
        endpoint = getattr(effective, "endpoint", None) or route.endpoint
        dependant = getattr(effective, "dependant", None) or getattr(route, "dependant", None)
        if isinstance(route, APIRoute):
            dependency_graph = _effective_dependency_graph(dependant)
        else:
            dependency_graph = []
        rows.append({
            "order": order,
            "path": path,
            "methods": methods,
            "handler": _handler_identity(endpoint),
            "handler_source_sha256": _source_sha256(endpoint),
            "include_in_schema": bool(include_in_schema and getattr(effective, "include_in_schema", getattr(route, "include_in_schema", True))),
            "route_kind": type(route).__name__,
            "application_dependencies": app_dependencies,
            "dependencies": dependency_graph,
        })

    def visit(entries: list[Any], prefix: str, include_in_schema: bool, order_prefix: str) -> None:
        for index, route in enumerate(entries):
            order = f"{order_prefix}{index:03d}"
            if type(route).__module__ == "fastapi.routing" and type(route).__name__ == "_IncludedRouter":
                original = getattr(route, "original_router", None)
                context = getattr(route, "include_context", None)
                if original is None or context is None or not hasattr(original, "routes"):
                    raise InventoryError(f"unreadable deferred router include at {order}")
                child_prefix = str(getattr(context, "prefix", ""))
                child_schema = bool(getattr(context, "include_in_schema", True))
                members = original.routes
                declarations = [
                    {
                        "path": item.path,
                        "methods": sorted(set(getattr(item, "methods", None) or ())),
                        "handler": _handler_identity(item.endpoint),
                        "declared_dependencies": _declared_dependencies(getattr(item, "dependencies", None)),
                        "effective_dependencies": _effective_dependency_graph(item.dependant)
                        if isinstance(item, APIRoute) else [],
                    }
                    for item in members if isinstance(item, (APIRoute, Route))
                ]
                effective_router_dependencies = _declared_dependencies(getattr(original, "dependencies", None))
                effective_include_dependencies = _declared_dependencies(getattr(context, "dependencies", None))
                includes.append({
                    "order": order,
                    "prefix": prefix + child_prefix,
                    "include_in_schema": include_in_schema and child_schema,
                    "router_dependencies": effective_router_dependencies,
                    "include_dependencies": effective_include_dependencies,
                    "application_dependencies": app_dependencies,
                    "route_declarations": declarations,
                })
                effective_candidates = getattr(route, "effective_candidates", None)
                if not callable(effective_candidates):
                    raise InventoryError(f"deferred router has no effective dependency resolver at {order}")
                candidates = effective_candidates()
                if not isinstance(candidates, list):
                    raise InventoryError(f"deferred router returned unsupported candidate collection at {order}")
                for candidate_index, candidate in enumerate(candidates):
                    original_route = getattr(candidate, "original_route", None)
                    if not isinstance(original_route, (APIRoute, Route)):
                        raise InventoryError(f"unsupported effective route candidate at {order}.{candidate_index:03d}")
                    append_route(
                        original_route, "", include_in_schema and child_schema,
                        f"{order}.{candidate_index:03d}", effective=candidate,
                    )
                continue
            append_route(route, prefix, include_in_schema, order)

    visit(list(app.routes), "", True, "")
    # Normalize to flattened registration order so FastAPI releases that
    # materialize included routes eagerly compare with releases that defer
    # `_IncludedRouter` expansion.
    for index, row in enumerate(rows):
        row["order"] = index
    return rows, includes


def _route_identity(row: dict[str, Any]) -> tuple[str, tuple[str, ...], str]:
    return row["path"], tuple(row["methods"]), row["handler"]


def _validate_inventory_document(document: dict[str, Any]) -> None:
    routes = document.get("routes")
    if not isinstance(routes, list) or not routes:
        raise InventoryError("inventory routes must be a non-empty list")
    identities = [_route_identity(row) for row in routes]
    if len(identities) != len(set(identities)):
        raise InventoryError("duplicate route identity")
    contracts = {item.get("id") for item in document.get("admission_contracts", [])}
    if None in contracts or len(contracts) != len(document.get("admission_contracts", [])):
        raise InventoryError("admission contracts have missing or duplicate identities")
    for row in routes:
        if set(row) != {
            "order", "path", "methods", "handler", "handler_source_sha256",
            "include_in_schema", "route_kind", "authority_class", "admission_contract", "dependencies",
            "application_dependencies",
        }:
            raise InventoryError(f"route row has missing or unexpected fields: {row!r}")
        if row["authority_class"] not in {
            "framework_documentation", "trusted_host_uncredentialed",
            "scoped_client_capability", "field_scoped_client_capability",
            "maintenance_import_capability", "public_subscription_capability",
            "legacy_subscription_compatibility",
        }:
            raise InventoryError(f"route has unknown authority classification: {row['path']}")
        if not isinstance(row["admission_contract"], str) or not row["admission_contract"]:
            raise InventoryError(f"route lacks explicit admission contract: {row['path']}")
        if row["admission_contract"] not in contracts:
            raise InventoryError(f"route references unknown admission contract: {row['path']}")
    expected_source_files = {"backend/fwrouter_api/main.py"} | {
        member.relative_to(REPO_ROOT).as_posix()
        for member in (REPO_ROOT / "backend/fwrouter_api/routes").glob("*.py")
    }
    reviewed_source_files = document.get("reviewed_source_files", [])
    source_file_paths = {item.get("path") for item in reviewed_source_files}
    if source_file_paths != expected_source_files:
        raise InventoryError("whole application/router source inventory is incomplete or contains unexpected files")
    if (len(source_file_paths) != len(reviewed_source_files)
            or any(set(item) != {"path", "sha256"}
                   or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))
                   for item in reviewed_source_files)):
        raise InventoryError("whole application/router source digest inventory is malformed")
    if not re.fullmatch(r"[0-9a-f]{64}", str(document.get("authority_projection_sha256", ""))):
        raise InventoryError("authority projection digest is missing or malformed")


def _authority_projection_sha256(routes: list[dict[str, Any]]) -> str:
    projection = [{key: row[key] for key in AUTHORITY_PROJECTION_FIELDS} for row in routes]
    canonical = json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _assert_authority_projection(document: dict[str, Any]) -> None:
    expected = REVIEWED_AUTHORITY_SHA256
    if document["authority_projection_sha256"] != expected:
        raise InventoryError("reviewed authority anchor changed; independent policy review required")
    actual = _authority_projection_sha256(document["routes"])
    if actual != expected:
        raise InventoryError("manual authority/dependency projection digest changed; explicit review anchor update required")


def _assert_route_snapshot(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> None:
    normalized = [{key: value for key, value in row.items()
                   if key not in {"authority_class", "admission_contract"}}
                  for row in expected]
    if actual != normalized:
        raise InventoryError("route order, path, methods, identity, visibility or handler source drift")


def _assert_protected_authority_bindings(routes: list[dict[str, Any]]) -> None:
    """Pin the explicitly scoped/capability handlers to their reviewed class."""
    bindings = {
        ("/api/v2/events/secure/recent", "fwrouter_api.routes.events:secure_recent_events_endpoint"):
            ("scoped_client_capability", "api_client_registry_scoped"),
        ("/api/v2/ui/settings/client-list", "fwrouter_api.routes.ui:patch_client_list_settings_endpoint"):
            ("scoped_client_capability", "api_client_registry_scoped"),
        ("/api/v2/ui/settings/display", "fwrouter_api.routes.ui:save_ui_display_settings_endpoint"):
            ("field_scoped_client_capability", "api_client_registry_scoped"),
        ("/api/v2/transfer/control-plane/import", "fwrouter_api.routes.transfer:import_control_plane_endpoint"):
            ("maintenance_import_capability", "maintenance_import_capability"),
        ("/s/{token}", "fwrouter_api.routes.xray:export_public_subscription_endpoint"):
            ("public_subscription_capability", "public_subscription_token"),
        ("/api/v2/xray/clients/{client_id}/subscription.txt",
         "fwrouter_api.routes.xray:export_xray_subscription_text_endpoint"):
            ("legacy_subscription_compatibility", "legacy_subscription_route_no_http_auth"),
    }
    found: set[tuple[str, str]] = set()
    for row in routes:
        key = (row["path"], row["handler"])
        if key in bindings:
            if (row["authority_class"], row["admission_contract"]) != bindings[key]:
                raise InventoryError(f"protected route authority/admission changed: {key[0]}")
            found.add(key)
    missing = set(bindings) - found
    if missing:
        raise InventoryError(f"protected route missing from inventory: {sorted(missing)!r}")


def _schema_operations(app: Any) -> set[tuple[str, str]]:
    schema = app.openapi()
    operations: set[tuple[str, str]] = set()
    for path, item in schema.get("paths", {}).items():
        for method in item:
            if method.upper() in HTTP_METHODS:
                operations.add((path, method.upper()))
    return operations


def _expected_schema_operations(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {
        (row["path"], method)
        for row in rows if row["include_in_schema"]
        for method in row["methods"]
        if method not in {"HEAD", "OPTIONS"}
    }


def _route_overlap_pairs(rows: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Find same-method identical or simple static-vs-parameter shadow pairs."""
    pairs: list[tuple[str, str, str]] = []
    for index, left in enumerate(rows):
        left_shape = _normalized_path(left["path"])
        for right in rows[index + 1:]:
            shared = sorted(set(left["methods"]) & set(right["methods"]))
            if not shared:
                continue
            right_shape = _normalized_path(right["path"])
            overlap = left_shape == right_shape
            if not overlap and "{" in left["path"] and "{" not in right["path"]:
                pattern = re.sub(r"\{[^{}]+\}", "[^/]+", left["path"])
                overlap = re.fullmatch(pattern, right["path"]) is not None
            elif not overlap and "{" in right["path"] and "{" not in left["path"]:
                pattern = re.sub(r"\{[^{}]+\}", "[^/]+", right["path"])
                overlap = re.fullmatch(pattern, left["path"]) is not None
            # A later generic parameter route is safe from shadowing by an
            # earlier static route; the inverse ordering is not. Equal shapes
            # with a shared method are always ambiguous.
            if overlap and (left_shape == right_shape or ("{" in left["path"] and "{" not in right["path"])):
                pairs.append((left["path"], right["path"], shared[0]))
    return pairs


def test_actual_app_routes_match_reviewed_authority_inventory() -> None:
    from fwrouter_api.main import create_app

    app = create_app(enable_startup_tasks=False)
    actual_routes, actual_includes = _flatten_routes(app)
    document = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    _validate_inventory_document(document)
    _assert_authority_projection(document)
    _assert_route_snapshot(actual_routes, document["routes"])
    _assert_protected_authority_bindings(document["routes"])
    # Newer FastAPI versions expose deferred include metadata. Older supported
    # versions eagerly copy APIRoutes, in which case the flattened route list,
    # registration order and main.py content digest remain the comparable
    # evidence.
    if actual_includes:
        assert actual_includes == document["router_includes"], "router include order/prefix/declaration drift"
    assert len(document["router_includes"]) == 25, "reviewed router include inventory changed"
    assert _schema_operations(app) == _expected_schema_operations(actual_routes), "OpenAPI projection omits or invents operations"
    assert not _route_overlap_pairs(actual_routes), "same-method route shadow or duplicate needs explicit review"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown_authority", "missing_admission", "changed_admission"])
def test_inventory_validation_rejects_unreviewed_or_ambiguous_entries(mutation: str) -> None:
    document = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    changed = json.loads(json.dumps(document))
    if mutation == "missing":
        changed["routes"].pop()
        from fwrouter_api.main import create_app
        actual_routes, _ = _flatten_routes(create_app(enable_startup_tasks=False))
        with pytest.raises(InventoryError, match="route order"):
            _assert_route_snapshot(actual_routes, changed["routes"])
        return
    if mutation == "duplicate":
        changed["routes"].append(json.loads(json.dumps(changed["routes"][0])))
    elif mutation == "unknown_authority":
        changed["routes"][0]["authority_class"] = "unclassified"
    elif mutation == "missing_admission":
        changed["routes"][0]["admission_contract"] = ""
        with pytest.raises(InventoryError):
            _validate_inventory_document(changed)
        return
    else:
        target = next(row for row in changed["routes"] if row["path"] == "/api/v2/events/secure/recent")
        target["authority_class"] = "trusted_host_uncredentialed"
        with pytest.raises(InventoryError, match="authority/admission changed"):
            _assert_protected_authority_bindings(changed["routes"])
        return
    with pytest.raises(InventoryError):
        _validate_inventory_document(changed)


def test_valid_but_wrong_authority_relabel_requires_manual_projection_update() -> None:
    document = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    changed = json.loads(json.dumps(document))
    health = next(row for row in changed["routes"] if row["path"] == "/api/v2/health")
    health["authority_class"] = "public_subscription_capability"
    health["admission_contract"] = "public_subscription_token"
    _validate_inventory_document(changed)
    with pytest.raises(InventoryError, match="manual authority/dependency projection digest changed"):
        _assert_authority_projection(changed)


def test_relabel_and_regenerated_json_digest_still_require_reviewed_test_anchor() -> None:
    document = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    changed = json.loads(json.dumps(document))
    health = next(row for row in changed["routes"] if row["path"] == "/api/v2/health")
    health["authority_class"] = "public_subscription_capability"
    health["admission_contract"] = "public_subscription_token"
    changed["authority_projection_sha256"] = _authority_projection_sha256(changed["routes"])
    _validate_inventory_document(changed)
    with pytest.raises(InventoryError, match="reviewed authority anchor changed"):
        _assert_authority_projection(changed)


@pytest.mark.parametrize("layer", ["application", "router", "include", "route"])
def test_dependency_added_at_any_registration_layer_changes_existing_handler_snapshot(layer: str) -> None:
    from fastapi import APIRouter, Depends, FastAPI
    from fwrouter_api.routes.system import health

    def gate() -> None:
        return None

    application = FastAPI(dependencies=[Depends(gate)] if layer == "application" else [])
    router = APIRouter(dependencies=[Depends(gate)] if layer == "router" else [])
    router.add_api_route(
        "/health", health, methods=["GET"],
        dependencies=[Depends(gate)] if layer == "route" else [],
    )
    application.include_router(
        router, prefix="/api/v2", dependencies=[Depends(gate)] if layer == "include" else [],
    )
    actual, _ = _flatten_routes(application)
    actual_health = next(row for row in actual if row["path"] == "/api/v2/health")
    expected_health = next(
        row for row in json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))["routes"]
        if row["path"] == "/api/v2/health"
    )
    assert actual_health["dependencies"]
    actual_health["order"] = expected_health["order"]
    with pytest.raises(InventoryError, match="route order"):
        _assert_route_snapshot([actual_health], [expected_health])


def test_annotated_depends_appears_in_effective_dependency_graph() -> None:
    from typing import Annotated
    from fastapi import APIRouter, Depends, FastAPI

    def gate() -> str:
        return "synthetic"

    def endpoint(_gate: Annotated[str, Depends(gate)]) -> dict[str, bool]:
        return {"ok": True}

    # This module enables postponed annotations; install the concrete
    # Annotated object so FastAPI resolves the local fixture dependency exactly
    # as it would a module-level application dependency.
    endpoint.__annotations__["_gate"] = Annotated[str, Depends(gate)]

    router = APIRouter()
    router.add_api_route("/annotated", endpoint, methods=["GET"])
    application = FastAPI()
    application.include_router(router, prefix="/synthetic")
    actual, _ = _flatten_routes(application)
    route = next(row for row in actual if row["path"] == "/synthetic/annotated")
    assert route["dependencies"][0]["call"].endswith("gate")


def test_schema_hidden_route_is_still_in_complete_inventory() -> None:
    document = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    hidden = [row for row in document["routes"] if not row["include_in_schema"]]
    assert {row["path"] for row in hidden} == {
        "/api/v2/openapi.json", "/api/v2/docs", "/docs/oauth2-redirect", "/api/v2/redoc",
    }


def test_unlisted_schema_hidden_addition_is_rejected() -> None:
    from fwrouter_api.main import create_app

    actual, _ = _flatten_routes(create_app(enable_startup_tasks=False))
    expected = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))["routes"]
    synthetic = dict(actual[0])
    synthetic.update({"order": "999.999", "path": "/hidden/new", "methods": ["GET"],
                      "handler": "fwrouter_api.routes.new:hidden", "handler_source_sha256": "0" * 64,
                      "include_in_schema": False, "route_kind": "APIRoute"})
    with pytest.raises(InventoryError, match="route order"):
        _assert_route_snapshot(actual + [synthetic], expected)


def test_route_order_keeps_static_events_path_ahead_of_parameter_path() -> None:
    from fwrouter_api.main import create_app

    actual, _ = _flatten_routes(create_app(enable_startup_tasks=False))
    recent = next(i for i, row in enumerate(actual) if row["path"] == "/api/v2/events/recent")
    parameter = next(i for i, row in enumerate(actual) if row["path"] == "/api/v2/events/{event_id}")
    assert recent < parameter
    assert not _route_overlap_pairs(actual)


def test_inventory_detects_dynamic_route_registered_before_static_route() -> None:
    dynamic = {"path": "/events/{event_id}", "methods": ["GET"]}
    static = {"path": "/events/recent", "methods": ["GET"]}
    assert _route_overlap_pairs([dynamic, static]) == [
        ("/events/{event_id}", "/events/recent", "GET"),
    ]
    assert _route_overlap_pairs([static, dynamic]) == []


@pytest.mark.parametrize("path", ["/items/{item_id:int}", "/items/{item_id/path}", "/items/{broken"])
def test_inventory_rejects_unreviewed_route_parameter_syntax(path: str) -> None:
    with pytest.raises(InventoryError):
        _normalized_path(path)


@pytest.mark.parametrize("route_shape", ["mount", "websocket", "unknown"])
def test_inventory_fails_closed_on_unsupported_route_shapes(route_shape: str) -> None:
    class Mount:
        pass

    class WebSocketRoute:
        pass

    class NewFrameworkRoute:
        pass

    route = {"mount": Mount, "websocket": WebSocketRoute, "unknown": NewFrameworkRoute}[route_shape]()
    with pytest.raises(InventoryError, match="unsupported route shape"):
        _flatten_routes(SimpleNamespace(routes=[route]))


def test_admission_contract_digests_match_reviewed_source() -> None:
    document = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    for contract in document["admission_contracts"]:
        for item in contract["files"]:
            source = (REPO_ROOT / item["path"]).read_bytes()
            assert hashlib.sha256(source).hexdigest() == item["sha256"], contract["id"]


def test_whole_application_and_router_modules_match_reviewed_source() -> None:
    document = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    for item in document["reviewed_source_files"]:
        source = (REPO_ROOT / item["path"]).read_bytes()
        assert hashlib.sha256(source).hexdigest() == item["sha256"], item["path"]
