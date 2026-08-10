# Retrieval 检索引擎规范（M4 核心）

> **版本**: 1.1
> **创建日期**: 2026-02-15
> **最后更新**: 2026-08-07（M13 W2 契约对齐）
> **文件位置**: `src/retrieval/`
> **作用**: M4 检索引擎的核心组件和接口规范

> **M13 发布边界**：所有检索器统一返回五态 `SearchResponse`，不得把失败伪装成空列表。GUI 发布面只保证 BM25；CLI/MCP 可显式选择向量或混合检索，并在真正进入语义分支时才按需创建 Provider。默认验证必须离线运行，不使用真实 Provider、密钥或 Vault 数据。

---

## 🎯 架构概览

### 检索策略

| 策略 | 适用场景 | 实现类 | 算法 |
|------|---------|--------|------|
| **BM25** | 短查询（< 5 tokens） | `BM25Retriever` | SQLite FTS5 |
| **向量检索** | 长查询（≥ 5 tokens）单独使用 | `VectorRetriever` | hnswlib + OpenAI-compatible Embedding |
| **混合检索** | 长查询（≥ 5 tokens）默认 | `HybridRetriever` | BM25 + 向量 + RRF 融合 |
| **智能路由** | 自动选择策略 | `QueryRouter` | 基于分词数量 |

---

## 📦 核心组件

### 1. SearchResult (统一结果格式)

**文件**: `src/retrieval/result.py`

```python
@dataclass(frozen=True)
class SearchResult:
    """搜索结果数据类"""
    knowledge_id: int        # 知识条目 ID
    title: str               # 标题
    score: float             # 相关性分数 (0.0-1.0)
    highlight: str           # 摘要或高亮片段
    metadata: Dict[str, Any] # 额外元数据
```

**约束**:
- `score` 必须在 `[0.0, 1.0]` 范围内
- 使用 `frozen=True` 确保不可变性

#### SearchResponse（统一结果信封）

```python
SearchStatus = Literal["success", "no_hits", "invalid", "error", "degraded"]

@dataclass(frozen=True)
class SearchResponse:
    status: SearchStatus
    results: tuple[SearchResult, ...] = ()
    strategy: str = "unknown"
    issues: tuple[RetrievalIssue, ...] = ()
```

状态语义：

| 状态 | 语义 | 结果 / issue 约束 |
|------|------|-------------------|
| `success` | 检索成功且命中 | 至少一条 `results`，无 `issues` |
| `no_hits` | 检索成功但无命中 | 无结果、无 issue |
| `invalid` | 查询或 limit 无效 | 无结果，至少一个 `RETRIEVAL_INVALID_QUERY` issue |
| `error` | 请求策略整体失败 | 无结果，至少一个 issue |
| `degraded` | 仅部分检索分支可用 | 可含部分结果，至少一个 issue |

`SearchResponse` 不是列表，也禁止隐式真值判断；调用方必须先检查 `status`，再读取 `results`。`RetrievalIssue` 只暴露稳定错误码、公开消息、阶段与可恢复性，不向响应泄露底层异常文本。

---

### 2. QueryRouter (智能路由)

**文件**: `src/retrieval/query_router.py`

#### 路由规则

```python
# 分词
tokenized = TextProcessor.tokenize_chinese(query)
tokens = tokenized.split()
token_count = len(tokens)

# 决策
if token_count < 5:
    return BM25Retriever.search(query, limit)  # 短查询 → BM25
else:
    return HybridRetriever.search(query, limit)  # 长查询 → 混合检索
```

**参数**:
- `token_threshold: int = 5` - 分词数阈值（可配置）

**示例**:
```python
# 短查询 (3 tokens)
"人工智能 应用" → BM25 检索

# 长查询 (8 tokens)
"如何使用 Python 实现一个简单的神经网络模型" → 混合检索
```

---

### 3. BM25Retriever (关键词检索)

**文件**: `src/retrieval/bm25_retriever.py`

#### 核心方法

```python
def search(self, query: str, limit: int = 10) -> SearchResponse:
    """执行 BM25 关键词检索"""
```

#### 实现流程

1. **分词**: 使用 jieba 分词
   ```python
   match_query = self._build_match_query(query)
   # "人工智能" → "人工 智能" (空格连接)
   ```

