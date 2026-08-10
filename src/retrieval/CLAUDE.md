# Retrieval 模块

[根目录](../../CLAUDE.md) > [src](..) > **retrieval**

---

## 模块职责

**智能检索引擎**：提供 BM25 关键词检索、向量语义检索、混合检索策略与自动路由。

### 核心理念

- **多策略检索**: BM25 (精确) + Vector (语义) + Hybrid (混合)
- **智能路由**: 根据查询特征自动选择最优策略
- **统一接口**: 所有检索器返回不可隐式判真的 `SearchResponse`
- **五态合同**: `success/no_hits/invalid/error/degraded` 不得互相伪装
- **性能优化**: BM25 零成本；Embedding Provider 仅在语义分支实际执行时通过工厂构造

M13 GUI 发布搜索只保证 BM25。Vector/Hybrid/Auto 由 CLI 与 MCP adapter 显式消费；默认离线验证不会连接真实 Embedding Provider、读取真实 API key 或真实 Vault。

---

## 入口与启动

### 快速使用（自动路由）

```python
from pathlib import Path
from src.retrieval import QueryRouter

router = QueryRouter(Path("isolated.db"), Path("isolated-vectors"))

# 自动选择检索策略
response = router.search("Claude Code 使用指南")

if response.status in {"success", "degraded"}:
    for result in response.results:
        print(f"{result.title}: {result.score:.4f}")
elif response.status == "no_hits":
    print("没有命中")
else:
    print(response.status, [issue.to_dict() for issue in response.issues])
```

`SearchResponse` 故意不实现列表兼容，`if response:` 会抛出 `TypeError`。调用方必须先检查 `status`，再读取不可变的 `results` tuple。

### 手动选择策略

```python
from pathlib import Path
from src.retrieval import BM25Retriever, VectorRetriever, HybridRetriever
from src.ai.provider_factory import create_embedder
from src.utils.config import get_config

embedder_factory = lambda: create_embedder(get_config())

# 1. BM25 关键词检索（精确、快速）
bm25 = BM25Retriever(Path("isolated.db"))
bm25_response = bm25.search("Claude Code")

# 2. 向量语义检索；工厂只在 search() 真正需要时调用
vector = VectorRetriever(
    Path("isolated.db"),
    Path("isolated-vectors"),
    embedder_factory=embedder_factory,
)
vector_response = vector.search("如何使用 AI 协作编程？")

# 3. 混合检索（兼顾精确与语义）
hybrid = HybridRetriever(
    Path("isolated.db"),
    Path("isolated-vectors"),
    embedder_factory=embedder_factory,
)
hybrid_response = hybrid.search("Claude Code 工作流引擎")
```

---

## 对外接口

### QueryRouter (智能路由)

**根据查询特征自动选择检索策略**

```python
class QueryRouter:
    def __init__(
        self,
        db_path: Path,
        vector_index_dir: Path,
        embedder: Embedder | None = None,
        token_threshold: int = 5,
        *,
        embedder_factory: Callable[[], Embedder] | None = None,
    ):
        """初始化查询路由器"""

    def search(self, query: str, limit: int = 10) -> SearchResponse:
        """自动选择策略并原样返回五态结果"""

    def _select_strategy(self, query: str) -> str:
        """
        选择策略规则:
        - token 数 < threshold → BM25
        - token 数 >= threshold → Hybrid
        """
```

---

### BM25Retriever (关键词检索)

**基于 SQLite FTS5 的全文检索**

```python
class BM25Retriever:
    def __init__(self, db_path: Path):
        """初始化 BM25 检索器"""

    def search(self, query: str, limit: int = 10) -> SearchResponse:
        """
        FTS5 全文检索

        注意:
        - 查询必须经过 TextProcessor.tokenize_chinese() 分词
        - 使用 MATCH 语法，而非 LIKE
        - 返回按 BM25 相关性排序
        """
```

**特点**:
- ✅ 零 API 成本
- ✅ 精确关键词匹配
- ✅ 中文分词支持（jieba）
- ✅ 速度极快（毫秒级）

