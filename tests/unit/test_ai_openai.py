"""
OpenAI API 客户端单元测试
"""

import logging
from unittest.mock import Mock, patch, call

import httpx
import numpy as np
import pytest
from openai import DefaultHttpxClient as SDKDefaultHttpxClient

from src.ai.openai_client import OpenAIClient
from src.runtime.errors import ErrorCode, PKVRuntimeError


def _embedding_item(vector, *, index: int = 0):
    return Mock(index=index, embedding=vector)


def _assert_embedding_protocol_failure(action) -> PKVRuntimeError:
    with pytest.raises(PKVRuntimeError) as captured:
        action()
    error = captured.value
    assert error.code is ErrorCode.PROVIDER_PROTOCOL_FAILED
    assert error.stage == "embedding_protocol"
    assert error.recoverable is True
    assert str(error) == "Embedding Provider 响应非法"
    return error


@pytest.fixture
def mock_config():
    """Mock 配置"""
    with patch("src.ai.openai_client.get_config") as mock:
        config = Mock()
        config.embd_provider = "openai_compatible"
        config.embd_api_key = "test-openai-key"
        config.embd_base_url = "https://api.openai.com/v1"
        config.embd_model = "text-embedding-3-small"
        config.embd_timeout_seconds = 30.0
        config.embd_max_retries = 3
        config.embedding_dim = 1536
        config.embedding_dim_is_auto = False
        config.set_runtime_embedding_dim = Mock()

        config_attributes = {
            "ai.embedding.provider": "embd_provider",
            "ai.embedding.api_key": "embd_api_key",
            "ai.embedding.base_url": "embd_base_url",
            "ai.embedding.model": "embd_model",
            "ai.embedding.dim": "embedding_dim",
            "ai.embedding.timeout_seconds": "embd_timeout_seconds",
            "ai.embedding.max_retries": "embd_max_retries",
        }

        def get_config_value(path, default=None):
            attribute = config_attributes.get(path)
            return getattr(config, attribute) if attribute else default

        config.get.side_effect = get_config_value
        mock.return_value = config
        yield config


@pytest.fixture
def client(mock_config):
    """创建测试客户端"""
    with patch("src.ai.openai_client.OpenAI"):
        return OpenAIClient()


