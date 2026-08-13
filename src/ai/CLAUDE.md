# AI Services 模块

[根目录](../../CLAUDE.md) > [src](..) > **ai**

---

## 模块职责

**AI 服务封装层**：提供 DeepSeek API (摘要/标签提取) 和 OpenAI API (Embedding) 的统一接口。

### 核心理念

- **服务隔离**: 每个 AI 服务独立封装，便于替换和测试
- **成本可控**: 智能缓存和策略优化，节省 API 成本
- **错误处理**: 自动重试和降级策略
- **配置驱动**: API Key 和参数通过 YAML 管理，生产调用通过统一 Provider factory 构造

W2 新增 `provider_factory.py` 与 `chat_provider.py`：Embedding 和 Chat 都从显式、不可变的配置快照构造，固定使用 `openai_compatible`，并传递 model/endpoint/timeout/retry/dimensions 等关键字段。Retrieval 的语义分支使用 lazy `embedder_factory`，BM25 或参数拒绝路径不会提前构造 Provider。默认自动化只注入 doubles，不连接真实 Provider、不读取真实 key 或真实 Vault；release 中不存在内置 fake/test mode。

---

## 入口与启动

### DeepSeek 服务

```python
from src.ai.deepseek_client import DeepSeekClient

# 初始化客户端（从 config/local.yaml 合并配置读取）
deepseek = DeepSeekClient()

# 生成摘要
summary = await deepseek.summarize(
    content="长文本内容...",
    max_words=300
)

# 提取标签
tags = await deepseek.extract_tags(content="文章内容...")
# 返回: ["技术", "AI", "编程"]

# 提取关键词
keywords = await deepseek.extract_keywords(content="文章内容...")
# 返回: ["Claude", "Code", "工作流"]
```

---

### OpenAI Embedding 服务

```python
from src.ai.embedder import Embedder

# 初始化
embedder = Embedder()

# 单个文本向量化
vector = await embedder.embed("查询文本")
# 返回: [0.123, -0.456, ...] (1536 维)

# 批量向量化
vectors = await embedder.embed_batch([
    "文本 1",
    "文本 2",
    "文本 3"
])
# 返回: [[...], [...], [...]]

# 分块向量化（长文本）
chunks_vectors = await embedder.embed_chunks(
    long_text="非常长的文本内容...",
    chunk_size=1000
)
```

---

## 对外接口

### DeepSeekClient

**摘要生成与内容分析**

```python
class DeepSeekClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = "deepseek-chat",
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        """初始化 DeepSeek 客户端"""

    async def summarize(
        self,
        content: str,
        max_words: int = 300
    ) -> str:
        """
        生成摘要

        Args:
            content: 原文内容
            max_words: 摘要最大字数

        Returns:
            摘要文本
        """

    async def extract_tags(
        self,
        content: str,
        max_tags: int = 5
    ) -> List[str]:
        """
        提取标签

        Args:
            content: 原文内容
            max_tags: 最大标签数

        Returns:
            标签列表（如 ["技术", "AI"]）
        """

    async def extract_keywords(
        self,
        content: str,
        max_keywords: int = 10
    ) -> List[str]:
        """
        提取关键词

        Args:
            content: 原文内容
            max_keywords: 最大关键词数

        Returns:
            关键词列表
        """

    async def generate_title(
        self,
        content: str
    ) -> str:
        """
        生成标题（用于无标题内容）

        Returns:
            标题文本
        """
```

---

### Embedder (OpenAI Embedding)

**文本向量化**

```python
class Embedder:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: str = "text-embedding-3-small",
        timeout: float = 30.0,
    ):
        """初始化 Embedder"""

    async def embed(self, text: str) -> List[float]:
        """
        单个文本向量化

        Returns:
            1536 维向量
        """

    async def embed_batch(
        self,
        texts: List[str],
        batch_size: int = 100
    ) -> List[List[float]]:
        """
        批量向量化

        Args:
            texts: 文本列表
            batch_size: 批次大小

        Returns:
            向量列表
        """

    async def embed_chunks(
        self,
        long_text: str,
        chunk_size: int = 1000,
        overlap: int = 100
    ) -> List[Tuple[str, List[float]]]:
        """
        长文本分块向量化

        Args:
            long_text: 长文本
            chunk_size: 分块大小（字符数）
            overlap: 重叠大小

        Returns:
            [(chunk_text, vector), ...]
        """
```

---

## 关键依赖与配置

### 依赖库

- `httpx`: 异步 HTTP 客户端
- `openai`: OpenAI 官方 SDK（可选）

### 配置文件

