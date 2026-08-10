# 更新日志 (Changelog)

所有重要的项目变更都将记录在此文件中喵～ ฅ'ω'ฅ

---

> 版本口径说明（2026-03-06）：
>
> - 当前仓库基线版本：`v0.8.0-alpha`
> - `v0.6.0` 表示 CLI 入口首次稳定引入
> - `v0.7.0` 表示 MCP 能力层首次稳定引入
> - `history/` 下的 `v0.8.0-beta / v0.8.0` 里程碑文档保留阶段性背景，不直接作为当前仓库发布标签

## [Unreleased] - 2026-03-11 (Phase A 收尾 / Phase B 推理基线推进)

### M13 W3-T0：短期 Test 治理门禁（2026-08-10）

- 冻结 `source`、`packaging-contract`、`artifact-only` 三条测试 lane 的唯一 owner、
  允许输入/输出与禁止替代关系；默认离线门禁继续包含打包合同，但明确排除必须显式运行的
  Artifact-only 用例。
- 新增 fail-closed Artifact preflight runner：要求显式 Artifact、入口、manifest、fixture、
  evidence root 与条件性 harness；从仓库外 cwd 启动，隔离 Python/Conda/Provider 环境，拒绝
  仓库内路径、junction/reparse point、硬链接、路径嵌套、超时与遗留子进程。该 runner 只证明
  交接边界，不产生 `artifact_verified`。
- 冻结 W4 的 10 个 scenario / 11 行 lifecycle matrix 及 evidence 状态机；恢复 W2 capability 到
  W4 handoff 的双向映射校验，并用合成负例拒绝跨 lane 晋级、缺失 identity/evidence、非法 hash
  以及 Chat 缺少 harness identity。
- QSettings 测试改为逐用例快照并恢复默认格式与 Qt organization/application identity；自动化
  禁止调用无法可靠逆转的 `QSettings.setPath()`，异常退出路径同样执行恢复。
- 独立 CodeReview 最终为 P0=0、P1=0。最终离线证据：默认全量
  `3492 passed, 20 skipped, 21 deselected`；MCP coverage
  `758 passed, 2759 deselected`、`src.mcp=95.33%`；Phase C `16/16 tasks`、
  `151/151 checks`、全部维度 `1.0`；显式合成 Artifact lane `12 passed`。
- W3-T0 已完成，正式 W3 可以开始；可复现 Artifact、外置 deterministic loopback harness 与
  W4 Artifact E2E/发布审查尚未完成，当前没有 `artifact_verified` 结论。

### M13 W2：已发布功能源代码合同（2026-08-07）

- Workflow 运行时合同收口到真实、版本化的 `archive-url.yaml` / `archive-text.yaml`：
  严格校验 schema、step 顺序、trigger、condition、`on_error` 与 processor route，并公开
  `success/degraded/error` 终态；`search.yaml` 明确不受支持。
- Retrieval 统一 `success/no_hits/invalid/error/degraded` 五态，BM25/Vector/Hybrid/Auto
  及 GUI、CLI、MCP adapter 不再把底层错误或降级伪装成空结果/成功。BM25 不构造
  Provider，语义路径按需创建 Provider 并在索引访问前校验数量、顺序、维度和数值边界。
- MCP 发布面冻结为 stdio-only，非 stdio transport 在 bootstrap/绑定前拒绝；14 Tools、
  9 Resources、3 Prompts 的发现/调用、公开 envelope、证据 locator、聚合守恒和稳定错误
  语义已形成 source-level 合同。URL 归档覆盖 DNS、连接目标、redirect、subresource 的
  SSRF 重校验，拒绝路径不产生存储副作用。
- GUI Chat 将 session 切换、双发、停止、Provider/save 失败收口到互斥的
  `completed/stopped/error`；turn 原子持久化、知识引用、URL 上下文和保存到知识库均走
  正常 Provider/Workflow seam，源码不含内置 fake/test mode；release payload 的同一结论仍待
  W3/W4 Artifact 扫描证明。
- `tests/contracts/m13_w2.v1.yaml` 中受支持、`partial-v1` 与明确 unsupported 的源代码能力
  已登记为 `source_verified`。`mcp.http.v1` 与 W3 主责的外置
  `chat.loopback_harness.v1` 仍为 `defined`；全部 W4 handoff 保持 `artifact_pending`。
- Phase C fresh run 通过 16/16 tasks、151/151 checks（119 项版本化声明式检查 + 32 项
  自动公开 envelope 检查）、`overall=1.0`、全部维度 `1.0`、0 failed、
  `targets_met=true`。2026-07-31 的 119-check 记录保留为历史声明式基线。
