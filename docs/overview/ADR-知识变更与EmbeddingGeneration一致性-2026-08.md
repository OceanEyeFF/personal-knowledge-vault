# ADR：知识变更与 Embedding Generation 一致性

**状态：策略已确认；实施计划等待用户确认**
**初始日期：2026-08-23；本次更新：2026-08-31**

## 决策

R4 采用**选项 2：mutation 后显式 `rebuild_required`** 作为内部一致性策略；但不把
“显式”误解为每一份文档都要人工确认。一次性的 AI 自动化授权、Provider 授权与费用
策略确认生效后，Application 内部生命周期会自动调度后续 AI 工作：

```text
文档的核心持久化提交
  -> binding = rebuild_required
  -> 内部任务获 lease、重检、预留用量配额（有价格卡时再预留金额）
  -> processing（摘要 / 标签 / Embedding / staged generation）
  -> 通过验证后原子切换 ready generation
```

没有已验证且与当前知识源一致的 ready generation 时，绝不使用旧 generation、平铺
`config.vector_index_dir` 或 `no_hits` 掩盖该事实。文档核心持久化与 AI 完成是两个可
区分的结果：AI 处理中、预算暂停或可重试失败时，文档仍须安全保存。

本决定不授权新的 public rebuild API、CLI rebuild 命令、MCP rebuild Tool，亦不改变
`pkv_kernel` 的公开 capability。自动重建只是 Application-owned 的内部生命周期能力。
完整的分阶段计划见
[R4 自动 AI 生命周期与费用控制实施计划](./R4-自动AI生命周期与费用控制实施计划-2026-08.md)。

## 已验证的现状与问题

Embedding lifecycle 已具备 staged generation、完整性清单、active pointer 与严格的
pointer 解析原语。它们目前是内部生命周期原语；Application 的 archive、delete、
related、`VectorRetriever`、CLI、MCP 与 Kernel 仍直接使用平铺的
`config.vector_index_dir`。首次“无索引 → archive”也仍可创建平铺索引。此分叉必须由
本 ADR 的 adapter 实施消除，不能被 flat fallback 或 `no_hits` 隐藏。

当前 archive workflow 仍是“AI 分析 → StoreStep（Markdown、SQLite、平铺 Vector）”。
因此它不能满足“核心文档先安全保存、AI 后续自动完成”的产品承诺。R4 实施必须把核心
存储提交、任务入队、AI enrichment 与 generation 发布拆开；本 ADR 不宣称当前代码已经
完成该转换。

## 不变量

- 一次 vector retrieval、related 或向量删除只能经 Application-owned generation binding
  解析；不得直接读写平铺 `config.vector_index_dir`，pointer 缺失时也不得回退到它。
- `ready` 仅表示 generation、Embedding contract 与当前知识源 revision 全部验证一致。
  任何不一致、未就绪或损坏状态都必须投影为稳定、可区分的状态，绝不能伪装为 `no_hits`。
- BM25 不依赖 generation，可继续只读工作；`vector` / `related` 必须报告 generation 状态。
  `hybrid` 如选择降级为 BM25，必须显式标出 vector unavailable 和具体原因，不能把结果称为
  完整 hybrid 命中。
- archive/delete 的业务 mutation、binding 状态转换、任务 claim、用量/金额 reservation、实际
  用量结算、stage/pointer publish、审计和 runtime file log 都是产品 data-root writer，均须
  由同一 `RuntimeLayout` 的有效 R3 writer lease 保护。
- 任何可能创建 Provider/网络客户端的自动任务都遵循：获得 writer lease → 重检任务、来源、
  Config snapshot 与授权 → 用量配额预留（有价格卡时再预留金额）→ 创建 Provider。竞争者先得到 `write_busy`，不会先
  构造 Provider 或发起网络。
- 一个运行中的任务绑定其捕获的 Application snapshot；reload 不会把新 Config/Provider/
  Layout 混入旧任务。若重检发现 source、授权或 Config revision 漂移，旧任务持久化为
  stale/retry 状态，由新 snapshot 重新计划。
- binding、快照 reload、lease fence、费用账本与失败恢复均属于 Core Application 生命周期
  内部。外部 GUI 只能依赖 `pkv_kernel`，不得 import `src.*`；本 ADR 不为它新增 rebuild
  能力。

## 状态与用户可见投影

`embedding_index` 的未来版本化 binding 至少表达下列状态；任务明细和用量账本不放入
可编辑用户配置，也不存储凭据或文档正文。

| 内部 binding 状态 | 条件 | vector / related 的稳定投影 |
| --- | --- | --- |
| `ready` | 当前 source revision 的 generation 已验证并已 pointer publish | 正常执行 |
| `rebuild_required` | 核心 mutation 已提交，任务尚未 claim | 已授权并入队时为 `embedding_processing`；未授权时为 `embedding_automation_authorization_required` |
| `processing` | worker 已 claim，且正重检、执行 AI 或构建 generation | `embedding_processing` |
| `retry_required` | 可恢复的 Provider、stage 或 stale-plan 失败已持久化 | `embedding_retry_required`，带不泄密的下一步/重试原因 |
| `budget_paused` | 用量配额或（有价格卡时的）金额 reservation 会超过日/月上限 | `embedding_budget_paused`，不得创建 Provider |
| `repair_required` | source、pointer、manifest 或账本不可验证 | `repair_required` |

删除也必须先把 binding 置为非 ready，再由同一内部流程构建 successor generation。即使
当前 pointer 指向旧的完整 generation，也不得在 mutation 后继续用它回答 vector/related。
“首次无索引 → archive”同样先保存文档并进入 `rebuild_required` / `processing`；仅在首个
generation 发布后 related 才可用。

## 自动化与费用授权

