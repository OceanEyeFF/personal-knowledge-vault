# Retrieval 模块

[根目录](../../CLAUDE.md) > [src](..) > **retrieval**

---

## 模块职责

**智能检索引擎**：提供 BM25 关键词检索、向量语义检索、混合检索策略与自动路由。

### 核心理念

- **多策略检索**: BM25 (精确) + Vector (语义) + Hybrid (混合)
- **智能路由**: 根据查询特征自动选择最优策略
- **统一接口**: 所有检索器返回标准的 `SearchResult` 数据类
- **性能优化**: BM25 零成本，向量检索按需触发

---

## 入口与启动

### 快速使用（自动路由）

```python
from src.retrieval import QueryRouter

router = QueryRouter()

# 自动选择检索策略
results = await router.search("Claude Code 使用指南")

for result in results:
    print(f"{result.title}: {result.score:.4f}")
```

### 手动选择策略

```python
from src.retrieval import BM25Retriever, VectorRetriever, HybridRetriever

# 1. BM25 关键词检索（精确、快速）
bm25 = BM25Retriever(db_path=".data/db/knowledge_vault.db")
results = bm25.search("Claude Code")

# 2. 向量语义检索（语义理解强）
vector = VectorRetriever(vector_dir=".data/vectors")
results = await vector.search("如何使用 AI 协作编程？")

# 3. 混合检索（兼顾精确与语义）
hybrid = HybridRetriever(bm25, vector)
results = await hybrid.search("Claude Code 工作流引擎")
```

---

## 对外接口

### QueryRouter (智能路由)

**根据查询特征自动选择检索策略**

```python
class QueryRouter:
    def __init__(self, db_path: Path, vector_dir: Path):
        """初始化查询路由器"""

    async def search(self, query: str, top_k: int = 10) -> List[SearchResult]:
        """自动选择策略并检索"""

    def _select_strategy(self, query: str) -> str:
        """
        选择策略规则:
        - 短查询 (<10 tokens) → BM25
        - 长查询 (≥10 tokens) → Vector
        - 可强制指定 strategy="hybrid"
        """
```

---

### BM25Retriever (关键词检索)

**基于 SQLite FTS5 的全文检索**

```python
class BM25Retriever:
    def __init__(self, db_path: Path):
        """初始化 BM25 检索器"""

    def search(self, query: str, top_k: int = 10) -> List[SearchResult]:
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
- 短查询（<10 tokens）

---

### VectorRetriever (语义检索)

**基于 hnswlib 的向量检索**

```python
class VectorRetriever:
    def __init__(self, vector_dir: Path, embedder: Optional[Embedder] = None):
        """初始化向量检索器"""

    async def search(self, query: str, top_k: int = 10) -> List[SearchResult]:
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
- ⚠️ 需要 API 调用（成本）

**适用场景**:
- 概念性查询
- 长文本查询（≥10 tokens）
- 语义相似性搜索

---

### HybridRetriever (混合检索)

**RRF 算法融合 BM25 和向量检索结果**

```python
class HybridRetriever:
    def __init__(
        self,
        bm25_retriever: BM25Retriever,
        vector_retriever: VectorRetriever,
        bm25_weight: float = 0.4,
        vector_weight: float = 0.6,
        rrf_k: int = 60,
    ):
        """初始化混合检索器"""

    async def search(self, query: str, top_k: int = 10) -> List[SearchResult]:
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

### SearchResult (统一输出)

所有检索器返回的数据类:

```python
@dataclass
class SearchResult:
    knowledge_id: str       # 条目 ID
    title: str              # 标题
    source_type: str        # 来源类型
    source_url: str         # 来源 URL
    abstract: str           # 摘要
    tags: List[str]         # 标签
    score: float            # 相关性分数
    highlight: str          # 高亮片段（可选）
```

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

    if token_count < 10:
        return "bm25"     # 短查询：精确关键词
    else:
        return "vector"   # 长查询：语义理解
```

### 手动选择建议

| 场景 | 推荐策略 | 原因 |
|------|---------|------|
| "Claude Code" | BM25 | 精确术语 |
| "如何使用 AI 协作编程写代码？" | Vector | 语义理解 |
| "工作流引擎 步骤编排" | Hybrid | 混合关键词+概念 |
| "深度学习" | BM25 | 常见术语 |
| "什么是 Transformer 注意力机制的原理？" | Vector | 概念性长查询 |

---

## 性能与成本

### 性能对比

| 策略 | 查询速度 | API 成本 | 准确率 |
|------|---------|----------|--------|
| BM25 | 5-20 ms | $0 | ⭐⭐⭐ (精确) |
| Vector | 50-200 ms | $0.0001/次 | ⭐⭐⭐⭐ (语义) |
| Hybrid | 100-300 ms | $0.0001/次 | ⭐⭐⭐⭐⭐ (最佳) |

