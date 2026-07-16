"""
向量存储层

基于 hnswlib 的向量索引管理
"""

import json
import hnswlib
import numpy as np
from pathlib import Path
from typing import Any, List, Tuple, Optional

from src.utils.config import get_config
from src.utils.logger import get_logger

logger = get_logger(__name__)


class VectorStore:
    """hnswlib 向量索引管理器"""

    CHUNK_ID_STRIDE = 10000
    MAX_CHUNK_INDEX = CHUNK_ID_STRIDE - 1

    def __init__(self, index_dir: Path, dim: Optional[int] = None):
        """
        初始化向量索引

        Args:
            index_dir: 向量索引目录
            dim: 向量维度；未传入时优先沿用已有索引维度，否则回落到配置值
        """
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.dim = self._resolve_index_dim(dim)
        self.embedding_fingerprint = self._resolve_embedding_fingerprint(self.dim)

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

    def _resolve_embedding_fingerprint(self, dim: int) -> dict[str, str]:
        """解析当前向量索引应绑定的 Embedding 契约指纹。"""
        config = get_config()
        if hasattr(config, "embedding_index_fingerprint"):
            return config.embedding_index_fingerprint(dim)
        return {
            "base_url": str(getattr(config, "embd_base_url", "")),
            "embedding_model": str(getattr(config, "embd_model", "")),
            "embedding_dim": str(int(dim)),
        }

    def _init_index(self, name: str) -> hnswlib.Index:
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
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
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
            self._validate_embedding_fingerprint(name, metadata)

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
            # 保存空索引
            index.save_index(str(index_path))

            # 创建元数据文件
            metadata = {
                "dim": self.dim,
                "space": "cosine",
                "M": self.M,
                "ef_construction": self.ef_construction,
                "embedding_fingerprint": self.embedding_fingerprint,
                "id_mapping": {}
            }
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2)

            logger.info(f"✅ 创建新索引: {index_path}")

        # 设置查询时的搜索深度
        index.set_ef(self.ef_search)

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

        # 确保容量充足
        self._ensure_capacity(self.doc_index)

        # 添加向量 (使用 knowledge_id 作为 hnswlib 的标签)
        self.doc_index.add_items(
            vector.reshape(1, -1),
            ids=[knowledge_id],
            replace_deleted=replace_deleted,
        )

        # 保存索引
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

        # 确保容量充足
        self._ensure_capacity(self.chunk_index)

        # 添加向量
        self.chunk_index.add_items(
            vector.reshape(1, -1),
            ids=[hnswlib_id],
            replace_deleted=replace_deleted,
        )

        # 保存映射关系
        self._update_metadata("chunk_vectors", hnswlib_id, (knowledge_id, chunk_index))

        # 保存索引
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
        self._ensure_capacity(self.chunk_index, count=len(hnswlib_ids))
        self.chunk_index.add_items(
            vectors,
            ids=hnswlib_ids,
            replace_deleted=replace_deleted,
        )

        mapping = {
            hnswlib_id: (knowledge_id, chunk_index)
            for hnswlib_id, chunk_index in zip(hnswlib_ids, chunk_indices)
        }
        self._update_metadata_batch("chunk_vectors", mapping)
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

        利用 hnswlib 原生 get_items() 从内存索引中读取。
        用于 get_related 关联推荐：取出条目的 embedding 后做相似搜索。

        Args:
            knowledge_id: 知识条目 ID

        Returns:
            float32 向量 (dim 维)，不存在时返回 None
        """
        try:
            vectors = self.doc_index.get_items([knowledge_id])
            if vectors is not None and len(vectors) > 0:
                return np.array(vectors[0], dtype=np.float32)
        except Exception as e:
            logger.debug(f"获取文档向量失败 (knowledge_id={knowledge_id}): {e}")
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

        # 1. 删除文档级向量
        try:
            self.doc_index.mark_deleted(knowledge_id)
            self._save_index("doc_vectors")
            stats["doc_deleted"] = True
            logger.info(f"标记删除文档向量: knowledge_id={knowledge_id}")
        except RuntimeError:
            # hnswlib: label not found
            logger.debug(f"文档向量不存在: knowledge_id={knowledge_id}")

        # 2. 删除分块级向量
        metadata = self._load_metadata("chunk_vectors")
        chunk_ids = [
            int(hnswlib_id)
            for hnswlib_id, mapping in metadata.get("id_mapping", {}).items()
            if mapping[0] == knowledge_id
        ]

        for hnswlib_id in chunk_ids:
            try:
                self.chunk_index.mark_deleted(hnswlib_id)
                stats["chunks_deleted"] += 1
            except RuntimeError:
                logger.debug(f"分块向量不存在: hnswlib_id={hnswlib_id}")

        if stats["chunks_deleted"] > 0:
            for hnswlib_id in chunk_ids:
                metadata.get("id_mapping", {}).pop(str(hnswlib_id), None)
            metadata_path = self.index_dir / "chunk_vectors_metadata.json"
            with open(metadata_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)
            self._save_index("chunk_vectors")
            logger.info(
                f"标记删除分块向量: knowledge_id={knowledge_id}, "
                f"count={stats['chunks_deleted']}"
            )

        return stats

    def get_chunk_indices_for_entry(self, knowledge_id: int) -> List[int]:
        """获取条目当前已记录的 chunk_index 列表。"""
        if knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")

        metadata = self._load_metadata("chunk_vectors")
        chunk_indices = []
        for hnswlib_id, mapping in metadata.get("id_mapping", {}).items():
            if int(mapping[0]) != knowledge_id:
                continue
            if not self._chunk_vector_exists(int(hnswlib_id)):
                logger.warning(
                    "检测到 chunk metadata/index 漂移: knowledge_id=%s, hnswlib_id=%s",
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

        metadata = self._load_metadata("chunk_vectors")
        chunk_ids = [
            int(hnswlib_id)
            for hnswlib_id, mapping in metadata.get("id_mapping", {}).items()
            if int(mapping[0]) == knowledge_id
        ]

        deleted_count = 0
        for hnswlib_id in chunk_ids:
            try:
                self.chunk_index.mark_deleted(hnswlib_id)
                deleted_count += 1
            except RuntimeError:
                logger.debug(f"分块向量不存在: hnswlib_id={hnswlib_id}")

        if deleted_count > 0:
            for hnswlib_id in chunk_ids:
                metadata.get("id_mapping", {}).pop(str(hnswlib_id), None)
            metadata_path = self.index_dir / "chunk_vectors_metadata.json"
            with open(metadata_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)
            self._save_index("chunk_vectors")
            logger.info(
                "标记删除分块向量: knowledge_id=%s, count=%s",
                knowledge_id,
                deleted_count,
            )

        return deleted_count

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

        current_count = self.doc_index.get_current_count()
        if current_count <= 0:
            return []

        k_safe = min(k, current_count)
        labels, distances = self.doc_index.knn_query(query_vector.reshape(1, -1), k=k_safe)
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

        current_count = self.chunk_index.get_current_count()
        if current_count <= 0:
            return []

        k_safe = min(k, current_count)
        labels, distances = self.chunk_index.knn_query(query_vector.reshape(1, -1), k=k_safe)

        # 从元数据中解析 (knowledge_id, chunk_index)
        results = []
        for label, dist in zip(labels[0], distances[0]):
            hnswlib_id = int(label)
            knowledge_id, chunk_index = self.decode_chunk_id(hnswlib_id)
            results.append((knowledge_id, chunk_index, float(dist)))

        return results

    def _save_index(self, name: str):
        """保存索引到磁盘"""
        index_path = self.index_dir / f"{name}.idx"
        if name == "doc_vectors":
            self.doc_index.save_index(str(index_path))
        else:
            self.chunk_index.save_index(str(index_path))

    def _load_metadata(self, name: str) -> dict:
        """加载元数据"""
        metadata_path = self.index_dir / f"{name}_metadata.json"
        with open(metadata_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _validate_embedding_fingerprint(self, name: str, metadata: dict[str, Any]) -> None:
        """校验索引元数据中的 Embedding 契约指纹。"""
        existing_fingerprint = metadata.get("embedding_fingerprint")
        if existing_fingerprint is None:
            logger.warning(
                "%s 缺少 Embedding 契约指纹，按旧索引兼容加载；"
                "如已切换 config/local.yaml 中的 Embedding 端点、模型或维度，"
                "请重建向量索引并重新生成 Embedding",
                name,
            )
            return

        expected = self.embedding_fingerprint
        normalized_existing = {
            key: str(existing_fingerprint.get(key, ""))
            for key in expected
        }
        if normalized_existing != expected:
            raise RuntimeError(
                "Embedding 索引契约不匹配: "
                f"name={name}, 已有={normalized_existing}, 当前={expected}。"
                "当前初始化不会自动重建索引。"
                "如果要继续使用现有索引，请切回原来的 Embedding 服务/模型/维度配置；"
                "如果确认切换模型或端点，请先重建向量索引并重新生成 Embedding。"
            )

    def _update_metadata(self, name: str, hnswlib_id: int, mapping: Tuple[int, int]):
        """更新元数据映射"""
        metadata = self._load_metadata(name)
        metadata["id_mapping"][str(hnswlib_id)] = mapping

        metadata_path = self.index_dir / f"{name}_metadata.json"
        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2)

    def _update_metadata_batch(self, name: str, mappings: dict[int, Tuple[int, int]]):
        """批量更新元数据映射。"""
        if not mappings:
            return

        metadata = self._load_metadata(name)
        for hnswlib_id, mapping in mappings.items():
            metadata["id_mapping"][str(hnswlib_id)] = mapping

        metadata_path = self.index_dir / f"{name}_metadata.json"
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

    def _chunk_vector_exists(self, hnswlib_id: int) -> bool:
        """校验 metadata 中的 chunk label 是否真实存在于索引。"""
        try:
            vectors = self.chunk_index.get_items([hnswlib_id])
        except RuntimeError:
            return False
        return vectors is not None and len(vectors) > 0

    def get_index_stats(self) -> dict:
        """
        获取索引统计信息

        Returns:
            统计信息字典
        """
        return {
            "doc_count": self.doc_index.get_current_count(),
            "chunk_count": self.chunk_index.get_current_count(),
            "dim": self.dim,
            "embedding_fingerprint": self.embedding_fingerprint,
            "M": self.M,
            "ef_search": self.ef_search,
        }
