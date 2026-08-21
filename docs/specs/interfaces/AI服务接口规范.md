# AI 服务接口规范

> **版本**: 1.0
> **创建日期**: 2026-02-15
> **文件位置**: `src/ai/`
> **作用**: 封装 OpenAI-compatible LLM 和 Embedding API 调用，提供摘要、标签提取和向量化功能

---

## 📋 核心组件

### 1. DeepSeekClient (摘要和标签提取，历史类名)

**文件**: `src/ai/deepseek_client.py`

**作用**: 封装 OpenAI-compatible Chat Completions API 调用，提供摘要生成和标签提取功能。`DeepSeekClient` 为历史类名；实际端点和模型来自调用开始时捕获的 `Config` snapshot（bundled defaults 加唯一可编辑的 `%USERPROFILE%\\.pkv\\config.yaml` 中的 `ai.llm.*`），而不是 data root 内的 runtime snapshot。

#### 构造函数

```python
def __init__(
    self,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: str = "deepseek-chat",
    timeout: float = 30.0,
    max_retries: int = 3,
    *,
    config: Any | None = None,
):
    """
    初始化 DeepSeek 客户端

    Args:
        api_key: API Key，默认从配置中读取
        base_url: OpenAI-compatible API Base URL，默认从配置中读取
        model: 使用的模型名称
        timeout: 请求超时时间（秒）
        max_retries: 最大重试次数
        config: 调用开始时捕获的不可变配置快照；省略时才走旧全局兼容路径
    """
```

#### 配置来源

| 参数 | YAML 配置键 | 默认值 |
|------|-------------|--------|
| `api_key` | `ai.llm.api_key` | 无（必填） |
| `base_url` | `ai.llm.base_url` | `https://api.deepseek.com/v1` |
| `model` | `ai.llm.model` | `"deepseek-chat"` |
| `timeout` | - | `30.0` |
| `max_retries` | - | `3` |

路径与 snapshot 合同：

- Application / Kernel 必须把同一个显式、不可变 `Config` snapshot 传给 Provider
  factory 或客户端；归档、Embedding 与索引不能在执行中回退读取过期的全局配置。
- `PKV_DATA_ROOT` 只选择数据根，优先于用户配置的 `storage.data_root`；
  它不替代上述用户业务配置。
- `<data-root>/config/local.yaml` 是 PKV 管理的无密钥 runtime snapshot，可包含
  数据库/Embedding 合同事实，但绝不包含 API key，也绝不参与业务配置合并。

#### Prompt 模板加载

```python
def _load_prompt(self, filename: str) -> str:
    """加载 Prompt 模板"""
    prompt_path = self._prompts_dir / filename
    # 从 src/ai/prompts/ 目录加载
```

**Prompt 文件**:
- `src/ai/prompts/summarize.txt` - 摘要生成 Prompt
- `src/ai/prompts/extract_tags.txt` - 标签提取 Prompt

---

### 2. DeepSeekClient 核心方法

#### summarize(content, max_words, temperature) -> str

**作用**: 生成内容摘要

**签名**:
```python
def summarize(
    self,
    content: str,
    max_words: int = 300,
    temperature: float = 0.7,
) -> str:
    """生成内容摘要"""
```

**输入**:
- `content: str` - 需要摘要的内容（必填）
- `max_words: int` - 摘要最大字数（默认 300）
- `temperature: float` - 采样温度 (0-1)，越低越确定性（默认 0.7）

**输出**:
- `str` - 生成的摘要文本

**异常**:
- `ValueError` - 输入内容为空
- `Exception` - API 调用失败（含重试）

**实现细节**:
```python
# 1. 验证输入
if not content or not content.strip():
    raise ValueError("摘要内容不能为空")

# 2. 构建 Prompt
prompt = self._summarize_prompt.format(content=content)
messages = [{"role": "user", "content": prompt}]

# 3. 调用 API
summary = self._call_api(
    messages=messages,
    temperature=temperature,
    max_tokens=max_words * 2,  # 预留 buffer（中文一个字符约等于 1-2 tokens）
)
```

