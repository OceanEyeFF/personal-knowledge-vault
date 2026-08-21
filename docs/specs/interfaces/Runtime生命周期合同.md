# Runtime 生命周期合同（R2 基础）

状态：R2 核心合同已实现并有隔离回归；CLI/MCP 等适配器只能投影本合同，不能重新解释或绕过它。

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
  schema_version: "1.2.4" # bundled migration chain 的 canonical semver
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
内部合同与尚未完成的 adapter gate 见
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

`execute_runtime_plan(..., writer_lease_factory=...)` 已预留 R3 的跨进程单写接点。factory
接收这次计划绑定的显式 Config，返回上下文管理器；获得 lease 后还会再次 inspect，避免
等待锁期间混合旧计划与新状态。R2 不替代 R3 的 lease 实现。

唯一被 re-inspect 视为执行基础设施的文件是
`<data-root>/runtime/write.lease` 及其空父目录：缺失、空数据根或仅此 lease anchor 都是
同一 fresh state。数据库、journal、runtime snapshot、Provider contract 与任何其他数据根
内容仍会使计划 stale；lease anchor 不能掩盖真实数据漂移。

## 后续边界

- R3 已为受支持的 Application/CLI/MCP 业务/数据 mutation 提供默认 writer lease，并由
  `write_busy` 回归覆盖竞争写入；这不宣称所有文件系统写入均已串行化。运行时日志写入的
  所有权和遗留维护 writer 的 lease/隔离是 R3.1 P1，不能由本文件推定已完成。
- R4 staged core 已扩展 runtime snapshot 的 Embedding generation 契约、confirmed rebuild、
  retained previous generation 与 atomic pointer；公开 CLI/MCP/Kernel adapter、自动清理和
  rollback action 仍是独立 gate，不能由 core 测试反推已经公开。
- CLI/MCP/Kernel 适配器必须调用或投影此 lifecycle，不得以 legacy bootstrap 偷偷完成
  inspect/plan 阶段的写入；CLI/MCP 的 status/setup/repair 适配行为须由各自模块合同和
  回归单独验证，不能反推 R3.1 或 R4 public-adapter gate 已完成。其具体命令/Tool envelope
  由各自公开合同冻结。
- 数据库 upgrade、journal repair 永远先展示影响和计划，后续专用 workflow 才能在确认后执行。
