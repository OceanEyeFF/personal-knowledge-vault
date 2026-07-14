# Phase B：推理型 MCP 路线图（2026-03）

> 文档类型：执行路线 / Roadmap  
> 创建日期：2026-03-27  
> 目的：把 Phase B 的目标、边界、任务分解、交付物与退出条件固化为可执行清单
> 当前状态：`closeout pending`（已完成核心闭环，剩余收口与验收项）

---

## 1. 定位与范围

Phase B 负责把“关系层基础”推进为“可调用的推理型 MCP 能力”，其产物必须在 MCP 侧可稳定对外调用，并具备可解释、可评测的输出结构。

**不在 Phase B 主战场的内容**：

- GUI 深度交互与图谱可视化
- 新的主存储形态（例如图数据库）
- 大规模语义推断关系的强依赖（可作为可选增强，不作为必选交付）

---

## 2. 当前事实基线（已落地）

基于 `docs/overview/当前事实基线-2026-03.md` 与 `docs/operations/CHANGELOG.md`：

- `query_subgraph`、`explain_relation`、`collect_evidence` 已实现并暴露 MCP Tool
- `find_bridges`、`timeline_of`、`contrast` 已暴露 MCP Tool，但标注为 partial
- 关系层具备模型、存储、显式抽取、回填、一跳查询与最小验证闭环
- 关系层当前已固定正式关系类型 / 来源合同，并把结构化 Front Matter 关系字段扩展到 `children` / `version_of`
- `collect_evidence` 已支持 chunk-aware 路径（`include_chunks=True`），并保留默认文档级兼容行为
- `collect_evidence` 当前已固定 `chunk_retrieval_status`：`not_requested` / `success` / `no_hits` / `path_unavailable` / `search_error`

---

## 3. 未完成任务与上游缺口（确认版）

> 说明：以下为 Phase B 未完成项，以及对应“上游未完成/不足”的实际阻塞点。

1. **chunk-aware 证据包收口仍未完成**
   - 当前状态：`collect_evidence` 已支持 chunk-aware 路径与文档级兼容路径
   - 未完成项：chunk 证据质量门禁与更完整的可观测性验收仍待收口
2. **`find_bridges/timeline_of/contrast` 仍是 partial**  
   - 未完成项：工具仍是弱语义/启发式版本  
   - 上游缺口：语义桥接评分、事件时间字段与对比维度规则未稳定
3. **真实库回填演练未完成**  
   - 当前状态：测试副本库上的 `--apply` 演练、`backfill_quality_report.v1` 与质量门禁参数已具备
   - 未完成项：真实库正式 `--apply` 演练与结果沉淀仍未完成
   - 上游缺口：真实库级质量报告样本与人工验收流程未固化
4. **关系来源扩展仍未完成**
   - 当前状态：已从 Markdown 显式链接 / `related_docs` 扩展到 Front Matter 关系字段 `children` / `version_of`
   - 未完成项：高阶推理输入仍以显式链接与结构化 Front Matter 为主
   - 上游缺口：更稳定的语义关系来源未形成可复用抽取策略
5. **推理评测闭环未完成（Phase C 入口阻塞）**  
   - 当前状态：最小评测样例集、关系回填质量门禁与最小回归命令已建立
   - 未完成项：代表性样例规模、自动评分规则与高阶推理评测闭环仍未完成
   - 上游缺口：更完整的样例覆盖面与稳定自动化评测规则未固化

---

## 4. Phase B 目标

**核心目标**：形成稳定的推理型 MCP 能力闭环，具备“可解释 + 可验证 + 可复用”的输出结构。

具体目标：

1. **推理型 Tool 稳定化**：统一输入约束与输出结构，消除“partial”语义的模糊边界。
2. **证据与解释链闭环**：每个推理输出都可追溯到“关系路径 + 证据片段 + 置信度”。
3. **可验证与可回归**：具备最小评测集与稳定回归指标，支持 Phase C 接手。

---

## 5. 任务分解（Roadmap）

### 5.1 统一输出结构与契约（优先级 P0）

**目标**：对 Phase B 所有 MCP Tool 的返回结构进行统一、版本化与严格约束。

交付物：

- 统一的返回结构版本标记（例如 `schema_version`）
- `implementation_level` 与 `limitation_notes` 规范化
- 明确的 `confidence` / `coverage` / `evidence_count` 字段

涉及模块：

- `src/relations/models.py`
- `src/mcp/tools.py`
- `docs/specs/` 或 `docs/modules/` 补充接口约定

当前阶段说明：

