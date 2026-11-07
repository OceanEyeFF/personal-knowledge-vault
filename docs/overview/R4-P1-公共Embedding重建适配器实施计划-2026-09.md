# R4-P1：公共 Embedding 重建适配器实施计划（2026-09）

> 状态：保留 **2026-09-04 的 source acceptance 记录**；2026-10-24 复审确认公共计划跨根
> 绑定、D 阶段争锁和预算暂停重入存在缺口。W2 已修复并定向验证 **R-01 的 v1 根/完整
> snapshot 确认绑定**，证据见第 10 节及
> [W2 修复记录](./W2-身份队列与CLI门禁修复记录-2026-10.md)。R-02、R-06 和 W4 的 v2
> 恢复/预算/迁移规则于 2026-10-26 **范围验收完成**，当前 SDK `0.10.0` / API `1.0.0`；
> 状态见 [W4 主记录](./W4-Embedding继续发布与预算恢复实施记录-2026-10.md)。不能由 R-01
> 或历史 source acceptance 通过推导恢复入口已验收。实施顺序见
> [生命周期一致性修复设计与分阶段计划](./生命周期一致性修复设计与分阶段计划-2026-10.md)。
>
> 前置基线：R1–R4 与 R3.1 已完成 source acceptance；R4 的 Q0 → Q1′ → Q2
> 仍是 Application 内部生命周期。本文只规划一个按需的、显式确认的 public rebuild
> adapter；不构成 Artifact、安装器、PyPI、真实 Provider、真实 Vault、真实迁移或
> release acceptance。

## 1. 目标与边界

### 目标

当一个已准备好的数据根因下列事实需要新的 Embedding generation 时，向 CLI、MCP 和
外部 Wrapper 提供同一条可审计、可确认、可恢复判定的公开路径：

```text
inspect (zero write / zero Provider)
  -> plan (zero write / opaque revision)
  -> explicit confirm (plan id + approved + allow_network)
  -> execute (fenced staging -> validate -> pointer CAS)
```

它只重建由已提交 Markdown + SQLite projection 派生的向量 generation；不会重新抓取、
重新分块、重新写入条目正文或修改 Q0/Q1′ 的内容事实。

### v1 明确交付

1. `pkv_kernel` 的窄、版本化、opaque rebuild lifecycle façade。
2. CLI 的 plan-first Embedding rebuild 命令。
3. MCP stdio 的只读 inspect/plan Tool 与显式 mutation execute Tool。
4. generation stage 的 lease/fence 安全边界、稳定错误/警告投影与审计。
5. 只使用合成数据和正式 OpenAI-compatible Provider 合同的隔离验证。

### v1 非目标

- 不公开 Q2 task 的 `resume`、`retry`、drain、后台 worker、daemon 或定时器。
- 不增加 public rollback、retained-generation 清理、flat-vector fallback 或手工目录操作。
- 不修复或绕过 `archive_url` 的 loopback SSRF 边界；它仍须 fail-closed。
- 不做真实 Vault、真实 Provider、真实 URL、真实迁移、GUI、Node、Docker、云端或 release
  工作。
- 不把本机 LM Studio 作为自动化依赖、测试 fake 或隐藏产品模式。它可在用户授权的后续手工
  OpenAI-compatible 兼容性验证中使用，但不计入 source acceptance。

## 2. 已确认的设计决定

### 2.1 只公开 rebuild，不公开内部自动任务恢复

R4 自动 Q2 的失败、预算暂停和 authorization-required 仍由内部 durable task ledger 表达。
只读 `show`、`search`、`get_entry`、`get_runtime_status`、MCP Resource 与 public rebuild
inspect 都不取得 writer lease、不创建 Provider、不 drain task。

public rebuild 是一项新的、前台、一次性确认的 generation maintenance action；它不把旧 Q2
任务伪装为已经恢复，也不改变 `archive` 的自动 drain 语义。

### 2.2 Kernel-first，CLI/MCP 只投影

公开实现顺序固定为：Core orchestration → `pkv_kernel` façade → CLI → MCP。CLI/MCP 不得
直接 import `src.runtime.embedding_lifecycle`、Store、Provider、lease 或路径对象；外部
Wrapper 仍只能依赖 `pkv_kernel`。

公开 handles 遵循既有 Kernel opaque-handle 模式，并位于独立的
`pkv_kernel.embedding` 模块：

- `EmbeddingRebuildInspection`：安全、不可变、可序列化的检查结果；
- `EmbeddingRebuildPlan`：process-local opaque handle；`to_dict()` 中的 public `plan_id` 只供
  跨 CLI/MCP 进程重新核验，不能反序列化成可执行计划；
- `EmbeddingRebuildConfirmation`：只由 façade 为同一 plan 创建；
- `EmbeddingRebuildExecution`：安全结果及 `audit_completion_pending` 警告投影。

façade 已放在 `pkv_kernel.embedding`，并通过新增 capability
`kernel.embedding-rebuild.v1` 握手声明。API-major-1 只增加能力，不改变既有 export 或语义。

### 2.3 public plan 可跨 CLI/MCP 进程重新计算，但不可伪造