- fresh 默认离线全量为 `3475 passed, 20 skipped, 9 deselected`；MCP coverage gate 为
  `755 passed, 5 deselected`，`src.mcp=95.33%`（门槛 `95%`）。
- 四条冻结工作流的独立 SubAgent 复审未发现确定性 P0/P1。历史 Vector 非 seed 候选的
  全量语义扫描按 fresh-install 范围外 P2 后置，不改变当前 fail-closed 合同。
- W2 已完成，W3-T0 Test 治理也已完成；W3 可复现 Artifact、外置 deterministic loopback harness 与
  W4 Artifact E2E/发布审查尚未完成，当前没有 `artifact_verified` 结论。

### M13 W1：运行时布局与数据安全底座（2026-08-02；2026-08-07 安全复审）

- 新增 `src/runtime/`：`RuntimeLayout` 将 bundled 只读资源与单一用户数据根分离，
  `bootstrap_runtime()` 成为 GUI、CLI、MCP 共用的资源校验、目录创建、数据库启动门禁和
  repair record 扫描入口。
- 新增 `VaultPathGateway`：所有产品 Vault 读取、原子写入、枚举、删除、隔离/恢复、
  raw preview、MCP evidence/resource 与 relation backfill 均执行 canonical containment；
  拒绝 traversal、symlink/junction/reparse point、硬链接及不可判定状态。
- Markdown 发布改为原子 no-clobber；并发同名归档只选择新后缀，不覆盖已有事实源。
  operation journal v3 同时记录文件身份与 SHA-256，补偿、隔离和崩溃恢复不再把
  same-inode 原地重写误判为原始事实。
- 新增 `StorageCoordinator` 和 durable operation journal：Markdown 主存储与
  SQLite/FTS/tags/chunks 必需投影采用补偿状态机，Vector 作为可修复辅助层；公开
  `ready/deleted/degraded/rejected/repair_required`、稳定错误码和 repair actions。
- SQLite 业务连接统一使用 `mode=rw/ro` 的 existing-only opener，缺失 DB 不再被查询代码
  隐式创建；归档的 SQLite/FTS/tags/chunks 写入收口为一个事务。迁移 010 新增
  `storage_operation_commits`，业务投影与 operation-bound commit proof 在同一事务提交，
  schema 更新为 `1.2.4`。
- Vector index/metadata 双文件发布新增持久 pair transaction marker；首次创建、运行期首份
  replace、两份 replace 后未清 marker、marker 发布后校验失败均可在重启时确定性回滚。
  未登记的普通文件替换保持现场并 fail-closed。
- migration 仅把路径完全不存在判定为 fresh；非 SQLite、缺失/非法版本表、迁移链前缀/
  必需表漂移、旧版和未来版分别 fail-closed。fresh/迁移先在 off-path 副本完成校验，
  临时数据库描述符从创建连续受控到发布，备份或脚本失败不继续、不发布半提交结果；
  M13 默认仍拒绝历史库原地升级。
- 新增 `packaging/runtime-resources.json` 只读资源 allowlist，明确排除密钥、`.env`、
  local config、数据库、Vault、vector、日志与临时文件。
- 新增/扩展 RuntimeLayout、bootstrap、Vault、migration、跨存储故障注入、Vector 崩溃恢复、
  adapter 终态和 manifest 自动化合同；独立 SubAgent 复审后未留确定性 P0/P1。
  全部验证仅使用 `.data-test` 与合成数据，未访问真实 `.data/`。
- 冻结后离线验收：unit `1583 passed, 19 skipped`；integration/blackbox/e2e
  `277 passed, 9 deselected`。Windows symlink 权限与 POSIX-only 合同按原因跳过，live 用例由
  离线入口按合同排除。
- 已知后置边界：非协作外部写者仍存在 Python/Windows 无 inode-bound unlink/rename 所致的
  极窄 ABA 窗口，Windows 掉电持久序依赖 NTFS；历史原地升级的 WAL 并发与列级 schema
  drift 继续因 fresh-install 发布范围而后置。

### P0 主线离线收口（2026-07-31）

- Phase C 固定评测在受控入口下通过 16/16 tasks、119/119 checks，
  `overall=1.0`、`citability=1.0`、0 failed、`thresholds_met=true`；gold、
  独立 proposals、检查项和阈值未降低。
- Phase B 三个探索 Tool 按 `partial-v1` 最小可用口径交付，公开响应继续声明
  `implementation_level=partial`；本次没有把受限实现改写为 full。
- FT7 generic Direct Python 离线入口已交付：支持仓库模块 `-m` 与仓库 `.py`
  脚本，拒绝 `-c`、stdin、解释器 flags 与仓库外目标；同进程安装 Python 级
  网络/子进程 guard。该机制不是 OS sandbox，也不覆盖非 Python Direct 命令。