默认值位于 `config/config.yaml`，本机服务地址、模型和密钥写入 Git 忽略的 `config/local.yaml`：

```yaml
ai:
  # OpenAI-compatible LLM
  llm:
    provider: "openai_compatible"
    api_key: ""
    base_url: "https://api.deepseek.com/v1"
    model: "deepseek-chat"
    max_tokens: 2000
    temperature: 0.7
    timeout_seconds: 30
    max_retries: 2

  # OpenAI-compatible Embedding
  embedding:
    provider: "openai_compatible"
    api_key: ""
    base_url: "https://api.openai.com/v1"
    model: "text-embedding-3-small"
    dim: auto
    timeout_seconds: 30
    max_retries: 3

  # Whisper 配置 (Phase 2)
  whisper:
    model: "whisper-1"
```

项目不加载 `.env`，旧的 Provider 环境变量不会覆盖这些 YAML 值。

---

## Prompt 模板

### Prompt 文件位置

`src/ai/prompts/`

```
src/ai/prompts/
├── summarize.txt          # 摘要生成
├── extract_tags.txt       # 标签提取
├── extract_keywords.txt   # 关键词提取
└── generate_title.txt     # 标题生成
```

### 示例: summarize.txt

```
你是一个专业的内容分析助手。请为以下内容生成摘要。

要求:
- 字数控制在 {max_words} 字以内
- 保留核心观点和关键信息
- 使用简洁、清晰的语言
- 不要包含主观评价

原文内容:
{content}

摘要:
```

### 使用 Prompt

```python
# 加载 Prompt 模板
prompt_template = self._load_prompt("summarize.txt")

# 替换变量
prompt = prompt_template.format(
    content=content,
    max_words=300
)

# 调用 API
response = await self._call_api(prompt)
```

---

## 成本与性能

### API 成本估算

#### DeepSeek API

```
定价: $0.14 / 1M tokens (输入)
      $0.28 / 1M tokens (输出)

示例: 2000 字文章生成摘要
- 输入: ~3000 tokens × $0.14 / 1M = $0.00042
- 输出: ~500 tokens × $0.28 / 1M = $0.00014
- 总计: ~$0.00056 / 次

月度估算: 1000 次/月 × $0.00056 = $0.56/月
```

#### OpenAI Embedding API

```
定价: $0.02 / 1M tokens (text-embedding-3-small)

示例: 2000 字文章向量化
- 输入: ~3000 tokens × $0.02 / 1M = $0.00006

月度估算: 10,000 次/月 × $0.00006 = $0.60/月
```

### 性能优化策略

1. **智能缓存**: 相同内容不重复向量化
2. **批量处理**: Embedding API 支持批量请求
3. **按需触发**: AnalyzeStep 可配置为跳过已有摘要
4. **长文本策略**: 超过阈值才使用向量分块

---

## 错误处理

### 自动重试

```python
class DeepSeekClient:
    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries

    async def _call_api_with_retry(self, prompt: str):
        for attempt in range(self.max_retries):
            try:
                return await self._call_api(prompt)
            except httpx.TimeoutException:
                if attempt == self.max_retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt)  # 指数退避
```

### 降级策略

```python
# 如果 AI 服务失败，使用简单算法
try:
    summary = await deepseek.summarize(content)
except Exception as e:
    logger.warning(f"AI 服务失败，使用降级策略: {e}")
    # 简单截取前 300 字
    summary = content[:300] + "..."
```

---

## 测试与质量

### 单元测试

```powershell
# 运行 AI 服务测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\ai-unit -Command @("pytest", "tests/unit", "-k", "ai", "-v")

# 测试 DeepSeek 客户端
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\ai-deepseek -Command @("pytest", "tests/unit/test_ai_deepseek.py", "-v")

# 测试 OpenAI 客户端
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\ai-openai -Command @("pytest", "tests/unit/test_ai_openai.py", "-v")

# 测试 Embedder
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\ai-embedder -Command @("pytest", "tests/unit/test_ai_embedder.py", "-v")
```

### Mock 策略

```python
# 使用 Mock 避免真实 API 调用
from unittest.mock import AsyncMock, patch

@patch("src.ai.deepseek_client.httpx.AsyncClient")
async def test_summarize(mock_client):
    mock_client.return_value.post = AsyncMock(
        return_value=MockResponse({"choices": [{"message": {"content": "摘要内容"}}]})
    )

    deepseek = DeepSeekClient()
    summary = await deepseek.summarize("测试内容")

    assert summary == "摘要内容"
```

### 手动测试

