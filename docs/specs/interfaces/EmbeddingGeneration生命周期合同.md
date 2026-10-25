# Embedding Generation 生命周期合同（R4 staged core）

状态：runtime-internal staged core 与内部 R4 Q0→Q1′→Q2 生命周期已实现并以隔离、
deterministic lifecycle 回归及真实 CLI/MCP 跨进程闭环验证。后续 R4-P1 已增加 public rebuild
v1 adapter；本文件中的内部 Core 函数仍不直接成为 Wrapper API，历史平铺 `vectors/` 目录
仍不是回退路径。2026-10-24 复审确认公共计划跨根绑定、D 争锁和预算重入缺陷；其中
R-01 已由 2026-10-25 W2 补齐根身份及完整已捕获 snapshot 的公共确认绑定。
R-02 的 D 争锁恢复和 R-06 的预算重入仍为 known_gap；下文标为修复目标的恢复状态/v2 协议
尚未实现，不把历史回归提升为修复完成证据。统一实施顺序见
[生命周期一致性修复设计与分阶段计划](../../overview/生命周期一致性修复设计与分阶段计划-2026-10.md)。
W1 已将签名/错误冻结到 [v2 接口合同](./生命周期一致性v2接口冻结合同-2026-10.md)，
将 ledger/grant/proof 版本及 canonical 材料冻结到
[迁移方案](../database/生命周期一致性迁移冻结方案-2026-10.md)；均待后续实现。
W2 当前改动与独立运行证据见
[身份、队列与 CLI 门禁修复记录](../../overview/W2-身份队列与CLI门禁修复记录-2026-10.md)。

## 边界与前置条件

R4 只在已就绪的 R2 runtime snapshot 上工作。`<data-root>/config/local.yaml` 必须
同时满足 R2 v1 的完整无密钥 base（`schema_version`、当前数据库 schema、Embedding
provider/fingerprint）以及 R2 的当前数据库/显式 Config 比较。缺失、结构无效、未知
标量 extension、数据库或 Embedding 漂移都不能发布 generation；调用方必须先完成
R2 inspect/plan/confirm/execute。

R4 的唯一 extension 是无密钥的 `embedding_index`：

```yaml
embedding_index:
  schema_version: 2
  data_root_identity_sha256: "<sha256>"
  state: ready
  source_digest: "<sha256>"
  active_generation: g-...
  previous_generation: g-... # 可为空
  retained_generations: [g-..., g-...]
  active_manifest_sha256: "<sha256>"
  contract:
    schema_version: 1
    provider: openai_compatible
    endpoint_sha256: "<sha256>"
    model: text-embedding-3-small
    dimension: 1536
    document_pipeline_version: 1
    stored_chunk_schema_version: 1
```

它通过 `RuntimeSnapshotStore` 的 read → semantic merge → raw-byte CAS publish 写入。
R2 lifecycle 和兼容的 `Config.write_runtime_config_snapshot()` 也使用同一 primitive，
因此任何一方都不得覆盖另一方的无密钥 extension。CAS 冲突为
`runtime_plan_stale`，不是“最后写者获胜”。

## 只读与 reader 合同

`inspect_embedding_index(config)` 和 `plan_embedding_rebuild(...)` 不得创建目录、锁、
数据库、VectorStore、Provider、staging 或 audit 文件；它们只接受显式 Config，绝不读
global `get_config()`。

来源是一个固定的 readonly SQLite + Markdown projection：SQLite `knowledge_id`、正文和
已存储 `(chunk_index, chunk_text)` 与 Markdown 正文逐一一致；孤儿、不连续/空、或
`VectorStore.MAX_CHUNK_INDEX` 之外的 chunk 都是 `repair_required`，且不会调用
Provider。重建永远使用已存 chunks，不能按当前 chunk 参数重新切分。

