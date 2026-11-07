# Kernel SDK 公开合同（K1a/K2）

> 范围：`pkv_kernel` 的外部 Wrapper 编程边界。
> 状态：K1a/K2 公开合同；K1b 的本地安装证据见
> [Kernel SDK 本地 wheel 验证合同](./KernelSDK本地Wheel验证合同.md)。
> 2026-10-26 修订：当前本地 SDK build 为 `0.10.0`，API 保持 `1.0.0`；W3 Outcome v2
> 与兼容弃用保留，W4 Embedding v2、ledger 3 与 publish-only 已完成范围验收。W5 major 切换未推进。
> [生命周期一致性修复计划](../../overview/生命周期一致性修复设计与分阶段计划-2026-10.md)
> 记录已知缺口、兼容迁移及新验收；W2 已验证的 v1 根/快照确认绑定见
> [W2 修复记录](../../overview/W2-身份队列与CLI门禁修复记录-2026-10.md)，不代表其余缺口已修复。
> W1 已冻结[生命周期一致性 v2 接口](./生命周期一致性v2接口冻结合同-2026-10.md)的
> SDK/CLI/MCP 名称、DTO、错误与兼容阶段；W3 的最终同树验收、wheel 与整体结果统一登记在
> [W3 实施记录](../../overview/W3-Outcome一致性与版本审批实施记录-2026-10.md)。W4 状态见
> [Embedding 继续发布与预算恢复实施记录](../../overview/W4-Embedding继续发布与预算恢复实施记录-2026-10.md)；历史组件通过数不代表 W4 验收完成。

## 唯一导入面

外部 GUI、Web 或自动化 Wrapper 只能导入 `pkv_kernel`（包括明确承诺的
`pkv_kernel.contracts`、`pkv_kernel.lifecycle`、`pkv_kernel.embedding` 与
`pkv_kernel.outcomes` 子模块）。`src.*`、CLI、MCP、Store、
Workflow、Processor、Provider、日志和文件打开实现对象均是 Core 内部细节，不能作为
集成依赖或 monkeypatch 目标。

`pkv_kernel.__all__` 是 API major 1 的完整、可执行**包根**公开清单；
`pkv_kernel.contracts.__all__`、`pkv_kernel.lifecycle.__all__`、
`pkv_kernel.embedding.__all__` 与 `pkv_kernel.outcomes.__all__` 是仅有的四个承诺嵌套公开清单。除这五处以外，安装 wheel 中可被 Python 解析到的模块或属性均为实现细节，
不构成 Wrapper 依赖。五个清单的冻结断言位于
`tests/unit/test_kernel_wrapper_boundary.py`。

### 获取 Kernel 与高级 SDK 类型

`KnowledgeKernel` 是 factory-only 的公开操作端口：正常 Wrapper 只能通过已确认的
`pkv_kernel.lifecycle.RuntimeExecution.open_kernel()`（或
`open_kernel_from_execution()`）取得它；不得直接调用 `KnowledgeKernel(...)`，也不得构造
或保留 `src.application.KnowledgeApplication`。直接构造会抛出 `TypeError`。

`bootstrap_kernel()`、`configure_kernel()`、`get_kernel()`、`get_config()` 和
`reset_kernel()` 为 API-major-1 的兼容/内部测试符号。其中 `bootstrap_kernel()` 会调用历史
mutating bootstrap，`get_kernel()` 的旧 lazy 路径也不等同 R2 检查；它们可保留给 K1b
离线 smoke、既有嵌入式进程或已完成 lifecycle 的兼容代码，但不得作为新的 Wrapper 首次
启动协议。新的 Wrapper 必须先走下节的 `pkv_kernel.lifecycle`。

`Config`、结果/错误 DTO、Provider 设置与流类型，以及 `__all__` 中其他较低层的公开辅助类型
属于 API-major-1 的高级 SDK 类型。Wrapper 可以只经其 `pkv_kernel` 名称使用文档化语义；这不
授权导入、monkeypatch 或依赖它们在 `src.*` 中的来源、构造图或私有属性。`src.*` 始终是
不受支持的实现私有命名空间。

