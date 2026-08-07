"""
OpenAI-compatible LLM API 客户端

封装 OpenAI-compatible Chat Completions API 调用，提供摘要生成和标签提取功能。

注意：DeepSeekClient 是历史类名，当前支持由 YAML 配置的兼容端点。
"""

import json
import time
from typing import List, Optional, Dict
from urllib.parse import urlsplit, urlunsplit
import httpx

from src.runtime.layout import open_user_file_nofollow
from src.utils.config import (
    get_config,
    redact_url_credentials,
    suppress_unsafe_http_transport_logs,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)


def _strip_trailing_url_path_slashes(base_url: str) -> str:
    """仅规范化 endpoint path，不能误改 query 或 fragment 的值。"""
    parsed = urlsplit(base_url)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip("/"),
            parsed.query,
            parsed.fragment,
        )
    )


def _append_transport_resource(base_url: str, resource: str) -> str:
    """在 URL path 后追加资源，保留 query，并明确移除 HTTP fragment。"""
    parsed = urlsplit(base_url)
    path = f"{parsed.path.rstrip('/')}/{resource.lstrip('/')}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


class DeepSeekClient:
    """OpenAI-compatible LLM 客户端（历史类名保留兼容）"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        """
        初始化 DeepSeek 客户端

        Args:
            api_key: API Key，默认从配置中读取
            base_url: API Base URL，默认从配置中读取
            model: 使用的模型名称，默认从配置中读取
            timeout: 请求超时时间（秒）
            max_retries: 最大重试次数
        """
        config = get_config()

        self.api_key = api_key or config.llm_api_key
        if not self.api_key:
            raise ValueError("LLM API Key 未配置，请检查用户数据目录中的 config/local.yaml")

        self.base_url = _strip_trailing_url_path_slashes(
            base_url or config.llm_base_url
        )
        self.model = model or config.llm_model
        self.timeout = timeout
        self.max_retries = max_retries
        suppress_unsafe_http_transport_logs()

        # 加载 Prompt 模板
        self._layout = config.layout
        self._prompts_dir = self._layout.prompts_dir
        self._summarize_prompt = self._load_prompt("summarize.txt")
        self._extract_tags_prompt = self._load_prompt("extract_tags.txt")

        display_base_url = redact_url_credentials(self.base_url) or "已配置（URL 格式不可解析）"
        logger.info(
            f"DeepSeek 客户端初始化成功: "
            f"model={self.model}, base_url={display_base_url}"
        )

    def _load_prompt(self, filename: str) -> str:
        """
        加载 Prompt 模板

        Args:
            filename: Prompt 文件名

        Returns:
            Prompt 内容

        Raises:
            FileNotFoundError: Prompt 文件不存在
        """
        if not filename or filename != filename.replace("\\", "/").split("/")[-1]:
            raise ValueError("Prompt 文件名非法")
        prompt_path = self._layout.validate_bundled_path(
            self._prompts_dir / filename,
            label="Prompt 模板",
        )

        with open_user_file_nofollow(
            prompt_path,
            "r",
            label="Prompt 模板",
            encoding="utf-8",
        ) as f:
            return f.read().strip()

    def _call_api(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 2000,
    ) -> str:
        """
        调用 DeepSeek API

        Args:
            messages: 消息列表
            temperature: 采样温度
            max_tokens: 最大生成 token 数

        Returns:
            API 响应内容

        Raises:
            Exception: API 调用失败
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        url = _append_transport_resource(self.base_url, "chat/completions")

        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(url, headers=headers, json=payload)

                # 检查响应状态
                if response.status_code == 200:
                    result = response.json()
                    content = result["choices"][0]["message"]["content"].strip()

                    # 记录 token 使用情况
                    usage = result.get("usage", {})
                    logger.info(
                        f"DeepSeek API 调用成功: "
                        f"prompt_tokens={usage.get('prompt_tokens', 0)}, "
                        f"completion_tokens={usage.get('completion_tokens', 0)}, "
                        f"total_tokens={usage.get('total_tokens', 0)}"
                    )

                    return content

                elif response.status_code == 429:
                    # API 限流，指数退避重试
                    wait_time = 2 ** attempt
                    logger.warning(f"DeepSeek API 限流，{wait_time}秒后重试 (第 {attempt}/{self.max_retries} 次)")
                    time.sleep(wait_time)
                    continue

                elif response.status_code >= 500:
                    # 服务器错误，重试
                    logger.warning(
                        f"DeepSeek API 服务器错误 ({response.status_code})，"
                        f"重试中 (第 {attempt}/{self.max_retries} 次)"
                    )
                    time.sleep(1)
                    continue

                else:
                    # 其他错误，直接抛出
                    error_msg = (
                        f"DeepSeek API 调用失败: status={response.status_code}"
                    )
                    logger.error(error_msg)
                    raise Exception(error_msg)

            except httpx.TimeoutException:
                logger.warning(f"DeepSeek API 请求超时，重试中 (第 {attempt}/{self.max_retries} 次)")
                if attempt == self.max_retries:
                    raise Exception(f"DeepSeek API 请求超时 (已重试 {self.max_retries} 次)")
                time.sleep(1)

            except httpx.NetworkError as exc:
                logger.warning(
                    "DeepSeek API 网络错误: error_type=%s，重试中 (第 %s/%s 次)",
                    type(exc).__name__,
                    attempt,
                    self.max_retries,
                )
                if attempt == self.max_retries:
                    raise Exception("DeepSeek API 网络错误") from None
                time.sleep(1)

            except Exception as exc:
                logger.error(
                    "DeepSeek API 调用异常: error_type=%s",
                    type(exc).__name__,
                )
                raise Exception("DeepSeek API 调用失败") from None

        # 所有重试都失败
        raise Exception(f"DeepSeek API 调用失败 (已重试 {self.max_retries} 次)")

    def summarize(
        self,
        content: str,
        max_words: int = 300,
        temperature: float = 0.7,
    ) -> str:
        """
        生成内容摘要

        Args:
            content: 需要摘要的内容
            max_words: 摘要最大字数
            temperature: 采样温度 (0-1)，越低越确定性

        Returns:
            生成的摘要

        Raises:
            ValueError: 输入内容为空
            Exception: API 调用失败

        Example:
            >>> client = DeepSeekClient()
            >>> summary = client.summarize("长文本内容...", max_words=300)
            >>> assert len(summary) <= 500
        """
        if not content or not content.strip():
            raise ValueError("摘要内容不能为空")

        # 构建 Prompt
        prompt = self._summarize_prompt.format(content=content)

        messages = [
            {"role": "user", "content": prompt}
        ]

        logger.info(f"开始生成摘要: content_length={len(content)}, max_words={max_words}")

        # 调用 API
        summary = self._call_api(
            messages=messages,
            temperature=temperature,
            max_tokens=max_words * 2,  # 预留 buffer（中文一个字符约等于 1-2 tokens）
        )

        logger.info(f"摘要生成完成: summary_length={len(summary)}")
        return summary

    def extract_tags(
        self,
        content: str,
        num_tags: int = 5,
        temperature: float = 0.3,
    ) -> List[str]:
        """
        提取内容标签

        Args:
            content: 需要提取标签的内容
            num_tags: 提取标签数量 (3-5)
            temperature: 采样温度 (0-1)，越低越确定性

        Returns:
            提取的标签列表 (3-5 个)

        Raises:
            ValueError: 输入内容为空或 num_tags 不在 3-5 范围内
            Exception: API 调用失败

        Example:
            >>> client = DeepSeekClient()
            >>> tags = client.extract_tags("文本内容...")
            >>> assert 3 <= len(tags) <= 5
        """
        if not content or not content.strip():
            raise ValueError("提取标签的内容不能为空")

        if not 3 <= num_tags <= 5:
            raise ValueError("标签数量必须在 3-5 之间")

        # 构建 Prompt
        prompt = self._extract_tags_prompt.format(content=content)

        messages = [
            {"role": "user", "content": prompt}
        ]

        logger.info(f"开始提取标签: content_length={len(content)}, num_tags={num_tags}")

        # 调用 API
        response = self._call_api(
            messages=messages,
            temperature=temperature,
            max_tokens=200,  # 标签提取不需要太多 tokens
        )

        # 解析 JSON 响应，使用多层降级策略
        try:
            # 策略 1: 尝试直接解析 JSON
            tags = json.loads(response)

            if not isinstance(tags, list):
                raise ValueError("API 返回的标签格式不是列表")

            # 过滤和验证标签
            tags = [str(tag).strip() for tag in tags if tag]

            # 确保标签数量在 3-5 之间
            if len(tags) < 3:
                logger.warning(f"提取的标签数量不足 3 个，实际: {len(tags)}")
            elif len(tags) > 5:
                logger.warning("提取的标签数量超过 5 个，截取前 5 个")
                tags = tags[:5]

            logger.info("标签提取完成: tag_count=%s", len(tags))
            return tags

        except json.JSONDecodeError:
            logger.warning("直接 JSON 解析失败: response_length=%s", len(response))

            # 策略 2: 查找 JSON 数组模式（可能包含说明文字）
            import re

            # 匹配 JSON 数组模式: ["tag1", "tag2", "tag3"]
            json_array_match = re.search(r'\[.*?\]', response, re.DOTALL)
            if json_array_match:
                try:
                    json_str = json_array_match.group(0)
                    tags = json.loads(json_str)

                    if isinstance(tags, list):
                        tags = [str(tag).strip() for tag in tags if tag]

                        if len(tags) >= 3:
                            tags = tags[:5]
                            logger.info(
                                "使用 JSON 数组模式提取标签: tag_count=%s",
                                len(tags),
                            )
                            return tags

                except json.JSONDecodeError:
                    logger.warning(
                        "JSON 数组模式解析失败: candidate_length=%s",
                        len(json_str),
                    )

            # 策略 3: 提取引号中的内容（最后的降级方案）
            matches = re.findall(r'["\']([^"\']+)["\']', response)
            if matches and len(matches) >= 3:
                tags = matches[:5]
                logger.info(
                    "使用正则提取标签（降级方案）: tag_count=%s",
                    len(tags),
                )
                return tags

            # 完全失败
            raise Exception("无法从 API 响应中提取标签")
