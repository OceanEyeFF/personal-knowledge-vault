"""
DeepSeek API 客户端单元测试
"""

import logging
from unittest.mock import Mock, patch, MagicMock

import httpx
import pytest

from src.ai.deepseek_client import DeepSeekClient
from src.ai.provider_factory import ChatProviderSettings
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout


@pytest.fixture
def mock_config():
    """Mock 配置"""
    with patch("src.ai.deepseek_client.get_config") as mock:
        config = Mock()
        config.llm_provider = "openai_compatible"
        config.llm_api_key = "test-api-key"
        config.llm_base_url = "https://api.deepseek.com/v1"
        config.llm_model = "configured-model"
        config.llm_max_tokens = 2000
        config.llm_temperature = 0.7
        config.llm_timeout_seconds = 30.0
        config.llm_max_retries = 2
        config.layout = RuntimeLayout.resolve()

        config_attributes = {
            "ai.llm.provider": "llm_provider",
            "ai.llm.api_key": "llm_api_key",
            "ai.llm.base_url": "llm_base_url",
            "ai.llm.model": "llm_model",
            "ai.llm.max_tokens": "llm_max_tokens",
            "ai.llm.temperature": "llm_temperature",
            "ai.llm.timeout_seconds": "llm_timeout_seconds",
            "ai.llm.max_retries": "llm_max_retries",
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
    return DeepSeekClient()


class TestDeepSeekClientInit:
    """测试客户端初始化"""

    def test_init_with_defaults(self, mock_config):
        """测试使用默认配置初始化"""
        client = DeepSeekClient()

        assert client.api_key == "test-api-key"
        assert client.base_url == "https://api.deepseek.com/v1"
        assert client.model == "configured-model"
        assert client.timeout == 30.0
        assert client.max_retries == 2

    def test_init_with_configured_model(self, mock_config):
        """测试默认模型从统一配置入口读取"""
        mock_config.llm_model = "deepseek-reasoner"

        client = DeepSeekClient()

        assert client.model == "deepseek-reasoner"

    def test_init_with_custom_params(self, mock_config):
        """测试使用自定义参数初始化"""
        client = DeepSeekClient(
            api_key="custom-key",
            base_url="https://custom.api.com",
            model="custom-model",
            timeout=60.0,
            max_retries=5,
        )

        assert client.api_key == "custom-key"
        assert client.base_url == "https://custom.api.com"
        assert client.model == "custom-model"
        assert client.timeout == 60.0
        assert client.max_retries == 5

    def test_explicit_snapshot_does_not_read_global_provider_config(self):
        settings = ChatProviderSettings(
            provider="openai_compatible",
            api_key="snapshot-key",
            base_url="https://chat.example/v1",
            model="snapshot-model",
            max_tokens=512,
            temperature=0.2,
            timeout_seconds=8.5,
            max_retries=1,
        )

        with (
            patch(
                "src.ai.deepseek_client.get_config",
                side_effect=AssertionError("global config must not be read"),
            ) as get_config,
            patch.object(
                DeepSeekClient,
                "_load_prompt",
                side_effect=["summarize", "extract-tags"],
            ),
        ):
            client = DeepSeekClient(
                settings=settings,
                layout=RuntimeLayout.resolve(),
            )

        get_config.assert_not_called()
        assert client.api_key == "snapshot-key"
        assert client.base_url == "https://chat.example/v1"
        assert client.model == "snapshot-model"
        assert client.timeout == 8.5
        assert client.max_retries == 1

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("llm_api_key", ""),
            ("llm_model", ""),
            ("llm_timeout_seconds", 0),
            ("llm_timeout_seconds", True),
            ("llm_max_retries", -1),
            ("llm_max_retries", True),
        ],
    )
    def test_invalid_config_snapshot_fails_before_prompt_or_http_construction(
        self, mock_config, field, value
    ):
        setattr(mock_config, field, value)

        with (
            patch.object(DeepSeekClient, "_load_prompt") as load_prompt,
            patch("src.ai.deepseek_client.httpx.Client") as http_client,
        ):
            with pytest.raises(PKVRuntimeError) as captured:
                DeepSeekClient()

        assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID
        load_prompt.assert_not_called()
        http_client.assert_not_called()

    @pytest.mark.parametrize(
        "override",
        [
            {"api_key": ""},
            {"model": ""},
            {"timeout": 0},
            {"max_retries": True},
        ],
    )
    def test_invalid_explicit_override_is_revalidated_before_prompt_loading(
        self, mock_config, override
    ):
        with patch.object(DeepSeekClient, "_load_prompt") as load_prompt:
            with pytest.raises(PKVRuntimeError) as captured:
                DeepSeekClient(**override)

        assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID
        load_prompt.assert_not_called()

    def test_init_log_exposes_only_fixed_provider_metadata(self, mock_config, caplog):
        """初始化日志不得写入 URL、model 或 API key。"""
        sentinel = "deepseek-init-log-secret"
        endpoint = (
            f"https://llm.example/v1;token={sentinel}"
            f"?api_key={sentinel}#fragment={sentinel}"
        )

        with caplog.at_level("INFO", logger="src.ai.deepseek_client"):
            DeepSeekClient(
                api_key=f"key-{sentinel}",
                base_url=endpoint,
                model=f"model-{sentinel}",
            )

        assert "component=llm status=ready" in caplog.text
        assert "llm.example" not in caplog.text
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
    def test_invalid_endpoint_fails_before_http_client_construction(
        self, mock_config, caplog, endpoint
    ):
        sentinel = "direct-llm-key-secret"

        with patch("src.ai.deepseek_client.httpx.Client") as http_client:
            with caplog.at_level("DEBUG"):
                with pytest.raises(PKVRuntimeError) as captured:
                    DeepSeekClient(api_key=sentinel, base_url=endpoint)

        assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID
        http_client.assert_not_called()
        assert sentinel not in str(captured.value)
        assert sentinel not in caplog.text
        assert "url-secret" not in caplog.text

    def test_numeric_loopback_http_remains_supported(self, mock_config):
        client = DeepSeekClient(base_url="http://127.0.0.1:43123/v1")

        assert client.base_url == "http://127.0.0.1:43123/v1"

    @pytest.mark.parametrize(
        "provider",
        ["fake", "unknown-provider-secret", "OPENAI_COMPATIBLE", True, None],
    )
    def test_unknown_provider_fails_before_url_or_http_construction(
        self, mock_config, caplog, provider
    ):
        mock_config.llm_provider = provider
        sentinel = "explicit-llm-key-secret"

        with (
            patch("src.ai.provider_factory.validate_provider_base_url") as validate_url,
            patch("src.ai.deepseek_client.httpx.Client") as http_client,
            caplog.at_level("DEBUG"),
        ):
            with pytest.raises(PKVRuntimeError) as captured:
                DeepSeekClient(
                    api_key=sentinel,
                    base_url="https://explicit.example/v1",
                    model="explicit-model",
                )

        assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID
        validate_url.assert_not_called()
        http_client.assert_not_called()
        assert sentinel not in str(captured.value)
        assert sentinel not in caplog.text
        assert "unknown-provider-secret" not in caplog.text

    def test_init_without_api_key(self, mock_config):
        """测试没有 API Key 时抛出异常"""
        mock_config.llm_api_key = None

        with pytest.raises(PKVRuntimeError) as captured:
            DeepSeekClient()

        assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID

    def test_load_prompts(self, client):
        """测试 Prompt 模板加载"""
        assert client._summarize_prompt
        assert client._extract_tags_prompt
        assert "{content}" in client._summarize_prompt
        assert "{content}" in client._extract_tags_prompt

    def test_load_prompt_missing_file(self, client):
        """缺失 Prompt 通过 bundled-resource 稳定错误 fail closed。"""

        with pytest.raises(PKVRuntimeError) as error:
            client._load_prompt("missing.txt")
        assert error.value.code is ErrorCode.RESOURCE_MISSING

    def test_load_prompt_rejects_path_escape(self, client):
        """Prompt API 不允许用相对路径绕过 bundled resource 根。"""

        with pytest.raises(ValueError, match="Prompt 文件名非法"):
            client._load_prompt("../config/config.yaml")


