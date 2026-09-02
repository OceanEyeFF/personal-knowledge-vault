"""Versioned contract for persistent writes below a PKV data root.

The R3 lease is deliberately acquired by *mutation owners* (Application,
Kernel and confirmed lifecycle execution), not by every low-level storage
primitive.  This module makes that boundary explicit and gives persistent
runtime sinks one shared fail-closed check.  It is intentionally small: the
inventory is a reviewable product contract, not a registry that dispatches
business operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.runtime.errors import ErrorCode, PKVRuntimeError


WRITER_INVENTORY_VERSION = 3

WriterKind = Literal["product", "test_fixture", "historical_fenced"]


@dataclass(frozen=True)
class DataRootWriter:
    """One reviewable persistent-write surface.

    ``owner`` names the only product boundary allowed to authorize the write;
    lower-level code may perform it only while that boundary's active R3 lease
    is present.  Test fixtures and fenced historical scripts remain listed so
    a future review does not mistake them for product runtime writers.
    """

    name: str
    paths: tuple[str, ...]
    owner: str
    kind: WriterKind
    read_semantics: str


DATA_ROOT_WRITER_INVENTORY: tuple[DataRootWriter, ...] = (
    DataRootWriter(
        "runtime_lifecycle",
        ("runtime/write.lease", "config/local.yaml", "db/", "backups/", "tmp/jieba.cache"),
        "confirmed lifecycle execution",
        "product",
        "inspect/plan are zero-write; execution owns the lease",
    ),
    DataRootWriter(
        "application_archive",
        (
            "vault/",
            "db/knowledge_vault.db",
            "runtime/operations/",
            "runtime/r4/ingress/",
            "runtime/r4/prepared/",
            "vectors/",
            "tmp/",
        ),
        "KnowledgeApplication archive mutation",
        "product",
        "read adapters open existing artifacts only",
    ),
    DataRootWriter(
        "kernel_mutations",
        (
            "vault/",
            "db/knowledge_vault.db",
            "runtime/operations/",
            "runtime/r4/prepared/",
            "vectors/",
        ),
        "KnowledgeKernel delete/chat mutation",
        "product",
        "Kernel reads do not acquire a data-root lease",
    ),
    DataRootWriter(
        "embedding_generation",
        ("vectors/staging/", "vectors/generations/", "config/local.yaml", "logs/audit.jsonl"),
        "confirmed embedding lifecycle execution",
        "product",
        "inspection and binding resolution are strict read-only",
    ),
    DataRootWriter(
        "ai_automation_lifecycle",
        (
            "db/knowledge_vault.db",
            "runtime/operations/",
            "runtime/r4/patches/",
            "config/local.yaml",
            "vectors/staging/",
            "vectors/generations/",
            "logs/audit.jsonl",
        ),
        "Application-owned automatic AI lifecycle",
        "product",
        "policy/status inspection is zero-write; task claim, token reservation, usage settlement, and generation publish require the owner lease",
    ),
    DataRootWriter(
        "runtime_audit",
        ("logs/audit.jsonl",),
        "an already-authorized mutation owner",
        "product",
        "append is rejected without the current task's lease",
    ),
    DataRootWriter(
        "runtime_file_logging",
        ("logs/pkv.log", "logs/pkv.log.*"),
        "an already-authorized mutation owner",
        "product",
        "read/status logging remains stderr-only and creates no files",
    ),
    DataRootWriter(
        "offline_test_fixtures",
        (".data-test/**",),
        "scripts/run-test.ps1 offline test wrapper",
        "test_fixture",
        "never a product runtime writer",
    ),
    DataRootWriter(
        "historical_maintenance",
        ("scripts/legacy/**", "scripts/backfill_*.py", "scripts/init_db.py", "scripts/migrate.py"),
        "none (fenced)",
        "historical_fenced",
        "entrypoints fail closed before Config, network, or data-root access",
    ),
)


def require_active_data_root_writer(layout: object, *, owner: str) -> None:
    """Fail closed unless this task/thread owns the supplied layout's R3 lease.

    A physical lock alone is not enough: ``has_active_write_lease`` verifies the
    ContextVar/worker capability bound to the current task.  That prevents an
    unrelated thread from borrowing another operation's lock file.
    """

    from src.runtime.write_lease import has_active_write_lease

    if has_active_write_lease(layout):
        return
    raise PKVRuntimeError(
        ErrorCode.WRITE_BUSY,
        "当前操作未持有知识库写入权限，无法执行持久化写入。",
        stage="write_lease",
        recoverable=True,
    )


__all__ = [
    "DATA_ROOT_WRITER_INVENTORY",
    "WRITER_INVENTORY_VERSION",
    "DataRootWriter",
    "require_active_data_root_writer",
]
