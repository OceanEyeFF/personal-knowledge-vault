"""Generation-based Embedding index lifecycle.

The historical ``<data-root>/vectors`` flat index is never rebuilt in place.
An explicit rebuild captures one readonly SQLite/Vault projection, builds a
complete index in a private staging directory, validates it, then atomically
flips a secret-free runtime-snapshot pointer.  Readers resolve that pointer
once and therefore either see the old complete generation or the new complete
generation; they never consume a partially-written index.

This is deliberately runtime-internal in the current incubation phase.  CLI,
MCP and Kernel adapters must explicitly adopt :func:`resolve_embedding_index_binding`
rather than silently falling back to the legacy flat directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

import frontmatter
import numpy as np

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import ensure_safe_directory
from src.runtime.runtime_snapshot import (
    RuntimeSnapshotDocument,
    RuntimeSnapshotStore,
    contains_secret_shaped_field,
)
from src.storage.sqlite_connection import connect_existing_sqlite
from src.storage.migration_manager import DatabaseState
from src.storage.vault_paths import VaultPathGateway
from src.storage.vector_store import VectorStore
from src.utils.config import endpoint_contract_sha256


_SNAPSHOT_EXTENSION_KEY = "embedding_index"
_SNAPSHOT_EXTENSION_SCHEMA = 1
_GENERATION_MANIFEST_NAME = "generation-manifest.json"
_GENERATION_MANIFEST_SCHEMA = 1
_DOCUMENT_PIPELINE_VERSION = 1
_STORED_CHUNK_SCHEMA_VERSION = 1
_GENERATION_ID_PATTERN = re.compile(r"g-[a-z0-9][a-z0-9-]{7,95}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SEMVER_PATTERN = re.compile(r"\d+\.\d+\.\d+\Z")
_PAIR_FILENAMES = (
    "doc_vectors.idx",
    "doc_vectors_metadata.json",
    "chunk_vectors.idx",
    "chunk_vectors_metadata.json",
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: object) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    )


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _is_generation_id(value: object) -> bool:
    return isinstance(value, str) and _GENERATION_ID_PATTERN.fullmatch(value) is not None


def _safe_runtime_error(
    code: ErrorCode,
    message: str,
    *,
    stage: str,
    recoverable: bool = True,
) -> PKVRuntimeError:
    return PKVRuntimeError(code, message, stage=stage, recoverable=recoverable)


class EmbeddingIndexState(str, Enum):
    """Safe externally-presentable interpretations of the generation state."""

    READY = "ready"
    INITIAL_GENERATION_PENDING = "initial_generation_pending"
    REBUILD_REQUIRED = "rebuild_required"
    REPAIR_REQUIRED = "repair_required"


@dataclass(frozen=True)
class EmbeddingContract:
    """The secret-free contract shared by one config snapshot and one index."""

    provider: str
    endpoint_sha256: str
    model: str
    dimension: int
    document_pipeline_version: int = _DOCUMENT_PIPELINE_VERSION
    stored_chunk_schema_version: int = _STORED_CHUNK_SCHEMA_VERSION

    @classmethod
    def from_config(cls, config: Any) -> "EmbeddingContract":
        try:
            dimension = config.embedding_dim
            if type(dimension) is not int or dimension <= 0:
                raise ValueError("embedding dimension unresolved")
            raw_fingerprint = config.embedding_index_fingerprint(dimension)
            if not isinstance(raw_fingerprint, Mapping):
                raise ValueError("embedding fingerprint unavailable")
            endpoint_hash = raw_fingerprint.get("base_url_sha256")
            model = raw_fingerprint.get("embedding_model")
            provider = config.embd_provider
            if (
                not isinstance(provider, str)
                or not provider
                or not _is_sha256(endpoint_hash)
                or not isinstance(model, str)
                or not model
            ):
                raise ValueError("embedding contract invalid")
            return cls(
                provider=provider,
                endpoint_sha256=endpoint_hash,
                model=model,
                dimension=dimension,
            )
        except PKVRuntimeError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            raise _safe_runtime_error(
                ErrorCode.REPAIR_REQUIRED,
                "当前 Embedding 配置未解析出可验证的维度或契约。",
                stage="embedding_contract",
            ) from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "provider": self.provider,
            "endpoint_sha256": self.endpoint_sha256,
            "model": self.model,
            "dimension": self.dimension,
            "document_pipeline_version": self.document_pipeline_version,
            "stored_chunk_schema_version": self.stored_chunk_schema_version,
        }


@dataclass(frozen=True)
class _VectorRuntimeConfigSnapshot:
    """The tiny immutable Config view that a staged VectorStore may observe.

    ``Config`` remains an application-owned object and can be superseded by a
    reload while a confirmed rebuild is embedding.  Passing that live object
    into :class:`VectorStore` would let a late metadata write observe a new
    model/dimension even though the staged vectors were made under the old
    contract.  This view contains only the captured, secret-free vector
    contract and the already-selected layout; it has no global-config fallback
    and no Provider credential.
    """

    layout: Any = field(repr=False, compare=False)
    contract: EmbeddingContract
    raw_endpoint: str = field(repr=False, compare=False)

    @property
    def embedding_dim(self) -> int:
        return self.contract.dimension

    @property
    def embd_base_url(self) -> str:
        # This private in-memory value lets VectorStore omit its legacy raw-v1
        # field for credential-bearing endpoints.  It is never copied into the
        # generation manifest/runtime snapshot; v2 metadata retains only the
        # canonical hash.
        return self.raw_endpoint

    @property
    def embd_model(self) -> str:
        return self.contract.model

    def embedding_index_fingerprint(self, dim: int) -> dict[str, str]:
        if int(dim) != self.contract.dimension:
            raise ValueError("staged vector dimension differs from captured contract")
        return {
            "base_url_sha256": self.contract.endpoint_sha256,
            "embedding_model": self.contract.model,
            "embedding_dim": str(self.contract.dimension),
        }

    @classmethod
    def capture(cls, config: Any, contract: EmbeddingContract) -> "_VectorRuntimeConfigSnapshot":
        """Capture one exact vector view before Provider work begins."""

        try:
            observed = EmbeddingContract.from_config(config)
            raw_endpoint = config.embd_base_url
            if (
                observed != contract
                or not isinstance(raw_endpoint, str)
                or endpoint_contract_sha256(raw_endpoint) != contract.endpoint_sha256
            ):
                raise ValueError("configuration changed before stage capture")
        except PKVRuntimeError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            raise _safe_runtime_error(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Embedding 配置在 generation 构建前已变化；请重新生成计划。",
                stage="embedding_plan",
            ) from exc
        return cls(config.layout, contract, raw_endpoint)


@dataclass(frozen=True)
class EmbeddingSourceSummary:
    """A content-free, stable capture summary suitable for plan serialization."""

    document_count: int
    chunk_count: int
    digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class EmbeddingSourceRecord:
    """One private rebuild source record; never serialized by lifecycle DTOs."""

    knowledge_id: int
    content: str = field(repr=False, compare=False)
    chunks: tuple[tuple[int, str], ...] = field(repr=False, compare=False)


@dataclass(frozen=True)
class CapturedEmbeddingSource:
    """A readonly SQLite + Markdown-consistent source capture."""

    summary: EmbeddingSourceSummary
    records: tuple[EmbeddingSourceRecord, ...] = field(repr=False, compare=False)


class EmbeddingSource(Protocol):
    """The source boundary lets tests use deterministic, non-network captures."""

    def capture(self, config: Any) -> CapturedEmbeddingSource:
        """Capture the current source strictly readonly or raise a typed error."""


class RebuildEmbedder(Protocol):
    """Provider boundary deliberately injected by an approved rebuild caller.

    ``embed_stored_chunks`` is intentionally *not* the historical
    ``Embedder.embed_chunks(text)`` API: that API re-splits a document according
    to today's chunk settings, whereas a generation rebuild must embed the
    already-persisted SQLite chunks byte-for-byte.  A future Application/Kernel
    adapter must explicitly bridge an approved Provider batch API to this
    protocol after confirmation; this runtime core never creates one.
    """

    def embed_document(self, text: str) -> np.ndarray:
        """Return one document vector for ``text``."""

    def embed_stored_chunks(self, chunks: Sequence[str]) -> np.ndarray:
        """Return one vector row per already-persisted stored chunk."""


class PreChunkedEmbeddingAdapter:
    """Bridge the existing ``src.ai.embedder.Embedder`` without re-chunking.

    The historical Embedder exposes ``embed_document(text)`` and a Provider
    client's ``embed_batch_numpy(texts)``; its public ``embed_chunks(text)``
    method always splits a document again and is therefore unsafe for an R4
    rebuild.  This adapter is deliberately structural so this module does not
    construct a Provider or import a factory.  A future mutation adapter creates
    the historical Embedder from the already-confirmed explicit Config, then
    wraps it here and passes this object to :func:`execute_embedding_rebuild`.
    """

    def __init__(self, embedder: Any) -> None:
        document = getattr(embedder, "embed_document", None)
        client = getattr(embedder, "client", None)
        batch = getattr(client, "embed_batch_numpy", None)
        if not callable(document) or not callable(batch):
            raise TypeError(
                "历史 Embedder 必须提供 embed_document 与 client.embed_batch_numpy"
            )
        self._document = document
        self._batch = batch

    def embed_document(self, text: str) -> np.ndarray:
        return self._document(text)

    def embed_stored_chunks(self, chunks: Sequence[str]) -> np.ndarray:
        if not isinstance(chunks, Sequence) or isinstance(chunks, (str, bytes)):
            raise TypeError("已存储分块必须是字符串序列")
        copied = list(chunks)
        if not copied or not all(isinstance(chunk, str) and chunk.strip() for chunk in copied):
            raise ValueError("已存储分块不能为空")
        # This is the key boundary: send the SQLite-projected chunks straight to
        # the Provider batch API; never call historical embed_chunks(text).
        return self._batch(copied)


class SQLiteEmbeddingSource:
    """Strictly readonly rebuild source from SQLite projection plus Markdown truth."""

    def capture(self, config: Any) -> CapturedEmbeddingSource:
        layout = config.layout
        try:
            connection = connect_existing_sqlite(layout.db_path, read_only=True)
            try:
                connection.row_factory = sqlite3.Row
                # Fix one SQLite read snapshot before the independent Markdown
                # reads.  A second capture immediately before pointer activation
                # detects any change that crossed that boundary.
                connection.execute("BEGIN")
                rows = connection.execute(
                    """
                    SELECT knowledge_id, content, file_path
                    FROM knowledge_items
                    ORDER BY knowledge_id ASC
                    """
                ).fetchall()
                chunk_rows = connection.execute(
                    """
                    SELECT knowledge_id, chunk_index, chunk_text
                    FROM content_chunks
                    ORDER BY knowledge_id ASC, chunk_index ASC
                    """
                ).fetchall()
                connection.rollback()
            finally:
                connection.close()
        except PKVRuntimeError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise _safe_runtime_error(
                ErrorCode.REPAIR_REQUIRED,
                "无法以只读方式捕获 Embedding 重建源。",
                stage="embedding_source",
            ) from exc

        try:
            chunks_by_id: dict[int, list[tuple[int, str]]] = defaultdict(list)
            for row in chunk_rows:
                knowledge_id = row["knowledge_id"]
                chunk_index = row["chunk_index"]
                chunk_text = row["chunk_text"]
                if (
                    type(knowledge_id) is not int
                    or knowledge_id <= 0
                    or type(chunk_index) is not int
                    or chunk_index < 0
                    or chunk_index > VectorStore.MAX_CHUNK_INDEX
                    or not isinstance(chunk_text, str)
                    or not chunk_text.strip()
                ):
                    raise ValueError("invalid chunk projection")
                chunks_by_id[knowledge_id].append((chunk_index, chunk_text))

            records: list[EmbeddingSourceRecord] = []
            digest_records: list[dict[str, object]] = []
            seen_ids: set[int] = set()
            seen_paths: set[str] = set()
            gateway: VaultPathGateway | None = None
            if rows:
                # ``create=False`` is crucial: inspection/capture must not turn
                # a missing Vault into a directory merely to report a problem.
                gateway = VaultPathGateway(layout.vault_dir, create=False)

            for row in rows:
                knowledge_id = row["knowledge_id"]
                content = row["content"]
                file_path = row["file_path"]
                if (
                    type(knowledge_id) is not int
                    or knowledge_id <= 0
                    or knowledge_id in seen_ids
                    or not isinstance(content, str)
                    or not content.strip()
                    or not isinstance(file_path, str)
                    or not file_path
                    or file_path in seen_paths
                ):
                    raise ValueError("invalid document projection")
                seen_ids.add(knowledge_id)
                seen_paths.add(file_path)
                assert gateway is not None
                markdown_text = gateway.read_text(file_path)
                markdown_content = frontmatter.loads(markdown_text).content
                if not isinstance(markdown_content, str) or markdown_content != content:
                    raise ValueError("markdown/sqlite content drift")

                chunks = tuple(chunks_by_id.pop(knowledge_id, ()))
                if not chunks or tuple(index for index, _ in chunks) != tuple(range(len(chunks))):
                    raise ValueError("stored chunks are incomplete or non-contiguous")
                records.append(EmbeddingSourceRecord(knowledge_id, content, chunks))
                digest_records.append(
                    {
                        "knowledge_id": knowledge_id,
                        "content_sha256": _sha256_bytes(content.encode("utf-8")),
                        "chunks": [
                            {
                                "chunk_index": chunk_index,
                                "text_sha256": _sha256_bytes(chunk_text.encode("utf-8")),
                            }
                            for chunk_index, chunk_text in chunks
                        ],
                    }
                )
            if chunks_by_id:
                raise ValueError("orphan chunk projection")
            summary = EmbeddingSourceSummary(
                document_count=len(records),
                chunk_count=sum(len(record.chunks) for record in records),
                digest=_canonical_sha256(digest_records),
            )
            return CapturedEmbeddingSource(summary, tuple(records))
        except PKVRuntimeError:
            raise
        except Exception as exc:
            raise _safe_runtime_error(
                ErrorCode.REPAIR_REQUIRED,
                "Markdown、SQLite 或已存储分块投影不一致，不能重建 Embedding 索引。",
                stage="embedding_source",
            ) from exc


@dataclass(frozen=True)
class EmbeddingIssue:
    """A stable, value-free lifecycle finding."""

    code: str
    message: str
    recoverable: bool = True
    stage: str = "embedding_index"

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "recoverable": self.recoverable,
            "stage": self.stage,
        }


@dataclass(frozen=True)
class EmbeddingIndexInspection:
    """A zero-write generation inspection bound to one explicit Config object."""

    state: EmbeddingIndexState
    revision: str
    contract: EmbeddingContract | None
    source: EmbeddingSourceSummary | None
    active_generation: str | None
    previous_generation: str | None
    active_manifest_sha256: str | None
    issues: tuple[EmbeddingIssue, ...]
    _config: Any = field(repr=False, compare=False, default=None)
    _source: EmbeddingSource | None = field(repr=False, compare=False, default=None)
    _snapshot: RuntimeSnapshotDocument | None = field(repr=False, compare=False, default=None)
    _captured_source: CapturedEmbeddingSource | None = field(
        repr=False, compare=False, default=None
    )
    _config_source_revision: str | None = field(repr=False, compare=False, default=None)

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "revision": self.revision,
            "contract": self.contract.to_dict() if self.contract is not None else None,
            "source": self.source.to_dict() if self.source is not None else None,
            "active_generation": self.active_generation,
            "previous_generation": self.previous_generation,
            "active_manifest_sha256": self.active_manifest_sha256,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class EmbeddingRebuildPlan:
    """A user-confirmed, source/config-bound request to create one generation."""

    plan_id: str
    inspection: EmbeddingIndexInspection
    force: bool
    _config: Any = field(repr=False, compare=False, default=None)
    _source: EmbeddingSource | None = field(repr=False, compare=False, default=None)

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "inspection": self.inspection.to_dict(),
            "force": self.force,
            "requires_confirmation": True,
            "requires_network": True,
        }


@dataclass(frozen=True)
class EmbeddingRebuildConfirmation:
    """Explicit permission for exactly one rebuild plan and its network calls."""

    plan_id: str
    approved: bool
    allow_network: bool


@dataclass(frozen=True)
class EmbeddingIndexBinding:
    """One immutable reader binding; it never points at legacy flat vectors."""

    generation_id: str
    index_dir: Path
    pointer_revision: str
    contract: EmbeddingContract


@dataclass(frozen=True)
class EmbeddingRebuildExecution:
    """The safe result of a rebuilt and atomically activated generation."""

    inspection: EmbeddingIndexInspection
    generation_id: str
    previous_generation: str | None
    audit_completion_pending: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "inspection": self.inspection.to_dict(),
            "generation_id": self.generation_id,
            "previous_generation": self.previous_generation,
            "audit_completion_pending": self.audit_completion_pending,
        }


WriterLeaseFactory = Callable[[Any], AbstractContextManager[object]]


def _data_root_identity_sha256(config: Any) -> str:
    identity = getattr(config, "data_root_identity", None)
    if not isinstance(identity, str) or not identity:
        raise _safe_runtime_error(
            ErrorCode.REPAIR_REQUIRED,
            "当前配置没有可验证的数据根身份。",
            stage="embedding_contract",
        )
    return _sha256_bytes(identity.encode("utf-8"))


def _vectors_root(config: Any) -> Path:
    return Path(config.layout.vector_index_dir)


def _generations_root(config: Any) -> Path:
    return _vectors_root(config) / "generations"


def _staging_root(config: Any) -> Path:
    return _vectors_root(config) / "staging"


def _generation_dir(config: Any, generation_id: str) -> Path:
    if not _is_generation_id(generation_id):
        raise _safe_runtime_error(
            ErrorCode.REPAIR_REQUIRED,
            "运行态配置包含非法的 Embedding generation 标识。",
            stage="embedding_pointer",
        )
    return _generations_root(config) / generation_id


def _safe_file_sha256(layout: Any, path: Path, *, label: str) -> str:
    try:
        layout.validate_user_file(path, label=label, allow_missing=False)
        digest = hashlib.sha256()
        with layout.open_user_file(path, "rb", label=label) as source:
            while True:
                data = source.read(1024 * 1024)
                if not data:
                    break
                digest.update(data)
        return digest.hexdigest()
    except PKVRuntimeError:
        raise
    except OSError as exc:
        raise _safe_runtime_error(
            ErrorCode.REPAIR_REQUIRED,
            "Embedding generation 文件无法安全读取。",
            stage="embedding_generation",
        ) from exc


def _read_json_file(layout: Any, path: Path, *, label: str) -> tuple[dict[str, Any], str]:
    try:
        layout.validate_user_file(path, label=label, allow_missing=False)
        with layout.open_user_file(path, "rb", label=label) as source:
            raw = source.read()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or contains_secret_shaped_field(payload):
            raise ValueError("invalid generation manifest")
        return payload, _sha256_bytes(raw)
    except PKVRuntimeError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise _safe_runtime_error(
            ErrorCode.REPAIR_REQUIRED,
            "Embedding generation 清单无法安全读取。",
            stage="embedding_generation",
        ) from exc


def _write_json_file(layout: Any, path: Path, payload: Mapping[str, Any], *, label: str) -> str:
    if contains_secret_shaped_field(payload):
        raise _safe_runtime_error(
            ErrorCode.REPAIR_REQUIRED,
            "Embedding generation 清单不得包含敏感字段。",
            stage="embedding_generation",
        )
    try:
        raw = (
            json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        layout.atomic_publish_user_file(path, label=label, data=raw)
        if _safe_file_sha256(layout, path, label=label) != _sha256_bytes(raw):
            raise ValueError("published generation manifest mismatch")
        return _sha256_bytes(raw)
    except PKVRuntimeError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise _safe_runtime_error(
            ErrorCode.REPAIR_REQUIRED,
            "Embedding generation 清单无法安全写入。",
            stage="embedding_generation",
        ) from exc


def _parse_contract(payload: object) -> EmbeddingContract | None:
    if not isinstance(payload, Mapping):
        return None
    try:
        if payload.get("schema_version") != 1:
            return None
        provider = payload.get("provider")
        endpoint_hash = payload.get("endpoint_sha256")
        model = payload.get("model")
        dimension = payload.get("dimension")
        doc_pipeline = payload.get("document_pipeline_version")
        chunk_schema = payload.get("stored_chunk_schema_version")
        if (
            not isinstance(provider, str)
            or not provider
            or not _is_sha256(endpoint_hash)
            or not isinstance(model, str)
            or not model
            or type(dimension) is not int
            or dimension <= 0
            or type(doc_pipeline) is not int
            or doc_pipeline <= 0
            or type(chunk_schema) is not int
            or chunk_schema <= 0
        ):
            return None
        return EmbeddingContract(
            provider=provider,
            endpoint_sha256=endpoint_hash,
            model=model,
            dimension=dimension,
            document_pipeline_version=doc_pipeline,
            stored_chunk_schema_version=chunk_schema,
        )
    except (TypeError, ValueError):
        return None


def _parse_source_summary(payload: object) -> EmbeddingSourceSummary | None:
    if not isinstance(payload, Mapping):
        return None
    document_count = payload.get("document_count")
    chunk_count = payload.get("chunk_count")
    digest = payload.get("digest")
    if (
        type(document_count) is not int
        or document_count < 0
        or type(chunk_count) is not int
        or chunk_count < 0
        or not _is_sha256(digest)
    ):
        return None
    return EmbeddingSourceSummary(document_count, chunk_count, digest)


def _validate_manifest(
    config: Any,
    generation_id: str,
    *,
    expected_contract: EmbeddingContract | None = None,
) -> tuple[dict[str, Any], str]:
    """Validate a complete generation without creating sidecars or providers."""

    layout = config.layout
    generation_dir = _generation_dir(config, generation_id)
    try:
        layout.validate_user_directory(
            generation_dir,
            label="Embedding generation 目录",
            allow_missing=False,
        )
        manifest, manifest_sha = _read_json_file(
            layout,
            generation_dir / _GENERATION_MANIFEST_NAME,
            label="Embedding generation 清单",
        )
        if manifest.get("schema_version") != _GENERATION_MANIFEST_SCHEMA:
            raise ValueError("generation manifest schema")
        if manifest.get("generation_id") != generation_id:
            raise ValueError("generation manifest id")
        contract = _parse_contract(manifest.get("contract"))
        source = _parse_source_summary(manifest.get("source"))
        files = manifest.get("files")
        if contract is None or source is None or not isinstance(files, Mapping):
            raise ValueError("generation manifest fields")
        if expected_contract is not None and contract != expected_contract:
            raise _safe_runtime_error(
                ErrorCode.EMBEDDING_REBUILD_REQUIRED,
                "已发布 Embedding generation 与当前配置契约不一致，需要重建。",
                stage="embedding_contract",
            )
        if set(files) != set(_PAIR_FILENAMES) or not all(
            _is_sha256(files.get(name)) for name in _PAIR_FILENAMES
        ):
            raise ValueError("generation manifest file list")
        for filename in _PAIR_FILENAMES:
            actual = _safe_file_sha256(
                layout,
                generation_dir / filename,
                label="Embedding generation 文件",
            )
            if actual != files[filename]:
                raise ValueError("generation artifact digest")

        # Validate the small metadata contract before the full strict opener.
        for name in ("doc_vectors", "chunk_vectors"):
            metadata, _ = _read_json_file(
                layout,
                generation_dir / f"{name}_metadata.json",
                label="Embedding generation 元数据",
            )
            fingerprint = metadata.get(VectorStore.EMBEDDING_FINGERPRINT_V2_KEY)
            if (
                metadata.get("schema_version") != VectorStore.METADATA_SCHEMA_VERSION
                or metadata.get("dim") != contract.dimension
                or not isinstance(fingerprint, Mapping)
                or fingerprint.get("base_url_sha256") != contract.endpoint_sha256
                or fingerprint.get("embedding_model") != contract.model
                or str(fingerprint.get("embedding_dim")) != str(contract.dimension)
            ):
                raise ValueError("generation metadata contract")
        # A checksum alone cannot prove this pair is accepted by the actual
        # reader: pending pair transactions, legacy v2 fingerprints, or a
        # malformed HNSW payload must make inspect/binding fail closed too.
        # ``open_readonly`` is the production strict path and never creates a
        # directory, lock, sidecar, migration, or Provider.
        vector_config = _VectorRuntimeConfigSnapshot.capture(config, contract)
        readonly = VectorStore.open_readonly(
            generation_dir,
            dim=contract.dimension,
            runtime_config=vector_config,
            layout=config.layout,
        )
        if (
            readonly.doc_index.element_count != source.document_count
            or readonly.chunk_index.element_count != source.chunk_count
        ):
            raise ValueError("generation vector counts")
        return manifest, manifest_sha
    except PKVRuntimeError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise _safe_runtime_error(
            ErrorCode.REPAIR_REQUIRED,
            "已发布 Embedding generation 不完整或不可验证，需要修复。",
            stage="embedding_generation",
        ) from exc


def _runtime_embedding_base_matches(
    snapshot: Mapping[str, Any],
    contract: EmbeddingContract,
    *,
    config: Any,
) -> bool:
    """Require and compare R2's complete secret-free v1 base contract.

    R4 owns only the ``embedding_index`` extension.  It may not turn an absent
    or malformed R2 snapshot into a pointer-only ``local.yaml`` because Config
    and lifecycle correctly reject that as an invalid runtime state.  The
    comparison is intentionally exact for the embedding contract but excludes
    API key rotation, which must not force an index rebuild.
    """

    if snapshot.get("schema_version") != 1:
        return False
    database = snapshot.get("database")
    if (
        not isinstance(database, Mapping)
        or not isinstance(database.get("schema_version"), str)
        or _SEMVER_PATTERN.fullmatch(database["schema_version"]) is None
    ):
        return False
    embedding = snapshot.get("embedding")
    if not isinstance(embedding, Mapping):
        return False
    provider = embedding.get("provider")
    fingerprint = embedding.get("fingerprint")
    if not isinstance(provider, str) or provider != contract.provider:
        return False
    if not isinstance(fingerprint, Mapping):
        return False
    if not (
        fingerprint.get("base_url_sha256") == contract.endpoint_sha256
        and fingerprint.get("embedding_model") == contract.model
        and str(fingerprint.get("embedding_dim")) == str(contract.dimension)
    ):
        return False
    try:
        # R2 owns the complete v1 schema and its current-DB/config comparison.
        # Calling its pure internal classifier (rather than re-reading the file
        # through inspect_runtime) validates the *same raw snapshot revision*
        # that this R4 plan will later CAS-publish.  It creates neither a DB nor
        # a Provider and rejects scalar/foreign extensions that the generic
        # RuntimeSnapshotStore rightly does not interpret.
        from src.ai.provider_factory import embedding_settings_from_config
        from src.runtime.lifecycle import (
            _inspect_database,
            _runtime_snapshot_contract_state,
            _validate_runtime_snapshot_payload,
        )

        _validate_runtime_snapshot_payload(snapshot)
        database, _ = _inspect_database(config.layout)
        settings = embedding_settings_from_config(config)
        return (
            database is not None
            and database.state is DatabaseState.READY
            and _runtime_snapshot_contract_state(
                snapshot,
                config=config,
                database=database,
                embedding_settings=settings,
            )
            == "valid"
        )
    except (ImportError, AttributeError, TypeError, ValueError, PKVRuntimeError):
        return False


def _legacy_flat_index_present(config: Any) -> bool:
    """Read only the legacy artifact names; never adopt, move, or delete them."""

    layout = config.layout
    root = _vectors_root(config)
    try:
        layout.validate_user_directory(root, label="向量索引目录", allow_missing=True)
        if not root.exists():
            return False
        return any(os.path.lexists(root / filename) for filename in _PAIR_FILENAMES)
    except (OSError, PKVRuntimeError):
        # A linked/unreadable flat root is a repair issue elsewhere.  Returning
        # false here avoids projecting a path/OS detail from a readonly status.
        return False


def _pointer_extension(
    snapshot: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    extension = snapshot.get(_SNAPSHOT_EXTENSION_KEY)
    if extension is None:
        return None
    return extension if isinstance(extension, Mapping) else None


def _inspection(
    *,
    state: EmbeddingIndexState,
    contract: EmbeddingContract | None,
    source: CapturedEmbeddingSource | None,
    active_generation: str | None,
    previous_generation: str | None,
    manifest_sha: str | None,
    issues: Sequence[EmbeddingIssue],
    config: Any,
    source_reader: EmbeddingSource,
    snapshot: RuntimeSnapshotDocument | None,
    config_source_revision: str | None = None,
) -> EmbeddingIndexInspection:
    revision = _canonical_sha256(
        {
            "state": state.value,
            "contract": contract.to_dict() if contract is not None else None,
            "source": source.summary.to_dict() if source is not None else None,
            "active_generation": active_generation,
            "previous_generation": previous_generation,
            "manifest_sha": manifest_sha,
            "snapshot_sha": snapshot.raw_sha256 if snapshot is not None else None,
            "config_source_revision": config_source_revision,
        }
    )
    return EmbeddingIndexInspection(
        state=state,
        revision=revision,
        contract=contract,
        source=source.summary if source is not None else None,
        active_generation=active_generation,
        previous_generation=previous_generation,
        active_manifest_sha256=manifest_sha,
        issues=tuple(issues),
        _config=config,
        _source=source_reader,
        _snapshot=snapshot,
        _captured_source=source,
        _config_source_revision=config_source_revision,
    )


def _config_source_revision(config: Any) -> str:
    """Capture the opaque user-config source revision used by the R2 plan gate."""

    try:
        reader = getattr(config, "user_config_source_revision", None)
        if not callable(reader):
            raise ValueError("explicit Config lacks source revision")
        revision = reader()
        if not isinstance(revision, str) or not _is_sha256(revision):
            raise ValueError("invalid opaque source revision")
        return revision
    except PKVRuntimeError:
        raise
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise _safe_runtime_error(
            ErrorCode.REPAIR_REQUIRED,
            "用户配置源无法安全确认；不能创建 Embedding 重建计划。",
            stage="embedding_config_source",
        ) from exc


def inspect_embedding_index(
    config: Any,
    *,
    source: EmbeddingSource | None = None,
) -> EmbeddingIndexInspection:
    """Inspect contract/source/pointer strictly readonly.

    It never creates a data root, lock, VectorStore, Provider, staging
    directory, or audit file.  All fields in the returned DTO are safe to pass
    to a future adapter; article text and filesystem paths stay private.
    """

    if config is None or not hasattr(config, "layout"):
        raise TypeError("config 必须是显式的 Config 快照")
    source_reader = source or SQLiteEmbeddingSource()
    try:
        contract = EmbeddingContract.from_config(config)
        _data_root_identity_sha256(config)
        config_source_revision = _config_source_revision(config)
    except PKVRuntimeError as error:
        return _inspection(
            state=EmbeddingIndexState.REPAIR_REQUIRED,
            contract=None,
            source=None,
            active_generation=None,
            previous_generation=None,
            manifest_sha=None,
            issues=(
                EmbeddingIssue(
                    code=error.code.value,
                    message="当前 Embedding 配置不可验证，需要先修复。",
                    recoverable=error.recoverable,
                    stage=error.stage or "embedding_contract",
                ),
            ),
            config=config,
            source_reader=source_reader,
            snapshot=None,
        )

    try:
        snapshot = RuntimeSnapshotStore(config.layout).read()
    except PKVRuntimeError as error:
        return _inspection(
            state=EmbeddingIndexState.REPAIR_REQUIRED,
            contract=contract,
            source=None,
            active_generation=None,
            previous_generation=None,
            manifest_sha=None,
            issues=(
                EmbeddingIssue(
                    code=error.code.value,
                    message="运行态配置快照不可验证，需要修复。",
                    recoverable=error.recoverable,
                    stage=error.stage or "runtime_snapshot",
                ),
            ),
            config=config,
            source_reader=source_reader,
            snapshot=None,
        )

    try:
        captured = source_reader.capture(config)
    except PKVRuntimeError as error:
        return _inspection(
            state=EmbeddingIndexState.REPAIR_REQUIRED,
            contract=contract,
            source=None,
            active_generation=None,
            previous_generation=None,
            manifest_sha=None,
            issues=(
                EmbeddingIssue(
                    code=error.code.value,
                    message="Embedding 重建源不可验证，需要先修复数据投影。",
                    recoverable=error.recoverable,
                    stage=error.stage or "embedding_source",
                ),
            ),
            config=config,
            source_reader=source_reader,
            snapshot=snapshot,
        config_source_revision=config_source_revision,
        )
    except Exception as exc:
        return _inspection(
            state=EmbeddingIndexState.REPAIR_REQUIRED,
            contract=contract,
            source=None,
            active_generation=None,
            previous_generation=None,
            manifest_sha=None,
            issues=(
                EmbeddingIssue(
                    code=ErrorCode.REPAIR_REQUIRED.value,
                    message="Embedding 重建源不可验证，需要先修复数据投影。",
                    stage="embedding_source",
                ),
            ),
            config=config,
            source_reader=source_reader,
            snapshot=snapshot,
        config_source_revision=config_source_revision,
        )

    if not _runtime_embedding_base_matches(snapshot.payload, contract, config=config):
        return _inspection(
            state=EmbeddingIndexState.REBUILD_REQUIRED,
            contract=contract,
            source=captured,
            active_generation=None,
            previous_generation=None,
            manifest_sha=None,
            issues=(
                EmbeddingIssue(
                    code=ErrorCode.EMBEDDING_REBUILD_REQUIRED.value,
                    message=(
                        "数据运行态快照尚未完成或与当前 Embedding 配置漂移；"
                        "请先完成 R2 生命周期检查/修复，再创建 generation。"
                    ),
                    stage="runtime_snapshot",
                ),
            ),
            config=config,
            source_reader=source_reader,
            snapshot=snapshot,
        config_source_revision=config_source_revision,
        )

    extension = _pointer_extension(snapshot.payload)
    if extension is None:
        if captured.summary.document_count == 0:
            return _inspection(
                state=EmbeddingIndexState.INITIAL_GENERATION_PENDING,
                contract=contract,
                source=captured,
                active_generation=None,
                previous_generation=None,
                manifest_sha=None,
                issues=(
                    EmbeddingIssue(
                        code="embedding_initial_generation_pending",
                        message="尚无已发布的 Embedding generation；可在确认后创建初始索引。",
                    ),
                ),
                config=config,
                source_reader=source_reader,
                snapshot=snapshot,
            config_source_revision=config_source_revision,
            )
        legacy_message = (
            "检测到历史平铺向量索引；它不会被自动采用或覆盖，需要显式重建。"
            if _legacy_flat_index_present(config)
            else "尚无已发布的 Embedding generation，需要显式重建。"
        )
        return _inspection(
            state=EmbeddingIndexState.REBUILD_REQUIRED,
            contract=contract,
            source=captured,
            active_generation=None,
            previous_generation=None,
            manifest_sha=None,
            issues=(
                EmbeddingIssue(
                    code=ErrorCode.EMBEDDING_REBUILD_REQUIRED.value,
                    message=legacy_message,
                ),
            ),
            config=config,
            source_reader=source_reader,
            snapshot=snapshot,
        config_source_revision=config_source_revision,
        )

    try:
        if extension.get("schema_version") != _SNAPSHOT_EXTENSION_SCHEMA:
            raise ValueError("pointer schema")
        if extension.get("data_root_identity_sha256") != _data_root_identity_sha256(config):
            raise ValueError("pointer root identity")
        active_generation = extension.get("active_generation")
        previous_generation = extension.get("previous_generation")
        manifest_sha = extension.get("active_manifest_sha256")
        pointer_contract = _parse_contract(extension.get("contract"))
        retained = extension.get("retained_generations")
        if (
            not _is_generation_id(active_generation)
            or (previous_generation is not None and not _is_generation_id(previous_generation))
            or not _is_sha256(manifest_sha)
            or pointer_contract is None
            or not isinstance(retained, list)
            or not all(_is_generation_id(item) for item in retained)
            or active_generation not in retained
        ):
            raise ValueError("pointer fields")
        if pointer_contract != contract or not _runtime_embedding_base_matches(
            snapshot.payload,
            contract,
            config=config,
        ):
            return _inspection(
                state=EmbeddingIndexState.REBUILD_REQUIRED,
                contract=contract,
                source=captured,
                active_generation=active_generation,
                previous_generation=previous_generation,
                manifest_sha=manifest_sha,
                issues=(
                    EmbeddingIssue(
                        code=ErrorCode.EMBEDDING_REBUILD_REQUIRED.value,
                        message="当前 Embedding 配置与已发布索引契约不同，需要显式重建。",
                        stage="embedding_contract",
                    ),
                ),
                config=config,
                source_reader=source_reader,
                snapshot=snapshot,
            config_source_revision=config_source_revision,
            )
        manifest, actual_manifest_sha = _validate_manifest(
            config,
            active_generation,
            expected_contract=contract,
        )
        if actual_manifest_sha != manifest_sha:
            raise ValueError("pointer manifest digest")
        manifest_source = _parse_source_summary(manifest.get("source"))
        if manifest_source is None:
            raise ValueError("manifest source")
        if manifest_source != captured.summary:
            return _inspection(
                state=EmbeddingIndexState.REBUILD_REQUIRED,
                contract=contract,
                source=captured,
                active_generation=active_generation,
                previous_generation=previous_generation,
                manifest_sha=manifest_sha,
                issues=(
                    EmbeddingIssue(
                        code=ErrorCode.EMBEDDING_REBUILD_REQUIRED.value,
                        message="知识源投影已变化，当前 Embedding generation 需要显式重建。",
                        stage="embedding_source",
                    ),
                ),
                config=config,
                source_reader=source_reader,
                snapshot=snapshot,
            config_source_revision=config_source_revision,
            )
        return _inspection(
            state=EmbeddingIndexState.READY,
            contract=contract,
            source=captured,
            active_generation=active_generation,
            previous_generation=previous_generation,
            manifest_sha=manifest_sha,
            issues=(),
            config=config,
            source_reader=source_reader,
            snapshot=snapshot,
        config_source_revision=config_source_revision,
        )
    except PKVRuntimeError as error:
        code = (
            ErrorCode.EMBEDDING_REBUILD_REQUIRED.value
            if error.code is ErrorCode.EMBEDDING_REBUILD_REQUIRED
            else ErrorCode.REPAIR_REQUIRED.value
        )
        return _inspection(
            state=(
                EmbeddingIndexState.REBUILD_REQUIRED
                if error.code is ErrorCode.EMBEDDING_REBUILD_REQUIRED
                else EmbeddingIndexState.REPAIR_REQUIRED
            ),
            contract=contract,
            source=captured,
            active_generation=None,
            previous_generation=None,
            manifest_sha=None,
            issues=(
                EmbeddingIssue(
                    code=code,
                    message=(
                        "当前 Embedding 配置与已发布索引契约不同，需要显式重建。"
                        if code == ErrorCode.EMBEDDING_REBUILD_REQUIRED.value
                        else "Embedding generation 指针或文件不可验证，需要修复。"
                    ),
                    recoverable=error.recoverable,
                    stage=error.stage or "embedding_generation",
                ),
            ),
            config=config,
            source_reader=source_reader,
            snapshot=snapshot,
        config_source_revision=config_source_revision,
        )
    except (OSError, ValueError, TypeError):
        return _inspection(
            state=EmbeddingIndexState.REPAIR_REQUIRED,
            contract=contract,
            source=captured,
            active_generation=None,
            previous_generation=None,
            manifest_sha=None,
            issues=(
                EmbeddingIssue(
                    code=ErrorCode.REPAIR_REQUIRED.value,
                    message="Embedding generation 指针或文件不可验证，需要修复。",
                    stage="embedding_generation",
                ),
            ),
            config=config,
            source_reader=source_reader,
            snapshot=snapshot,
        config_source_revision=config_source_revision,
        )


def plan_embedding_rebuild(
    inspection: EmbeddingIndexInspection,
    *,
    force: bool = False,
) -> EmbeddingRebuildPlan:
    """Create an explicit rebuild plan; inspection itself remains zero-write."""

    if not isinstance(inspection, EmbeddingIndexInspection):
        raise TypeError("inspection 必须由 inspect_embedding_index 返回")
    if type(force) is not bool:
        raise TypeError("force 必须是 bool")
    if (
        inspection._config is None
        or inspection._source is None
        or inspection.contract is None
        or inspection._config_source_revision is None
    ):
        raise _safe_runtime_error(
            ErrorCode.REPAIR_REQUIRED,
            "Embedding 检查未绑定到可重检的显式配置与源。",
            stage="embedding_plan",
        )
    if inspection.state is EmbeddingIndexState.REPAIR_REQUIRED:
        raise _safe_runtime_error(
            ErrorCode.REPAIR_REQUIRED,
            "Embedding 源、指针或契约需要修复，不能创建重建计划。",
            stage="embedding_plan",
        )
    if (
        inspection._snapshot is None
        or not _runtime_embedding_base_matches(
            inspection._snapshot.payload,
            inspection.contract,
            config=inspection._config,
        )
    ):
        raise _safe_runtime_error(
            ErrorCode.EMBEDDING_REBUILD_REQUIRED,
            "R2 数据运行态快照尚未完成或已漂移；不能发布 Embedding generation。",
            stage="runtime_snapshot",
        )
    if inspection.state is EmbeddingIndexState.READY and not force:
        # A caller may still explicitly request a fresh generation, but forcing
        # that is intentionally visible in the plan rather than automatic.
        raise _safe_runtime_error(
            ErrorCode.EMBEDDING_REBUILD_REQUIRED,
            "当前 Embedding generation 已就绪；如需主动重建，请显式设置 force。",
            stage="embedding_plan",
        )
    plan_id = _canonical_sha256(
        {
            "revision": inspection.revision,
            "force": force,
            "action": "embedding_rebuild_v1",
        }
    )
    return EmbeddingRebuildPlan(
        plan_id=plan_id,
        inspection=inspection,
        force=force,
        _config=inspection._config,
        _source=inspection._source,
    )


def confirm_embedding_rebuild(
    plan: EmbeddingRebuildPlan,
    *,
    allow_network: bool,
    approved: bool = True,
) -> EmbeddingRebuildConfirmation:
    """Construct the caller's explicit confirmation for one approved plan."""

    if not isinstance(plan, EmbeddingRebuildPlan):
        raise TypeError("plan 必须由 plan_embedding_rebuild 返回")
    if type(allow_network) is not bool or type(approved) is not bool:
        raise TypeError("allow_network 与 approved 必须是 bool")
    return EmbeddingRebuildConfirmation(
        plan_id=plan.plan_id,
        approved=approved,
        allow_network=allow_network,
    )


