"""
OpenAI API 客户端单元测试
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import pytest
import numpy as np
from unittest.mock import Mock, patch, MagicMock, call

from src.ai.openai_client import OpenAIClient


@pytest.fixture
def mock_config():
    """Mock 配置"""
    with patch('src.ai.openai_client.get_config') as mock:
        config = Mock()
        config.openai_api_key = "test-openai-key"
        config.openai_base_url = "https://api.openai.com/v1"
        config.openai_embedding_model = "text-embedding-3-small"
        config.embedding_dim = 1536
        config.embedding_dim_is_auto = False
        config.set_runtime_embedding_dim = Mock()
        mock.return_value = config
        yield config


@pytest.fixture
def client(mock_config):
    """创建测试客户端"""
    with patch('src.ai.openai_client.OpenAI'):
        return OpenAIClient()


class TestOpenAIClientInit:
    """测试客户端初始化"""

    def test_init_with_defaults(self, mock_config):
        """测试使用默认配置初始化"""
        with patch('src.ai.openai_client.OpenAI') as mock_openai:
            client = OpenAIClient()

            assert client.api_key == "test-openai-key"
            assert client.base_url == "https://api.openai.com/v1"
            assert client.model == "text-embedding-3-small"
            assert client.dimensions == 1536
            assert client.timeout == 30.0
            assert client.max_retries == 3

            # 检查 OpenAI 客户端是否正确初始化
            mock_openai.assert_called_once_with(
                api_key="test-openai-key",
                base_url="https://api.openai.com/v1",
                timeout=30.0,
                max_retries=3,
            )

    def test_init_with_custom_params(self, mock_config):
        """测试使用自定义参数初始化"""
        with patch('src.ai.openai_client.OpenAI'):
            client = OpenAIClient(
                api_key="custom-key",
                base_url="https://custom.api.com",
                model="text-embedding-3-large",
                dimensions=1024,
                timeout=60.0,
                max_retries=5,
            )

            assert client.api_key == "custom-key"
            assert client.base_url == "https://custom.api.com"
            assert client.model == "text-embedding-3-large"
            assert client.dimensions == 1024
            assert client.timeout == 60.0
            assert client.max_retries == 5

    def test_init_without_api_key(self):
        """测试没有 API Key 时抛出异常"""
        with patch('src.ai.openai_client.get_config') as mock:
            config = Mock()
            config.openai_api_key = None
            config.openai_base_url = "https://api.openai.com/v1"
            mock.return_value = config

            with pytest.raises(ValueError, match="OpenAI API Key 未配置"):
                OpenAIClient()


class TestOpenAIEmbed:
    """测试单个文本 Embedding"""

    def test_embed_success(self, client):
        """测试成功生成 Embedding"""
        # Mock API 响应
        mock_response = Mock()
        mock_response.data = [Mock(embedding=[0.1] * 1536)]
        mock_response.usage = Mock(prompt_tokens=10, total_tokens=10)

        client.client.embeddings.create = Mock(return_value=mock_response)

        embedding = client.embed("Hello, world!")

        assert len(embedding) == 1536
        assert all(isinstance(x, float) for x in embedding)
        client.client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-small",
            input="Hello, world!",
            dimensions=1536,
        )

    def test_embed_rejects_dimension_mismatch(self, client):
        """测试返回维度与配置不一致时抛出异常。"""
        mock_response = Mock()
        mock_response.data = [Mock(embedding=[0.1] * 2560)]
        mock_response.usage = Mock(prompt_tokens=10, total_tokens=10)

        client.client.embeddings.create = Mock(return_value=mock_response)

        with pytest.raises(ValueError, match="Embedding 维度不匹配"):
            client.embed("dimension mismatch")

    def test_embed_retries_without_dimensions_when_backend_rejects_it(self, client):
        """测试后端不支持 dimensions 参数时回退重试。"""
        from openai import OpenAIError

        mock_response = Mock()
        mock_response.data = [Mock(embedding=[0.1] * 1536)]
        mock_response.usage = Mock(prompt_tokens=10, total_tokens=10)
        client.client.embeddings.create = Mock(
            side_effect=[
                OpenAIError("unknown parameter: dimensions"),
                mock_response,
            ]
        )

        embedding = client.embed("fallback text")

        assert len(embedding) == 1536
        assert client.client.embeddings.create.call_args_list == [
            call(
                model="text-embedding-3-small",
                input="fallback text",
                dimensions=1536,
            ),
            call(
                model="text-embedding-3-small",
                input="fallback text",
            ),
        ]

    def test_embed_auto_detects_dimension_on_first_success(self, mock_config):
        """测试 auto 模式会锁定首次成功返回的真实维度。"""
        mock_config.embedding_dim = None
        mock_config.embedding_dim_is_auto = True

        with patch('src.ai.openai_client.OpenAI'):
            client = OpenAIClient()

        mock_response = Mock()
        mock_response.data = [Mock(embedding=[0.1] * 2560)]
        mock_response.usage = Mock(prompt_tokens=10, total_tokens=10)
        client.client.embeddings.create = Mock(return_value=mock_response)

        embedding = client.embed("auto dimension text")

        assert len(embedding) == 2560
        assert client.dimensions == 2560
        client.client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-small",
            input="auto dimension text",
        )
        mock_config.set_runtime_embedding_dim.assert_called_once_with(2560)

    def test_embed_uses_persisted_auto_dimension_after_restart(self, mock_config):
        """测试 auto 模式在已持久化维度后会直接复用该维度。"""
        mock_config.embedding_dim = 2560
        mock_config.embedding_dim_is_auto = True

        with patch('src.ai.openai_client.OpenAI'):
            client = OpenAIClient()

        mock_response = Mock()
        mock_response.data = [Mock(embedding=[0.1] * 2560)]
        mock_response.usage = Mock(prompt_tokens=10, total_tokens=10)
        client.client.embeddings.create = Mock(return_value=mock_response)

        embedding = client.embed("persisted auto dimension text")

        assert len(embedding) == 2560
        assert client.dimensions == 2560
        client.client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-small",
            input="persisted auto dimension text",
            dimensions=2560,
        )
        mock_config.set_runtime_embedding_dim.assert_not_called()

    def test_embed_empty_text(self, client):
        """测试空文本时抛出异常"""
        with pytest.raises(ValueError, match="Embedding 文本不能为空"):
            client.embed("")

        with pytest.raises(ValueError, match="Embedding 文本不能为空"):
            client.embed("   ")

    def test_embed_rate_limit_error(self, client):
        """测试 API 限流错误"""
        from openai import RateLimitError

        client.client.embeddings.create = Mock(
            side_effect=RateLimitError(
                "Rate limit exceeded",
                response=Mock(status_code=429),
                body=None
            )
        )

        with pytest.raises(Exception, match="OpenAI API 限流"):
            client.embed("text")

    def test_embed_timeout_error(self, client):
        """测试 API 超时错误"""
        from openai import APITimeoutError

        client.client.embeddings.create = Mock(
            side_effect=APITimeoutError("Request timeout")
        )

        with pytest.raises(Exception, match="OpenAI API 请求超时"):
            client.embed("text")

    def test_embed_generic_error(self, client):
        """测试通用 OpenAI 错误"""
        from openai import OpenAIError

        client.client.embeddings.create = Mock(
            side_effect=OpenAIError("Unknown error")
        )

        with pytest.raises(Exception, match="OpenAI API 调用失败"):
            client.embed("text")


class TestOpenAIEmbedBatch:
    """测试批量 Embedding"""

    def test_embed_batch_success(self, client):
        """测试成功批量生成 Embedding"""
        texts = ["text1", "text2", "text3"]

        # Mock API 响应
        mock_response = Mock()
        mock_response.data = [
            Mock(embedding=[0.1] * 1536),
            Mock(embedding=[0.2] * 1536),
            Mock(embedding=[0.3] * 1536),
        ]
        mock_response.usage = Mock(prompt_tokens=30, total_tokens=30)

        client.client.embeddings.create = Mock(return_value=mock_response)

        embeddings = client.embed_batch(texts)

        assert len(embeddings) == 3
        assert all(len(emb) == 1536 for emb in embeddings)
        client.client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-small",
            input=texts,
            dimensions=1536,
        )

    def test_embed_batch_empty_list(self, client):
        """测试空列表时抛出异常"""
        with pytest.raises(ValueError, match="Embedding 文本列表不能为空"):
            client.embed_batch([])

    def test_embed_batch_filter_empty_texts(self, client):
        """测试过滤空文本"""
        texts = ["text1", "", "  ", "text2"]

        mock_response = Mock()
        mock_response.data = [
            Mock(embedding=[0.1] * 1536),
            Mock(embedding=[0.2] * 1536),
        ]
        mock_response.usage = Mock(prompt_tokens=20, total_tokens=20)

        client.client.embeddings.create = Mock(return_value=mock_response)

        embeddings = client.embed_batch(texts)

        # 应该只处理有效文本
        assert len(embeddings) == 2

        # 检查传递给 API 的文本
        call_args = client.client.embeddings.create.call_args
        assert call_args[1]['input'] == ["text1", "text2"]
        assert call_args[1]['dimensions'] == 1536

    def test_embed_batch_with_batching(self, client):
        """测试分批处理"""
        # 创建 250 个文本（超过默认 batch_size=100）
        texts = [f"text{i}" for i in range(250)]

        # Mock API 响应
        def create_mock_response(batch_size):
            mock_response = Mock()
            mock_response.data = [Mock(embedding=[0.1] * 1536) for _ in range(batch_size)]
            mock_response.usage = Mock(prompt_tokens=batch_size * 10, total_tokens=batch_size * 10)
            return mock_response

        client.client.embeddings.create = Mock(
            side_effect=[
                create_mock_response(100),
                create_mock_response(100),
                create_mock_response(50),
            ]
        )

        embeddings = client.embed_batch(texts, batch_size=100)

        # 应该调用 3 次（100 + 100 + 50）
        assert client.client.embeddings.create.call_count == 3
        assert len(embeddings) == 250

    def test_embed_batch_rejects_dimension_mismatch(self, client):
        """测试批量返回错维度时抛出异常。"""
        texts = ["text1", "text2"]

        mock_response = Mock()
        mock_response.data = [
            Mock(embedding=[0.1] * 1536),
            Mock(embedding=[0.2] * 2560),
        ]
        mock_response.usage = Mock(prompt_tokens=20, total_tokens=20)
        client.client.embeddings.create = Mock(return_value=mock_response)

        with pytest.raises(ValueError, match="Embedding 维度不匹配"):
            client.embed_batch(texts)

    def test_embed_batch_retries_without_dimensions_when_backend_rejects_it(
        self, client
    ):
        """测试批量请求在后端不支持 dimensions 时回退重试。"""
        from openai import OpenAIError

        texts = ["text1", "text2"]
        mock_response = Mock()
        mock_response.data = [
            Mock(embedding=[0.1] * 1536),
            Mock(embedding=[0.2] * 1536),
        ]
        mock_response.usage = Mock(prompt_tokens=20, total_tokens=20)
        client.client.embeddings.create = Mock(
            side_effect=[
                OpenAIError("dimensions is not supported"),
                mock_response,
            ]
        )

        embeddings = client.embed_batch(texts)

        assert len(embeddings) == 2
        assert client.client.embeddings.create.call_args_list == [
            call(
                model="text-embedding-3-small",
                input=texts,
                dimensions=1536,
            ),
            call(
                model="text-embedding-3-small",
                input=texts,
            ),
        ]

    def test_embed_batch_auto_detects_dimension_on_first_success(self, mock_config):
        """测试批量请求在 auto 模式下锁定首次成功返回的真实维度。"""
        mock_config.embedding_dim = None
        mock_config.embedding_dim_is_auto = True

        with patch('src.ai.openai_client.OpenAI'):
            client = OpenAIClient()

        texts = ["text1", "text2"]
        mock_response = Mock()
        mock_response.data = [
            Mock(embedding=[0.1] * 2560),
            Mock(embedding=[0.2] * 2560),
        ]
        mock_response.usage = Mock(prompt_tokens=20, total_tokens=20)
        client.client.embeddings.create = Mock(return_value=mock_response)

        embeddings = client.embed_batch(texts)

        assert len(embeddings) == 2
        assert client.dimensions == 2560
        client.client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-small",
            input=texts,
        )
        mock_config.set_runtime_embedding_dim.assert_called_once_with(2560)


class TestOpenAIEmbedNumpy:
    """测试 numpy 格式的 Embedding"""

    def test_embed_numpy_success(self, client):
        """测试 numpy 格式 Embedding"""
        mock_response = Mock()
        mock_response.data = [Mock(embedding=[0.1] * 1536)]
        mock_response.usage = Mock(prompt_tokens=10, total_tokens=10)

        client.client.embeddings.create = Mock(return_value=mock_response)

        embedding = client.embed_numpy("text")

        assert isinstance(embedding, np.ndarray)
        assert embedding.shape == (1536,)
        assert embedding.dtype == np.float32

    def test_embed_batch_numpy_success(self, client):
        """测试批量 numpy 格式 Embedding"""
        texts = ["text1", "text2", "text3"]

        mock_response = Mock()
        mock_response.data = [
            Mock(embedding=[0.1] * 1536),
            Mock(embedding=[0.2] * 1536),
            Mock(embedding=[0.3] * 1536),
        ]
        mock_response.usage = Mock(prompt_tokens=30, total_tokens=30)

        client.client.embeddings.create = Mock(return_value=mock_response)

        embeddings = client.embed_batch_numpy(texts)

        assert isinstance(embeddings, np.ndarray)
        assert embeddings.shape == (3, 1536)
        assert embeddings.dtype == np.float32

    def test_embed_numpy_values(self, client):
        """测试 numpy 数组的值"""
        mock_embedding = [0.5, 0.3, 0.7] + [0.0] * 1533

        mock_response = Mock()
        mock_response.data = [Mock(embedding=mock_embedding)]
        mock_response.usage = Mock(prompt_tokens=10, total_tokens=10)

        client.client.embeddings.create = Mock(return_value=mock_response)

        embedding = client.embed_numpy("text")

        assert embedding[0] == 0.5
        assert embedding[1] == 0.3
        assert embedding[2] == 0.7
        assert np.allclose(embedding[3:], 0.0)