真实 API 手动脚本会加载本机密钥并产生网络/费用，不属于 CAT-0。当前不提供可复制
命令；必须等待 U1/G8 user-only launcher、明确授权和脱敏证据流程后由用户执行。

### 测试覆盖

- ✅ `test_ai_deepseek.py`: DeepSeek 客户端单元测试
- ✅ `test_ai_openai.py`: OpenAI 客户端单元测试
- ✅ `test_ai_embedder.py`: Embedder 单元测试
- ✅ `manual_test_ai_services.py`: 真实环境手动测试

---

## 常见问题 (FAQ)

### Q1: 如何切换到其他 AI 服务？

**方案 1: 替换 API Base URL**

```python
# 使用兼容 OpenAI API 的服务
deepseek = DeepSeekClient(
    base_url="https://api.your-service.com/v1"
)
```

**方案 2: 实现新客户端**

```python
from src.ai.deepseek_client import DeepSeekClient

class MyAIClient(DeepSeekClient):
    def __init__(self):
        super().__init__(
            base_url="https://api.my-service.com",
            model="my-model"
        )
```

### Q2: Embedding 维度可以调整吗？

可以，OpenAI 提供多种模型:

```python
# text-embedding-3-small (1536 维)
embedder = Embedder(model="text-embedding-3-small")

# text-embedding-3-large (3072 维，更准确但更贵)
embedder = Embedder(model="text-embedding-3-large")

# text-embedding-ada-002 (1536 维，旧版)
embedder = Embedder(model="text-embedding-ada-002")
```

注意: 更改维度需要重建向量索引。

### Q3: 如何处理超长文本？

**DeepSeek**: 自动截断前 8000 tokens

```python
async def summarize(self, content: str, max_words: int = 300) -> str:
    # 截断到 8000 tokens
    if len(content) > 8000 * 4:  # 估算 4 字符 = 1 token
        content = content[:8000 * 4]
    ...
```

**Embedder**: 分块处理

```python
# 分块向量化
chunks_vectors = await embedder.embed_chunks(
    long_text=very_long_content,
    chunk_size=1000,
    overlap=100
)
```

### Q4: 如何调试 Prompt？

```python
# 启用日志记录
import logging
logging.basicConfig(level=logging.DEBUG)

# DeepSeekClient 会记录完整的 Prompt 和响应
deepseek = DeepSeekClient()
summary = await deepseek.summarize(content)

# 查看日志:
# [DEBUG] Prompt: ...
# [DEBUG] Response: ...
```

---

## 相关文件清单

### 核心文件

| 文件 | 说明 |
|------|------|
| `__init__.py` | AI 服务模块入口 |
| `deepseek_client.py` | DeepSeek API 客户端 |
| `openai_client.py` | OpenAI API 客户端（备用） |
| `embedder.py` | Embedding 服务封装 |
| `provider_factory.py` | W2 Provider 配置快照、严格校验与生产构造入口 |
| `chat_provider.py` | GUI Chat 的 OpenAI-compatible 流式 adapter |
| `prompts/` | Prompt 模板目录 |
| `prompts/summarize.txt` | 摘要生成 Prompt |
| `prompts/extract_tags.txt` | 标签提取 Prompt |

### 测试文件

| 文件 | 说明 |
|------|------|
| `tests/unit/test_ai_deepseek.py` | DeepSeek 单元测试 |
| `tests/unit/test_ai_openai.py` | OpenAI 单元测试 |
| `tests/unit/test_ai_embedder.py` | Embedder 单元测试 |
| `tests/unit/test_provider_factory.py` | Provider 快照、校验与构造合同 |
| `tests/unit/test_chat_provider.py` | Chat 流式 adapter 离线合同 |
| `tests/manual_test_ai_services.py` | 手动测试脚本 |

### 文档

| 文件 | 说明 |
|------|------|
| [docs/specs/interfaces/AI服务接口规范.md](../../docs/specs/interfaces/AI服务接口规范.md) | AI 服务接口规范 |
| [docs/history/milestones/MILESTONE2_COMPLETE.md](../../docs/history/milestones/MILESTONE2_COMPLETE.md) | M2 完成报告 |

---

## 变更记录 (Changelog)

### 2026-02-16
- 生成模块级 CLAUDE.md 文档
- 添加导航面包屑
- 补充成本估算和优化策略

### 2026-02-10 (M2)
- 完成 DeepSeek 客户端实现
- 完成 OpenAI Embedder 实现
- 完成 Prompt 模板管理
- 完成所有单元测试

---

**模块维护者**: AI Agent
**最后更新**: 2026-02-16 01:53:22

*本文档由 Claude Code 自动生成*