def _assert_confirmation(
    plan: EmbeddingRebuildPlan,
    confirmation: EmbeddingRebuildConfirmation | None,
) -> None:
    if (
        not isinstance(confirmation, EmbeddingRebuildConfirmation)
        or confirmation.plan_id != plan.plan_id
    ):
        raise _safe_runtime_error(
            ErrorCode.RUNTIME_PLAN_STALE,
            "确认不属于当前 Embedding 重建计划。",
            stage="embedding_plan",
        )
    if (
        type(confirmation.approved) is not bool
        or type(confirmation.allow_network) is not bool
        or not confirmation.approved
        or not confirmation.allow_network
    ):
        raise _safe_runtime_error(
            ErrorCode.CONFIRMATION_REQUIRED,
            "Embedding 重建需要明确的写入与网络确认。",
            stage="embedding_plan",
        )


def _canonical_plan_matches(
    plan: EmbeddingRebuildPlan,
    current: EmbeddingIndexInspection,
) -> bool:
    try:
        canonical = plan_embedding_rebuild(current, force=plan.force)
    except PKVRuntimeError:
        return False
    return (
        canonical.plan_id == plan.plan_id
        and canonical.inspection.to_dict() == current.to_dict()
        and current.revision == plan.inspection.revision
    )


def _ensure_execution_directory(layout: Any, directory: Path, *, label: str) -> None:
    """Create one known-contained directory only after confirmation + lease."""

    try:
        layout.validate_user_directory(directory, label=label, allow_missing=True)
        ensure_safe_directory(directory, label=label)
        layout.validate_user_directory(directory, label=label, allow_missing=False)
    except PKVRuntimeError:
        raise
    except OSError as exc:
        raise _safe_runtime_error(
            ErrorCode.REPAIR_REQUIRED,
            "无法安全创建 Embedding generation 工作目录。",
            stage="embedding_generation",
        ) from exc