## 版本与能力握手

```python
from pkv_kernel import require_kernel_compatibility

capabilities = require_kernel_compatibility(
    minimum_sdk_version="0.10.0",
    maximum_sdk_version="0.10.0",
    required_api_major=1,
    required_capabilities=(
        "kernel.archive.v1",
        "kernel.configuration-snapshot-reload.v1",
        "kernel.traceable-outcomes.v2",
        "kernel.embedding-rebuild.v2",
    ),
)
```

- `__version__` 是 SDK build 版本，采用 `MAJOR.MINOR.PATCH`；当前本地兼容 build 为 `0.10.0`。
- `KERNEL_API_VERSION` 是公开 Python surface 的协议版本；当前为 `1.0.0`。
- `get_kernel_capabilities()` 返回不可变的 SDK 版本、API 版本、能力集合、Python 范围和平台范围。
- `require_kernel_compatibility()` 对 Wrapper 的包含式最低/最高 SDK 范围及所需能力 fail-closed；
  不匹配时抛出 `KernelCompatibilityError`，Wrapper 不得猜测或回退到 `src.*`。
- 包根函数与 `pkv_kernel.contracts.require_kernel_compatibility(sdk_version, ...)` 均新增
  optional `required_api_major`。当前 `None` 保留旧行为，严格整数 `1` 通过；bool、字符串、
  浮点数或其他整数均在 metadata 握手阶段拒绝，不打开数据根。旧 Wrapper 显式要求
  `maximum_sdk_version="0.9.0"` 时仍须拒绝 `0.10.0`，不能用兼容期名义绕过版本上界。
- 当前 K1a 支持范围为 CPython `>=3.11,<3.13`、Windows。完整跨平台 wheel/安装与测试工程
  留待 Rust 代码版本再重新评估，不由本合同预支。

能力标识在同一 API major 内只可新增，当前承诺：

- `kernel.lifecycle.v1`
- `kernel.runtime-lifecycle.v1`
- `kernel.archive.v1`
- `kernel.retrieval.v1`
- `kernel.entries.v1`
- `kernel.chat-sessions.v1`
- `kernel.configuration-snapshot-reload.v1`
- `kernel.embedding-rebuild.v1`
- `kernel.traceable-outcomes.v1`
- `kernel.traceable-outcomes.v2`
- `kernel.embedding-rebuild.v2`

上述已承诺标识全部保留。W4 的 `kernel.embedding-rebuild.v2` 已完成公开链路实现与组件
验证，按冻结合同 §6 作为兼容增量声明；W4 范围验收完成。当前 API-major-2 尚未实施。

## 兼容与废弃

- API major 1 中，已在 `__all__` 出现的符号不能移除、重命名或进行不兼容的参数/返回语义变化。
- 新增公开符号或能力是向后兼容的；Wrapper 应仅按自己声明的所需能力启用功能。
- 移除前必须至少经过一个兼容 SDK 版本：保留原符号、发出可观察的 `DeprecationWarning`、
  文档给出替代与移除的下一个 API major。没有该阶段不得移除。
- 新 API major 必须更新 `KERNEL_API_VERSION`、能力集合、此文档和 executable boundary tests。

### 2026-10 review/rebuild 兼容迁移状态

W3 的 SDK `0.9.0` / API `1.0.0` 已增加独立 Outcome v2 符号。旧
`approve_research_brief(*, topic_id, kernel=None)` 与 `return_research_brief(*, topic_id, kernel=None)`
保持原签名和既有成功语义，并实际发出 `DeprecationWarning`，包含对应 `*_v2` 替代名和
API major 2 将拒绝无版本 review 的说明。CLI/MCP 也提供可观察的替代提示；警告不污染
JSON stdout 或 MCP 协议帧。v1 DTO 的 `to_dict()` 不增加 v2 字段。

