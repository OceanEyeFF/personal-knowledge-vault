"""
OpenAI API 客户端

封装 OpenAI Embedding API 调用
"""

from typing import List, Optional
import numpy as np
from openai import OpenAI, OpenAIError, RateLimitError, APITimeoutError

from src.utils.config import get_config
from src.utils.logger import get_logger

logger = get_logger(__name__)


class OpenAIClient:
    """OpenAI API 客户端（专注于 Embedding 功能）"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = "text-embedding-ada-002",
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        """
        初始化 OpenAI 客户端

        Args:
            api_key: API Key，默认从配置中读取
            base_url: API Base URL，默认从配置中读取
            model: Embedding 模型名称
            timeout: 请求超时时间（秒）
            max_retries: 最大重试次数
        """
        config = get_config()

        self.api_key = api_key or config.openai_api_key
        if not self.api_key:
            raise ValueError("OpenAI API Key 未配置，请设置环境变量 OPENAI_API_KEY")

        self.base_url = base_url or config.openai_base_url
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

        # 初始化 OpenAI 客户端
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

        logger.info(f"OpenAI 客户端初始化成功: model={model}, base_url={self.base_url}")

    def embed(self, text: str) -> List[float]:
        """
        生成单个文本的 Embedding 向量

        Args:
            text: 需要向量化的文本

        Returns:
            Embedding 向量（1536 维）

        Raises:
            ValueError: 输入文本为空
            Exception: API 调用失败

        Example:
            >>> client = OpenAIClient()
            >>> embedding = client.embed("Hello, world!")
            >>> assert len(embedding) == 1536
        """
        if not text or not text.strip():
            raise ValueError("Embedding 文本不能为空")

        try:
            logger.debug(f"开始生成 Embedding: text_length={len(text)}")

            response = self.client.embeddings.create(
                model=self.model,
                input=text,
            )

            embedding = response.data[0].embedding

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
            >>> assert all(len(emb) == 1536 for emb in embeddings)
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

                response = self.client.embeddings.create(
                    model=self.model,
                    input=batch,
                )

                # 提取 embedding
                batch_embeddings = [item.embedding for item in response.data]
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

    def embed_numpy(self, text: str) -> np.ndarray:
        """
        生成单个文本的 Embedding 向量（返回 numpy 数组）

        Args:
            text: 需要向量化的文本

        Returns:
            Embedding 向量（numpy 数组，shape=(1536,)）

        Example:
            >>> client = OpenAIClient()
            >>> embedding = client.embed_numpy("Hello, world!")
            >>> assert embedding.shape == (1536,)
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
            Embedding 向量矩阵（numpy 数组，shape=(n, 1536)）

        Example:
            >>> client = OpenAIClient()
            >>> embeddings = client.embed_batch_numpy(["text1", "text2", "text3"])
            >>> assert embeddings.shape == (3, 1536)
        """
        embeddings = self.embed_batch(texts, batch_size=batch_size)
        return np.array(embeddings, dtype=np.float32)