class TestDeepSeekSummarize:
    """测试摘要生成功能"""

    def test_summarize_success(self, client):
        """测试成功生成摘要"""
        with patch.object(client, "_call_api") as mock_call:
            mock_call.return_value = "这是一个简洁的摘要。"

            summary = client.summarize("长文本内容...", max_words=300)

            assert summary == "这是一个简洁的摘要。"
            mock_call.assert_called_once()

            # 检查调用参数
            call_args = mock_call.call_args
            assert call_args[1]["temperature"] == 0.7
            assert call_args[1]["max_tokens"] == 600  # max_words * 2

    def test_summarize_empty_content(self, client):
        """测试空内容时抛出异常"""
        with pytest.raises(ValueError, match="摘要内容不能为空"):
            client.summarize("")

        with pytest.raises(ValueError, match="摘要内容不能为空"):
            client.summarize("   ")

    def test_summarize_with_custom_temperature(self, client):
        """测试自定义温度参数"""
        with patch.object(client, "_call_api") as mock_call:
            mock_call.return_value = "摘要"

            client.summarize("内容", max_words=200, temperature=0.5)

            call_args = mock_call.call_args
            assert call_args[1]["temperature"] == 0.5


class TestDeepSeekExtractTags:
    """测试标签提取功能"""

    def test_extract_tags_success(self, client):
        """测试成功提取标签"""
        with patch.object(client, "_call_api") as mock_call:
            mock_call.return_value = '["标签1", "标签2", "标签3"]'

            tags = client.extract_tags("文本内容...")

            assert tags == ["标签1", "标签2", "标签3"]
            assert len(tags) == 3
            mock_call.assert_called_once()

    def test_extract_tags_with_more_than_5_tags(self, client):
        """测试提取超过 5 个标签时自动截取"""
        with patch.object(client, "_call_api") as mock_call:
            mock_call.return_value = '["tag1", "tag2", "tag3", "tag4", "tag5", "tag6"]'

            tags = client.extract_tags("content")

            assert len(tags) == 5
            assert tags == ["tag1", "tag2", "tag3", "tag4", "tag5"]

    def test_extract_tags_with_less_than_3_tags(self, client):
        """测试 API 返回少于 3 个标签时保持返回并记录告警"""
        with patch.object(client, "_call_api") as mock_call:
            mock_call.return_value = '["tag1", "tag2"]'

            tags = client.extract_tags("content")

            assert tags == ["tag1", "tag2"]

    def test_extract_tags_non_list_json_response(self, client):
        """测试 API 返回 JSON 但不是列表时抛出异常"""
        with patch.object(client, "_call_api") as mock_call:
            mock_call.return_value = '{"tag": "tag1"}'

            with pytest.raises(ValueError, match="API 返回的标签格式不是列表"):
                client.extract_tags("content")

    def test_extract_tags_empty_content(self, client):
        """测试空内容时抛出异常"""
        with pytest.raises(ValueError, match="提取标签的内容不能为空"):
            client.extract_tags("")

    def test_extract_tags_invalid_num_tags(self, client):
        """测试无效的标签数量"""
        with pytest.raises(ValueError, match="标签数量必须在 3-5 之间"):
            client.extract_tags("content", num_tags=2)

        with pytest.raises(ValueError, match="标签数量必须在 3-5 之间"):
            client.extract_tags("content", num_tags=6)

    def test_extract_tags_fallback_parsing(self, client):
        """测试降级解析（非标准 JSON 格式）"""
        with patch.object(client, "_call_api") as mock_call:
            # API 返回非标准格式
            mock_call.return_value = '"tag1", "tag2", "tag3"'

            tags = client.extract_tags("content")

            assert len(tags) >= 3
            assert "tag1" in tags
            assert "tag2" in tags
            assert "tag3" in tags

    def test_extract_tags_json_array_embedded_in_text(self, client):
        """测试从说明文字中提取 JSON 数组"""
        with patch.object(client, "_call_api") as mock_call:
            mock_call.return_value = (
                '标签如下：["tag1", "tag2", "tag3", "tag4", "tag5", "tag6"]'
            )

            tags = client.extract_tags("content")

            assert tags == ["tag1", "tag2", "tag3", "tag4", "tag5"]

    def test_extract_tags_invalid_embedded_json_falls_back_to_quotes(self, client):
        """测试嵌入 JSON 数组解析失败后使用引号降级方案"""
        with patch.object(client, "_call_api") as mock_call:
            mock_call.return_value = (
                '标签如下：["broken", invalid] "tag1" "tag2" "tag3"'
            )

            tags = client.extract_tags("content")

            assert tags == ["broken", "tag1", "tag2", "tag3"]

    def test_extract_tags_invalid_response(self, client):
        """测试无法解析的响应"""
        with patch.object(client, "_call_api") as mock_call:
            mock_call.return_value = "这不是 JSON 也没有标签"

            with pytest.raises(Exception, match="无法从 API 响应中提取标签"):
                client.extract_tags("content")


