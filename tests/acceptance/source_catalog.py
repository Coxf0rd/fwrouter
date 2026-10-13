"""Static acceptance node inventory; never import application or test modules."""
from __future__ import annotations

import ast
import json
from pathlib import Path


class CatalogError(ValueError):
    pass


def collect_source_nodes(root: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted((root / "tests/application_acceptance").glob("test_*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        # Reject collection forms this exact-ID gate does not understand.
        # Silently ignoring a pytest class/module parametrization would allow
        # a selected acceptance scenario to disappear from the receipt.
        for statement in tree.body:
            if isinstance(statement, ast.ClassDef) and (statement.name.startswith("Test") or any(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_")
                for item in statement.body
            )):
                raise CatalogError(f"{path.name}: test classes require an explicit catalog extension")
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                if any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
                    raise CatalogError(f"{path.name}: module pytestmark is unsupported by exact-ID catalog")
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test_"):
                continue
            suffixes = [""]
            recovery = path.name.endswith("_l7.py")
            packet = False
            for decorator in reversed(node.decorator_list):
                func = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(func, ast.Attribute) and func.attr == "l7":
                    recovery = True
                if isinstance(func, ast.Attribute) and func.attr == "packet":
                    packet = True
                if isinstance(func, ast.Attribute) and func.attr == "parametrize":
                    try:
                        cases = ast.literal_eval(decorator.args[1])
                        ids = ast.literal_eval(next(k.value for k in decorator.keywords if k.arg == "ids"))
                    except (ValueError, TypeError, IndexError, StopIteration) as exc:
                        raise CatalogError(f"{path.name}:{node.name}: literal cases and explicit literal ids required") from exc
                    if not isinstance(cases, (tuple, list)) or not isinstance(ids, (tuple, list)) or len(cases) != len(ids) or not ids:
                        raise CatalogError(f"{node.name}: parameter cases/ids mismatch")
                    if any(not isinstance(value, str) or not value or any(ch in value for ch in ("[", "]", "\n", "\r")) for value in ids) or len(set(ids)) != len(ids):
                        raise CatalogError(f"{node.name}: invalid or duplicate parameter ids")
                    suffixes = [f"{existing}-{label}" if existing else label for existing in suffixes for label in ids]
            for suffix in suffixes:
                if packet and recovery:
                    raise CatalogError(f"{path.name}:{node.name}: packet and release-recovery suites are exclusive")
                suite = "packet" if packet else "recovery" if recovery else "functional"
                level = "L3" if packet else "L7" if recovery else "L3"
                rows.append({"nodeid": path.relative_to(root).as_posix() + "::" + node.name + (f"[{suffix}]" if suffix else ""),
                             "suite": suite,
                             "level": level,
                             "contract": ast.get_docstring(node) or node.name})
    ids = [row["nodeid"] for row in rows]
    if not rows or len(ids) != len(set(ids)):
        raise CatalogError("acceptance catalog is empty or duplicated")
    return rows


def read_catalog(root: Path) -> list[dict[str, str]]:
    path = root / "tests/acceptance/scenarios.json"
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise CatalogError("scenario registry is unsafe or exceeds size bound")
    value = json.loads(path.read_text())
    if value.get("schema") != "fwrouter-acceptance-scenarios/v1":
        raise CatalogError("unsupported scenario registry")
    rows = collect_source_nodes(root)
    if value.get("scenarios") != rows:
        raise CatalogError("scenario registry differs from static source inventory; regenerate and review it")
    return rows


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate source-only registry; executes no tests")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if args.write:
        payload = {"schema": "fwrouter-acceptance-scenarios/v1", "scenarios": collect_source_nodes(root)}
        (root / "tests/acceptance/scenarios.json").write_text(json.dumps(payload, indent=2) + "\n")
    else:
        rows = read_catalog(root)
        print(f"PASS: {len(rows)} statically inventoried acceptance nodes")