def _new_staging_directory(config: Any, plan_id: str) -> Path:
    root = _staging_root(config)
    layout = config.layout
    _ensure_execution_directory(layout, _vectors_root(config), label="向量索引目录")
    _ensure_execution_directory(layout, root, label="Embedding staging 目录")
    for _ in range(8):
        candidate = root / f".stage-g-{plan_id[:16]}-{uuid.uuid4().hex[:16]}"
        try:
            layout.validate_user_directory(candidate, label="Embedding staging generation", allow_missing=True)
            candidate.mkdir()
            layout.validate_user_directory(candidate, label="Embedding staging generation", allow_missing=False)
            return candidate
        except FileExistsError:
            continue
        except PKVRuntimeError:
            raise
        except OSError as exc:
            raise _safe_runtime_error(
                ErrorCode.REPAIR_REQUIRED,
                "无法安全创建 Embedding staging generation。",
                stage="embedding_generation",
            ) from exc
    raise _safe_runtime_error(
        ErrorCode.REPAIR_REQUIRED,
        "无法安全分配 Embedding staging generation。",
        stage="embedding_generation",
    )


def _validated_document_vector(value: object, *, dimension: int) -> np.ndarray:
    """Fail as a Provider protocol error before a stage can be partially written."""

    try:
        vector = np.asarray(value, dtype=np.float32)
        if (
            vector.ndim != 1
            or vector.shape[0] != dimension
            or not bool(np.all(np.isfinite(vector)))
        ):
            raise ValueError("invalid document vector")
        return vector
    except (TypeError, ValueError) as exc:
        raise _safe_runtime_error(
            ErrorCode.PROVIDER_PROTOCOL_FAILED,
            "Embedding Provider 返回的文档向量不符合已确认维度。",
            stage="embedding_rebuild_provider",
        ) from exc