- 开发 vault 重建脚本已完成合成 `rebuilt -> up_to_date -> checked` 演练，结果为
  schema `1.2.4`、9 migrations、3 seed。
- 冻结工作树默认离线全量为 `1684 passed, 1 skipped, 9 deselected`；MCP
  门禁为 `364 passed, 1326 deselected`，`src.mcp` 覆盖率 `96.88%`（门槛 `95%`）。
- 本轮未访问或执行真实数据；真实快照仍受 U1/G8 与迁移 FT5 阻塞。M13 是
  离线基线之后的下一阶段，不等于真实数据验收。

### 真实数据验证 Runbook v1.3：执行入口与日志安全闭环（2026-07-31）

- G0 收紧为按目标子进程验证的离线入口：5c14caa 成套机制覆盖 pytest 与 CLI/MCP，
  FT7 generic guard 现已覆盖受支持的 `-Direct` Python 仓库模块/脚本；单独路径预检或 wrapper 环境不再可替代。
- 新增 U1/G8 user-only launcher 硬前置，负责授权、`.data-test`/clone 边界、工作区文件日志
  禁用或源头脱敏；未交付前所有真实快照 CAT-U/CAT-C 步骤明确剔除，Runbook 不再给出可误执行命令。
- archive 写入统一迁移到 writable clone，授权快照根保持只读；migration 明确依赖 FT5+U1，
  不再错误声称 G0/FT7 base-only 预检能够保护 user-only migration。
- 合成种子命令固定 `--seed/--count/--output`，并纠正“纯 stdlib”描述；pytest 示例显式排除
  `network/manual`；判读模板新增“不适用 + 原因”。

### 真实数据验证 Runbook 复审修订：双通道执行模型与证据契约统一（2026-07-31）

- 建立**双通道执行模型**：Agent-safe 通道仅限不接触授权快照、不加载 `config/local.yaml`
  的静态/合成验证与已交付 base-only 工具（CAT-0）；所有读取真实快照或可能加载 local.yaml
  的实际 CLI/MCP/migrate/run-test 命令（离线与 live）一律由用户手动执行（CAT-U/CAT-C），
  Agent 不执行、不读取原始输出，只接收脱敏摘要（Runbook 0.2/2/7.1/8）。
- G0 明确为**受控入口语义**：只证明 base-only 入口本身，不改变后续子进程 `Config()` 行为；
  未接入入口的命令保持 user-only，预检不是全局保护（Runbook 7.1-5/10.3-G0/18.1）。
- 证据契约统一：T-D/E3 禁止"原样记录"；版本化记录仅含脱敏摘要、退出码、计数、哈希、
  假名 ID、时间、命令类别与模板指纹；原始证据只能存于**工作区之外、用户 ACL 隔离、
  Agent 不可读**位置，不再称 `.data-backup/` 或工作区内目录为 Agent 不可读（Runbook 12/13，模板 T-D/T-F/T-G）。
- live 阶段执行人明确为**用户**（或未来不向 Agent 暴露凭据/输出的 launcher，本次不实现）；
  离线真实快照命令同样按双通道处理（Runbook 8/15.1-15.4）。
- disposable clone 与 migration 落到可审计步骤：`clones/<clone-id>` 专属数据根、用户制备/验证
  流程（7.1-7）、历史 schema baseline（版本号/哈希）、pending migration 要求（pending=0 标记
  "未覆盖"）；migrate.py 无 `--db-path`/受控入口时对应命令不得执行，登记 FT5/FT7 前置（Runbook 15.5）。
- 校正 20.1 一致性 checklist、风险/成本表与文档版本（v1.2）。

### 真实数据验证 Runbook P0 安全/可执行性修订（2026-07-31）

- archive（URL/文本）与 `search --strategy vector/hybrid/auto` 确认会触发真实抓取/LLM/
  Embedding HTTP（数据出境），已从默认步骤移入 **live/数据出境阶段**（单独授权）；
  `PKV_RUN_LIVE` 明确为 pytest 收集开关而非应用层网络开关（Runbook 7.3/8/15.1-15.2）。
- 隔离预检不再使用 `config show`（其默认加载 `config/local.yaml`）；改为 base-only/
  fail-closed 机制（5c14caa 的 `offline_entrypoint`/`offline_runtime` 或等价实现 FT7）并
  提升为 **G0 硬前置**——未就绪则 P0/P1/P2 不可执行（Runbook 7.1/10.3/18.1）。
- 证据留存改为仅退出码、计数、哈希、假名 ID 与人工脱敏摘要；原始证据只能由用户保存在
  Agent 不可读受控位置（Runbook 12/13，模板 T-F/T-G）。
