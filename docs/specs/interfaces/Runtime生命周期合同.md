# Runtime 生命周期合同（R2 基础）

状态：R2 核心合同与其上的 R4 Q0→Q1′→Q2 内部生命周期均已完成 source acceptance；
CLI/MCP 等适配器只能投影本合同，不能重新解释或绕过它。R4 证据不提升 Artifact/release
状态，见 [R4-E 源码验收记录](../../overview/R4-E-源码验收记录-2026-09.md)。

2026-10-24：上述是原基线的 source 证据。当前工作树另含 R4-P1 public rebuild 与 P1-B，
其本轮已知缺口和待实现修订见
[生命周期一致性修复计划](../../overview/生命周期一致性修复设计与分阶段计划-2026-10.md)。
在该冻结时点，本文标注的目标状态、字段和恢复入口均未因文档修订成为产品能力。
W1 的具体协议与持久字段分别以
[v2 接口冻结合同](./生命周期一致性v2接口冻结合同-2026-10.md) 和
[迁移冻结方案](../database/生命周期一致性迁移冻结方案-2026-10.md) 为准；红测试证据不表示修复完成。

2026-10-25 W2 更新：R-01 的公共重建根身份与完整已捕获 snapshot 确认绑定已修复，
identity/Kernel 定向运行 `49 passed、7 deselected`、身份 CLI/MCP 黑盒 `7 passed`，
最终组合公共流程黑盒 `9 passed`，均 exit 0；具体范围与日志见
[W2 修复记录](../../overview/W2-身份队列与CLI门禁修复记录-2026-10.md)。
这些运行不累加为全量计数。W2 保持 v1 durable replay 算法；该阶段尚未实施 v2 API/schema/
ledger/grant 迁移，R-02 的 D 阶段恢复及 R-06 的预算重入继续为 known_gap。

2026-10-25 W3 更新：Outcome 输入事务、任意版本发布、四元组 review 与 SQLite `014` /
schema `1.2.8` 已实现。SDK `0.9.0` 为本地兼容 build，API 保持 `1.0.0`；新 v2 入口与
旧入口并存，legacy review 发出弃用警告但保留原签名和既有语义，R-04 仍为 known_gap。
Embedding v2、ledger 3、grant 2 / publish-ready proof 尚待 W4，API-major-2 切换尚待 W5。
组件证据与最终验收状态以
[W3 实施记录](../../overview/W3-Outcome一致性与版本审批实施记录-2026-10.md)为准；
本地 build 和合成迁移不改变 release hold，也不表示已执行用户数据迁移。

## 决策

运行时启动不再只有“启动即创建/恢复”的一种语义。新入口分成三个明确阶段：

```text
inspect_runtime(config)  ──只读──>  RuntimeInspection
plan_runtime(inspection) ──只读──>  RuntimePlan
execute_runtime_plan(plan, confirmation) ──确认后──> RuntimeExecution
```

`bootstrap_runtime()` 保留为历史兼容入口，默认仍会建立目录、初始化 fresh
database 并执行 journal recovery；它不能被 reinterpret 成 inspect。R2 仅增加
`recover_interrupted=False` 的显式兼容开关，供已确认的 fresh setup 避免不必要的
recovery 调用。

## 只读检查合同

`inspect_runtime(config)` 不得：

- 创建目录、数据库、索引、锁或 runtime snapshot；
- 执行 migration、journal recovery 或任何补偿；
- 建立 Provider 客户端、发起网络请求或读取真实 Provider 凭据以外的输出；
- 回退读取旧的业务 `local.yaml` 作为 runtime snapshot。

它返回 frozen、可 JSON 序列化的 `RuntimeInspection`、`RuntimeIssue` 和
`ProviderValidation`。序列化结果不包含路径、API key、完整配置、journal payload 或
Provider 返回内容。

数据库/数据根解释为：

| 发现 | readiness |
| --- | --- |
| 数据根不存在或为空，且数据库 fresh | `setup_required` |
| 非空数据根缺数据库、异常数据库、legacy journal 或 in-progress journal | `repair_required` |
| 数据库比 bundled migration chain 旧 | `upgrade_required` |
| 数据库和 journal 正常、Provider 结构合法、无密钥 runtime snapshot 有效 | `ready` |

Provider 的 structural validation 只验证本地配置结构。实际 LLM/Embedding probe
仅是计划中的 `validate_providers` action，必须有 `allow_network=True` 的显式确认；
测试只使用注入的 fake probe。

`<data-root>/config/local.yaml` 是版本化、无密钥的 runtime contract，不是业务
配置输入。R2 v1 的必需 base 字段为：

```yaml
schema_version: 1
database:
  schema_version: "1.2.8" # W3 bundled migration chain 的 canonical semver
embedding:
  provider: openai_compatible
  fingerprint:
    base_url_sha256: "<64 lowercase hex>"
    embedding_model: text-embedding-3-small
    embedding_dim: "1536"
```