**使用示例**:
```python
from src.utils.config import Config

config = Config()
client = DeepSeekClient(config=config)
summary = client.summarize("长文本内容...", max_words=300)
print(summary)
```

---

#### extract_tags(content, num_tags, temperature) -> List[str]

**作用**: 提取内容标签

**签名**:
```python
def extract_tags(
    self,
    content: str,
    num_tags: int = 5,
    temperature: float = 0.3,
) -> List[str]:
    """提取内容标签"""
```

**输入**:
- `content: str` - 需要提取标签的内容（必填）
- `num_tags: int` - 提取标签数量 (3-5)（默认 5）
- `temperature: float` - 采样温度 (0-1)（默认 0.3，比摘要更低）

**输出**:
- `List[str]` - 提取的标签列表（3-5 个）

**异常**:
- `ValueError` - 输入内容为空或 `num_tags` 不在 3-5 范围内
- `Exception` - API 调用失败或 JSON 解析失败

**实现细节**:
```python
# 1. 验证输入
if not content or not content.strip():
    raise ValueError("提取标签的内容不能为空")

if not 3 <= num_tags <= 5:
    raise ValueError("标签数量必须在 3-5 之间")

# 2. 调用 API
response = self._call_api(
    messages=[{"role": "user", "content": prompt}],
    temperature=temperature,
    max_tokens=200,  # 标签提取不需要太多 tokens
)

# 3. 解析 JSON 响应
tags = json.loads(response)  # 期望格式: ["tag1", "tag2", "tag3"]

# 4. 验证和过滤
tags = [str(tag).strip() for tag in tags if tag]
if len(tags) > 5:
    tags = tags[:5]  # 截取前 5 个
```

**降级策略**:
- 如果 JSON 解析失败，尝试正则提取引号中的内容
- 完全失败则抛出异常

**使用示例**:
```python
from src.utils.config import Config

config = Config()
client = DeepSeekClient(config=config)
tags = client.extract_tags("文本内容...", num_tags=5)
assert 3 <= len(tags) <= 5
```

---

### 3. DeepSeekClient 底层方法

#### _call_api(messages, temperature, max_tokens) -> str

**作用**: 调用 OpenAI-compatible LLM API（带重试和错误处理）

**签名**:
```python
def _call_api(
    self,
    messages: List[Dict[str, str]],
    temperature: float = 0.7,
    max_tokens: int = 2000,
) -> str:
    """调用 OpenAI-compatible LLM API"""
```

**输入**:
- `messages: List[Dict[str, str]]` - 消息列表，格式：`[{"role": "user", "content": "..."}]`
- `temperature: float` - 采样温度
- `max_tokens: int` - 最大生成 token 数

**输出**:
- `str` - API 响应内容（`choices[0].message.content`）

**重试策略**:

| HTTP 状态码 | 处理策略 | 重试 | 退避策略 |
|------------|---------|------|---------|
| `200` | 成功返回 | ❌ | - |
| `429` | API 限流 | ✅ | 指数退避（2^attempt 秒） |
| `500+` | 服务器错误 | ✅ | 固定 1 秒 |
| `4xx` (非 429) | 客户端错误 | ❌ | 直接抛出异常 |
| `TimeoutException` | 请求超时 | ✅ | 固定 1 秒 |
| `NetworkError` | 网络错误 | ✅ | 固定 1 秒 |

**日志记录**:
```python
# 成功时记录 token 使用情况
logger.info(
    f"LLM API 调用成功: "
    f"prompt_tokens={usage.get('prompt_tokens', 0)}, "
    f"completion_tokens={usage.get('completion_tokens', 0)}, "
    f"total_tokens={usage.get('total_tokens', 0)}"
)
```

---

## 🎯 Embedder (向量化)

**文件**: `src/ai/embedder.py`

**作用**: 提供文档级和分块级的向量化功能（封装 OpenAI-compatible Embedding API）

### 构造函数