执行命令/Tool 在当前 Config snapshot 上重新 inspect 与 plan，再精确比较用户提交的 public
`plan_id`。因此客户端不必持有服务端 plan cache，新进程也可执行仍未过期的同一计划。public
identity 的目标合同必须绑定以下安全、跨进程稳定的 material projection；v1 历史修复与 W4
v2 增量的实施边界见本节末：

- `data_root_identity_sha256`：当前 canonical data-root identity 的摘要；内容、配置相同的
  两个不同根必须产生不同 public plan id，不能靠当前进程重新生成 Core plan 补救原根绑定。
- `runtime_snapshot_sha256`：本次只读捕获的完整无密钥 runtime snapshot 原始字节摘要，
  与执行时的 snapshot CAS revision 对齐；不能只比较 active generation 的可见字段。
- index state、source digest/count、active/previous generation 与 manifest digest、Embedding
  contract、force、独立 policy fingerprint，以及版本化的 action/identity schema。
- 新网络计划还须绑定 `budget_check_period = {timezone, local_day, local_month}`；
  publish-only 计划则绑定原 operation identity、sealed-stage identity 与当前发布条件，
  不能据此建立新的网络预算。

数据根、源 projection、runtime snapshot、Provider endpoint/model/dimension 或 token policy
漂移都以 `runtime_plan_stale` fail-closed。CLI/MCP 的重新计算必须保留上述所有输入，
不允许通过重建当前根的 confirmation 接受另一个根的旧 public plan id。

Core 同时保留进程私有 Config-source HMAC，用于一次 execute 内 A/D fence 的外部编辑检测；它
绝不进入 public plan id，因为那会使每个新 CLI/MCP 进程都无法确认同一计划。API key rotation
本身不是 public plan identity 的输入，也不会公开其可验证指纹；实际执行仍只使用当前捕获的
Config，且必须再次显式确认网络 mutation。去掉进程私有 HMAC 时，必须单独保留根与
snapshot 的稳定摘要；不能删除一个混合 revision 后遗漏其承担的确认绑定。

Plan payload 不包含本机路径、endpoint 原文、密钥、正文、chunk 文本、audit payload 或可重建
私有 handle 的字段。

**W2 历史交付边界：**v1 public plan id 已在原材料中增加 `data_root_identity_sha256` 与
`runtime_snapshot_sha256`；后者复用本次已验证 inspection 捕获的完整原始字节摘要，不重新
读取 snapshot、不散列可编辑用户配置。原 v1 action、source/count、pointer/manifest、contract、
policy 与 force 继续参与确认。跨根、snapshot 扩展字段或原始字节变化均拒绝旧确认；同根
同材料可由新 CLI/MCP 进程重算。旧 public plan id 须重新 inspect/plan/confirm。
这次修复没有新增 v1 DTO 字段、API major 或 capability，也没有引入 v2 分域 identity、
`budget_check_period`、publish-only 身份或 ledger v3；这些在 W4 单独实施验收。W2 当时的 v1
durable replay key 保留原材料和编码，兼容已落盘 active operation 的围栏；W4 状态规则见第 4.1 节。
ER-01/02 与旧 replay 兼容性验证见第 10 节。

### 2.4 Provider 调用必须在 root writer lease 之外

这是 R4 的硬约束，而不是 adapter 层优化。public rebuild 的前台执行要分为：

```text
A. lease 内：re-inspect、plan/fence 复核、创建 durable rebuild operation、预算/审计意图、
             分配受限 private stage capability
B. lease 外：按捕获的 Config 创建 Provider，生成 document/chunk vectors
C. capability 内：只写本 operation 的 private staging leaves；不能写 Vault/SQLite/
                  snapshot/log/audit/其他任务目录
D. lease 内：重检 source/config/fence，strict readonly validate，reserve final generation，
             activation_intent，snapshot pointer CAS，terminal audit/ledger
```

当前实现已经拆分 lease-bound preflight/finalize 与 lease 外 Provider work；2026-10-24
修复仍须保持该边界。不得以“嵌套 no-op lease”、全局锁放宽或 test-only bypass 作为替代。

为同时保持 R3.1 的 writer inventory，阶段 B/C 使用最小化的
`GenerationStageGrant`：它由 A 在 lease 内创建，绑定 `operation_id`、owner fence、plan/source
revision、过期时间和单一 `vectors/staging/.stage-*` 目录；C 的每次写入都复核 grant。该能力
不授权任何 public 路径、generation publish、snapshot、audit 或其他 operation。进程中断后，
未引用 stage 只能由受控 recovery/expiry 清理，绝不被 reader 解析或自动成为 active generation。

### 2.5 A 与 D 的争锁必须分别处理（2026-10-24 修复目标）

A 尚未接受 operation、预留预算或调用 Provider。A 获取 lease 失败返回 `write_busy`，
不产生 operation、reservation、stage 或业务 audit；竞争解除后可重新 inspect/plan/confirm。

D 可能已经完成可计费工作，不能套用 A 的“零副作用”断言。目标是持久化 private-stage
publish-ready proof，并据此投影 effective `awaiting_publish`：保留原 operation、已完成且
封存的 stage、Provider usage 与原 reservation，
active pointer 不变；不得把已退出的执行永久报告为 `processing`，不得重新调用 Provider。

