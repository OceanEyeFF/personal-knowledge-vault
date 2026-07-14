# Relations 接口规范

> **版本**: 1.1
> **创建日期**: 2026-03-31
> **最后更新**: 2026-04-03
> **文件位置**: `src/relations/`、`src/storage/relation_store.py`、`src/mcp/tools.py`
> **作用**: 固定关系层当前公开模型、抽取合同、回填合同与查询合同

---

## 1. 枚举与常量契约

### 1.1 关系类型

| 枚举值 | 说明 |
|--------|------|
| `references` | 正文显式引用 |
| `related_document` | Front Matter `related_docs` 显式相关文档 |
| `parent_of` | Front Matter `children` 显式层级关系 |
| `version_of` | Front Matter `version_of` 显式版本关系 |

### 1.2 关系来源类型

| 枚举值 | 说明 |
|--------|------|
| `markdown_link` | Markdown 正文链接 |
| `frontmatter_related_docs` | Front Matter `related_docs` |
| `frontmatter_field` | Front Matter 白名单关系字段 |
| `manual` | 人工维护 |
| `backfill` | 预留的回填来源标记 |

### 1.3 当前低歧义关系范围

`LOW_AMBIGUITY_RELATION_TYPES` 当前包括：

- `references`
- `related_document`
- `parent_of`
- `version_of`

---

## 2. 抽取层契约

### 2.1 Front Matter 解析

```python
parse_front_matter(markdown_text: str) -> tuple[dict[str, object], str]
```

合同：

- 只识别标准 `--- ... ---` YAML Front Matter
- 返回 `(metadata, body)`
- `metadata` 解析失败时退回空字典

### 2.2 自动抽取函数

| 函数 | 输入 | 输出 | 当前规则 |
|------|------|------|----------|
| `extract_markdown_link_references()` | Markdown 文本 | `refs, issues` | 只接受内部 Markdown 链接，过滤外链/锚点/图片 |
| `extract_frontmatter_related_docs()` | Markdown 文本 | `refs, issues` | 只接受 `related_docs: list[str]` |
| `extract_frontmatter_relation_fields()` | Markdown 文本 | `refs, issues` | 只接受 `children: list[str]` 与 `version_of: str` |

补充说明：

- `extract_frontmatter_relation_fields()` 当前直接读取原始 Front Matter，不依赖 `Entry` dataclass
- 因此 `children` / `version_of` 目前属于 Front Matter 扩展字段，而不是 `MarkdownStore.Entry` 的标准属性

### 2.3 `frontmatter_field` 字段白名单

| 字段 | 类型 | 产出关系 | 非法值处理 |
|------|------|----------|------------|
| `children` | `list[str]` | `parent_of` | 不是列表时记为 `invalid_field_type` |
| `version_of` | `str` | `version_of` | 空值/外链/锚点记为 issue |

### 2.4 `ExtractedReference` 契约

```python
ExtractedReference(
    relation_type: RelationType,
    relation_source_type: RelationSourceType,
    raw_target: str,
    evidence_payload: dict[str, object],
)
```

要求：

- `raw_target` 必须保留源文档中的目标表达
- `evidence_payload` 必须足以审计来源字段

### 2.5 `ReferenceIssue` 契约

```python
ReferenceIssue(
    relation_type: RelationType,
    relation_source_type: RelationSourceType,
    raw_target: str,
    reason: str,
    detail: dict[str, object] = {},
)
```

当前常见 `reason`：

- `invalid_target`
- `invalid_field_type`
- `external_link`
- `anchor_link`

---

## 3. 证据载荷契约

### 3.1 抽取阶段

| 来源 | 抽取证据字段 |
|------|--------------|
| `markdown_link` | `raw_target`、`normalized_target`、`anchor_text` |
| `frontmatter_related_docs` | `field=related_docs`、`normalized_target` |
| `frontmatter_field(children)` | `field=children`、`raw_target`、`normalized_target` |
| `frontmatter_field(version_of)` | `field=version_of`、`raw_target`、`normalized_target` |

### 3.2 落库阶段补充字段

所有自动边在写入前统一补充：

- `declared_in_knowledge_id`
- `source_file_path`
- `target_file_path`

这些字段构成当前 `knowledge_relations.evidence_payload` 的最小审计面。

---

## 4. 存储层契约

### 4.1 `RelationRecord`

```python
RelationRecord(
    source_knowledge_id: int,
    target_knowledge_id: int,
    relation_type: RelationType | str,
    relation_source_type: RelationSourceType | str,
    direction: RelationDirection | str = "directed",
    weight: float = 1.0,
    evidence_payload: dict[str, Any] = {},
)
```

约束：