```python
def __init__(
    self,
    openai_client: Optional[OpenAIClient] = None,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
):
    """
    初始化 Embedder

    Args:
        openai_client: OpenAI-compatible Embedding 客户端，默认创建新实例
        chunk_size: 分块大小（字符数）
        chunk_overlap: 分块重叠大小（字符数）
    """
```

**配置说明**:
- `chunk_size`: 分块大小（默认 500 字符）
- `chunk_overlap`: 分块重叠（默认 50 字符，10% 重叠）

---

### Embedder 核心方法

#### 1. embed_document(text) -> np.ndarray

**作用**: 生成文档级 Embedding（整篇文档的向量表示）

**签名**:
```python
def embed_document(self, text: str) -> np.ndarray:
    """生成文档级 Embedding（整篇文档的向量表示）"""
```

**输入**:
- `text: str` - 文档文本

**输出**:
- `np.ndarray` - 文档向量（shape=(dim,)）

**异常**:
- `ValueError` - 文本为空
- `Exception` - 向量化失败

**处理逻辑**:
```python
# 1. 短文档（< 8000 字符）
if len(text) <= 8000:
    vector = self.client.embed_numpy(text)  # 直接向量化

# 2. 长文档（>= 8000 字符）
else:
    vector = self._embed_long_document(text)  # 分块后取平均
```

**长文档处理** (`_embed_long_document`):
```python
# 1. 分块
chunks = split_text_into_chunks(text, chunk_size=500, chunk_overlap=50)

# 2. 批量向量化
chunk_vectors = self.client.embed_batch_numpy(chunks)

# 3. 取平均向量
avg_vector = np.mean(chunk_vectors, axis=0)
```

**注意**:
- ⚠️ OpenAI-compatible Embedding API 通常存在输入长度限制；OpenAI 官方服务约 8191 tokens（约 8000 字符）
- ⚠️ 长文档会被分块并平均，可能损失部分语义信息
- ⚠️ `dim` 不再固定为 `1536`，而是取决于当前 Embedding 模型的真实输出维度
- ⚠️ 当 `ai.embedding.dim: auto` 时，客户端会在首次成功请求后锁定真实维度
- ⚠️ `ai.embedding.base_url`、`ai.embedding.model`、`ai.embedding.dim` 是向量索引契约；更换后必须重建索引并重新生成文档级、分块级 Embedding

---

#### 2. embed_chunks(text, return_chunks) -> Tuple[np.ndarray, Optional[List[str]]]

**作用**: 生成分块级 Embedding（每个分块的向量表示）

**签名**:
```python
def embed_chunks(
    self, text: str, return_chunks: bool = False
) -> Tuple[np.ndarray, Optional[List[str]]]:
    """生成分块级 Embedding"""
```

**输入**:
- `text: str` - 文档文本
- `return_chunks: bool` - 是否返回分块文本（默认 False）

**输出**:
- `Tuple[np.ndarray, Optional[List[str]]]`
  - 分块向量矩阵：shape=(num_chunks, dim)
  - 分块文本列表：如果 `return_chunks=True` 则返回，否则返回 `None`

**实现**:
```python
# 1. 分块
chunks = split_text_into_chunks(text, chunk_size=500, chunk_overlap=50)

# 2. 批量向量化
chunk_vectors = self.client.embed_batch_numpy(chunks)

# 3. 返回结果
if return_chunks:
    return chunk_vectors, chunks
else:
    return chunk_vectors, None
```

**使用场景**:
- 向量存储（VectorStore）需要分块向量
- 长文档的细粒度检索
- 在 `auto` 模式下，调用方应以运行期解析出的 `dim` 为准初始化向量索引

---

#### 3. embed_batch_documents(texts) -> np.ndarray

**作用**: 批量生成文档级 Embedding

**签名**:
```python
def embed_batch_documents(self, texts: List[str]) -> np.ndarray:
    """批量生成文档级 Embedding"""
```

**输入**:
- `texts: List[str]` - 文档文本列表

**输出**:
- `np.ndarray` - 文档向量矩阵（shape=(num_docs, dim)，其中 `dim` 为当前 Embedding 模型的真实维度）