- MCP 真实快照证据验证改为待实现前置（FT6 受控 harness）：stdio 无法跨会话附着，16-task
  runner 固定 `OfflineMcpScenario`；G7 在 FT6 交付前不可执行（Runbook 10.3/15.3）。
- delete 完整性指标默认从 P1/P2 剔除，FT3 工具为其硬前置；删除/可写迁移强制在每场景
  disposable clone 内执行，快照根只读（Runbook 9/10.1/14/15.4-15.5）。
- #3 依赖统一为明确 OR 条件：（#3 dev vault 已就绪）或（合成样本预演降级）（Runbook 18.2）。
- 新增后续任务 FT6（真实快照 MCP harness）与 FT7（base-only 隔离入口工具）；FT7 后续已在本次 P0 主线离线收口中交付。

### 真实数据验证 Runbook 规划（2026-07-31）

- 新增 `docs/operations/testing/真实数据验证Runbook.md`：规划优先、授权后执行的小样本
  真实数据验证流程，定义 P0 小样本预演 / P1 受控评测 / P2 定期回归三阶段。
- 新增空白记录模板 `docs/operations/testing/templates/真实数据验证记录模板.md`
  （T-A 授权单 / T-B 样本清单 / T-C 人工判读表 / T-D 自动门禁 / T-E 失败分流 /
  T-F 审计证据 / T-G 留存清除 / T-H 阶段执行记录）。
- 覆盖样本选择与代表性、数据分级最小化（D1–D4）、授权快照唯一取数通道、脱敏/
  假名化、环境与凭据隔离、命令类别（CAT-A/B/C/D）、人工判读表、自动门禁
  （G1–G7）、失败分流（F1–F5）、审计证据、留存清除与回滚。
- 明确合成 fixture 与真实数据的边界及不可作为发布结论的指标（第 16 章）。
- 覆盖 archive / search / MCP evidence & citation / delete / migration 五类验证。
- 与 `codex/review-testcase-repair`（5c14caa，当时按只读参考规划）及 #3 开发 vault
  轻量重建（当时仅定义接口与依赖）的依赖顺序已记录（第 18 章）；两项后续均已纳入 P0 主线收口。
- 未实现任何访问真实数据的脚本；安全工具/TestCase 扩展（FT1–FT5）登记为后续任务。
- 本任务不读取、不复制、不导出真实数据，不访问 `.data/`，不做真实 API/网络测试。

### Phase B citation 合同收口（2026-07-29）

- `collect_evidence` 为每条 chunk 证据新增稳定唯一的
  `citation_source` / `citation_locator`。
- `find_bridges` 为每个桥接候选新增可逐跳核验的 `evidence_path`。
- `timeline_of` 为每个时间点新增 source、原始 source URL/file path 和定位
  实际时间字段的稳定 locator。
- `contrast` 为 shared/only tags、重叠条目及关系图 signal 新增
  `comparison_dimensions.provenance` 候选—来源映射。
- 三个探索 Tool 继续声明 `implementation_level=partial`，并保留原有
  `limitation_notes`、`evidence_sources` 与降级边界。
- 固定离线评测达到 16/16 任务、119/119 检查、`overall=1.0`、
  `citability=1.0`、0 failed、`targets_met=true`（兼容字段 `thresholds_met=true`）；gold、baseline
  proposals、断言、fixture 与阈值未弱化。
- 评测策略升级为 `threshold_enforced`，CLI 新增
  `--enforce-thresholds` 退出码门禁。

### Phase C 最小评测闭环（2026-07-29）

- 新增 `evals/mcp_quality/tasks.v1.yaml`，固定 16 条离线推理任务。
- 新增可复现 runner/scorer，通过真实 FastMCP 调用验证 Tool、参数、结果、
  chunk 证据和 partial/degraded 契约。
- 初始基线为 115/119（96.64%）；4 个未通过检查均集中在可引用性，
  已形成按优先级排序的 Phase B 失败矩阵。
- 新增 scorer 单元测试与基线集成回归；不依赖 API key、网络或生产 `.data`。
- gold taskset 与 baseline proposals 独立存储，并用错误 Tool/参数/chunk query
  反例证明评分能够检出错配。
- 所有公开评测读写路径在任何读取、建目录或写入前拒绝生产 `.data`。
- Phase C 初始 CI 明确为 baseline-only：阻断 schema/失败矩阵漂移，不阻断当时
  citation 目标；Phase B 完成后再升级质量门禁。

### ✨ 新增功能

- ✅ **关系层基础骨架**
  - `src/relations/models.py`
  - `src/storage/relation_store.py`
  - `scripts/migrations/006_add_relations_foundation.sql`

- ✅ **低歧义关系抽取与安全回填**
  - `src/relations/extractors.py`
  - `scripts/backfill_relations.py`
  - `RelationStore.delete_outgoing_relations_for_knowledge()`