schema v2 把 binding 状态和 ready pointer 置于同一无密钥 extension。`ready` 必须同时有
完整 pointer/manifest；`rebuild_required`、`processing`、`retry_required`、`budget_paused` 与
`authorization_required` 不可解析为 vector binding，即使它仍保留一代历史 generation。reader
必须把它们投影为相应稳定状态，绝不能使用旧 generation、平铺目录或 `no_hits`。schema v1 的
历史 ready pointer 仍可严格读取；新 generation publish 写入 schema v2 的 `ready` binding。

`resolve_embedding_index_binding(config)` 只在 `ready` 时返回一次性的
`generation_id`、`index_dir`、pointer revision 和 contract。一个逻辑读操作必须只解析
一次 binding；后续 pointer flip 只影响后续操作。检查会用
`VectorStore.open_readonly()` 验证实际 HNSW pair、transaction marker、metadata schema 和
fingerprint，且不产生 sidecar。binding 永远指向
`vectors/generations/<generation-id>`，不存在 flat-index fallback。

启用内部自动化的当前 Application 读路径会在每个操作开始时解析此 binding，并以
`VectorStore.open_readonly(binding.index_dir, ...)` 打开；它不会重新指向 writer store 或
`<data-root>/vectors`。轻量 legacy test graph 的注入 seam 不构成产品 flat-index 回退。

## 自动化授权与 token 用量（R4 P0）

自动 AI lifecycle 的 policy 只从显式 Config 的 `ai.automation` 解析：默认关闭；启用时必须
有一次性、当前 Provider/model/token quota policy hash 确认，以及至少一个日/月 token hard cap。
policy inspect 不创建数据根、Provider、网络、日志或 snapshot。可选的 price-card reference
不是启用前提；没有 card 时只记录 token，绝不估算金额。

所有后续 usage ledger 至少区分（Provider 适用且已报告时）
`uncached_input_tokens`、`cached_input_tokens`、`generated_tokens` 与
`embedding_input_tokens`。Provider 未报告的字段保存 unknown，而不是零。任务 claim、token
reservation、usage settlement 与 binding state publish 都是 lease-protected owner 写入。

### 内部 Q0→Q1′→Q2 自动生命周期

带真实 production `Config` 的既有 archive 输入均遵循以下私有链路；它不新增 CLI/MCP Tool，
也不允许外部调用者直接调度 Provider。自动化 policy 只决定 Q2 的可计费 Provider stages：
未启用或未确认时 Q0/Q1′/handoff 仍完成，Q2 投影 disabled/authorization 状态且零 Provider。

```text
Q0 ingress admission + private spool
  -> fenced crawler/parser produces PreparedDocument
  -> Q1′ sole content writer + durable handoff/non-ready binding
  -> Q2 source/policy/budget fence + reservation
  -> private DerivationPatch
  -> Q1′ apply_ai_patch
  -> confirmed generation stage/validation/pointer CAS
```

- Q0 在 crawler、文件解析或 Provider 之前持久化最小 request identity/hash；慢速前处理在
  根 writer lease 外执行，但完成/失败只接受当前 claim token 与 fence。URL、文本和已授权
  文件均交付同一版本化 `PreparedDocument`；正文只在私有 spool，不进入 task ledger、日志或
  runtime snapshot。
- Q1′ 是 Markdown + SQLite 内容和 patch 的唯一提交者，绝不导入 crawler/processor 或创建
  Provider。其 core proof、handoff、non-ready binding 和 Q2 activation 可在重启后幂等续接。
- Q2 在 Provider construction 前重新验证 source、target revision、policy、retry/not-before 和
  token/可选金额 reservation。过期或 superseded claim 不能发布 patch、binding 或 generation；
  已经启动的 Provider reservation 只可按原 token/fence 保守结算。
- 摘要/标签先写为不可变 `DerivationPatch`，再由 Q1′ 原子应用。记录的 patch 在 Q1′ 失败后
  会复用而不重复调用 LLM；所有要求的 patch 成功后才会重新捕获 source 并执行 generation。