**处理逻辑**:
```python
# 1. 过滤空文本
valid_texts = [text for text in texts if text and text.strip()]

# 2. 处理长文档（截断）
processed_texts = []
for text in valid_texts:
    if len(text) > 8000:
        logger.warning(f"文档过长 ({len(text)} 字符)，截取前 8000 字符")
        processed_texts.append(text[:8000])
    else:
        processed_texts.append(text)

# 3. 批量向量化
vectors = self.client.embed_batch_numpy(processed_texts)
```

**注意**:
- ⚠️ 长文档会被截断（不是取平均），可能损失尾部信息
- ⚠️ 与 `embed_document` 的长文档处理策略不一致！

---

#### 4. cosine_similarity(vector1, vector2) -> float

**作用**: 计算两个向量的余弦相似度

**签名**:
```python
def cosine_similarity(
    self, vector1: np.ndarray, vector2: np.ndarray
) -> float:
    """计算两个向量的余弦相似度"""
```

**输入**:
- `vector1: np.ndarray` - 向量 1（shape=(dim,)）
- `vector2: np.ndarray` - 向量 2（shape=(dim,)）

**输出**:
- `float` - 余弦相似度（-1 到 1）

**实现**:
```python
# 1. 归一化
v1_norm = vector1 / np.linalg.norm(vector1)
v2_norm = vector2 / np.linalg.norm(vector2)

# 2. 计算余弦相似度
similarity = np.dot(v1_norm, v2_norm)
```

---

#### 5. batch_cosine_similarity(query_vector, vectors) -> np.ndarray

**作用**: 批量计算查询向量与向量集的余弦相似度

**签名**:
```python
def batch_cosine_similarity(
    self, query_vector: np.ndarray, vectors: np.ndarray
) -> np.ndarray:
    """批量计算查询向量与向量集的余弦相似度"""
```

**输入**:
- `query_vector: np.ndarray` - 查询向量（shape=(dim,)）
- `vectors: np.ndarray` - 向量矩阵（shape=(n, dim)）

**输出**:
- `np.ndarray` - 相似度数组（shape=(n,)）

**实现**:
```python
# 1. 归一化查询向量
query_norm = query_vector / np.linalg.norm(query_vector)

# 2. 归一化文档向量矩阵
vectors_norm = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

# 3. 批量计算余弦相似度（矩阵点积）
similarities = np.dot(vectors_norm, query_norm)
```

**性能优势**:
- 比循环调用 `cosine_similarity` 快得多（向量化运算）
- 用于检索排序

---

## 🔗 依赖关系

### Embedder → OpenAIClient

```python
# Embedder 依赖 OpenAIClient
from src.ai.openai_client import OpenAIClient

class Embedder:
    def __init__(self, openai_client: Optional[OpenAIClient] = None, ...):
        self.client = openai_client or OpenAIClient()
```

**OpenAIClient 提供的方法**:
- `embed_numpy(text: str) -> np.ndarray` - 单个文本向量化
- `embed_batch_numpy(texts: List[str]) -> np.ndarray` - 批量文本向量化

---

## ⚠️ 已知问题和改进建议

### 问题 1: 长文档处理策略不一致

**问题描述**:
- `embed_document` 对长文档**分块后取平均**
- `embed_batch_documents` 对长文档**直接截断前 8000 字符**

**影响范围**: 中 - 长文档的向量表示不一致

**优先级**: 中

**建议修复**: 统一为分块取平均策略

---

### 问题 2: DeepSeek 标签 JSON 解析脆弱

**问题描述**:
- 依赖 API 返回严格的 JSON 格式
- 如果 API 返回格式稍有偏差（如包含说明文字），解析会失败

**示例**:
```json
// API 返回（失败）
"这是提取的标签：[\"tag1\", \"tag2\", \"tag3\"]"

// 期望返回（成功）
["tag1", "tag2", "tag3"]
```

**影响范围**: 中 - 可能导致标签提取失败

**优先级**: 中