因为 D 争锁时不能写全局 ledger，C 必须在其私有 stage capability 范围内先持久化最小完成
凭据，再尝试 D。凭据至少绑定 operation id、原 plan/root/source/contract/snapshot 身份、
manifest 与四个 artifact 的摘要、原 grant identity/fence 及其有效期内的封存事实，以及已知
Provider usage/unknown 标记；不保存正文、密钥或原始 Provider 响应。封存后不再允许原
capability 改写 stage。可验证完成凭据与尚未完成的
pointer publication 共同证明 effective `awaiting_publish`；下一次获得 root lease 才可对齐
全局 ledger 和结算记录。只读检查可以投影该状态，不能借机对齐或写入。

继续发布须经过明确的 public mutation：提交**原 operation identity + 针对此操作的当前
publish plan 确认**，在 lease 内重检根、source、contract、policy、snapshot/pointer、封存凭据
和每个 artifact，再仅执行 validate/publish/CAS。operation id 单独不是授权；它不能切换根、
选择任意 stage 或构造 Provider。旧 stage grant 到期不授权续写；继续发布使用新的 lease/fence
验证封存产物，不延长旧写入 capability。已能证明在原 grant 有效期内合法封存的完整产物，
经当前 strict validation 后可纳入新的 publish grant；旧 grant 过期不能单独否决其只读采用，
否则等待窗口会再次成为永久阻塞。源或目标已漂移则终结旧发布操作或返回明确 repair 状态，
在可取得 lease 时按原事实/保守估算结算用量；不得悄悄生成新向量。

**W4 已实现完成凭据、`awaiting_publish` 对齐和公开继续入口，并完成范围验收。**按 W1 冻结合同第 6 节，
`kernel.embedding-rebuild.v2` 已在公开链路实现与组件验证后于 SDK `0.10.0` / API `1.0.0` 中
作为兼容增量声明，并保留 v1；W4 范围验收完成，它不以 W5 的 API-major-2 切换为前提。lookup、publish-plan、
confirm + execute 的准确签名、DTO 与错误投影已在 W1 的
[v2 接口冻结合同](../specs/interfaces/生命周期一致性v2接口冻结合同-2026-10.md) 固定；
ledger v3、grant v2 与 proof 见[迁移冻结方案](../specs/database/生命周期一致性迁移冻结方案-2026-10.md)。
当前验收状态以 W4 主记录为准，不能把普通 rebuild execute 重放称为 publish-only 恢复。
这项正常 D 争锁修复不开放 Q2 task resume，也不依赖第 8 节后置的 B/C 强杀回收。

## 3. 当前 v1 协议与 v2 冻结入口

### 3.1 Kernel façade

下列 v1 API 保留；W4 在同一模块增加独立 v2 名称与 DTO，具体形状以 W1 冻结合同为准：

```python
from pkv_kernel import embedding

inspection = embedding.inspect_embedding_rebuild(config)
plan = embedding.plan_embedding_rebuild(inspection, force=False)
confirmation = embedding.confirm_embedding_rebuild(
    plan, approved=True, allow_network=True
)
execution = embedding.execute_embedding_rebuild(plan, confirmation)
```

`force=False` 只允许 `rebuild_required` / 初始 generation 等 actionable state；在 `ready` 上
要求显式 `force=True`，并把该事实写入计划。`repair_required` 不生成可执行计划。

公开 DTO 至少包含以下可安全显示的字段：

| DTO | 允许字段 | 不允许字段 |
| --- | --- | --- |
| inspection | state、opaque revision、source counts/digest、active/previous generation id、contract provider/model/dimension、stable issues | 路径、endpoint 原文、credential、正文/chunk、SQLite/audit 原文 |
| plan | plan_id、force、source 工作量摘要、requires_confirmation、requires_network、稳定 actions/issues | 可执行 Provider/lease/store 对象、可反序列化 private state |
| execution | terminal status、operation id、generation id、previous generation id、`audit_completion_pending`、stable warnings/issues | stage/final directory、audit source body、Provider response/credential |

“operation id”只用于审计关联和受控状态投影，不是 public resume token。v1 不增加
`get_embedding_operation` 或 `resume_embedding_operation`。这是现有 v1 surface 的事实；
第 2.5 节的 manual `awaiting_publish` 继续入口是 W4 已实现并完成范围验收的显式 v2 增量，不能由这条
历史限制推导出“正常 D 争锁可以永久卡住”，也不能假称现有 v1 已具备该能力。

`kernel.embedding-rebuild.v2` 已按冻结 §6 的兼容增量规则实现并声明下列语义，外部调用者须先
进行版本/capability 握手；W4 范围验收完成，不能以 capability 声明替代最终结果：

- lookup：只读返回指定 manual operation 的安全 effective state、publication/usage 状态；
  不对齐 ledger，不调 Provider，不返回 stage 路径、正文或私有 capability。
- publish-plan：只读检查原 operation 与当前根/source/snapshot，明确 `publish-only`、
  零 Provider、无需新 token reservation，并生成绑定该 operation 的当前确认 identity。
