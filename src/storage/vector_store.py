"""
向量存储层

基于 hnswlib 的向量索引管理
"""

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

import hnswlib
import numpy as np
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import (
    atomic_publish_file,
    ensure_safe_directory,
    lexically_within,
    open_user_file_nofollow,
    validate_directory_components,
    validate_path_components,
    verify_fd_matches_path,
)
from src.utils.config import (
    endpoint_contract_sha256,
    get_config,
    url_contains_credentials,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)

_INVALID_VECTOR_WRITE_INPUT = "向量写入输入不符合索引契约"
_INVALID_VECTOR_QUERY_INPUT = "查询向量不符合索引契约"
_INVALID_DOCUMENT_VECTOR_READ = "文档向量索引内容不一致"


@dataclass(frozen=True)
class _PathContract:
    """统一 containment/link 合同：可选 layout（完整合同）或裸 validator。"""

    layout: Any = None
    validator: Optional[Callable[..., Any]] = None


@dataclass
class _PairTransaction:
    """内存中的持久 index/metadata 配对事务。"""

    name: str
    payload: dict[str, Any]
    marker_identity: Optional[tuple[int, int]] = None
    marker_sha256: Optional[str] = None


def _contract_validate(contract: Optional[_PathContract], path: Path, *, label: str) -> Path:
    """对单个叶子执行统一链接/包含检查（不打开）。"""
    if contract is not None and contract.validator is not None:
        return contract.validator(path, label=label)
    return validate_path_components(path, label=label)


def _contract_validate_dir(
    contract: Optional[_PathContract], path: Path, *, label: str
) -> Path:
    """对单个目录叶子执行统一链接/包含检查。"""
    if contract is not None and contract.layout is not None:
        return contract.layout.validate_user_directory(path, label=label)
    return validate_directory_components(path, label=label)


def _contract_open(
    contract: Optional[_PathContract],
    path: Path,
    mode: str,
    *,
    label: str,
    encoding: Optional[str] = None,
    newline: Optional[str] = None,
):
    """按合同打开叶子：有 layout 时走 O_NOFOLLOW + 身份核验的完整路径。"""
    if contract is not None and contract.layout is not None:
        return contract.layout.open_user_file(
            path,
            mode,
            label=label,
            encoding=encoding,
            newline=newline,
        )
    if contract is not None and contract.validator is not None:
        target = contract.validator(path, label=label)
    else:
        target = validate_path_components(path, label=label)
    return open_user_file_nofollow(
        target,
        mode,
        label=label,
        encoding=encoding,
        newline=newline,
    )


def _contract_publish(
    contract: Optional[_PathContract],
    path: Path,
    *,
    label: str,
    writer: Optional[Callable[[Path], None]] = None,
    data: Optional[bytes] = None,
    pre_replace: Optional[Callable[[], None]] = None,
) -> None:
    """按合同写完整临时文件后原子发布（不能通过链接覆盖根外目标）。"""
    if contract is not None and contract.layout is not None:
        contract.layout.atomic_publish_user_file(
            path,
            label=label,
            writer=writer,
            data=data,
            pre_replace=pre_replace,
        )
        return
    if contract is not None and contract.validator is not None:
        atomic_publish_file(
            path,
            label=label,
            writer=writer,
            data=data,
            extra_validate=lambda candidate: contract.validator(candidate, label=label),
            pre_replace=pre_replace,
        )
        return
    atomic_publish_file(
        path,
        label=label,
        writer=writer,
        data=data,
        pre_replace=pre_replace,
    )


def _read_contract_bytes(
    path: Path,
    contract: Optional[_PathContract],
    *,
    label: str,
) -> bytes:
    """按合同读取叶子完整字节（CAS 并发写保护用）。"""
    with _contract_open(contract, path, "rb", label=label) as source:
        return source.read()


class _UnsupportedMetadataFormatError(RuntimeError):
    """metadata 使用了不受支持、畸形或相互冲突的格式。"""


class _FutureMetadataSchemaError(_UnsupportedMetadataFormatError):
    """metadata 使用了当前 reader 不认识的未来 schema。"""


def _is_idempotent_delete_error(error: RuntimeError) -> bool:
    """Recognize only hnswlib's exact benign delete terminal messages."""

    return str(error) in {
        "Label not found",
        "The requested to delete element is already deleted",
    }


