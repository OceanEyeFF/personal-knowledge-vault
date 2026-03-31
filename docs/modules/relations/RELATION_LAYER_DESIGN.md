# 关系层模块设计文档

> **文档版本**: v1.0
> **创建日期**: 2026-03-31
> **最后更新**: 2026-03-31
> **对应代码**: `src/relations/`、`src/storage/relation_store.py`、`scripts/backfill_relations.py`
> **定位**: 当前关系层实现的模块边界、数据流与能力约束说明

---

## 1. 目标与范围

关系层当前负责把知识库中的低歧义显式信号沉淀为可查询、可解释、可回填的关系边，为后续 MCP 推理能力提供稳定底座。

当前模块目标：

- 定义统一的关系模型、关系类型、来源类型和查询结果结构
- 从 Markdown / Front Matter 中提取低歧义显式关系
- 把显式关系安全写入 `knowledge_relations`
- 提供一跳关系查询、受限子图遍历与最小关系解释
- 为证据聚合、桥接发现、时间线和对比能力提供关系底座

当前不在本模块内解决的问题：

- 高噪声纯语义推断边
- 图数据库与图可视化
- MCP 参数适配与传输层协议处理
- GUI 展示层

---

## 2. 模块边界

| 文件 | 责任 |
|------|------|
| `src/relations/models.py` | 关系枚举、关系记录、查询结果与推理结果模型 |
| `src/relations/extractors.py` | Front Matter / Markdown 显式关系抽取、回填报告、回填服务 |
| `src/storage/relation_store.py` | `knowledge_relations` 低层读写、过滤、清理 |
| `src/relations/query_service.py` | 一跳查询、受限多跳子图、最小关系解释 |
| `src/relations/evidence_service.py` | 基于检索结果与关系查询做证据包聚合 |
| `src/relations/exploration_service.py` | `find_bridges` / `timeline_of` / `contrast` 的受限探索实现 |
| `scripts/backfill_relations.py` | 命令行回填入口与质量报告输出 |

依赖关系：

1. `extractors.py` 依赖 `RelationStore` 完成 apply 模式写入与冲突检测
2. `query_service.py` 依赖 `RelationStore` 完成关系读取
3. `evidence_service.py` / `exploration_service.py` 依赖 `RelationQueryService`
4. `MCP Tool` 侧只消费 `query_service` / `evidence_service` / `exploration_service` 的结果，不直接操作底层关系表

---

## 3. 当前关系事实模型

### 3.1 关系类型

| `RelationType` | 语义 | 当前主要来源 |
|----------------|------|--------------|
| `references` | 当前文档显式引用目标文档 | `markdown_link` |
| `related_document` | 当前文档在 Front Matter 中显式声明相关文档 | `frontmatter_related_docs` |
| `parent_of` | 当前文档是目标文档的父级/目录性文档 | `frontmatter_field(children)` |
| `version_of` | 当前文档是目标文档的版本/演化结果 | `frontmatter_field(version_of)` |

### 3.2 来源类型

| `RelationSourceType` | 当前状态 | 说明 |
|----------------------|----------|------|
| `markdown_link` | 已实现自动抽取 | 来自正文 Markdown 链接 |
| `frontmatter_related_docs` | 已实现自动抽取 | 来自 Front Matter `related_docs` |
| `frontmatter_field` | 已实现自动抽取 | 来自 Front Matter 白名单关系字段 |
| `manual` | 预留/人工写入 | 手工维护的高优先级事实边 |
| `backfill` | 预留来源标记 | 表示通过外部回填策略物化的边 |

### 3.3 显式事实边与推断边边界

当前自动回填只处理**显式事实边**：

- 正文中明确写出的 Markdown 链接
- Front Matter 中明确声明的 `related_docs`
- Front Matter 中明确声明的 `children` / `version_of`

当前不自动写入**推断边**：

- 纯语义相似边
- 标签共现边
- 标题/摘要重合边
- 桥接、时间线、对比中的启发式候选边

这些推断信号可以用于探索或评分，但不能与显式事实边混用。

---

## 4. 自动抽取与回填

### 4.1 当前自动抽取来源

| 抽取函数 | 输入字段 | 产出关系 | 来源类型 |
|----------|----------|----------|----------|
| `extract_markdown_link_references()` | Markdown 正文链接 | `references` | `markdown_link` |
| `extract_frontmatter_related_docs()` | `related_docs: list[str]` | `related_document` | `frontmatter_related_docs` |
| `extract_frontmatter_relation_fields()` | `children` / `version_of` | `parent_of` / `version_of` | `frontmatter_field` |

