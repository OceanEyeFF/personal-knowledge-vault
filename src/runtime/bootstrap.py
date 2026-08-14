"""Explicit, shared runtime bootstrap for CLI, MCP, and external wrappers."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Optional

from src.runtime.errors import ErrorCode, PKVRuntimeError, StorageStage
from src.storage.migration_manager import (
    DatabaseInspection,
    DatabaseState,
    MigrationManager,
)


_BOOTSTRAP_ADAPTERS = frozenset({"cli", "mcp", "wrapper"})
_MACHINE_STAGE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


@dataclass(frozen=True)
class RuntimeContext:
    """Validated runtime objects adapters may safely consume."""

    config: Any
    database: DatabaseInspection
    repair_records: tuple[dict[str, Any], ...] = ()

    @property
    def layout(self):
        return self.config.layout


def project_bootstrap_error(
    error: BaseException,
    *,
    adapter: str,
    stage: str = "runtime_bootstrap",
) -> dict[str, object]:
    """Project a startup failure into a stable, non-sensitive adapter shape.

    Exception messages may contain local paths, malformed YAML, or credentials.
    Release entrypoints therefore publish only a canonical code, a bounded
    stage, and an exact recoverability bit.  Known runtime errors retain their
    domain code; unexpected startup errors use one fixed fail-closed code.
    """

    if adapter not in _BOOTSTRAP_ADAPTERS:
        raise ValueError("unsupported bootstrap adapter")
    fallback_stage = stage
    if (
        type(fallback_stage) is not str
        or _MACHINE_STAGE.fullmatch(fallback_stage) is None
    ):
        fallback_stage = "runtime_bootstrap"
    if isinstance(error, PKVRuntimeError):
        code = error.code.value
        recoverable = error.recoverable is True
        projected_stage = error.stage
        if (
            type(projected_stage) is not str
            or _MACHINE_STAGE.fullmatch(projected_stage) is None
        ):
            projected_stage = fallback_stage
    else:
        code = "runtime_startup_failed"
        recoverable = False
        projected_stage = fallback_stage
    return {
        "adapter": adapter,
        "code": code,
        "recoverable": recoverable,
        "stage": projected_stage,
        "status": "error",
    }


def _configure_jieba_cache(tmp_dir: object) -> None:
    """Keep jieba's generated cache inside the declared user-data root."""

    import jieba

    jieba.dt.tmp_dir = str(tmp_dir)


def bootstrap_runtime(
    config: Optional[Any] = None,
    *,
    initialize_fresh: bool = True,
) -> RuntimeContext:
    """Validate resources/layout and establish a READY fresh-install database.

    Existing historical databases are deliberately rejected in M13 instead of
    being auto-upgraded.  This function is the only product bootstrap contract;
    constructing ``Config`` remains read-only.
    """

    if config is None:
        from src.utils.config import Config

        config = Config()

    layout = config.layout
    layout.validate_bundled_resources()
    layout.ensure_user_directories()
    _configure_jieba_cache(layout.tmp_dir)
    config.sanitize_runtime_state()

    manager = MigrationManager(
        layout.db_path,
        layout.migrations_dir,
        backup_dir=layout.backup_dir,
    )
    inspection = manager.require_ready()
    if inspection.state is DatabaseState.FRESH:
        if not initialize_fresh:
            raise PKVRuntimeError(
                ErrorCode.DATABASE_MISSING,
                f"数据库尚未初始化: {layout.db_path}",
            )
        inspection = manager.initialize_fresh()
    if inspection.state is not DatabaseState.READY:
        raise PKVRuntimeError(
            ErrorCode.DATABASE_SCHEMA_DRIFT,
            f"启动后数据库未达到 READY: {inspection.state.value}",
        )
    from src.storage.coordinator import (
        StorageOperationJournal,
        recover_interrupted_operations,
    )
    from src.storage.markdown_store import MarkdownStore
    from src.storage.sqlite_store import SQLiteStore

    journal = StorageOperationJournal(layout.runtime_state_dir / "operations")
    markdown_store = MarkdownStore(layout.vault_dir)
    sqlite_store = SQLiteStore(layout.db_path)
    usable_records, blocking_records = recover_interrupted_operations(
        journal,
        markdown_store,
        sqlite_store,
    )
    if blocking_records:
        details = ", ".join(
            f"{record.get('operation_id', '?')}"
            f"({record.get('action', '?')}@{record.get('stage', '?')})"
            for record in blocking_records
        )
        raise PKVRuntimeError(
            ErrorCode.STORAGE_REPAIR_REQUIRED,
            f"启动检测到无法自动恢复的存储操作，需要人工修复: {details}",
            stage=StorageStage.PREPARING.value,
            recoverable=True,
        )
    return RuntimeContext(
        config=config,
        database=inspection,
        repair_records=tuple(usable_records),
    )