这是兼容准备，不是旧调用安全缺口已经关闭：v1 无版本调用仍是 R-04 `known_gap`，其
W1 红例须保留到 W5。W4 的 Embedding operation inspect/publish-only v2 已完成范围验收；W5
才切换 API-major-2、要求客户端显式 `required_api_major=2`、拒绝旧无版本 review，并
移除这两个领域的 v1 capability 声明。不能读取 latest 后替旧请求补四元组，也不能在
major 1 静默增加必填参数。W3 与 W4 的兼容 build、wheel 和最终同树证据分别按各阶段主记录
登记；本页不是发行声明。

W1 的 SDK、CLI、MCP 完整签名、DTO/错误码以
[v2 接口冻结合同](./生命周期一致性v2接口冻结合同-2026-10.md)为准；SDK-01 验证版本/能力握手、兼容期
弃用证据、旧入口拒绝、精确版本 review 和无私有 import 回退。OC-10a 的兼容准备属于
W3；OC-10b/SDK-01 的真实 major 切换与旧入口拒绝属于 W5，不能合并为一个已完成状态。

## K2 配置快照与 reload

一个 `KnowledgeKernel` 绑定一个不可替换的 Application config graph。`configuration_generation`
是该图的单调递增、可观察 generation：

1. 已启动操作捕获旧 Kernel/Application；其 Workflow、steps、Store、Provider、VectorStore
   始终只使用旧 config snapshot，即使此时完成 reload。
2. `reload_kernel()` 原子发布新的 legacy `Config` identity、`KnowledgeApplication` 和
   `KnowledgeKernel`；其返回对象与之后取得的默认 Kernel 使用新 snapshot。
3. 当前 process-default Kernel 的 `update_local_config(updates)` 在同一生命周期锁内完成
   受控本机配置写入并发布新的 default Kernel；过期的 former default 在写入前被拒绝，调用者
   必须使用返回的新 Kernel 进行后续操作。
4. 显式 `get_kernel(config_b)` 是隔离的操作图，不改变全局 Config A；其
   `update_local_config(updates)` 只写入并重载 B、返回新的 isolated B Kernel，绝不发布默认
   Kernel。默认组合、Workflow 和所有默认 steps 都显式传入 B。
5. 缺失 vector index 不会被负缓存：读取到无索引后会重探测。归档创建索引后，后续
   related/delete 路径可打开并缓存已存在的同一 snapshot 索引。

K2 本身只证明单进程 embedded snapshot 语义。跨进程单写者另由后续已经实现的 R3
writer lease 合同约束，不能由 K2 推导；Node/独立后台宿主仍未交付。

## R2 公开 runtime lifecycle

需要首次启动、健康检查或 setup 的 Wrapper 只使用下列 `pkv_kernel.lifecycle` 操作：

```python
from pkv_kernel import Config, lifecycle

config = Config()
inspection = lifecycle.inspect_runtime(config)  # 只读、零网络、零初始化
plan = lifecycle.plan_runtime(inspection)       # 只读、可安全展示 to_dict()

# 仅在操作者明确确认展示出的 plan 后；allow_network 默认为 False。
confirmation = lifecycle.confirm_runtime_plan(
    plan,
    allow_network=True,
)
execution = lifecycle.execute_runtime_plan(plan, confirmation)
kernel = execution.open_kernel()  # 或 lifecycle.open_kernel_from_execution(execution)
```

- `RuntimeInspection`、`RuntimePlan`、`RuntimeConfirmation` 与 `RuntimeExecution` 是
  opaque、进程内句柄；只能由对应 lifecycle 函数创建。它们的 `to_dict()` 是可展示/传输的
  无密钥 DTO，不能反序列化或伪造为可执行计划。