class TestOpenAIClientInit:
    """测试客户端初始化"""

    def test_init_with_defaults(self, mock_config):
        """测试使用默认配置初始化"""
        with patch("src.ai.openai_client.OpenAI") as mock_openai:
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
        with patch("src.ai.openai_client.OpenAI"):
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

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("embd_api_key", ""),
            ("embd_model", ""),
            ("embd_timeout_seconds", 0),
            ("embd_timeout_seconds", True),
            ("embd_max_retries", -1),
            ("embd_max_retries", True),
            ("embedding_dim", 0),
            ("embedding_dim", True),
        ],
    )
    def test_invalid_config_snapshot_fails_before_sdk_construction(
        self, mock_config, field, value
    ):
        setattr(mock_config, field, value)

        with (
            patch("src.ai.openai_client.OpenAI") as sdk_client,
            patch("src.ai.openai_client.DefaultHttpxClient") as http_client,
        ):
            with pytest.raises(PKVRuntimeError) as captured:
                OpenAIClient()

        assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID
        sdk_client.assert_not_called()
        http_client.assert_not_called()

    @pytest.mark.parametrize(
        "override",
        [
            {"api_key": ""},
            {"model": ""},
            {"timeout": 0},
            {"max_retries": True},
            {"dimensions": 0},
        ],
    )
    def test_invalid_explicit_override_is_revalidated_before_sdk_construction(
        self, mock_config, override
    ):
        with patch("src.ai.openai_client.OpenAI") as sdk_client:
            with pytest.raises(PKVRuntimeError) as captured:
                OpenAIClient(**override)

        assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID
        sdk_client.assert_not_called()

    def test_init_log_exposes_only_fixed_provider_metadata(self, mock_config, caplog):
        """初始化日志不得写入 URL、model 或 API key。"""
        sentinel = "openai-init-log-secret"
        endpoint = (
            f"https://embd.example/v1;token={sentinel}"
            f"?api_key={sentinel}#fragment={sentinel}"
        )

        with (
            patch("src.ai.openai_client.OpenAI"),
            patch("src.ai.openai_client.DefaultHttpxClient"),
            caplog.at_level("INFO", logger="src.ai.openai_client"),
        ):
            OpenAIClient(
                api_key=f"key-{sentinel}",
                base_url=endpoint,
                model=f"model-{sentinel}",
            )

        assert "component=embedding status=ready dimensions=1536" in caplog.text
        assert "embd.example" not in caplog.text
        assert sentinel not in caplog.text

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://provider.example/v1",
            "http://localhost:43123/v1",
            "https://user:password@provider.example/v1",
            "https://provider.example/v1\r\nX-Api-Key: url-secret",
            "ftp://provider.example/v1",
            "not-a-provider-url",
        ],
    )
    def test_invalid_endpoint_fails_before_sdk_client_construction(
        self, mock_config, caplog, endpoint
    ):
        sentinel = "direct-embedding-key-secret"

        with (
            patch("src.ai.openai_client.OpenAI") as sdk_client,
            patch("src.ai.openai_client.DefaultHttpxClient") as http_client,
            caplog.at_level("DEBUG"),
        ):
            with pytest.raises(PKVRuntimeError) as captured:
                OpenAIClient(api_key=sentinel, base_url=endpoint)

        assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID
        sdk_client.assert_not_called()
        http_client.assert_not_called()
        assert sentinel not in str(captured.value)
        assert sentinel not in caplog.text
        assert "url-secret" not in caplog.text

    def test_numeric_loopback_http_remains_supported(self, mock_config):
        with patch("src.ai.openai_client.OpenAI") as sdk_client:
            client = OpenAIClient(base_url="http://127.0.0.1:43123/v1")

        assert client.base_url == "http://127.0.0.1:43123/v1"
        assert sdk_client.call_args.kwargs["base_url"] == ("http://127.0.0.1:43123/v1")

    @pytest.mark.parametrize(
        "provider",
        ["fake", "unknown-provider-secret", "OPENAI_COMPATIBLE", True, None],
    )
    def test_unknown_provider_fails_before_url_or_sdk_construction(
        self, mock_config, caplog, provider
    ):
        mock_config.embd_provider = provider
        sentinel = "explicit-embedding-key-secret"

        with (
            patch("src.ai.provider_factory.validate_provider_base_url") as validate_url,
            patch("src.ai.openai_client.OpenAI") as sdk_client,
            patch("src.ai.openai_client.DefaultHttpxClient") as http_client,
            caplog.at_level("DEBUG"),
        ):
            with pytest.raises(PKVRuntimeError) as captured:
                OpenAIClient(
                    api_key=sentinel,
                    base_url="https://explicit.example/v1",
                    model="explicit-model",
                )

        assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID
        validate_url.assert_not_called()
        sdk_client.assert_not_called()
        http_client.assert_not_called()
        assert sentinel not in str(captured.value)
        assert sentinel not in caplog.text
        assert "unknown-provider-secret" not in caplog.text

    def test_init_splits_base_url_query_from_sdk_path_and_drops_fragment(
        self, mock_config
    ):
        """真实 SDK 请求保留重复/空 query，资源 path 与 fragment 语义正确。"""
        endpoint = (
            "https://embd.example/v1?region_code=north&region_code=south"
            "&flag=&routing_key=primary#client-only"
        )
        request_urls = []

        def handle_request(request: httpx.Request) -> httpx.Response:
            request_urls.append(str(request.url))
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"embedding": [0.1] * 1536, "index": 0, "object": "embedding"}
                    ],
                    "model": "text-embedding-3-small",
                    "object": "list",
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )

        def build_http_client(**kwargs):
            return SDKDefaultHttpxClient(
                **kwargs,
                transport=httpx.MockTransport(handle_request),
            )

        with patch(
            "src.ai.openai_client.DefaultHttpxClient",
            side_effect=build_http_client,
        ):
            client = OpenAIClient(base_url=endpoint)
            client.embed("transport semantics")

        assert client.base_url == endpoint
        assert request_urls == [
            "https://embd.example/v1/embeddings"
            "?region_code=north&region_code=south&flag=&routing_key=primary"
        ]

    def test_native_http_logs_do_not_echo_real_request_url_credentials(
        self, mock_config, caplog
    ):
        """真实 SDK/httpx 请求在 DEBUG 下也不能通过第三方 logger 泄密。"""
        sentinel = "native-transport-secret"
        endpoint = (
            f"https://embd.example/v1;JSESSIONID={sentinel}"
            f"?subscription-key={sentinel}&jwt={sentinel}"
        )
        request_urls = []

        def handle_request(request: httpx.Request) -> httpx.Response:
            request_urls.append(str(request.url))
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"embedding": [0.1] * 1536, "index": 0, "object": "embedding"}
                    ],
                    "model": "text-embedding-3-small",
                    "object": "list",
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                },
            )

        def build_http_client(**kwargs):
            return SDKDefaultHttpxClient(
                **kwargs,
                transport=httpx.MockTransport(handle_request),
            )

        native_loggers = (
            "httpx",
            "httpx._client",
            "httpcore",
            "httpcore.connection",
            "httpcore.http11",
            "httpcore.http2",
            "httpcore.proxy",
            "openai",
            "openai._base_client",
        )
        previous_levels = {
            name: logging.getLogger(name).level for name in native_loggers
        }
        for name in native_loggers:
            logging.getLogger(name).setLevel(logging.NOTSET)

        try:
            with (
                patch(
                    "src.ai.openai_client.DefaultHttpxClient",
                    side_effect=build_http_client,
                ),
                caplog.at_level(logging.DEBUG),
            ):
                client = OpenAIClient(base_url=endpoint)
                client.embed("native log safety")
        finally:
            for name, level in previous_levels.items():
                logging.getLogger(name).setLevel(level)

        assert request_urls
        assert sentinel in request_urls[0]
        assert sentinel not in caplog.text

    def test_init_without_api_key(self, mock_config):
        """测试没有 API Key 时抛出异常"""
        mock_config.embd_api_key = None

        with pytest.raises(PKVRuntimeError) as captured:
            OpenAIClient()

        assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID


