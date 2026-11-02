"""Compatibility-only aliases for historical module attribute patch points.

Implementation code imports its collaborators directly. Only compatibility
surfaces use this helper; it owns no operation, configuration or persistent
state. Alias targets are explicit consumer-local namespaces, never discovered
by scanning loaded modules. Direct dictionary writes and concurrent hot-patching
are not supported configuration mechanisms.
"""
from __future__ import annotations

from types import ModuleType
from typing import Any
import sys


AttributeAliases = dict[str, tuple[tuple[dict[str, Any], str], ...]]


class _AttributeAliasModule(ModuleType):
    def __getattribute__(self, name: str) -> Any:
        namespace = ModuleType.__getattribute__(self, "__dict__")
        targets = namespace.get("_attribute_aliases", {}).get(name)
        if targets:
            target, key = targets[0]
            try:
                return target[key]
            except KeyError:
                raise AttributeError(name) from None
        return ModuleType.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        namespace = ModuleType.__getattribute__(self, "__dict__")
        for target, key in namespace.get("_attribute_aliases", {}).get(name, ()):
            target[key] = value
        ModuleType.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        namespace = ModuleType.__getattribute__(self, "__dict__")
        for target, key in namespace.get("_attribute_aliases", {}).get(name, ()):
            target.pop(key, None)
        ModuleType.__delattr__(self, name)


def install_attribute_aliases(module_name: str, aliases: AttributeAliases) -> None:
    """Attach explicit aliases after the compatibility module is initialized."""
    module = sys.modules[module_name]
    for name, targets in aliases.items():
        if not targets:
            raise ValueError(f"Empty compatibility alias: {name}")
        module.__dict__[name] = targets[0][0][targets[0][1]]
    module.__dict__["_attribute_aliases"] = aliases
    module.__class__ = _AttributeAliasModule


def attribute_alias_view(name: str, aliases: AttributeAliases) -> ModuleType:
    """Create an unregistered compatibility view, without an implementation import."""
    module = _AttributeAliasModule(name)
    for field, targets in aliases.items():
        if not targets:
            raise ValueError(f"Empty compatibility alias: {field}")
        module.__dict__[field] = targets[0][0][targets[0][1]]
    module.__dict__["_attribute_aliases"] = aliases
    return module
