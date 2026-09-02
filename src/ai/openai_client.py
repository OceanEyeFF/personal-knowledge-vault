"""
OpenAI-compatible Embedding API 客户端

封装 OpenAI-compatible Embedding API 调用。

注意：OpenAIClient 是历史类名，当前支持由 YAML 配置的兼容端点。
"""

from __future__ import annotations

from dataclasses import replace
import math
from threading import Lock
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit
import httpx
import numpy as np
from openai import (
    APITimeoutError,
    DefaultHttpxClient,
    OpenAI,
    OpenAIError,
    RateLimitError,
)

from src.ai.provider_factory import (
    EmbeddingProviderSettings,
    embedding_settings_from_config,
    safe_provider_usage_count,
    validate_embedding_provider_settings,
    validate_provider_base_url,
)
from src.runtime.ai_automation_policy import TokenUsage
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.utils.config import (
    get_config,
    suppress_unsafe_http_transport_logs,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)

_MAX_EMBEDDING_VECTOR_DIMENSIONS = 65_536
_INVALID_EMBEDDING_RESPONSE = "Embedding Provider 响应非法"


def _embedding_protocol_error() -> PKVRuntimeError:
    return PKVRuntimeError(
        ErrorCode.PROVIDER_PROTOCOL_FAILED,
        _INVALID_EMBEDDING_RESPONSE,
        stage="embedding_protocol",
        recoverable=True,
    )


def project_float32_cosine_vector(values: Any) -> np.ndarray:
    """Project one vector to the exact finite, non-zero domain HNSW cosine consumes."""
    try:
        with np.errstate(over="ignore", under="ignore", invalid="ignore"):
            vector = np.asarray(values, dtype=np.float32)
        if (
            vector.ndim != 1
            or not 1 <= vector.size <= _MAX_EMBEDDING_VECTOR_DIMENSIONS
            or not bool(np.all(np.isfinite(vector)))
        ):
            raise _embedding_protocol_error()
        # hnswlib's cosine path accumulates squared norms in float32.  Reject
        # vectors that would become zero or non-finite there instead of silently
        # normalizing corrupt Provider output into an unusable index entry.
        with np.errstate(over="ignore", under="ignore", invalid="ignore"):
            norm_squared = np.sum(vector * vector, dtype=np.float32)
        if not bool(np.isfinite(norm_squared)) or float(norm_squared) <= 0.0:
            raise _embedding_protocol_error()
        return vector
    except PKVRuntimeError:
        raise
    except (OverflowError, TypeError, ValueError):
        raise _embedding_protocol_error() from None


def _safe_usage_field(usage: Any, field: str) -> int | None:
    try:
        value = getattr(usage, field)
    except Exception:
        return None
    return safe_provider_usage_count(value)


def split_openai_transport_url(base_url: str) -> tuple[str, httpx.QueryParams]:
    """拆分纯 SDK base_url 与保序、可重复的 endpoint query。"""
    parsed = urlsplit(base_url)
    transport_base_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return transport_base_url, httpx.QueryParams(parsed.query)


# 保留本轮内部 helper 名称，避免已存在的调用点失效。
_split_transport_base_url = split_openai_transport_url


