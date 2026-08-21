"""Explicit, side-effect-bounded runtime lifecycle contracts.

The legacy :func:`src.runtime.bootstrap.bootstrap_runtime` entry point remains
the compatibility bootstrap path.  This module is deliberately separate: it
first inspects a runtime without changing it, then produces a transparent plan,
and only performs an approved plan after re-checking the inspected revision.

No object returned by ``inspect_runtime`` or ``plan_runtime`` contains local
paths, provider credentials, raw journal data, or provider responses.  This is
important because CLI, MCP, and future wrappers may serialize these objects.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from src.ai.provider_factory import (
    ChatProviderSettings,
    EmbeddingProviderSettings,
    chat_settings_from_config,
    embedding_settings_from_config,
)
from src.runtime.bootstrap import RuntimeContext, bootstrap_runtime
from src.runtime.errors import ErrorCode, OperationStatus, PKVRuntimeError
from src.runtime.layout import open_user_file_nofollow
from src.storage.migration_manager import (
    DatabaseInspection,
    DatabaseState,
    MigrationManager,
)


class RuntimeReadiness(str, Enum):
    """A side-effect-free interpretation of the current runtime state."""

    READY = "ready"
    SETUP_REQUIRED = "setup_required"
    REPAIR_REQUIRED = "repair_required"
    UPGRADE_REQUIRED = "upgrade_required"
    DEGRADED = "degraded"


class RuntimeIssueSeverity(str, Enum):
    """Stable severity labels intended for adapters, not end-user prose."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class RuntimeActionKind(str, Enum):
    """Explicit actions a runtime lifecycle plan can describe."""

    CONFIGURE_PROVIDERS = "configure_providers"
    VALIDATE_PROVIDERS = "validate_providers"
    INITIALIZE_FRESH = "initialize_fresh"
    RECOVER_JOURNAL = "recover_journal"
    REPAIR_RUNTIME = "repair_runtime"
    UPGRADE_DATABASE = "upgrade_database"
    RECORD_RUNTIME_SNAPSHOT = "record_runtime_snapshot"


@dataclass(frozen=True)
class RuntimeIssue:
    """One safe-to-serialize finding from a readonly runtime inspection."""

    code: str
    severity: RuntimeIssueSeverity
    message: str
    recoverable: bool
    next_action: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "recoverable": self.recoverable,
        }
        if self.next_action is not None:
            payload["next_action"] = self.next_action
        return payload


@dataclass(frozen=True)
class ProviderValidation:
    """Structural and actual provider validation are intentionally distinct."""

    llm_structural: str
    embedding_structural: str
    llm_actual: str = "not_requested"
    embedding_actual: str = "not_requested"
    targets: tuple[str, ...] = ()
    embedding_dimension: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "llm_structural": self.llm_structural,
            "embedding_structural": self.embedding_structural,
            "llm_actual": self.llm_actual,
            "embedding_actual": self.embedding_actual,
            "targets": list(self.targets),
            "embedding_dimension": self.embedding_dimension,
        }


@dataclass(frozen=True)
class RuntimeInspection:
    """Immutable, serializable result of ``inspect_runtime``.

    ``_config`` is intentionally private and omitted from comparisons and
    serialization.  It lets the matching plan re-inspect the exact explicit
    Config object without falling back to the process-global Config singleton.
    """

    readiness: RuntimeReadiness
    revision: str
    database_state: str
    issues: tuple[RuntimeIssue, ...]
    provider_validation: ProviderValidation
    journal_record_count: int
    runtime_snapshot: str
    _config: Any = field(repr=False, compare=False, default=None)

    def to_dict(self) -> dict[str, object]:
        return {
            "readiness": self.readiness.value,
            "revision": self.revision,
            "database_state": self.database_state,
            "issues": [issue.to_dict() for issue in self.issues],
            "provider_validation": self.provider_validation.to_dict(),
            "journal_record_count": self.journal_record_count,
            "runtime_snapshot": self.runtime_snapshot,
        }


@dataclass(frozen=True)
class RuntimeAction:
    """One action proposed by a lifecycle plan, including its impact boundary."""

    kind: RuntimeActionKind
    impact: str
    requires_confirmation: bool
    requires_network: bool
    executable: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "impact": self.impact,
            "requires_confirmation": self.requires_confirmation,
            "requires_network": self.requires_network,
            "executable": self.executable,
        }


@dataclass(frozen=True)
class RuntimePlan:
    """A readonly plan bound to one inspection revision."""

    plan_id: str
    inspection: RuntimeInspection
    actions: tuple[RuntimeAction, ...]
    _config: Any = field(repr=False, compare=False, default=None)

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "inspection": self.inspection.to_dict(),
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass(frozen=True)
class RuntimeConfirmation:
    """An explicit acknowledgement of one concrete lifecycle plan."""

    plan_id: str
    approved_actions: frozenset[str] = frozenset()
    allow_network: bool = False

    def __post_init__(self) -> None:
        if type(self.allow_network) is not bool:
            raise TypeError("allow_network 必须是 bool")
        normalized = frozenset(
            item.value if isinstance(item, RuntimeActionKind) else str(item)
            for item in self.approved_actions
        )
        object.__setattr__(self, "approved_actions", normalized)

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "approved_actions": sorted(self.approved_actions),
            "allow_network": self.allow_network,
        }


@dataclass(frozen=True)
class RuntimeExecution:
    """Result of an approved lifecycle execution.

    ``context`` is intentionally not serialized; it is only present after a
    successful fresh initialization and is for the immediate in-process caller.
    """

    inspection: RuntimeInspection
    provider_validation: ProviderValidation
    context: RuntimeContext | None = field(repr=False, compare=False, default=None)
    # The confirmed/revalidated Config is intentionally private.  It is used by
    # the public Kernel lifecycle facade to compose a Kernel only after a
    # successful execution; it must never enter the serializable DTO.
    _config: Any = field(repr=False, compare=False, default=None)

    def to_dict(self) -> dict[str, object]:
        return {
            "inspection": self.inspection.to_dict(),
            "provider_validation": self.provider_validation.to_dict(),
            "context_created": self.context is not None,
        }


class ProviderProbe(Protocol):
    """Network boundary used only by approved ``validate_providers`` actions."""

    def probe_llm(self, settings: ChatProviderSettings) -> None:
        """Perform the smallest meaningful LLM health probe."""

    def probe_embedding(self, settings: EmbeddingProviderSettings) -> int | None:
        """Perform the smallest meaningful embedding health probe."""


class LiveProviderProbe:
    """The production provider probe; it is never instantiated by inspection."""

    def __init__(self, layout: Any) -> None:
        self._layout = layout

    def probe_llm(self, settings: ChatProviderSettings) -> None:
        from src.ai.deepseek_client import DeepSeekClient

        client = DeepSeekClient(settings=settings, layout=self._layout)
        client._call_api(
            [{"role": "user", "content": "PKV runtime health probe"}],
            temperature=0.0,
            max_tokens=1,
        )

    def probe_embedding(self, settings: EmbeddingProviderSettings) -> int | None:
        from src.ai.openai_client import OpenAIClient

        embedding = OpenAIClient(settings=settings).embed("__pkv_runtime_health_probe__")
        return len(embedding)


WriterLeaseFactory = Callable[[Any], AbstractContextManager[object]]
_WRITER_LEASE_FILENAME = "write.lease"
_RUNTIME_SNAPSHOT_SCHEMA_VERSION = 1
_MAX_EMBEDDING_DIMENSION = 65_536
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]*\Z")
_SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z"
)