- `inspect_runtime` 和 `plan_runtime` 不创建目录、数据库、索引、锁或 Provider；
  `execute_runtime_plan` 只接受由本 façade 生成的确认，沿用 R2 的 revision 复检和 R3
  writer lease。缺确认或网络授权时投影稳定的 `PKVRuntimeError`。
- 成功 `RuntimeExecution` 私有地保存已确认的 immutable Config snapshot，并且只通过
  `open_kernel()` 组合 Kernel。`isolated=True` 可获得 K2 的显式 Config B 图，而不发布
  默认 Config A。
- 该接口不导出 `RuntimeContext`、`RuntimeLayout`、Store、Provider、lease 或路径；R4
  Embedding generation rebuild 位于下节的独立 gated façade。

## R4-P1 public Embedding generation rebuild

需要重建 generation 的 Wrapper 只能使用 `pkv_kernel.embedding`：

```python
from pkv_kernel import embedding

inspection = embedding.inspect_embedding_rebuild(config)
plan = embedding.plan_embedding_rebuild(inspection, force=True)
confirmation = embedding.confirm_embedding_rebuild(
    plan,
    approved=True,
    allow_network=True,
)
execution = embedding.execute_embedding_rebuild(plan, confirmation)
```

- `EmbeddingRebuildInspection`、`EmbeddingRebuildPlan`、`EmbeddingRebuildConfirmation` 与
  `EmbeddingRebuildExecution` 是 opaque handles；展示用 `to_dict()` 不包含路径、正文、endpoint
  原文、凭据、Provider、Store 或 lease。
- 当前 v1 public `plan_id` 已在 W2 增加 `data_root_identity_sha256` 与
  `runtime_snapshot_sha256`，分别绑定当前 canonical root 与本次 inspection 已验证的完整
  无密钥 snapshot 原始字节摘要；其余既有 source/count、pointer/manifest、contract、policy、
  v1 action 与 force 材料保留。同根、同 snapshot 可跨进程重算；跨根或完整 snapshot 漂移
  fail-closed。修复前的 public plan id 需要重新 inspect/plan/confirm，不自动升级旧确认。
  摘要作为 identity 内部材料，不新增 v1 DTO 必填字段。Core 的进程私有 Config-source fence
  继续检测一次执行内的秘密配置变化；私有 HMAC、API key 及其可验证指纹不进入公共身份。
- W2 当时保留 v1 durable replay key 的原材料及编码，以继续识别已落盘的 active operation，
  防止仅因 public plan id 加强而绕过旧记录、重复调用 Provider。这不是 v2 replay 迁移：
  分域 identity/schema、`budget_check_period`（timezone/local_day/local_month）、publish-only
  operation 身份和 ledger v3 属于下节 W4 增量，不改写 W2 当时的验收结论。
- execute 是一次确认的 network mutation：独立 `ai.embedding_rebuild` hard cap 和 durable
  reservation/usage ledger 先于 Provider；Provider 仅在 root writer lease 外对受限 private stage
  写入，最终 generation 经 validate + pointer CAS 才 READY。W4 让 v1/v2 复用修复后的 Core：
  防重复规则按 active/awaiting_publish/completed/零 Provider 拒绝/未知外部影响分类，
  不将所有历史终态视为永久 consumed。旧 ledger 1/2 的 v1 mutation 需要先走 v2 显式升级，
  返回 `embedding_ledger_upgrade_required`；空/已 v3 ledger 可沿用 v1 rebuild。
- v1 不公开 Q2 task 的 resume/retry、operation lookup/cancel、rollback、flat-vector fallback 或
  手工路径/Provider 注入。这是 source acceptance capability，不构成 Artifact/release 资格。