- ✅ **一跳关系查询与验证闭环**
  - `src/relations/query_service.py`
  - `RelationQueryResult.grouped_items`
  - `tests/unit/test_relation_query_service.py`
  - `tests/integration/test_relation_query_pipeline.py`

- ✅ **内部多跳子图查询基础**
  - `RelationQueryService.query_subgraph()`
  - `RelationSubgraphNode` / `RelationSubgraphResult`

- ✅ **最小关系解释基础**
  - `RelationQueryService.explain_relation()`
  - 直接关系优先，找不到时降级到受限跳数内的最短解释路径

- ✅ **最小推理型 MCP Tool**
  - `query_subgraph`
  - `explain_relation`
  - `collect_evidence`
  - `find_bridges` (partial)
  - `timeline_of` (partial)
  - `contrast` (partial)

- ✅ **迁移链健康检查**
  - `scripts/migrate.py --health-check`
  - `MigrationManager.run_health_check()`

- ✅ **Phase B 5.4 回填质量验证基线**
  - `backfill_quality_report.v1`
  - `scripts/backfill_relations.py` 质量门禁参数
  - `tests/fixtures/phase_b_5_4_min_regression.yaml`
  - `docs/operations/关系回填质量验证指南.md`

### 📝 变更

- `004_add_chat_sessions.sql` 标准化为 `v1.1.1`
- `005_add_review_system.sql` 标准化为 `v1.1.2`
- `006_add_relations_foundation.sql` 作为 `v1.2.0` 接入当前有效迁移链
- Batch2 当前覆盖低歧义关系：Markdown 显式链接、Front Matter `related_docs`，以及 Front Matter 关系字段 `children` / `version_of`
- `scripts/backfill_relations.py` 默认 `dry-run`，仅在显式传入 `--apply` 时写入关系表
- 当前已补上内部 `query_subgraph` 多跳子图遍历基础，并将 `query_subgraph` / `explain_relation` 暴露为最小推理型 MCP Tool，但仍未进行真实库正式回填执行
- 当前已补上 `collect_evidence(question, include_chunks=False/True)` 的 chunk-aware 证据包聚合路径，并将其接入只读 MCP Tool；默认仍保持文档级兼容行为
- 当前已补上 `collect_evidence` 的 `chunk_retrieval_status` 固定合同：`not_requested` / `success` / `no_hits` / `path_unavailable` / `search_error`
- 当前已补上 `find_bridges`、`timeline_of`、`contrast` 的 partial implementation，并在返回结构中显式标注 `implementation_level=partial`
- 当前已增强 `find_bridges`、`timeline_of`、`contrast` 的探索深度：桥接结果新增局部图桥接信号 `graph_bridge_score` / `graph_bridge_signal`，时间线新增 `structured_time_fields` 证据来源并优先使用真实时间字段，对比结果新增 `relation_graph_signal` 以及候选级 `relation_signal_score` / `relation_types`
- 当前 `timeline_of` 在多时间源并列主导场景下返回 `inferred_time_field=mixed`，避免单一高优先级字段导致整体偏乐观
- 当前已补上关系回填质量报告结构与可选质量门禁参数，可在测试副本库执行 `--apply` 演练并输出 JSON/YAML/Markdown 报告
- 当前已补上最小关系推理回归样例集，并通过数据文件驱动的集成测试固定 Phase B 5.4 基线
- 查询结果当前优先按 `relation_type` 分组，组内按 `weight DESC -> updated_at DESC -> relation_id ASC` 稳定排序
- `README.md`、当前事实基线、阶段路线与差异清单同步更新为 `Phase A / T1+T5` 口径

### 📦 当前工作区增量（2026-03-10 ~ 2026-03-31）

