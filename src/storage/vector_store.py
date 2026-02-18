"""
向量存储层

基于 hnswlib 的向量索引管理
"""

import json
import hnswlib
import numpy as np
from pathlib import Path
from typing import List, Tuple, Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)


class VectorStore:
    """hnswlib 向量索引管理器"""

    def __init__(self, index_dir: Path, dim: int = 1536):
        """
        初始化向量索引

        Args:
            index_dir: 向量索引目录
            dim: 向量维度 (默认 1536，对应 text-embedding-3-small)
        """
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.dim = dim

        # HNSW 参数
        self.M = 16  # 每个节点的连接数
        self.ef_construction = 200  # 构建时搜索深度
        self.ef_search = 50  # 查询时搜索深度

        # 初始化文档级和分块级索引
        self.doc_index = self._init_index("doc_vectors")
        self.chunk_index = self._init_index("chunk_vectors")

        logger.info(f"向量存储初始化完成: {self.index_dir}")

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

        # 创建索引对象
        index = hnswlib.Index(space='cosine', dim=self.dim)

        if index_path.exists():
            # 加载已有索引
            index.load_index(str(index_path))
            logger.info(f"✅ 加载已有索引: {index_path}")
        else:
            # 初始化新索引
            index.init_index(
                max_elements=10000,  # 初始容量，可自动扩展
                ef_construction=self.ef_construction,
                M=self.M
            )
            # 保存空索引
            index.save_index(str(index_path))

            # 创建元数据文件
            metadata = {
                "dim": self.dim,
                "space": "cosine",
                "M": self.M,
                "ef_construction": self.ef_construction,
                "id_mapping": {}
            }
            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2)

            logger.info(f"✅ 创建新索引: {index_path}")

        # 设置查询时的搜索深度
        index.set_ef(self.ef_search)

        return index

    def add_doc_vector(self, knowledge_id: int, vector: np.ndarray):
        """
        添加文档级向量

        Args:
            knowledge_id: 知识条目 ID (对应 knowledge_items.id)
            vector: 向量 (1536 维)
        """
        # 确保向量是 float32 类型
        if vector.dtype != np.float32:
            vector = vector.astype('float32')

        # 添加向量 (使用 knowledge_id 作为 hnswlib 的标签)
        self.doc_index.add_items(vector.reshape(1, -1), ids=[knowledge_id])

        # 保存索引
        self._save_index("doc_vectors")

        logger.info(f"添加文档向量: knowledge_id={knowledge_id}")

    def add_chunk_vector(self, knowledge_id: int, chunk_index: int, vector: np.ndarray):
        """
        添加分块级向量

        Args:
            knowledge_id: 知识条目 ID
            chunk_index: 块序号
            vector: 向量 (1536 维)
        """
        # 确保向量是 float32 类型
        if vector.dtype != np.float32:
            vector = vector.astype('float32')

        # 生成唯一 ID: knowledge_id * 10000 + chunk_index
        hnswlib_id = knowledge_id * 10000 + chunk_index

        # 添加向量
        self.chunk_index.add_items(vector.reshape(1, -1), ids=[hnswlib_id])

        # 保存映射关系
        self._update_metadata("chunk_vectors", hnswlib_id, (knowledge_id, chunk_index))

        # 保存索引
        self._save_index("chunk_vectors")

        logger.info(f"添加分块向量: knowledge_id={knowledge_id}, chunk_index={chunk_index}")

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

        labels, distances = self.doc_index.knn_query(query_vector.reshape(1, -1), k=k)
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

        labels, distances = self.chunk_index.knn_query(query_vector.reshape(1, -1), k=k)

        # 从元数据中解析 (knowledge_id, chunk_index)
        results = []
        for label, dist in zip(labels[0], distances[0]):
            hnswlib_id = int(label)
            knowledge_id = hnswlib_id // 10000
            chunk_index = hnswlib_id % 10000
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

    def _update_metadata(self, name: str, hnswlib_id: int, mapping: Tuple[int, int]):
        """更新元数据映射"""
        metadata = self._load_metadata(name)
        metadata["id_mapping"][str(hnswlib_id)] = mapping

        metadata_path = self.index_dir / f"{name}_metadata.json"
        with open(metadata_path, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2)

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
            "M": self.M,
            "ef_search": self.ef_search,
        }