class TestOpenAIEmbed:
    """测试单个文本 Embedding"""

    def test_request_revalidates_endpoint_before_sdk_call(self, client, caplog):
        sentinel = "mutated-embedding-key-secret"
        client.api_key = sentinel
        client.base_url = "http://remote.example/v1"

        with caplog.at_level("DEBUG"):
            with pytest.raises(PKVRuntimeError) as captured:
                client.embed("must not dispatch")

        assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID
        client.client.embeddings.create.assert_not_called()
        assert sentinel not in str(captured.value)
        assert sentinel not in caplog.text

    def test_embed_success(self, client):
        """测试成功生成 Embedding"""
        # Mock API 响应
        mock_response = Mock()
        mock_response.data = [_embedding_item([0.1] * 1536)]
        mock_response.usage = Mock(prompt_tokens=10, total_tokens=10)

        client.client.embeddings.create = Mock(return_value=mock_response)

        embedding = client.embed("Hello, world!")

        assert len(embedding) == 1536
        assert all(isinstance(x, float) for x in embedding)
        assert client.last_usage is not None
        assert client.last_usage.embedding_input_tokens == 10
        assert client.last_usage_complete is True
        client.client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-small",
            input="Hello, world!",
            dimensions=1536,
        )

    def test_success_usage_log_rejects_untrusted_sdk_values(self, client, caplog):
        sentinel = "sdk-usage-api-key-secret\r\nInjected-Header"
        mock_response = Mock()
        mock_response.data = [_embedding_item([0.1] * 1536)]
        mock_response.usage = Mock(
            prompt_tokens=sentinel,
            total_tokens=True,
        )
        client.client.embeddings.create = Mock(return_value=mock_response)

        with caplog.at_level("INFO", logger="src.ai.openai_client"):
            client.embed("usage log safety")

        assert "prompt_tokens=unknown" in caplog.text
        assert "total_tokens=unknown" in caplog.text
        assert "sdk-usage-api-key-secret" not in caplog.text
        assert "Injected-Header" not in caplog.text
        assert client.last_usage is None
        assert client.last_usage_complete is False

    def test_embed_rejects_dimension_mismatch(self, client):
        """测试返回维度与配置不一致时抛出异常。"""
        mock_response = Mock()
        mock_response.data = [_embedding_item([0.1] * 2560)]
        mock_response.usage = Mock(prompt_tokens=10, total_tokens=10)

        client.client.embeddings.create = Mock(return_value=mock_response)

        _assert_embedding_protocol_failure(lambda: client.embed("dimension mismatch"))

    @pytest.mark.parametrize(
        "data",
        [
            [],
            [
                _embedding_item([0.1] * 1536),
                _embedding_item([0.2] * 1536, index=1),
            ],
            [_embedding_item([0.1] * 1536, index=1)],
            [_embedding_item([0.1] * 1536, index=True)],
        ],
    )
    def test_embed_rejects_invalid_cardinality_or_index(self, client, data):
        response = Mock(data=data, usage=None)
        client.client.embeddings.create = Mock(return_value=response)

        _assert_embedding_protocol_failure(lambda: client.embed("invalid response"))

    @pytest.mark.parametrize(
        "invalid_value",
        [
            True,
            "0.1",
            pytest.param(10**10_000, id="huge-int"),
            pytest.param(1e308, id="float32-overflow"),
            float("nan"),
            float("inf"),
            float("-inf"),
        ],
    )
    def test_embed_rejects_non_exact_or_non_finite_vector_values(
        self,
        client,
        invalid_value,
    ):
        vector = [0.1] * 1536
        vector[0] = invalid_value
        response = Mock(
            data=[_embedding_item(vector)],
            usage=None,
        )
        client.client.embeddings.create = Mock(return_value=response)

        _assert_embedding_protocol_failure(lambda: client.embed("invalid vector"))

    @pytest.mark.parametrize(
        "vector",
        [
            pytest.param([0.0] * 1536, id="zero-norm"),
            pytest.param([1e-30] * 1536, id="float32-norm-underflow"),
            pytest.param([3e38] * 1536, id="float32-norm-overflow"),
        ],
    )
    def test_embed_rejects_cosine_unsafe_vectors(self, client, vector):
        client.client.embeddings.create = Mock(
            return_value=Mock(
                data=[_embedding_item(vector)],
                usage=None,
            )
        )

        _assert_embedding_protocol_failure(lambda: client.embed("unsafe vector"))

    def test_embedding_protocol_failure_does_not_leak_vector_value(
        self,
        client,
        caplog,
    ):
        sentinel = "embedding-vector-secret\r\nInjected-Header"
        vector = [0.1] * 1536
        vector[0] = sentinel
        client.client.embeddings.create = Mock(
            return_value=Mock(
                data=[_embedding_item(vector)],
                usage=None,
            )
        )

        with caplog.at_level("ERROR", logger="src.ai.openai_client"):
            error = _assert_embedding_protocol_failure(
                lambda: client.embed("malformed vector")
            )

        assert sentinel not in str(error)
        assert sentinel not in caplog.text
        assert "Injected-Header" not in caplog.text

    def test_embed_retries_without_dimensions_when_backend_rejects_it(self, client):
        """测试后端不支持 dimensions 参数时回退重试。"""
        from openai import OpenAIError

        mock_response = Mock()
        mock_response.data = [_embedding_item([0.1] * 1536)]
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

    def test_dimensions_fallback_log_does_not_expose_model(self, client, caplog):
        sentinel = "fallback-model-secret"
        client.model = sentinel
        mock_response = Mock()
        mock_response.data = [_embedding_item([0.1] * 1536)]
        mock_response.usage = Mock(prompt_tokens=1, total_tokens=1)
        client.client.embeddings.create = Mock(
            side_effect=[
                TypeError("unexpected keyword argument 'dimensions'"),
                mock_response,
            ]
        )

        with caplog.at_level("WARNING", logger="src.ai.openai_client"):
            client.embed("fallback log safety")

        assert "component=embedding parameter=dimensions status=retried" in (
            caplog.text
        )
        assert sentinel not in caplog.text

    def test_embed_auto_detects_dimension_on_first_success(self, mock_config):
        """测试 auto 模式会锁定首次成功返回的真实维度。"""
        mock_config.embedding_dim = None
        mock_config.embedding_dim_is_auto = True

        with patch("src.ai.openai_client.OpenAI"):
            client = OpenAIClient()

        mock_response = Mock()
        mock_response.data = [_embedding_item([0.1] * 2560)]
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

    @pytest.mark.parametrize(
        "vector",
        [[], [0.1] * 65_537],
    )
    def test_auto_dimension_rejects_out_of_bounds_before_locking(
        self,
        mock_config,
        vector,
    ):
        mock_config.embedding_dim = None
        mock_config.embedding_dim_is_auto = True
        with patch("src.ai.openai_client.OpenAI"):
            client = OpenAIClient()
        client.client.embeddings.create = Mock(
            return_value=Mock(
                data=[_embedding_item(vector)],
                usage=None,
            )
        )

        _assert_embedding_protocol_failure(
            lambda: client.embed("invalid auto dimension")
        )

        assert client.dimensions is None
        assert client._auto_dimensions_pending is True
        mock_config.set_runtime_embedding_dim.assert_not_called()

    def test_resolve_dimensions_preserves_protocol_failure(self, mock_config):
        mock_config.embedding_dim = None
        mock_config.embedding_dim_is_auto = True
        with patch("src.ai.openai_client.OpenAI"):
            client = OpenAIClient()
        client.client.embeddings.create = Mock(return_value=Mock(data=[], usage=None))

        _assert_embedding_protocol_failure(client.resolve_dimensions)

        assert client.dimensions is None
        assert client._auto_dimensions_pending is True
        mock_config.set_runtime_embedding_dim.assert_not_called()

    @pytest.mark.parametrize("unsafe_kind", ["zero", "norm-overflow"])
    def test_protocol_code_survives_retrieval_and_mcp_projection(
        self,
        client,
        unsafe_kind,
    ):
        from threading import Lock
        from types import SimpleNamespace

        from src.ai.embedder import Embedder
        from src.mcp.tools import _serialize_search_response
        from src.retrieval.vector_retriever import VectorRetriever

        vector = [0.0] * 1536 if unsafe_kind == "zero" else [3e38] * 1536
        client.client.embeddings.create = Mock(
            return_value=Mock(
                data=[_embedding_item(vector)],
                usage=None,
            )
        )
        retriever = object.__new__(VectorRetriever)
        retriever.embedder = Embedder(openai_client=client)
        retriever._embedder_factory = None
        retriever._embedder_lock = Lock()
        retriever._embedder_dim = client.dim
        vector_store = SimpleNamespace(
            dim=1536,
            add_doc_vector=Mock(),
            add_chunk_vectors=Mock(),
        )

        response = retriever._embed_query(
            "protocol canary",
            vector_store,
            strategy="vector",
        )

        assert response.status == "error"
        assert len(response.issues) == 1
        assert response.issues[0].code is ErrorCode.PROVIDER_PROTOCOL_FAILED
        assert response.issues[0].stage == "embedding_protocol"
        payload = _serialize_search_response(
            response,
            source_type=None,
            tag=None,
        )
        assert payload["issues"] == [
            {
                "code": ErrorCode.PROVIDER_PROTOCOL_FAILED.value,
                "message": "Provider 响应协议无效",
                "stage": "embedding_protocol",
                "recoverable": True,
                "cause_type": "PKVRuntimeError",
            }
        ]
        assert "3e+38" not in repr(response)
        assert "3e+38" not in repr(payload)
        vector_store.add_doc_vector.assert_not_called()
        vector_store.add_chunk_vectors.assert_not_called()

    def test_auto_dimension_error_does_not_echo_provider_details(
        self, mock_config, caplog
    ):
        """自动维度探测错误只暴露固定消息和异常类型。"""
        from openai import OpenAIError

        sentinel = "auto-dimension-provider-secret"
        mock_config.embedding_dim = None
        mock_config.embedding_dim_is_auto = True
        with patch("src.ai.openai_client.OpenAI"):
            client = OpenAIClient()
        client.client.embeddings.create = Mock(
            side_effect=OpenAIError(
                f"response body at https://example/v1?jwt={sentinel}: {sentinel}"
            )
        )

        with caplog.at_level("ERROR", logger="src.ai.openai_client"):
            with pytest.raises(RuntimeError) as exc_info:
                client.resolve_dimensions()

        assert str(exc_info.value) == "Embedding 维度探测失败"
        assert sentinel not in caplog.text
        assert sentinel not in str(exc_info.value)

    def test_embed_uses_persisted_auto_dimension_after_restart(self, mock_config):
        """测试 auto 模式在已持久化维度后会直接复用该维度。"""
        mock_config.embedding_dim = 2560
        mock_config.embedding_dim_is_auto = True

        with patch("src.ai.openai_client.OpenAI"):
            client = OpenAIClient()

        mock_response = Mock()
        mock_response.data = [_embedding_item([0.1] * 2560)]
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
                "Rate limit exceeded", response=Mock(status_code=429), body=None
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

        client.client.embeddings.create = Mock(side_effect=OpenAIError("Unknown error"))

        with pytest.raises(Exception, match="OpenAI API 调用失败"):
            client.embed("text")

    def test_embed_error_does_not_echo_endpoint_credentials(self, client, caplog):
        """SDK 异常中的 endpoint/API 凭据不得进入日志或公开异常。"""
        from openai import OpenAIError

        sentinel = "runtime-endpoint-secret"
        client.client.embeddings.create = Mock(
            side_effect=OpenAIError(
                f"request failed at https://user:{sentinel}@example/v1"
                f"?pwd={sentinel}"
            )
        )

        with caplog.at_level("ERROR", logger="src.ai.openai_client"):
            with pytest.raises(Exception) as exc_info:
                client.embed("text")

        assert sentinel not in caplog.text
        assert sentinel not in str(exc_info.value)