**建议修复**: 改进 Prompt，明确要求只返回 JSON 数组

---

### 问题 3: Embedder 缺少批量分块向量化

**问题描述**:
- `embed_chunks` 只支持单个文档
- 缺少批量文档分块向量化方法

**影响范围**: 低 - 性能优化需求

**优先级**: 低

**建议修复**: 添加 `embed_batch_chunks` 方法

---

### 问题 4: 重试策略硬编码

**问题描述**:
- 重试次数和退避策略硬编码在 `_call_api` 中
- 无法针对不同方法配置不同重试策略

**影响范围**: 低 - 灵活性不足

**优先级**: 低

**建议修复**: 提取重试装饰器，支持配置化

---

### 问题 5: 缺少 API 调用成本追踪

**问题描述**:
- 只记录单次调用的 token 使用情况
- 缺少累计成本统计

**影响范围**: 低 - 成本监控需求

**优先级**: 低

**建议修复**: 添加全局计数器
```python
class DeepSeekClient:
    _total_tokens = 0
    _total_requests = 0

    @classmethod
    def get_usage_stats(cls):
        return {
            "total_requests": cls._total_requests,
            "total_tokens": cls._total_tokens
        }
```

---

### 问题 6: Embedding 索引迁移仍需人工执行

**问题描述**:
- 当前模型、端点、维度由 bundled `config/config.yaml` 与用户
  `%USERPROFILE%\\.pkv\\config.yaml` 合并后的同一 `Config` snapshot 显式配置
- 新索引会记录非敏感契约指纹，加载时会拒绝复用不匹配索引
- 但系统不会自动删除旧索引或自动重算已有 Embedding

**影响范围**: 中 - 切换 Embedding 配置后需要维护者执行索引重建和回填

**优先级**: 中

**建议**: 更换 `ai.embedding.base_url`、`ai.embedding.model` 或 `ai.embedding.dim` 时，按索引迁移流程重建向量索引并重新生成 Embedding

---

## 🎯 总结

### 设计优点

✅ 清晰的职责分离（OpenAI-compatible LLM 文本生成、OpenAI-compatible Embedding 向量化）
✅ 完善的重试和错误处理机制
✅ 灵活的向量化方式（文档级、分块级、批量）
✅ 丰富的日志记录
✅ Prompt 模板化管理

### 需要改进

⚠️ 长文档处理策略不一致
⚠️ JSON 解析脆弱（标签提取）
⚠️ 缺少批量分块向量化
⚠️ 重试策略硬编码
⚠️ 缺少 API 成本追踪
⚠️ Embedding 索引迁移仍需人工执行

---

## 📝 使用示例

### DeepSeek 使用示例

```python
from src.ai.deepseek_client import DeepSeekClient
from src.utils.config import Config

# 1. 初始化客户端
config = Config()
client = DeepSeekClient(config=config)

# 2. 生成摘要
summary = client.summarize(
    content="长文本内容...",
    max_words=300,
    temperature=0.7
)
print(f"摘要: {summary}")

# 3. 提取标签
tags = client.extract_tags(
    content="文本内容...",
    num_tags=5,
    temperature=0.3
)
print(f"标签: {tags}")
```

### Embedder 使用示例

```python
from src.ai.provider_factory import create_embedder
from src.utils.config import Config

# 1. 初始化
config = Config()
embedder = create_embedder(config)

# 2. 文档级向量化
doc_vector = embedder.embed_document("这是一篇文档")
print(f"向量维度: {doc_vector.shape}")  # (dim,)

# 3. 分块级向量化
chunk_vectors, chunks = embedder.embed_chunks(
    "这是一篇长文档...",
    return_chunks=True
)
print(f"分块数: {len(chunks)}, 向量维度: {chunk_vectors.shape}")

# 4. 计算相似度
similarity = embedder.cosine_similarity(doc_vector, doc_vector)
print(f"自身相似度: {similarity}")  # 应接近 1.0
```

---

**文档维护者**: AI Agent
**最后更新**: 2026-08-21
