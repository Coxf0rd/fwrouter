from __future__ import annotations

import os
import hashlib
import stat
from typing import Any

import yaml
from yaml.events import (
    AliasEvent,
    DocumentEndEvent,
    DocumentStartEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


# The production Mihomo config observed in the October 2026 qualification is
# about 20.8 MB. Keep a finite envelope with headroom for native ruleset scale.
MAX_MIHOMO_CONFIG_BYTES = 32 * 1024 * 1024
MAX_MIHOMO_YAML_NODES = 1_000_000
MAX_MIHOMO_YAML_DEPTH = 100


class MihomoYamlError(ValueError):
    """A YAML file could not be safely read or interpreted."""


class MihomoYamlLimitError(MihomoYamlError):
    """A Mihomo YAML file exceeds the supported bounded resource envelope."""


class MihomoYamlInvalidError(MihomoYamlError):
    """A Mihomo YAML file is ambiguous, invalid, or not JSON-compatible."""


def hash_mihomo_yaml_file(
    path: str | os.PathLike[str], *, max_bytes: int = MAX_MIHOMO_CONFIG_BYTES,
) -> str:
    """Hash one stable, bounded regular file without following symlinks.

    This is for generation/runtime fences that need a digest but do not need
    to materialize or parse the YAML document. It shares the byte envelope and
    path/descriptor stability checks with the parser reader.
    """
    target = os.fspath(path)
    try:
        path_before = os.lstat(target)
        if not stat.S_ISREG(path_before.st_mode) or stat.S_ISLNK(path_before.st_mode):
            raise MihomoYamlInvalidError("not_regular_file")
        if path_before.st_size > max_bytes:
            raise MihomoYamlLimitError("byte_limit")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(target, flags)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or (before.st_dev, before.st_ino) != (path_before.st_dev, path_before.st_ino)
            ):
                raise MihomoYamlInvalidError("file_changed_before_read")
            if before.st_size > max_bytes:
                raise MihomoYamlLimitError("byte_limit")
            digest = hashlib.sha256()
            total = 0
            while total <= max_bytes:
                chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise MihomoYamlLimitError("byte_limit")
                digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)

        path_after = os.lstat(target)
        stable = lambda value: (
            value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns,
        )
        if (
            total != before.st_size
            or stable(before) != stable(after)
            or stable(after) != stable(path_after)
            or stat.S_ISLNK(path_after.st_mode)
            or not stat.S_ISREG(path_after.st_mode)
        ):
            raise MihomoYamlInvalidError("file_changed_during_read")
        return digest.hexdigest()
    except MihomoYamlError:
        raise
    except OSError:
        raise MihomoYamlInvalidError("file_unavailable") from None


def read_mihomo_yaml_text(
    path: str | os.PathLike[str], *, max_bytes: int = MAX_MIHOMO_CONFIG_BYTES,
) -> str:
    """Read bounded stable UTF-8 text without exposing decoder details."""
    raw = read_mihomo_yaml_bytes(path, max_bytes=max_bytes)
    try:
        return raw.decode("utf-8")
    except UnicodeError:
        raise MihomoYamlInvalidError("invalid_utf8") from None


def read_mihomo_yaml_bytes(path: str | os.PathLike[str], *, max_bytes: int = MAX_MIHOMO_CONFIG_BYTES) -> bytes:
    """Read one stable regular file without following symlinks or exceeding max_bytes."""
    target = os.fspath(path)
    try:
        path_before = os.lstat(target)
        if not stat.S_ISREG(path_before.st_mode) or stat.S_ISLNK(path_before.st_mode):
            raise MihomoYamlInvalidError("not_regular_file")
        if path_before.st_size > max_bytes:
            raise MihomoYamlLimitError("byte_limit")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(target, flags)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or (before.st_dev, before.st_ino) != (path_before.st_dev, path_before.st_ino)
            ):
                raise MihomoYamlInvalidError("file_changed_before_read")
            if before.st_size > max_bytes:
                raise MihomoYamlLimitError("byte_limit")

            chunks: list[bytes] = []
            total = 0
            while total <= max_bytes:
                chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > max_bytes:
                raise MihomoYamlLimitError("byte_limit")
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)

        path_after = os.lstat(target)
        stable = lambda value: (
            value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns,
        )
        if (
            len(raw) != before.st_size
            or stable(before) != stable(after)
            or stable(after) != stable(path_after)
            or stat.S_ISLNK(path_after.st_mode)
            or not stat.S_ISREG(path_after.st_mode)
        ):
            raise MihomoYamlInvalidError("file_changed_during_read")
        return raw
    except MihomoYamlError:
        raise
    except OSError:
        raise MihomoYamlInvalidError("file_unavailable") from None


def _json_safe(value: Any) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if current is None or isinstance(current, (str, bool, int)):
            continue
        if isinstance(current, float):
            if current != current or current in (float("inf"), float("-inf")):
                return False
            continue
        if isinstance(current, list):
            pending.extend(current)
            continue
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str):
                    return False
                pending.append(item)
            continue
        return False
    return True