2. **FTS5 查询**:
   ```sql
   SELECT
       ki.knowledge_id,
       ki.title,
       bm25(knowledge_items_fts) as bm25_score,
       snippet(knowledge_items_fts, 0, '...', '...', '', 64) as snippet
   FROM knowledge_items ki
   JOIN knowledge_items_fts ON ki.knowledge_id = knowledge_items_fts.rowid
   WHERE knowledge_items_fts MATCH ?
   ORDER BY bm25_score ASC  -- FTS5 的 BM25 分数越小越相关
   LIMIT ?
   ```

3. **分数归一化**:
   ```python
   normalized_score = self._normalize_score(raw_score, rank)
   # 转换到 [0.0, 1.0]，排名第一的分数最高
   ```

#### 关键特性

- ✅ 中文分词（jieba）
- ✅ 空格连接分词结果（FTS5 要求）
- ✅ BM25 算法（SQLite FTS5 内置）
- ✅ 分数归一化

---

### 4. VectorRetriever (向量检索)

**文件**: `src/retrieval/vector_retriever.py`

#### 核心方法

```python
def search(self, query: str, limit: int = 10) -> SearchResponse:
    """执行向量语义检索"""
```

#### 实现流程

1. **查询向量化**:
   ```python
   query_vector = self.embedder.embed_document(query)
   ```

2. **hnswlib 近似最近邻搜索**:
   ```python
   labels, distances = self.index.knn_query(query_vector, k=limit)
   ```

3. **分数转换**:
   ```python
   # 距离 → 相似度
   similarity = max(min(1.0 - distance, 1.0), 0.0)
   ```

4. **查询元数据**:
   ```python
   # 通过 knowledge_id 查询 SQLite 获取标题、摘要等
   ```

#### 关键特性

- ✅ OpenAI-compatible Embedding（维度取决于当前模型，常见默认值为 1536）
- ✅ 向量索引绑定 `ai.embedding.base_url` / `ai.embedding.model` / `ai.embedding.dim` 契约，配置漂移时拒绝静默复用旧索引
- ✅ hnswlib 索引（HNSW 算法）
- ✅ 余弦距离 → 相似度转换
- ✅ 支持 `embedder_factory` 懒创建；纯 BM25 路径不触发 Provider 初始化

---

### 5. HybridRetriever (混合检索)

**文件**: `src/retrieval/hybrid_retriever.py`

#### RRF 融合算法

**Reciprocal Rank Fusion (RRF)**:

```python
# 对于每个文档 d
RRF_score(d) = Σ branch_weight / (k + rank(d))

# 其中:
# - k = 60 (RRF 常数)
# - rank(d) 是文档在某个结果列表中的排名（从 1 开始）
```

#### 实现流程

1. **并行检索**:
   ```python
   with ThreadPoolExecutor(max_workers=2) as executor:
       future_bm25 = executor.submit(bm25_retriever.search, query, limit*2)
       future_vector = executor.submit(vector_retriever.search, query, limit*2)

       bm25_response = future_bm25.result()
       vector_response = future_vector.result()
   ```

2. **RRF 融合**:
   ```python
   def _compute_rrf_scores(self, bm25_results, vector_results):
       scores = {}
       bm25_ranks = {
           result.knowledge_id: rank
           for rank, result in enumerate(bm25_results, start=1)
       }
       vector_ranks = {
           result.knowledge_id: rank
           for rank, result in enumerate(vector_results, start=1)
       }

       all_ids = (
           {r.knowledge_id for r in bm25_results}
           | {r.knowledge_id for r in vector_results}
       )
       for knowledge_id in all_ids:
           score = 0.0
           if knowledge_id in bm25_ranks:
               score += self.bm25_weight / (self.rrf_k + bm25_ranks[knowledge_id])
           if knowledge_id in vector_ranks:
               score += self.vector_weight / (self.rrf_k + vector_ranks[knowledge_id])
           scores[knowledge_id] = score

       return scores
   ```

3. **去重和排序**:
   ```python
   # 按 RRF 分数降序排列
   sorted_results = sorted(merged_results, key=lambda x: x.score, reverse=True)
   return sorted_results[:limit]
   ```

#### 关键参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `bm25_weight` | 0.4 | BM25 分支的加权 RRF 权重 |
| `vector_weight` | 0.6 | 向量分支的加权 RRF 权重 |
| `rrf_k` | 60 | RRF 常数（标准值） |