- `src/relations/models.py` 当前新增 `RelationSubgraphNode` 与 `RelationSubgraphResult`，作为内部多跳子图查询的统一返回结构
- `src/relations/query_service.py` 当前新增 `query_subgraph(seed_knowledge_id, depth, ...)`，基于现有一跳查询服务做受限 BFS 子图扩展
- `src/relations/query_service.py` 当前新增 `explain_relation(a, b, ...)`，优先返回直接关系解释，失败时降级为最短路径解释
- `src/relations/evidence_service.py` 当前新增 `collect_evidence(question, ...)`，围绕问题聚合 chunk-aware / 文档级兼容证据包，并为候选条目补充相对种子条目的关系解释
- `src/relations/exploration_service.py` 当前增强 `find_bridges(seed_knowledge_id, ...)`、`timeline_of(topic, ...)` 与 `contrast(topic_a, topic_b, ...)`：桥接结果新增局部图桥接信号，时间线优先使用结构化真实时间字段并支持 `inferred_time_field=mixed`，对比结果新增跨主题显式关系路径信号
- `src/mcp/tools.py` 当前新增 `query_subgraph`、`explain_relation`、`collect_evidence`、`find_bridges`、`timeline_of` 与 `contrast`，把 Phase B 的最小关系推理与受限探索能力接入 MCP
- `tests/unit/test_relation_query_service.py` 当前补充两跳子图查询与深度限制断言
- `tests/integration/test_relation_query_pipeline.py` 当前补充 `backfill -> query_subgraph` 与 `backfill -> explain_relation` 联通验证，并把样例图扩展到 `Alpha -> Gamma -> Delta`
- `tests/unit/test_relation_exploration_service.py` 与 `tests/integration/test_relation_query_pipeline.py` 当前补充探索能力增强后的断言，覆盖图桥接信号、真实时间优先级与关系图对比信号
- 当前工作区文档同步范围已覆盖 `README.md`、`当前事实基线-2026-03.md`、`当前战略与路线收敛-2026-03.md`、`PhaseB-推理型MCP路线图-2026-03.md`、`docs/modules/relations/RELATION_LAYER_DESIGN.md`、`docs/specs/interfaces/Relations接口规范.md` 与 `docs/modules/mcp/` 下的相关文档
- 该 2026-03 增量在当时应理解为 `Phase A closeout with Phase B closeout pending`；2026-07-31 当前口径以本节顶部 P0 主线离线收口记录为准

### 🧪 测试

- 新增 `tests/unit/test_relation_store.py`
- 新增 `tests/integration/test_relations_migration.py`
- 新增 `tests/unit/test_relation_extractors.py`
- 新增 `tests/integration/test_relation_backfill.py`
- 新增 `tests/unit/test_relation_query_service.py`
- 新增 `tests/integration/test_relation_query_pipeline.py`
- 新增 `tests/unit/test_migration_manager_versions.py`
- 新增 `tests/unit/test_migration_health_check.py`
- `Phase A` 当时记录了裸 pytest 命令；现行等价回归必须经
  `scripts/run-test.ps1 -Direct -DataRoot .data-test\phase-a-regression -Command
  @("pytest", "tests/integration/test_relations_migration.py",
  "tests/unit/test_relation_store.py", "tests/unit/test_relation_extractors.py",
  "tests/integration/test_relation_backfill.py",
  "tests/unit/test_relation_query_service.py",
  "tests/integration/test_relation_query_pipeline.py",
  "tests/unit/test_migration_manager_versions.py",
  "tests/unit/test_migration_health_check.py", "-q")` 执行。

## [v0.8.0-alpha] - 2026-03-06 (当前仓库基线对齐)

### ✨ 新增功能

- ✅ **当前事实基线文档**
  - `docs/overview/当前事实基线-2026-03.md`
  - `docs/overview/阶段开发路线与依赖-2026-03.md`
  - `docs/overview/文档与代码差异清单-2026-03.md`

### 📝 变更

- 统一仓库当前基线版本为 `v0.8.0-alpha`
- `README.md` 当前版本口径改为“当前仓库基线”
- `src/__init__.py` 与 `src/main.py` 版本号对齐到 `0.8.0-alpha`
- CLI 黑盒测试中的版本断言同步更新

### 📚 文档更新

- 补齐版本口径说明，区分“当前仓库基线”和“能力首次引入版本”
- 明确 `history/` 下的后续里程碑文档不直接承担当前发布标签职责

---

## [v0.7.0] - 2026-02-19 (M8+M9 MCP 服务)

### ✨ 新增功能

- ✅ **MCP 只读服务**
  - `search_knowledge`
  - `get_entry`
  - `list_entries`
  - `list_tags`
  - `get_stats`

- ✅ **MCP 写入与关联能力**
  - `archive_url`
  - `archive_text`
  - `get_related`

- ✅ **MCP Prompt / Resource**
  - 3 个 Prompt 模板
  - 4 个 Resource

- ✅ **安全加固**
  - SSRF 拦截
  - 文本长度限制
  - Bearer Token 认证

### 📝 变更

- `README.md` 中的 MCP 功能说明与当前真相源保持一致
- `src/mcp/` 模块成为当前一等交互面

---

## [v0.6.1] - 2026-02-16 (AI 安全 + 数据库迁移)

### ✨ 新增功能

#### AI 安全测试功能 ✅

- ✅ **AI 安全规则** ([.ai-safety-rules.md](../../.ai-safety-rules.md))
  - 禁止操作生产数据目录 (.data/)
  - 强制使用测试环境 (run-test.ps1)
  - 备份要求和典型场景处理