- confirm + execute：显式确认该 publish-plan，持 lease 验证并发布既有 sealed stage。
  缺 operation identity、计划不匹配、已完成的操作或需要新 Provider 的场景不得隐式降级为重建。

### 3.2 CLI

新增一个独立的 `embedding` 命令组，所有机器可读输出为 path/secret-free JSON：

```text
pkv embedding inspect
pkv embedding rebuild [--force]
pkv embedding rebuild [--force] --apply --confirm PLAN_ID --allow-network
```

- 默认 `rebuild` 只输出 inspection + plan，不会创建目录、lock、Provider、stage、audit 或
  generation。
- `--apply` 必须同时有精确 `--confirm PLAN_ID`；`--allow-network` 不能单独成为网络授权。
- `--force` 仅是布尔值，不接受 truthy string；所有无效组合返回稳定
  `confirmation_required` / `runtime_plan_stale` / `embedding_rebuild_required` 等错误。
- 成功只在 pointer CAS 后返回 READY generation；CAS 后 audit completion 失败返回成功状态加
  `audit_completion_pending=true`，绝不伪装为回滚或重试许可。

### 3.3 MCP stdio

新增三个 Tool，继续仅支持 stdio：

| Tool | Hint | 行为 |
| --- | --- | --- |
| `inspect_embedding_rebuild` | `readOnlyHint=true` | 返回 inspection；零写、零 Provider、零 task drain。 |
| `plan_embedding_rebuild` | `readOnlyHint=true` | 接受严格 bool `force`，返回安全 plan。 |
| `execute_embedding_rebuild` | mutation | 接受 `plan_id`、严格 bool `approved`、严格 bool `allow_network`、`force`；重新计算计划后才执行。 |

MCP envelope 必须包含 `terminal` / `status`、可操作的 stable `issues` 与 public operation /
generation ids；不得以空 `results`、成功或内部异常文本替代 degraded/error。所有 Tool 通过
同一 Kernel/Application port，不增加 MCP 的 Store 或 Provider composition。

2026-09 的 MCP manifest 从 15 Tool 变为 18 Tool 是当时的 public-contract 变更。W3 增量后的
历史总数为 35；W4 再增加 6 个，当前为 41（23 只读 + 18 写入）。Tool discovery、MCP quality
taskset/score、coverage gate 和文档必须同步，不能只改实现或复用历史计数。

### 3.4 W4 独立 v2 表面

SDK 保留全部 v1，增加八个函数：`inspect / plan / confirm / execute_embedding_rebuild_v2`、
`inspect_embedding_operation_v2` 以及 `plan / confirm / execute_embedding_publication_v2`。
返回七个 facade-only opaque handle（publication execute 复用 rebuild execution 类型），禁止
直接构造、copy、pickle 或由展示 DTO 恢复执行能力。完整类型名称见
[SDK 公开合同](../specs/interfaces/KernelSDK公开合同.md)。

CLI 增加 `embedding inspect-v2`、`rebuild-v2 [--force] [--apply --confirm ID --allow-network]`、
`operation-v2 OPERATION_ID`、`publish-v2 OPERATION_ID [--apply --confirm ID]`。publish 不接收
force/network；默认只读计划，写入必须明确确认。MCP 增加 inspect rebuild、plan rebuild、inspect
operation、plan publication 四个只读 v2 Tool，以及 execute rebuild/publication 两个写入 v2 Tool。
新入口使用 `pkv.lifecycle-response.v2`，operation 固定为对应 MCP 名，不接收客户端路径、Config
或私有 handle。v1 pending code/details 保留旧形状，lookup/publish 仅在 v2 提供。

## 4. 费用、授权与状态规则

public rebuild 是一次性、显式确认的网络 mutation，plan 必须显示来源规模和本地 token estimate
（若现有 estimator 可安全复用）；没有已确认 price card 时仍只显示 token，不估算金额。

既有独立手动策略和本轮修复须共同满足以下 contract：

1. per-plan `approved + allow_network` 是本次手动 rebuild 的授权，但仍须创建
   durable reservation/usage ledger，沿用 token hard cap；它不自动启用或修改 `ai.automation`。
2. Provider/model/endpoint/dimension、token policy、source projection 或 runtime pointer 的变化都会
   使 public plan stale；一次 execute 内仍用私有 Config-source revision 防止中途编辑。API key
   不进入 public identity，也不在 DTO/audit/ledger 中暴露可验证指纹。
3. `budget_paused` / authorization-required 不创建 Provider；返回可操作状态，不自动转入 Q2
   retry。预算拒绝不消费执行资格；日/月额度恢复后，用户新的显式提交必须重新检查当前额度，
   不能被无 Provider 的历史拒绝永久挡在 replay fence 前。
4. Provider 返回 usage 时按阶段记录；未报告字段保持 unknown/`NULL`，不是零。