class TestDeepSeekAPICall:
    """测试 API 调用"""

    def test_request_revalidates_endpoint_before_http_client_construction(
        self, client, caplog
    ):
        sentinel = "mutated-llm-key-secret"
        client.api_key = sentinel
        client.base_url = "http://remote.example/v1"

        with patch("src.ai.deepseek_client.httpx.Client") as http_client:
            with caplog.at_level("DEBUG"):
                with pytest.raises(PKVRuntimeError) as captured:
                    client._call_api([{"role": "user", "content": "test"}])

        assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID
        http_client.assert_not_called()
        assert sentinel not in str(captured.value)
        assert sentinel not in caplog.text

    def test_api_call_success(self, client):
        """测试成功的 API 调用"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "API 响应内容"}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
            },
        }

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.return_value = mock_response
            mock_client_class.return_value = mock_client

            result = client._call_api([{"role": "user", "content": "test"}])

            assert result == "API 响应内容"
            mock_client.post.assert_called_once()
        assert client.last_usage is not None
        assert client.last_usage.uncached_input_tokens == 10
        assert client.last_usage.generated_tokens == 20

    def test_success_usage_log_rejects_untrusted_remote_values(self, client, caplog):
        sentinel = "usage-api-key-secret\r\nInjected-Header"
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": sentinel,
                "completion_tokens": True,
                "total_tokens": -1,
            },
        }

        with patch("src.ai.deepseek_client.httpx.Client") as client_type:
            http_client = MagicMock()
            http_client.__enter__.return_value = http_client
            http_client.post.return_value = mock_response
            client_type.return_value = http_client
            with caplog.at_level("INFO", logger="src.ai.deepseek_client"):
                assert client._call_api([{"role": "user", "content": "test"}]) == "ok"

        assert "prompt_tokens=unknown" in caplog.text
        assert "completion_tokens=unknown" in caplog.text
        assert "total_tokens=unknown" in caplog.text
        assert "usage-api-key-secret" not in caplog.text
        assert "Injected-Header" not in caplog.text
        assert client.last_usage is None

    def test_api_call_appends_resource_to_path_and_drops_fragment(self, mock_config):
        """资源路径追加在 path 后，query 保留而 fragment 不进入 HTTP。"""
        endpoint = (
            "https://llm.example/root/v1?region_code=north&routing_key=primary"
            "#client-only"
        )
        client = DeepSeekClient(base_url=endpoint)
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {},
        }

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.return_value = mock_response
            mock_client_class.return_value = mock_client

            assert client._call_api([{"role": "user", "content": "test"}]) == "ok"

        request_url = mock_client.post.call_args.args[0]
        assert request_url == (
            "https://llm.example/root/v1/chat/completions"
            "?region_code=north&routing_key=primary"
        )
        assert "#client-only" not in request_url

    def test_native_http_logs_do_not_echo_real_request_url_credentials(
        self, mock_config, caplog
    ):
        """真实 httpx 请求在 DEBUG 下也不能通过第三方 logger 泄密。"""
        sentinel = "deepseek-native-secret"
        endpoint = (
            f"https://llm.example/v1;JSESSIONID={sentinel}"
            f"?jwt={sentinel}#client-only"
        )
        request_urls = []

        def handle_request(request: httpx.Request) -> httpx.Response:
            request_urls.append(str(request.url))
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {},
                },
            )

        real_client = httpx.Client

        def build_http_client(*args, **kwargs):
            return real_client(
                *args,
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
        )
        previous_levels = {
            name: logging.getLogger(name).level for name in native_loggers
        }
        for name in native_loggers:
            logging.getLogger(name).setLevel(logging.NOTSET)

        try:
            with (
                patch(
                    "src.ai.deepseek_client.httpx.Client",
                    side_effect=build_http_client,
                ),
                caplog.at_level(logging.DEBUG),
            ):
                client = DeepSeekClient(base_url=endpoint)
                assert client._call_api([{"role": "user", "content": "test"}]) == "ok"
        finally:
            for name, level in previous_levels.items():
                logging.getLogger(name).setLevel(level)

        assert request_urls
        assert sentinel in request_urls[0]
        assert sentinel not in caplog.text

    def test_api_call_rate_limit_retry(self, client):
        """测试 API 限流重试"""
        # 第一次返回 429，第二次成功
        mock_response_429 = Mock()
        mock_response_429.status_code = 429

        mock_response_200 = Mock()
        mock_response_200.status_code = 200
        mock_response_200.json.return_value = {
            "choices": [{"message": {"content": "成功"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.side_effect = [mock_response_429, mock_response_200]
            mock_client_class.return_value = mock_client

            with patch("time.sleep"):  # 跳过等待时间
                result = client._call_api([{"role": "user", "content": "test"}])

            assert result == "成功"
            assert mock_client.post.call_count == 2

    def test_api_call_server_error_retry(self, client):
        """测试服务器错误重试"""
        mock_response_500 = Mock()
        mock_response_500.status_code = 500

        mock_response_200 = Mock()
        mock_response_200.status_code = 200
        mock_response_200.json.return_value = {
            "choices": [{"message": {"content": "成功"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.side_effect = [mock_response_500, mock_response_200]
            mock_client_class.return_value = mock_client

            with patch("time.sleep"):
                result = client._call_api([{"role": "user", "content": "test"}])

            assert result == "成功"

    def test_api_call_client_error(self, client):
        """测试客户端错误（4xx 非 429）"""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.return_value = mock_response
            mock_client_class.return_value = mock_client

            with pytest.raises(Exception, match="DeepSeek API 调用失败"):
                client._call_api([{"role": "user", "content": "test"}])

    def test_api_error_does_not_echo_response_or_network_credentials(
        self, client, caplog
    ):
        """响应正文或网络异常中的 endpoint 凭据不得进入日志/异常。"""
        sentinel = "runtime-endpoint-secret"
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.text = (
            f"failed https://user:{sentinel}@example/v1?passwd={sentinel}"
        )

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.return_value = mock_response
            mock_client_class.return_value = mock_client

            with caplog.at_level("ERROR", logger="src.ai.deepseek_client"):
                with pytest.raises(Exception) as exc_info:
                    client._call_api([{"role": "user", "content": "test"}])

        assert sentinel not in caplog.text
        assert sentinel not in str(exc_info.value)

    def test_generic_provider_exception_is_replaced_with_safe_message(
        self, client, caplog
    ):
        sentinel = "generic-provider-response-secret"

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.side_effect = ValueError(
                f"bad response https://example/v1?session_id={sentinel}: {sentinel}"
            )
            mock_client_class.return_value = mock_client

            with caplog.at_level("ERROR", logger="src.ai.deepseek_client"):
                with pytest.raises(Exception) as exc_info:
                    client._call_api([{"role": "user", "content": "test"}])

        assert str(exc_info.value) == "DeepSeek API 调用失败"
        assert sentinel not in caplog.text

    def test_api_call_max_retries_exceeded(self, client):
        """测试超过最大重试次数"""
        client.max_retries = 2

        mock_response = Mock()
        mock_response.status_code = 429

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.return_value = mock_response
            mock_client_class.return_value = mock_client

            with patch("time.sleep"):
                with pytest.raises(Exception, match="DeepSeek API 调用失败"):
                    client._call_api([{"role": "user", "content": "test"}])

        assert mock_client.post.call_count == 3

    def test_api_call_timeout_max_retries_exceeded(self, client):
        """测试请求超时超过最大重试次数"""
        client.max_retries = 2

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.side_effect = httpx.TimeoutException("timeout")
            mock_client_class.return_value = mock_client

            with patch("time.sleep"):
                with pytest.raises(Exception, match="DeepSeek API 请求超时"):
                    client._call_api([{"role": "user", "content": "test"}])

        assert mock_client.post.call_count == 3

    def test_api_call_network_error_max_retries_exceeded(self, client):
        """测试网络错误超过最大重试次数"""
        client.max_retries = 2

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.side_effect = httpx.NetworkError("network down")
            mock_client_class.return_value = mock_client

            with patch("time.sleep"):
                with pytest.raises(Exception, match="DeepSeek API 网络错误"):
                    client._call_api([{"role": "user", "content": "test"}])

        assert mock_client.post.call_count == 3

    def test_zero_retries_still_makes_exactly_one_initial_request(self, client):
        """max_retries counts retries, never the initial request."""
        client.max_retries = 0
        mock_response = Mock(status_code=500)

        with patch("httpx.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.return_value = mock_response
            mock_client_class.return_value = mock_client

            with patch("time.sleep") as sleep:
                with pytest.raises(Exception, match="DeepSeek API 调用失败"):
                    client._call_api([{"role": "user", "content": "test"}])

        assert mock_client.post.call_count == 1
        sleep.assert_not_called()
