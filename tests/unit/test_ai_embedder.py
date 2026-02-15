"""
Embedder 统一向量化接口单元测试
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import pytest
import numpy as np
from unittest.mock import Mock, patch

from src.ai.embedder import Embedder


@pytest.fixture
def mock_openai_client():
    """Mock OpenAI 客户端"""
    mock_client = Mock()

    # Mock embed 方法
    mock_client.embed.return_value = [0.1] * 1536
    mock_client.embed_numpy.return_value = np.array([0.1] * 1536, dtype=np.float32)

    # Mock embed_batch 方法
    mock_client.embed_batch.return_value = [[0.1] * 1536, [0.2] * 1536, [0.3] * 1536]
    mock_client.embed_batch_numpy.return_value = np.array(
        [[0.1] * 1536, [0.2] * 1536, [0.3] * 1536], dtype=np.float32
    )

    return mock_client


@pytest.fixture
def embedder(mock_openai_client):
    """创建测试 Embedder"""
    return Embedder(openai_client=mock_openai_client, chunk_size=500, chunk_overlap=50)


class TestEmbedderInit:
    """测试 Embedder 初始化"""

    def test_init_with_custom_client(self, mock_openai_client):
        """测试使用自定义客户端初始化"""
        embedder = Embedder(
            openai_client=mock_openai_client,
            chunk_size=1000,
            chunk_overlap=100,
        )

        assert embedder.client == mock_openai_client
        assert embedder.chunk_size == 1000
        assert embedder.chunk_overlap == 100

    def test_init_with_defaults(self):
        """测试使用默认参数初始化"""
        with patch('src.ai.embedder.OpenAIClient'):
            embedder = Embedder()

            assert embedder.chunk_size == 500
            assert embedder.chunk_overlap == 50


class TestEmbedDocument:
    """测试文档级 Embedding"""

    def test_embed_document_short_text(self, embedder, mock_openai_client):
        """测试短文本（直接向量化）"""
        text = "这是一个短文本"

        vector = embedder.embed_document(text)

        assert isinstance(vector, np.ndarray)
        assert vector.shape == (1536,)
        mock_openai_client.embed_numpy.assert_called_once_with(text)

    def test_embed_document_long_text(self, embedder, mock_openai_client):
        """测试长文本（分块后平均）"""
        # 创建超过 8000 字符的长文本
        long_text = "这是一个很长的文本。" * 1000  # 约 9000 字符

        with patch.object(embedder, '_embed_long_document') as mock_embed_long:
            mock_embed_long.return_value = np.array([0.5] * 1536, dtype=np.float32)

            vector = embedder.embed_document(long_text)

            assert isinstance(vector, np.ndarray)
            assert vector.shape == (1536,)
            mock_embed_long.assert_called_once()

    def test_embed_document_empty_text(self, embedder):
        """测试空文本时抛出异常"""
        with pytest.raises(ValueError, match="文档文本不能为空"):
            embedder.embed_document("")

        with pytest.raises(ValueError, match="文档文本不能为空"):
            embedder.embed_document("   ")


class TestEmbedLongDocument:
    """测试长文档分块向量化"""

    def test_embed_long_document(self, embedder, mock_openai_client):
        """测试长文档分块后取平均"""
        long_text = "a" * 2000  # 创建长文本

        # Mock 分块和批量向量化
        with patch('src.ai.embedder.split_text_into_chunks') as mock_split:
            mock_split.return_value = ["chunk1", "chunk2", "chunk3"]

            # Mock embed_batch_numpy 返回 3 个向量
            mock_openai_client.embed_batch_numpy.return_value = np.array(
                [[1.0] * 1536, [2.0] * 1536, [3.0] * 1536], dtype=np.float32
            )

            vector = embedder._embed_long_document(long_text)

            # 检查分块调用
            mock_split.assert_called_once_with(long_text, chunk_size=500, chunk_overlap=50)

            # 检查批量向量化调用
            mock_openai_client.embed_batch_numpy.assert_called_once_with(
                ["chunk1", "chunk2", "chunk3"]
            )

            # 检查平均向量（应该是 [2.0, 2.0, ...]）
            assert isinstance(vector, np.ndarray)
            assert vector.shape == (1536,)
            assert np.allclose(vector, 2.0)


class TestEmbedChunks:
    """测试分块级 Embedding"""

    def test_embed_chunks_without_return(self, embedder, mock_openai_client):
        """测试不返回分块文本"""
        text = "这是一个需要分块的文本"

        with patch('src.ai.embedder.split_text_into_chunks') as mock_split:
            mock_split.return_value = ["chunk1", "chunk2"]

            mock_openai_client.embed_batch_numpy.return_value = np.array(
                [[0.1] * 1536, [0.2] * 1536], dtype=np.float32
            )

            vectors, chunks = embedder.embed_chunks(text, return_chunks=False)

            assert isinstance(vectors, np.ndarray)
            assert vectors.shape == (2, 1536)
            assert chunks is None

    def test_embed_chunks_with_return(self, embedder, mock_openai_client):
        """测试返回分块文本"""
        text = "这是一个需要分块的文本"

        with patch('src.ai.embedder.split_text_into_chunks') as mock_split:
            mock_split.return_value = ["chunk1", "chunk2", "chunk3"]

            mock_openai_client.embed_batch_numpy.return_value = np.array(
                [[0.1] * 1536, [0.2] * 1536, [0.3] * 1536], dtype=np.float32
            )

            vectors, chunks = embedder.embed_chunks(text, return_chunks=True)

            assert isinstance(vectors, np.ndarray)
            assert vectors.shape == (3, 1536)
            assert chunks == ["chunk1", "chunk2", "chunk3"]

    def test_embed_chunks_empty_text(self, embedder):
        """测试空文本时抛出异常"""
        with pytest.raises(ValueError, match="文档文本不能为空"):
            embedder.embed_chunks("")


class TestEmbedBatchDocuments:
    """测试批量文档级 Embedding"""

    def test_embed_batch_documents_success(self, embedder, mock_openai_client):
        """测试成功批量向量化"""
        texts = ["doc1", "doc2", "doc3"]

        mock_openai_client.embed_batch_numpy.return_value = np.array(
            [[0.1] * 1536, [0.2] * 1536, [0.3] * 1536], dtype=np.float32
        )

        vectors = embedder.embed_batch_documents(texts)

        assert isinstance(vectors, np.ndarray)
        assert vectors.shape == (3, 1536)
        mock_openai_client.embed_batch_numpy.assert_called_once()

    def test_embed_batch_documents_empty_list(self, embedder):
        """测试空列表时抛出异常"""
        with pytest.raises(ValueError, match="文档文本列表不能为空"):
            embedder.embed_batch_documents([])

    def test_embed_batch_documents_filter_empty(self, embedder, mock_openai_client):
        """测试过滤空文本"""
        texts = ["doc1", "", "   ", "doc2"]

        mock_openai_client.embed_batch_numpy.return_value = np.array(
            [[0.1] * 1536, [0.2] * 1536], dtype=np.float32
        )

        vectors = embedder.embed_batch_documents(texts)

        # 应该只处理有效文本
        assert vectors.shape == (2, 1536)

    def test_embed_batch_documents_truncate_long(self, embedder, mock_openai_client):
        """测试截断过长文本"""
        long_text = "a" * 9000
        texts = ["short", long_text]

        mock_openai_client.embed_batch_numpy.return_value = np.array(
            [[0.1] * 1536, [0.2] * 1536], dtype=np.float32
        )

        embedder.embed_batch_documents(texts)

        # 检查传递给 API 的文本（长文本应该被截断）
        call_args = mock_openai_client.embed_batch_numpy.call_args
        processed_texts = call_args[0][0]

        assert processed_texts[0] == "short"
        assert len(processed_texts[1]) == 8000  # 截断到 8000


class TestCosineSimilarity:
    """测试余弦相似度计算"""

    def test_cosine_similarity_identical_vectors(self, embedder):
        """测试相同向量的余弦相似度"""
        v1 = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        v2 = np.array([1.0, 2.0, 3.0], dtype=np.float32)

        similarity = embedder.cosine_similarity(v1, v2)

        assert np.isclose(similarity, 1.0)

    def test_cosine_similarity_orthogonal_vectors(self, embedder):
        """测试正交向量的余弦相似度"""
        v1 = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        v2 = np.array([0.0, 1.0, 0.0], dtype=np.float32)

        similarity = embedder.cosine_similarity(v1, v2)

        assert np.isclose(similarity, 0.0)

    def test_cosine_similarity_opposite_vectors(self, embedder):
        """测试相反向量的余弦相似度"""
        v1 = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        v2 = np.array([-1.0, -2.0, -3.0], dtype=np.float32)

        similarity = embedder.cosine_similarity(v1, v2)

        assert np.isclose(similarity, -1.0)


class TestBatchCosineSimilarity:
    """测试批量余弦相似度计算"""

    def test_batch_cosine_similarity(self, embedder):
        """测试批量余弦相似度"""
        query = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        vectors = np.array(
            [
                [1.0, 0.0, 0.0],  # 相同方向
                [0.0, 1.0, 0.0],  # 正交
                [-1.0, 0.0, 0.0], # 相反方向
            ],
            dtype=np.float32,
        )

        similarities = embedder.batch_cosine_similarity(query, vectors)

        assert similarities.shape == (3,)
        assert np.isclose(similarities[0], 1.0)
        assert np.isclose(similarities[1], 0.0)
        assert np.isclose(similarities[2], -1.0)

    def test_batch_cosine_similarity_1536_dim(self, embedder):
        """测试 1536 维向量的批量余弦相似度"""
        query = np.random.rand(1536).astype(np.float32)
        vectors = np.random.rand(10, 1536).astype(np.float32)

        similarities = embedder.batch_cosine_similarity(query, vectors)

        assert similarities.shape == (10,)
        assert all(-1.0 <= s <= 1.0 for s in similarities)
