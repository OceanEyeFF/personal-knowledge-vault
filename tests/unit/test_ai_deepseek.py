"""
DeepSeek API 客户端单元测试
"""

import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import pytest
from unittest.mock import Mock, patch, MagicMock
import json

from src.ai.deepseek_client import DeepSeekClient


@pytest.fixture
def mock_config():
    """Mock 配置"""
    with patch('src.ai.deepseek_client.get_config') as mock:
        config = Mock()
        config.deepseek_api_key = "test-api-key"
        config.deepseek_base_url = "https://api.deepseek.com/v1"
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
        assert client.model == "deepseek-chat"
        assert client.timeout == 30.0
        assert client.max_retries == 3

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

    def test_init_without_api_key(self):
        """测试没有 API Key 时抛出异常"""
        with patch('src.ai.deepseek_client.get_config') as mock:
            config = Mock()
            config.deepseek_api_key = None
            config.deepseek_base_url = "https://api.deepseek.com/v1"
            mock.return_value = config

            with pytest.raises(ValueError, match="DeepSeek API Key 未配置"):
                DeepSeekClient()

    def test_load_prompts(self, client):
        """测试 Prompt 模板加载"""
        assert client._summarize_prompt
        assert client._extract_tags_prompt
        assert "{content}" in client._summarize_prompt
        assert "{content}" in client._extract_tags_prompt


class TestDeepSeekSummarize:
    """测试摘要生成功能"""

    def test_summarize_success(self, client):
        """测试成功生成摘要"""
        with patch.object(client, '_call_api') as mock_call:
            mock_call.return_value = "这是一个简洁的摘要。"

            summary = client.summarize("长文本内容...", max_words=300)

            assert summary == "这是一个简洁的摘要。"
            mock_call.assert_called_once()

            # 检查调用参数
            call_args = mock_call.call_args
            assert call_args[1]['temperature'] == 0.7
            assert call_args[1]['max_tokens'] == 600  # max_words * 2

    def test_summarize_empty_content(self, client):
        """测试空内容时抛出异常"""
        with pytest.raises(ValueError, match="摘要内容不能为空"):
            client.summarize("")

        with pytest.raises(ValueError, match="摘要内容不能为空"):
            client.summarize("   ")

    def test_summarize_with_custom_temperature(self, client):
        """测试自定义温度参数"""
        with patch.object(client, '_call_api') as mock_call:
            mock_call.return_value = "摘要"

            client.summarize("内容", max_words=200, temperature=0.5)

            call_args = mock_call.call_args
            assert call_args[1]['temperature'] == 0.5


class TestDeepSeekExtractTags:
    """测试标签提取功能"""

    def test_extract_tags_success(self, client):
        """测试成功提取标签"""
        with patch.object(client, '_call_api') as mock_call:
            mock_call.return_value = '["标签1", "标签2", "标签3"]'

            tags = client.extract_tags("文本内容...")

            assert tags == ["标签1", "标签2", "标签3"]
            assert len(tags) == 3
            mock_call.assert_called_once()

    def test_extract_tags_with_more_than_5_tags(self, client):
        """测试提取超过 5 个标签时自动截取"""
        with patch.object(client, '_call_api') as mock_call:
            mock_call.return_value = '["tag1", "tag2", "tag3", "tag4", "tag5", "tag6"]'

            tags = client.extract_tags("content")

            assert len(tags) == 5
            assert tags == ["tag1", "tag2", "tag3", "tag4", "tag5"]

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
        with patch.object(client, '_call_api') as mock_call:
            # API 返回非标准格式
            mock_call.return_value = '"tag1", "tag2", "tag3"'

            tags = client.extract_tags("content")

            assert len(tags) >= 3
            assert "tag1" in tags
            assert "tag2" in tags
            assert "tag3" in tags

    def test_extract_tags_invalid_response(self, client):
        """测试无法解析的响应"""
        with patch.object(client, '_call_api') as mock_call:
            mock_call.return_value = "这不是 JSON 也没有标签"

            with pytest.raises(Exception, match="无法从 API 响应中提取标签"):
                client.extract_tags("content")


class TestDeepSeekAPICall:
    """测试 API 调用"""

    def test_api_call_success(self, client):
        """测试成功的 API 调用"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [
                {"message": {"content": "API 响应内容"}}
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
            }
        }

        with patch('httpx.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.return_value = mock_response
            mock_client_class.return_value = mock_client

            result = client._call_api([{"role": "user", "content": "test"}])

            assert result == "API 响应内容"
            mock_client.post.assert_called_once()

    def test_api_call_rate_limit_retry(self, client):
        """测试 API 限流重试"""
        # 第一次返回 429，第二次成功
        mock_response_429 = Mock()
        mock_response_429.status_code = 429

        mock_response_200 = Mock()
        mock_response_200.status_code = 200
        mock_response_200.json.return_value = {
            "choices": [{"message": {"content": "成功"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        }

        with patch('httpx.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.side_effect = [mock_response_429, mock_response_200]
            mock_client_class.return_value = mock_client

            with patch('time.sleep'):  # 跳过等待时间
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
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        }

        with patch('httpx.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.side_effect = [mock_response_500, mock_response_200]
            mock_client_class.return_value = mock_client

            with patch('time.sleep'):
                result = client._call_api([{"role": "user", "content": "test"}])

            assert result == "成功"

    def test_api_call_client_error(self, client):
        """测试客户端错误（4xx 非 429）"""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"

        with patch('httpx.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.return_value = mock_response
            mock_client_class.return_value = mock_client

            with pytest.raises(Exception, match="DeepSeek API 调用失败"):
                client._call_api([{"role": "user", "content": "test"}])

    def test_api_call_max_retries_exceeded(self, client):
        """测试超过最大重试次数"""
        client.max_retries = 2

        mock_response = Mock()
        mock_response.status_code = 429

        with patch('httpx.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client.__enter__.return_value = mock_client
            mock_client.post.return_value = mock_response
            mock_client_class.return_value = mock_client

            with patch('time.sleep'):
                with pytest.raises(Exception, match="DeepSeek API 调用失败"):
                    client._call_api([{"role": "user", "content": "test"}])
