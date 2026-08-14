"""Stable error and operation-state contracts shared by all adapters."""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """Machine-readable runtime errors.

    External wrappers, CLI, MCP and Workflow adapters may change presentation, but must not
    erase or reinterpret these codes.
    """

    RESOURCE_MISSING = "resource_missing"
    RESOURCE_NOT_READABLE = "resource_not_readable"
    DATA_ROOT_UNSAFE = "data_root_unsafe"
    PATH_OUTSIDE_VAULT = "path_outside_vault"
    PATH_LINK_UNSAFE = "path_link_unsafe"
    PATH_STATE_UNDETERMINED = "path_state_undetermined"
    PATH_NOT_REGULAR_FILE = "path_not_regular_file"
    DATABASE_MISSING = "database_missing"
    DATABASE_NOT_SQLITE = "database_not_sqlite"
    DATABASE_VERSION_TABLE_MISSING = "database_version_table_missing"
    DATABASE_VERSION_TABLE_INVALID = "database_version_table_invalid"
    DATABASE_INTEGRITY_FAILED = "database_integrity_failed"
    DATABASE_SCHEMA_DRIFT = "database_schema_drift"
    DATABASE_UPGRADE_REQUIRED = "database_upgrade_required"
    DATABASE_FUTURE_VERSION = "database_future_version"
    MIGRATION_BACKUP_FAILED = "migration_backup_failed"
    MIGRATION_LOCKED = "migration_locked"
    MIGRATION_FAILED = "migration_failed"
    STORAGE_PRIMARY_FAILED = "storage_primary_failed"
    STORAGE_INDEX_FAILED = "storage_index_failed"
    STORAGE_VECTOR_FAILED = "storage_vector_failed"
    STORAGE_COMPENSATION_FAILED = "storage_compensation_failed"
    STORAGE_REPAIR_REQUIRED = "storage_repair_required"
    WORKFLOW_CONFIG_INVALID = "workflow_config_invalid"
    WORKFLOW_STEP_FAILED = "workflow_step_failed"
    WORKFLOW_CONDITION_INVALID = "workflow_condition_invalid"
    WORKFLOW_PROCESSOR_UNKNOWN = "workflow_processor_unknown"
    RETRIEVAL_INVALID_QUERY = "retrieval_invalid_query"
    RETRIEVAL_BACKEND_FAILED = "retrieval_backend_failed"
    RETRIEVAL_INDEX_UNAVAILABLE = "retrieval_index_unavailable"
    RETRIEVAL_METADATA_INCONSISTENT = "retrieval_metadata_inconsistent"
    PROVIDER_CONFIG_INVALID = "provider_config_invalid"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_PROTOCOL_FAILED = "provider_protocol_failed"
    URL_INVALID = "url_invalid"
    SSRF_TARGET_FORBIDDEN = "ssrf_target_forbidden"
    SSRF_RESOLUTION_FAILED = "ssrf_resolution_failed"
    SSRF_REDIRECT_LIMIT = "ssrf_redirect_limit"
    PROCESSOR_RESOURCE_LIMIT = "processor_resource_limit"
    TRANSPORT_UNSUPPORTED = "transport_unsupported"
    CHAT_BUSY = "chat_busy"
    CHAT_PROVIDER_FAILED = "chat_provider_failed"
    CHAT_SAVE_FAILED = "chat_save_failed"
    CHAT_STATE_CONFLICT = "chat_state_conflict"


class OperationStatus(str, Enum):
    """Externally observable terminal operation states."""

    READY = "ready"
    DEGRADED = "degraded"
    REPAIR_REQUIRED = "repair_required"
    REJECTED = "rejected"
    DELETED = "deleted"


class StorageStage(str, Enum):
    """Durable storage-operation stages used for diagnosis and recovery."""

    PREPARING = "preparing"
    PRIMARY_COMMITTED = "primary_committed"
    INDEX_COMMITTED = "index_committed"
    VECTOR_COMMITTED = "vector_committed"
    DELETE_PLANNED = "delete_planned"
    DELETE_QUARANTINED = "delete_quarantined"
    DELETE_INDEX_COMMITTED = "delete_index_committed"
    COMPLETED = "completed"
    COMPENSATING = "compensating"


class PKVRuntimeError(RuntimeError):
    """Runtime failure carrying stable, adapter-safe metadata."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        stage: str | None = None,
        recoverable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.recoverable = recoverable

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code.value,
            "message": str(self),
            "recoverable": self.recoverable,
        }
        if self.stage:
            payload["stage"] = self.stage
        return payload