base 字段缺失、类型不符、未知标量字段、YAML/路径错误，或任何层级出现
`api_key`、token、cookie 等敏感字段，均为 `runtime_snapshot_invalid` /
`repair_required`，绝不把它解释为可用配置。未来功能可保存**无密钥的 mapping
extension**（例如 R4 的 embedding generation）；R2 严格校验自身 base，不丢弃
extension，也不承担其语义校验。

不存在 snapshot 是可见的 `degraded` 状态。结构有效但其 database schema 或当前
Embedding provider / endpoint hash / model / resolved dim 与已发布 contract 不一致时，
状态为 `drifted` / `degraded`，给出 `runtime_snapshot_drift`；R2 不覆盖快照、更不
隐式重建向量，必须先展示 R4 rebuild 的影响与确认。

R2 的已确认 fresh setup 会写入最小 snapshot：schema version、当前数据库 schema
version，以及不含 endpoint 明文/API key 的 Embedding provider contract。它通过共享
`RuntimeSnapshotStore` 的 read → semantic merge → CAS publish 写入，因而不会清除 R4
generation pointer 等 extension；CAS 发现 snapshot 在执行中变化时返回
`runtime_plan_stale`。发布后再次只读校验，成功后才成为 `ready`。R4 staged core 已在该
基础上实现 generation compatibility/rebuild/retention/atomic pointer；其精确 runtime
内部合同与独立 public adapter 边界见
[Embedding Generation 生命周期合同](./EmbeddingGeneration生命周期合同.md)。R2 不把现有索引
重建或 snapshot repair 自动化。

Provider structural validation 只在本地检查配置。被确认的 actual probe 必须返回一个
`1..65536` 的 Embedding 维度；显式声明的 `dim` 必须与它相等。`dim: auto` 只有在 probe
成功且数据库已确认 ready（fresh setup 时即初始化成功后）才会持久化解析维度并发布 snapshot。probe 不返回维度、
越界或不匹配均为 `provider_protocol_failed`，不会继续初始化/发布。

## Plan 与执行合同

每个 `RuntimeAction` 都公开：稳定 kind、影响说明、是否需要确认、是否需要网络以及本阶段
是否可执行。`repair_runtime`、`recover_journal` 和 `upgrade_database` 目前只产生透明计划，
不能由 R2 基础层自动修改既有数据。

执行前必须重新 inspect 并比较 revision；任意数据根、数据库、journal、snapshot 或
Provider 配置变化都以 `runtime_plan_stale` fail-closed。普通 archive/workflow 在飞行中仍
保持其 captured Config B；但 lifecycle 的确认边界会以 `Config.user_config_source_revision()`
安全复核其最初加载的可编辑用户配置源，并在 lease 内调用同根 `reload_snapshot()` 建立候选
图。因此外部文件（包括 API key 或 `storage.data_root`）在 inspect 后发生字节级变化，一律在
lease / Provider probe / 写入之前以 `runtime_plan_stale` 拒绝，必须重新 inspect/plan；直接
`Config.reload_snapshot()` / Kernel reload 的数据根改向仍以 `data_root_switch_required` 拒绝。
如果用户配置源本身无法经 no-follow 路径规则安全读取，inspect 直接给出
`repair_required`，也不会生成可执行的 Provider 或初始化动作。
revision 只含进程私有 HMAC，HMAC、key、路径和 endpoint 原文都不会进入 inspection / plan /
log / snapshot。API key 不属于 stored
embedding contract，因此 key rotation 本身不触发 vector rebuild。写入或网络确认缺失返回
`confirmation_required`。稳定错误码还包括 `setup_required` 与 `repair_required`。

这里的私有 Config-source revision 属于 R2 进程内 fence。R4-P1 跨进程 public plan 不直接
复用该私有 HMAC。W2 已为现有 v1 公共确认补齐独立稳定的 `data_root_identity_sha256`
和 `runtime_snapshot_sha256`：前者来自同一 immutable Config 的根身份，后者复用同次
无密钥验证读取的完整 snapshot 原始字节摘要，不另读新快照拼接确认材料。它们与既有
source、pointer/manifest、Provider contract、policy、v1 action 和 force 一起绑定。
不同根即使内容和配置相同也不能共享确认；完整 snapshot 字节漂移使旧确认失效，
同根未变时仍可跨进程重算。执行前及 A 阶段的 Core 重检继续防止捕获后的竞态。

