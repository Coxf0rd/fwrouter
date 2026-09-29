"""Mihomo YAML serialization with cross-resolver-safe string scalars."""

from __future__ import annotations

import re
from typing import Any

import yaml


_NUMERIC_SCALAR = re.compile(
    r"""
    [+-]?(?:
        0[xX][0-9a-fA-F_]+
      | 0[bB][01_]+
      | 0[oO][0-7_]+
      | 0[0-7_]+
      | (?:[0-9][0-9_]*)(?:\.[0-9_]*)?(?:[eE][+-]?[0-9_]+)?
      | \.[0-9_]+(?:[eE][+-]?[0-9_]+)?
    )
    """,
    re.VERBOSE,
)
_SPECIAL_SCALAR = re.compile(r"^(?:~|null|true|false|yes|no|on|off|\.inf|\.nan)$", re.IGNORECASE)


def needs_quoted_string(value: str) -> bool:
    """Return whether common YAML 1.1 resolvers may coerce this string."""
    return bool(_NUMERIC_SCALAR.fullmatch(value) or _SPECIAL_SCALAR.fullmatch(value))


_SafeDumper = getattr(yaml, "CSafeDumper", yaml.SafeDumper)


class MihomoSafeDumper(_SafeDumper):
    pass


def _represent_string(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = '"' if needs_quoted_string(value) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


MihomoSafeDumper.add_representer(str, _represent_string)


def dump_mihomo_config(config: dict[str, Any]) -> str:
    return yaml.dump(config, Dumper=MihomoSafeDumper, sort_keys=False)


__all__ = ["MihomoSafeDumper", "dump_mihomo_config", "needs_quoted_string"]