W4 按已冻结的第 4.1 节与迁移合同实施 ledger 3、replay/状态规则和 grant 2/proof；完整验收
单列在 W4 主记录，不得绕过费用控制或复用 Q2 的验收结论。legacy 1/2 只读分类后在明确
确认的 v2 mutation 中升级；v1 不隐式迁移。legacy schema 1 缺 public_plan_id，公开
original_plan_id 保持 null，不以 Core 私有 plan_id 代填。

### 4.1 phase / 状态 × replay × reservation（W4 已完成范围验收的冻结规范）

material transition identity 用于并发去重，operation identity 用于一次已接受执行的事实关联；
二者不能混用为“见过此 source 即永不再执行”。`force` 不得绕过同一 transition 的并发围栏。
用于确认的 plan identity 与用于并发去重的 replay key 必须分开：后者排除 `force` 和
`budget_check_period`，避免午夜/月界使仍在 processing/awaiting_publish 的同一 transition
获得第二次 Provider 调用资格；预算拒绝能否重新申请由 operation 状态和当前额度共同判定。

| 阶段 / 状态 | replay / 显式重入规则 | reservation / usage 规则 |
| --- | --- | --- |
| A 未取得 lease | 不接受 operation；返回 `write_busy` 后可重新确认提交 | 不创建 reservation 或 usage |
| A `budget_paused` / authorization-required | 无 Provider 拒绝不消耗执行资格；新的显式提交重检授权和日/月额度 | 无 reservation；本地 estimate 是预估而非消费 |
| A 已预留、确认未开始 Provider 后失败 | 失败是终态，原 operation 不冒充执行中；新的显式确认可建立新 attempt | release 原 reservation；不记 Provider 消费 |
| B/C `processing` | 同一 transition 的并发执行拒绝；已开始或是否开始不确定时不得自动重调 Provider | 保留原 reservation，usage 未知仍为 unknown；强杀边界见第 8 节 |
| C 已封存 / D `awaiting_publish` | 原 operation + 当前 publish plan 确认后只继续发布；零 Provider | 复用原 reservation/usage，不再次预留或重复结算；全局更新须在 lease 内 |
| D source/config/pointer drift | 拒绝发布旧 stage；返回明确 stale/repair 状态。新网络重建须有新的显式计划/确认 | 原已发生用量保留、保守结算，不能因未发布而退成零消费 |
| D `activation_intent` / CAS 结果待核验 | 先在 lease 内核对 pointer 与该 operation 的 generation，再决定完成发布或对齐终态；不调 Provider | 不重复预留或结算；未核验不得报告成功或自动重建 |
| `completed` / CAS 后 audit 或 ledger 待对齐 | 原执行资格已消费；只能对齐完成事实，不能重跑原网络工作 | usage 结算幂等，保留 completion-pending 提示 |

预算期间按已确认 policy timezone 计算 local day/month。每个**新网络 attempt**在 A 内重检
当期已结算 + 已预留 + 本次预估，同时遵守日/月上限；跨日不能绕过仍有效的月上限。历史
`budget_paused` 不能单独令新提交 stale；旧日/月 reservation、已发生 Provider usage 不因
周期变化而被删掉或重复计入。已封存的 `awaiting_publish` 没有新的网络 attempt，跨日继续
发布仍沿用原 reservation，不在新周期重新收取同一工作的额度。

新计划的授权信息须显示其预算检查期间；预算窗口或 policy 变化后须重新 inspect/plan/confirm，
执行时再次检查。上述期间信息不得改变“已完成或仍在执行的同一操作不能重复收费”的规则。
现有 ledger 记录若不能证明 Provider 尚未开始或 stage 已完整封存，必须明确给出待修复状态，
不能自动释放预算、伪造完成凭据或把它当作新的未执行 attempt。

## 5. 分阶段实施计划

以下 P1.0–P1.4 保留原交付顺序和历史验收门槛。2026-10-24 的修复阶段、依赖与退出条件
以[生命周期一致性修复设计与分阶段计划](./生命周期一致性修复设计与分阶段计划-2026-10.md)
为准；不得用第 9 节旧结果替代第 6 节新增 ER 用例。

### P1.0 — 合同与现状验证（先行）

- 冻结 Kernel capability、DTO schema、CLI JSON、MCP envelope 和 stable error mapping。
- 以测试证明当前 Provider 调用与 root lease 的实际边界；若不满足第 2.4 节，先完成下阶段
  refactor，不暴露任何 adapter。
- 明确 manual rebuild 的 reservation/usage 账本归属及 audit retention/redaction。

**退出条件：**设计文档、API diff、ledger decision 和 fault matrix 经 review；没有新增 public
命令/Tool。

### P1.1 — Core operation 与 stage capability

- 引入 fenced `GenerationStageGrant` 与最小 durable operation record；更新 writer inventory。
- 将 rebuild orchestration 拆为 lease-bound preflight/finalize 与 Provider-outside-lease build。
- 保留 `PreChunkedEmbeddingAdapter`；只能对已存 chunks 调用 Provider，绝不重新分块。
- A 的 `write_busy` 在 Provider、audit、stage 之前返回；D 争锁须满足第 2.5 节的持久
  publish-ready proof、effective `awaiting_publish` 与受确认继续合同；共享 ledger 只在
  取得 lease 后对齐。所有 pre-CAS 失败不改变 active pointer。

