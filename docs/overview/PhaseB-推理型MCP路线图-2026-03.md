# Phase B：推理型 MCP 路线图（2026-03）

> 文档类型：执行路线 / Roadmap  
> 创建日期：2026-03-27  
> 目的：把 Phase B 的目标、边界、任务分解、交付物与退出条件固化为可执行清单

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
- evidence 仍为文档级聚合 v1，尚未到 chunk 级证据管理

---

## 3. 未完成任务与上游缺口（确认版）

> 说明：以下为 Phase B 未完成项，以及对应“上游未完成/不足”的实际阻塞点。

1. **证据包仍是文档级 v1**  
   - 未完成项：`collect_evidence` 仍停留在文档级聚合  
   - 上游缺口：chunk 索引与证据去重/排序策略未落地
2. **`find_bridges/timeline_of/contrast` 仍是 partial**  
   - 未完成项：工具仍是弱语义/启发式版本  
   - 上游缺口：语义桥接评分、事件时间字段与对比维度规则未稳定
3. **真实库回填演练未完成**  
   - 未完成项：仅提供 `dry-run`，缺少正式 `--apply` 质量报告  
   - 上游缺口：回填质量指标体系（覆盖率/噪声/冲突率）未建立
4. **关系来源仍偏显式信号**  
   - 未完成项：高阶推理输入仍依赖显式链接/Front Matter  
   - 上游缺口：更稳定的语义关系来源未形成可复用抽取策略
5. **推理评测闭环未完成（Phase C 入口阻塞）**  
   - 未完成项：缺少最小评测集与门禁指标  
   - 上游缺口：代表性样例集与自动化回归规则未固化

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

### 5.2 证据包升级到 chunk 级（优先级 P0）

**目标**：将 `collect_evidence` 从文档级 v1 升级为 chunk 级证据聚合。

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

### 5.4 关系回填与质量验证（优先级 P1）

**目标**：在测试副本库完成一次正式回填演练并输出质量报告。

交付物：

- `backfill --apply` 演练报告（覆盖率、噪声率、冲突率）
- 最小回归数据集（10~20 条关系推理样例）

涉及模块：

- `scripts/backfill_relations.py`
- `tests/integration/`
- `docs/operations/`

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