自动化不是无条件网络开关。首次启用必须在唯一可编辑用户配置
`%USERPROFILE%\\.pkv\\config.yaml` 中保存一个经确认的、无密钥授权记录；其有效指纹至少
覆盖：启用状态、Provider/endpoint 的不可逆 contract 指纹、LLM 与 Embedding model、计费时区、
日/月 token hard cap、估算与重试策略。price card、币种和金额 hard cap 均为可选的第二层
控制；存在时也纳入授权指纹。API key 本身绝不写入该记录、runtime snapshot、任务或审计。

- 无有效授权、token 配额/重试策略变更、模型变更或（若启用）price-card/金额预算变更时，
  自动任务暂停并要求一次新的确认；不逐文档询问。只轮换 API key 会使已在飞任务按 Config
  revision stale/replan，但若 Provider/model/用量策略未变，不单独扩大授权。
- 单文档预估首先是本地 token estimator 的预估，不会创建 Provider。若有已确认的 price
  card，才额外显示金额预估；没有 price card 时**不计算、不显示推测价格**。
- worker 在 Provider 创建前以 `已结算 token + 已预留 token + 本次保守 token 预估` 同时
  检查日/月 token cap，并在 lease 内原子预留。超限只写 `budget_paused` 与可见提示，不做
  网络调用。有价格卡和金额 cap 时，对金额做同样的附加检查；金额控制不能替代 token cap。
- 实际用量优先采用 Provider 返回的可验证 usage，并分别记录 `uncached_input_tokens`、
  `cached_input_tokens`、`generated_tokens` 与 `embedding_input_tokens`（适用时）。Provider
  未报告某一细分字段时保存 unknown/来源标记，绝不记作零；未返回可信 usage 时保留保守 token
  reservation。仅在有已确认价格卡时，才从这些用量结算金额。账本不保存正文、密钥或 URL 凭据。
- 价格不从网络自动抓取；price card 是可选、随产品版本审阅的本地数据。新增、移除或变更
  price card/金额预算，以及模型或 token 策略变更，都必须重新确认后才会继续自动工作。

## 考虑过的其他策略

### 选项 1：每次 archive/delete 同步产生并确认 successor generation

优点是 mutation 成功时相关功能立刻可用。缺点是每次写入都会变成昂贵、依赖网络、可能
长时间持有 lease 的复合操作；尤其删除一个条目也可能全量重嵌入，并且难以把“文档已保存”
和“AI 失败”解释清楚。它也不能自然实现一次授权后的排队、预算 reservation 和失败恢复。

### 选项 2：mutation 后 `rebuild_required`，由内部自动生命周期处理（已选择）

核心 mutation 先落盘，再持久化 binding/job；有有效授权和预算时，内部 worker 自动执行
enrichment 与 staged rebuild。它既不把旧 generation 当作正确答案，又避免每篇文档的确认
摩擦，并能清晰呈现 processing、retry、budget pause 和 repair。

### 选项 3：delta / overlay generation

需要定义双层 ANN、一致性、删除屏蔽、压实、pointer CAS、崩溃恢复、费用核算与 reload 的
全部组合。当前 staged core 没有这些原语，复杂度和错误面明显过高；不作为本轮实现。

## 运行宿主边界

当前 Core 是 headless、无 daemon 的 Windows-first 程序，Node/Docker 均仍是后置门禁。
R4 v1 因而采用**持久任务 + 当前 Application 进程内自动 drain**：archive 在核心提交后自动
尝试处理；可恢复失败被持久化为 `retry_required`，下一次允许写入的内部生命周期触发可继续
drain。只读 status/related 不得为了重试而获取 lease、创建 Provider 或写 data root。

这保证没有“每文档手动确认”，但不承诺进程退出后存在独立后台服务继续运行。若产品随后
要求“关闭所有宿主后仍自动重试”，必须先单独通过 Node/持久 worker 的门禁，不能暗中把
daemon、Docker 或 GUI 代码带回 Core。

## 实施约束与非目标

- R4 实施前可补 characterization tests、内部 binding 设计和文档；在本 ADR 的实施计划
  获用户确认前，不改变 archive/delete 的正式 generation 语义。
- 不公开 `pkv_kernel` rebuild API，不增加 CLI rebuild 命令或 MCP rebuild Tool；已有 archive、
  related、search 等入口只可投影状态和自动处理结果。
- 若持久任务/费用账本需要数据库 schema 变更，既有用户数据根只能经 R2 的 inspect → plan →
  confirm → execute upgrade 路径升级；R4 不执行真实迁移，也不隐式修改用户 Vault。
- 不修改默认数据根、release hold、历史 release/install 合同或 MCP stdio-only 合同；不得
  重新引入 `src/gui`、PySide6、qasync、GUI 测试、`pkv-gui.exe`、Docker 或跨平台 CI parity。
- 所有实现和验证仅使用 `scripts/run-test.ps1`、隔离 `.data-test`、合成数据与 fake Provider；
  不读取真实 Vault、`%USERPROFILE%\\.pkv\\config.yaml` 或真实凭据。

## 进入实施前的确认点

用户已确认选项 2、“一次授权后的内部自动处理”与“无价格卡时只记录 token、不估价”的
费用原则。进入代码实施前还需确认随附实施计划的宿主边界：

1. v1 是进程内自动 drain，不承诺独立后台 daemon；进程退出后的可恢复任务显示“待重试”，
   由下一次允许写入的内部生命周期恢复。
自动化默认关闭；启用时必须明确提供计费时区和至少一个日/月 token hard cap。price card、
币种和金额上限可选；没有 price card 时只记录 token、绝不推测价格。缺少 token 配额、超限或
策略变更一律暂停，不存在隐式 unlimited usage。