**退出条件：**Core unit/fault tests 覆盖 lease、fence expiry、source/config drift、Provider failure、
staging crash、generation collision、pre/post-CAS audit 与无 flat fallback。

### P1.2 — Kernel public façade

- 将安全 DTO/opaque handles 纳入 `pkv_kernel.embedding` 与 capability handshake。
- façade 不接受 Provider、lease、path、Store、fake 或 test-mode 参数。
- 只在 execute 成功后允许使用同一 confirmed Config 打开/刷新 Kernel；不重绑 in-flight graph。

**退出条件：**API-major compatibility AST/export tests、wheel source-free lifecycle smoke 与
zero-write inspection/plan regressions 通过。

### P1.3 — CLI 与 MCP adapters

- CLI 实现 plan-first command 与精确确认比较；所有错误输出为稳定 JSON。
- MCP 仅在 stdio 上注册三 Tool，完整更新 Tool discovery 与 public schemas。
- CLI/MCP 都不读取 `local.yaml` 作为用户输入、不修改用户配置、不创建 Provider 于 inspect/plan，
  不直接调用 Core private lifecycle/store。

**退出条件：**CLI subprocess、MCP JSON-RPC stdio 公开协议和 path/credential-redaction 回归通过。

### P1.4 — Source acceptance 与独立 review

- 新增 public adapter 的定向、blackbox、MCP coverage、P0 与 Phase C 回归。
- 使用独立 reviewer 检查跨进程边界、公开入口、lease、fake/test-mode、网络隔离、固定端口、
  sleep、global Config 污染和 release 表述。

**退出条件：**所有命令通过 `scripts/run-test.ps1`，且 reviewer P0/P1 为零；记录只称为
source acceptance。

## 6. 验收矩阵

| 场景 | 入口 | 关键断言 |
| --- | --- | --- |
| inspect / plan 零副作用 | Kernel、真实 CLI 子进程、MCP stdio | 不创建 DataRoot/Provider/stage/audit；无 source/path/credential 泄露。 |
| 确认门禁 | CLI、MCP、Kernel | 缺 `approved`、`allow_network` 或 plan id 不匹配时零 Provider、零 generation 变更。 |
| A stale / write busy | Core + 公共入口 | admission 前的 drift 或竞争在 Provider 前失败；无 operation/reservation/audit/stage/pointer 副作用。 |
| D write busy | Core + W4 公共继续入口 | 持久 publish-ready proof 支撑 effective `awaiting_publish`，争锁期间不写共享 ledger；取得 lease 后对齐并显式继续发布，不重复 Provider/预算。 |
| Provider lease 边界 | 外置 deterministic harness barrier | Provider 活跃期间，另一 writer 可取得/释放 root lease；stage 仅能通过有效 grant 写自己的叶子。 |
| Provider/stage 失败 | Core + public envelope | active pointer 不变，reader 不退回 flat index，错误可操作且不泄密。 |
| 成功重建 | CLI 与 MCP 真进程 | 新 READY generation 绑定、previous retained、usage/reservation/audit 事实正确；新进程 vector/hybrid 命中。 |
| audit post-CAS | fault injection | 返回成功 + `audit_completion_pending`，不重复执行旧 plan。 |
| 安全回归 | CLI/MCP blackbox | 无真实网络/凭据/Vault；无固定端口、sleep 或全局 Config 污染；MCP 仍 stdio-only。 |

以下是 2026-10-24 复审建立的修复验收门槛。W2 已取得 ER-01/02 的第 10 节证据；
其余行是本次 W4 的验收要求，不能因同一表内部分用例通过而视为完成。R-02/R-06 与
W4 的公开继续协议、预算恢复和迁移当前范围验收完成；最终逐项结果、节点与指纹见 W4 主记录。

| Case ID | 场景与边界 | 必须断言 |
| --- | --- | --- |
| ER-01 | 不同根、相同 Config/source/pointer 内容；真实 CLI 与 MCP | 两根 public plan id 不同；A 根确认交给 B 根时，在 Provider/业务写入前 stale。W2 已通过，节点与零副作用 oracle 见第 10 节。 |
| ER-02 | 同根跨进程；snapshot 中非 active-pointer 字段变化 | 无变化时新进程可重算；完整 snapshot revision 变化后旧确认 stale，不被重新构造 Core confirmation 绕过。W2 已通过；同根新进程为 CLI/MCP 黑盒证据，完整 snapshot 漂移为单元证据，见第 10 节。 |
| ER-03 | A 已有另一个 writer | `write_busy`，零 operation/reservation/Provider/stage/audit；释放竞争后新确认可以执行。 |
| ER-04 | 外置 Provider barrier 完成 B/C 后，另一 writer 覆盖 D 取锁窗口 | durable publish-ready proof + effective `awaiting_publish`、pointer 未变、usage 保留；争锁期间共享 ledger 不写，取得 lease 后才 reconcile。竞争解除后经公开入口提交原 operation + 当前确认，Provider 调用次数不再增加，最终 READY。 |
| ER-05 | ER-04 后关闭宿主，在新 CLI/MCP 进程继续 | 由封存凭据重建待发布事实；不凭 PID/内存缓存判断，零 Provider、零重复预留/结算；stage grant 到期不授权续写。 |
| ER-06 | awaiting_publish 的根/source/contract/pointer/凭据漂移或 operation 不匹配 | 在发布前拒绝，pointer 不变、已付费用量保留，不自动创建新 Provider。 |
| ER-07 | 日额度耗尽被拒，推进至下一 local day；另覆盖月额度耗尽后下一 local month | 无 Provider 的预算拒绝不永久消耗 replay；当前额度允许且重新确认后可申请。同月仍超月 cap 时继续拒绝。 |
| ER-08 | 已封存操作跨日/月继续发布；另一个同 transition 并发执行 | 沿用原 reservation/usage、不重复计费；并发执行仍被围栏拒绝，`force` 不能绕过。 |
| ER-09 | Provider 明确未开始的失败与开始状态不确定的旧记录 | 前者 release 后可重新确认新的 attempt；后者 fail-closed 为明确 repair，不擅自重调 Provider 或清空额度。 |
| ER-10 | activation_intent / CAS 前后中断与 settlement 重入 | pointer 是最终成功判据；只对齐同 operation，audit/ledger pending 不诱发重复调用或重复结算。 |