def _validated_chunk_vectors(
    value: object,
    *,
    expected_count: int,
    dimension: int,
) -> np.ndarray:
    """Validate the fixed stored-chunk batch before publishing any pair update."""

    try:
        vectors = np.asarray(value, dtype=np.float32)
        if (
            vectors.ndim != 2
            or vectors.shape != (expected_count, dimension)
            or not bool(np.all(np.isfinite(vectors)))
        ):
            raise ValueError("invalid stored chunk vectors")
        return vectors
    except (TypeError, ValueError) as exc:
        raise _safe_runtime_error(
            ErrorCode.PROVIDER_PROTOCOL_FAILED,
            "Embedding Provider 返回的已存储分块向量不符合已确认维度。",
            stage="embedding_rebuild_provider",
        ) from exc


def _build_generation(
    config: Any,
    *,
    plan: EmbeddingRebuildPlan,
    captured: CapturedEmbeddingSource,
    embedder: RebuildEmbedder,
) -> tuple[Path, str, str]:
    """Build and validate a full private stage; it never flips an active pointer."""

    contract = plan.inspection.contract
    assert contract is not None
    stage_dir = _new_staging_directory(config, plan.plan_id)
    try:
        vector_config = _VectorRuntimeConfigSnapshot.capture(config, contract)
        store = VectorStore(
            stage_dir,
            dim=contract.dimension,
            runtime_config=vector_config,
            layout=config.layout,
        )
        for record in captured.records:
            try:
                document_vector = embedder.embed_document(record.content)
                chunk_vectors = embedder.embed_stored_chunks(
                    tuple(chunk for _, chunk in record.chunks)
                )
            except PKVRuntimeError:
                raise
            except Exception as exc:
                raise _safe_runtime_error(
                    ErrorCode.PROVIDER_UNAVAILABLE,
                    "Embedding Provider 在重建 generation 时失败。",
                    stage="embedding_rebuild_provider",
                ) from exc
            document_vector = _validated_document_vector(
                document_vector,
                dimension=contract.dimension,
            )
            chunk_vectors = _validated_chunk_vectors(
                chunk_vectors,
                expected_count=len(record.chunks),
                dimension=contract.dimension,
            )
            store.add_doc_vector(record.knowledge_id, document_vector)
            store.add_chunk_vectors(
                record.knowledge_id,
                [index for index, _ in record.chunks],
                chunk_vectors,
            )

        files = {
            filename: _safe_file_sha256(
                config.layout,
                stage_dir / filename,
                label="Embedding staging 文件",
            )
            for filename in _PAIR_FILENAMES
        }
        generation_id = f"g-{plan.plan_id[:20]}-{uuid.uuid4().hex[:12]}"
        manifest = {
            "schema_version": _GENERATION_MANIFEST_SCHEMA,
            "generation_id": generation_id,
            "contract": contract.to_dict(),
            "source": captured.summary.to_dict(),
            "files": files,
        }
        manifest_sha = _write_json_file(
            config.layout,
            stage_dir / _GENERATION_MANIFEST_NAME,
            manifest,
            label="Embedding staging 清单",
        )

        # Strict readonly open is deliberately used as the final validation
        # oracle.  It rejects incomplete pair transactions or metadata that a
        # writer constructor might otherwise repair/migrate in place.
        readonly = VectorStore.open_readonly(
            stage_dir,
            dim=contract.dimension,
            runtime_config=vector_config,
            layout=config.layout,
        )
        if (
            readonly.doc_index.element_count != captured.summary.document_count
            or readonly.chunk_index.element_count != captured.summary.chunk_count
        ):
            raise _safe_runtime_error(
                ErrorCode.REPAIR_REQUIRED,
                "Embedding staging generation 的向量数量与源投影不一致。",
                stage="embedding_generation",
            )
        for record in captured.records:
            if readonly.get_doc_vector(record.knowledge_id) is None:
                raise _safe_runtime_error(
                    ErrorCode.REPAIR_REQUIRED,
                    "Embedding staging generation 缺少文档向量。",
                    stage="embedding_generation",
                )
            if readonly.get_chunk_indices_for_entry(record.knowledge_id) != [
                index for index, _ in record.chunks
            ]:
                raise _safe_runtime_error(
                    ErrorCode.REPAIR_REQUIRED,
                    "Embedding staging generation 缺少已存储分块向量。",
                    stage="embedding_generation",
                )
        # The stage is intentionally not addressable through a reader binding
        # yet.  Its manifest and every artifact digest have been verified above;
        # the full ``_validate_manifest`` path runs again after the private
        # directory is moved below ``generations/`` and before pointer CAS.
        return stage_dir, generation_id, manifest_sha
    except PKVRuntimeError:
        raise
    except Exception as exc:
        raise _safe_runtime_error(
            ErrorCode.REPAIR_REQUIRED,
            "Embedding staging generation 构建或校验失败。",
            stage="embedding_generation",
        ) from exc