**适用场景**:
- 精确关键词查询
- 技术术语搜索
- 短查询（<5 tokens，当前 `QueryRouter` 默认阈值）

---

### VectorRetriever (语义检索)

**基于 hnswlib 的向量检索**

```python
class VectorRetriever:
    def __init__(
        self,
        db_path: Path,
        vector_index_dir: Path,
        embedder: Embedder | None = None,
        *,
        embedder_factory: Callable[[], Embedder] | None = None,
    ):
        """初始化向量检索器"""

    def search(self, query: str, limit: int = 10) -> SearchResponse:
        """
        向量语义检索

        流程:
        1. 查询文本 → Embedding (OpenAI API)
        2. hnswlib 近似最近邻搜索 (HNSW 算法)
        3. 返回相似度排序结果
        """
```

**特点**:
- ✅ 语义理解能力强
- ✅ 跨语言检索支持
- ✅ 同义词/近义词匹配
- ⚠️ 需要有效 Provider；配置/调用失败返回显式 `error`，不会被伪装为 `no_hits`

**适用场景**:
- 概念性查询
- 长文本查询（CLI/MCP 可显式选择；自动路由在 ≥5 tokens 时进入 Hybrid）
- 语义相似性搜索

---

### HybridRetriever (混合检索)

**RRF 算法融合 BM25 和向量检索结果**

```python
class HybridRetriever:
    def __init__(
        self,
        db_path: Path,
        vector_index_dir: Path,
        embedder: Embedder | None = None,
        bm25_weight: float = 0.4,
        vector_weight: float = 0.6,
        rrf_k: int = 60,
        *,
        embedder_factory: Callable[[], Embedder] | None = None,
    ):
        """初始化混合检索器"""

    def search(self, query: str, limit: int = 10) -> SearchResponse:
        """
        混合检索流程:

        1. 并行执行 BM25 和向量检索
        2. RRF (Reciprocal Rank Fusion) 融合排名:
           score = bm25_weight / (k + rank_bm25) +
                   vector_weight / (k + rank_vector)
        3. 按融合分数重新排序
        """
```

**RRF 算法参数**:
- `k = 60`: 排名平滑参数（标准值）
- `bm25_weight = 0.4`: BM25 权重
- `vector_weight = 0.6`: 向量权重

**特点**:
- ✅ 兼顾精确与语义
- ✅ 鲁棒性强
- ✅ 排名融合科学
- ⚠️ 双倍 API 成本

**适用场景**:
- 中等长度查询
- 对准确率要求高的场景
- 混合关键词+概念查询

---

### SearchResponse / SearchResult（统一输出）

所有检索器返回 `SearchResponse`，单条候选才是 `SearchResult`:

```python
@dataclass
class SearchResult:
    knowledge_id: int       # 条目 ID
    title: str              # 标题
    score: float            # 相关性分数
    highlight: str          # 高亮片段
    metadata: dict          # 来源类型、URL、标签等扩展字段

@dataclass(frozen=True)
class SearchResponse:
    status: Literal["success", "no_hits", "invalid", "error", "degraded"]
    results: tuple[SearchResult, ...]
    strategy: str
    issues: tuple[RetrievalIssue, ...]
```

- `success`：至少一条结果且无 issue。
- `no_hits`：检索正常完成但零命中，无结果、无 issue。
- `invalid`：请求非法，携带稳定 `RETRIEVAL_INVALID_QUERY` issue。
- `error`：请求未成功执行，无部分结果，携带 issue。
- `degraded`：部分能力失败，可有或没有结果，但必须携带 issue。

`RetrievalIssue` 公开稳定的 `code/message/stage/recoverable`，可选 `cause_type`；异常原文只进入私有日志，不能通过 adapter 返回。

---

## 关键依赖与配置

### 依赖模块

- `src.storage.sqlite_store.SQLiteStore`: FTS5 查询
- `src.storage.vector_store.VectorStore`: 向量索引
- `src.ai.embedder.Embedder`: 查询向量化
- `src.utils.text_utils.TextProcessor`: 中文分词
- `hnswlib`: HNSW 近似最近邻算法

### 配置项

在 `config/config.yaml` 中:

