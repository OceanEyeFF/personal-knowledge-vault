# Phase B 上游：Chunk 索引/去重/排序 Phase 1 开发清单（2026-03）

> 文档类型：执行清单 / Task Breakdown
> 创建日期：2026-03-27
> 适用范围：Phase B 上游基础能力（Chunk 索引、去重、排序）
> 关联文档：`docs/overview/PhaseB-推理型MCP路线图-2026-03.md`

---

## 1. 文档定位

本文档负责把 Phase B 上游“Chunk 索引/去重/排序”拆解为可执行的 Phase 1 任务清单，明确分层边界、改动范围与风险控制点。

Phase 1 的定位是“内部可用的 chunk 能力”，目标是先打通存储与检索链路，不改变 MCP 对外接口，不引入破坏性变更。

---

## 2. 分层边界

分层职责如下，每层只做自己的事：

- 存储层（SQLite）：负责 chunk 文本与元数据的持久化、查询、删除，不负责向量索引。
- 向量索引层（hnswlib）：负责 chunk 向量写入与搜索，不负责文本存储。
- 检索层（BM25/Vector/Hybrid）：负责检索策略与结果融合，不负责证据拼装。
- 证据聚合层（EvidenceService）：负责证据去重、排序、content_preview 决策。
- 接口层（MCP/tools + models）：负责输出结构稳定，新增字段必须 Optional。
- 迁移与运维层：负责回填入口、数据一致性检查与失败重试。
- 测试层：覆盖 CRUD、检索、证据回退与兼容性。

---

## 3. Phase 1 目标与退出条件

目标：

- Chunk 文本可落库、可检索、可回退到文档级。
- 向量索引与 SQLite 双写完成，保持一致性。
- 证据聚合可以返回“更相关的 content_preview”（基于 chunk）。
- MCP 接口保持 100% 向后兼容。

退出条件：

- 新入库条目可在 SQLite 查到 chunk 文本。
- chunk 向量可检索，且能映射回 (knowledge_id, chunk_index)。
- collect_evidence 能优先返回 chunk 片段，旧数据自动回退到文档级。
- 单元测试覆盖 chunk CRUD 与证据回退路径。

---

## 4. 详细任务清单（按层分解）

### 4.1 存储层（SQLite）

1. T-SQL-1 新增 chunk CRUD 方法
目标：补齐 chunk 的插入、查询与删除接口。
改动范围：`src/storage/sqlite_store.py`。
接口建议：`insert_chunks(knowledge_id: int, chunks: list[str]) -> int`；`get_chunks_by_knowledge_id(knowledge_id: int) -> list[dict]`；`get_chunk_by_index(knowledge_id: int, chunk_index: int) -> dict | None`；`delete_chunks_by_knowledge_id(knowledge_id: int) -> int`。
验收：新增接口可在集成流程中被调用，且满足 UNIQUE(knowledge_id, chunk_index)。
风险控制点：必须使用事务，任何单条写入失败需要回滚并抛错。

2. T-SQL-2 补齐索引校验工具
目标：增加简单的存在性检查或统计方法，用于验证 chunk 是否落库。
改动范围：`src/storage/sqlite_store.py`。
验收：新增方法可输出 chunk 数或判断某 knowledge_id 是否拥有 chunk。
风险控制点：查询必须走索引字段，避免全表扫描。

### 4.2 向量索引层（hnswlib）

1. T-VEC-1 双写一致性处理
目标：保证 SQLite 与向量索引写入顺序一致，失败时可重试。
改动范围：`src/storage/vector_store.py` 与 `src/workflow/steps.py`。
验收：向量写入失败时不影响 SQLite 数据完整性，日志可定位失败条目。
风险控制点：必须记录失败条目或返回错误，让上层决定降级。

2. T-VEC-2 保护 chunk_id 编码边界
目标：确保 `knowledge_id * 10000 + chunk_index` 不溢出或冲突。
改动范围：`src/storage/vector_store.py`。
验收：超限 chunk_index 触发明确报错或被安全截断。
风险控制点：不能静默丢失向量。

### 4.3 工作流层（入库双写）

1. T-WF-1 StoreStep 保存 chunk 文本
目标：在入库阶段把 chunk 文本写入 SQLite。
改动范围：`src/workflow/steps.py`。
验收：入库后 SQLite content_chunks 有记录。
风险控制点：写入失败要明确回退策略，避免向量存在但文本缺失。