- ✅ **Claude 忽略文件** ([.claudeignore](../../.claudeignore))
  - 防止 AI 访问 .data/ 生产数据
  - 防止 AI 访问 .env 敏感配置
  - 防止 AI 修改 .data-backup/ 备份

- ✅ **环境检测脚本** ([scripts/check-environment.ps1](../../scripts/check-environment.ps1))
  - 自动检测生产/测试环境
  - 显示数据库统计信息
  - 给出安全建议

- ✅ **测试环境文档**
  - [docs/operations/testing/AI安全与数据库升级完整方案.md](./testing/AI安全与数据库升级完整方案.md) - 总结文档
  - [docs/operations/testing/测试环境隔离指南.md](./testing/测试环境隔离指南.md) - 完整指南
  - [docs/operations/testing/测试环境快速开始.md](./testing/测试环境快速开始.md) - 3 分钟入门
  - [docs/operations/testing/测试环境演示.md](./testing/测试环境演示.md) - 完整演示

#### 数据库增量迁移系统 ✅

- ✅ **迁移管理器** ([src/storage/migration_manager.py](src/storage/migration_manager.py))
  - 获取当前数据库版本
  - 扫描待执行的迁移脚本
  - 自动执行迁移（支持自动备份）
  - 语义化版本号比较

- ✅ **命令行迁移工具** ([scripts/migrate.py](scripts/migrate.py))
  - 交互式升级（python scripts/migrate.py）
  - 自动升级（--auto）
  - Dry-run 模式（--dry-run）
  - 版本查看（--version）

- ✅ **迁移脚本目录** ([scripts/migrations/](scripts/migrations/))
  - 001_initial_schema.sql (v1.0.0) - M1 初始 Schema
  - 002_add_cli_tables.sql (v1.1.0) - M6 CLI 统计表
  - README.md - 迁移脚本说明

- ✅ **Schema 版本管理表**
  - schema_version 表追踪升级历史
  - 记录版本号、描述、应用时间

- ✅ **数据库迁移文档** ([docs/operations/数据库迁移指南.md](./数据库迁移指南.md))
  - 版本管理机制
  - 迁移脚本标准
  - 完整升级流程
  - 回滚程序

#### 测试脚本 ✅

- ✅ **手动测试脚本** ([tests/manual_test_text_archive_safe.py](tests/manual_test_text_archive_safe.py))
  - 环境检测演示
  - 纯文本归档测试（知乎回答样本）
  - 生产环境验证

### 🐛 Bug 修复

- ✅ **TextFallbackProcessor** ([src/processors/text_fallback_processor.py](src/processors/text_fallback_processor.py))
  - 修复 source_type：`"text_fallback"` → `"text"`
  - 符合 M5.1 Bug 修复规范

### 📝 变更

- .gitignore - 添加 .env.test, .data-test/, .data-backup/ 排除
- docs/README.md - 更新文档总索引与分类说明

### 📚 文档更新

- 新增 7 份核心文档（测试环境 + 数据库迁移）
- 更新文档分类清单（从 53 份增至 60 份）

---

## [v0.6.0] - 2026-02-16 (M6+M7)

### ✨ 新增功能

#### Milestone 6: CLI 入口与交互界面 ✅

- ✅ **CLI 入口** ([src/main.py](src/main.py))
  - Click 命令组
  - 全局参数（--verbose, --debug）
  - 版本信息（0.6.0）

- ✅ **6 个核心命令** ([src/cli/commands.py](src/cli/commands.py))
  - pkv archive - 归档内容（集成 WorkflowEngine）
  - pkv search - 搜索知识库（集成 QueryRouter）
  - pkv show - 显示条目详情
  - pkv list - 列出所有条目
  - pkv config - 配置管理（show/get/set）
  - pkv stats - 统计信息

- ✅ **终端 UI 组件** ([src/cli/ui.py](src/cli/ui.py))
  - Rich.Progress 进度条
  - Rich.Table 表格
  - Rich.Panel 面板
  - Rich.Confirm 确认对话框

- ✅ **输出格式化器** ([src/cli/formatters.py](src/cli/formatters.py))
  - JSON 格式化
  - Markdown 格式化
  - 搜索结果表格
  - 条目详情面板

- ✅ **测试覆盖**
  - tests/unit/test_cli_commands.py（17 个单元测试）
  - tests/unit/test_cli_ui.py（UI 组件测试）
  - tests/unit/test_cli_formatters.py（格式化器测试）
  - tests/integration/test_cli_e2e.py（端到端测试）
  - 测试覆盖率 ≥ 80%

#### Milestone 7: 文档完善与交付 ✅

- ✅ **使用手册** ([docs/operations/使用手册.md](./使用手册.md))
  - 快速开始
  - 核心功能详解
  - 配置和定制
  - 故障排查