### 成本节省策略

**智能路由节省 85% API 成本**:

```
假设: 10,000 次查询/月
- 纯向量检索: 10,000 × $0.0001 = $1.00/月
- 智能路由 (70% BM25 + 30% Vector):
  7,000 × $0 + 3,000 × $0.0001 = $0.30/月
  节省: 70%

- 混合检索场景下可节省 50%
```

---

## 测试与质量

### 单元测试

```bash
# 运行所有检索测试
python -m pytest tests/unit/test_*retrieval*.py -v

# 测试 BM25 检索器
python -m pytest tests/unit/test_bm25_retriever.py -v

# 测试向量检索器
python -m pytest tests/unit/test_vector_retriever.py -v

# 测试混合检索器
python -m pytest tests/unit/test_hybrid_retriever.py -v

# 测试查询路由器
python -m pytest tests/unit/test_query_router.py -v
```

### 集成测试

```bash
# 端到端检索测试（需要真实数据）
python -m pytest tests/integration/test_retrieval_integration.py -v
```

### 测试覆盖

- ✅ `test_retrieval_result.py`: SearchResult 数据类
- ✅ `test_bm25_retriever.py`: FTS5 检索
- ✅ `test_vector_retriever.py`: 向量检索
- ✅ `test_hybrid_retriever.py`: RRF 融合算法
- ✅ `test_query_router.py`: 策略路由
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
results = bm25.search(query)  # ❌ 空结果

# 正确用法
query = "Claude Code"
tokens = TextProcessor.tokenize_chinese(query)  # "Claude Code"
results = bm25.search(tokens)  # ✅ 有结果
```

BM25Retriever 内部已集成分词，直接传入原始查询即可。

### Q2: 向量检索相似度分数如何解释？

hnswlib 返回的是 **余弦距离** (1 - 余弦相似度):
- `0.0`: 完全相同
- `0.2`: 非常相似
- `0.5`: 中等相似
- `0.8+`: 不相关

转换为相似度: `similarity = 1 - distance`

### Q3: 如何调整混合检索的权重？

根据业务场景调整:

```python
# 偏向精确匹配
hybrid = HybridRetriever(
    bm25, vector,
    bm25_weight=0.7,
    vector_weight=0.3
)

# 偏向语义理解
hybrid = HybridRetriever(
    bm25, vector,
    bm25_weight=0.3,
    vector_weight=0.7
)
```

建议: 通过 A/B 测试优化权重。

### Q4: RRF 的 k 参数如何选择？

标准值 `k=60` 适用于大多数场景。

- `k` 越小: 排名靠前的结果权重越大
- `k` 越大: 排名靠后的结果也有机会

公式: `score = 1 / (k + rank)`

### Q5: 如何处理多语言查询？

向量检索天然支持多语言（OpenAI Embedding 支持 100+ 语言）:

```python
# 中文查询，返回英文结果
results = await vector.search("人工智能")
# 可能返回: "Artificial Intelligence", "Machine Learning" 等

# 英文查询，返回中文结果
results = await vector.search("deep learning")
# 可能返回: "深度学习", "神经网络" 等
```

---

## 相关文件清单

### 核心文件

| 文件 | 说明 |
|------|------|
| `__init__.py` | 检索引擎模块入口 |
| `result.py` | SearchResult 数据类 |
| `bm25_retriever.py` | BM25 关键词检索 |
| `vector_retriever.py` | 向量语义检索 |
| `hybrid_retriever.py` | RRF 混合检索 |
| `query_router.py` | 智能查询路由 |

### 测试文件

| 文件 | 说明 |
|------|------|
| `tests/unit/test_retrieval_result.py` | SearchResult 测试 |
| `tests/unit/test_bm25_retriever.py` | BM25 测试 |
| `tests/unit/test_vector_retriever.py` | 向量检索测试 |
| `tests/unit/test_hybrid_retriever.py` | 混合检索测试 |
| `tests/unit/test_query_router.py` | 路由器测试 |
| `tests/unit/test_retrievers_integration.py` | 集成测试 |
| `tests/integration/test_retrieval_integration.py` | 端到端测试 |

### 文档

| 文件 | 说明 |
|------|------|
| [docs/refactor/Retrieval检索引擎规范.md](../../docs/refactor/Retrieval检索引擎规范.md) | 检索引擎完整设计 |
| [docs/milestones/M4_COMPLETION_REPORT.md](../../docs/milestones/M4_COMPLETION_REPORT.md) | M4 完成报告 |
| [docs/issues/M4_RETRIEVAL_ISSUES_FIXED.md](../../docs/issues/M4_RETRIEVAL_ISSUES_FIXED.md) | M4 问题修复记录 |

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
**最后更新**: 2026-02-16 01:53:22

*本文档由 Claude Code 自动生成*