- `source_knowledge_id` / `target_knowledge_id` 必须为正整数
- 不支持自指关系
- `weight > 0`
- `evidence_payload` 必须为字典

### 4.2 `RelationStore`

| 方法 | 作用 |
|------|------|
| `table_exists()` | 检查 `knowledge_relations` 是否存在 |
| `upsert_relation(relation)` | 按 `(source, target, type, source_type)` 幂等写入 |
| `get_relation(relation_id)` | 按主键读取 |
| `list_relations_for_knowledge(...)` | 按 seed / direction / type / source 查询 |
| `list_relations_between(source, target)` | 精确读取两点之间的有向边 |
| `delete_relations_by_source_type(source_type)` | 按来源清理 |
| `delete_outgoing_relations_for_knowledge(knowledge_id, source_types)` | 删除指定条目导出的自动边 |

### 4.3 排序与过滤

`RelationStore.list_relations_for_knowledge()` 的 SQL 原生排序为：

`updated_at DESC -> relation_id DESC`

上层 `RelationQueryService` 会再执行统一分组与稳定排序。

---

## 5. 回填服务契约

### 5.1 `RelationBackfillService.backfill()`

```python
backfill(
    knowledge_ids: Optional[Iterable[int]] = None,
    apply: bool = False,
) -> BackfillReport
```

合同：

- `apply=False` 时只做 dry-run
- `apply=True` 时要求 `knowledge_relations` 已存在
- apply 模式先删除当前条目导出的自动边，再写入本轮新边
- 删除范围只限自动抽取来源集合

### 5.2 冲突优先级

同一 `(source, target, relation_type)` 内按来源优先级比较：

1. `manual`
2. `frontmatter_field`
3. `markdown_link` / `frontmatter_related_docs`
4. `backfill`

### 5.3 `BackfillReport`

当前质量报告核心指标：

- `total_references`
- `resolved_references`
- `invalid_references`
- `unresolved_references`
- `conflicted_relations`
- `coverage_rate`
- `noise_rate`
- `conflict_rate`

当前固定报告版本：

- `schema_version = backfill_quality_report.v1`

---

## 6. 查询服务契约

### 6.1 一跳查询

| 方法 | 返回 |
|------|------|
| `list_relations()` | `RelationQueryResult` |
| `get_neighbors()` | `RelationQueryResult` |
| `get_relations_between()` | `RelationQueryResult` |

### 6.2 多跳与解释

| 方法 | 返回 | 说明 |
|------|------|------|
| `query_subgraph()` | `RelationSubgraphResult` | 受限 BFS 子图 |
| `explain_relation()` | `RelationExplanationResult` | 直接边优先，否则最短路径 |

### 6.3 查询结果排序规则

当前 `RelationQueryService` 对结果执行统一排序：

1. 先按 `relation_type` 分组
2. 组内按：
   `weight DESC -> updated_at DESC -> relation_id ASC`

这条规则同时适用于：

- `grouped_items`
- `grouped_edges`
- `explain_relation()` 内部候选关系排序

### 6.4 推理型 MCP 输出合同（closeout pending）

当前 `collect_evidence` 与 `timeline_of` 的输出语义合同如下：

`collect_evidence(question, top_k, relation_max_depth, include_chunks)`：

- `include_chunks=False`：默认文档级兼容路径
- `include_chunks=True`：启用 chunk-aware 证据聚合路径
- `chunk_retrieval_status` 允许值固定为：
  - `not_requested`
  - `success`
  - `no_hits`
  - `path_unavailable`
  - `search_error`
- 当 `chunk_retrieval_status` 为 `path_unavailable` 或 `search_error` 时，`limitation_notes` 必须包含可观测降级信号（如 `chunk_degraded[reason] ...`）

`timeline_of(topic, top_k, sort_order)`：

- 时间来源优先级：`event_time > published_at > archived_at`
- `inferred_time_field` 可返回单一来源字段，也可在多来源并列主导时返回 `mixed`
- `sort_order=desc` 仅对可解析时间做真正倒序；不可解析时间值保持中性稳定排序

---

## 7. 非目标与限制

- 不支持高噪声纯语义边落库
- 不支持 `parent` 等会破坏当前 outgoing 幂等清理合同的字段
- 不在关系层存储中自动推断双向边
- 不在本规范内定义 MCP Tool 的输入输出适配层

---

## 8. 关联文档

- `docs/modules/relations/RELATION_LAYER_DESIGN.md`
- `docs/specs/models/Entry数据模型规范.md`
- `docs/specs/models/数据规范.md`
- `docs/specs/database/SQLite_Schema完整规范.md`
- `docs/operations/关系回填质量验证指南.md`