```yaml
retrieval:
  # BM25 参数
  bm25:
    k1: 1.5           # 词频饱和参数
    b: 0.75           # 文档长度归一化

  # 向量检索参数
  vector:
    top_k: 10         # 返回 Top K 结果
    ef_search: 50     # HNSW 搜索深度

  # 混合检索权重
  hybrid:
    bm25_weight: 0.4
    vector_weight: 0.6

  # 策略阈值
  strategy_thresholds:
    keyword_max_length: 2000    # 短文本用 BM25
    vector_min_length: 5000     # 长文本用向量分块
```

---

## 检索策略选择

### QueryRouter 自动路由规则

```python
def _select_strategy(self, query: str) -> str:
    token_count = len(TextProcessor.tokenize_chinese(query).split())

    if token_count < self.token_threshold:
        return "bm25"     # 短查询：精确关键词
    else:
        return "hybrid"   # 较长查询：BM25 + 语义分支
```

### 手动选择建议

| 场景 | 推荐策略 | 原因 |
|------|---------|------|
| "Claude Code" | BM25 | 精确术语 |
| "如何使用 AI 协作编程写代码？" | Hybrid | 关键词与语义共同参与 |
| "工作流引擎 步骤编排" | Hybrid | 混合关键词+概念 |
| "深度学习" | BM25 | 常见术语 |
| "什么是 Transformer 注意力机制的原理？" | Hybrid | 概念性长查询 |

---

## 性能与成本

### 性能对比

| 策略 | Provider 行为 | 当前发布边界 |
|------|---------------|--------------|
| BM25 | 不构造 Provider | GUI/CLI/MCP 可用；GUI 发布搜索只保证此策略 |
| Vector | 在执行时懒创建 Embedding Provider | CLI/MCP 显式策略 |
| Hybrid | BM25 + 懒创建的 Vector 分支 | CLI/MCP 显式策略或 ≥5 tokens 的 auto 路由 |

### 成本节省策略

短查询走 BM25，参数拒绝路径也不会创建 Provider。实际费用取决于查询分布、所选服务及其当期计价，项目不承诺固定节省比例；需要语义能力时才由 Vector/Hybrid 分支按需创建正常 Provider。

---

## 测试与质量

### 单元测试

```powershell
# 运行所有检索测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\retrieval-unit -Command @("pytest", "tests/unit", "-k", "retriev or query_router", "-v")

# 测试 BM25 检索器
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\retrieval-bm25 -Command @("pytest", "tests/unit/test_bm25_retriever.py", "-v")

# 测试向量检索器
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\retrieval-vector -Command @("pytest", "tests/unit/test_vector_retriever_contract.py", "tests/unit/test_vector_retriever_sql.py", "-v")

# 测试混合检索器
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\retrieval-hybrid -Command @("pytest", "tests/unit/test_hybrid_retriever_contract.py", "-v")

# 测试查询路由器
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\retrieval-router -Command @("pytest", "tests/unit/test_query_router_contract.py", "-v")
```

### 集成测试

真实数据端到端检索不属于 CAT-0，当前受 U1/G8 阻塞，不提供可复制命令。默认
集成回归只使用 wrapper 下的合成 fixture，并显式排除 `network/manual`。

### 测试覆盖

- ✅ `test_retrieval_result.py`: SearchResult 数据类
- ✅ `test_bm25_retriever.py`: FTS5 检索
- ✅ `test_vector_retriever_contract.py` / `test_vector_retriever_sql.py`: 向量五态与 SQL 映射
- ✅ `test_hybrid_retriever_contract.py`: RRF 分支降级合同
- ✅ `test_query_router_contract.py`: 策略路由与 lazy Provider
- ✅ `test_retrievers_integration.py`: 三种检索器集成
- ✅ `test_retrieval_integration.py`: 端到端集成

---

## 常见问题 (FAQ)

### Q1: BM25 为什么返回空结果？

**原因**: 查询未经过中文分词