def _parse_bounded_yaml_mapping(raw: bytes) -> dict[str, Any]:
    """Parse a JSON-compatible YAML mapping with early event, alias and key bounds.

    A streaming event pass rejects aliases, duplicate/non-string mapping keys,
    excess depth, and excess nodes before the SafeLoader constructs application
    dictionaries. The second parse is bounded by the same in-memory byte string;
    no YAML node tree is retained alongside the constructed result.
    """
    if len(raw) > MAX_MIHOMO_CONFIG_BYTES:
        raise MihomoYamlLimitError("byte_limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise MihomoYamlInvalidError("invalid_utf8") from None

    loader_type = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    resolver = yaml.resolver.Resolver()
    frames: list[dict[str, Any]] = []
    nodes = 0
    documents = 0
    root_seen = False

    def count_node() -> int:
        nonlocal nodes, root_seen
        depth = frames[-1]["depth"] + 1 if frames else 0
        nodes += 1
        if nodes > MAX_MIHOMO_YAML_NODES or depth > MAX_MIHOMO_YAML_DEPTH:
            raise MihomoYamlLimitError("node_or_depth_limit")
        if not frames:
            if root_seen:
                raise MihomoYamlInvalidError("multiple_root_nodes")
            root_seen = True
        return depth

    try:
        for event in yaml.parse(text, Loader=loader_type):
            if isinstance(event, AliasEvent):
                raise MihomoYamlInvalidError("aliases_unsupported")
            if isinstance(event, DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise MihomoYamlInvalidError("multiple_documents")
                continue
            if isinstance(event, DocumentEndEvent):
                if frames:
                    raise MihomoYamlInvalidError("unclosed_collection")
                continue
            if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
                if not frames and isinstance(event, SequenceStartEvent):
                    raise MihomoYamlInvalidError("root_mapping_required")
                depth = count_node()
                expected_tag = (
                    "tag:yaml.org,2002:map"
                    if isinstance(event, MappingStartEvent)
                    else "tag:yaml.org,2002:seq"
                )
                resolved_tag = event.tag or resolver.resolve(
                    MappingNode if isinstance(event, MappingStartEvent) else SequenceNode,
                    None,
                    event.implicit,
                )
                if resolved_tag != expected_tag:
                    raise MihomoYamlInvalidError("unsupported_collection_tag")
                if frames and frames[-1]["kind"] == "mapping":
                    parent = frames[-1]
                    if parent["expect_key"]:
                        raise MihomoYamlInvalidError("non_scalar_mapping_key")
                    parent["expect_key"] = True
                frames.append({
                    "kind": "mapping" if isinstance(event, MappingStartEvent) else "sequence",
                    "depth": depth,
                    "expect_key": True,
                    "keys": set(),
                })
                continue
            if isinstance(event, (MappingEndEvent, SequenceEndEvent)):
                if not frames:
                    raise MihomoYamlInvalidError("unexpected_collection_end")
                frame = frames.pop()
                expected_kind = "mapping" if isinstance(event, MappingEndEvent) else "sequence"
                if frame["kind"] != expected_kind or not frame["expect_key"]:
                    raise MihomoYamlInvalidError("invalid_collection_shape")
                continue
            if isinstance(event, ScalarEvent):
                count_node()
                if not frames:
                    raise MihomoYamlInvalidError("root_mapping_required")
                if frames and frames[-1]["kind"] == "mapping" and frames[-1]["expect_key"]:
                    tag = event.tag or resolver.resolve(ScalarNode, event.value, event.implicit)
                    if tag != "tag:yaml.org,2002:str":
                        raise MihomoYamlInvalidError("non_string_mapping_key")
                    frame = frames[-1]
                    if event.value in frame["keys"]:
                        raise MihomoYamlInvalidError("duplicate_mapping_key")
                    frame["keys"].add(event.value)
                    frame["expect_key"] = False
                elif frames and frames[-1]["kind"] == "mapping":
                    frames[-1]["expect_key"] = True
                continue
        if documents != 1 or not root_seen or frames:
            raise MihomoYamlInvalidError("root_mapping_required")
    except MihomoYamlError:
        raise
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        # Parser exception text may embed source snippets, including secrets.
        raise MihomoYamlInvalidError("invalid_yaml") from None

    try:
        value = yaml.load(text, Loader=loader_type)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError):
        # Do not propagate parser messages that can contain raw config values.
        raise MihomoYamlInvalidError("invalid_yaml") from None
    if not isinstance(value, dict):
        raise MihomoYamlInvalidError("root_mapping_required")
    if not _json_safe(value):
        raise MihomoYamlInvalidError("non_json_value")
    return value


def load_mihomo_yaml_mapping(
    path: str | os.PathLike[str], *, max_bytes: int = MAX_MIHOMO_CONFIG_BYTES,
) -> dict[str, Any]:
    raw = read_mihomo_yaml_bytes(path, max_bytes=max_bytes)
    return _parse_bounded_yaml_mapping(raw)


def parse_mihomo_yaml_mapping(raw: bytes) -> dict[str, Any]:
    return _parse_bounded_yaml_mapping(raw)