补充说明：

- `extract_frontmatter_relation_fields()` 当前直接解析原始 Markdown Front Matter
- `children` / `version_of` 目前不是 `Entry` dataclass 的标准字段，不经过 `MarkdownStore.save()/load()` 的常规 round-trip

### 4.2 `frontmatter_field` 白名单

当前只支持两个低歧义字段：

| 字段 | Front Matter 形态 | 写入关系 | 存储方向 |
|------|-------------------|----------|----------|
| `children` | `list[str]` | `parent_of` | `当前文档 -> 子文档` |
| `version_of` | `str` | `version_of` | `当前文档 -> 基线文档` |

不支持：

- `parent`
- `derived_from`
- `supersedes`
- 任意别名字段自动映射

原因：

- 当前回填幂等清理以“当前条目导出的 outgoing 自动边”为边界
- `children` 与 `version_of` 都能保持“当前条目声明、当前条目出边”的稳定合同

### 4.3 证据载荷

抽取阶段的 `ExtractedReference.evidence_payload` 会保留来源字段；写入阶段会统一补充：

- `declared_in_knowledge_id`
- `source_file_path`
- `target_file_path`

当前已定义的来源特有字段：

| 来源 | 关键字段 |
|------|----------|
| `markdown_link` | `raw_target`、`normalized_target`、`anchor_text` |
| `frontmatter_related_docs` | `field=related_docs`、`normalized_target` |
| `frontmatter_field(children)` | `field=children`、`raw_target`、`normalized_target` |
| `frontmatter_field(version_of)` | `field=version_of`、`raw_target`、`normalized_target` |

### 4.4 回填合同

`RelationBackfillService.backfill()` 当前遵循以下约束：

- 默认 `dry-run`
- 只有 `apply=True` 才会写入 `knowledge_relations`
- apply 模式会先删除当前条目导出的自动关系，再写入最新抽取结果
- 删除范围仅限自动来源集合：
  - `markdown_link`
  - `frontmatter_related_docs`
  - `frontmatter_field`
- 冲突检测按同一 `(source, target, relation_type)` 比较来源优先级

来源优先级：

1. `manual`
2. `frontmatter_field`
3. `markdown_link` / `frontmatter_related_docs`
4. `backfill`

---

## 5. 查询与解释能力

### 5.1 一跳查询

`RelationQueryService` 当前提供：

- `list_relations(seed, direction, relation_types, relation_source_types, ...)`
- `get_neighbors(seed, ...)`
- `get_relations_between(a, b, ...)`

统一规则：

- 结果优先按 `relation_type` 分组
- 组内稳定排序规则：
  `weight DESC -> updated_at DESC -> relation_id ASC`

### 5.2 多跳与解释

当前关系层还提供：

- `query_subgraph(seed, depth, ...)`
  - 基于一跳查询做受限 BFS 扩展
  - 返回节点、边、分组边与截断状态
- `explain_relation(a, b, ...)`
  - 先查直接边
  - 直接边缺失时退化为受限深度内的最短路径解释

这些能力当前已被 MCP Tool 复用，但仍属于“最小可解释版本”。

---

## 6. 关系层与上游/下游的连接

### 6.1 上游

- Markdown 文件正文
- Front Matter 结构化字段
- SQLite `knowledge_items.file_path`

### 6.2 中游

- `ExtractedReference`
- `RelationRecord`
- `BackfillReport`
- `knowledge_relations`

### 6.3 下游

- `RelationQueryService`
- `EvidenceCollectionService`
- `ExplorationService`
- MCP Tool：`query_subgraph`、`explain_relation`、`collect_evidence`、`find_bridges`、`timeline_of`、`contrast`

---

## 7. 当前测试锚点

当前关系层的代码级回归主要由以下测试固定：

- `tests/unit/test_relation_extractors.py`
- `tests/unit/test_relation_query_service.py`
- `tests/integration/test_relation_backfill.py`
- `tests/integration/test_relation_query_pipeline.py`
- `tests/fixtures/phase_b_5_4_min_regression.yaml`

建议把这些测试与本文一起视为关系层当前合同的一部分。

---

## 8. 关联文档

- `docs/specs/interfaces/Relations接口规范.md`
- `docs/specs/models/Entry数据模型规范.md`
- `docs/specs/models/数据规范.md`
- `docs/specs/database/SQLite_Schema完整规范.md`
- `docs/operations/关系回填质量验证指南.md`