**分支状态聚合**：两个分支都健康时返回 `success` / `no_hits`；一个分支为 `invalid`、`error` 或 `degraded` 时，保留健康分支结果并返回 `degraded`；两个分支都不可用时返回 `invalid`（均为输入无效）或 `error`。issue 顺序固定为 BM25、Vector，不受线程完成顺序影响。

---

## 🔄 数据流

### 检索流程

```
用户查询
    ↓
QueryRouter.search()
    ↓
分词 (jieba) → token_count
    ↓
    ├─ token_count < 5  → BM25Retriever.search()
    │                        ↓
    │                     FTS5 MATCH → BM25 分数 → 归一化
    │
    └─ token_count >= 5 → HybridRetriever.search()
                             ↓
                          ┌──────────────┬──────────────┐
                          ↓              ↓              ↓
                    BM25检索      向量检索         并行执行
                          ↓              ↓
                      RRF 融合 (k=60)
                          ↓
                    排序 + 去重 + 截断
                          ↓
       SearchResponse(status, results, strategy, issues)
```

---

## ⚙️ 配置和初始化

### 初始化示例

```python
from pathlib import Path
from src.ai.provider_factory import create_embedder
from src.retrieval.query_router import QueryRouter
from src.utils.config import get_config

# 1. 声明懒工厂；短查询的 BM25 路径不会调用它
embedder_factory = lambda: create_embedder(get_config())

# 2. 创建 QueryRouter
router = QueryRouter(
    db_path=Path(".data/pkv.db"),
    vector_index_dir=Path(".data/vectors"),
    embedder_factory=embedder_factory,
    token_threshold=5  # 可配置
)

# 3. 执行检索
response = router.search("如何使用向量检索", limit=10)

# 4. 先判断五态，再访问结果
if response.status in {"invalid", "error"}:
    for issue in response.issues:
        print(issue.code.value, issue.message)

for result in response.results:
    print(f"ID: {result.knowledge_id}")
    print(f"标题: {result.title}")
    print(f"分数: {result.score:.3f}")
    print(f"摘要: {result.highlight}")
    print(f"元数据: {result.metadata}")
```

---

## ⚠️ 已知问题

### 问题 1: 加权 RRF 的同分与元数据合并仍需强化

**问题**: 当前已使用 `bm25_weight` / `vector_weight`，但精确同分排序与冲突元数据合并仍属于后续硬化项

**影响**: 极端同分场景下，需要更明确的稳定排序与字段优先级规则

**优先级**: 低

**建议**: 补充明确的二级排序键和冲突字段合并测试

---

### 问题 2: 分词阈值硬编码

**问题**: `token_threshold=5` 可配置但缺少文档说明

**影响**: 用户难以调优

**优先级**: 低

**建议**: 添加配置文件支持和最佳实践文档

---

### 问题 3: BM25 和向量检索的 `top_k` 不一致

**问题**: `HybridRetriever` 使用 `max(limit * 2, 20)` 作为候选数，但目前不可配置

**影响**: 性能和召回率的平衡

**优先级**: 低

**建议**: 添加 `candidate_multiplier` 参数

---

### 问题 4: 各入口必须保持五态适配一致

**问题**: 核心检索器已统一 `SearchResponse`，但新增入口仍可能错误地把 `error` / `degraded` 映射成“无结果”

**影响**: 上层调用可能丢失故障或降级信息

**优先级**: 低

**建议**: 入口适配必须显式映射全部五态，并保留稳定 issue；禁止列表兼容和 truthiness 兼容

---

## 🎯 总结

### 设计优点

✅ 智能路由（基于查询长度）
✅ 三种检索策略互补（BM25、向量、混合）
✅ RRF 算法融合结果
✅ 并行执行提高性能
✅ 统一的 SearchResult 格式
✅ 五态 SearchResponse 区分无命中、无效、失败与降级
✅ 分数归一化到 [0.0, 1.0]

### 核心参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `token_threshold` | 5 | 短/长查询分界点 |
| `rrf_k` | 60 | RRF 算法常数 |
| `chunk_size` | 500 | 向量分块大小 |
| `chunk_overlap` | 50 | 向量分块重叠 |

---

**文档维护者**: AI Agent
**最后更新**: 2026-08-07