2. T-WF-2 StoreStep 同步写入 chunk 向量
目标：向量写入与 chunk 文本存储保持一致。
改动范围：`src/workflow/steps.py`。
验收：SQLite 与 hnswlib 中 chunk 数一致。
风险控制点：嵌入失败要返回错误并降级，不得中断主流程。

### 4.4 检索层

1. T-RET-1 新增 chunk 级向量检索入口
目标：提供可调用的 chunk 检索方法。
改动范围：`src/retrieval/vector_retriever.py` 或新增 `src/retrieval/chunk_vector_retriever.py`。
验收：可返回 (knowledge_id, chunk_index, score) 列表。
风险控制点：检索失败需回退为文档级。

2. T-RET-2 结果映射与基础过滤
目标：将向量检索结果映射到 SQLite chunk 文本。
改动范围：检索层或 EvidenceService。
验收：EvidenceService 能拿到 chunk_text。
风险控制点：缺失 chunk_text 时自动回退到文档级 preview。

### 4.5 证据聚合层

1. T-EVD-1 优先返回 chunk 片段作为 content_preview
目标：让 content_preview 变成语义相关片段。
改动范围：`src/relations/evidence_service.py`。
验收：返回的 preview 来自 chunk 文本，旧数据回退到文档级。
风险控制点：保持字段名不变，保证 MCP 兼容。

2. T-EVD-2 Phase 1 精确去重
目标：按 (knowledge_id, chunk_index) 去重。
改动范围：`src/relations/evidence_service.py`。
验收：重复 chunk 不进入结果。
风险控制点：不能误删来自不同文档的证据。

### 4.6 模型与接口层

1. T-MDL-1 增加 Optional chunk 字段
目标：结构上可表达 chunk，但不要求调用方必须使用。
改动范围：`src/relations/models.py`。
字段建议：`chunk_index`、`chunk_text`、`chunk_id`。
验收：旧调用不报错，序列化输出包含新字段。
风险控制点：字段必须 Optional，不得影响旧逻辑。

2. T-MCP-1 MCP 输出透传
目标：collect_evidence 输出结构兼容旧调用。
改动范围：`src/mcp/tools.py`。
验收：接口无破坏性变更，新增字段仅做透传。
风险控制点：严禁修改输入参数结构。

### 4.7 迁移与运维层

1. T-OPS-1 一致性校验脚本入口
目标：提供检查 SQLite 与 hnswlib 一致性的入口（可以是函数或脚本草案）。
改动范围：`scripts/` 或 `src/utils/`。
验收：可输出缺失 chunk 的 knowledge_id 列表。
风险控制点：只读操作，不改库。

2. T-OPS-2 回填入口定义
目标：定义将历史 entry 回填 chunk 的执行入口与参数。
改动范围：`scripts/`。
验收：可 dry-run 输出预计处理条目数。
风险控制点：Phase 1 仅提供入口，不直接执行。

### 4.8 测试层

1. T-TEST-1 SQLite chunk CRUD
目标：覆盖 insert/get/delete 行为。
改动范围：`tests/unit/`。
验收：通过单测并覆盖异常路径。
风险控制点：测试不依赖外部 API。

2. T-TEST-2 EvidenceService 回退路径
目标：chunk 缺失时回退文档级 preview。
改动范围：`tests/unit/`。
验收：旧数据场景通过。
风险控制点：确保与 MCP 输出兼容。

---

## 5. 风险控制点总表

- 双写不一致：先写 SQLite，向量失败可重试，绝不反向写。
- chunk_id 编码冲突：限定 chunk_index 上限或换编码策略。
- 旧数据缺失：EvidenceService 强制回退到文档级。
- 语义预览变化：保留 content_preview 字段含义与稳定性。
- 存储膨胀：配置 chunk_size 与最大 chunk 数上限。

---

## 6. 交付顺序建议

1. SQLite chunk CRUD + StoreStep 文本写入
2. chunk 向量写入与检索入口
3. EvidenceService 改造与回退逻辑
4. Optional 字段扩展与 MCP 透传
5. 测试补齐与一致性检查入口

---

## 7. 文档同步要求

- 更新 `docs/overview/PhaseB-推理型MCP路线图-2026-03.md`，为 5.2 补充执行清单引用。
- 如有新增脚本或接口，补充到 `docs/overview/文档与代码差异清单-2026-03.md`。