def _stage_generation_id(layout: Any, stage_dir: Path) -> str:
    manifest, _ = _read_json_file(
        layout,
        stage_dir / _GENERATION_MANIFEST_NAME,
        label="Embedding staging 清单",
    )
    generation_id = manifest.get("generation_id")
    if not _is_generation_id(generation_id):
        raise _safe_runtime_error(
            ErrorCode.REPAIR_REQUIRED,
            "Embedding staging 清单包含非法 generation 标识。",
            stage="embedding_generation",
        )
    return generation_id


def _publish_generation_directory(
    config: Any,
    stage_dir: Path,
    *,
    expected_generation_id: str,
    expected_manifest_sha: str,
    expected_contract: EmbeddingContract,
    expected_source: EmbeddingSourceSummary,
) -> tuple[str, str]:
    """Reserve then materialize a validated stage without replacing a generation.

    A generation remains unreferenced until the later runtime-snapshot CAS, so
    its directory need not itself be atomically visible.  Reserving an empty
    final directory with ``mkdir(exist_ok=False)`` is safer than replacing a
    possibly-existing directory: a collision or hostile race becomes stale and
    never clobbers a retained rollback generation.
    """

    layout = config.layout
    generation_id = _stage_generation_id(layout, stage_dir)
    generations_root = _generations_root(config)
    _ensure_execution_directory(layout, generations_root, label="Embedding generations 目录")
    final_dir = _generation_dir(config, generation_id)
    try:
        staged_manifest, staged_manifest_sha = _read_json_file(
            layout,
            stage_dir / _GENERATION_MANIFEST_NAME,
            label="Embedding staging 清单",
        )
        if (
            generation_id != expected_generation_id
            or staged_manifest_sha != expected_manifest_sha
            or _parse_contract(staged_manifest.get("contract")) != expected_contract
            or _parse_source_summary(staged_manifest.get("source")) != expected_source
        ):
            raise _safe_runtime_error(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Embedding staging generation 在发布前已变化；未切换 active generation。",
                stage="embedding_generation",
            )
        layout.validate_user_directory(final_dir, label="Embedding generation 目录", allow_missing=True)
        try:
            final_dir.mkdir()
        except FileExistsError as exc:
            raise _safe_runtime_error(
                ErrorCode.RUNTIME_PLAN_STALE,
                "目标 Embedding generation 已存在；请重新生成计划。",
                stage="embedding_generation",
            ) from exc
        layout.validate_user_directory(final_dir, label="Embedding generation 目录", allow_missing=False)
        # Move each leaf only into our freshly reserved, unreferenced directory.
        # A crash leaves an unreferenced partial generation for later diagnosis;
        # it cannot affect any reader because pointer activation has not begun.
        for child in stage_dir.iterdir():
            layout.validate_user_file(
                child,
                label="Embedding staging 文件",
                allow_missing=False,
            )
            destination = final_dir / child.name
            layout.validate_user_file(
                destination,
                label="Embedding generation 文件",
                allow_missing=True,
            )
            os.replace(child, destination)
        stage_dir.rmdir()
        manifest, manifest_sha = _validate_manifest(
            config,
            generation_id,
            expected_contract=expected_contract,
        )
        if (
            manifest_sha != expected_manifest_sha
            or _parse_source_summary(manifest.get("source")) != expected_source
        ):
            raise ValueError("published generation manifest changed")
        return generation_id, manifest_sha
    except PKVRuntimeError:
        raise
    except OSError as exc:
        raise _safe_runtime_error(
            ErrorCode.REPAIR_REQUIRED,
            "Embedding generation 无法安全发布到保留目录。",
            stage="embedding_generation",
        ) from exc