W2 只加强 public plan identity，保留 v1 durable replay 的原材料与编码，避免已存在
processing 记录因算法换代而失去防重复效果。根由独立 ledger 文件隔离，旧 replay 不区分
完整 snapshot 是保守围栏；不能将新 plan_id 当作重复调用 Provider 的许可。
W1 冻结的 v2 分域 identity、`budget_check_period` 和 ledger/grant 升级仍待 W4：
新网络计划届时绑定用户可见预算期间，publish-only 计划绑定原 operation 与当前发布材料、
不新建网络预算；v2 material replay 排除 force/期间。R-02/R-06 不因本次身份修复关闭。
具体规则与阶段证据以 R4-P1/ER 合同及上述 W2 记录为准。

`execute_runtime_plan(..., writer_lease_factory=...)` 已预留 R3 的跨进程单写接点。factory
接收这次计划绑定的显式 Config，返回上下文管理器；获得 lease 后还会再次 inspect，避免
等待锁期间混合旧计划与新状态。R2 不替代 R3 的 lease 实现。

唯一被 re-inspect 视为执行基础设施的文件是
`<data-root>/runtime/write.lease` 及其空父目录：缺失、空数据根或仅此 lease anchor 都是
同一 fresh state。数据库、journal、runtime snapshot、Provider contract 与任何其他数据根
内容仍会使计划 stale；lease anchor 不能掩盖真实数据漂移。

## R3.1 writer inventory

`src.runtime.writer_inventory.DATA_ROOT_WRITER_INVENTORY` 是版本化的
data-root 持久写入清单（当前版本 `4`）。它明确区分：

- 产品 mutation owner：已确认 lifecycle、Application archive、Kernel delete/chat、
  Embedding generation、内部自动 AI lifecycle（task/用量/generation）、audit 与 runtime file logging；
- `.data-test` 下的 offline fixture；
- 必须在入口 fail-closed 的历史 maintenance/setup 脚本。

产品 sink 在创建目录、打开可写文件、发布 snapshot、创建/恢复显式 Config-bound 的 writable
`VectorStore` 或 append audit 前，都必须验证当前 task/thread 持有同一 `RuntimeLayout` 的 R3
lease。缺失能力一律为 `write_busy` / `write_lease`，不是隐式取锁、更不是静默写入。窄能力例外
包括已被 Q0 claim 的 task-private temporary asset grant：其父目录在 lease 内创建，发布时复核
task/token/fence/expiry，且当前只能写自己的 temporary-image 叶子；它不授权 Vault、SQLite、vector、日志、
config 或其他任务目录。只读/status 路径不获取 lease；`pkv.log` 为 delayed、layout+immutable-
Config binding 的 handler，只有匹配 binding 的 mutation scope 才能触发打开、轮转或追加。reload
会替换新的 binding，同时保留仍在飞行的旧 binding handler 至其 mutation 排空，避免旧写入混入
新 snapshot。

R4-P1 已另引入 operation-private generation stage grant，允许在根 lease 外只写自己的
staging leaves，不授权 final generation、snapshot、共享 ledger、audit 或日志。2026-10
目标进一步要求在该窄能力内封存 publish-ready proof；D 争锁后不能为了记录
awaiting_publish 而绕过根 lease 写共享状态。effective 状态可由完整 proof 与 pointer
只读推导，获得 lease 后再 reconcile；proof 字段、grant 到期后的重新采用及错误终态见
[R4-P1 计划](../../overview/R4-P1-公共Embedding重建适配器实施计划-2026-09.md)。这项补充尚待实现验收。

## R4 内部内容与自动 AI 生命周期

schema `1.2.6` 在不改变公开 CLI/MCP/Kernel capability 的前提下增加私有 Q0→Q1′→Q2
状态机。它是 archive 输入的实现细节，不是可由 adapter 直接调用的后台 daemon：

- Q0 先在 writer lease 内接受 ingress identity/hash，并创建以 opaque `task_id` 和 `owner_fence`
  命名的 private temporary workspace；再在 lease 外运行 crawler、文本或已授权文件前处理。它只保存
  private spool 引用，且所有完成/失败 transition 都受 claim token、expiry 和 owner fence 约束。URL
  抓取的原始 HTML 只在内存中处理，不落本地副本；当前 workspace 仅允许显式 temporary-image，文件
  附件另待独立合同，Q1′ core commit 或 Q0 terminal rejection 后 best-effort 安全释放。
- Q1′ 是 Markdown、SQLite metadata/chunks 和 `DerivationPatch` 的唯一内容 writer。每个已证明
  core commit 同事务登记可恢复 handoff，先发布 non-ready generation binding，后激活 Q2。
- Q2 在重新捕获 source/target、确认当前 automation policy、not-before/retry 与 token/可选金额
  reservation 后才可创建 Provider。Provider 输出不能直接写内容：summary/tag 只能经 immutable
  patch 回送 Q1′；generation 仍遵守 staged validation + pointer CAS。