# Lifecycle plans are deliberately process-bound (they also hold an explicit
# Config object rather than a serializable config payload).  A process-private
# HMAC key lets the revision react to credential rotation without returning a
# reusable hash of an API key in an inspection, plan, log, or snapshot.
_REVISION_FINGERPRINT_KEY = os.urandom(32)


def _issue(
    code: ErrorCode | str,
    severity: RuntimeIssueSeverity,
    message: str,
    *,
    recoverable: bool = True,
    next_action: RuntimeActionKind | None = None,
) -> RuntimeIssue:
    return RuntimeIssue(
        code=code.value if isinstance(code, ErrorCode) else code,
        severity=severity,
        message=message,
        recoverable=recoverable,
        next_action=next_action.value if next_action is not None else None,
    )


def _safe_origin(value: str) -> str:
    """Return a credential-free origin suitable for an inspection response."""

    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            return "configured_endpoint"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"
    except (TypeError, ValueError):
        return "configured_endpoint"


def _safe_hash(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _revision_hmac(value: object) -> str:
    """Return a process-private revision marker without exposing ``value``.

    This is intentionally not a durable configuration or embedding fingerprint:
    it only protects the short-lived inspect/plan/execute comparison.  In
    particular, callers must not be able to use an inspection's public
    ``revision`` as an oracle for an API key.
    """

    encoded = str(value).encode("utf-8")
    return hmac.new(_REVISION_FINGERPRINT_KEY, encoded, hashlib.sha256).hexdigest()


def _provider_fingerprint(
    llm_settings: ChatProviderSettings | None,
    embedding_settings: EmbeddingProviderSettings | None,
) -> str:
    """Return a secret-safe provider revision input.

    The public data-runtime/embedding contract intentionally excludes API keys:
    rotating a key must not itself require rebuilding an index.  An *approved
    lifecycle plan*, however, may make a live health probe.  It must therefore
    become stale when either credential changes, so that the confirmation never
    silently authorizes a probe with different credentials.
    """

    def _settings_payload(settings: Any) -> dict[str, object] | None:
        if settings is None:
            return None
        payload = {
            "provider": settings.provider,
            # Keep potentially credential-bearing endpoint components and API
            # keys inside a process-private HMAC.  Only the resulting final
            # revision is exposed, never these individual values.
            "base_url_hmac": _revision_hmac(settings.base_url),
            "model": settings.model,
            "timeout_seconds": settings.timeout_seconds,
            "max_retries": settings.max_retries,
            "api_key_hmac": _revision_hmac(settings.api_key),
        }
        if isinstance(settings, ChatProviderSettings):
            payload["max_tokens"] = settings.max_tokens
            payload["temperature"] = settings.temperature
        else:
            payload["dimensions"] = settings.dimensions
        return payload

    return _safe_hash(
        {
            "llm": _settings_payload(llm_settings),
            "embedding": _settings_payload(embedding_settings),
        }
    )


def _inspect_providers(
    config: Any,
) -> tuple[ProviderValidation, tuple[RuntimeIssue, ...], str, ChatProviderSettings | None, EmbeddingProviderSettings | None]:
    issues: list[RuntimeIssue] = []
    targets: list[str] = []
    llm_settings: ChatProviderSettings | None = None
    embedding_settings: EmbeddingProviderSettings | None = None

    try:
        llm_settings = chat_settings_from_config(config)
        targets.append(_safe_origin(llm_settings.base_url))
        llm_state = "valid"
    except (PKVRuntimeError, TypeError, ValueError):
        llm_state = "invalid"
        issues.append(
            _issue(
                ErrorCode.PROVIDER_CONFIG_INVALID,
                RuntimeIssueSeverity.ERROR,
                "LLM Provider 配置不完整或不符合结构合同。",
                next_action=RuntimeActionKind.CONFIGURE_PROVIDERS,
            )
        )

    try:
        embedding_settings = embedding_settings_from_config(config)
        # ``dim: auto`` is structurally valid but must be resolved by the
        # confirmed live probe before we can publish a vector/runtime contract.
        # A plain missing dimension is not the same thing and cannot establish
        # an index-compatible snapshot.
        if embedding_settings.dimensions is None and getattr(
            config, "embedding_dim_is_auto", False
        ) is not True:
            raise ValueError("embedding dimension is neither declared nor auto")
        targets.append(_safe_origin(embedding_settings.base_url))
        embedding_state = "valid"
    except (PKVRuntimeError, TypeError, ValueError):
        embedding_state = "invalid"
        issues.append(
            _issue(
                ErrorCode.PROVIDER_CONFIG_INVALID,
                RuntimeIssueSeverity.ERROR,
                "Embedding Provider 配置不完整或不符合结构合同。",
                next_action=RuntimeActionKind.CONFIGURE_PROVIDERS,
            )
        )

    return (
        ProviderValidation(
            llm_structural=llm_state,
            embedding_structural=embedding_state,
            targets=tuple(sorted(set(targets))),
            embedding_dimension=(
                embedding_settings.dimensions
                if embedding_settings is not None
                else None
            ),
        ),
        tuple(issues),
        _provider_fingerprint(llm_settings, embedding_settings),
        llm_settings,
        embedding_settings,
    )


def _path_marker(path: Path) -> dict[str, object]:
    """Return unexported file identity metadata for stale-plan detection."""

    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {"exists": False}
    except OSError:
        return {"state": "undetermined"}
    return {
        "exists": True,
        "mode": stat.S_IFMT(info.st_mode),
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "device": info.st_dev,
        "inode": info.st_ino,
    }


def _config_source_marker(config: Any) -> dict[str, object]:
    """Capture an opaque revision of the editable config source.

    ``Config`` is an immutable execution snapshot, so provider settings in the
    captured object never silently change midway through an operation.  A plan
    has not begun an in-flight operation yet, though: if its editable user
    config file is atomically replaced before execute, the user must reconfirm
    the fresh plan rather than authorize a live probe with a superseded key.
    Production ``Config`` owns a process-private HMAC over the source bytes.
    It detects even a same-size/same-timestamp replacement without exposing a
    path, setting, or credential.  The metadata fallback exists only for narrow
    Config-like test/admin seams predating that API.
    """

    source_revision = getattr(config, "user_config_source_revision", None)
    if callable(source_revision):
        # A production Config deliberately lets no-follow/path failures escape.
        # Inspection turns those into a repair-required finding below; treating
        # an unreadable source as merely "unchanged" would let a confirmed
        # Provider probe cross an unsafe configuration boundary.
        value = source_revision()
        if type(value) is not str or _SHA256_HEX.fullmatch(value) is None:
            raise ValueError("用户配置源 revision 不符合 opaque HMAC 合同")
        return {"revision": value}

    try:
        candidate = getattr(config, "user_config_path", None)
        if candidate is None:
            return {"state": "unavailable"}
        return _path_marker(Path(candidate))
    except (OSError, TypeError, ValueError, PKVRuntimeError):
        return {"state": "undetermined"}


def _fresh_data_root(layout: Any) -> tuple[bool, str]:
    """Strictly distinguish an absent/empty new root from partial legacy state."""

    root = Path(layout.user_data_root)
    if not os.path.lexists(root):
        return True, "absent"
    try:
        # RuntimeLayout's contained-directory gateway deliberately rejects the
        # root itself as a child.  Validating one declared child still checks
        # the root's type/link safety without creating that child.
        layout.validate_user_directory(
            root / "runtime",
            label="运行时目录",
            allow_missing=True,
        )
        entries = tuple(sorted(root.iterdir(), key=lambda path: path.name))
    except (OSError, PKVRuntimeError):
        return False, "unsafe_or_unreadable"
    if len(entries) == 1 and entries[0].name == "runtime":
        runtime_dir = entries[0]
        try:
            layout.validate_user_directory(
                runtime_dir,
                label="运行时目录",
                allow_missing=False,
            )
            runtime_entries = tuple(sorted(runtime_dir.iterdir(), key=lambda path: path.name))
            if len(runtime_entries) == 1 and runtime_entries[0].name == _WRITER_LEASE_FILENAME:
                layout.validate_user_file(
                    runtime_entries[0],
                    label="知识库写入锁文件",
                    allow_missing=False,
                )
                return True, "lease_only"
        except (OSError, PKVRuntimeError):
            return False, "unsafe_or_unreadable"
    return not entries, "empty" if not entries else "nonempty"


def _inspect_database(layout: Any) -> tuple[DatabaseInspection | None, tuple[RuntimeIssue, ...]]:
    try:
        manager = MigrationManager(
            layout.db_path,
            layout.migrations_dir,
            read_only=True,
            backup_dir=layout.backup_dir,
        )
        return manager.inspect_database(), ()
    except PKVRuntimeError as exc:
        return (
            None,
            (
                _issue(
                    exc.code,
                    RuntimeIssueSeverity.ERROR,
                    "数据库状态无法安全确认，需要修复。",
                    next_action=RuntimeActionKind.REPAIR_RUNTIME,
                ),
            ),
        )
    except (OSError, ValueError):
        return (
            None,
            (
                _issue(
                    ErrorCode.REPAIR_REQUIRED,
                    RuntimeIssueSeverity.ERROR,
                    "数据库状态无法安全确认，需要修复。",
                    next_action=RuntimeActionKind.REPAIR_RUNTIME,
                ),
            ),
        )


def _inspect_tokenizer_cache(
    layout: Any,
    *,
    database: DatabaseInspection | None,
) -> tuple[bool, tuple[RuntimeIssue, ...], dict[str, object]]:
    """Check the durable jieba cache needed by explicit-config readers.

    This remains fully readonly: it does not construct ``TextProcessor`` or
    touch jieba's process-global tokenizer.  A new root has no cache until its
    confirmed bootstrap, so the cache becomes a repair requirement only after
    the database itself is READY.  That makes lifecycle inspection agree with
    the later BM25/evidence reader instead of reporting a misleading READY.
    """

    cache_path = Path(layout.tmp_dir) / "jieba.cache"
    marker = {"path": _path_marker(cache_path)}
    if database is None or database.state is not DatabaseState.READY:
        return True, (), marker
    try:
        validated_cache = layout.validate_user_file(
            cache_path,
            label="jieba 运行态缓存",
            allow_missing=True,
        )
        if validated_cache.is_file():
            return True, (), marker
    except (OSError, TypeError, ValueError, PKVRuntimeError):
        pass
    return (
        False,
        (
            _issue(
                ErrorCode.REPAIR_REQUIRED,
                RuntimeIssueSeverity.ERROR,
                "数据库已就绪但 jieba 运行态缓存缺失或不安全；请先通过确认的运行时修复恢复。",
                next_action=RuntimeActionKind.REPAIR_RUNTIME,
            ),
        ),
        marker,
    )


def _inspect_journal(layout: Any) -> tuple[int, bool, bool, tuple[RuntimeIssue, ...], tuple[dict[str, object], ...]]:
    """Read journal records without constructing ``StorageOperationJournal``.

    Its constructor creates a directory, so inspection must only use its static
    validation helpers and contained no-follow reads.
    """

    from src.storage.coordinator import StorageOperationJournal

    journal_dir = Path(layout.runtime_state_dir) / "operations"
    try:
        layout.validate_user_directory(
            journal_dir,
            label="operation journal 目录",
            allow_missing=True,
        )
        if not os.path.lexists(journal_dir):
            return 0, False, False, (), ()
        candidates = tuple(sorted(journal_dir.iterdir(), key=lambda path: path.name))
    except (OSError, PKVRuntimeError):
        issue = _issue(
            ErrorCode.REPAIR_REQUIRED,
            RuntimeIssueSeverity.ERROR,
            "操作日志状态无法安全确认，需要修复。",
            next_action=RuntimeActionKind.RECOVER_JOURNAL,
        )
        return 0, True, False, (issue,), ({"state": "unreadable"},)

    blocking = False
    degraded = False
    issues: list[RuntimeIssue] = []
    marker: list[dict[str, object]] = []
    for path in candidates:
        record_state = "valid"
        status = "unknown"
        try:
            if path.suffix != ".json":
                raise OSError("legacy journal entry")
            StorageOperationJournal._validate_record_file(path)
            with open_user_file_nofollow(
                path,
                "r",
                label="operation journal 记录",
                encoding="utf-8",
            ) as handle:
                payload = json.load(handle)
            valid, reason = StorageOperationJournal._validate_payload(path, payload)
            if valid is None:
                raise ValueError(reason or "invalid journal record")
            status = str(valid["status"])
            if status in {"in_progress", OperationStatus.REPAIR_REQUIRED.value}:
                blocking = True
                record_state = "repair_required"
            elif status == OperationStatus.DEGRADED.value:
                degraded = True
                record_state = "degraded"
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError, PKVRuntimeError):
            blocking = True
            record_state = "invalid"
        marker.append(
            {
                "file": _path_marker(path),
                "record_state": record_state,
                "status": status,
            }
        )

    if blocking:
        issues.append(
            _issue(
                ErrorCode.REPAIR_REQUIRED,
                RuntimeIssueSeverity.ERROR,
                "检测到未完成、异常或旧版操作日志，需要显式修复。",
                next_action=RuntimeActionKind.RECOVER_JOURNAL,
            )
        )
    elif degraded:
        issues.append(
            _issue(
                ErrorCode.REPAIR_REQUIRED,
                RuntimeIssueSeverity.WARNING,
                "检测到降级的操作记录；读取可继续，写入前应完成修复。",
                next_action=RuntimeActionKind.REPAIR_RUNTIME,
            )
        )
    return len(candidates), blocking, degraded, tuple(issues), tuple(marker)


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