ER-04/05/07/08 必须在 Core 的确定性阶段/时钟 seam 与真实 CLI/MCP 公共协议两层形成
相互独立证据。公开场景的 phase barrier、预算时钟推进方案须先在 payload 外测试基础设施
中冻结；不得加入产品 test mode、隐藏参数、真实等待一天/月、固定端口或 sleep 同步。
基础设施或公开继续入口未交付时记录为 pending，不能用直接调用 ledger 代替黑盒验收。

自动化只使用独立 `.data-test/<scenario>`、随机 numeric-loopback 的进程外 deterministic
OpenAI-compatible harness 或正式 test doubles。LM Studio、真实 Provider、真实 URL、真实
用户配置和真实 Vault 一律不在这些用例中读取或启动。

建议验证序列：新定向测试连续两次 → 既有 CLI/MCP blackbox → R4 unit/integration →
`test-conda.ps1 -Suite P0` → `test-conda.ps1 -Suite MCP` → 经 `run-test.ps1 -Direct` 的
`python -m evals.mcp_quality --enforce-thresholds` → `git diff --check` 与致命 Ruff 检查。

## 7. 进入 B 主线的条件

只有在本计划的 P1.4 收口后，才开始 B 的 `Topic / Claim / Evidence / Outcome` 最小合同。
届时 B 可依赖已明确的 READY/degraded generation 投影和 public maintenance boundary，但仍不得
把 public rebuild、自动 task resume 或 release compliance 混为一项完成结论。

## 8. 仍保留的后置项

| 项目 | 不在 R4-P1 v1 的原因 |
| --- | --- |
| Q2 task public resume / retry | 需要独立 durable operation/status/authorization 产品设计，不能由 rebuild 代替。 |
| public rollback / generation cleanup | 需要 retention、可见影响、确认与恢复合同。 |
| Provider 中强杀后的即时 reclaim / recovery | 限定为 B/C 尚无可验证完整 stage 凭据、Provider 完成事实不确定的强杀。fail-closed 保留未知外部影响与 reservation，占用 replay 围栏和预算，不自动清理或重试；后续需 claim-time / provider-start、expiry/reconciliation 与 cleanup 合同。**不包含**进程正常存活时的 D 普通争锁，或已完整封存的 awaiting_publish 跨进程继续；后两者已完成 W4 范围验收。 |
| `archive_url` 成功离线黑盒 | 当前没有同时满足 SSRF fail-closed 与公开 fetch harness 的合同。 |
| release / Artifact | 当前 3 项 compliance blocker 与 held candidate 状态完全独立。 |

## 9. Source acceptance 记录（2026-09-04）

本节保留当时记录的运行结果，不是 2026-10-24 修复完成证据。该历史矩阵未覆盖 ER-01–10
新增的组合故障；“P0/P1 为零”只描述当次 review，不能覆盖本轮已复现缺陷。2026-10-24
复审时本文件尚未由 Git 跟踪，不能仅凭文中日期证明这些文字在实现前已经提交。

- 新增的真实 CLI 子进程与 MCP stdio 黑盒连续独立运行两次，均为 `5 passed`；既有
  CLI/MCP/R4 blackbox 聚合为 `68 passed`，R4 定向 unit/integration 为 `116 passed`。
- 最终隔离 P0（`scripts/test-conda.ps1 -EnvironmentName py311-private -Suite P0`）为
  `3324 passed, 21 skipped, 219 deselected, 35 warnings`（exit 0）；MCP suite 为
  `803 passed, 5 deselected`、`src.mcp 95.05%`。Phase C 经规定的
  `run-test.ps1` 隔离入口执行，`16/16`、`overall=1.0`。
- `git diff --check` 与致命 Ruff 静态检查通过。独立 review 的最终 P0/P1 为零；审查确认
  真实进程/公开入口、外置 deterministic Provider harness、无产品 test mode、无固定端口或
  sleep 同步。