W2 的 ER-01/02 证据已绑定至 [R4-P1 第 10 节](../../overview/R4-P1-公共Embedding重建适配器实施计划-2026-09.md#10-w2-r-01-身份修复与验收证据)：
真实 CLI/MCP 节点分别为
`tests/blackbox/test_embedding_rebuild_cli_blackbox.py::test_public_cli_rebuild_confirmation_is_root_bound_and_process_stable`
与 `tests/blackbox/test_embedding_rebuild_mcp_blackbox.py::test_mcp_rebuild_confirmation_is_root_bound_across_fresh_servers`；
完整 snapshot 漂移由
`tests/unit/test_w2_embedding_identity.py::test_er02_full_snapshot_change_invalidates_public_confirmation[mapping_extension]`
及 `tests/unit/test_w2_embedding_identity.py::test_er02_full_snapshot_change_invalidates_public_confirmation[raw_bytes]`
验证。运行 `j-40q7zd` 为 `49 passed, 7 deselected`，`j-92sed5` 为
`7 passed`，均 exit 0；完整命令、oracle 和兼容回归见该节及
[W2 修复记录](../../overview/W2-身份队列与CLI门禁修复记录-2026-10.md)。前一运行显式排除
R-02/R-06，不能据此把它们或 W4 标为已通过。

### W4 rebuild v2：只继续发布，不重复 Provider（已完成范围验收）

以下是 W1 冻结、W4 已完成范围验收的独立 v2 合同，不是 W2 当时的身份修复交付范围。
新网络 plan 须绑定当前 `budget_check_period`；publish-only plan 绑定原 operation 与当前
发布材料，不产生新的网络预算预留。

`pkv_kernel.embedding.__all__` 在保留 v1 名称的同时增加八个函数与七个 opaque handle：

| 路径 | 函数 | 返回 handle |
| --- | --- | --- |
| 网络重建检查 | `inspect_embedding_rebuild_v2(config=None)` | `EmbeddingRebuildInspectionV2` |
| 网络重建计划 | `plan_embedding_rebuild_v2(inspection, *, force=False)` | `EmbeddingRebuildPlanV2` |
| 网络重建确认 | `confirm_embedding_rebuild_v2(plan, *, approved=True, allow_network=False)` | `EmbeddingRebuildConfirmationV2` |
| 网络重建执行 | `execute_embedding_rebuild_v2(plan, confirmation=None)` | `EmbeddingRebuildExecutionV2` |
| 原操作检查 | `inspect_embedding_operation_v2(operation_id, *, config=None)` | `EmbeddingOperationInspectionV2` |
| 发布/完成对齐计划 | `plan_embedding_publication_v2(inspection)` | `EmbeddingPublicationPlanV2` |
| 发布确认 | `confirm_embedding_publication_v2(plan, *, approved=True)` | `EmbeddingPublicationConfirmationV2` |
| 发布执行 | `execute_embedding_publication_v2(plan, confirmation=None)` | 复用 `EmbeddingRebuildExecutionV2` |

名称、完整类型签名和封闭字段在 [W1 接口合同](./生命周期一致性v2接口冻结合同-2026-10.md)冻结。
七个类型均由 façade 创建，不能直接构造、copy、pickle 或由 JSON 恢复为执行能力；公开属性与
`to_dict()` 返回独立安全副本，调用者修改展示数据不能改变执行计划。跨进程只能提交 public
plan ID，再在当前 Config 重算并精确匹配。inspect/plan 零写、零 lease、零 Provider。

每个 publication 写请求必须绑定原 operation_id、根、当前 plan 与完整
stage proof。它不接管 Q2 task，也不授权新的 Provider 调用或第二次预留相同费用。

A 争锁返回 `write_busy`，不创建 operation、reservation、Provider 或 stage。D 争锁在完整 stage
已封存后返回 `embedding_publication_pending`，safe details 明确原 operation、
`effective_state=awaiting_publish`、`provider_effect=completed` 与 `publish_only_required=true`。
不能无 lease 改共享 ledger，effective state 来自验证后的私有 proof
与 pointer，下次 lease 内再协调持久状态。proof 的产生、旧 grant 到期后的重新采用、
漂移终结及 usage 处理见 [R4-P1](../../overview/R4-P1-公共Embedding重建适配器实施计划-2026-09.md)。
无完整 proof 的 B/C 强杀恢复继续后置；v1 只按旧 DTO 形状透传 pending code/details，不支持
lookup/publish 新入口。pointer 已提交时仍返回 ready 与 completion-pending 警告；后续仅对齐
ledger/audit，不重跑 Provider 或 pointer CAS。完整完成后再次请求发布计划明确拒绝。

ledger 3 在新根直接使用；legacy 1/2 的 inspection 只读分类并展示升级摘要，v2 plan 绑定
该摘要，明确确认后在 A 的 lease 内原子升级。legacy schema 1 没有 public_plan_id，公共
`original_plan_id` 保持 null，绝不能用私有 Core plan_id 代填。grant 2 的有效期内封存事实与
完整 artifact/manifest SHA 组成 sealed proof；原 grant 到期后禁止续写，但可经新 publish grant
验证并只读采用完整封存产物。Provider usage 的 partial 已报告合计只是下界，完全未知维度为
null；跨进程保守结算取 reservation 与已知 Provider total 的较大值，保留已知超额。

预算拒绝且零 Provider effect、零 reservation 时不消费执行资格。用户新的显式提交应在
当前 policy/timezone 的日/月周期重新检查额度；publish-only 继续沿用原 operation 的
费用事实，不作为新 network attempt。force 和预算期间不进入 material-transition replay
key，跨期不能让同一 active/awaiting_publish 工作重复调用 Provider。当前 ER/MIG 状态与完整
验收证据统一登记于 [W4 主记录](../../overview/W4-Embedding继续发布与预算恢复实施记录-2026-10.md)。

CLI 增加 `embedding inspect-v2 / rebuild-v2 / operation-v2 / publish-v2` 四个叶子；MCP 增加
4 个只读与 2 个写入 Tool，总计 41（23 只读 + 18 写入）。v2 均采用
`pkv.lifecycle-response.v2` envelope，operation 固定为对应 MCP Tool 名，成功 issues 保留完成
对齐警告。publish 不接受 force/network；确切签名和退出码仍以冻结合同 §5 为准。

## P1-B traceable outcomes

需要创建可审阅、可追溯的研究成果的 Wrapper 只使用 `pkv_kernel.outcomes`：

```python
from pkv_kernel import outcomes

topic = outcomes.create_topic(title="问题", question="需要回答的具体问题")
evidence = outcomes.attach_evidence(
    topic_id=topic.topic_id,
    locator="pkv://entries/42",
)
claim = outcomes.create_claim(
    topic_id=topic.topic_id,
    statement="用户明确提出的有限主张",
    state="supported",
)
outcomes.link_claim_evidence(
    claim_id=claim.claim_id,
    evidence_id=evidence.evidence_id,
    polarity="support",
)
brief = outcomes.render_research_brief_v2(topic_id=topic.topic_id)
# 展示 brief.to_dict()，并保存该次展示的 review_confirmation 等待用户明确审阅。
# 审批时传回保存的四元组；不得重新 get 最新版替换用户已审阅的确认。
```

- `outcomes` 是 body-free、Provider-free 公开模块：DTO 只包含 user-authored Topic/Claim、
  canonical `pkv://` locator、captured digest、状态、limitation 与相对 Markdown pointer；
  不暴露 source body、原始网页/凭据、绝对路径、Store、Layout、lease、Provider 或内部 service。
  Topic/question/Claim/BM25 query/候选标题中的绝对路径、`file:` URI 或带凭据 URL 均在此边界
  fail-closed，不会被持久化、渲染或回显。
- 只读 `get_outcome` / `get_research_brief` / `get_outcome_v2` / `get_research_brief_v2` /
  `get_topic` / `list_topics` / `suggest_evidence`
  不获取 writer lease、不写入状态；它们复核 evidence binding，并把 drift/delete 表示为
  `stale`/`unavailable`、Claim `insufficient` 和 brief effective `needs_review`。
- 显式 mutation（Topic、attach/detach Evidence、create/revise Claim、link/unlink、render、
  approve/return）只经 Application root writer lease 与审计。Evidence 不自动重抓、不重绑；
  strict BM25 suggestion 也不构造 QueryRouter、LLM、Embedding 或 vector generation。
- `research_brief` 的结构权威在 SQLite；每次 render 先发布不可覆盖的 deterministic Markdown
  revision，后提交其相对 pointer。每次只读访问都会安全地复核 pointer 的 `content_sha256`：只有
  可直接确认的缺失/字节漂移显示为 `projection_state=unavailable|stale` 与 effective
  `needs_review`；权限、I/O 或路径状态不确定则返回安全错误。不会把 SQLite 中旧的 approved state
  当作仍可用的投影。规范要求批准仅对被审阅 revision 生效，不回写 Entry、关系或检索语料；
  当前 v1 只收 topic_id，尚不能保证这个要求，属于 known_gap。
- 这是 source-level capability；它不解锁 Artifact/release、真实 Provider、GUI、自动写作或 Q2
  public resume/retry。

### Outcome v2：W3 已实现的公开接口

以下十个符号已在 `pkv_kernel.outcomes.__all__` 中单独登记，没有新增公开子模块：

| 类型/操作 | 当前合同 |
| --- | --- |
| `ResearchBriefReviewConfirmation` | frozen 四元组：`outcome_id / expected_revision / content_sha256 / expected_input_revision`；只表示用户保留的审阅输入，不是绕过复核的授权能力。 |
| `ResearchBriefReturnReason` | `needs_revision / insufficient_evidence / other`；不接收自由文本退回原因。 |
| `ResearchBriefRecordV2` | 独立不可变 DTO，schema 为 `pkv.outcome.brief.v2`；明确包含 publication/input/renderer binding、projection/effective state 与可用时的 `review_confirmation`。 |
| `OutcomeBundleV2` | schema 为 `pkv.outcome.bundle.v2`；topic 是不可变 mapping，增加稳定 outcome ID 与当前输入绑定；evidence/claims 保留 v1 body-free 字段，brief 使用 v2 DTO。 |
| `ResearchBriefReviewResultV2` | schema 为 `pkv.outcome.review-result.v2`；仅包含 brief、opaque `review_receipt_id` 和严格 bool `idempotent`。 |
| `get_outcome_v2(topic_id, *, kernel=None)` | 一致只读 bundle；Topic 不存在返回 `outcome_not_found`。 |
| `get_research_brief_v2(*, topic_id=None, outcome_id=None, kernel=None)` | selector 必须且只能给一个；合法但不存在时返回 `None`。 |
| `render_research_brief_v2(*, topic_id, kernel=None)` | 使用持久 renderer 合同发布绑定输入的不可变 revision。 |
| `approve_research_brief_v2(*, confirmation, kernel=None)` | 校验并批准用户保留的精确四元组，不获取 latest 补值。 |
| `return_research_brief_v2(*, confirmation, reason_code, kernel=None)` | 校验精确四元组及必填受限原因，返回 receipt 与幂等事实。 |

标识采用冻结的 opaque ID 形状；revision 只接受 `1..2**63-1` 的严格整数，bool 不作为
整数；摘要只接受 64 位小写十六进制。对象请求拒绝额外字段。上述 DTO 的 `to_dict()`
只序列化冻结字段，不转发内部 metadata。新版只读入口不初始化、不迁移、不取得 writer
lease：013/schema 1.2.7 返回 `database_upgrade_required`；伪造最新 version 但缺少必要 CHECK
等结构时返回 `database_schema_drift`，不能因绕过 bootstrap 取得访问资格。

输入 mutation、Topic `input_revision/input_digest` 更新、已有 brief 的 `needs_review`
必须同事务提交。brief 保存 `rendered_input_revision/rendered_input_digest`；只读 bundle
使用一致 SQLite snapshot，再核验外部 source/projection，任何输入绑定失配都不得维持
approved。任意 revision 的不可变 publication identity 和 orphan 恢复遵循
[P1-B 目标合同](../../overview/P1-B-可追溯知识成果最小合同实施计划-2026-09.md)，不局限首次 render。

v2 approve/return 请求必须提交用户实际审阅 DTO 中的以下元组；完整 Python 类型、字段与
adapter 映射见 [W1 接口合同](./生命周期一致性v2接口冻结合同-2026-10.md)。旧 v1 函数
不采用此新签名：

```text
outcome_id
expected_revision
content_sha256
expected_input_revision
```

事务内必须核对当前 pointer、完整确认元组、输入版本和可审阅状态；source/projection
漂移仍按 P1-B 规则拒绝。匹配后才批准或退回，版本变化返回稳定 conflict，要求重新读取/
审阅。重复的精确请求只有当前元组与目标状态仍匹配时才可幂等成功。禁止省略元组、默认
latest、把无版本旧请求包装成“先 get 再 approve”，也禁止在旧 major 中悄悄加必填参数。
匹配的 returned revision 只提供同原因 return 的幂等查询，不能再次 approve；后者须显式
render 新 revision。legacy_unverified、输入/renderer 不匹配或 source/projection 不可验证
时，不提供 review confirmation。普通 render 保留已持久化 renderer 版本；不支持时返回
`outcome_render_contract_unsupported`，不随 SDK 升级静默重渲染。

CLI 五个命令为 `outcome get-v2 / brief-v2 / render-v2 / approve-v2 / return-v2`；MCP 五个
工具为 `get_outcome_v2`、`get_outcome_research_brief_v2`、`render_outcome_research_brief_v2`、
`approve_outcome_research_brief_v2`、`return_outcome_research_brief_v2`。它们统一使用
`pkv.lifecycle-response.v2` envelope，operation 固定为相应 MCP Tool 名称；具体参数、
退出码及 stdout/stderr 规则仍以冻结合同为准。

### W3 组件证据与完成边界

- [SDK/architecture 日志](../../../.data-test/w3-sdk-20261025-boundary/pytest.log)：
  `tests/unit/test_w3_outcome_public.py`（75 项）与 `tests/unit/test_kernel_wrapper_boundary.py`
  （9 项），**84 passed / 20.80s，exit 0**。包含直接 SDK 的旧/伪 schema 零写拒绝、
  严格 DTO、v1 payload/警告兼容以及 package/contracts 握手。
- [真实 CLI/MCP 黑盒日志](../../../.data-test/w3-adapter-20261025-full/pytest.log)：
  两份新增 `test_w3_outcome_*_blackbox.py` 的 18 项与既有 Outcome 黑盒 3 项，
  **21 passed / 230.03s，exit 0**。覆盖旧确认拒绝、精确审批跨进程重放、严格 MCP 参数
  与旧入口弃用。一次启动错误 stage 透传微调发生在该组件运行中段，最终同树证据由主记录另行固定。

以上是组件证据，不是 P0、MCP 覆盖率、wheel 或整体 W3 已通过的声明。最终冻结指纹、
完整命令及同树验收统一见 [W3 实施记录](../../overview/W3-Outcome一致性与版本审批实施记录-2026-10.md)。
R-04 仍须等待 W5 的旧入口拒绝及 OC-10b/SDK-01，不能用新 v2 成功测试关闭。

## 非目标

K1a/K2/K1b 都不上传 PyPI、不改变 release hold、默认数据根、迁移授权或 MCP stdio-only
合同。K1b 的离线本地 wheel/clean-install 证据是在预置第三方依赖的解释器上证明源码隔离；
它不是 wheelhouse、依赖闭包、锁文件或正式发布声明。