def _snapshot_contains_secret(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = "".join(ch for ch in str(key).casefold() if ch.isalnum())
            if any(part in normalized for part in _SECRET_KEY_PARTS):
                return True
            if _snapshot_contains_secret(nested):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_snapshot_contains_secret(item) for item in value)
    return False


def _require_snapshot_mapping(
    value: object,
    *,
    label: str,
    required_keys: frozenset[str],
    optional_keys: frozenset[str] = frozenset(),
    allow_mapping_extensions: bool = False,
) -> Mapping[str, object]:
    """Validate one exact, serializable mapping node of runtime snapshot v1."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{label}必须是映射")
    try:
        keys = frozenset(value.keys())
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{label}键集合无效") from exc
    allowed_keys = required_keys | optional_keys
    extension_keys = keys - allowed_keys
    if any(type(key) is not str for key in keys) or not required_keys.issubset(keys):
        raise ValueError(f"{label}字段不符合 schema")
    if extension_keys and not allow_mapping_extensions:
        raise ValueError(f"{label}字段不符合 schema")
    if extension_keys and any(
        not isinstance(value.get(key), Mapping) for key in extension_keys
    ):
        raise ValueError(f"{label}扩展字段必须是映射")
    return value


def _validate_runtime_snapshot_payload(value: object) -> Mapping[str, object]:
    """Fail closed unless a secret-free runtime snapshot exactly matches v1.

    ``Config`` owns safe no-follow YAML I/O; lifecycle owns the meaning of the
    data-runtime contract.  Keeping this check here means an older Config
    helper that merely accepts a YAML mapping cannot accidentally make an
    incomplete/foreign ``local.yaml`` look READY.
    """

    root = _require_snapshot_mapping(
        value,
        label="运行时配置快照",
        required_keys=frozenset({"schema_version", "database", "embedding"}),
        # R4 owns the semantic validation of this named extension.  Lifecycle
        # v1 only verifies that it remains a secret-free mapping and never
        # discards it when publishing an R2 base refresh.
        optional_keys=frozenset({"embedding_index"}),
        allow_mapping_extensions=True,
    )
    if _snapshot_contains_secret(root):
        raise ValueError("运行时配置快照包含敏感字段")
    if root.get("schema_version") != _RUNTIME_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("运行时配置快照版本不受支持")
    extension = root.get("embedding_index")
    if extension is not None and not isinstance(extension, Mapping):
        raise ValueError("运行时配置快照.embedding_index 必须是映射")

    database = _require_snapshot_mapping(
        root.get("database"),
        label="运行时配置快照.database",
        required_keys=frozenset({"schema_version"}),
    )
    database_schema_version = database.get("schema_version")
    if (
        type(database_schema_version) is not str
        or _SEMVER.fullmatch(database_schema_version) is None
    ):
        raise ValueError("运行时配置快照.database.schema_version 无效")

    embedding = _require_snapshot_mapping(
        root.get("embedding"),
        label="运行时配置快照.embedding",
        required_keys=frozenset({"provider", "fingerprint"}),
        optional_keys=frozenset({"generation"}),
        allow_mapping_extensions=True,
    )
    provider = embedding.get("provider")
    if type(provider) is not str or not provider.strip():
        raise ValueError("运行时配置快照.embedding.provider 无效")

    fingerprint = _require_snapshot_mapping(
        embedding.get("fingerprint"),
        label="运行时配置快照.embedding.fingerprint",
        required_keys=frozenset(
            {"base_url_sha256", "embedding_model", "embedding_dim"}
        ),
    )
    endpoint_hash = fingerprint.get("base_url_sha256")
    model = fingerprint.get("embedding_model")
    dimension = fingerprint.get("embedding_dim")
    if type(endpoint_hash) is not str or _SHA256_HEX.fullmatch(endpoint_hash) is None:
        raise ValueError("运行时配置快照.embedding.fingerprint.base_url_sha256 无效")
    if type(model) is not str or not model.strip():
        raise ValueError("运行时配置快照.embedding.fingerprint.embedding_model 无效")
    if (
        type(dimension) is not str
        or _POSITIVE_DECIMAL.fullmatch(dimension) is None
        or not 1 <= int(dimension) <= _MAX_EMBEDDING_DIMENSION
    ):
        raise ValueError("运行时配置快照.embedding.fingerprint.embedding_dim 无效")
    generation = embedding.get("generation")
    if generation is not None and not isinstance(generation, Mapping):
        raise ValueError("运行时配置快照.embedding.generation 必须是映射")
    return root


def _embedding_snapshot_fingerprint(
    config: Any,
    settings: EmbeddingProviderSettings,
    dimension: int,
) -> dict[str, object]:
    """Return the exact secret-free v1 embedding contract representation."""

    candidate: Mapping[str, object] | None = None
    index_fingerprint = getattr(config, "embedding_index_fingerprint", None)
    if callable(index_fingerprint):
        supplied = index_fingerprint(dimension)
        if isinstance(supplied, Mapping):
            candidate = supplied
    if candidate is None:
        # Production Config supplies ``embedding_index_fingerprint`` and uses
        # endpoint_contract_sha256.  This narrow fallback keeps explicit
        # Config-like test/admin seams usable without returning a raw endpoint.
        candidate = {
            "base_url_sha256": _safe_hash(settings.base_url),
            "embedding_model": settings.model,
            "embedding_dim": str(dimension),
        }
    try:
        return {
            "base_url_sha256": candidate["base_url_sha256"],
            "embedding_model": candidate["embedding_model"],
            "embedding_dim": candidate["embedding_dim"],
        }
    except (KeyError, TypeError) as exc:
        raise ValueError("Embedding 运行态指纹不完整") from exc


def _runtime_snapshot_contract_state(
    payload: Mapping[str, object],
    *,
    config: Any,
    database: DatabaseInspection | None,
    embedding_settings: EmbeddingProviderSettings | None,
) -> str:
    """Classify an otherwise valid snapshot against this immutable Config graph.

    Snapshot structure alone proves it is safe to read, not that it describes
    the database/index contract selected by the current Config.  A difference
    is a visible degraded state; R2 must never overwrite it or silently rebuild
    an index.  R4 owns the confirmed rebuild workflow.
    """

    try:
        snapshot_database = payload["database"]
        snapshot_embedding = payload["embedding"]
        if not isinstance(snapshot_database, Mapping) or not isinstance(
            snapshot_embedding, Mapping
        ):
            return "drifted"
        if database is not None and database.state is DatabaseState.READY:
            if snapshot_database.get("schema_version") != database.current_version:
                return "drifted"
        if embedding_settings is None:
            return "drifted"
        dimension = embedding_settings.dimensions
        if (
            type(dimension) is not int
            or not 1 <= dimension <= _MAX_EMBEDDING_DIMENSION
        ):
            # ``dim: auto`` without a resolved local runtime value must not
            # pretend an older snapshot remains compatible.  Inspect stays
            # offline; the plan can request a confirmed probe where applicable.
            return "drifted"
        expected_fingerprint = _embedding_snapshot_fingerprint(
            config, embedding_settings, dimension
        )
        if snapshot_embedding.get("provider") != embedding_settings.provider:
            return "drifted"
        if snapshot_embedding.get("fingerprint") != expected_fingerprint:
            return "drifted"
    except (KeyError, TypeError, ValueError, PKVRuntimeError):
        return "drifted"
    return "valid"


def _inspect_runtime_snapshot(
    config: Any,
    layout: Any,
    *,
    database: DatabaseInspection | None,
    embedding_settings: EmbeddingProviderSettings | None,
) -> tuple[str, tuple[RuntimeIssue, ...], dict[str, object]]:
    """Use R1's secret-free snapshot helper when it is available.

    Before R1 lands, the absence of both helpers is explicitly a compatibility
    state rather than an invitation to read the legacy business ``local.yaml``.
    """

    reader = getattr(config, "read_runtime_config_snapshot", None)
    validator = getattr(config, "validate_runtime_config_snapshot", None)
    runtime_path = getattr(config, "runtime_config_path", None)
    if runtime_path is None:
        runtime_path = getattr(layout, "runtime_config_path", None)
    marker: dict[str, object] = {
        "path": _path_marker(Path(runtime_path)) if runtime_path is not None else {"state": "unavailable"}
    }
    if not callable(reader) and not callable(validator):
        return "unsupported", (), marker

    try:
        # R1's reader already delegates to its no-argument validation helper.
        # Prefer it exactly once to avoid a second read and a TOCTOU window.
        if callable(reader):
            payload = reader()
        elif callable(validator):
            payload = validator()
        else:
            payload = None
    except FileNotFoundError:
        payload = None
    except (OSError, PKVRuntimeError, ValueError, TypeError):
        return (
            "invalid",
            (
                _issue(
                    "runtime_snapshot_invalid",
                    RuntimeIssueSeverity.ERROR,
                    "运行态配置快照无法安全读取或不符合版本化、无密钥合同，需要修复。",
                    next_action=RuntimeActionKind.REPAIR_RUNTIME,
                ),
            ),
            marker,
        )
    if payload is None:
        return (
            "missing",
            (
                _issue(
                    "runtime_snapshot_missing",
                    RuntimeIssueSeverity.WARNING,
                    "运行态配置快照不存在，无法验证当前数据契约。",
                    next_action=RuntimeActionKind.RECORD_RUNTIME_SNAPSHOT,
                ),
            ),
            marker,
        )

    try:
        payload = _validate_runtime_snapshot_payload(payload)
    except (TypeError, ValueError):
        return (
            "invalid",
            (
                _issue(
                    "runtime_snapshot_invalid",
                    RuntimeIssueSeverity.ERROR,
                    "运行态配置快照不符合版本化、无密钥的运行数据合同，需要修复。",
                    next_action=RuntimeActionKind.REPAIR_RUNTIME,
                ),
            ),
            marker,
        )
    marker["payload_sha256"] = _safe_hash(payload)
    contract_state = _runtime_snapshot_contract_state(
        payload,
        config=config,
        database=database,
        embedding_settings=embedding_settings,
    )
    marker["contract_state"] = contract_state
    if contract_state != "valid":
        return (
            "drifted",
            (
                _issue(
                    "runtime_snapshot_drift",
                    RuntimeIssueSeverity.WARNING,
                    "当前数据库或 Embedding 配置与已发布的运行态快照不一致；请先检查重建影响，不能自动覆盖。",
                    next_action=RuntimeActionKind.REPAIR_RUNTIME,
                ),
            ),
            marker,
        )
    return "valid", (), marker


def _runtime_snapshot_payload(
    config: Any,
    database: DatabaseInspection,
) -> dict[str, object]:
    """Build the minimal R2 data-runtime snapshot without credentials or paths."""

    settings = embedding_settings_from_config(config)
    dimension = settings.dimensions
    if (
        type(dimension) is not int
        or not 1 <= dimension <= _MAX_EMBEDDING_DIMENSION
    ):
        raise PKVRuntimeError(
            ErrorCode.PROVIDER_PROTOCOL_FAILED,
            "Embedding 维度尚未经过已确认的 Provider probe，不能发布运行态配置快照。",
            stage="runtime_lifecycle",
            recoverable=True,
        )

    fingerprint: Mapping[str, object] | None = None
    index_fingerprint = getattr(config, "embedding_index_fingerprint", None)
    if callable(index_fingerprint):
        candidate = index_fingerprint(dimension)
        if isinstance(candidate, Mapping):
            fingerprint = candidate
    if fingerprint is None:
        # Fallback remains secret-free: it is only used by narrow explicit
        # Config-like test/admin seams that do not expose the normal endpoint
        # contract helper.  Production Config supplies that helper, which also
        # strips credential-shaped endpoint parameters before hashing.
        fingerprint = {
            "base_url_sha256": _safe_hash(settings.base_url),
            "embedding_model": settings.model,
            "embedding_dim": str(dimension),
        }

    try:
        endpoint_hash = fingerprint["base_url_sha256"]
        model = fingerprint["embedding_model"]
        fingerprint_dimension = fingerprint["embedding_dim"]
    except (KeyError, TypeError) as exc:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "Embedding 运行态指纹不完整，不能发布运行态配置快照。",
            stage="runtime_lifecycle",
            recoverable=True,
        ) from exc
    normalized_fingerprint = {
        "base_url_sha256": endpoint_hash,
        "embedding_model": model,
        "embedding_dim": fingerprint_dimension,
    }

    payload: dict[str, object] = {
        "schema_version": _RUNTIME_SNAPSHOT_SCHEMA_VERSION,
        "database": {"schema_version": database.current_version},
        "embedding": {
            "provider": settings.provider,
            "fingerprint": normalized_fingerprint,
        },
    }
    try:
        _validate_runtime_snapshot_payload(payload)
    except (TypeError, ValueError) as exc:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "无法生成符合版本化、无密钥合同的运行态配置快照。",
            stage="runtime_lifecycle",
            recoverable=True,
        ) from exc
    return payload


def _write_runtime_snapshot(
    config: Any,
    database: DatabaseInspection,
    *,
    expected_absent: bool = False,
) -> None:
    """CAS-merge and publish the R2 base snapshot after confirmation.

    R4 may already own an active-generation extension in the same internal
    ``local.yaml``.  Lifecycle therefore never replaces the whole document via
    ``Config.write_runtime_config_snapshot``; it records only its owned base
    facts and preserves every secret-free extension under the caller's writer
    lease.
    """

    try:
        payload = _runtime_snapshot_payload(config, database)
        from src.runtime.runtime_snapshot import RuntimeSnapshotStore

        store = RuntimeSnapshotStore(config.layout)
        observed = store.read()
        if expected_absent and observed.exists:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "运行态配置快照已在计划执行期间出现；请重新检查并生成计划。",
                stage="runtime_lifecycle",
                recoverable=True,
            )
        if observed.exists:
            _validate_runtime_snapshot_payload(observed.payload)
        published = store.publish(observed, observed.merged(payload))
        _validate_runtime_snapshot_payload(published.payload)
    except PKVRuntimeError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "运行态配置快照无法安全写入或校验。",
            stage="runtime_lifecycle",
            recoverable=True,
        ) from exc


def inspect_runtime(config: Any | None = None) -> RuntimeInspection:
    """Inspect a runtime without creating files, recovering journals, or probing providers."""

    if config is None:
        from src.utils.config import Config

        config = Config()
    layout = config.layout
    issues: list[RuntimeIssue] = []
    resources_ok = True
    try:
        layout.validate_bundled_resources()
    except PKVRuntimeError as exc:
        resources_ok = False
        issues.append(
            _issue(
                exc.code,
                RuntimeIssueSeverity.ERROR,
                "Bundled runtime resources are unavailable or unsafe.",
                recoverable=False,
                next_action=RuntimeActionKind.REPAIR_RUNTIME,
            )
        )

    (
        provider_validation,
        provider_issues,
        provider_marker,
        _,
        embedding_settings,
    ) = _inspect_providers(config)
    issues.extend(provider_issues)
    database, database_issues = _inspect_database(layout)
    issues.extend(database_issues)
    tokenizer_cache_usable, tokenizer_cache_issues, tokenizer_cache_marker = (
        _inspect_tokenizer_cache(layout, database=database)
    )
    issues.extend(tokenizer_cache_issues)
    root_is_fresh, root_state = _fresh_data_root(layout)
    journal_count, journal_blocking, journal_degraded, journal_issues, journal_marker = _inspect_journal(layout)
    issues.extend(journal_issues)
    snapshot_state, snapshot_issues, snapshot_marker = _inspect_runtime_snapshot(
        config,
        layout,
        database=database,
        embedding_settings=embedding_settings,
    )
    issues.extend(snapshot_issues)
    source_revision_safe = True
    try:
        source_marker = _config_source_marker(config)
    except (OSError, TypeError, ValueError, PKVRuntimeError):
        source_revision_safe = False
        source_marker = {"state": "invalid"}
        issues.append(
            _issue(
                ErrorCode.REPAIR_REQUIRED,
                RuntimeIssueSeverity.ERROR,
                "用户配置源无法安全确认；请先修复配置路径或文件后重新检查。",
                next_action=RuntimeActionKind.REPAIR_RUNTIME,
            )
        )

    database_state = database.state.value if database is not None else "unknown"
    if database is not None and database.state is DatabaseState.FRESH:
        if root_is_fresh:
            issues.append(
                _issue(
                    ErrorCode.SETUP_REQUIRED,
                    RuntimeIssueSeverity.INFO,
                    "尚未初始化新的数据根。",
                    next_action=RuntimeActionKind.INITIALIZE_FRESH,
                )
            )
        else:
            issues.append(
                _issue(
                    ErrorCode.REPAIR_REQUIRED,
                    RuntimeIssueSeverity.ERROR,
                    "已有数据根缺少可用数据库，不能按新安装自动初始化。",
                    next_action=RuntimeActionKind.REPAIR_RUNTIME,
                )
            )
    elif database is not None and database.state is DatabaseState.UPGRADE_REQUIRED:
        issues.append(
            _issue(
                ErrorCode.DATABASE_UPGRADE_REQUIRED,
                RuntimeIssueSeverity.ERROR,
                "数据库版本较旧，需要备份和显式升级。",
                next_action=RuntimeActionKind.UPGRADE_DATABASE,
            )
        )
    elif database is not None and database.state is DatabaseState.FUTURE_VERSION:
        issues.append(
            _issue(
                ErrorCode.REPAIR_REQUIRED,
                RuntimeIssueSeverity.ERROR,
                "数据库版本高于当前运行时，不能安全打开。",
                next_action=RuntimeActionKind.REPAIR_RUNTIME,
            )
        )

    critical = (
        not resources_ok
        or not source_revision_safe
        or database is None
        or not tokenizer_cache_usable
        or journal_blocking
        or snapshot_state == "invalid"
        or (database is not None and database.state is DatabaseState.FRESH and not root_is_fresh)
        or (database is not None and database.state is DatabaseState.FUTURE_VERSION)
    )
    providers_valid = (
        provider_validation.llm_structural == "valid"
        and provider_validation.embedding_structural == "valid"
    )
    if critical:
        readiness = RuntimeReadiness.REPAIR_REQUIRED
    elif database is not None and database.state is DatabaseState.UPGRADE_REQUIRED:
        readiness = RuntimeReadiness.UPGRADE_REQUIRED
    elif (
        (database is not None and database.state is DatabaseState.FRESH)
        or not providers_valid
    ):
        readiness = RuntimeReadiness.SETUP_REQUIRED
    elif journal_degraded or snapshot_state in {"missing", "drifted"}:
        readiness = RuntimeReadiness.DEGRADED
    else:
        readiness = RuntimeReadiness.READY

    root_revision_state = "fresh" if root_is_fresh else root_state
    revision_payload = {
        "resources_ok": resources_ok,
        # A R3 lease may create only runtime/write.lease before the second
        # check.  Treat absent, empty, and lease-only roots as the same fresh
        # state, while preserving nonempty/unsafe data-root drift.  Database,
        # journal, snapshot, and provider markers below remain independent.
        "root": {"state": root_revision_state},
        "database": {
            "state": database_state,
            "current_version": database.current_version if database is not None else None,
            "latest_version": database.latest_version if database is not None else None,
            "pending_versions": database.pending_versions if database is not None else (),
            "marker": _path_marker(Path(layout.db_path)),
        },
        "tokenizer_cache": tokenizer_cache_marker,
        "providers": provider_marker,
        "user_config": source_marker,
        "journal": journal_marker,
        "snapshot": snapshot_marker,
    }
    return RuntimeInspection(
        readiness=readiness,
        revision=_safe_hash(revision_payload),
        database_state=database_state,
        issues=tuple(issues),
        provider_validation=provider_validation,
        journal_record_count=journal_count,
        runtime_snapshot=snapshot_state,
        _config=config,
    )


def _action(
    kind: RuntimeActionKind,
    impact: str,
    *,
    requires_confirmation: bool,
    requires_network: bool = False,
    executable: bool = True,
) -> RuntimeAction:
    return RuntimeAction(
        kind=kind,
        impact=impact,
        requires_confirmation=requires_confirmation,
        requires_network=requires_network,
        executable=executable,
    )


def plan_runtime(inspection: RuntimeInspection) -> RuntimePlan:
    """Turn a readonly inspection into a side-effect-free, explicit action plan."""

    if not isinstance(inspection, RuntimeInspection):
        raise TypeError("inspection 必须由 inspect_runtime 返回")
    actions: list[RuntimeAction] = []
    providers_valid = (
        inspection.provider_validation.llm_structural == "valid"
        and inspection.provider_validation.embedding_structural == "valid"
    )
    if inspection.readiness is RuntimeReadiness.SETUP_REQUIRED:
        if not providers_valid:
            actions.append(
                _action(
                    RuntimeActionKind.CONFIGURE_PROVIDERS,
                    "补全唯一用户配置中的 LLM 与 Embedding Provider；此动作由用户编辑配置完成。",
                    requires_confirmation=False,
                    executable=False,
                )
            )
        elif inspection.database_state == DatabaseState.FRESH.value:
            actions.extend(
                (
                    _action(
                        RuntimeActionKind.VALIDATE_PROVIDERS,
                        "向已配置的 LLM 与 Embedding endpoint 发送最小健康探测；可能产生网络流量和费用。",
                        requires_confirmation=True,
                        requires_network=True,
                    ),
                    _action(
                        RuntimeActionKind.INITIALIZE_FRESH,
                        "创建声明的数据目录和新的 SQLite 数据库；不会执行历史数据库迁移。",
                        requires_confirmation=True,
                    ),
                    _action(
                        RuntimeActionKind.RECORD_RUNTIME_SNAPSHOT,
                        "写入无密钥的运行态配置快照，用于后续数据与 Embedding 契约校验。",
                        requires_confirmation=True,
                    ),
                )
            )
    elif inspection.readiness is RuntimeReadiness.UPGRADE_REQUIRED:
        actions.append(
            _action(
                RuntimeActionKind.UPGRADE_DATABASE,
                "需要先备份后执行显式数据库升级；本基础层不会自动升级。",
                requires_confirmation=True,
                executable=False,
            )
        )
    elif inspection.readiness is RuntimeReadiness.REPAIR_REQUIRED:
        if inspection.journal_record_count:
            actions.append(
                _action(
                    RuntimeActionKind.RECOVER_JOURNAL,
                    "检查并修复操作日志；可能修改日志、数据库或受隔离的内容文件。",
                    requires_confirmation=True,
                    executable=False,
                )
            )
        actions.append(
            _action(
                RuntimeActionKind.REPAIR_RUNTIME,
                "先生成可审计的修复方案；本基础层不会隐式修复既有数据。",
                requires_confirmation=True,
                executable=False,
            )
        )
    elif inspection.readiness is RuntimeReadiness.DEGRADED:
        if inspection.runtime_snapshot == "missing":
            if inspection.provider_validation.embedding_dimension is None:
                actions.append(
                    _action(
                        RuntimeActionKind.VALIDATE_PROVIDERS,
                        "向已配置的 LLM 与 Embedding endpoint 发送最小健康探测，以解析 auto Embedding 维度；可能产生网络流量和费用。",
                        requires_confirmation=True,
                        requires_network=True,
                    )
                )
            actions.append(
                _action(
                    RuntimeActionKind.RECORD_RUNTIME_SNAPSHOT,
                    "写入无密钥的运行态配置快照，用于后续数据与 Embedding 契约校验。",
                    requires_confirmation=True,
                )
            )
        elif inspection.runtime_snapshot == "drifted":
            actions.append(
                _action(
                    RuntimeActionKind.REPAIR_RUNTIME,
                    "当前数据库或 Embedding 合同与运行态快照不一致；必须先展示重建影响并获得确认，本基础层不会覆盖快照或重建索引。",
                    requires_confirmation=True,
                    executable=False,
                )
            )
        else:
            actions.append(
                _action(
                    RuntimeActionKind.REPAIR_RUNTIME,
                    "处理已降级的操作记录；本基础层不会自动修改既有数据。",
                    requires_confirmation=True,
                    executable=False,
                )
            )

    payload = {
        "revision": inspection.revision,
        "actions": [action.to_dict() for action in actions],
    }
    return RuntimePlan(
        plan_id=_safe_hash(payload),
        inspection=inspection,
        actions=tuple(actions),
        _config=inspection._config,
    )


def confirm_runtime_plan(
    plan: RuntimePlan,
    *,
    allow_network: bool = False,
    approved_actions: frozenset[str] | set[str] | tuple[str, ...] | None = None,
) -> RuntimeConfirmation:
    """Build an explicit confirmation for exactly the supplied plan."""

    if not isinstance(plan, RuntimePlan):
        raise TypeError("plan 必须由 plan_runtime 返回")
    if type(allow_network) is not bool:
        raise TypeError("allow_network 必须是 bool")
    if approved_actions is None:
        approved_actions = frozenset(
            action.kind.value for action in plan.actions if action.requires_confirmation
        )
    return RuntimeConfirmation(
        plan_id=plan.plan_id,
        approved_actions=frozenset(approved_actions),
        allow_network=allow_network,
    )


def _assert_confirmation(plan: RuntimePlan, confirmation: RuntimeConfirmation | None) -> None:
    required = {
        action.kind.value for action in plan.actions if action.requires_confirmation
    }
    if not required:
        return
    if not isinstance(confirmation, RuntimeConfirmation):
        raise PKVRuntimeError(
            ErrorCode.CONFIRMATION_REQUIRED,
            "执行运行时计划需要显式确认。",
            stage="runtime_lifecycle",
            recoverable=True,
        )
    if confirmation.plan_id != plan.plan_id:
        raise PKVRuntimeError(
            ErrorCode.RUNTIME_PLAN_STALE,
            "确认不属于当前运行时计划。",
            stage="runtime_lifecycle",
            recoverable=True,
        )
    missing = required - confirmation.approved_actions
    needs_network = any(action.requires_network for action in plan.actions)
    if (
        type(confirmation.allow_network) is not bool
        or missing
        or (needs_network and not confirmation.allow_network)
    ):
        raise PKVRuntimeError(
            ErrorCode.CONFIRMATION_REQUIRED,
            "运行时计划尚未获得完整的写入或网络确认。",
            stage="runtime_lifecycle",
            recoverable=True,
        )


def _assert_executable(plan: RuntimePlan) -> None:
    if any(not action.executable for action in plan.actions):
        error_code = (
            ErrorCode.SETUP_REQUIRED
            if plan.inspection.readiness is RuntimeReadiness.SETUP_REQUIRED
            else ErrorCode.REPAIR_REQUIRED
        )
        raise PKVRuntimeError(
            error_code,
            "当前计划包含需要专用工作流或人工配置的动作，不能在生命周期基础层直接执行。",
            stage="runtime_lifecycle",
            recoverable=True,
        )


def _revalidate_plan_config_after_lease(plan: RuntimePlan) -> Any:
    """Build one same-root successor for a confirmation-bound lifecycle plan.

    Normal archive workflows deliberately keep their captured Config B while
    they are in flight.  A lifecycle plan has not started work until its writer
    boundary, however, and may perform a paid live Provider probe.  Re-read the
    editable source at that point so an external settings/key edit cannot reuse
    an old confirmation.  ``Config.reload_snapshot`` itself is readonly and
    rejects a root switch before any publication; narrow Config-like test seams
    without it retain the already revision-checked explicit snapshot.
    """

    reloader = getattr(plan._config, "reload_snapshot", None)
    if not callable(reloader):
        return plan._config
    candidate = reloader()
    candidate_inspection = inspect_runtime(candidate)
    if candidate_inspection.revision != plan.inspection.revision:
        raise PKVRuntimeError(
            ErrorCode.RUNTIME_PLAN_STALE,
            "运行时计划确认后用户配置已改变；请重新检查并生成计划。",
            stage="runtime_lifecycle",
            recoverable=True,
        )
    return candidate


def _probe_providers(
    probe: ProviderProbe,
    config: Any,
) -> tuple[ProviderValidation, int]:
    _, _, _, llm_settings, embedding_settings = _inspect_providers(config)
    if llm_settings is None or embedding_settings is None:
        raise PKVRuntimeError(
            ErrorCode.SETUP_REQUIRED,
            "Provider 结构配置尚未就绪。",
            stage="provider_probe",
            recoverable=True,
        )
    try:
        probe.probe_llm(llm_settings)
        reported_dimension = probe.probe_embedding(embedding_settings)
        if (
            type(reported_dimension) is not int
            or not 1 <= reported_dimension <= _MAX_EMBEDDING_DIMENSION
        ):
            raise PKVRuntimeError(
                ErrorCode.PROVIDER_PROTOCOL_FAILED,
                "Embedding Provider 未返回可用于运行态契约的有效维度。",
                stage="provider_probe",
                recoverable=True,
            )
        expected_dimension = embedding_settings.dimensions
        if (
            expected_dimension is not None
            and reported_dimension != expected_dimension
        ):
            raise PKVRuntimeError(
                ErrorCode.PROVIDER_PROTOCOL_FAILED,
                "Embedding Provider 返回维度与当前配置不一致。",
                stage="provider_probe",
                recoverable=True,
            )
    except PKVRuntimeError as exc:
        if exc.code is ErrorCode.PROVIDER_PROTOCOL_FAILED:
            raise
        raise PKVRuntimeError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            "Provider 实际连通性验证失败。",
            stage="provider_probe",
            recoverable=True,
        ) from exc
    except Exception as exc:
        raise PKVRuntimeError(
            ErrorCode.PROVIDER_UNAVAILABLE,
            "Provider 实际连通性验证失败。",
            stage="provider_probe",
            recoverable=True,
        ) from exc
    validation, _, _, _, _ = _inspect_providers(config)
    return (
        replace(
            validation,
            llm_actual="verified",
            embedding_actual="verified",
        ),
        reported_dimension,
    )


def _persist_probed_embedding_dimension(config: Any, reported_dimension: int) -> None:
    """Persist an auto dimension only after its confirmed Provider probe.

    A declared dimension was already equality-checked in :func:`_probe_providers`
    and needs no write.  For ``dim: auto`` we deliberately delay the durable
    cache until fresh database initialization has succeeded, avoiding a
    half-configured root if the Provider probe fails.
    """

    settings = embedding_settings_from_config(config)
    if settings.dimensions is not None:
        if settings.dimensions != reported_dimension:
            raise PKVRuntimeError(
                ErrorCode.PROVIDER_PROTOCOL_FAILED,
                "Embedding Provider 返回维度与当前配置不一致。",
                stage="provider_probe",
                recoverable=True,
            )
        return
    if getattr(config, "embedding_dim_is_auto", False) is not True:
        raise PKVRuntimeError(
            ErrorCode.PROVIDER_PROTOCOL_FAILED,
            "Embedding 维度未声明且不是 auto，不能建立运行态契约。",
            stage="provider_probe",
            recoverable=True,
        )
    setter = getattr(config, "set_runtime_embedding_dim", None)
    if not callable(setter):
        raise PKVRuntimeError(
            ErrorCode.PROVIDER_PROTOCOL_FAILED,
            "Embedding auto 维度缺少受控的运行态持久化入口。",
            stage="provider_probe",
            recoverable=True,
        )
    try:
        setter(reported_dimension)
    except PKVRuntimeError:
        raise
    except Exception as exc:
        raise PKVRuntimeError(
            ErrorCode.PROVIDER_PROTOCOL_FAILED,
            "Embedding auto 维度无法安全持久化。",
            stage="provider_probe",
            recoverable=True,
        ) from exc
    resolved = embedding_settings_from_config(config).dimensions
    if resolved != reported_dimension:
        raise PKVRuntimeError(
            ErrorCode.PROVIDER_PROTOCOL_FAILED,
            "Embedding auto 维度持久化后与 Provider 结果不一致。",
            stage="provider_probe",
            recoverable=True,
        )


def execute_runtime_plan(
    plan: RuntimePlan,
    confirmation: RuntimeConfirmation | None,
    *,
    provider_probe: ProviderProbe | None = None,
    writer_lease_factory: WriterLeaseFactory | None = None,
) -> RuntimeExecution:
    """Execute an approved plan after a fail-closed revision re-inspection.

    ``writer_lease_factory`` is the R3 integration seam.  It receives the
    explicit config object and must return a context manager that holds the
    cross-process writer lease.  The default lazily uses R3's
    ``write_lease_scope(config.layout)``; callers may inject a fake only in
    isolated tests or a compatible host integration.
    """

    if not isinstance(plan, RuntimePlan):
        raise TypeError("plan 必须由 plan_runtime 返回")
    if plan._config is None:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "运行时计划没有可重检的显式配置快照。",
            stage="runtime_lifecycle",
            recoverable=True,
        )
    if plan._config is not plan.inspection._config:
        raise PKVRuntimeError(
            ErrorCode.RUNTIME_PLAN_STALE,
            "运行时计划未绑定到原始显式配置快照；请重新生成计划。",
            stage="runtime_lifecycle",
            recoverable=True,
        )
    current = inspect_runtime(plan._config)
    if current.revision != plan.inspection.revision:
        raise PKVRuntimeError(
            ErrorCode.RUNTIME_PLAN_STALE,
            "运行时状态已改变；请重新检查并生成计划。",
            stage="runtime_lifecycle",
            recoverable=True,
        )
    canonical_plan = plan_runtime(current)
    if (
        plan.plan_id != canonical_plan.plan_id
        or plan.actions != canonical_plan.actions
        or plan.inspection.to_dict() != current.to_dict()
    ):
        raise PKVRuntimeError(
            ErrorCode.RUNTIME_PLAN_STALE,
            "运行时计划不再匹配当前检查结果；请重新生成计划。",
            stage="runtime_lifecycle",
            recoverable=True,
        )
    # Execute only a freshly derived plan.  RuntimePlan and RuntimeAction are
    # public frozen DTOs, so callers must not be able to forge a weaker
    # confirmation/network flag by constructing one directly.
    plan = canonical_plan
    _assert_confirmation(plan, confirmation)
    _assert_executable(plan)
    if not plan.actions:
        return RuntimeExecution(
            inspection=current,
            provider_validation=current.provider_validation,
            _config=plan._config,
        )

    if writer_lease_factory is None:
        # Delayed import avoids a runtime-package import cycle while retaining
        # the strict R3 single-writer default for every real execution.
        from src.runtime.write_lease import write_lease_scope

        lease = write_lease_scope(plan._config.layout)
    else:
        lease = writer_lease_factory(plan._config)
    execution_config = plan._config
    actual_validation = current.provider_validation
    context: RuntimeContext | None = None
    probed_embedding_dimension: int | None = None
    with lease:
        # Re-check after the writer boundary has been obtained: another writer
        # may have completed work while this plan was waiting for the lease.
        current = inspect_runtime(plan._config)
        if current.revision != plan.inspection.revision:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "等待写入权限期间运行时状态已改变；请重新生成计划。",
                stage="runtime_lifecycle",
                recoverable=True,
            )
        execution_config = _revalidate_plan_config_after_lease(plan)
        kinds = {action.kind for action in plan.actions}
        if RuntimeActionKind.VALIDATE_PROVIDERS in kinds:
            probe = provider_probe or LiveProviderProbe(execution_config.layout)
            actual_validation, probed_embedding_dimension = _probe_providers(
                probe, execution_config
            )
        if RuntimeActionKind.INITIALIZE_FRESH in kinds:
            # Legacy bootstrap remains intentionally mutating; this call is
            # reachable only after confirmation.  Fresh initialization cannot
            # have a journal to recover, so avoid the legacy recovery side effect.
            context = bootstrap_runtime(
                execution_config,
                initialize_fresh=True,
                recover_interrupted=False,
            )
        if probed_embedding_dimension is not None:
            _persist_probed_embedding_dimension(
                execution_config, probed_embedding_dimension
            )
        if RuntimeActionKind.RECORD_RUNTIME_SNAPSHOT in kinds:
            snapshot_database = context.database if context is not None else None
            if snapshot_database is None:
                snapshot_database, _ = _inspect_database(execution_config.layout)
            if snapshot_database is None or snapshot_database.state is not DatabaseState.READY:
                raise PKVRuntimeError(
                    ErrorCode.REPAIR_REQUIRED,
                    "当前数据库未达到可写入运行态配置快照的状态。",
                    stage="runtime_lifecycle",
                    recoverable=True,
                )
            _write_runtime_snapshot(
                execution_config,
                snapshot_database,
                expected_absent=plan.inspection.runtime_snapshot == "missing",
            )

    inspected_after = inspect_runtime(execution_config)
    return RuntimeExecution(
        inspection=inspected_after,
        provider_validation=actual_validation,
        context=context,
        _config=execution_config,
    )


__all__ = [
    "LiveProviderProbe",
    "ProviderProbe",
    "ProviderValidation",
    "RuntimeAction",
    "RuntimeActionKind",
    "RuntimeConfirmation",
    "RuntimeExecution",
    "RuntimeInspection",
    "RuntimeIssue",
    "RuntimeIssueSeverity",
    "RuntimePlan",
    "RuntimeReadiness",
    "WriterLeaseFactory",
    "confirm_runtime_plan",
    "execute_runtime_plan",
    "inspect_runtime",
    "plan_runtime",
]