- 没有已确认 price card 时只进行 token 控制和记录，不展示或写入推测金额；Provider usage 的
  缺失维度保持 `NULL`，本地估算、reservation 与 Provider facts 以不同账本记录保存。

## Confirmed rebuild

```text
inspect (zero write)
  -> plan (contains opaque revision; requires confirmation + network)
  -> confirm(approved=True, allow_network=True)
  -> A: acquire root writer lease; re-inspect source/config/R2 base;
        reserve budget and allocate private stage grant
  -> B/C: release root lease; Provider work and private staging validation
  -> D: acquire root lease again; revalidate and reserve retained generation;
        audit activation_intent -> runtime snapshot pointer CAS
```

The two lease acquisitions have different failure contracts. A is admission before
Provider work. D follows work that may already be billable; it cannot claim the
zero-side-effect admission semantics. The target `awaiting_publish` contract below
adds a durable seal before attempting D; that recovery path is not implemented yet.

Plan and confirmation booleans must be actual booleans, not truthy strings. An
external edit to the loaded user `config.yaml` (including API-key rotation) changes
the opaque `Config.user_config_source_revision()` and rejects an already-approved
plan before lease/Provider/write as `runtime_plan_stale`. A key rotation by itself
does not change the stored Embedding contract; a provider/endpoint/model/dimension/
pipeline change is visible as `embedding_rebuild_required`.

W2 has corrected the existing v1 public cross-process confirmation identity to bind
`data_root_identity_sha256`, `runtime_snapshot_sha256` (the complete secret-free
snapshot raw-byte CAS revision captured by the same validated inspection), source digest/count, index state and active/previous
pointer/manifest, Embedding contract, policy fingerprint, force, and the versioned
v1 action. Root identity comes from that inspection's immutable Config; planning does
not re-read a newer snapshot to rewrite the captured confirmation. Two different roots
with identical content/configuration cannot share a confirmation, and any complete
snapshot byte change invalidates the old confirmation. The process-private user-config HMAC remains an
in-process execution fence; removing it from the public identity must not remove
the stable root or snapshot bindings. A key rotation alone is excluded from the
cross-process public identity, but an external edit during an in-flight execution
still invalidates that execution's private config-source fence.

W2 retains the original v1 durable replay material and encoding. Strengthening public
confirmation must not bypass an existing processing operation by silently changing its
replay key. Each root has its own ledger; retaining a snapshot-independent v1 replay
key is conservative. The v2 domain-separated identity and ledger/grant upgrades remain
unimplemented.

Under the frozen v2 target, a new network plan also binds
`budget_check_period = {timezone, local_day, local_month}`.
A publish-only plan binds the original operation, sealed-stage
identity and current publication conditions instead; it cannot authorize new
Provider work or a new token reservation. These budget/recovery rules remain W4 work,
not capabilities delivered by the W2 confirmation fix.

The generation is assembled under `vectors/staging/.stage-*`, its four index
artifacts plus manifest are verified, then materialized under
`vectors/generations/g-*`. The target directory is reserved with no-clobber
creation; a crash or stage failure may leave an unreferenced diagnostic stage, but
cannot alter the active pointer. The pointer is changed only after validation, and
the previous generation remains in `retained_generations`; automatic deletion and a
public rollback action are deliberately not part of this phase.

`PreChunkedEmbeddingAdapter` is the explicit bridge for the existing
`src.ai.embedder.Embedder`: it calls `embed_document(text)` and the underlying
`client.embed_batch_numpy(stored_chunks)`, never historical `embed_chunks(text)`.
内部 Q2 owner 只能在完成 source/policy/budget fence 后创建该 embedder，且必须绑定已捕获的
显式 Config；公开 adapter 仍不得绕过 inspect → plan → confirm 边界自行构造 Provider。

## Failure and audit semantics