def _retained_generation_ids(
    snapshot: RuntimeSnapshotDocument,
    *,
    new_active: str,
    previous_active: str | None,
) -> list[str]:
    extension = _pointer_extension(snapshot.payload)
    historical = extension.get("retained_generations", []) if extension is not None else []
    values = [new_active]
    if previous_active is not None:
        values.append(previous_active)
    if isinstance(historical, list):
        values.extend(item for item in historical if _is_generation_id(item))
    retained: list[str] = []
    for value in values:
        if value not in retained:
            retained.append(value)
    return retained


def _pointer_update(
    config: Any,
    snapshot: RuntimeSnapshotDocument,
    *,
    contract: EmbeddingContract,
    generation_id: str,
    previous_generation: str | None,
    manifest_sha: str,
) -> RuntimeSnapshotDocument:
    extension = {
        "schema_version": _SNAPSHOT_EXTENSION_SCHEMA,
        "data_root_identity_sha256": _data_root_identity_sha256(config),
        "active_generation": generation_id,
        "previous_generation": previous_generation,
        "retained_generations": _retained_generation_ids(
            snapshot,
            new_active=generation_id,
            previous_active=previous_generation,
        ),
        "active_manifest_sha256": manifest_sha,
        "contract": contract.to_dict(),
    }
    return RuntimeSnapshotStore(config.layout).publish(
        snapshot,
        snapshot.merged({_SNAPSHOT_EXTENSION_KEY: extension}),
    )