class VectorStore:
    """hnswlib 向量索引管理器"""

    CHUNK_ID_STRIDE = 10000
    MAX_CHUNK_INDEX = CHUNK_ID_STRIDE - 1
    METADATA_SCHEMA_VERSION = 2
    EMBEDDING_FINGERPRINT_SCHEMA_VERSION = 2
    LEGACY_EMBEDDING_FINGERPRINT_KEY = "embedding_fingerprint"
    EMBEDDING_FINGERPRINT_V2_KEY = "embedding_fingerprint_v2"
    PAIR_TRANSACTION_SCHEMA_VERSION = 1
    PAIR_NAMES = ("doc_vectors", "chunk_vectors")

    def __init__(
        self,
        index_dir: Path,
        dim: Optional[int] = None,
        *,
        layout: Any = None,
        path_validator: Optional[Callable[..., Any]] = None,
        allow_index_creation: bool = True,
    ):
        """
        初始化向量索引

        Args:
            index_dir: 向量索引目录
            dim: 向量维度；未传入时优先沿用已有索引维度，否则回落到配置值
            layout: 显式注入的 RuntimeLayout（测试/运维 seam）；缺省时若
                index_dir 位于已声明用户数据根内则自动启用完整 containment 合同
            path_validator: 显式注入的叶子验证器（测试 seam）
            allow_index_creation: 是否允许为缺失 pair 创建新索引。只读检索入口
                必须传入 False，避免把丢失或损坏的 pair 解释为空索引。
        """
        if not isinstance(allow_index_creation, bool):
            raise TypeError("allow_index_creation 必须是 bool")
        self.index_dir = Path(index_dir)
        self._allow_index_creation = allow_index_creation
        self._contract = self._resolve_path_contract(
            self.index_dir,
            layout=layout,
            path_validator=path_validator,
        )
        if self._contract.layout is not None:
            # 产品目录由 bootstrap 创建；这里幂等重建并拒绝链接/越界。
            self._contract.layout.ensure_user_directories()
            self._contract.layout.validate_user_directory(
                self.index_dir,
                label="向量索引目录",
            )
            if not self.index_dir.is_dir():
                # 父路径已验证；单个叶目录由我们创建（不递归，避免穿过链接）。
                self.index_dir.mkdir()
        else:
            ensure_safe_directory(self.index_dir, label="向量索引目录")
        self._active_pair_transactions: dict[str, _PairTransaction] = {}
        self._validated_chunk_pair_key: tuple[int, int, int, int, int, str] | None = (
            None
        )
        # 必须先恢复跨 index/metadata 两次发布的中断事务；否则后续维度解析或
        # metadata 迁移会把可恢复的半提交状态误判为永久损坏。
        self._recover_incomplete_pair_transactions()
        if not self._allow_index_creation:
            self._require_complete_index_pairs()
        self._migrate_legacy_embedding_fingerprints()
        self.dim = self._resolve_index_dim(dim)
        self.embedding_fingerprint = self._resolve_embedding_fingerprint(self.dim)
        self._remove_legacy_fingerprints_for_credential_endpoint()

        # HNSW 参数
        self.M = 16  # 每个节点的连接数
        self.ef_construction = 200  # 构建时搜索深度
        self.ef_search = 50  # 查询时搜索深度

        # 初始化文档级和分块级索引
        self.doc_index = self._init_index("doc_vectors")
        self.chunk_index = self._init_index("chunk_vectors")

        logger.info("向量存储初始化完成")

    def _require_complete_index_pairs(self) -> None:
        """只读打开前要求 doc/chunk 两个 pair 均完整存在。

        正常 ``VectorStore`` 初始化总是发布两个 pair；只剩其中一个 pair 或单个
        artifact 表示状态不可判定。这里在任何 metadata 迁移或新索引发布前拒绝，
        从而保证检索不会以写入空索引的方式掩盖损坏。
        """

        for name in self.PAIR_NAMES:
            with self._index_pair_lock(name):
                self._recover_pair_transaction_locked(name)
                index_path = self.index_dir / f"{name}.idx"
                metadata_path = self.index_dir / f"{name}_metadata.json"
                index_exists = os.path.lexists(index_path)
                metadata_exists = os.path.lexists(metadata_path)
                if not index_exists or not metadata_exists:
                    raise PKVRuntimeError(
                        ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                        f"{name} index/metadata pair 不完整，拒绝只读加载",
                        stage="vector_index_pair_load",
                        recoverable=True,
                    )
                self._validate_leaf(
                    index_path,
                    label="向量索引文件",
                    allow_missing=False,
                )
                self._validate_leaf(
                    metadata_path,
                    label="向量元数据文件",
                    allow_missing=False,
                )

    @staticmethod
    def _resolve_path_contract(
        index_dir: Path,
        *,
        layout: Any = None,
        path_validator: Optional[Callable[..., Any]] = None,
    ) -> _PathContract:
        """解析本实例的 containment/link 合同。

        显式 ``layout``/``path_validator`` 优先；否则当 ``index_dir`` 词法上位于
        已声明用户数据根内时自动启用完整合同（产品路径必然满足），任意测试目录
        则退化为本地链接/硬链接检查。
        """
        if layout is not None:
            return _PathContract(layout=layout, validator=layout.writable_user_path)
        if path_validator is not None:
            return _PathContract(layout=None, validator=path_validator)
        config = get_config()
        candidate_layout = getattr(config, "layout", None)
        if candidate_layout is not None:
            candidate = Path(
                os.path.abspath(os.path.normpath(os.fspath(index_dir)))
            )
            if lexically_within(
                candidate,
                candidate_layout.user_data_root,
                allow_equal=True,
            ):
                return _PathContract(
                    layout=candidate_layout,
                    validator=candidate_layout.writable_user_path,
                )
        return _PathContract(layout=None, validator=None)

    def _validate_leaf(
        self,
        path: Path,
        *,
        label: str,
        allow_missing: bool = True,
    ) -> Path:
        """统一链接/包含合同：任何读取或写入前的叶子检查。"""
        if allow_missing:
            return _contract_validate(self._contract, path, label=label)
        if self._contract.layout is not None:
            return self._contract.layout.validate_user_file(
                path,
                label=label,
                allow_missing=False,
            )
        target = validate_path_components(path, label=label)
        if not os.path.lexists(target):
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"{label}不存在: {target}",
            )
        return target

    def _open_leaf(
        self,
        path: Path,
        mode: str = "rb",
        *,
        label: str,
        encoding: Optional[str] = None,
        newline: Optional[str] = None,
    ):
        """通过统一合同打开叶子；无 layout 时仍做链接/身份核验。"""
        return _contract_open(
            self._contract,
            path,
            mode,
            label=label,
            encoding=encoding,
            newline=newline,
        )

    @classmethod
    def has_index_artifacts(cls, index_dir: Path) -> bool:
        """检查索引目录中是否已经存在向量索引相关文件。"""
        target_dir = validate_path_components(Path(index_dir), label="向量索引目录")
        if not os.path.lexists(target_dir):
            return False
        validate_directory_components(target_dir, label="向量索引目录")
        for name in cls.PAIR_NAMES:
            index_path = target_dir / f"{name}.idx"
            metadata_path = target_dir / f"{name}_metadata.json"
            transaction_path = target_dir / f".{name}.pair-transaction.json"
            for artifact in (index_path, metadata_path, transaction_path):
                if os.path.lexists(artifact):
                    validate_path_components(artifact, label="向量索引文件")
                    return True
        return False

    def _resolve_index_dim(self, requested_dim: Optional[int]) -> int:
        """解析当前索引目录应使用的向量维度。"""
        metadata_dims: dict[str, int] = {}
        for name in ("doc_vectors", "chunk_vectors"):
            metadata_path = self.index_dir / f"{name}_metadata.json"
            with self._index_pair_lock(name):
                self._recover_pair_transaction_locked(name)
                if not os.path.lexists(metadata_path):
                    continue

                with self._open_leaf(
                    metadata_path,
                    "r",
                    encoding="utf-8",
                    label="向量元数据文件",
                ) as f:
                    metadata = json.load(f)

            dim = metadata.get("dim")
            if dim is None:
                raise RuntimeError(f"{name} 缺少 dim 元数据，无法安全加载索引")
            metadata_dims[name] = int(dim)

        unique_dims = set(metadata_dims.values())
        if len(unique_dims) > 1:
            raise RuntimeError(
                f"索引目录存在不一致的维度定义: {metadata_dims}，请先人工修复"
            )

        existing_dim = next(iter(unique_dims), None)
        if existing_dim is not None:
            if requested_dim is not None and int(requested_dim) != existing_dim:
                raise RuntimeError(
                    "索引维度不匹配: "
                    f"已有={existing_dim}, 当前请求={int(requested_dim)}。"
                    "当前初始化不会自动重建索引。"
                    "如果要继续使用现有索引，请切回原来的 Embedding 服务/模型/维度配置；"
                    "如果确认切换模型，请先重建向量索引。"
                )
            return existing_dim

        if requested_dim is not None:
            return int(requested_dim)

        config_dim = get_config().embedding_dim
        if config_dim is None:
            raise RuntimeError(
                "当前未解析 Embedding 维度，无法创建新索引。"
                "请先完成一次 Embedding 请求以写入运行期缓存，或显式传入 dim。"
            )
        return int(config_dim)

    def _resolve_embedding_fingerprint(self, dim: int) -> dict[str, Any]:
        """解析当前向量索引应绑定的 Embedding 契约指纹。"""
        config = get_config()
        resolved: Any = None
        if hasattr(config, "embedding_index_fingerprint"):
            resolved = config.embedding_index_fingerprint(dim)

        resolved = resolved if isinstance(resolved, dict) else {}
        endpoint = str(
            resolved.get("base_url", getattr(config, "embd_base_url", ""))
        )
        endpoint_hash = str(resolved.get("base_url_sha256") or "").lower()
        if not self._is_sha256(endpoint_hash):
            endpoint_hash = endpoint_contract_sha256(endpoint)
        normalized = {
            "base_url_sha256": endpoint_hash,
            "embedding_model": str(
                resolved.get(
                    "embedding_model", str(getattr(config, "embd_model", ""))
                )
            ),
            "embedding_dim": str(int(dim)),
        }
        self._legacy_embedding_fingerprint = self._legacy_fingerprint(
            normalized,
            endpoint,
        )
        return self._persisted_embedding_fingerprint(normalized)

    @staticmethod
    def _is_sha256(value: str) -> bool:
        return len(value) == 64 and all(
            char in "0123456789abcdef" for char in value.lower()
        )

    @classmethod
    def _normalize_stored_embedding_fingerprint(
        cls, fingerprint: dict[str, Any]
    ) -> dict[str, str]:
        """只提取可安全比较的 endpoint hash/model/dim 契约。"""
        endpoint_hash = str(fingerprint.get("base_url_sha256") or "").lower()
        if not cls._is_sha256(endpoint_hash):
            legacy_base_url = fingerprint.get("base_url")
            endpoint_hash = (
                endpoint_contract_sha256(str(legacy_base_url))
                if legacy_base_url is not None
                else "<invalid>"
            )
        return {
            "base_url_sha256": endpoint_hash,
            "embedding_model": str(fingerprint.get("embedding_model", "")),
            "embedding_dim": str(fingerprint.get("embedding_dim", "")),
        }

    @classmethod
    def _persisted_embedding_fingerprint(
        cls,
        normalized: dict[str, str],
    ) -> dict[str, Any]:
        """构造不含 endpoint 原文的正式 v2 指纹。"""
        return {
            "schema_version": cls.EMBEDDING_FINGERPRINT_SCHEMA_VERSION,
            "base_url_sha256": normalized["base_url_sha256"],
            "embedding_model": normalized["embedding_model"],
            "embedding_dim": normalized["embedding_dim"],
        }

    @staticmethod
    def _legacy_fingerprint(
        normalized: dict[str, str],
        legacy_base_url: Any = None,
    ) -> Optional[dict[str, str]]:
        """仅为无凭据 endpoint 构造 82381bb 可读取的 raw-v1 指纹。"""
        if legacy_base_url is not None:
            endpoint = str(legacy_base_url)
            if not url_contains_credentials(endpoint):
                return {
                    "base_url": endpoint,
                    "embedding_model": normalized["embedding_model"],
                    "embedding_dim": normalized["embedding_dim"],
                }
        # 含凭据 endpoint 不得保留旧键：旧 reader 将走缺失-warning 路径，
        # 避免 mismatch 异常把其 raw expected endpoint 回显出来。
        return None

    @classmethod
    def _read_json_snapshot(
        cls,
        path: Path,
        *,
        validate_schema: bool = True,
        contract: Optional[_PathContract] = None,
    ) -> tuple[dict[str, Any], bytes]:
        """一次读取 metadata 内容及其 CAS 字节快照（走链接安全合同）。"""
        with _contract_open(contract, path, "rb", label="向量元数据文件") as source:
            original_bytes = source.read()
        payload = json.loads(original_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("metadata 必须是 JSON object")
        if validate_schema:
            cls._validate_supported_metadata(payload)
        return payload, original_bytes

    @classmethod
    def _validate_supported_metadata(cls, metadata: dict[str, Any]) -> None:
        """畸形、未来或相互冲突的格式必须在任何改写前 fail closed。"""
        versioned_values: list[tuple[str, Any, int]] = []
        if "schema_version" in metadata:
            versioned_values.append(
                (
                    "metadata",
                    metadata["schema_version"],
                    cls.METADATA_SCHEMA_VERSION,
                )
            )
        for key in (
            cls.EMBEDDING_FINGERPRINT_V2_KEY,
            cls.LEGACY_EMBEDDING_FINGERPRINT_KEY,
        ):
            if key in metadata and not isinstance(metadata[key], dict):
                raise _UnsupportedMetadataFormatError(
                    f"{key} 必须是 JSON object，拒绝读取或改写"
                )
            fingerprint = metadata.get(key)
            if isinstance(fingerprint, dict) and "schema_version" in fingerprint:
                versioned_values.append(
                    (
                        "fingerprint",
                        fingerprint["schema_version"],
                        cls.EMBEDDING_FINGERPRINT_SCHEMA_VERSION,
                    )
                )
            if isinstance(fingerprint, dict) and "base_url" in fingerprint:
                endpoint_hash = str(
                    fingerprint.get("base_url_sha256") or ""
                ).lower()
                if cls._is_sha256(endpoint_hash) and endpoint_hash != (
                    endpoint_contract_sha256(str(fingerprint["base_url"]))
                ):
                    raise _UnsupportedMetadataFormatError(
                        f"{key} 内部 endpoint hash/base_url 冲突，拒绝改写"
                    )

        for label, value, supported_version in versioned_values:
            if type(value) is not int or value < 0:
                raise _UnsupportedMetadataFormatError(
                    f"{label} schema_version 必须是非负 JSON integer，拒绝改写"
                )
            if value > supported_version:
                raise _FutureMetadataSchemaError(
                    f"检测到高于当前版本的 {label} schema，拒绝读取或改写"
                )

        fingerprint_v2 = metadata.get(cls.EMBEDDING_FINGERPRINT_V2_KEY)
        legacy_fingerprint = metadata.get(cls.LEGACY_EMBEDDING_FINGERPRINT_KEY)
        if isinstance(fingerprint_v2, dict) and isinstance(
            legacy_fingerprint, dict
        ):
            if cls._normalize_stored_embedding_fingerprint(
                fingerprint_v2
            ) != cls._normalize_stored_embedding_fingerprint(legacy_fingerprint):
                raise _UnsupportedMetadataFormatError(
                    "v2 与 legacy Embedding 契约冲突，拒绝读取或改写"
                )

    @staticmethod
    @contextmanager
    def _metadata_write_lock(
        path: Path,
        *,
        contract: Optional[_PathContract] = None,
    ):
        """用同目录 sidecar advisory lock 串行化当前版本的 metadata writer。"""
        lock_path = path.with_name(f".{path.name}.lock")
        lock_file = _contract_open(
            contract,
            lock_path,
            "a+b",
            label="向量索引锁文件",
        )
        acquired = False
        try:
            if lock_file.seek(0, os.SEEK_END) == 0:
                lock_file.write(b"\0")
                lock_file.flush()

            deadline = time.monotonic() + 10.0
            while not acquired:
                try:
                    lock_file.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"等待 {path.name} metadata 写锁超时，请重试"
                        ) from None
                    time.sleep(0.01)
            yield
        finally:
            if acquired:
                try:
                    lock_file.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            lock_file.close()

    @staticmethod
    def _atomic_write_json(
        path: Path,
        payload: dict[str, Any],
        *,
        expected_bytes: Optional[bytes] = None,
        require_missing: bool = False,
        contract: Optional[_PathContract] = None,
        pre_publish: Optional[Callable[[Path], None]] = None,
    ) -> None:
        """在 metadata 写锁内落盘、CAS 并原子替换 JSON 文件。"""
        with VectorStore._metadata_write_lock(path, contract=contract):
            def write_temp(temp_path: Path) -> None:
                with _contract_open(
                    contract,
                    temp_path,
                    "w",
                    encoding="utf-8",
                    label="向量元数据临时文件",
                ) as temp_file:
                    json.dump(payload, temp_file, indent=2)

            def pre_replace() -> None:
                if expected_bytes is not None:
                    if not os.path.lexists(path):
                        current_bytes: Optional[bytes] = None
                    else:
                        current_bytes = _read_contract_bytes(
                            path,
                            contract,
                            label="向量元数据文件",
                        )
                    if current_bytes != expected_bytes:
                        raise RuntimeError(
                            f"{path.name} 在写入期间发生并发修改，请重试"
                        )
                elif require_missing and os.path.lexists(path):
                    raise RuntimeError(f"{path.name} 已被并发创建，请重试")
                if pre_publish is not None:
                    pre_publish(temp_path_holder[0])

            temp_path_holder: list[Path] = []

            def tracked_write_temp(temp_path: Path) -> None:
                temp_path_holder.append(temp_path)
                write_temp(temp_path)

            _contract_publish(
                contract,
                path,
                label="向量元数据文件",
                writer=tracked_write_temp,
                pre_replace=pre_replace,
            )

    def _migrate_legacy_embedding_fingerprints(self) -> None:
        """在任何校验前，以文件级原子操作升级 doc/chunk 元数据。"""
        future_schema_names: list[str] = []
        unsupported_format_names: list[str] = []
        for name in ("doc_vectors", "chunk_vectors"):
            metadata_path = self.index_dir / f"{name}_metadata.json"
            try:
                with self._index_pair_lock(name):
                    self._recover_pair_transaction_locked(name)
                    if not os.path.lexists(metadata_path):
                        continue
                    metadata, _ = self._read_json_snapshot(
                        metadata_path,
                        validate_schema=False,
                        contract=self._contract,
                    )
                    self._validate_supported_metadata(metadata)
            except _FutureMetadataSchemaError:
                future_schema_names.append(name)
            except _UnsupportedMetadataFormatError:
                unsupported_format_names.append(name)
            except Exception:
                # 普通读取错误仍交给逐文件迁移处理，避免阻断另一份安全清理。
                continue

        if future_schema_names:
            raise RuntimeError(
                "检测到高于当前版本的 metadata/fingerprint schema，"
                f"拒绝改写: {', '.join(future_schema_names)}"
            ) from None
        if unsupported_format_names:
            raise RuntimeError(
                "检测到畸形或冲突的 metadata/fingerprint，"
                f"拒绝改写: {', '.join(unsupported_format_names)}"
            ) from None

        failed_names: list[str] = []
        for name in ("doc_vectors", "chunk_vectors"):
            metadata_path = self.index_dir / f"{name}_metadata.json"
            try:
                if not os.path.lexists(metadata_path):
                    continue
                with self._index_pair_lock(name):
                    self._recover_pair_transaction_locked(name)
                    self._migrate_embedding_metadata_file(name, metadata_path)
            except Exception:
                # 必须继续处理另一份文件；异常内容可能包含旧 endpoint，不能回显。
                failed_names.append(name)

        if failed_names:
            names = ", ".join(failed_names)
            raise RuntimeError(
                f"Embedding 元数据安全迁移失败: {names}；"
                "原文件保持不变，可在修复文件系统问题后重试"
            ) from None

    def _migrate_embedding_metadata_file(self, name: str, path: Path) -> None:
        """将单份 metadata 升级到版本化、安全且可幂等重试的格式。"""
        metadata, original_bytes = self._read_json_snapshot(
            path,
            contract=self._contract,
        )
        fingerprint_v2 = metadata.get(self.EMBEDDING_FINGERPRINT_V2_KEY)
        legacy_fingerprint = metadata.get(self.LEGACY_EMBEDDING_FINGERPRINT_KEY)
        source = (
            fingerprint_v2
            if isinstance(fingerprint_v2, dict)
            else legacy_fingerprint
            if isinstance(legacy_fingerprint, dict)
            else None
        )

        # 无任何可迁移指纹时保留真正 legacy schema；现代 schema 会在校验时严格报错。
        if source is None:
            return

        migrated_metadata = dict(metadata)
        normalized = self._normalize_stored_embedding_fingerprint(source)
        migrated_metadata[self.EMBEDDING_FINGERPRINT_V2_KEY] = (
            self._persisted_embedding_fingerprint(normalized)
        )
        migrated_metadata["schema_version"] = self.METADATA_SCHEMA_VERSION

        legacy_compatibility: Optional[dict[str, Any]] = None
        is_hash_in_v1 = isinstance(legacy_fingerprint, dict) and (
            "base_url_sha256" in legacy_fingerprint
        )
        if is_hash_in_v1:
            # 过渡格式必须保留到 current contract 校验成功；若 mismatch，
            # 原有 rollback/诊断状态不能被预迁移隐藏。
            legacy_compatibility = dict(legacy_fingerprint)
            legacy_base_url = legacy_compatibility.get("base_url")
            if legacy_base_url is not None and url_contains_credentials(
                str(legacy_base_url)
            ):
                legacy_compatibility.pop("base_url", None)
        elif isinstance(legacy_fingerprint, dict) and "base_url" in legacy_fingerprint:
            legacy_compatibility = self._legacy_fingerprint(
                self._normalize_stored_embedding_fingerprint(legacy_fingerprint),
                legacy_fingerprint.get("base_url"),
            )
        elif (
            not isinstance(legacy_fingerprint, dict)
            and isinstance(fingerprint_v2, dict)
            and "base_url" in fingerprint_v2
        ):
            legacy_compatibility = self._legacy_fingerprint(
                normalized,
                fingerprint_v2.get("base_url"),
            )

        if legacy_compatibility is None:
            migrated_metadata.pop(self.LEGACY_EMBEDDING_FINGERPRINT_KEY, None)
        else:
            migrated_metadata[self.LEGACY_EMBEDDING_FINGERPRINT_KEY] = (
                legacy_compatibility
            )

        if migrated_metadata == metadata:
            return

        raw_endpoints = [
            fingerprint.get("base_url")
            for fingerprint in (fingerprint_v2, legacy_fingerprint)
            if isinstance(fingerprint, dict) and "base_url" in fingerprint
        ]
        removed_credential_endpoint = any(
            endpoint is not None and url_contains_credentials(str(endpoint))
            for endpoint in raw_endpoints
        )
        self._atomic_write_json(
            path,
            migrated_metadata,
            expected_bytes=original_bytes,
            contract=self._contract,
        )
        if removed_credential_endpoint:
            logger.warning(
                "%s 的旧 Embedding endpoint 含凭据；已按安全优先移除 "
                "legacy fingerprint，旧版 reader 将降级为缺失指纹兼容加载",
                name,
            )
        else:
            logger.info("%s 已迁移为版本化 Embedding 元数据", name)

    def _remove_legacy_fingerprints_for_credential_endpoint(self) -> None:
        """当前 endpoint 含凭据时，在任何契约校验前移除两份旧 reader 键。"""
        if self._legacy_embedding_fingerprint is not None:
            return

        failed_names: list[str] = []
        removed_names: list[str] = []
        for name in ("doc_vectors", "chunk_vectors"):
            metadata_path = self.index_dir / f"{name}_metadata.json"
            try:
                if not os.path.lexists(metadata_path):
                    continue
                with self._index_pair_lock(name):
                    self._recover_pair_transaction_locked(name)
                    metadata, original_bytes = self._read_json_snapshot(
                        metadata_path,
                        contract=self._contract,
                    )
                    if self.LEGACY_EMBEDDING_FINGERPRINT_KEY not in metadata:
                        continue
                    legacy_fingerprint = metadata[
                        self.LEGACY_EMBEDDING_FINGERPRINT_KEY
                    ]
                    if "base_url_sha256" in legacy_fingerprint:
                        # hash-in-v1 只有在现代契约验证成功后才能清理。
                        continue
                    migrated_metadata = dict(metadata)
                    migrated_metadata.pop(
                        self.LEGACY_EMBEDDING_FINGERPRINT_KEY, None
                    )
                    self._atomic_write_json(
                        metadata_path,
                        migrated_metadata,
                        expected_bytes=original_bytes,
                        contract=self._contract,
                    )
                    removed_names.append(name)
            except Exception:
                failed_names.append(name)

        if removed_names:
            logger.warning(
                "%s 已按安全优先移除 legacy fingerprint；"
                "旧版 reader 将降级为缺失指纹兼容加载",
                ", ".join(removed_names),
            )
        if failed_names:
            raise RuntimeError(
                "含凭据 endpoint 的 legacy fingerprint 安全清理失败: "
                f"{', '.join(failed_names)}"
            ) from None

    @contextmanager
    def _index_pair_lock(self, name: str):
        """串行化同一 index/metadata 配对的检查、变更与保存。"""
        index_path = self.index_dir / f"{name}.idx"
        with self._metadata_write_lock(index_path, contract=self._contract):
            yield

    @classmethod
    def _validate_pair_name(cls, name: str) -> None:
        if name not in cls.PAIR_NAMES:
            raise ValueError(f"未知向量索引配对: {name}")

    def _pair_paths(self, name: str) -> dict[str, Path]:
        self._validate_pair_name(name)
        return {
            "index": self.index_dir / f"{name}.idx",
            "metadata": self.index_dir / f"{name}_metadata.json",
        }

    def _pair_transaction_path(self, name: str) -> Path:
        self._validate_pair_name(name)
        return self.index_dir / f".{name}.pair-transaction.json"

    def _fsync_index_directory(self) -> None:
        """持久化目录项顺序；Windows 不支持目录 fsync，进程崩溃不受影响。"""
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(self.index_dir, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _snapshot_file(self, path: Path, *, label: str) -> dict[str, Any]:
        """在统一路径合同下取得稳定 identity/size/content 快照。"""
        digest = hashlib.sha256()
        byte_count = 0
        with self._open_leaf(path, "rb", label=label) as source:
            before = os.fstat(source.fileno())
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                byte_count += len(chunk)
            after = os.fstat(source.fileno())
        published = os.lstat(path)
        before_state = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        after_state = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        path_state = (
            published.st_dev,
            published.st_ino,
            published.st_size,
            published.st_mtime_ns,
        )
        if before_state != after_state or after_state != path_state:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"{label}在读取期间发生变化: {path}",
            )
        if byte_count != before.st_size:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"{label}读取长度与文件状态不一致: {path}",
            )
        return {
            "identity": [int(before.st_dev), int(before.st_ino)],
            "size": int(before.st_size),
            "sha256": digest.hexdigest(),
        }

    def _read_small_file_snapshot(
        self,
        path: Path,
        *,
        label: str,
        max_bytes: int,
    ) -> tuple[bytes, dict[str, Any]]:
        """读取小型事务标记，并绑定读取字节与路径身份。"""
        with self._open_leaf(path, "rb", label=label) as source:
            before = os.fstat(source.fileno())
            if before.st_size > max_bytes:
                raise RuntimeError(f"{label}超过允许大小，拒绝自动恢复")
            content = source.read(max_bytes + 1)
            after = os.fstat(source.fileno())
        if len(content) > max_bytes:
            raise RuntimeError(f"{label}超过允许大小，拒绝自动恢复")
        published = os.lstat(path)
        before_state = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        after_state = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        path_state = (
            published.st_dev,
            published.st_ino,
            published.st_size,
            published.st_mtime_ns,
        )
        if before_state != after_state or after_state != path_state:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"{label}在读取期间发生变化: {path}",
            )
        return content, {
            "identity": [int(before.st_dev), int(before.st_ino)],
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    @staticmethod
    def _snapshot_matches(
        snapshot: Optional[dict[str, Any]],
        expected: Optional[dict[str, Any]],
    ) -> bool:
        if snapshot is None or expected is None:
            return False
        return all(
            snapshot.get(key) == expected.get(key)
            for key in ("identity", "size", "sha256")
        )

    def _unlink_exact_snapshot(
        self,
        path: Path,
        expected: dict[str, Any],
        *,
        label: str,
    ) -> None:
        """仅删除身份和内容均与事务记录一致的辅助文件。"""
        if not os.path.lexists(path):
            return
        current = self._snapshot_file(path, label=label)
        if not self._snapshot_matches(current, expected):
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"{label}身份或内容已变化，拒绝删除: {path}",
            )
        path.unlink()
        if os.path.lexists(path):
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"{label}删除结果不确定: {path}",
            )

    @classmethod
    def _validate_snapshot_record(
        cls,
        record: Any,
        *,
        expected_target: Optional[str] = None,
        auxiliary: bool = False,
    ) -> None:
        if not isinstance(record, dict):
            raise RuntimeError("向量配对事务快照必须是 JSON object")
        identity = record.get("identity")
        if (
            not isinstance(identity, list)
            or len(identity) != 2
            or any(type(value) is not int or value < 0 for value in identity)
        ):
            raise RuntimeError("向量配对事务快照 identity 无效")
        size = record.get("size")
        if type(size) is not int or size < 0:
            raise RuntimeError("向量配对事务快照 size 无效")
        digest = record.get("sha256")
        if not isinstance(digest, str) or not cls._is_sha256(digest):
            raise RuntimeError("向量配对事务快照 sha256 无效")
        name_key = "file_name" if auxiliary else "target_name"
        file_name = record.get(name_key)
        if not isinstance(file_name, str) or Path(file_name).name != file_name:
            raise RuntimeError("向量配对事务快照文件名无效")
        if expected_target is not None and file_name != expected_target:
            raise RuntimeError("向量配对事务快照目标不匹配")
        staged_name = record.get("staged_file_name")
        if staged_name is not None and (
            not isinstance(staged_name, str)
            or Path(staged_name).name != staged_name
            or not staged_name.startswith(f".{file_name}.")
            or not staged_name.endswith(".tmp")
        ):
            raise RuntimeError("向量配对事务 staged 文件名无效")

    @classmethod
    def _validate_pair_transaction_payload(
        cls,
        payload: Any,
        *,
        expected_name: str,
    ) -> None:
        if not isinstance(payload, dict):
            raise RuntimeError("向量配对事务标记必须是 JSON object")
        schema_version = payload.get("schema_version")
        if (
            type(schema_version) is not int
            or schema_version != cls.PAIR_TRANSACTION_SCHEMA_VERSION
        ):
            raise RuntimeError("向量配对事务标记 schema 不受支持")
        if payload.get("name") != expected_name or expected_name not in cls.PAIR_NAMES:
            raise RuntimeError("向量配对事务标记名称无效")
        operation_id = payload.get("operation_id")
        if (
            not isinstance(operation_id, str)
            or len(operation_id) != 32
            or any(char not in "0123456789abcdef" for char in operation_id)
        ):
            raise RuntimeError("向量配对事务 operation_id 无效")
        if payload.get("mode") not in {"create", "update"}:
            raise RuntimeError("向量配对事务 mode 无效")

        expected_targets = {
            "index": f"{expected_name}.idx",
            "metadata": f"{expected_name}_metadata.json",
        }
        originals = payload.get("originals")
        if not isinstance(originals, dict) or set(originals) != set(expected_targets):
            raise RuntimeError("向量配对事务 originals 无效")
        for kind, target_name in expected_targets.items():
            original = originals[kind]
            if not isinstance(original, dict) or type(original.get("exists")) is not bool:
                raise RuntimeError("向量配对事务 original 状态无效")
            if original.get("target_name") != target_name:
                raise RuntimeError("向量配对事务 original 目标无效")
            if original["exists"]:
                cls._validate_snapshot_record(
                    original,
                    expected_target=target_name,
                )
                rollback = original.get("rollback")
                cls._validate_snapshot_record(rollback, auxiliary=True)
                rollback_name = rollback["file_name"]
                if (
                    not rollback_name.startswith(f".{target_name}.")
                    or not rollback_name.endswith(".rollback")
                ):
                    raise RuntimeError("向量配对事务 rollback 文件名无效")
                if rollback["sha256"] != original["sha256"]:
                    raise RuntimeError("向量配对事务 rollback 内容摘要不匹配")
            elif any(
                key in original
                for key in ("identity", "size", "sha256", "rollback")
            ):
                raise RuntimeError("不存在的 original 不得携带文件快照")

        if payload["mode"] == "create" and any(
            originals[kind]["exists"] for kind in expected_targets
        ):
            raise RuntimeError("create 配对事务不得声明既有 artifact")
        if payload["mode"] == "update" and not all(
            originals[kind]["exists"] for kind in expected_targets
        ):
            raise RuntimeError("update 配对事务必须包含完整既有配对")

        for collection_name in ("outputs", "recovery_outputs"):
            collection = payload.get(collection_name, {})
            if not isinstance(collection, dict) or not set(collection).issubset(
                expected_targets
            ):
                raise RuntimeError(f"向量配对事务 {collection_name} 无效")
            for kind, record in collection.items():
                cls._validate_snapshot_record(
                    record,
                    expected_target=expected_targets[kind],
                )

    def _persist_pair_transaction(
        self,
        transaction: _PairTransaction,
        *,
        require_missing: bool = False,
    ) -> None:
        marker_path = self._pair_transaction_path(transaction.name)
        marker_bytes = (
            json.dumps(
                transaction.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

        def pre_replace() -> None:
            if require_missing:
                if os.path.lexists(marker_path):
                    raise RuntimeError(
                        f"{transaction.name} 已存在未完成配对事务，拒绝覆盖"
                    )
                return
            if transaction.marker_identity is None or transaction.marker_sha256 is None:
                raise RuntimeError("向量配对事务缺少上一版标记身份")
            if not os.path.lexists(marker_path):
                raise RuntimeError("向量配对事务标记在更新前消失")
            current = self._snapshot_file(marker_path, label="向量配对事务标记")
            expected = {
                "identity": list(transaction.marker_identity),
                "size": current["size"],
                "sha256": transaction.marker_sha256,
            }
            if current["identity"] != expected["identity"] or current[
                "sha256"
            ] != expected["sha256"]:
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    "向量配对事务标记在更新期间被替换",
                )

        _contract_publish(
            self._contract,
            marker_path,
            label="向量配对事务标记",
            data=marker_bytes,
            pre_replace=pre_replace,
        )
        marker_snapshot = self._snapshot_file(
            marker_path,
            label="向量配对事务标记",
        )
        if marker_snapshot["sha256"] != hashlib.sha256(marker_bytes).hexdigest():
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                "向量配对事务标记发布后内容不一致",
            )
        transaction.marker_identity = tuple(marker_snapshot["identity"])
        transaction.marker_sha256 = marker_snapshot["sha256"]
        self._fsync_index_directory()

    def _begin_pair_transaction(
        self,
        name: str,
        *,
        allow_missing: bool,
    ) -> _PairTransaction:
        if name in self._active_pair_transactions:
            raise RuntimeError(f"{name} 已存在当前进程内配对事务")
        marker_path = self._pair_transaction_path(name)
        if os.path.lexists(marker_path):
            raise RuntimeError(f"{name} 存在未恢复配对事务，拒绝开始新写入")

        paths = self._pair_paths(name)
        existence = {kind: os.path.lexists(path) for kind, path in paths.items()}
        if len(set(existence.values())) != 1:
            raise RuntimeError(f"{name} 当前配对不完整，拒绝开始事务")
        pair_exists = all(existence.values())
        if not pair_exists and not allow_missing:
            raise RuntimeError(f"{name} 当前配对缺失，拒绝开始更新事务")

        originals: dict[str, dict[str, Any]] = {}
        rollback_snapshots: list[tuple[Path, dict[str, Any]]] = []
        try:
            for kind, target_path in paths.items():
                original: dict[str, Any] = {
                    "target_name": target_path.name,
                    "exists": pair_exists,
                }
                if pair_exists:
                    before = self._snapshot_file(
                        target_path,
                        label="向量索引文件",
                    )
                    rollback_path, rollback_identity = self._create_rollback_copy(
                        target_path
                    )
                    after = self._snapshot_file(
                        target_path,
                        label="向量索引文件",
                    )
                    rollback = self._snapshot_file(
                        rollback_path,
                        label="向量 rollback 文件",
                    )
                    if not self._snapshot_matches(before, after):
                        raise PKVRuntimeError(
                            ErrorCode.PATH_STATE_UNDETERMINED,
                            f"{target_path.name} 在 rollback 准备期间发生变化",
                        )
                    if rollback["sha256"] != before["sha256"] or rollback[
                        "size"
                    ] != before["size"]:
                        raise PKVRuntimeError(
                            ErrorCode.PATH_STATE_UNDETERMINED,
                            f"{target_path.name} rollback 副本内容不一致",
                        )
                    if tuple(rollback["identity"]) != rollback_identity:
                        raise PKVRuntimeError(
                            ErrorCode.PATH_STATE_UNDETERMINED,
                            f"{target_path.name} rollback 副本身份不一致",
                        )
                    original.update(before)
                    original["rollback"] = {
                        "file_name": rollback_path.name,
                        **rollback,
                    }
                    rollback_snapshots.append((rollback_path, rollback))
                originals[kind] = original

            transaction = _PairTransaction(
                name=name,
                payload={
                    "schema_version": self.PAIR_TRANSACTION_SCHEMA_VERSION,
                    "operation_id": uuid.uuid4().hex,
                    "name": name,
                    "mode": "update" if pair_exists else "create",
                    "originals": originals,
                    "outputs": {},
                    "recovery_outputs": {},
                },
            )
            self._persist_pair_transaction(transaction, require_missing=True)
        except BaseException:
            # Once the marker may have been published, its rollback references
            # are durable recovery authority.  A post-publish snapshot/fsync
            # failure must not turn that marker into a dangling record by
            # deleting the referenced copies.  Clean only when absence is
            # positively proven; any other state is fail-closed recovery debt.
            marker_definitely_absent = False
            try:
                os.lstat(marker_path)
            except FileNotFoundError:
                marker_definitely_absent = True
            except OSError:
                pass
            if marker_definitely_absent:
                for rollback_path, snapshot in rollback_snapshots:
                    try:
                        self._unlink_exact_snapshot(
                            rollback_path,
                            snapshot,
                            label="向量 rollback 文件",
                        )
                    except BaseException:
                        logger.error(
                            "配对事务准备失败后无法安全清理 rollback: component=%s",
                            name,
                        )
            else:
                logger.error(
                    "%s 配对事务 marker 已存在或状态不确定；保留全部 rollback 供启动恢复",
                    name,
                )
            raise

        self._active_pair_transactions[name] = transaction
        return transaction

    def _prepare_pair_output(self, name: str, kind: str, temp_path: Path) -> None:
        """在目标 replace 前持久记录将发布临时文件的身份和摘要。"""
        transaction = self._active_pair_transactions.get(name)
        if transaction is None:
            return
        paths = self._pair_paths(name)
        if kind not in paths:
            raise ValueError(f"未知配对 artifact: {kind}")
        snapshot = self._snapshot_file(temp_path, label="向量配对发布临时文件")
        transaction.payload["outputs"][kind] = {
            "target_name": paths[kind].name,
            "staged_file_name": temp_path.name,
            **snapshot,
        }
        self._persist_pair_transaction(transaction)

    def _read_pair_transaction_locked(self, name: str) -> _PairTransaction:
        marker_path = self._pair_transaction_path(name)
        content, marker_snapshot = self._read_small_file_snapshot(
            marker_path,
            label="向量配对事务标记",
            max_bytes=64 * 1024,
        )
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"{name} 配对事务标记无法解析，拒绝自动恢复"
            ) from error
        self._validate_pair_transaction_payload(payload, expected_name=name)
        return _PairTransaction(
            name=name,
            payload=payload,
            marker_identity=tuple(marker_snapshot["identity"]),
            marker_sha256=marker_snapshot["sha256"],
        )

    @staticmethod
    def _matches_any_snapshot(
        current: Optional[dict[str, Any]],
        candidates: List[Optional[dict[str, Any]]],
    ) -> bool:
        return any(
            VectorStore._snapshot_matches(current, candidate)
            for candidate in candidates
            if candidate is not None
        )

    def _target_snapshot_or_none(self, path: Path) -> Optional[dict[str, Any]]:
        if not os.path.lexists(path):
            return None
        return self._snapshot_file(path, label="向量索引文件")

    def _prepare_recovery_output(
        self,
        transaction: _PairTransaction,
        kind: str,
        temp_path: Path,
    ) -> None:
        paths = self._pair_paths(transaction.name)
        current = self._target_snapshot_or_none(paths[kind])
        original = transaction.payload["originals"][kind]
        candidates = [
            original if original["exists"] else None,
            transaction.payload.get("outputs", {}).get(kind),
            transaction.payload.get("recovery_outputs", {}).get(kind),
        ]
        if (
            current is not None
            and not self._matches_any_snapshot(current, candidates)
        ):
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"{paths[kind].name} 不属于已记录事务，拒绝覆盖",
            )
        snapshot = self._snapshot_file(temp_path, label="向量恢复临时文件")
        transaction.payload.setdefault("recovery_outputs", {})[kind] = {
            "target_name": paths[kind].name,
            "staged_file_name": temp_path.name,
            **snapshot,
        }
        self._persist_pair_transaction(transaction)

    def _publish_rollback_restore(
        self,
        transaction: _PairTransaction,
        kind: str,
        rollback_path: Path,
    ) -> None:
        target_path = self._pair_paths(transaction.name)[kind]
        restore_temp_path: list[Optional[Path]] = [None]

        def copy_rollback(temp_path: Path) -> None:
            restore_temp_path[0] = temp_path
            with self._open_leaf(
                rollback_path,
                "rb",
                label="向量 rollback 文件",
            ) as source, self._open_leaf(
                temp_path,
                "wb",
                label="向量恢复临时文件",
            ) as destination:
                shutil.copyfileobj(source, destination)

        def prepare_restore() -> None:
            temp_path = restore_temp_path[0]
            if temp_path is None:
                raise RuntimeError("向量恢复临时文件尚未准备")
            self._prepare_recovery_output(
                transaction,
                kind,
                temp_path,
            )

        _contract_publish(
            self._contract,
            target_path,
            label="向量索引文件",
            writer=copy_rollback,
            pre_replace=prepare_restore,
        )

    def _clear_pair_transaction(self, transaction: _PairTransaction) -> None:
        """先持久移除恢复意图，再按精确身份清理 rollback/staged 辅助文件。"""
        marker_path = self._pair_transaction_path(transaction.name)
        if transaction.marker_identity is None or transaction.marker_sha256 is None:
            raise RuntimeError("向量配对事务缺少标记身份，拒绝清理")
        marker_content, marker_snapshot = self._read_small_file_snapshot(
            marker_path,
            label="向量配对事务标记",
            max_bytes=64 * 1024,
        )
        del marker_content
        if marker_snapshot["identity"] != list(transaction.marker_identity) or (
            marker_snapshot["sha256"] != transaction.marker_sha256
        ):
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                "向量配对事务标记在清理前被替换",
            )
        marker_path.unlink()
        if os.path.lexists(marker_path):
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                "向量配对事务标记删除结果不确定",
            )
        self._fsync_index_directory()

        auxiliary_records: list[tuple[Path, dict[str, Any], str]] = []
        for original in transaction.payload["originals"].values():
            rollback = original.get("rollback")
            if isinstance(rollback, dict):
                auxiliary_records.append(
                    (
                        self.index_dir / rollback["file_name"],
                        rollback,
                        "向量 rollback 文件",
                    )
                )
        for collection_name in ("outputs", "recovery_outputs"):
            for record in transaction.payload.get(collection_name, {}).values():
                staged_name = record.get("staged_file_name")
                if staged_name:
                    auxiliary_records.append(
                        (
                            self.index_dir / staged_name,
                            record,
                            "向量配对残留临时文件",
                        )
                    )
        for auxiliary_path, snapshot, label in auxiliary_records:
            if not os.path.lexists(auxiliary_path):
                continue
            try:
                self._unlink_exact_snapshot(
                    auxiliary_path,
                    snapshot,
                    label=label,
                )
            except BaseException:
                # 标记已删除，辅助副本不会再影响正式配对；不触碰未知替换物。
                logger.warning(
                    "拒绝清理身份不确定的向量辅助文件: component=%s",
                    transaction.name,
                )
        self._fsync_index_directory()

    def _restore_pair_transaction_locked(
        self,
        transaction: _PairTransaction,
    ) -> None:
        """回滚 prepared 配对事务；严格恢复只接受标记中登记过的身份。"""
        paths = self._pair_paths(transaction.name)
        originals = transaction.payload["originals"]
        outputs = transaction.payload.get("outputs", {})
        recovery_outputs = transaction.payload.get("recovery_outputs", {})
        current_by_kind: dict[str, Optional[dict[str, Any]]] = {}

        # 先验证两份目标及所有 rollback，任何不确定状态都必须在首个写动作前失败。
        for kind, target_path in paths.items():
            current = self._target_snapshot_or_none(target_path)
            current_by_kind[kind] = current
            original = originals[kind]
            allowed = [
                original if original["exists"] else None,
                outputs.get(kind),
                recovery_outputs.get(kind),
            ]
            if (
                current is not None
                and not self._matches_any_snapshot(current, allowed)
            ):
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"{target_path.name} 身份或内容不属于未完成事务；"
                    "已保留标记与副本，拒绝自动恢复",
                )
            rollback = original.get("rollback")
            if rollback is not None:
                rollback_path = self.index_dir / rollback["file_name"]
                rollback_current = self._snapshot_file(
                    rollback_path,
                    label="向量 rollback 文件",
                )
                if not self._snapshot_matches(rollback_current, rollback):
                    raise PKVRuntimeError(
                        ErrorCode.PATH_STATE_UNDETERMINED,
                        f"{target_path.name} rollback 身份或内容不一致",
                    )

        for kind, target_path in paths.items():
            original = originals[kind]
            current = current_by_kind[kind]
            if original["exists"]:
                already_restored = self._snapshot_matches(current, original) or (
                    self._snapshot_matches(current, recovery_outputs.get(kind))
                    and recovery_outputs.get(kind, {}).get("sha256")
                    == original["sha256"]
                )
                if not already_restored:
                    rollback_path = self.index_dir / original["rollback"][
                        "file_name"
                    ]
                    self._publish_rollback_restore(
                        transaction,
                        kind,
                        rollback_path,
                    )
                    restored = self._target_snapshot_or_none(target_path)
                    recovery_record = transaction.payload["recovery_outputs"][kind]
                    if (
                        not self._snapshot_matches(restored, recovery_record)
                        or restored["sha256"] != original["sha256"]
                    ):
                        raise PKVRuntimeError(
                            ErrorCode.PATH_STATE_UNDETERMINED,
                            f"{target_path.name} 回滚发布后校验失败",
                        )
            elif current is not None:
                # create 中断时只能删除明确登记为本事务输出的 artifact。
                output = outputs.get(kind)
                if not self._snapshot_matches(current, output):
                    raise PKVRuntimeError(
                        ErrorCode.PATH_STATE_UNDETERMINED,
                        f"{target_path.name} 不是已登记的创建输出，拒绝删除",
                    )
                self._unlink_exact_snapshot(
                    target_path,
                    output,
                    label="向量中断创建 artifact",
                )

        self._clear_pair_transaction(transaction)

    def _complete_pair_transaction(self, transaction: _PairTransaction) -> None:
        paths = self._pair_paths(transaction.name)
        originals = transaction.payload["originals"]
        outputs = transaction.payload.get("outputs", {})
        for kind, target_path in paths.items():
            current = self._target_snapshot_or_none(target_path)
            expected = outputs.get(kind)
            if expected is None and originals[kind]["exists"]:
                expected = originals[kind]
            if expected is None or not self._snapshot_matches(current, expected):
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"{target_path.name} 配对提交结果与事务记录不一致",
                )
        self._clear_pair_transaction(transaction)

    def _recover_pair_transaction_locked(self, name: str) -> bool:
        marker_path = self._pair_transaction_path(name)
        if not os.path.lexists(marker_path):
            return False
        transaction = self._read_pair_transaction_locked(name)
        self._restore_pair_transaction_locked(transaction)
        logger.warning("已确定性回滚未完成向量配对事务: %s", name)
        return True

    def _recover_incomplete_pair_transactions(self) -> None:
        for name in self.PAIR_NAMES:
            marker_path = self._pair_transaction_path(name)
            if not os.path.lexists(marker_path):
                continue
            with self._index_pair_lock(name):
                self._recover_pair_transaction_locked(name)

    def _create_rollback_copy(self, source: Path) -> tuple[Path, tuple[int, int]]:
        """在同目录创建并 fsync 一份精确的 rollback 副本（先过链接安全合同）。"""
        source = self._validate_leaf(
            source,
            label="向量索引文件",
            allow_missing=False,
        )
        parent = source.parent
        _contract_validate_dir(self._contract, parent, label="向量索引目录")
        parent_info = os.lstat(parent)
        parent_identity = (parent_info.st_dev, parent_info.st_ino)
        descriptor, raw_path = tempfile.mkstemp(
            dir=parent,
            prefix=f".{source.name}.",
            suffix=".rollback",
        )
        rollback_path = Path(raw_path)
        initial_rollback_info = os.fstat(descriptor)
        rollback_identity = (
            initial_rollback_info.st_dev,
            initial_rollback_info.st_ino,
        )
        descriptor_open = True

        def cleanup_owned_rollback() -> None:
            if not os.path.lexists(rollback_path):
                return
            try:
                validated = self._validate_leaf(
                    rollback_path,
                    label="向量 rollback 文件",
                    allow_missing=False,
                )
                info = os.lstat(validated)
                if (info.st_dev, info.st_ino) == rollback_identity:
                    validated.unlink()
            except (OSError, PKVRuntimeError):
                # 路径已被替换时保留未知文件，绝不按名称盲删。
                pass
        try:
            verify_fd_matches_path(
                descriptor,
                rollback_path,
                label="向量 rollback 文件",
            )
            rollback_info = os.fstat(descriptor)
            rollback_identity = (rollback_info.st_dev, rollback_info.st_ino)
            parent_after = os.lstat(parent)
            if (parent_after.st_dev, parent_after.st_ino) != parent_identity:
                raise PKVRuntimeError(
                    ErrorCode.DATA_ROOT_UNSAFE,
                    f"向量索引目录在回滚副本创建期间被替换: {parent}",
                )
            destination_handle = os.fdopen(descriptor, "wb")
            descriptor_open = False
            with destination_handle as destination, self._open_leaf(
                source,
                "rb",
                label="向量索引文件",
            ) as source_file:
                shutil.copyfileobj(source_file, destination)
                destination.flush()
                os.fsync(destination.fileno())
                verify_fd_matches_path(
                    destination.fileno(),
                    rollback_path,
                    label="向量 rollback 文件",
                )
        except BaseException:
            if descriptor_open:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            cleanup_owned_rollback()
            raise
        try:
            published = self._validate_leaf(
                rollback_path,
                label="向量 rollback 文件",
                allow_missing=False,
            )
            published_info = os.lstat(published)
            if (published_info.st_dev, published_info.st_ino) != rollback_identity:
                raise PKVRuntimeError(
                    ErrorCode.PATH_STATE_UNDETERMINED,
                    f"向量 rollback 文件创建后身份已变化: {rollback_path}",
                )
        except BaseException:
            cleanup_owned_rollback()
            raise
        return rollback_path, rollback_identity

    @contextmanager
    def _pair_rollback_guard(
        self,
        name: str,
        *,
        allow_missing: bool = False,
        reload_on_error: bool = True,
    ):
        """以持久事务标记保护两次发布，异常与下次启动共用恢复协议。"""
        transaction = self._begin_pair_transaction(
            name,
            allow_missing=allow_missing,
        )
        try:
            yield
        except BaseException as original_error:
            try:
                # 正常发布在 replace 前已经把输出身份和摘要写入 marker。
                # 即使仍在同一异常展开中，也不能把未登记的普通文件替换
                # 假定为“本进程所有”，否则会覆盖外部并发写入。
                self._restore_pair_transaction_locked(transaction)
            except BaseException:
                raise RuntimeError(
                    f"index/metadata 回滚失败: {name}；"
                    "已保留事务标记与可验证 rollback 副本"
                ) from original_error
            if reload_on_error:
                try:
                    self._reload_index_for_update_locked(name)
                except BaseException as reload_error:
                    raise RuntimeError(
                        f"{name} 磁盘文件已回滚，但当前实例重载失败: {reload_error}"
                    ) from original_error
            raise
        else:
            self._complete_pair_transaction(transaction)
        finally:
            self._active_pair_transactions.pop(name, None)

    def _init_index(self, name: str) -> hnswlib.Index:
        """在配对锁内初始化或加载 hnswlib 索引。"""
        with self._index_pair_lock(name):
            return self._init_index_locked(name)

    def _init_index_locked(self, name: str) -> hnswlib.Index:
        """
        初始化或加载 hnswlib 索引

        Args:
            name: 索引名称 (doc_vectors 或 chunk_vectors)

        Returns:
            hnswlib.Index 对象
        """
        self._recover_pair_transaction_locked(name)
        index_path = self.index_dir / f"{name}.idx"
        metadata_path = self.index_dir / f"{name}_metadata.json"

        index_exists = os.path.lexists(index_path)
        metadata_exists = os.path.lexists(metadata_path)
        if index_exists != metadata_exists:
            raise RuntimeError(
                f"{name} 索引文件与元数据不一致，无法安全初始化: "
                f"index_exists={index_exists}, metadata_exists={metadata_exists}"
            )
        if not index_exists and not self._allow_index_creation:
            raise PKVRuntimeError(
                ErrorCode.RETRIEVAL_INDEX_UNAVAILABLE,
                f"{name} index/metadata pair 缺失，拒绝只读加载",
                stage="vector_index_pair_load",
                recoverable=True,
            )

        # 创建索引对象
        index = hnswlib.Index(space='cosine', dim=self.dim)

        if index_exists:
            metadata, metadata_bytes = self._read_json_snapshot(
                metadata_path,
                contract=self._contract,
            )
            existing_dim = metadata.get("dim")
            if existing_dim is None:
                raise RuntimeError(f"{name} 缺少 dim 元数据，无法安全加载索引")
            if int(existing_dim) != self.dim:
                raise RuntimeError(
                    "索引维度不匹配: "
                    f"name={name}, 已有={int(existing_dim)}, 当前请求={self.dim}。"
                    "当前初始化不会自动重建索引。"
                    "如果要继续使用现有索引，请切回原来的 Embedding 服务/模型/维度配置；"
                    "如果确认切换模型，请先重建向量索引。"
                )
            self._validate_embedding_fingerprint(
                name,
                metadata,
                expected_bytes=metadata_bytes,
            )
            # hnswlib 不能接收 fd：加载前拒绝链接/硬链接叶子，加载后再次核验。
            # 校验与 hnswlib 内部 open 之间的替换窗口无法消除（平台限制）。
            self._validate_leaf(
                index_path,
                label="向量索引文件",
                allow_missing=False,
            )
            index.load_index(
                str(index_path),
                allow_replace_deleted=True,
            )
            self._validate_leaf(
                index_path,
                label="向量索引文件",
                allow_missing=False,
            )
            self._validate_leaf(
                metadata_path,
                label="向量元数据文件",
                allow_missing=False,
            )
            # 修正容量：若 max_elements 不足则扩容到安全值
            if index.max_elements < index.element_count + 1000:
                safe_size = max(10000, index.element_count + 1000)
                index.resize_index(safe_size)
                logger.info(
                    f"🔄 索引容量不足，已扩容至 {safe_size}: {name}"
                )
            logger.info("✅ 加载已有索引: component=%s", name)
        else:
            # 初始化新索引
            index.init_index(
                max_elements=10000,  # 初始容量，由 _ensure_capacity 按需扩展
                ef_construction=self.ef_construction,
                M=self.M,
                allow_replace_deleted=True,
            )
            # 创建元数据文件
            metadata = {
                "schema_version": self.METADATA_SCHEMA_VERSION,
                "dim": self.dim,
                "space": "cosine",
                "M": self.M,
                "ef_construction": self.ef_construction,
                self.EMBEDDING_FINGERPRINT_V2_KEY: self.embedding_fingerprint,
                "id_mapping": {}
            }
            if self._legacy_embedding_fingerprint is not None:
                metadata[self.LEGACY_EMBEDDING_FINGERPRINT_KEY] = (
                    self._legacy_embedding_fingerprint
                )
            with self._pair_rollback_guard(
                name,
                allow_missing=True,
                reload_on_error=False,
            ):
                self._publish_index(name, index)
                self._atomic_write_json(
                    metadata_path,
                    metadata,
                    require_missing=True,
                    contract=self._contract,
                    pre_publish=lambda temp_path: self._prepare_pair_output(
                        name,
                        "metadata",
                        temp_path,
                    ),
                )

            logger.info("✅ 创建新索引: component=%s", name)

        # 设置查询时的搜索深度
        index.set_ef(self.ef_search)

        return index

    def _reload_index_for_update_locked(
        self,
        name: str,
        *,
        include_file_state: bool = False,
    ) -> (
        hnswlib.Index
        | tuple[
            hnswlib.Index,
            tuple[int, int, int, int, int],
            dict[str, Any],
            bytes,
        ]
    ):
        """在配对锁内从磁盘重载最新 index，并重新校验其 metadata。"""
        self._recover_pair_transaction_locked(name)
        index_path = self.index_dir / f"{name}.idx"
        metadata_path = self.index_dir / f"{name}_metadata.json"
        if not os.path.lexists(index_path) or not os.path.lexists(metadata_path):
            raise RuntimeError(f"{name} index/metadata 配对缺失，无法安全更新")

        metadata, metadata_bytes = self._read_json_snapshot(
            metadata_path,
            contract=self._contract,
        )
        existing_dim = metadata.get("dim")
        if existing_dim is None or int(existing_dim) != self.dim:
            raise RuntimeError(f"{name} 维度不匹配，无法安全更新")
        self._validate_embedding_fingerprint(
            name,
            metadata,
            expected_bytes=metadata_bytes,
        )

        validated_metadata = metadata
        validated_metadata_bytes = metadata_bytes
        if include_file_state:
            # 前一次校验可能完成合法 metadata 迁移；重新读取并完整校验最终快照，
            # 后续 mapping/cache 只能消费这份 bytes。
            try:
                validated_metadata, validated_metadata_bytes = (
                    self._load_metadata_snapshot(name)
                )
                validated_dim = validated_metadata.get("dim")
                if validated_dim is None or int(validated_dim) != self.dim:
                    raise RuntimeError(f"{name} 维度不匹配，无法安全读取")
                self._validate_embedding_fingerprint(
                    name,
                    validated_metadata,
                    expected_bytes=validated_metadata_bytes,
                )
            except PKVRuntimeError:
                raise
            except Exception as exc:
                raise PKVRuntimeError(
                    ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                    f"{name} metadata 契约不一致",
                    stage="chunk_index_metadata",
                    recoverable=True,
                ) from exc

        # hnswlib 不能接收 fd：加载前后以安全 fd 状态绑定实际读取的文件。
        before_state = self._index_file_state(index_path)
        index = hnswlib.Index(space="cosine", dim=self.dim)
        index.load_index(str(index_path), allow_replace_deleted=True)
        after_state = self._index_file_state(index_path)
        if before_state != after_state:
            raise PKVRuntimeError(
                ErrorCode.PATH_STATE_UNDETERMINED,
                f"{name} 索引在 hnswlib 加载期间发生变化",
                stage="vector_index_load",
                recoverable=True,
            )
        if include_file_state:
            try:
                _, after_metadata_bytes = self._load_metadata_snapshot(name)
            except PKVRuntimeError:
                raise
            except Exception as exc:
                raise PKVRuntimeError(
                    ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                    f"{name} metadata 状态不可判定",
                    stage="chunk_index_metadata",
                    recoverable=True,
                ) from exc
            if after_metadata_bytes != validated_metadata_bytes:
                raise PKVRuntimeError(
                    ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                    f"{name} metadata 在 index 加载期间发生变化",
                    stage="chunk_index_metadata",
                    recoverable=True,
                )
        if index.max_elements < index.element_count + 1000:
            index.resize_index(max(10000, index.element_count + 1000))
        index.set_ef(self.ef_search)
        if name == "doc_vectors":
            self.doc_index = index
        else:
            self.chunk_index = index
        if include_file_state:
            return (
                index,
                after_state,
                validated_metadata,
                validated_metadata_bytes,
            )
        return index

    def _index_file_state(
        self,
        index_path: Path,
    ) -> tuple[int, int, int, int, int]:
        """通过安全 fd 取得可缓存的 index 文件状态。"""

        with self._open_leaf(
            index_path,
            "rb",
            label="向量索引文件",
        ) as source:
            state = os.fstat(source.fileno())
        return (
            int(state.st_dev),
            int(state.st_ino),
            int(state.st_size),
            int(state.st_mtime_ns),
            int(state.st_ctime_ns),
        )

    def _ensure_capacity(self, index: "hnswlib.Index", count: int = 1) -> None:
        """确保索引有足够容量，不足时自动扩容（翻倍策略）

        Args:
            index: hnswlib 索引对象
            count: 本次需要添加的元素数量
        """
        if index.element_count + count > index.max_elements:
            new_size = max(
                index.max_elements * 2,
                index.element_count + count + 1000,
            )
            index.resize_index(new_size)
            logger.info(
                f"🔄 索引自动扩容: {index.max_elements // 2} → {new_size}"
            )

    @staticmethod
    def _invalid_vector_write_input() -> PKVRuntimeError:
        """Return the stable, value-free failure exposed by the write boundary."""

        return PKVRuntimeError(
            ErrorCode.STORAGE_VECTOR_FAILED,
            _INVALID_VECTOR_WRITE_INPUT,
            stage="vector_write_preflight",
            recoverable=False,
        )

    def _preflight_vector_write(
        self,
        vectors: np.ndarray,
        *,
        batch_size: Optional[int] = None,
    ) -> np.ndarray:
        """Freeze and validate exactly what hnswlib's float32 cosine path consumes.

        A fresh C-contiguous float32 snapshot closes the caller-mutation gap between
        validation and ``add_items``.  Every row is checked before a pair lock or
        transaction can be entered, so a bad batch is all-or-nothing.
        """

        expected_shape = (
            (int(self.dim),)
            if batch_size is None
            else (batch_size, int(self.dim))
        )
        return self._project_float32_cosine_input(
            vectors,
            expected_shape=expected_shape,
            invalid_error=self._invalid_vector_write_input,
        )

    @staticmethod
    def _invalid_vector_query_input() -> PKVRuntimeError:
        """Return the stable, value-free failure exposed by the query boundary."""

        return PKVRuntimeError(
            ErrorCode.RETRIEVAL_INVALID_QUERY,
            _INVALID_VECTOR_QUERY_INPUT,
            stage="vector_query_preflight",
            recoverable=False,
        )

    def _preflight_vector_query(self, query_vector: np.ndarray) -> np.ndarray:
        """Freeze one query in the exact float32 cosine domain before any read."""

        return self._project_float32_cosine_input(
            query_vector,
            expected_shape=(int(self.dim),),
            invalid_error=self._invalid_vector_query_input,
        )

    @staticmethod
    def _invalid_document_vector_read() -> PKVRuntimeError:
        """Return the stable failure for malformed persisted document vectors."""

        return PKVRuntimeError(
            ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
            _INVALID_DOCUMENT_VECTOR_READ,
            stage="document_vector_read",
            recoverable=True,
        )

    def _preflight_document_vector_read(self, vectors: np.ndarray) -> np.ndarray:
        """Validate one exact hnswlib get_items matrix and return an owned row."""

        projected = self._project_float32_cosine_input(
            vectors,
            expected_shape=(1, int(self.dim)),
            invalid_error=self._invalid_document_vector_read,
        )
        return projected[0].copy()

    def _project_float32_cosine_input(
        self,
        vectors: np.ndarray,
        *,
        expected_shape: tuple[int, ...],
        invalid_error: Callable[[], PKVRuntimeError],
    ) -> np.ndarray:
        """Project an owned, exact-shape snapshot into hnswlib's safe domain."""

        try:
            if type(vectors) is not np.ndarray:
                raise invalid_error()
            is_real_numeric = np.issubdtype(
                vectors.dtype,
                np.integer,
            ) or np.issubdtype(vectors.dtype, np.floating)
            if not is_real_numeric or vectors.shape != expected_shape:
                raise invalid_error()

            with np.errstate(over="ignore", under="ignore", invalid="ignore"):
                projected = vectors.astype(
                    np.float32,
                    order="C",
                    casting="unsafe",
                    subok=False,
                    copy=True,
                )
            if not bool(np.all(np.isfinite(projected))):
                raise invalid_error()

            matrix = (
                projected.reshape(1, int(self.dim))
                if projected.ndim == 1
                else projected
            )
            with np.errstate(over="ignore", under="ignore", invalid="ignore"):
                norm_squared = np.sum(
                    matrix * matrix,
                    axis=1,
                    dtype=np.float32,
                )
                norms = np.sqrt(norm_squared)
            if (
                not bool(np.all(np.isfinite(norm_squared)))
                or not bool(np.all(norm_squared > np.float32(0.0)))
                or not bool(np.all(np.isfinite(norms)))
                or not bool(np.all(norms > np.float32(0.0)))
            ):
                raise invalid_error()
            return projected
        except PKVRuntimeError:
            raise
        except Exception:
            raise invalid_error() from None

    def add_doc_vector(
        self,
        knowledge_id: int,
        vector: np.ndarray,
        replace_deleted: bool = False,
    ):
        """
        添加文档级向量

        Args:
            knowledge_id: 知识条目 ID (对应 knowledge_items.id)
            vector: 向量 (维度须与索引一致)
        """
        if type(knowledge_id) is not int or knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        vector = self._preflight_vector_write(vector)

        with self._index_pair_lock("doc_vectors"):
            index = self._reload_index_for_update_locked("doc_vectors")
            with self._pair_rollback_guard("doc_vectors"):
                self._ensure_capacity(index)
                index.add_items(
                    vector.reshape(1, -1),
                    ids=[knowledge_id],
                    replace_deleted=replace_deleted,
                )
                self._save_index("doc_vectors")

        logger.info(f"添加文档向量: knowledge_id={knowledge_id}")

    def add_chunk_vector(
        self,
        knowledge_id: int,
        chunk_index: int,
        vector: np.ndarray,
        replace_deleted: bool = False,
    ):
        """
        添加分块级向量

        Args:
            knowledge_id: 知识条目 ID
            chunk_index: 块序号
            vector: 向量 (维度须与索引一致)
        """
        hnswlib_id = self.encode_chunk_id(knowledge_id, chunk_index)
        vector = self._preflight_vector_write(vector)

        with self._index_pair_lock("chunk_vectors"):
            index = self._reload_index_for_update_locked("chunk_vectors")
            metadata, metadata_bytes = self._load_metadata_snapshot("chunk_vectors")
            with self._pair_rollback_guard("chunk_vectors"):
                self._ensure_capacity(index)
                index.add_items(
                    vector.reshape(1, -1),
                    ids=[hnswlib_id],
                    replace_deleted=replace_deleted,
                )
                metadata["id_mapping"][str(hnswlib_id)] = (
                    knowledge_id,
                    chunk_index,
                )
                metadata_path = self.index_dir / "chunk_vectors_metadata.json"
                self._atomic_write_json(
                    metadata_path,
                    metadata,
                    expected_bytes=metadata_bytes,
                    contract=self._contract,
                    pre_publish=lambda temp_path: self._prepare_pair_output(
                        "chunk_vectors", "metadata", temp_path
                    ),
                )
                self._save_index("chunk_vectors")

        logger.info(f"添加分块向量: knowledge_id={knowledge_id}, chunk_index={chunk_index}")

    def add_chunk_vectors(
        self,
        knowledge_id: int,
        chunk_indices: List[int],
        vectors: np.ndarray,
        replace_deleted: bool = False,
    ) -> int:
        """
        批量添加分块级向量。

        Args:
            knowledge_id: 知识条目 ID
            chunk_indices: 分块序号列表
            vectors: 向量矩阵 (shape=(num_chunks, dim))
            replace_deleted: 是否复用已标记删除的 label

        Returns:
            实际写入的向量数量
        """
        if type(knowledge_id) is not int or knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if type(chunk_indices) is not list:
            raise ValueError("chunk_indices 必须是整数列表")
        frozen_chunk_indices = tuple(chunk_indices)
        if any(type(chunk_index) is not int for chunk_index in frozen_chunk_indices):
            raise ValueError("chunk_indices 必须是整数列表")
        if len(set(frozen_chunk_indices)) != len(frozen_chunk_indices):
            raise ValueError("chunk_indices 不能重复")

        hnswlib_ids = [
            self.encode_chunk_id(knowledge_id, chunk_index)
            for chunk_index in frozen_chunk_indices
        ]
        vectors = self._preflight_vector_write(
            vectors,
            batch_size=len(frozen_chunk_indices),
        )
        if len(frozen_chunk_indices) == 0:
            return 0
        mapping = {
            hnswlib_id: (knowledge_id, chunk_index)
            for hnswlib_id, chunk_index in zip(
                hnswlib_ids,
                frozen_chunk_indices,
            )
        }
        with self._index_pair_lock("chunk_vectors"):
            index = self._reload_index_for_update_locked("chunk_vectors")
            metadata, metadata_bytes = self._load_metadata_snapshot("chunk_vectors")
            with self._pair_rollback_guard("chunk_vectors"):
                self._ensure_capacity(index, count=len(hnswlib_ids))
                index.add_items(
                    vectors,
                    ids=hnswlib_ids,
                    replace_deleted=replace_deleted,
                )
                for hnswlib_id, mapped_value in mapping.items():
                    metadata["id_mapping"][str(hnswlib_id)] = mapped_value
                metadata_path = self.index_dir / "chunk_vectors_metadata.json"
                self._atomic_write_json(
                    metadata_path,
                    metadata,
                    expected_bytes=metadata_bytes,
                    contract=self._contract,
                    pre_publish=lambda temp_path: self._prepare_pair_output(
                        "chunk_vectors", "metadata", temp_path
                    ),
                )
                self._save_index("chunk_vectors")
        logger.info(
            "批量添加分块向量: knowledge_id=%s, count=%s",
            knowledge_id,
            len(hnswlib_ids),
        )
        return len(hnswlib_ids)

    @classmethod
    def encode_chunk_id(cls, knowledge_id: int, chunk_index: int) -> int:
        """将 (knowledge_id, chunk_index) 编码为 hnswlib label。"""
        if type(knowledge_id) is not int or knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if type(chunk_index) is not int:
            raise ValueError("chunk_index 必须为整数")
        if chunk_index < 0:
            raise ValueError("chunk_index 不能为负数")
        if chunk_index > cls.MAX_CHUNK_INDEX:
            raise ValueError(
                f"chunk_index 超出编码范围: {chunk_index} > {cls.MAX_CHUNK_INDEX}"
            )
        return knowledge_id * cls.CHUNK_ID_STRIDE + chunk_index

    @classmethod
    def decode_chunk_id(cls, hnswlib_id: int) -> Tuple[int, int]:
        """将 hnswlib label 解码为 (knowledge_id, chunk_index)。"""
        if hnswlib_id < 0:
            raise ValueError("hnswlib_id 不能为负数")
        knowledge_id = hnswlib_id // cls.CHUNK_ID_STRIDE
        chunk_index = hnswlib_id % cls.CHUNK_ID_STRIDE
        return knowledge_id, chunk_index

    def get_doc_vector(self, knowledge_id: int) -> Optional[np.ndarray]:
        """
        根据 knowledge_id 取回已存储的文档级向量

        在 doc pair lock 内重载最新磁盘索引，再用 hnswlib get_items() 读取。
        用于 get_related 关联推荐：取出条目的 embedding 后做相似搜索。

        Args:
            knowledge_id: 知识条目 ID

        Returns:
            float32 向量 (dim 维)，不存在时返回 None
        """
        with self._index_pair_lock("doc_vectors"):
            index = self._reload_index_for_update_locked("doc_vectors")
            try:
                vectors = index.get_items([knowledge_id])
            except RuntimeError as error:
                if "Label not found" not in str(error):
                    raise
                logger.debug(
                    "文档向量不存在: knowledge_id=%s",
                    knowledge_id,
                )
                return None
            if vectors is not None:
                return self._preflight_document_vector_read(vectors)
        return None

    def delete_vectors_for_entry(self, knowledge_id: int) -> dict:
        """删除指定条目的文档级和分块级向量。

        使用 hnswlib 的 mark_deleted() 标记删除（不重建索引），
        被标记的向量不再出现在搜索结果中。

        Args:
            knowledge_id: 知识条目 ID。

        Returns:
            统计字典 {"doc_deleted": bool, "chunks_deleted": int}。
        """
        stats = {"doc_deleted": False, "chunks_deleted": 0}

        with self._index_pair_lock("doc_vectors"):
            index = self._reload_index_for_update_locked("doc_vectors")
            with self._pair_rollback_guard("doc_vectors"):
                try:
                    index.mark_deleted(knowledge_id)
                except RuntimeError as error:
                    if not _is_idempotent_delete_error(error):
                        raise
                    logger.debug(f"文档向量不存在: knowledge_id={knowledge_id}")
                else:
                    self._save_index("doc_vectors")
                    stats["doc_deleted"] = True
            if stats["doc_deleted"]:
                logger.info(f"标记删除文档向量: knowledge_id={knowledge_id}")

        with self._index_pair_lock("chunk_vectors"):
            index = self._reload_index_for_update_locked("chunk_vectors")
            metadata, metadata_bytes = self._load_metadata_snapshot("chunk_vectors")
            chunk_ids = [
                int(hnswlib_id)
                for hnswlib_id, mapping in metadata.get("id_mapping", {}).items()
                if mapping[0] == knowledge_id
            ]
            with self._pair_rollback_guard("chunk_vectors"):
                for hnswlib_id in chunk_ids:
                    try:
                        index.mark_deleted(hnswlib_id)
                        stats["chunks_deleted"] += 1
                    except RuntimeError as error:
                        if not _is_idempotent_delete_error(error):
                            raise
                        logger.debug(f"分块向量不存在: hnswlib_id={hnswlib_id}")

                if stats["chunks_deleted"] > 0:
                    for hnswlib_id in chunk_ids:
                        metadata.get("id_mapping", {}).pop(str(hnswlib_id), None)
                    metadata_path = self.index_dir / "chunk_vectors_metadata.json"
                    self._atomic_write_json(
                        metadata_path,
                        metadata,
                        expected_bytes=metadata_bytes,
                        contract=self._contract,
                        pre_publish=lambda temp_path: self._prepare_pair_output(
                            "chunk_vectors", "metadata", temp_path
                        ),
                    )
                    self._save_index("chunk_vectors")
            if stats["chunks_deleted"] > 0:
                logger.info(
                    f"标记删除分块向量: knowledge_id={knowledge_id}, "
                    f"count={stats['chunks_deleted']}"
                )

        return stats

    def get_chunk_indices_for_entry(self, knowledge_id: int) -> List[int]:
        """获取条目当前已记录的 chunk_index 列表。"""
        if knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")

        with self._index_pair_lock("chunk_vectors"):
            index = self._reload_index_for_update_locked("chunk_vectors")
            metadata = self._load_metadata("chunk_vectors")
            chunk_indices = []
            for hnswlib_id, mapping in metadata.get("id_mapping", {}).items():
                if int(mapping[0]) != knowledge_id:
                    continue
                canonical_hnswlib_id = int(hnswlib_id)
                if not self._chunk_vector_exists(index, canonical_hnswlib_id):
                    logger.warning(
                        "检测到 chunk metadata/index 漂移: "
                        "knowledge_id=%s, hnswlib_id=%s",
                        knowledge_id,
                        canonical_hnswlib_id,
                    )
                    continue
                chunk_indices.append(int(mapping[1]))
        return sorted(chunk_indices)

    def delete_chunk_vectors_for_entry(self, knowledge_id: int) -> int:
        """仅删除指定条目的分块级向量。"""
        if knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")

        deleted_count = 0
        with self._index_pair_lock("chunk_vectors"):
            index = self._reload_index_for_update_locked("chunk_vectors")
            metadata, metadata_bytes = self._load_metadata_snapshot("chunk_vectors")
            chunk_ids = [
                int(hnswlib_id)
                for hnswlib_id, mapping in metadata.get("id_mapping", {}).items()
                if int(mapping[0]) == knowledge_id
            ]
            with self._pair_rollback_guard("chunk_vectors"):
                for hnswlib_id in chunk_ids:
                    try:
                        index.mark_deleted(hnswlib_id)
                        deleted_count += 1
                    except RuntimeError as error:
                        if not _is_idempotent_delete_error(error):
                            raise
                        logger.debug(f"分块向量不存在: hnswlib_id={hnswlib_id}")

                if deleted_count > 0:
                    for hnswlib_id in chunk_ids:
                        metadata.get("id_mapping", {}).pop(str(hnswlib_id), None)
                    metadata_path = self.index_dir / "chunk_vectors_metadata.json"
                    self._atomic_write_json(
                        metadata_path,
                        metadata,
                        expected_bytes=metadata_bytes,
                        contract=self._contract,
                        pre_publish=lambda temp_path: self._prepare_pair_output(
                            "chunk_vectors", "metadata", temp_path
                        ),
                    )
                    self._save_index("chunk_vectors")
            if deleted_count > 0:
                logger.info(
                    "标记删除分块向量: knowledge_id=%s, count=%s",
                    knowledge_id,
                    deleted_count,
                )

        return deleted_count

    @staticmethod
    def _knn_query_active(
        index: hnswlib.Index,
        query_vector: np.ndarray,
        k: int,
        *,
        allowed_labels: Optional[set[int]] = None,
    ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """查询至多 k 个 active label，兼容 hnswlib 计数包含 deleted label。"""
        if k <= 0:
            return None
        current_count = index.get_current_count()
        if current_count <= 0:
            return None

        requested_k = min(k, current_count)
        reshaped_query = query_vector.reshape(1, -1)

        def query(candidate_k: int) -> Tuple[np.ndarray, np.ndarray]:
            if allowed_labels is None:
                return index.knn_query(reshaped_query, k=candidate_k)
            return index.knn_query(
                reshaped_query,
                k=candidate_k,
                filter=lambda label: int(label) in allowed_labels,
            )

        try:
            return query(requested_k)
        except RuntimeError as error:
            # hnswlib 没有 active_count API；deleted label 仍计入
            # get_current_count()，当 k 大于 active 数时会抛此固定错误。
            if "Cannot return the results in a contiguous 2D array" not in str(error):
                raise

        best_result: Optional[Tuple[np.ndarray, np.ndarray]] = None
        lower_bound = 1
        upper_bound = requested_k - 1
        while lower_bound <= upper_bound:
            candidate_k = (lower_bound + upper_bound) // 2
            try:
                candidate_result = query(candidate_k)
            except RuntimeError as error:
                if "Cannot return the results in a contiguous 2D array" not in str(
                    error
                ):
                    raise
                upper_bound = candidate_k - 1
            else:
                best_result = candidate_result
                lower_bound = candidate_k + 1

        return best_result

    def search_doc(self, query_vector: np.ndarray, k: int = 10) -> List[Tuple[int, float]]:
        """
        搜索文档级向量

        Args:
            query_vector: 查询向量
            k: 返回前 k 个结果

        Returns:
            [(knowledge_id, distance), ...] 列表
        """
        query_vector = self._preflight_vector_query(query_vector)

        with self._index_pair_lock("doc_vectors"):
            index = self._reload_index_for_update_locked("doc_vectors")
            neighbors = self._knn_query_active(index, query_vector, k)
        if neighbors is None:
            return []

        labels, distances = neighbors
        return [(int(label), float(dist)) for label, dist in zip(labels[0], distances[0])]

    def search_chunk(self, query_vector: np.ndarray, k: int = 10) -> List[Tuple[int, int, float]]:
        """
        搜索分块级向量

        Args:
            query_vector: 查询向量
            k: 返回前 k 个结果

        Returns:
            [(knowledge_id, chunk_index, distance), ...] 列表
        """
        query_vector = self._preflight_vector_query(query_vector)

        with self._index_pair_lock("chunk_vectors"):
            try:
                index, index_state, metadata, metadata_bytes = (
                    self._reload_index_for_update_locked(
                        "chunk_vectors",
                        include_file_state=True,
                    )
                )
            except Exception:
                self._validated_chunk_pair_key = None
                raise
            validation_key = self._chunk_pair_validation_key(
                index_state,
                metadata_bytes,
            )
            cache_hit = validation_key == self._validated_chunk_pair_key
            if not cache_hit:
                # 旧 key 对当前 pair 没有证明力；失败路径必须保持 cache 为空。
                self._validated_chunk_pair_key = None
            active_mapping = self._parse_chunk_id_mapping(metadata)
            if not active_mapping:
                if cache_hit:
                    return []
                # 空 mapping 只在索引确实没有 active label 时表示正常 no_hits。
                # 如果仍能查询到 label，则 metadata 已与 index 漂移，必须 fail closed。
                if self._knn_query_active(index, query_vector, 1) is not None:
                    raise PKVRuntimeError(
                        ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                        "chunk id_mapping 为空但索引仍包含 active label",
                        stage="chunk_index_metadata",
                        recoverable=True,
                    )
                if self._index_file_state(
                    self.index_dir / "chunk_vectors.idx"
                ) != index_state:
                    raise PKVRuntimeError(
                        ErrorCode.PATH_STATE_UNDETERMINED,
                        "chunk 索引在完整性校验期间发生变化",
                        stage="chunk_index_metadata",
                        recoverable=True,
                    )
                self._validated_chunk_pair_key = validation_key
                return []

            # Pair 首次出现或身份变化时，用 mapping_count + 1 探测全部 active
            # labels。稳定且已验证的 pair 恢复普通 top-k，避免每次 O(N) 输出。
            requested_k = k if cache_hit else len(active_mapping) + 1
            neighbors = self._knn_query_active(
                index,
                query_vector,
                requested_k,
            )
            if neighbors is None:
                raise PKVRuntimeError(
                    ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                    "chunk id_mapping 未对应任何 active label",
                    stage="chunk_index_metadata",
                    recoverable=True,
                )
            labels, distances = neighbors
            actual_labels = tuple(int(label) for label in labels[0])
            if not cache_hit and (
                len(actual_labels) != len(active_mapping)
                or set(actual_labels) != set(active_mapping)
            ):
                raise PKVRuntimeError(
                    ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                    "chunk index active labels 与 id_mapping 不一致",
                    stage="chunk_index_metadata",
                    recoverable=True,
                )

            # 从同一个受锁快照解析结果；任一失败都不得发布新的 cache key。
            results = []
            result_count = min(k, len(actual_labels))
            for label, dist in zip(
                labels[0][:result_count],
                distances[0][:result_count],
            ):
                hnswlib_id = int(label)
                mapping = active_mapping.get(hnswlib_id)
                if mapping is None:
                    self._validated_chunk_pair_key = None
                    raise PKVRuntimeError(
                        ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                        "chunk 查询命中缺少 id_mapping",
                        stage="chunk_index_metadata",
                        recoverable=True,
                    )
                knowledge_id, chunk_index = mapping
                results.append((knowledge_id, chunk_index, float(dist)))

            if not cache_hit:
                if self._index_file_state(
                    self.index_dir / "chunk_vectors.idx"
                ) != index_state:
                    raise PKVRuntimeError(
                        ErrorCode.PATH_STATE_UNDETERMINED,
                        "chunk 索引在完整性校验期间发生变化",
                        stage="chunk_index_metadata",
                        recoverable=True,
                    )
                self._validated_chunk_pair_key = validation_key
            return results

    @staticmethod
    def _chunk_pair_validation_key(
        index_state: tuple[int, int, int, int, int],
        metadata_bytes: bytes,
    ) -> tuple[int, int, int, int, int, str]:
        """绑定实际加载的 index 状态与同锁内 metadata 内容。"""

        return (*index_state, hashlib.sha256(metadata_bytes).hexdigest())

    @classmethod
    def _parse_chunk_id_mapping(
        cls,
        metadata: dict[str, Any],
    ) -> dict[int, tuple[int, int]]:
        """严格解析 chunk mapping；任一畸形项都使整个快照不可用。"""

        if "id_mapping" not in metadata:
            raise PKVRuntimeError(
                ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                "chunk metadata 缺少 id_mapping",
                stage="chunk_index_metadata",
                recoverable=True,
            )
        raw_mapping = metadata["id_mapping"]
        if not isinstance(raw_mapping, dict):
            raise PKVRuntimeError(
                ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                "chunk id_mapping 必须是 JSON object",
                stage="chunk_index_metadata",
                recoverable=True,
            )

        parsed: dict[int, tuple[int, int]] = {}
        for raw_label, raw_value in raw_mapping.items():
            valid_label = (
                isinstance(raw_label, str)
                and raw_label.isascii()
                and raw_label.isdigit()
            )
            valid_value = (
                isinstance(raw_value, (list, tuple))
                and len(raw_value) == 2
                and type(raw_value[0]) is int
                and type(raw_value[1]) is int
            )
            if not valid_label or not valid_value:
                raise PKVRuntimeError(
                    ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                    "chunk id_mapping 包含畸形条目",
                    stage="chunk_index_metadata",
                    recoverable=True,
                )

            label = int(raw_label)
            knowledge_id = raw_value[0]
            chunk_index = raw_value[1]
            if (
                knowledge_id <= 0
                or chunk_index < 0
                or chunk_index > cls.MAX_CHUNK_INDEX
                or cls.encode_chunk_id(knowledge_id, chunk_index) != label
                or str(label) != raw_label
            ):
                raise PKVRuntimeError(
                    ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
                    "chunk id_mapping 与编码合同不一致",
                    stage="chunk_index_metadata",
                    recoverable=True,
                )
            parsed[label] = (knowledge_id, chunk_index)
        return parsed

    def _publish_index(self, name: str, index: hnswlib.Index) -> None:
        """保存 idx，并在 replace 前把将发布临时文件绑定进配对事务。"""
        index_path = self.index_dir / f"{name}.idx"
        temp_path_holder: list[Path] = []

        def write_index(temp_path: Path) -> None:
            temp_path_holder.append(temp_path)
            index.save_index(str(temp_path))

        def prepare_output() -> None:
            if not temp_path_holder:
                raise RuntimeError("向量索引临时文件尚未准备")
            self._prepare_pair_output(name, "index", temp_path_holder[0])

        _contract_publish(
            self._contract,
            index_path,
            label="向量索引文件",
            writer=write_index,
            pre_replace=prepare_output,
        )

    def _save_index(self, name: str):
        """通过同目录临时文件原子保存索引，避免破坏现有 idx（统一叶子合同）。"""
        index = self.doc_index if name == "doc_vectors" else self.chunk_index
        self._publish_index(name, index)

    def _load_metadata(self, name: str) -> dict:
        """加载元数据"""
        return self._load_metadata_snapshot(name)[0]

    def _load_metadata_snapshot(self, name: str) -> tuple[dict[str, Any], bytes]:
        """加载 metadata 及用于并发写保护的原始字节。"""
        metadata_path = self.index_dir / f"{name}_metadata.json"
        return self._read_json_snapshot(
            metadata_path,
            contract=self._contract,
        )

    def _validate_embedding_fingerprint(
        self,
        name: str,
        metadata: dict[str, Any],
        *,
        expected_bytes: bytes,
    ) -> None:
        """校验索引元数据中的 Embedding 契约指纹。"""
        existing_fingerprint = metadata.get(self.EMBEDDING_FINGERPRINT_V2_KEY)
        if existing_fingerprint is None:
            invalid_legacy_fingerprint = metadata.get(
                self.LEGACY_EMBEDDING_FINGERPRINT_KEY
            )
            if invalid_legacy_fingerprint is not None:
                raise RuntimeError(
                    f"Embedding 索引契约格式无效: name={name}。"
                    "请重建向量索引并重新生成 Embedding。"
                )
            schema_version = metadata.get("schema_version")
            is_genuine_legacy = schema_version is None or (
                type(schema_version) is int
                and 0 <= schema_version < self.METADATA_SCHEMA_VERSION
            )

            if is_genuine_legacy:
                logger.warning(
                    "%s 缺少 Embedding 契约指纹，按旧索引兼容加载；"
                    "如已切换 Embedding 端点、模型或维度，请重建向量索引",
                    name,
                )
                return
            raise RuntimeError(
                f"{name} 的当前 metadata schema 缺少 Embedding v2 契约指纹，"
                "拒绝静默复用索引；请重建向量索引并重新生成 Embedding。"
            )

        if not isinstance(existing_fingerprint, dict):
            raise RuntimeError(
                f"Embedding 索引契约格式无效: name={name}。"
                "请重建向量索引并重新生成 Embedding。"
            )

        expected = self._normalize_stored_embedding_fingerprint(
            self.embedding_fingerprint
        )
        normalized_existing = self._normalize_stored_embedding_fingerprint(
            existing_fingerprint
        )
        if normalized_existing != expected:
            mismatched_fields = ", ".join(
                key
                for key in expected
                if normalized_existing.get(key) != expected.get(key)
            )
            raise RuntimeError(
                "Embedding 索引契约不匹配: "
                f"name={name}, fields={mismatched_fields}。"
                "当前初始化不会自动重建索引。"
                "如果要继续使用现有索引，请切回原来的 Embedding 服务/模型/维度配置；"
                "如果确认切换模型或端点，请先重建向量索引并重新生成 Embedding。"
            )

        persisted = self._persisted_embedding_fingerprint(normalized_existing)
        migrated_metadata = dict(metadata)
        migrated_metadata[self.EMBEDDING_FINGERPRINT_V2_KEY] = persisted
        migrated_metadata["schema_version"] = self.METADATA_SCHEMA_VERSION
        if self._legacy_embedding_fingerprint is None:
            migrated_metadata.pop(self.LEGACY_EMBEDDING_FINGERPRINT_KEY, None)
        else:
            migrated_metadata[self.LEGACY_EMBEDDING_FINGERPRINT_KEY] = (
                self._legacy_embedding_fingerprint
            )

        if migrated_metadata != metadata:
            metadata_path = self.index_dir / f"{name}_metadata.json"
            self._atomic_write_json(
                metadata_path,
                migrated_metadata,
                expected_bytes=expected_bytes,
                contract=self._contract,
            )

    def _update_metadata(self, name: str, hnswlib_id: int, mapping: Tuple[int, int]):
        """更新元数据映射"""
        with self._index_pair_lock(name):
            metadata, metadata_bytes = self._load_metadata_snapshot(name)
            metadata["id_mapping"][str(hnswlib_id)] = mapping
            metadata_path = self.index_dir / f"{name}_metadata.json"
            self._atomic_write_json(
                metadata_path,
                metadata,
                expected_bytes=metadata_bytes,
                contract=self._contract,
            )

    def _update_metadata_batch(self, name: str, mappings: dict[int, Tuple[int, int]]):
        """批量更新元数据映射。"""
        if not mappings:
            return

        with self._index_pair_lock(name):
            metadata, metadata_bytes = self._load_metadata_snapshot(name)
            for hnswlib_id, mapping in mappings.items():
                metadata["id_mapping"][str(hnswlib_id)] = mapping
            metadata_path = self.index_dir / f"{name}_metadata.json"
            self._atomic_write_json(
                metadata_path,
                metadata,
                expected_bytes=metadata_bytes,
                contract=self._contract,
            )

    @staticmethod
    def _chunk_vector_exists(index: hnswlib.Index, hnswlib_id: int) -> bool:
        """校验 metadata 中的 chunk label 是否真实存在于索引。"""
        try:
            vectors = index.get_items([hnswlib_id])
        except RuntimeError as error:
            if "Label not found" not in str(error):
                raise
            return False
        return vectors is not None and len(vectors) > 0

    def get_index_stats(self) -> dict:
        """
        获取索引统计信息

        Returns:
            统计信息字典
        """
        with self._index_pair_lock("doc_vectors"):
            doc_index = self._reload_index_for_update_locked("doc_vectors")
            doc_count = doc_index.get_current_count()
        with self._index_pair_lock("chunk_vectors"):
            chunk_index = self._reload_index_for_update_locked("chunk_vectors")
            chunk_count = chunk_index.get_current_count()
        return {
            "doc_count": doc_count,
            "chunk_count": chunk_count,
            "dim": self.dim,
            "embedding_fingerprint": self.embedding_fingerprint,
            "M": self.M,
            "ef_search": self.ef_search,
        }