Before pointer CAS, invalid source, Provider protocol/stage failure, config/source/
snapshot drift, or `write_busy` leave the active pointer and existing generation
bytes unchanged. A competing writer rejected at A has no operation, reservation,
business audit, stage, or Provider side effect. A D-stage contention must instead
preserve a durable publish-ready proof and project effective `awaiting_publish`
under the repair contract below; the shared ledger is reconciled only after a
root lease is acquired, never while D is contended;
it must not strand an exited request in `processing` or imply Provider work never
happened. The current implementation does not yet satisfy this D-stage contract.

After lease acquisition R4 records local AuditTrace events with complete article and
stored-chunk provenance, while redacting configured keys, credential fields, inline
credentials and credential-bearing URLs. Audit context contains
`embedding_contract_sha256`, `config_source_revision` and
`data_root_identity_sha256`; no field calls a hash a “generation”.

An `activation_intent` record is written before the irreversible pointer CAS. If the
final audit completion append/fsync fails *after* CAS, execution returns success
with `audit_completion_pending: true`, not a false rollback-like error. The intent
record remains for reconciliation; public adapters must surface this warning and
must not blindly retry the stale plan. Pre-CAS audit failures still prevent
activation.

## Manual publication recovery and budget replay（2026-10-24 修复目标）

以下规则适用于独立的 manual rebuild operation，不改变内部 Q2 的 claim/resume 合同。

### Durable publish-ready proof and effective awaiting_publish

1. C 在已签发 private stage grant 的边界内封存完成凭据：原 operation/plan/root/source/
   contract/snapshot identity、manifest 与 artifact 摘要、grant identity/fence 与有效期内
   封存事实、已知 usage/unknown。它不得保存正文、密钥或原始 Provider 响应，也不得获得
   写全局 ledger、audit、snapshot 或其他目录的权限。封存后禁止原 capability 继续改写。
2. D 取锁失败时，全局 ledger 可能仍是 processing；持久 publish-ready proof 与尚未提交的
   pointer 共同支持 effective `awaiting_publish` 的推导。只读 lookup 可以展示该事实，不能写回。
   下一次取得 root lease 后才对齐 ledger/settlement，不能为修复状态而绕过 R3。
3. 继续操作必须绑定原 operation identity 和重新检查后的当前 publish plan，并取得明确
   确认。新 publish grant 只允许在 lease 下严格校验并发布原 sealed stage，零 Provider、
   零新 reservation。旧 stage grant 到期仅禁止续写；若可证明合法封存且当前 strict
   validation 通过，不得仅因其过期否决只读采用。operation id 或旧 confirmation 单独
   均不能授权继续发布。
4. root/source/contract/policy/snapshot 或目标 pointer 漂移、凭据/产物不可验证时，终结旧
   发布或明确要求 repair；不自动重建。已发生 usage/保守估算在可持 lease 时按原 operation
   结算，不能因尚未发布而释放成零消费。CAS 已提交但终态不完整时，以该 operation 的
   pointer/generation proof 对齐成功，不重复调用 Provider。

`awaiting_publish` 是 operation 状态，不是 reader 可解析的新 READY binding；读者始终
遵守现有 active pointer 与 generation validation。此恢复合同包括正常 D 争锁以及完整
sealed stage 跨宿主继续。B/C 尚无完整可信凭据、Provider 完成事实未知的强杀回收仍是
独立后置项，不得用于豁免 D 正常失败路径，也不得把不确定旧记录自动转换成 sealed stage。

### Replay and quota transitions

确认用 plan identity 与并发去重的 material-transition replay key 分离。replay key 排除
`force` 和 `budget_check_period`，防止跨午夜/月界绕过仍在 processing/awaiting_publish 的
同一 transition 围栏；允许预算拒绝后重新申请靠状态与额度判断，不靠给活跃操作换 key。