def _audit_secret_values(config: Any) -> tuple[str, ...]:
    values = (getattr(config, "llm_api_key", None), getattr(config, "embd_api_key", None))
    return tuple(value for value in values if isinstance(value, str) and value)


def _audit_source_records(captured: CapturedEmbeddingSource) -> list[dict[str, object]]:
    """Full article/chunk provenance for the deliberately local audit channel."""

    return [
        {
            "knowledge_id": record.knowledge_id,
            "article_text": record.content,
            "stored_chunks": [
                {"chunk_index": index, "text": text} for index, text in record.chunks
            ],
        }
        for record in captured.records
    ]


def execute_embedding_rebuild(
    plan: EmbeddingRebuildPlan,
    confirmation: EmbeddingRebuildConfirmation | None,
    *,
    embedder: RebuildEmbedder,
    writer_lease_factory: WriterLeaseFactory | None = None,
) -> EmbeddingRebuildExecution:
    """Build, strictly validate, retain, and atomically activate one generation.

    No live provider is constructed here: callers must inject an approved
    provider, while default tests inject deterministic fakes.  ``write_busy``
    occurs before audit creation, leaving a competing writer's active index and
    audit file untouched.
    """

    if not isinstance(plan, EmbeddingRebuildPlan):
        raise TypeError("plan 必须由 plan_embedding_rebuild 返回")
    if plan._config is None or plan._source is None:
        raise _safe_runtime_error(
            ErrorCode.RUNTIME_PLAN_STALE,
            "Embedding 重建计划未绑定到原始显式配置与源。",
            stage="embedding_plan",
        )
    if (
        embedder is None
        or not callable(getattr(embedder, "embed_document", None))
        or not callable(getattr(embedder, "embed_stored_chunks", None))
    ):
        # The Protocol remains structural so normal adapters do not need to
        # inherit a test-only base class.
        raise TypeError(
            "embedder 必须显式提供 embed_document 与 embed_stored_chunks"
        )
    _assert_confirmation(plan, confirmation)

    current = inspect_embedding_index(plan._config, source=plan._source)
    if not _canonical_plan_matches(plan, current):
        raise _safe_runtime_error(
            ErrorCode.RUNTIME_PLAN_STALE,
            "Embedding 源、配置或运行态指针已变化；请重新检查并生成计划。",
            stage="embedding_plan",
        )

    if writer_lease_factory is None:
        from src.runtime.write_lease import write_lease_scope

        lease = write_lease_scope(plan._config.layout)
    else:
        lease = writer_lease_factory(plan._config)

    with lease:
        current = inspect_embedding_index(plan._config, source=plan._source)
        if not _canonical_plan_matches(plan, current):
            raise _safe_runtime_error(
                ErrorCode.RUNTIME_PLAN_STALE,
                "等待写入权限期间 Embedding 源、配置或指针已变化。",
                stage="embedding_plan",
            )
        assert current.contract is not None
        assert current._snapshot is not None
        assert current._captured_source is not None

        # Audit starts only once the writer lease is held.  It deliberately
        # retains full source articles/chunks locally while redacting configured
        # Provider values, inline credentials and credential-bearing URLs.
        from src.runtime.audit import AuditTrace, AuditTraceError

        trace = AuditTrace(plan._config.layout, secret_values=_audit_secret_values(plan._config))
        audit_context = {
            "embedding_contract_sha256": _canonical_sha256(current.contract.to_dict()),
            "config_source_revision": current._config_source_revision,
            "data_root_identity_sha256": _data_root_identity_sha256(plan._config),
            "plan_id": plan.plan_id,
            "embedding_contract": current.contract.to_dict(),
        }
        with trace.operation("embedding_rebuild", context=audit_context) as audit:
            try:
                trace.append(
                    {
                        "operation": "embedding_rebuild",
                        "phase": "source_captured",
                        "plan_id": plan.plan_id,
                        "source": _audit_source_records(current._captured_source),
                    }
                )
                stage_dir, staged_generation_id, staged_manifest_sha = _build_generation(
                    plan._config,
                    plan=plan,
                    captured=current._captured_source,
                    embedder=embedder,
                )

                # A changed source/config/pointer during expensive Provider work
                # may leave an unreferenced staging directory, but can never
                # change the active generation.
                before_publish = inspect_embedding_index(plan._config, source=plan._source)
                if before_publish.revision != current.revision:
                    raise _safe_runtime_error(
                        ErrorCode.RUNTIME_PLAN_STALE,
                        "Embedding 重建期间源、配置或指针已变化；未切换 active generation。",
                        stage="embedding_plan",
                    )
                generation_id, manifest_sha = _publish_generation_directory(
                    plan._config,
                    stage_dir,
                    expected_generation_id=staged_generation_id,
                    expected_manifest_sha=staged_manifest_sha,
                    expected_contract=current.contract,
                    expected_source=current._captured_source.summary,
                )
                # This durable pre-activation record makes a later audit fsync
                # failure reconcilable without claiming that the pointer flip
                # rolled back.  Its append must succeed before the irreversible
                # snapshot CAS; otherwise active remains unchanged.
                trace.append(
                    {
                        "operation": "embedding_rebuild",
                        "phase": "activation_intent",
                        "plan_id": plan.plan_id,
                        "generation_id": generation_id,
                        "previous_generation": current.active_generation,
                        "manifest_sha256": manifest_sha,
                    }
                )
                published_snapshot = _pointer_update(
                    plan._config,
                    current._snapshot,
                    contract=current.contract,
                    generation_id=generation_id,
                    previous_generation=current.active_generation,
                    manifest_sha=manifest_sha,
                )
                # Do not re-capture mutable source data after a successful CAS.
                # The immediately preceding inspection was the activation
                # guard under the root-wide writer lease; the manifest was
                # validated after its move, and RuntimeSnapshotStore.publish
                # re-reads the exact pointer bytes.  A later manual source edit
                # is a *new* operation's drift, not a reason to report this
                # already-activated generation as a failed build.
                inspected_after = _inspection(
                    state=EmbeddingIndexState.READY,
                    contract=current.contract,
                    source=current._captured_source,
                    active_generation=generation_id,
                    previous_generation=current.active_generation,
                    manifest_sha=manifest_sha,
                    issues=(),
                    config=plan._config,
                    source_reader=plan._source,
                    snapshot=published_snapshot,
                    config_source_revision=current._config_source_revision,
                )
                audit_completion_pending = False
                try:
                    audit.complete(
                        {
                            "generation_id": generation_id,
                            "previous_generation": current.active_generation,
                            "source": inspected_after.source.to_dict()
                            if inspected_after.source is not None
                            else None,
                        }
                    )
                except AuditTraceError:
                    # Pointer CAS already committed.  Return that fact rather
                    # than a false failure/stale result; an adapter must surface
                    # this warning and reconcile the preceding activation_intent
                    # record before a future cleanup/rebuild decision.
                    audit.mark_completion_pending_after_commit()
                    audit_completion_pending = True
                return EmbeddingRebuildExecution(
                    inspection=inspected_after,
                    generation_id=generation_id,
                    previous_generation=current.active_generation,
                    audit_completion_pending=audit_completion_pending,
                )
            except PKVRuntimeError as error:
                try:
                    audit.fail_runtime_error(error, details={"plan_id": plan.plan_id})
                except AuditTraceError:
                    # A diagnostic sink failure must not replace the original
                    # typed no-activation error.  The context manager preserves
                    # that business exception on its best-effort terminal path.
                    pass
                raise


