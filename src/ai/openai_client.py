"""
OpenAI API 客户端

封装 OpenAI Embedding API 调用
"""

from __future__ import annotations

from typing import List, Optional
import numpy as np
from openai import OpenAI, OpenAIError, RateLimitError, APITimeoutError

from src.utils.config import get_config
from src.utils.logger import get_logger

logger = get_logger(__name__)


class OpenAIClient:
    """OpenAI API 客户端（专注于 Embedding 功能）"""

    _AUTO_DIM_PROBE_TEXT = "__pkv_embedding_dimension_probe__"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        """
        初始化 OpenAI 客户端

        Args:
            api_key: API Key，默认从配置中读取
            base_url: API Base URL，默认从配置中读取
            model: Embedding 模型名称，默认从 Config 读取（环境变量 > config.yaml > 内置默认）
            dimensions: Embedding 目标维度，默认从 Config 读取
            timeout: 请求超时时间（秒）
            max_retries: 最大重试次数
        """
        config = get_config()
        self._config = config

        self.api_key = api_key or config.openai_api_key
        if not self.api_key:
            raise ValueError("OpenAI API Key 未配置，请设置环境变量 OPENAI_API_KEY")

        self.base_url = base_url or config.openai_base_url
        self.model = model or config.openai_embedding_model
        configured_dimensions = dimensions if dimensions is not None else config.embedding_dim
        self.dimensions = int(configured_dimensions) if configured_dimensions is not None else None
        self._auto_dimensions_pending = (
            dimensions is None
            and getattr(config, "embedding_dim_is_auto", False)
            and self.dimensions is None
        )
        self._use_dimensions = self.dimensions is not None
        self.timeout = timeout
        self.max_retries = max_retries

        # 初始化 OpenAI 客户端
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

        logger.info(
            "OpenAI 客户端初始化成功: "
            f"model={self.model}, base_url={self.base_url}, dimensions={self.dimensions}"
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
            logger.debug(f"开始生成 Embedding: text_length={len(text)}")

            response = self._create_embedding_response(text)

            embedding = response.data[0].embedding
            self._validate_embedding_dimension(len(embedding))

            # 记录 token 使用情况
            usage = response.usage
            logger.info(
                f"OpenAI Embedding 成功: "
                f"prompt_tokens={usage.prompt_tokens}, "
                f"total_tokens={usage.total_tokens}, "
                f"embedding_dim={len(embedding)}"
            )

            return embedding

        except RateLimitError as e:
            logger.error(f"OpenAI API 限流: {e}")
            raise Exception(f"OpenAI API 限流，请稍后重试: {e}")

        except APITimeoutError as e:
            logger.error(f"OpenAI API 超时: {e}")
            raise Exception(f"OpenAI API 请求超时: {e}")

        except OpenAIError as e:
            logger.error(f"OpenAI API 错误: {e}")
            raise Exception(f"OpenAI API 调用失败: {e}")

        except Exception as e:
            logger.error(f"Embedding 生成异常: {e}")
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

        logger.info(f"开始批量生成 Embedding: total={len(valid_texts)}, batch_size={batch_size}")

        all_embeddings: List[List[float]] = []

        # 分批处理
        for i in range(0, len(valid_texts), batch_size):
            batch = valid_texts[i : i + batch_size]

            try:
                logger.debug(f"处理批次 {i // batch_size + 1}: size={len(batch)}")

                response = self._create_embedding_response(batch)

                # 提取 embedding
                batch_embeddings = [item.embedding for item in response.data]
                for embedding in batch_embeddings:
                    self._validate_embedding_dimension(len(embedding))
                all_embeddings.extend(batch_embeddings)

                # 记录 token 使用情况
                usage = response.usage
                logger.info(
                    f"批次 {i // batch_size + 1} 完成: "
                    f"prompt_tokens={usage.prompt_tokens}, "
                    f"total_tokens={usage.total_tokens}"
                )

            except RateLimitError as e:
                logger.error(f"OpenAI API 限流: {e}")
                raise Exception(f"OpenAI API 限流，请稍后重试: {e}")

            except APITimeoutError as e:
                logger.error(f"OpenAI API 超时: {e}")
                raise Exception(f"OpenAI API 请求超时: {e}")

            except OpenAIError as e:
                logger.error(f"OpenAI API 错误: {e}")
                raise Exception(f"OpenAI API 调用失败: {e}")

            except Exception as e:
                logger.error(f"批量 Embedding 生成异常: {e}")
                raise

        logger.info(f"批量 Embedding 完成: total={len(all_embeddings)}")
        return all_embeddings

    @property
    def dim(self) -> Optional[int]:
        """当前已知的向量维度；auto 模式首次成功前可能为空。"""
        return self.dimensions

    def resolve_dimensions(self) -> int:
        """解析并返回当前模型实际输出维度。"""
        if self.dimensions is not None and not self._auto_dimensions_pending:
            return self.dimensions

        response = self._create_embedding_response(self._AUTO_DIM_PROBE_TEXT)
        embedding = response.data[0].embedding
        self._validate_embedding_dimension(len(embedding))
        if self.dimensions is None:
            raise RuntimeError("Embedding 维度解析失败")
        return self.dimensions

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
        if self._auto_dimensions_pending or self.dimensions is None:
            self._lock_detected_dimension(actual_dim)
            return

        if actual_dim != self.dimensions:
            raise ValueError(
                f"Embedding 维度不匹配: expected={self.dimensions}, actual={actual_dim}"
            )

    def _lock_detected_dimension(self, actual_dim: int) -> None:
        """在 auto 模式下锁定首次成功返回的向量维度。"""
        self.dimensions = int(actual_dim)
        self._auto_dimensions_pending = False
        if hasattr(self._config, "set_runtime_embedding_dim"):
            self._config.set_runtime_embedding_dim(self.dimensions)
        logger.info("Embedding auto 维度已锁定: dim=%s", self.dimensions)

    def _create_embedding_response(self, input_payload: str | List[str]):
        """调用 Embedding API，并在后端不支持 dimensions 时自动回退。"""
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
            "Embedding 后端不支持 dimensions 参数，已回退为不传该参数: model=%s",
            self.model,
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
