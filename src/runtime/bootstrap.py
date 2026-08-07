"""Explicit, shared runtime bootstrap for GUI, CLI and MCP adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.runtime.errors import ErrorCode, PKVRuntimeError, StorageStage
from src.storage.migration_manager import (
    DatabaseInspection,
    DatabaseState,
    MigrationManager,
)


@dataclass(frozen=True)
class RuntimeContext:
    """Validated runtime objects adapters may safely consume."""

    config: Any
    database: DatabaseInspection
    repair_records: tuple[dict[str, Any], ...] = ()

    @property
    def layout(self):
        return self.config.layout


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