def resolve_embedding_index_binding(
    config: Any,
    *,
    source: EmbeddingSource | None = None,
) -> EmbeddingIndexBinding:
    """Resolve one ready active generation without opening a writer store.

    Adapters must keep this returned binding for the whole logical read
    operation.  A later pointer flip then affects only later operations, which
    prevents a query from mixing old and new index files.
    """

    inspection = inspect_embedding_index(config, source=source)
    if inspection.state is EmbeddingIndexState.READY:
        assert inspection.active_generation is not None
        assert inspection.contract is not None
        return EmbeddingIndexBinding(
            generation_id=inspection.active_generation,
            index_dir=_generation_dir(config, inspection.active_generation),
            pointer_revision=inspection.revision,
            contract=inspection.contract,
        )
    if inspection.state in {
        EmbeddingIndexState.REBUILD_REQUIRED,
        EmbeddingIndexState.INITIAL_GENERATION_PENDING,
    }:
        raise _safe_runtime_error(
            ErrorCode.EMBEDDING_REBUILD_REQUIRED,
            "当前没有与配置和知识源一致的 Embedding generation；需要显式重建。",
            stage="embedding_index",
        )
    raise _safe_runtime_error(
        ErrorCode.REPAIR_REQUIRED,
        "Embedding generation 状态不可验证，需要修复。",
        stage="embedding_index",
    )


__all__ = [
    "CapturedEmbeddingSource",
    "EmbeddingContract",
    "EmbeddingIndexBinding",
    "EmbeddingIndexInspection",
    "EmbeddingIndexState",
    "EmbeddingIssue",
    "EmbeddingRebuildConfirmation",
    "EmbeddingRebuildExecution",
    "EmbeddingRebuildPlan",
    "EmbeddingSource",
    "EmbeddingSourceRecord",
    "EmbeddingSourceSummary",
    "PreChunkedEmbeddingAdapter",
    "RebuildEmbedder",
    "SQLiteEmbeddingSource",
    "confirm_embedding_rebuild",
    "execute_embedding_rebuild",
    "inspect_embedding_index",
    "plan_embedding_rebuild",
    "resolve_embedding_index_binding",
]