- 已为 `query_subgraph`、`explain_relation`、`collect_evidence`、`find_bridges`、`timeline_of`、`contrast` 统一补入 `schema_version`
- 已为上述结果统一补入 `implementation_level`、`limitation_notes`、`confidence`、`coverage`、`evidence_count`
- 当前版本统一口径为 `schema_version=phase_b.v1`

### 5.2 证据包升级到 chunk 级（优先级 P0）

**目标**：将 `collect_evidence` 从文档级兼容模式收口为稳定的 chunk-aware 证据聚合。

交付物：

- chunk 索引设计与存储策略（不引入新主存储）
- 证据去重与排序规则
- 可选的证据来源标记（显式关系 / 检索 / 语义）

涉及模块：

- `src/relations/evidence_service.py`
- `src/storage/`（如需扩展）
- `tests/`（新增回归用例）

执行清单：

`docs/overview/PhaseB-Chunk索引去重排序-Phase1开发清单-2026-03.md`

当前阶段说明：

- Phase 1 已完成内部 chunk 索引、检索与证据聚合基础能力
- Phase 2 当前通过 `collect_evidence(include_chunks: bool = False)` 以可选参数形式暴露 chunk 级证据字段，默认保持文档级兼容行为
- Phase 3 当前在 `include_chunks=True` 路径上补入多因子排序与近重复去重，仍未引入破坏性接口升级

### 5.3 partial 工具收敛（优先级 P1）

**目标**：明确 `find_bridges`、`timeline_of`、`contrast` 的可用边界，并完成最小可用升级。

交付物：

- `find_bridges`：引入语义桥接候选评分/过滤规则
- `timeline_of`：引入事件时间字段或明确的“时间来源优先级”
- `contrast`：输出稳定结构（核心维度 + 证据来源 + 置信度）

涉及模块：

- `src/relations/exploration_service.py`
- `src/mcp/tools.py`
- `docs/overview/` 与 `docs/specs/` 同步

当前阶段说明：

- `find_bridges` 当前已补入结构分、局部图桥接信号 `graph_bridge_score` 与轻量文本重合评分，并显式返回 `graph_bridge_signal`
- `timeline_of` 当前已明确时间来源优先级：`event_time > published_at > archived_at`，并把 `structured_time_fields` 作为证据来源
- `timeline_of` 当前在多时间源并列主导时会返回 `inferred_time_field=mixed`，避免整体时间源判断偏乐观
- `contrast` 当前已补入稳定的 `comparison_dimensions` 与 `evidence_sources` 输出结构，并新增 `relation_graph_signal` 与候选级 `relation_signal_score` / `relation_types`
- 三个 Tool 当前仍属于 `partial`，但边界与限制已更明确

### 5.4 关系回填与质量验证（优先级 P1）

**目标**：在测试副本库完成一次正式回填演练并输出质量报告。

交付物：

- `backfill --apply` 演练报告（覆盖率、噪声率、冲突率）
- 最小回归数据集（10~20 条关系推理样例）

涉及模块：

- `scripts/backfill_relations.py`
- `tests/integration/`
- `docs/operations/`

当前阶段说明：

- 当前已补入 `backfill_quality_report.v1`，报告中包含 `mode`、`knowledge_scope`、`quality_gate`、`conflict_samples` 与执行上下文
- 当前 `scripts/backfill_relations.py` 已支持可选质量门禁参数：`--min-coverage`、`--max-noise`、`--max-conflict`、`--fail-on-gate`
- 当前已补入最小关系推理回归样例集：`tests/fixtures/phase_b_5_4_min_regression.yaml`
- 当前已新增 `docs/operations/关系回填质量验证指南.md`，用于规范测试副本库的 dry-run / apply 演练与回归命令

---

## 6. 退出条件（Exit Criteria）

满足以下条件即可视为 Phase B 结束、可交接 Phase C：

1. Phase B MCP Tool 有统一、稳定、版本化的输出结构
2. `collect_evidence` 具备 chunk 级证据，且能给出可追溯链路
3. `find_bridges/timeline_of/contrast` 均达到“最小可用”并解除 partial 标记
4. 有最小推理评测集与回归指标（≥10 个样例）
5. 文档与代码一致性已同步（`docs/overview`、`docs/operations/CHANGELOG.md`）

---

## 7. 风险与注意事项

- 不应把 GUI 作为 Phase B 成功的必要条件
- 关系层未形成稳定推理闭环前，避免继续扩展高阶 UI
- 证据链路必须能复现，避免只依赖隐式语义推断

---

## 8. 推荐阅读顺序

1. `docs/overview/当前事实基线-2026-03.md`
2. `docs/overview/阶段开发路线与依赖-2026-03.md`
3. `docs/operations/CHANGELOG.md`
4. 本文档