- 这只证明 source blackbox acceptance。第 8 节所列 Provider-in-flight 强杀恢复与
  `archive_url` 成功离线 fetch harness 仍是后置基础设施项；held Artifact/release 的
  compliance hold 不变。

## 10. W2 R-01 身份修复与验收证据

本节仅关闭 R-01 的公共确认根/snapshot 绑定缺口。实现范围是当前 v1 façade 的 identity
材料补齐，保留原 durable replay 算法；未实现 R-02 的 D pending/proof/publish-only、R-06
的预算重入或 W4 的 v2 接口/分域身份/ledger v3。完整 W2 范围与其他修复证据见
[W2 身份、队列与 CLI 门禁修复记录](./W2-身份队列与CLI门禁修复记录-2026-10.md)。

| 验收绑定 | 实际 pytest nodeid | oracle / 已验证边界 |
| --- | --- | --- |
| ER-01，W1 红测试转绿 | `tests/unit/test_w1_embedding_regressions.py::test_r01_er01_public_plan_confirmation_identity_is_bound_to_data_root` | 同内容不同根的 public confirmation identity 不同。 |
| ER-02，同根控制 | `tests/unit/test_w1_embedding_regressions.py::test_r01_er02_unchanged_same_root_reinspection_keeps_public_plan_identity` | 同根同材料重新检查保持 public plan id。 |
| ER-01/02，真实 CLI | `tests/blackbox/test_embedding_rebuild_cli_blackbox.py::test_public_cli_rebuild_confirmation_is_root_bound_and_process_stable` | B 根拒绝 A 根确认，零 Provider、空 ledger、无 stage/generation、snapshot 不变；A 根新进程重算并成功执行原确认。 |
| ER-01/02，真实 MCP stdio | `tests/blackbox/test_embedding_rebuild_mcp_blackbox.py::test_mcp_rebuild_confirmation_is_root_bound_across_fresh_servers` | 不同 server/root 的确认不能互用；同根新 server 保持身份并可成功执行。B 的零副作用断言与 CLI 相同。 |
| ER-02，完整 snapshot | `tests/unit/test_w2_embedding_identity.py::test_er02_full_snapshot_change_invalidates_public_confirmation[mapping_extension]`；`tests/unit/test_w2_embedding_identity.py::test_er02_full_snapshot_change_invalidates_public_confirmation[raw_bytes]` | 非 active-pointer 扩展字段及仅原始字节变化均使新 plan id 改变；旧 captured inspection 不被重新读取覆盖，旧执行确认在 Provider 前 stale。 |
| 公共身份保密 | `tests/unit/test_w2_embedding_identity.py::test_key_rotation_does_not_enter_public_identity_or_disclose_private_material`；`tests/unit/test_w2_embedding_identity.py::test_secret_bearing_runtime_snapshot_cannot_be_hashed_into_public_plan` | 合成 key rotation 不改变公共身份，不输出私有 HMAC/密钥/路径；含秘密字段的 snapshot 验证失败，不成为可确认身份。 |
| v1 durable replay 兼容 | `tests/unit/test_w2_embedding_identity.py::test_strengthened_confirmation_preserves_existing_v1_active_replay_fence` | 预先写入旧算法 replay key 的 active operation 继续拦截新确认；不调用 Provider，不建新 stage，不改变 ledger。 |

CLI/MCP 黑盒各使用隔离双根和进程外 deterministic Provider harness；拒绝 B 后在 A 成功
执行，整个 harness 精确收到 document/chunk 两次请求。它同时证明被拒根没有隐藏 Provider
调用和同根新进程仍能执行；不是完整 snapshot 漂移的公开协议测试，后者的本次证据仅在
单元层。

实际运行（目录名保留运行时标识，均通过规定的隔离入口）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run-test.ps1 -Direct -DataRoot .data-test/w2-identity-unit-20261024 pytest tests/unit/test_w2_embedding_identity.py tests/unit/test_kernel_embedding_rebuild.py tests/unit/test_embedding_lifecycle.py tests/unit/test_embedding_rebuild_ledger.py tests/unit/test_kernel_wrapper_boundary.py tests/unit/test_w1_embedding_regressions.py -q -k "not r02 and not r06"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run-test.ps1 -Direct -DataRoot .data-test/w2-identity-blackbox-20261024 pytest tests/blackbox/test_embedding_rebuild_cli_blackbox.py tests/blackbox/test_embedding_rebuild_mcp_blackbox.py -q
```

- `j-40q7zd`：`49 passed, 7 deselected`，exit 0，165.34 秒；日志
  `C:/Users/fdch0/.fastctx/jobs/j-40q7zd/output.log`。该命令明确选择 W2 范围、排除 R-02/R-06，
  不是全量 W1 通过证据。
- `j-92sed5`：`7 passed`，exit 0，235.51 秒；日志
  `C:/Users/fdch0/.fastctx/jobs/j-92sed5/output.log`，含既有 5 个与新增 2 个黑盒节点。

以上不替代第 9 节历史记录，也不声明 W4、全部 ER 用例、发布或 Artifact 验收完成。