**解决方案**:
```python
from src.utils.text_utils import TextProcessor

# 错误用法
query = "Claude Code"
raw_sql_match = query         # ❌ 不要绕过 Retriever 直接拼 FTS MATCH

# 正确用法
query = "Claude Code"
response = bm25.search(query)  # ✅ Retriever 内部分词并返回五态响应
```

BM25Retriever 内部已集成分词，直接传入原始查询即可。

### Q2: 向量检索相似度分数如何解释？

hnswlib 返回余弦距离，当前 adapter 使用 `1 - distance` 并夹取到 `[0, 1]`:
- `0.0`: 完全相同
- `0.2`: 非常相似
- `0.5`: 中等相似
- `0.8+`: 不相关

公开分数: `similarity = max(min(1.0 - distance, 1.0), 0.0)`；不得把底层距离直接当成最终分数。

### Q3: 如何调整混合检索的权重？

根据业务场景调整:

```python
# 偏向精确匹配
hybrid = HybridRetriever(
    db_path, vector_index_dir,
    bm25_weight=0.7,
    vector_weight=0.3,
    embedder_factory=embedder_factory,
)

# 偏向语义理解
hybrid = HybridRetriever(
    db_path, vector_index_dir,
    bm25_weight=0.3,
    vector_weight=0.7,
    embedder_factory=embedder_factory,
)
```

建议: 通过 A/B 测试优化权重。

### Q4: RRF 的 k 参数如何选择？

标准值 `k=60` 适用于大多数场景。

- `k` 越小: 排名靠前的结果权重越大
- `k` 越大: 排名靠后的结果也有机会

单分支贡献: `score += branch_weight / (k + rank)`

### Q5: 如何处理多语言查询？

多语言效果取决于用户配置的 OpenAI-compatible Embedding Provider 与模型；接口本身不承诺固定语言数量:

```python
# 中文查询，返回英文结果
response = vector.search("人工智能")
# 可能返回: "Artificial Intelligence", "Machine Learning" 等

# 英文查询，返回中文结果
response = vector.search("deep learning")
# 可能返回: "深度学习", "神经网络" 等
```

---

## 相关文件清单

### 核心文件

| 文件 | 说明 |
|------|------|
| `__init__.py` | 检索引擎模块入口 |
| `result.py` | SearchResponse / SearchResult / RetrievalIssue 五态合同 |
| `bm25_retriever.py` | BM25 关键词检索 |
| `vector_retriever.py` | 向量语义检索 |
| `hybrid_retriever.py` | RRF 混合检索 |
| `query_router.py` | 智能查询路由 |

### 测试文件

| 文件 | 说明 |
|------|------|
| `tests/unit/test_retrieval_result.py` | SearchResult 测试 |
| `tests/unit/test_bm25_retriever.py` | BM25 测试 |
| `tests/unit/test_vector_retriever_contract.py` / `test_vector_retriever_sql.py` | 向量检索合同与映射测试 |
| `tests/unit/test_hybrid_retriever_contract.py` | 混合检索合同测试 |
| `tests/unit/test_query_router_contract.py` | 路由器合同测试 |
| `tests/unit/test_retrievers_integration.py` | 集成测试 |
| `tests/integration/test_retrieval_integration.py` | 端到端测试 |

### 文档

| 文件 | 说明 |
|------|------|
| [Retrieval 检索引擎规范](../../docs/specs/interfaces/Retrieval检索引擎规范.md) | 当前检索接口与五态合同 |
| [M4 完成报告](../../docs/history/milestones/M4_COMPLETION_REPORT.md) | 历史 M4 快照 |
| [M4 问题修复记录](../../docs/history/issues/M4_RETRIEVAL_ISSUES_FIXED.md) | 历史问题记录 |

---

## 变更记录 (Changelog)

### 2026-02-16
- 生成模块级 CLAUDE.md 文档
- 添加导航面包屑
- 补充 RRF 算法和策略选择说明

### 2026-02-12 (M4)
- 完成 BM25、向量、混合检索三种策略
- 完成智能查询路由器
- 修复 FTS5 中文分词问题
- 完成所有单元测试和集成测试

---

**模块维护者**: AI Agent
**最后更新**: 2026-08-07

*本文档由 Claude Code 自动生成*