- ✅ **维护指南** ([docs/operations/维护指南.md](./维护指南.md))
  - 系统架构
  - 日常维护
  - 性能监控
  - 扩展开发

- ✅ **环境变量模板** ([.env.example](.env.example))
  - PKV_LLM_API_KEY
  - PKV_EMBD_API_KEY

### 📝 变更

- requirements.txt - 添加 Click 依赖
- docs/operations/API文档.md - 补充 CLI 命令参考

### 🧪 测试

- 单元测试: 17+ 个测试用例
- 集成测试: 端到端测试通过
- 测试覆盖率: ≥ 80%

### 📦 依赖

- click>=8.0.0（新增）

---

## [v0.1.0] - 2026-02-14

### ✨ 新增功能

#### Milestone 1: 基础设施层 ✅

**核心模块**:

- ✅ **配置系统** ([src/utils/config.py](src/utils/config.py))
  - YAML 配置文件支持
  - 环境变量支持
  - 单例模式实现
  - 自动创建数据目录

- ✅ **Markdown 存储** ([src/storage/markdown_store.py](src/storage/markdown_store.py))
  - YAML Front Matter 解析
  - Entry 数据类定义
  - 文件保存和加载
  - 文件名自动生成

- ✅ **SQLite 存储** ([src/storage/sqlite_store.py](src/storage/sqlite_store.py))
  - 完整 Schema 设计（5 张表）
  - FTS5 全文搜索支持
  - jieba 中文分词集成
  - 自动触发器同步

- ✅ **向量存储** ([src/storage/vector_store.py](src/storage/vector_store.py))
  - hnswlib HNSW 算法
  - 文档级和分块级向量索引
  - 高效近似最近邻搜索

- ✅ **文本处理** ([src/utils/text_utils.py](src/utils/text_utils.py))
  - jieba 中文分词
  - 文件名清理
  - 字数统计

**开发工具**:

- ✅ **验证脚本** ([src/utils/verify_setup.py](src/utils/verify_setup.py))
  - 全面的系统验证
  - 模块测试
  - 详细日志记录

**安装脚本** ([scripts/](scripts/)):

- ✅ **Conda 方案** (推荐) ⭐
  - `setup-conda.ps1` - 自动创建 Python 3.11 环境
  - `test-conda.ps1` - Conda 环境测试
  - 完整测试验证通过

- 📦 **Legacy venv 方案** (已归档)
  - 移动到 `scripts/legacy/` 目录
  - 不推荐使用（Python 3.13 兼容性问题）
  - 详见 `scripts/legacy/README.md`

**文档**:

- ✅ [RUN_ME_FIRST.md](../../RUN_ME_FIRST.md) - 快速开始指南
- ✅ [scripts/README.md](../../scripts/README.md) - 脚本详细说明
- ✅ [QUICKSTART.md](QUICKSTART.md) - 安装教程
- ✅ [docs/operations/开发环境搭建.md](./开发环境搭建.md) - 详细环境搭建指南

### 🐛 问题修复

- ✅ 修复 pytest 版本冲突（8.0.0 → 7.4.4，兼容 pytest-asyncio）
- ✅ 升级 lxml 到 5.3.0（支持 Python 3.13）
- ✅ 升级 playwright 到 1.48.0（支持 Python 3.13）

### ⚠️ 已知问题

- Python 3.13 存在依赖兼容性问题（`lxml`、`greenlet` 编译失败）
  - **解决方案**: 使用 Conda 创建 Python 3.11 环境
  - **影响**: 仅影响使用 Python 3.13 的用户

### 📝 技术栈

**核心依赖**:

- Python 3.11
- SQLite 3.35+ (FTS5)
- hnswlib 0.8.0
- jieba 0.42.1
- python-frontmatter 1.1.0
- pyyaml 6.0.1

**AI 服务** (Milestone 2):

- DeepSeek API (摘要生成)
- OpenAI API (Embedding)

**开发工具**:

- pytest 7.4.4
- black 24.1.1
- mypy 1.8.0

### 🎯 下一步计划

**Milestone 2: AI 服务层** (计划中):

- [ ] DeepSeek 客户端封装
- [ ] OpenAI 客户端封装
- [ ] Embedder 向量化服务
- [ ] Prompt 模板管理

**Milestone 3: 内容处理器** (计划中):

- [ ] 微信文章处理器
- [ ] 知乎内容处理器
- [ ] 通用网页处理器

---

## 版本说明

版本格式: `v主版本.次版本.修订版本`

- **主版本**: 重大架构变更或不兼容更新
- **次版本**: 新功能添加（向后兼容）
- **修订版本**: Bug 修复和小改进

---

*作者: 幽浮酱 ฅ'ω'ฅ*
*项目代号: Personal Knowledge Vault*