class TestOpenAIEmbedBatch:
    """测试批量 Embedding"""

    def test_embed_batch_success(self, client):
        """测试成功批量生成 Embedding"""
        texts = ["text1", "text2", "text3"]

        # Mock API 响应
        mock_response = Mock()
        mock_response.data = [
            _embedding_item([0.1] * 1536),
            _embedding_item([0.2] * 1536, index=1),
            _embedding_item([0.3] * 1536, index=2),
        ]
        mock_response.usage = Mock(prompt_tokens=30, total_tokens=30)

        client.client.embeddings.create = Mock(return_value=mock_response)

        embeddings = client.embed_batch(texts)

        assert len(embeddings) == 3
        assert all(len(emb) == 1536 for emb in embeddings)
        assert client.last_usage is not None
        assert client.last_usage.embedding_input_tokens == 30
        assert client.last_usage_complete is True
        client.client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-small",
            input=texts,
            dimensions=1536,
        )

    @pytest.mark.parametrize(
        "data",
        [
            [_embedding_item([0.1] * 1536)],
            [
                _embedding_item([0.1] * 1536, index=1),
                _embedding_item([0.2] * 1536),
            ],
            [
                _embedding_item([0.1] * 1536),
                _embedding_item([0.2] * 1536, index=2),
            ],
        ],
    )
    def test_embed_batch_rejects_missing_or_misordered_items(self, client, data):
        response = Mock(data=data, usage=None)
        client.client.embeddings.create = Mock(return_value=response)

        _assert_embedding_protocol_failure(
            lambda: client.embed_batch(["first", "second"])
        )

    def test_embed_batch_rejects_float32_overflow_before_return(self, client):
        response = Mock(
            data=[
                _embedding_item([0.1] * 1536),
                _embedding_item([1e308] * 1536, index=1),
            ],
            usage=None,
        )
        client.client.embeddings.create = Mock(return_value=response)

        _assert_embedding_protocol_failure(
            lambda: client.embed_batch(["first", "second"])
        )

    @pytest.mark.parametrize(
        "unsafe_vector",
        [[0.0] * 1536, [3e38] * 1536],
        ids=["zero-norm", "norm-overflow"],
    )
    def test_embed_batch_rejects_cosine_unsafe_vector(self, client, unsafe_vector):
        response = Mock(
            data=[
                _embedding_item([0.1] * 1536),
                _embedding_item(unsafe_vector, index=1),
            ],
            usage=None,
        )
        client.client.embeddings.create = Mock(return_value=response)

        _assert_embedding_protocol_failure(
            lambda: client.embed_batch(["first", "second"])
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
            _embedding_item([0.1] * 1536),
            _embedding_item([0.2] * 1536, index=1),
        ]
        mock_response.usage = Mock(prompt_tokens=20, total_tokens=20)

        client.client.embeddings.create = Mock(return_value=mock_response)

        embeddings = client.embed_batch(texts)

        # 应该只处理有效文本
        assert len(embeddings) == 2

        # 检查传递给 API 的文本
        call_args = client.client.embeddings.create.call_args
        assert call_args[1]["input"] == ["text1", "text2"]
        assert call_args[1]["dimensions"] == 1536

    def test_embed_batch_with_batching(self, client):
        """测试分批处理"""
        # 创建 250 个文本（超过默认 batch_size=100）
        texts = [f"text{i}" for i in range(250)]

        # Mock API 响应
        def create_mock_response(batch_size):
            mock_response = Mock()
            mock_response.data = [
                _embedding_item([0.1] * 1536, index=index)
                for index in range(batch_size)
            ]
            mock_response.usage = Mock(
                prompt_tokens=batch_size * 10, total_tokens=batch_size * 10
            )
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
        assert client.last_usage is not None
        assert client.last_usage.embedding_input_tokens == 2500
        assert client.last_usage_complete is True
        assert len(embeddings) == 250

    def test_embed_batch_keeps_partial_usage_non_priceable(self, client):
        """One omitted batch field must not be disguised as a complete total."""

        first = Mock(
            data=[_embedding_item([0.1] * 1536)],
            usage=Mock(prompt_tokens=10, total_tokens=10),
        )
        second = Mock(
            data=[_embedding_item([0.2] * 1536)],
            usage=Mock(prompt_tokens=None, total_tokens=None),
        )
        client.client.embeddings.create = Mock(side_effect=[first, second])

        client.embed_batch(["first", "second"], batch_size=1)

        assert client.last_usage is not None
        assert client.last_usage.embedding_input_tokens == 10
        assert client.last_usage_complete is False

    def test_embed_batch_rejects_dimension_mismatch(self, client):
        """测试批量返回错维度时抛出异常。"""
        texts = ["text1", "text2"]

        mock_response = Mock()
        mock_response.data = [
            _embedding_item([0.1] * 1536),
            _embedding_item([0.2] * 2560, index=1),
        ]
        mock_response.usage = Mock(prompt_tokens=20, total_tokens=20)
        client.client.embeddings.create = Mock(return_value=mock_response)

        _assert_embedding_protocol_failure(lambda: client.embed_batch(texts))

    def test_embed_batch_retries_without_dimensions_when_backend_rejects_it(
        self, client
    ):
        """测试批量请求在后端不支持 dimensions 时回退重试。"""
        from openai import OpenAIError

        texts = ["text1", "text2"]
        mock_response = Mock()
        mock_response.data = [
            _embedding_item([0.1] * 1536),
            _embedding_item([0.2] * 1536, index=1),
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

        with patch("src.ai.openai_client.OpenAI"):
            client = OpenAIClient()

        texts = ["text1", "text2"]
        mock_response = Mock()
        mock_response.data = [
            _embedding_item([0.1] * 2560),
            _embedding_item([0.2] * 2560, index=1),
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
        mock_response.data = [_embedding_item([0.1] * 1536)]
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
            _embedding_item([0.1] * 1536),
            _embedding_item([0.2] * 1536, index=1),
            _embedding_item([0.3] * 1536, index=2),
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
        mock_response.data = [_embedding_item(mock_embedding)]
        mock_response.usage = Mock(prompt_tokens=10, total_tokens=10)

        client.client.embeddings.create = Mock(return_value=mock_response)

        embedding = client.embed_numpy("text")

        assert embedding[0] == 0.5
        assert embedding[1] == 0.3
        assert embedding[2] == 0.7
        assert np.allclose(embedding[3:], 0.0)
