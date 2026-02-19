"""
统一向量化接口

提供文档级和分块级的向量化功能
"""

from typing import List, Tuple, Optional
import numpy as np

from src.ai.openai_client import OpenAIClient
from src.utils.text_utils import split_text_into_chunks
from src.utils.logger import get_logger

logger = get_logger(__name__)


class Embedder:
    """统一向量化接口"""

    def __init__(
        self,
        openai_client: Optional[OpenAIClient] = None,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
    ):
        """
        初始化 Embedder

        Args:
            openai_client: OpenAI 客户端，默认创建新实例
            chunk_size: 分块大小（字符数）
            chunk_overlap: 分块重叠大小（字符数）
        """
        self.client = openai_client or OpenAIClient()
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

        logger.info(
            f"Embedder 初始化成功: chunk_size={chunk_size}, chunk_overlap={chunk_overlap}"
        )

    def embed_document(self, text: str) -> np.ndarray:
        """
        生成文档级 Embedding（整篇文档的向量表示）

        Args:
            text: 文档文本

        Returns:
            文档向量（shape=(dim,)）

        Raises:
            ValueError: 文本为空
            Exception: 向量化失败

        Example:
            >>> embedder = Embedder()
            >>> doc_vector = embedder.embed_document("This is a document.")
            >>> doc_vector.ndim == 1  # 一维向量，维度由模型决定
        """
        if not text or not text.strip():
            raise ValueError("文档文本不能为空")

        logger.info(f"生成文档级 Embedding: text_length={len(text)}")

        # 如果文本过长，截断或分块后平均
        if len(text) > 8000:  # OpenAI Embedding 上限约 8191 tokens
            logger.warning(f"文档过长 ({len(text)} 字符)，将分块后取平均向量")
            return self._embed_long_document(text)

        # 直接向量化
        vector = self.client.embed_numpy(text)
        logger.info("文档级 Embedding 完成")
        return vector

    def _embed_long_document(self, text: str) -> np.ndarray:
        """
        对长文档进行分块向量化，然后取平均

        Args:
            text: 长文档文本

        Returns:
            平均向量（shape=(dim,)）
        """
        # 分块
        chunks = split_text_into_chunks(
            text,
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
        )

        logger.info(f"长文档分块: chunks={len(chunks)}")

        # 批量向量化
        chunk_vectors = self.client.embed_batch_numpy(chunks)

        # 取平均
        avg_vector = np.mean(chunk_vectors, axis=0)

        logger.info("长文档 Embedding 完成（平均向量）")
        return avg_vector

    def embed_chunks(
        self, text: str, return_chunks: bool = False
    ) -> Tuple[np.ndarray, Optional[List[str]]]:
        """
        生成分块级 Embedding（每个分块的向量表示）

        Args:
            text: 文档文本
            return_chunks: 是否返回分块文本

        Returns:
            (分块向量矩阵, 分块文本列表)
            - 分块向量矩阵: shape=(num_chunks, dim)
            - 分块文本列表: 如果 return_chunks=True 则返回，否则返回 None

        Raises:
            ValueError: 文本为空
            Exception: 向量化失败

        Example:
            >>> embedder = Embedder()
            >>> vectors, chunks = embedder.embed_chunks("Long text...", return_chunks=True)
            >>> vectors.ndim == 2  # (num_chunks, dim)
            >>> assert len(chunks) == vectors.shape[0]
        """
        if not text or not text.strip():
            raise ValueError("文档文本不能为空")

        logger.info(f"生成分块级 Embedding: text_length={len(text)}")

        # 分块
        chunks = split_text_into_chunks(
            text,
            chunk_size=self.chunk_size,
            chunk_overlap=self.chunk_overlap,
        )

        logger.info(f"文档分块: chunks={len(chunks)}")

        # 批量向量化
        chunk_vectors = self.client.embed_batch_numpy(chunks)

        logger.info(f"分块级 Embedding 完成: vectors_shape={chunk_vectors.shape}")

        if return_chunks:
            return chunk_vectors, chunks
        else:
            return chunk_vectors, None

    def embed_batch_documents(
        self, texts: List[str]
    ) -> np.ndarray:
        """
        批量生成文档级 Embedding

        Args:
            texts: 文档文本列表

        Returns:
            文档向量矩阵（shape=(num_docs, dim)）

        Raises:
            ValueError: 文本列表为空
            Exception: 向量化失败

        Example:
            >>> embedder = Embedder()
            >>> vectors = embedder.embed_batch_documents(["doc1", "doc2", "doc3"])
            >>> vectors.shape[0] == 3  # num_docs
        """
        if not texts:
            raise ValueError("文档文本列表不能为空")

        logger.info(f"批量生成文档级 Embedding: num_docs={len(texts)}")

        # 过滤空文本
        valid_texts = [text for text in texts if text and text.strip()]

        # 处理长文档：使用分块取平均策略（与 embed_document 保持一致）
        doc_vectors = []
        for text in valid_texts:
            if len(text) > 8000:
                logger.warning(f"文档过长 ({len(text)} 字符)，将分块后取平均向量")
                vector = self._embed_long_document(text)
            else:
                vector = self.client.embed_numpy(text)
            doc_vectors.append(vector)

        # 转换为 numpy 数组
        vectors = np.vstack(doc_vectors)

        logger.info(f"批量文档级 Embedding 完成: vectors_shape={vectors.shape}")
        return vectors

    def cosine_similarity(
        self, vector1: np.ndarray, vector2: np.ndarray
    ) -> float:
        """
        计算两个向量的余弦相似度

        Args:
            vector1: 向量 1
            vector2: 向量 2

        Returns:
            余弦相似度 (-1 到 1)

        Example:
            >>> embedder = Embedder()
            >>> v1 = embedder.embed_document("Hello")
            >>> v2 = embedder.embed_document("Hi")
            >>> similarity = embedder.cosine_similarity(v1, v2)
            >>> assert -1.0 <= similarity <= 1.0
        """
        # 归一化
        v1_norm = vector1 / np.linalg.norm(vector1)
        v2_norm = vector2 / np.linalg.norm(vector2)

        # 计算余弦相似度
        similarity = np.dot(v1_norm, v2_norm)

        return float(similarity)

    def batch_cosine_similarity(
        self, query_vector: np.ndarray, vectors: np.ndarray
    ) -> np.ndarray:
        """
        批量计算查询向量与向量集的余弦相似度

        Args:
            query_vector: 查询向量（shape=(dim,)）
            vectors: 向量矩阵（shape=(n, dim)）

        Returns:
            相似度数组（shape=(n,)）

        Example:
            >>> embedder = Embedder()
            >>> query = embedder.embed_document("query")
            >>> docs = embedder.embed_batch_documents(["doc1", "doc2", "doc3"])
            >>> similarities = embedder.batch_cosine_similarity(query, docs)
            >>> assert similarities.shape == (3,)
        """
        # 归一化查询向量
        query_norm = query_vector / np.linalg.norm(query_vector)

        # 归一化文档向量矩阵
        vectors_norm = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

        # 批量计算余弦相似度
        similarities = np.dot(vectors_norm, query_norm)

        return similarities
