"""
向量存储层

基于 hnswlib 的向量索引管理
"""

import json
import os
import shutil
import tempfile
import time
from contextlib import contextmanager

import hnswlib
import numpy as np
from pathlib import Path
from typing import Any, List, Tuple, Optional

from src.utils.config import (
    endpoint_contract_sha256,
    get_config,
    url_contains_credentials,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)


class _UnsupportedMetadataFormatError(RuntimeError):
    """metadata 使用了不受支持、畸形或相互冲突的格式。"""


class _FutureMetadataSchemaError(_UnsupportedMetadataFormatError):
    """metadata 使用了当前 reader 不认识的未来 schema。"""


class VectorStore:
    """hnswlib 向量索引管理器"""

    CHUNK_ID_STRIDE = 10000
    MAX_CHUNK_INDEX = CHUNK_ID_STRIDE - 1
    METADATA_SCHEMA_VERSION = 2
    EMBEDDING_FINGERPRINT_SCHEMA_VERSION = 2
    LEGACY_EMBEDDING_FINGERPRINT_KEY = "embedding_fingerprint"
    EMBEDDING_FINGERPRINT_V2_KEY = "embedding_fingerprint_v2"

    def __init__(self, index_dir: Path, dim: Optional[int] = None):
        """
        初始化向量索引

        Args:
            index_dir: 向量索引目录
            dim: 向量维度；未传入时优先沿用已有索引维度，否则回落到配置值
        """
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
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

        logger.info(f"向量存储初始化完成: {self.index_dir}")

    @classmethod
    def has_index_artifacts(cls, index_dir: Path) -> bool:
        """检查索引目录中是否已经存在向量索引相关文件。"""
        target_dir = Path(index_dir)
        for name in ("doc_vectors", "chunk_vectors"):
            index_path = target_dir / f"{name}.idx"
            metadata_path = target_dir / f"{name}_metadata.json"
            if index_path.exists() or metadata_path.exists():
                return True
        return False

    def _resolve_index_dim(self, requested_dim: Optional[int]) -> int:
        """解析当前索引目录应使用的向量维度。"""
        metadata_dims: dict[str, int] = {}
        for name in ("doc_vectors", "chunk_vectors"):
            metadata_path = self.index_dir / f"{name}_metadata.json"
            if not metadata_path.exists():
                continue

            with open(metadata_path, "r", encoding="utf-8") as f:
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
    ) -> tuple[dict[str, Any], bytes]:
        """一次读取 metadata 内容及其 CAS 字节快照。"""
        original_bytes = path.read_bytes()
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
    def _metadata_write_lock(path: Path):
        """用同目录 sidecar advisory lock 串行化当前版本的 metadata writer。"""
        lock_path = path.with_name(f".{path.name}.lock")
        lock_file = open(lock_path, "a+b")
        if lock_file.seek(0, os.SEEK_END) == 0:
            lock_file.write(b"\0")
            lock_file.flush()

        acquired = False
        deadline = time.monotonic() + 10.0
        try:
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
    ) -> None:
        """在 metadata 写锁内落盘、CAS 并原子替换 JSON 文件。"""
        with VectorStore._metadata_write_lock(path):
            temp_path: Optional[Path] = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temp_file:
                    temp_path = Path(temp_file.name)
                    json.dump(payload, temp_file, indent=2)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())

                if expected_bytes is not None:
                    try:
                        current_bytes = path.read_bytes()
                    except FileNotFoundError:
                        current_bytes = None
                    if current_bytes != expected_bytes:
                        raise RuntimeError(
                            f"{path.name} 在写入期间发生并发修改，请重试"
                        )
                elif require_missing and path.exists():
                    raise RuntimeError(f"{path.name} 已被并发创建，请重试")

                os.replace(temp_path, path)
                temp_path = None
            finally:
                if temp_path is not None:
                    try:
                        temp_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        # 不覆盖原始写入异常；正常可删除的临时文件已在此清理。
                        pass

    def _migrate_legacy_embedding_fingerprints(self) -> None:
        """在任何校验前，以文件级原子操作升级 doc/chunk 元数据。"""
        future_schema_names: list[str] = []
        unsupported_format_names: list[str] = []
        for name in ("doc_vectors", "chunk_vectors"):
            metadata_path = self.index_dir / f"{name}_metadata.json"
            try:
                if not metadata_path.exists():
                    continue
                metadata, _ = self._read_json_snapshot(
                    metadata_path,
                    validate_schema=False,
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
                if not metadata_path.exists():
                    continue
                with self._index_pair_lock(name):
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
        metadata, original_bytes = self._read_json_snapshot(path)
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
                if not metadata_path.exists():
                    continue
                with self._index_pair_lock(name):
                    metadata, original_bytes = self._read_json_snapshot(
                        metadata_path
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
        with self._metadata_write_lock(index_path):
            yield

    @staticmethod
    def _create_rollback_copy(source: Path) -> Path:
        """在同目录创建并 fsync 一份精确的 rollback 副本。"""
        descriptor, raw_path = tempfile.mkstemp(
            dir=source.parent,
            prefix=f".{source.name}.",
            suffix=".rollback",
        )
        rollback_path = Path(raw_path)
        try:
            with os.fdopen(descriptor, "wb") as destination, open(
                source, "rb"
            ) as source_file:
                shutil.copyfileobj(source_file, destination)
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            try:
                rollback_path.unlink()
            except OSError:
                pass
            raise
        return rollback_path

    @contextmanager
    def _pair_rollback_guard(self, name: str):
        """写失败时恢复 index/metadata 原字节，并重载当前实例内存。"""
        index_path = self.index_dir / f"{name}.idx"
        metadata_path = self.index_dir / f"{name}_metadata.json"
        rollback_files: list[tuple[Path, Path]] = []
        preserve_rollback_files = False
        try:
            rollback_files.append(
                (self._create_rollback_copy(index_path), index_path)
            )
            rollback_files.append(
                (self._create_rollback_copy(metadata_path), metadata_path)
            )
        except BaseException:
            for rollback_path, _ in rollback_files:
                try:
                    rollback_path.unlink()
                except OSError:
                    pass
            raise

        try:
            yield
        except BaseException as original_error:
            restore_failures: list[str] = []
            for rollback_path, target_path in rollback_files:
                try:
                    os.replace(rollback_path, target_path)
                except OSError:
                    restore_failures.append(target_path.name)
            if restore_failures:
                preserve_rollback_files = True
                retained_paths = [
                    str(rollback_path)
                    for rollback_path, _ in rollback_files
                    if rollback_path.exists()
                ]
                raise RuntimeError(
                    "index/metadata 回滚失败: "
                    + ", ".join(restore_failures)
                    + "；已保留可用 rollback 副本: "
                    + (", ".join(retained_paths) or "无")
                ) from original_error
            try:
                self._reload_index_for_update_locked(name)
            except BaseException as reload_error:
                raise RuntimeError(
                    f"{name} 磁盘文件已回滚，但当前实例重载失败: {reload_error}"
                ) from original_error
            raise
        finally:
            if not preserve_rollback_files:
                for rollback_path, _ in rollback_files:
                    try:
                        rollback_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        logger.warning(
                            "无法清理 rollback 文件: %s",
                            rollback_path.name,
                        )

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
        index_path = self.index_dir / f"{name}.idx"
        metadata_path = self.index_dir / f"{name}_metadata.json"

        if index_path.exists() != metadata_path.exists():
            raise RuntimeError(
                f"{name} 索引文件与元数据不一致，无法安全初始化: "
                f"index_exists={index_path.exists()}, metadata_exists={metadata_path.exists()}"
            )

        # 创建索引对象
        index = hnswlib.Index(space='cosine', dim=self.dim)

        if index_path.exists():
            metadata, metadata_bytes = self._read_json_snapshot(metadata_path)
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

            index.load_index(
                str(index_path),
                allow_replace_deleted=True,
            )
            # 修正容量：若 max_elements 不足则扩容到安全值
            if index.max_elements < index.element_count + 1000:
                safe_size = max(10000, index.element_count + 1000)
                index.resize_index(safe_size)
                logger.info(
                    f"🔄 索引容量不足，已扩容至 {safe_size}: {name}"
                )
            logger.info(f"✅ 加载已有索引: {index_path}")
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
            try:
                index.save_index(str(index_path))
                self._atomic_write_json(
                    metadata_path,
                    metadata,
                    require_missing=True,
                )
            except Exception:
                # 本分支进入时两份 artifact 均不存在；失败必须回收本次创建物，
                # 否则下次初始化会永久卡在 index/metadata mismatch。
                # metadata helper 失败时不会创建目标；若目标此时存在则属于并发 writer，
                # 不能清理。这里只回收本实例刚写出的 index artifact。
                try:
                    index_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.error("新索引初始化失败后无法清理: %s", index_path.name)
                raise

            logger.info(f"✅ 创建新索引: {index_path}")

        # 设置查询时的搜索深度
        index.set_ef(self.ef_search)

        return index

    def _reload_index_for_update_locked(self, name: str) -> hnswlib.Index:
        """在配对锁内从磁盘重载最新 index，并重新校验其 metadata。"""
        index_path = self.index_dir / f"{name}.idx"
        metadata_path = self.index_dir / f"{name}_metadata.json"
        if not index_path.exists() or not metadata_path.exists():
            raise RuntimeError(f"{name} index/metadata 配对缺失，无法安全更新")

        metadata, metadata_bytes = self._read_json_snapshot(metadata_path)
        existing_dim = metadata.get("dim")
        if existing_dim is None or int(existing_dim) != self.dim:
            raise RuntimeError(f"{name} 维度不匹配，无法安全更新")
        self._validate_embedding_fingerprint(
            name,
            metadata,
            expected_bytes=metadata_bytes,
        )

        index = hnswlib.Index(space="cosine", dim=self.dim)
        index.load_index(str(index_path), allow_replace_deleted=True)
        if index.max_elements < index.element_count + 1000:
            index.resize_index(max(10000, index.element_count + 1000))
        index.set_ef(self.ef_search)
        if name == "doc_vectors":
            self.doc_index = index
        else:
            self.chunk_index = index
        return index

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
        # 确保向量是 float32 类型
        if vector.dtype != np.float32:
            vector = vector.astype('float32')

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
        # 确保向量是 float32 类型
        if vector.dtype != np.float32:
            vector = vector.astype('float32')

        hnswlib_id = self.encode_chunk_id(knowledge_id, chunk_index)

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
        if knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if len(chunk_indices) == 0:
            return 0
        if vectors.ndim != 2:
            raise ValueError("vectors 必须是二维矩阵")
        if len(chunk_indices) != vectors.shape[0]:
            raise ValueError("chunk_indices 与 vectors 行数必须一致")

        if vectors.dtype != np.float32:
            vectors = vectors.astype("float32")

        hnswlib_ids = [
            self.encode_chunk_id(knowledge_id, chunk_index)
            for chunk_index in chunk_indices
        ]
        mapping = {
            hnswlib_id: (knowledge_id, chunk_index)
            for hnswlib_id, chunk_index in zip(hnswlib_ids, chunk_indices)
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
        if knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
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
            if vectors is not None and len(vectors) > 0:
                return np.array(vectors[0], dtype=np.float32)
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
                except RuntimeError:
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
                    except RuntimeError:
                        logger.debug(f"分块向量不存在: hnswlib_id={hnswlib_id}")

                if stats["chunks_deleted"] > 0:
                    for hnswlib_id in chunk_ids:
                        metadata.get("id_mapping", {}).pop(str(hnswlib_id), None)
                    metadata_path = self.index_dir / "chunk_vectors_metadata.json"
                    self._atomic_write_json(
                        metadata_path,
                        metadata,
                        expected_bytes=metadata_bytes,
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
                if not self._chunk_vector_exists(index, int(hnswlib_id)):
                    logger.warning(
                        "检测到 chunk metadata/index 漂移: "
                        "knowledge_id=%s, hnswlib_id=%s",
                        knowledge_id,
                        hnswlib_id,
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
                    except RuntimeError:
                        logger.debug(f"分块向量不存在: hnswlib_id={hnswlib_id}")

                if deleted_count > 0:
                    for hnswlib_id in chunk_ids:
                        metadata.get("id_mapping", {}).pop(str(hnswlib_id), None)
                    metadata_path = self.index_dir / "chunk_vectors_metadata.json"
                    self._atomic_write_json(
                        metadata_path,
                        metadata,
                        expected_bytes=metadata_bytes,
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
        # 确保向量是 float32 类型
        if query_vector.dtype != np.float32:
            query_vector = query_vector.astype('float32')

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
        # 确保向量是 float32 类型
        if query_vector.dtype != np.float32:
            query_vector = query_vector.astype('float32')

        with self._index_pair_lock("chunk_vectors"):
            index = self._reload_index_for_update_locked("chunk_vectors")
            metadata = self._load_metadata("chunk_vectors")
            id_mapping = metadata.get("id_mapping", {})
            if not isinstance(id_mapping, dict) or not id_mapping:
                return []

            active_mapping: dict[int, tuple[int, int]] = {}
            for hnswlib_id, mapping in id_mapping.items():
                if not isinstance(mapping, (list, tuple)) or len(mapping) < 2:
                    continue
                try:
                    active_mapping[int(hnswlib_id)] = (
                        int(mapping[0]),
                        int(mapping[1]),
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
            if not active_mapping:
                return []

            neighbors = self._knn_query_active(
                index,
                query_vector,
                min(k, len(active_mapping)),
                allowed_labels=set(active_mapping),
            )
        if neighbors is None:
            return []
        labels, distances = neighbors

        # 从元数据中解析 (knowledge_id, chunk_index)
        results = []
        for label, dist in zip(labels[0], distances[0]):
            hnswlib_id = int(label)
            mapping = active_mapping.get(hnswlib_id)
            if mapping is None:
                logger.warning(
                    "忽略缺少 active metadata mapping 的 chunk label: %s",
                    hnswlib_id,
                )
                continue
            knowledge_id, chunk_index = mapping
            results.append((knowledge_id, chunk_index, float(dist)))

        return results

    def _save_index(self, name: str):
        """通过同目录临时文件原子保存索引，避免破坏现有 idx。"""
        index_path = self.index_dir / f"{name}.idx"
        index = self.doc_index if name == "doc_vectors" else self.chunk_index
        descriptor, raw_path = tempfile.mkstemp(
            dir=index_path.parent,
            prefix=f".{index_path.name}.",
            suffix=".tmp",
        )
        os.close(descriptor)
        temp_path: Optional[Path] = Path(raw_path)
        try:
            index.save_index(str(temp_path))
            # Windows CRT 的 fsync/_commit 需要可写文件描述符。
            with open(temp_path, "r+b") as temp_file:
                os.fsync(temp_file.fileno())
            os.replace(temp_path, index_path)
            temp_path = None
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    # 不覆盖原始保存异常。
                    pass

    def _load_metadata(self, name: str) -> dict:
        """加载元数据"""
        return self._load_metadata_snapshot(name)[0]

    def _load_metadata_snapshot(self, name: str) -> tuple[dict[str, Any], bytes]:
        """加载 metadata 及用于并发写保护的原始字节。"""
        metadata_path = self.index_dir / f"{name}_metadata.json"
        return self._read_json_snapshot(metadata_path)

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
