"""
存储层模块

提供 Markdown、SQLite、向量索引的统一存储接口
"""

from src.storage.coordinator import (
    StorageCoordinator,
    StorageOperationJournal,
    StorageOperationResult,
    recover_interrupted_operations,
)
from src.storage.markdown_store import PlannedVaultWrite
from src.storage.relation_store import RelationStore
from src.storage.vault_paths import QuarantinedVaultFile, VaultPathGateway

__all__ = [
    "PlannedVaultWrite",
    "QuarantinedVaultFile",
    "RelationStore",
    "StorageCoordinator",
    "StorageOperationJournal",
    "StorageOperationResult",
    "VaultPathGateway",
    "recover_interrupted_operations",
]