class OpenAIClient:
    """OpenAI-compatible Embedding 客户端（历史类名保留兼容）"""

    _AUTO_DIM_PROBE_TEXT = "__pkv_embedding_dimension_probe__"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        *,
        settings: EmbeddingProviderSettings | None = None,
        dimension_sink: Callable[[int], None] | None = None,
    ):
        """
        初始化 OpenAI 客户端

        Args:
            api_key: API Key，默认从配置中读取
            base_url: API Base URL，默认从配置中读取
            model: Embedding 模型名称，默认从 Config 读取（local.yaml > config.yaml）
            dimensions: Embedding 目标维度，默认从 Config 读取
            timeout: 请求超时时间（秒）
            max_retries: 最大重试次数
        """
        if settings is None:
            config = get_config()
            effective_settings = embedding_settings_from_config(config)
            effective_settings = replace(
                effective_settings,
                api_key=(effective_settings.api_key if api_key is None else api_key),
                base_url=(
                    effective_settings.base_url if base_url is None else base_url
                ),
                model=effective_settings.model if model is None else model,
                dimensions=(
                    effective_settings.dimensions if dimensions is None else dimensions
                ),
                timeout_seconds=(
                    effective_settings.timeout_seconds if timeout is None else timeout
                ),
                max_retries=(
                    effective_settings.max_retries
                    if max_retries is None
                    else max_retries
                ),
            )
            auto_dimensions_pending = bool(
                dimensions is None
                and getattr(config, "embedding_dim_is_auto", False) is True
                and effective_settings.dimensions is None
            )
            if auto_dimensions_pending and dimension_sink is None:
                candidate_sink = getattr(
                    config,
                    "set_runtime_embedding_dim",
                    None,
                )
                if callable(candidate_sink):
                    dimension_sink = candidate_sink
        else:
            if any(
                value is not None
                for value in (
                    api_key,
                    base_url,
                    model,
                    dimensions,
                    timeout,
                    max_retries,
                )
            ):
                raise TypeError("settings 不能与单独的 Provider 参数同时传入")
            effective_settings = settings
            auto_dimensions_pending = effective_settings.dimensions is None

        validate_embedding_provider_settings(effective_settings)
        if dimension_sink is not None and not callable(dimension_sink):
            raise TypeError("dimension_sink 必须可调用")

        self.api_key = effective_settings.api_key
        self.base_url = effective_settings.base_url
        self.model = effective_settings.model
        self.dimensions = effective_settings.dimensions
        self._auto_dimensions_pending = auto_dimensions_pending
        self._dimension_sink = dimension_sink
        self._dimension_lock = Lock()
        self._use_dimensions = self.dimensions is not None
        self.timeout = effective_settings.timeout_seconds
        self.max_retries = effective_settings.max_retries
        # The Q2 ledger reads this immediately after one embedding operation.
        # It is deliberately ``None`` when the provider omits usable token
        # metadata; missing facts must not become a fabricated zero.
        self._last_usage: TokenUsage | None = None
        self._last_usage_complete = False

        # SDK 将资源路径拼接到纯 base_url，之后再合并 endpoint query；
        # fragment 按 HTTP 语义不发送。
        suppress_unsafe_http_transport_logs()
        transport_base_url, endpoint_query = split_openai_transport_url(self.base_url)
        client_kwargs: Dict[str, Any] = {
            "api_key": self.api_key,
            "base_url": transport_base_url,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
        }
        if endpoint_query:
            client_kwargs["http_client"] = DefaultHttpxClient(params=endpoint_query)
        self.client = OpenAI(**client_kwargs)

        logger.info(
            "Provider 客户端初始化成功: "
            "component=embedding status=ready dimensions=%s",
            self.dimensions,
        )

    def embed(self, text: str) -> List[float]:
        """
        生成单个文本的 Embedding 向量

        Args:
            text: 需要向量化的文本

        Returns:
            Embedding 向量（维度由配置的 embedding_dim 决定）

        Raises:
            ValueError: 输入文本为空
            Exception: API 调用失败

        Example:
            >>> client = OpenAIClient()
            >>> embedding = client.embed("Hello, world!")
            >>> len(embedding)  # 维度取决于模型配置
        """
        if not text or not text.strip():
            raise ValueError("Embedding 文本不能为空")

        try:
            self._last_usage = None
            self._last_usage_complete = True
            logger.debug(f"开始生成 Embedding: text_length={len(text)}")

            response = self._create_embedding_response(text)

            embedding = self._project_embedding_response(
                response,
                expected_count=1,
            )[0]

            # 记录 token 使用情况
            usage = self._safe_response_usage(response)
            prompt_tokens = _safe_usage_field(usage, "prompt_tokens")
            total_tokens = _safe_usage_field(usage, "total_tokens")
            self._append_embedding_usage(prompt_tokens)
            logger.info(
                "Provider 调用成功: component=embedding "
                "prompt_tokens=%s total_tokens=%s embedding_dim=%s",
                prompt_tokens if prompt_tokens is not None else "unknown",
                total_tokens if total_tokens is not None else "unknown",
                len(embedding),
            )

            return embedding

        except RateLimitError:
            logger.error("OpenAI API 限流")
            raise Exception("OpenAI API 限流，请稍后重试") from None

        except APITimeoutError:
            logger.error("OpenAI API 超时")
            raise Exception("OpenAI API 请求超时") from None

        except OpenAIError as exc:
            logger.error("OpenAI API 错误: error_type=%s", type(exc).__name__)
            raise Exception("OpenAI API 调用失败") from None

        except Exception as exc:
            logger.error("Embedding 生成异常: error_type=%s", type(exc).__name__)
            raise

    def embed_batch(self, texts: List[str], batch_size: int = 100) -> List[List[float]]:
        """
        批量生成 Embedding 向量

        Args:
            texts: 需要向量化的文本列表
            batch_size: 批次大小（OpenAI 限制单次最多 2048 个）

        Returns:
            Embedding 向量列表

        Raises:
            ValueError: 输入列表为空
            Exception: API 调用失败

        Example:
            >>> client = OpenAIClient()
            >>> embeddings = client.embed_batch(["text1", "text2", "text3"])
            >>> assert len(embeddings) == 3
            >>> assert all(len(emb) > 0 for emb in embeddings)
        """
        if not texts:
            raise ValueError("Embedding 文本列表不能为空")

        # 过滤空文本
        valid_texts = [text for text in texts if text and text.strip()]
        if not valid_texts:
            raise ValueError("Embedding 文本列表中没有有效文本")

        logger.info(
            f"开始批量生成 Embedding: total={len(valid_texts)}, batch_size={batch_size}"
        )

        all_embeddings: List[List[float]] = []
        self._last_usage = None
        self._last_usage_complete = True

        # 分批处理
        for i in range(0, len(valid_texts), batch_size):
            batch = valid_texts[i : i + batch_size]

            try:
                logger.debug(f"处理批次 {i // batch_size + 1}: size={len(batch)}")

                response = self._create_embedding_response(batch)

                batch_embeddings = self._project_embedding_response(
                    response,
                    expected_count=len(batch),
                )
                all_embeddings.extend(batch_embeddings)

                # 记录 token 使用情况
                usage = self._safe_response_usage(response)
                prompt_tokens = _safe_usage_field(usage, "prompt_tokens")
                total_tokens = _safe_usage_field(usage, "total_tokens")
                self._append_embedding_usage(prompt_tokens)
                logger.info(
                    "Provider 批次完成: component=embedding batch=%s "
                    "prompt_tokens=%s total_tokens=%s",
                    i // batch_size + 1,
                    prompt_tokens if prompt_tokens is not None else "unknown",
                    total_tokens if total_tokens is not None else "unknown",
                )

            except RateLimitError:
                logger.error("OpenAI API 限流")
                raise Exception("OpenAI API 限流，请稍后重试") from None

            except APITimeoutError:
                logger.error("OpenAI API 超时")
                raise Exception("OpenAI API 请求超时") from None

            except OpenAIError as exc:
                logger.error("OpenAI API 错误: error_type=%s", type(exc).__name__)
                raise Exception("OpenAI API 调用失败") from None

            except Exception as exc:
                logger.error(
                    "批量 Embedding 生成异常: error_type=%s",
                    type(exc).__name__,
                )
                raise

        logger.info(f"批量 Embedding 完成: total={len(all_embeddings)}")
        return all_embeddings

    @property
    def dim(self) -> Optional[int]:
        """当前已知的向量维度；auto 模式首次成功前可能为空。"""
        return self.dimensions

    @property
    def last_usage(self) -> TokenUsage | None:
        """Known token usage from the immediately preceding embedding operation."""

        return self._last_usage

    @property
    def last_usage_complete(self) -> bool:
        """Whether every response in the preceding operation reported input use."""

        return self._last_usage_complete

    def resolve_dimensions(self) -> int:
        """解析并返回当前模型实际输出维度。"""
        if self.dimensions is not None and not self._auto_dimensions_pending:
            return self.dimensions

        try:
            response = self._create_embedding_response(self._AUTO_DIM_PROBE_TEXT)
            self._project_embedding_response(
                response,
                expected_count=1,
            )
            if self.dimensions is None:
                raise RuntimeError("Embedding 维度解析失败")
            return self.dimensions
        except PKVRuntimeError:
            raise
        except RateLimitError:
            logger.error("Embedding 维度探测遇到 API 限流")
            raise RuntimeError("Embedding 维度探测失败：API 限流") from None
        except APITimeoutError:
            logger.error("Embedding 维度探测请求超时")
            raise RuntimeError("Embedding 维度探测失败：请求超时") from None
        except OpenAIError as exc:
            logger.error(
                "Embedding 维度探测 API 错误: error_type=%s",
                type(exc).__name__,
            )
            raise RuntimeError("Embedding 维度探测失败") from None
        except Exception as exc:
            logger.error(
                "Embedding 维度探测异常: error_type=%s",
                type(exc).__name__,
            )
            raise RuntimeError("Embedding 维度探测失败") from None

    def embed_numpy(self, text: str) -> np.ndarray:
        """
        生成单个文本的 Embedding 向量（返回 numpy 数组）

        Args:
            text: 需要向量化的文本

        Returns:
            Embedding 向量（numpy 数组，shape=(dim,)，dim 由模型决定）

        Example:
            >>> client = OpenAIClient()
            >>> embedding = client.embed_numpy("Hello, world!")
            >>> embedding.ndim == 1  # 一维向量
        """
        embedding = self.embed(text)
        return np.array(embedding, dtype=np.float32)

    def embed_batch_numpy(self, texts: List[str], batch_size: int = 100) -> np.ndarray:
        """
        批量生成 Embedding 向量（返回 numpy 数组）

        Args:
            texts: 需要向量化的文本列表
            batch_size: 批次大小

        Returns:
            Embedding 向量矩阵（numpy 数组，shape=(n, dim)，dim 由模型决定）

        Example:
            >>> client = OpenAIClient()
            >>> embeddings = client.embed_batch_numpy(["text1", "text2", "text3"])
            >>> embeddings.shape[0] == 3  # n 个文本
        """
        embeddings = self.embed_batch(texts, batch_size=batch_size)
        return np.array(embeddings, dtype=np.float32)

    def _validate_embedding_dimension(self, actual_dim: int) -> None:
        """校验返回向量维度与配置一致。"""
        if not 1 <= actual_dim <= _MAX_EMBEDDING_VECTOR_DIMENSIONS:
            raise _embedding_protocol_error()
        if self._auto_dimensions_pending or self.dimensions is None:
            self._lock_detected_dimension(actual_dim)
            return

        if actual_dim != self.dimensions:
            raise _embedding_protocol_error()

    def _lock_detected_dimension(self, actual_dim: int) -> None:
        """在 auto 模式下锁定首次成功返回的向量维度。"""
        detected_dim = int(actual_dim)
        with self._dimension_lock:
            if self.dimensions is not None and not self._auto_dimensions_pending:
                if self.dimensions != detected_dim:
                    raise _embedding_protocol_error()
                return
            if self._dimension_sink is not None:
                self._dimension_sink(detected_dim)
            self.dimensions = detected_dim
            self._auto_dimensions_pending = False
            logger.info("Embedding auto 维度已锁定: dim=%s", self.dimensions)

    def _project_embedding_response(
        self,
        response: Any,
        *,
        expected_count: int,
    ) -> List[List[float]]:
        """把不可信 SDK 响应投影为与输入一一对应的有限向量。"""

        try:
            data = response.data
        except Exception:
            raise _embedding_protocol_error() from None
        if type(data) is not list or len(data) != expected_count:
            raise _embedding_protocol_error()

        vectors: List[List[float]] = []
        vector_dim: int | None = None
        for expected_index, item in enumerate(data):
            try:
                item_index = item.index
                raw_vector = item.embedding
            except Exception:
                raise _embedding_protocol_error() from None

            if type(item_index) is not int or item_index != expected_index:
                raise _embedding_protocol_error()
            if type(raw_vector) is not list:
                raise _embedding_protocol_error()

            current_dim = len(raw_vector)
            if not 1 <= current_dim <= _MAX_EMBEDDING_VECTOR_DIMENSIONS:
                raise _embedding_protocol_error()
            if vector_dim is not None and current_dim != vector_dim:
                raise _embedding_protocol_error()

            vector: List[float] = []
            for value in raw_vector:
                if type(value) not in (int, float):
                    raise _embedding_protocol_error()
                try:
                    projected_value = float(value)
                except (OverflowError, TypeError, ValueError):
                    raise _embedding_protocol_error() from None
                if not math.isfinite(projected_value):
                    raise _embedding_protocol_error()
                with np.errstate(over="ignore", invalid="ignore"):
                    float32_value = np.float32(projected_value)
                if not bool(np.isfinite(float32_value)):
                    raise _embedding_protocol_error()
                vector.append(float(float32_value))
            safe_vector = project_float32_cosine_vector(vector)
            vectors.append([float(value) for value in safe_vector])
            vector_dim = current_dim

        if vector_dim is None:
            raise _embedding_protocol_error()
        self._validate_embedding_dimension(vector_dim)
        return vectors

    @staticmethod
    def _safe_response_usage(response: Any) -> Any | None:
        try:
            return response.usage
        except Exception:
            return None

    def _append_embedding_usage(self, prompt_tokens: int | None) -> None:
        """Accumulate known embedding input tokens without guessing omissions."""

        if prompt_tokens is None:
            self._last_usage_complete = False
            return
        previous = self._last_usage
        previous_tokens = (
            previous.embedding_input_tokens
            if previous is not None and previous.source == "provider_reported"
            else 0
        )
        self._last_usage = TokenUsage(
            embedding_input_tokens=previous_tokens + prompt_tokens,
            source="provider_reported",
        )

    def _create_embedding_response(self, input_payload: str | List[str]):
        """调用 Embedding API，并在后端不支持 dimensions 时自动回退。"""
        validate_provider_base_url(self.base_url)
        request_kwargs = {
            "model": self.model,
            "input": input_payload,
        }
        if self._use_dimensions and self.dimensions is not None:
            request_kwargs["dimensions"] = self.dimensions

        try:
            return self.client.embeddings.create(**request_kwargs)
        except TypeError as e:
            if not self._should_retry_without_dimensions(e):
                raise
        except OpenAIError as e:
            if not self._should_retry_without_dimensions(e):
                raise

        logger.warning(
            "Provider 参数回退: component=embedding parameter=dimensions "
            "status=retried",
        )
        self._use_dimensions = False
        fallback_kwargs = {
            "model": self.model,
            "input": input_payload,
        }
        return self.client.embeddings.create(**fallback_kwargs)

    def _should_retry_without_dimensions(self, error: Exception) -> bool:
        """判断当前异常是否表示后端不支持 dimensions 参数。"""
        if not self._use_dimensions:
            return False

        message = str(error).lower()
        if "dimension" not in message:
            return False

        unsupported_markers = (
            "unknown parameter",
            "unsupported",
            "not supported",
            "unexpected keyword argument",
            "extra fields not permitted",
            "extra_forbidden",
        )
        return any(marker in message for marker in unsupported_markers)
