"""Secret-free, CAS-published runtime snapshot storage.

``<data-root>/config/local.yaml`` is runtime-owned state, never an input to the
business configuration merge.  More than one lifecycle feature needs to record
facts there (R2 database/provider readiness and R4 active vector generation),
so a feature must not replace the whole mapping with just its own fields.

This module provides the narrow shared primitive: safe read, deep semantic
merge, and compare-and-swap atomic publication.  It deliberately does *not*
take a writer lease itself.  A product mutation boundary owns that lease so a
compound operation (build generation -> flip pointer) has one writer authority.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.runtime.errors import ErrorCode, PKVRuntimeError


_SECRET_KEY_PARTS = (
    "key",
    "apikey",
    "secret",
    "token",
    "password",
    "auth",
    "authorization",
    "cookie",
    "credential",
)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ValueError("运行态配置快照键必须是字符串")
        if key in mapping:
            raise ValueError("运行态配置快照包含重复键")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _normalized_key(value: object) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def contains_secret_shaped_field(value: object) -> bool:
    """Return whether a mapping contains a credential-shaped key at any depth.

    The check intentionally keys off structure rather than values: an internal
    snapshot must never become a second secret store, regardless of whether the
    value looks like a real credential today.
    """

    def visit(nested_value: object, ancestors: set[int]) -> bool:
        if isinstance(nested_value, Mapping):
            identity = id(nested_value)
            if identity in ancestors:
                # A cyclic alias is neither a safe snapshot nor a safe object to
                # recurse into.  Treat it as forbidden rather than overflowing.
                return True
            ancestors.add(identity)
            try:
                for key, child in nested_value.items():
                    normalized = _normalized_key(key)
                    if any(part in normalized for part in _SECRET_KEY_PARTS):
                        return True
                    if visit(child, ancestors):
                        return True
                return False
            finally:
                ancestors.remove(identity)
        if isinstance(nested_value, (list, tuple)):
            identity = id(nested_value)
            if identity in ancestors:
                return True
            ancestors.add(identity)
            try:
                return any(visit(item, ancestors) for item in nested_value)
            finally:
                ancestors.remove(identity)
        return False

    return visit(value, set())


def _safe_copy_value(value: object, *, ancestors: set[int]) -> Any:
    """Copy only JSON/YAML scalar trees and reject aliases that form cycles."""

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in ancestors:
            raise ValueError("运行态配置快照不得包含循环别名")
        ancestors.add(identity)
        try:
            copied: dict[str, Any] = {}
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise ValueError("运行态配置快照键必须是字符串")
                copied[key] = _safe_copy_value(nested, ancestors=ancestors)
            return copied
        finally:
            ancestors.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in ancestors:
            raise ValueError("运行态配置快照不得包含循环别名")
        ancestors.add(identity)
        try:
            return [_safe_copy_value(item, ancestors=ancestors) for item in value]
        finally:
            ancestors.remove(identity)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ValueError("运行态配置快照包含不受支持的值")


def _copy_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("运行态配置快照必须是映射对象")
    try:
        copied = _safe_copy_value(payload, ancestors=set())
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("运行态配置快照无法安全复制") from exc
    if not isinstance(copied, dict):
        raise ValueError("运行态配置快照必须是映射对象")
    if contains_secret_shaped_field(copied):
        raise ValueError("运行态配置快照不得包含敏感字段")
    return copied


def _deep_merge(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    """Merge a feature-owned extension without discarding other runtime facts."""

    merged = _copy_mapping(base)
    for key, value in update.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = _safe_copy_value(value, ancestors=set())
    if contains_secret_shaped_field(merged):
        raise ValueError("运行态配置快照不得包含敏感字段")
    return merged


@dataclass(frozen=True)
class RuntimeSnapshotDocument:
    """One immutable safe read plus the raw-byte CAS revision it observed."""

    payload: dict[str, Any]
    raw_sha256: str
    exists: bool

    def merged(self, update: Mapping[str, Any]) -> dict[str, Any]:
        """Return a deep semantic merge, leaving this observed document intact."""

        if not isinstance(update, Mapping):
            raise TypeError("运行态配置快照更新必须是映射对象")
        return _deep_merge(self.payload, update)


class RuntimeSnapshotStore:
    """Safe storage for the one per-data-root secret-free runtime snapshot."""

    def __init__(self, layout: Any) -> None:
        self._layout = layout
        self.path = Path(layout.runtime_config_path)

    def read(self) -> RuntimeSnapshotDocument:
        """Read without creating a root, directory, lock, or snapshot file."""

        try:
            target = self._layout.validate_user_file(
                self.path,
                label="运行时配置快照",
                allow_missing=True,
            )
            if not target.exists():
                return RuntimeSnapshotDocument({}, hashlib.sha256(b"").hexdigest(), False)
            with self._layout.open_user_file(
                target,
                "rb",
                label="运行时配置快照",
            ) as source:
                raw = source.read()
            loaded = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeySafeLoader)
            if not isinstance(loaded, Mapping):
                raise ValueError("运行态配置快照必须是映射对象")
            payload = _copy_mapping(loaded)
            return RuntimeSnapshotDocument(
                payload,
                hashlib.sha256(raw).hexdigest(),
                True,
            )
        except PKVRuntimeError:
            raise
        except (OSError, UnicodeError, ValueError, RecursionError, yaml.YAMLError) as exc:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "运行态配置快照无法安全读取。",
                stage="runtime_snapshot",
                recoverable=True,
            ) from exc

    def publish(
        self,
        expected: RuntimeSnapshotDocument,
        payload: Mapping[str, Any],
    ) -> RuntimeSnapshotDocument:
        """Atomically publish exactly one expected revision after caller approval.

        The final compare runs in ``atomic_publish_user_file`` immediately
        before the replace.  Thus a concurrent lifecycle writer never has its
        database facts silently lost by an embedding pointer flip.
        """

        if not isinstance(expected, RuntimeSnapshotDocument):
            raise TypeError("expected 必须由 RuntimeSnapshotStore.read 返回")
        safe_payload = _copy_mapping(payload)
        try:
            encoded = yaml.safe_dump(
                safe_payload,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError, yaml.YAMLError) as exc:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "运行态配置快照无法安全序列化。",
                stage="runtime_snapshot",
                recoverable=True,
            ) from exc

        def assert_expected_revision() -> None:
            current = self.read()
            if (
                current.exists != expected.exists
                or current.raw_sha256 != expected.raw_sha256
            ):
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "运行态配置快照在发布前已变化；请重新检查并生成计划。",
                    stage="runtime_snapshot",
                    recoverable=True,
                )

        try:
            self._layout.ensure_user_directories()
            target = self._layout.writable_user_path(
                self.path,
                label="运行时配置快照",
            )
            self._layout.atomic_publish_user_file(
                target,
                label="运行时配置快照",
                data=encoded,
                pre_replace=assert_expected_revision,
            )
            published = self.read()
            if published.payload != safe_payload:
                raise PKVRuntimeError(
                    ErrorCode.REPAIR_REQUIRED,
                    "运行态配置快照发布后校验失败。",
                    stage="runtime_snapshot",
                    recoverable=True,
                )
            return published
        except PKVRuntimeError:
            raise
        except (OSError, ValueError, RecursionError) as exc:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "运行态配置快照无法安全写入。",
                stage="runtime_snapshot",
                recoverable=True,
            ) from exc


__all__ = [
    "RuntimeSnapshotDocument",
    "RuntimeSnapshotStore",
    "contains_secret_shaped_field",
]