公开 archive 结果在 Q0/Q1′ 未完成时投影为“前处理中/入库中”；只有 `core_committed` 后才可称
文档已保存。Q2 的 `processing`、`retry_required`、`budget_paused` 与
`authorization_required` 是可操作的降级状态，不可被 adapter 重写为成功或空检索。

W2 已统一同步/异步 Q1 的 archive/delete/apply_ai_patch 执行与恢复分发，并实现轮内去重、
持久退避及独立任务的有界推进；消费者不能把不能执行的动作持续领取为自重试。
Q1-01..05 对 pending/retry patch → 同步 delete、patch 幂等及零额外 Provider 的阶段证据
见 [W2 修复记录](../../overview/W2-身份队列与CLI门禁修复记录-2026-10.md)。
既有物化 patch 的本地提交/恢复不授权新的 AI retry；
后者按下一主线的显式 attempt 目标另行实施，不能靠队列修复混入自动收费。

## P1-B traceable outcome lifecycle

schema `1.2.7` 新增 SQLite-authoritative 的 `outcome_topics`、`outcome_evidence`、
`outcome_claims`、`outcome_claim_evidence` 与 `outcome_briefs`。它们不改变 R4 archive 的
Q0/Q1′/Q2 生命周期，也不触发 runtime setup、Provider probe 或 generation rebuild。

- Outcome 的 source binding 只接受 canonical PKV locator 和 captured digest；source drift/delete
  在 public read 中可见，但 read 是零 lease、零写入的 revalidation。
- render/approve/return 等明确 mutation 由 Application writer lease 所有；Markdown projection
  必须先以不可覆盖 revision 发布，再提交 SQLite relative pointer。
- P1-B 不把 Topic/Claim/brief 写回 Vault 内容、FTS 或 vector generation，不引入 daemon、网络、
  LLM/Embedding、Q2 resume/retry 或 release acceptance。

2026-10-25 W3 已实现下列 Outcome 修订，并通过 `014` 增至 schema `1.2.8`；
组件及整体验收证据以 W3 实施记录为准：

- 影响 render input 的 mutation、input_revision/input_digest 更新、已有 brief 失效同 SQLite
  事务提交；lease 只能互斥，不能代替该原子性。brief 记录 rendered_input_revision/digest。
- 任何 revision 均通过绑定 outcome/candidate revision/input digest 的 publication_id 发布，
  相同输入可验证收养相同字节 orphan，新输入使用不同 identity；pointer CAS 同时检查旧
  pointer 与当前输入。不得覆盖 orphan 或把后续版本恢复限制为第一次生成。
- 新 v2 approve/return 绑定用户审阅的 outcome_id、expected_revision、content_sha256、
  expected_input_revision，事务内重检后改变状态；只读输入或 projection 失配不保持
  approved。API-major-2 迁移与旧入口处理见 Kernel SDK 合同，不在当前 major 静默改签名。
- `014` 保留旧 brief 的持久 state、版本、pointer 与文件字节，并标记为 legacy_unverified；
  其有效状态为 needs_review，须显式重新生成与审阅，不伪造历史绑定或 receipt。迁移仅在
  授权的合成 fixture 中验证，未执行用户数据迁移。

当前兼容阶段旧无版本 review 仍以 topic 定位最新成果，弃用警告不会使其具备 exact review
保证；R-04 保持 known_gap。W5 才执行 major 2、旧入口明确拒绝与客户端握手迁移，不能以
W3 新入口通过验收替代该退出条件。Embedding v2 的恢复/预算修订继续属于 W4。

价格卡是 bundled、审阅的可选资源；无 card 时只记录 token，Provider 未报告的 usage 字段保持
`NULL`。任何 policy、model 或 card digest 变化都要求重新确认，绝不能复用旧 reservation。

## 后续边界

- R3.1 的 writer inventory 只约束产品 data-root persistent writers；测试 fixture、
  report、backup/restore 和已栅栏历史脚本不能被笼统称作“所有文件系统写入”。
- R4 staged core 已扩展 runtime snapshot 的 Embedding generation 契约、confirmed rebuild、
  retained previous generation 与 atomic pointer；当前工作树的公开 adapter 由 R4-P1 独立
  声明。其 v2 publish-only 继续、自动清理和 rollback 的状态分别按修复计划/后置项判断，
  不能由旧 core 测试反推已交付。
- CLI/MCP/Kernel 适配器必须调用或投影此 lifecycle，不得以 legacy bootstrap 偷偷完成
  inspect/plan 阶段的写入；CLI/MCP 的 status/setup/repair 适配行为须由各自模块合同和
  回归单独验证，不能反推 R3.1 或 R4 public-adapter gate 已完成。其具体命令/Tool envelope
  由各自公开合同冻结。
- 数据库 upgrade、journal repair 永远先展示影响和计划，后续专用 workflow 才能在确认后执行。
