# 存储与检索方案讨论2：LanceDB vs SQLite 方案

> Personal Knowledge Vault - 非传统数据库方案调研
> 创建日期: 2026-02-02
> 状态: 调研完成，结论明确

---

## 📋 目录

1. [调研背景](#调研背景)
2. [LanceDB 简介](#lancedb-简介)
3. [LanceDB 深度调研](#lancedb-深度调研)
4. [风险点逐条对比](#风险点逐条对比)
5. [结论与建议](#结论与建议)
6. [其他非传统方案备忘](#其他非传统方案备忘)
7. [参考资料](#参考资料)

---

## 调研背景

在 [存储与检索方案讨论](./存储与检索方案讨论.md) 中，我们已经确定了方案3（SQLite + hnswlib + jieba）作为推荐方案。

但在讨论过程中，提出了一个问题：**有没有非传统键值对数据库的方法？**

于是对 **LanceDB** 进行了深度调研，评估其是否能成为更优的替代方案。

---

## LanceDB 简介

### 什么是 LanceDB

LanceDB 是一个**嵌入式 AI 原生向量数据库**，基于 Lance 列式存储格式构建。

```
特点：
- 向量搜索 + 全文搜索 + 混合搜索一体化
- 零配置，嵌入式，纯本地文件
- 内置 Reranking
- 基于 Tantivy（Rust 写的 Lucene 替代品）做全文搜索
- pip install lancedb 即可使用
```

### 为什么考虑 LanceDB

对比方案3，LanceDB 有一个非常诱人的优势：

| 维度 | LanceDB | 方案3 (SQLite + hnswlib + jieba) |
|------|---------|----------------------------------|
| 组件数量 | ✅ **1个** | ⚠️ 3个（SQLite + hnswlib + jieba） |
| 混合检索融合 | ✅ **内置 + Reranking** | ⚠️ 需自行编写 |
| 文件管理 | ✅ 统一 Lance 格式 | ⚠️ .db + .idx 两类文件 |

如果 LanceDB 能满足 PKV 的需求，将大大简化架构。

---

## LanceDB 深度调研

### 调研维度

1. 中文全文搜索支持（Tantivy 分词器）
2. 标签/元数据灵活性和 SQL 过滤能力
3. 成熟度、社区、10年维护性
4. 已知问题和生产环境表现

---

### 🔴 致命风险：中文全文搜索不支持

#### 发现的 GitHub Issues

| Issue | 状态 | 日期 |
|-------|------|------|
| [#2168 - Feature: Support Chinese Language in FTS](https://github.com/lancedb/lancedb/issues/2168) | **Open** | 2025-03 |
| [#2329 - Feature: Support Chinese/CJK text search in BM25 indexing](https://github.com/lancedb/lancedb/issues/2329) | **Open** | 2025-04 |

#### 官方回复

> "这个功能在路线图上，但我们人手不足"
> — BubbleCal (LanceDB 贡献者)

#### 技术现状

- Lance 库已在 PR #3218 中添加了 **Lindera** 支持（日文分词器）
- **中文分词尚未集成**
- LanceDB 层面的 tokenizer 选择 API 也未暴露

#### 临时方案

官方提供了 **ngram 分词器** 作为 workaround，可用于 "substring search"。

但 ngram 的问题：
- 只能模糊匹配，**精度远不如 jieba**
- 无法理解中文词边界
- 召回噪音大

#### 影响评估

```
PKV 是中文用户的个人知识库
中文全文搜索是核心功能
LanceDB 目前无法原生满足

如果要 workaround（Python 层预分词后写入）：
  → 失去了 LanceDB "一体化"的优势
  → 和方案3 没有本质区别
  → 反而多了一个不如 SQLite 成熟的存储层
```

---

### 🟢 元数据过滤/SQL 能力：良好

#### 支持的能力

| 能力 | 详情 |
|------|------|
| SQL-like 过滤 | `where("(item IN ('a', 'b')) AND (id > 10)")` |
| 嵌套数据访问 | 用反引号转义：`` `nested`.`field` < 2 `` |
| Pre-filtering | 向量搜索前过滤，缩小搜索空间 |
| Post-filtering | 向量搜索后过滤，精细筛选 |
| 列投影 | `select()` 控制返回列，支持动态计算 |

#### 性能表现

- 元数据过滤延迟：50ms 级别
- 支持千 QPS
- 底层基于 DataFusion

#### 与 SQLite 对比

| 维度 | LanceDB | SQLite |
|------|---------|--------|
| 查询语法 | SQL-like（DataFusion） | 完整 SQL |
| JOIN 支持 | ❌ 不支持 | ✅ 支持 |
| 事务 | 有限 | ✅ 完整 ACID |
| 聚合函数 | 有限 | ✅ 丰富 |

**结论**：LanceDB 的过滤能力对于 PKV 的标签系统**基本够用**，但灵活性不如 SQLite。

---

### 🟡 其他已知问题

| 问题 | 详情 | 来源 |
|------|------|------|
| **S3 内存泄漏** | 2GB 数据集消耗 16GB+ RAM | [Issue #2468](https://github.com/lancedb/lancedb/issues/2468) |
| **Async + FTS 冲突** | v0.20.0 async optimize 会破坏 FTS | [Issue #2193](https://github.com/lancedb/lancedb/issues/2193) |
| **并发写入限制** | 过多并发写入会失败，有重试次数限制 | [FAQ](https://docs.lancedb.com/faq/faq-oss) |
| **小表索引限制** | < 256 行无法创建向量索引 | [FAQ](https://docs.lancedb.com/faq/faq-oss) |
| **Python multiprocessing** | 不能用 fork 模式（Lance 内部多线程） | [FAQ](https://docs.lancedb.com/faq/faq-oss) |
| **merge insert bug** | 可能引用无效 fragment ID | [Issue #2751](https://github.com/lancedb/lancedb/issues/2751) |

---

### 🟢 积极的方面

#### Lance 格式稳定性

| 里程碑 | 日期 | 意义 |
|--------|------|------|
| [Lance SDK 1.0.0](https://lancedb.com/blog/announcing-lance-sdk/) | 2025-12 | 正式采用语义化版本，承诺 API 稳定 |
| [Lance File 2.1 稳定](https://lancedb.com/blog/lance-file-2-1-stable/) | 2025-10 | 格式规范化，承诺向后兼容 |

#### 生产案例

- [7亿向量迁移成功案例](https://sprytnyk.dev/posts/running-lancedb-in-production/)
- 有企业版本（LanceDB Cloud、Enterprise）

#### 社区

- LanceDB: **8.7k** GitHub stars
- Lance (格式库): **5.8k** GitHub stars
- 活跃开发，有商业支持

---

## 风险点逐条对比

### PKV 核心需求 vs 两方案能力

| PKV 需求 | LanceDB | 方案3 (SQLite + hnswlib + jieba) |
|---------|---------|----------------------------------|
| **中文全文搜索** | 🔴 **不支持**（只有 ngram） | ✅ jieba + FTS5 |
| **向量 ANN 搜索** | ✅ 内置 IVF_PQ | ✅ hnswlib HNSW |
| **混合检索融合** | ✅ 内置 + Reranking | 🟡 需自行编写 |
| **标签系统** | ✅ 列存储 + 过滤 | ✅ SQLite 表 + SQL |
| **元数据 SQL 查询** | 🟡 SQL-like（有限） | ✅ 完整 SQL |
| **部署复杂度** | ✅ pip install 一个包 | ✅ pip install 三个包 |
| **组件管理** | ✅ 单一组件 | 🟡 两类文件需同步 |
| **10年维护性** | 🟡 2023年出现，1.0 刚发布 | ✅ SQLite 极稳定 + hnswlib 成熟 |
| **并发写入** | 🟡 有限制，可能失败 | ✅ SQLite WAL 稳定 |
| **数据恢复** | 🟡 格式较新，工具链不完善 | ✅ SQLite 工具链成熟 |

### 关键差异分析

```
LanceDB 的优势：
  ✅ 一体化，组件少
  ✅ 混合检索内置
  ✅ Reranking 内置

LanceDB 的致命问题：
  🔴 中文全文搜索不支持
     → PKV 是中文知识库，这是核心功能
     → 无法原生满足

方案3 的权衡：
  🟡 需要自己写混合检索融合
  🟡 管理两类文件
  ✅ 但中文全文搜索完全支持
  ✅ 各组件都久经考验
```

---

## 结论与建议

### 最终结论

**LanceDB 目前不适合 PKV**

| 原因 | 说明 |
|------|------|
| **中文全文搜索不支持** | PKV 的核心需求，LanceDB 无法原生满足 |
| **Workaround 无意义** | 如果要 Python 层预分词，LanceDB 的"一体化"优势就没了 |
| **维护性风险** | 2023年才出现，未经历长期验证；相比之下 SQLite 极度稳定 |

### 推荐维持方案3

```
方案3：SQLite + hnswlib + jieba

  ✅ 中文全文搜索：jieba + FTS5 完美支持
  ✅ 向量搜索：hnswlib 成熟稳定
  ✅ 元数据：SQLite 完整 SQL
  ✅ 10年维护性：各组件都久经考验

  代价：
  🟡 需要管理两类文件
  🟡 需要自己写混合检索融合

  但这些代价可接受，因为核心功能有保障
```

### LanceDB 观察名单

将 LanceDB 列入**观察名单**，等待以下条件满足后重新评估：

| 观察条件 | 相关 Issue |
|---------|-----------|
| 中文 FTS 支持合并 | [#2168](https://github.com/lancedb/lancedb/issues/2168) |
| CJK BM25 支持 | [#2329](https://github.com/lancedb/lancedb/issues/2329) |
| 经历 2+ 年生产验证 | - |

---

## 其他非传统方案备忘

调研过程中也了解了其他非传统方案，简要记录：

### DuckDB + VSS 扩展

| 维度 | 评估 |
|------|------|
| 定位 | 嵌入式分析型数据库 + 向量搜索 |
| 向量索引 | HNSW（基于 usearch） |
| SQL 能力 | **极强**，比 SQLite 强 |
| **致命问题** | 🔴 VSS 持久化**仍为实验性**，官方不建议生产使用 |
| 索引限制 | 必须完全放在 RAM 中 |
| 结论 | **暂不考虑**，等 VSS 稳定后再评估 |

来源：[DuckDB VSS 文档](https://duckdb.org/docs/stable/core_extensions/vss)

### ChromaDB

| 维度 | 评估 |
|------|------|
| 定位 | AI 应用向量数据库 |
| 后端 | SQLite + hnswlib |
| 问题 | 引入额外抽象层，底层就是方案3 的封装 |
| 结论 | **不如直接用方案3**，减少抽象层 |

### Tantivy (tantivy-py)

| 维度 | 评估 |
|------|------|
| 定位 | Rust 写的全文搜索引擎（Lucene 替代品） |
| 中文支持 | 有 [tantivy-jieba](https://docs.rs/tantivy-jieba/) 库 |
| 向量搜索 | ❌ 不支持 |
| 结论 | 只解决全文搜索，向量搜索还需要别的组件，**不如方案3 简洁** |

---

## 参考资料

### LanceDB 官方

- [LanceDB 官网](https://lancedb.com/)
- [LanceDB 文档](https://docs.lancedb.com/)
- [LanceDB GitHub](https://github.com/lancedb/lancedb) - 8.7k stars
- [Lance SDK 1.0.0 发布公告](https://lancedb.com/blog/announcing-lance-sdk/)
- [Lance File 2.1 稳定公告](https://lancedb.com/blog/lance-file-2-1-stable/)

### LanceDB 中文支持相关

- [Issue #2168 - Feature: Support Chinese Language in FTS](https://github.com/lancedb/lancedb/issues/2168)
- [Issue #2329 - Feature: Support Chinese/CJK text search in BM25](https://github.com/lancedb/lancedb/issues/2329)
- [Issue #1315 - Enable stemming and choosing tokenizer](https://github.com/lancedb/lancedb/issues/1315)

### LanceDB 已知问题

- [Issue #2468 - Memory Leak with S3 storage](https://github.com/lancedb/lancedb/issues/2468)
- [Issue #2193 - Async optimize breaks FTS](https://github.com/lancedb/lancedb/issues/2193)
- [Issue #2751 - Merge insert invalid fragment IDs](https://github.com/lancedb/lancedb/issues/2751)
- [LanceDB FAQ](https://docs.lancedb.com/faq/faq-oss)

### LanceDB 生产案例

- [Running 700M vectors in production](https://sprytnyk.dev/posts/running-lancedb-in-production/)

### Tantivy 中文支持

- [tantivy-jieba](https://docs.rs/tantivy-jieba/) - Tantivy 中文分词器（Rust）
- [cang-jie](https://github.com/DCjanus/cang-jie) - 另一个 Tantivy 中文分词器

### 其他方案

- [DuckDB VSS 扩展](https://duckdb.org/docs/stable/core_extensions/vss)
- [DuckDB VSS 更新 (2024-10)](https://duckdb.org/2024/10/23/whats-new-in-the-vss-extension)

### 上游文档

- [存储与检索方案讨论](./存储与检索方案讨论.md) - 方案3 详细设计

---

**文档结束**

*本文档为 LanceDB 调研记录，结论已反映到主讨论文档中*