| 阶段 / 状态 | replay 与显式继续 | reservation / usage |
| --- | --- | --- |
| A 未取锁 | `write_busy` 后可重新计划确认 | 零 operation/reservation/Provider |
| A budget_paused / 授权拒绝 | 不消费执行资格；新的显式提交重检授权及当前日/月额度 | 无 reservation，无 Provider 消费 |
| Provider 明确未开始的已接受失败 | 原操作终结；新确认可建立新的 attempt | release 原 reservation |
| B/C processing 或开始事实未知 | 禁止重复/并发 Provider；未知旧记录保持明确 repair 边界 | 保留原预留和已知 usage，不伪造零 |
| C sealed / D awaiting_publish | 原 operation + 当前 publish plan 确认，复用 sealed stage | 原 reservation 只结算一次；不再次预留 |
| D stale/repair | 拒绝旧 stage 发布；新的网络工作须重新计划确认 | 保留并结算已发生用量 |
| activation_intent / CAS 待核验 | 只核验 pointer 并继续发布或对齐终态，不重调 Provider | 幂等 settlement，不重复预留 |
| completed / completion pending | 执行资格已消费；只能对齐完成事实 | 不重复计费，保留 pending 警告 |

预算按 policy timezone 的 local day/month 检查；新网络 attempt 在 A 内用当期已结算 +
已预留 + 本次预估同时检查日/月 cap。新计划须展示预算检查期间，窗口或 policy 变化后
重新 inspect/plan/confirm。`budget_paused` 没有调用 Provider，不能永久消耗 material
transition 的执行资格：日/月额度恢复后允许重新申请；日界变化不能绕过仍耗尽的月额度。
历史已发生 usage 不能因周期改变而删除，publish-only 重入沿用原 reservation/期间，不能
在新周期再次收取同一工作的额度。stage 完成后等待发布不等于新的网络 attempt。

## Public adapter and verification boundary

现有 `kernel.embedding-rebuild.v1` 提供 public inspect/plan/confirm/execute，但不提供上述
manual operation lookup/继续发布。修复目标随 API-major-2 与 `kernel.embedding-rebuild.v2`
一并交付：只读 lookup、安全 publish-plan，以及绑定原 operation + 当前确认的 publish-only
execute。具体签名、DTO 和错误码已在 W1 的
[v2 接口合同](./生命周期一致性v2接口冻结合同-2026-10.md)冻结、尚未实现；
不能把 v1 重放当作已存在的恢复入口。

公开适配器仍须使用 `PreChunkedEmbeddingAdapter`，投影稳定错误和 completion-pending 警告，
并让每个 vector read 绑定已验证 generation。内部 lifecycle/store/grant 均不直接暴露给
Wrapper；Q2 public resume、后台 worker、rollback 和 stage cleanup 不由本次扩展隐式开放。

修复验收以[R4-P1 计划的 ER-01–10 矩阵](../../overview/R4-P1-公共Embedding重建适配器实施计划-2026-09.md#6-验收矩阵)
为准。W2 的 ER-01/02 身份修复已有 identity/Kernel `49 passed、7 deselected`、
真实 CLI/MCP 身份黑盒 `7 passed` 和最终组合公共流程黑盒 `9 passed`，均 exit 0；
运行范围及日志见 [W2 记录](../../overview/W2-身份队列与CLI门禁修复记录-2026-10.md)，
三组不累加为全量计数。这些证据覆盖同内容跨根确认拒绝、同根跨进程身份与完整 snapshot 漂移，
不关闭 R-02/R-06。A/D 竞争分离、完整 sealed stage 跨进程继续、
stage grant 到期后的合法只读采用、日/月预算恢复、usage/settlement 幂等、CAS 前后核验均
须独立覆盖。Core seam 测试不能代替真实 CLI/MCP 的公开继续协议黑盒；测试阶段/时钟
基础设施或公开入口未交付时记录 pending，不能宣称 source acceptance 已关闭这些缺口。
全部测试仍须经规定 wrapper 使用隔离 `.data-test`、确定性 seam 或正式 Provider 合同的
进程外 harness；不得读取真实凭据/Vault、运行真实迁移，或添加产品测试后门。
