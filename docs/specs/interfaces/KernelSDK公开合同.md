# Kernel SDK 公开合同（K1a/K2）

> 范围：`pkv_kernel` 的外部 Wrapper 编程边界。
> 状态：K1a/K2 公开合同；K1b 的本地安装证据见
> [Kernel SDK 本地 wheel 验证合同](./KernelSDK本地Wheel验证合同.md)。
> 2026-10-24 修订：当前版本/导出仍以实现为准；下文明确标注的 v2 是待实现目标。
> [生命周期一致性修复计划](../../overview/生命周期一致性修复设计与分阶段计划-2026-10.md)
> 记录已知缺口、兼容迁移及新验收；W2 已验证的 v1 根/快照确认绑定见
> [W2 修复记录](../../overview/W2-身份队列与CLI门禁修复记录-2026-10.md)，不代表其余缺口已修复。
> W1 已冻结[生命周期一致性 v2 接口](./生命周期一致性v2接口冻结合同-2026-10.md)的
> SDK/CLI/MCP 名称、DTO、错误与兼容阶段；新增接口仍未实现，当前清单与版本不变。

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
    minimum_sdk_version="0.8.1",
    maximum_sdk_version="0.8.1",
    required_capabilities=(
        "kernel.archive.v1",
        "kernel.configuration-snapshot-reload.v1",
    ),
)
```

- `__version__` 是 SDK build 版本，采用 `MAJOR.MINOR.PATCH`。
- `KERNEL_API_VERSION` 是公开 Python surface 的协议版本；当前为 `1.0.0`。
- `get_kernel_capabilities()` 返回不可变的 SDK 版本、API 版本、能力集合、Python 范围和平台范围。
- `require_kernel_compatibility()` 对 Wrapper 的包含式最低/最高 SDK 范围及所需能力 fail-closed；
  不匹配时抛出 `KernelCompatibilityError`，Wrapper 不得猜测或回退到 `src.*`。
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

## 兼容与废弃

- API major 1 中，已在 `__all__` 出现的符号不能移除、重命名或进行不兼容的参数/返回语义变化。
- 新增公开符号或能力是向后兼容的；Wrapper 应仅按自己声明的所需能力启用功能。
- 移除前必须至少经过一个兼容 SDK 版本：保留原符号、发出可观察的 `DeprecationWarning`、
  文档给出替代与移除的下一个 API major。没有该阶段不得移除。
- 新 API major 必须更新 `KERNEL_API_VERSION`、能力集合、此文档和 executable boundary tests。

### 2026-10 review/rebuild 协议迁移目标（待实现）

当前 SDK `0.8.1`、API `1.0.0` 和 v1 capability 清单不因本文改变。目标 API-major-2
引入 `kernel.traceable-outcomes.v2` 的强制版本 review 协议，以及
`kernel.embedding-rebuild.v2` 的 operation inspect/publish-only 继续协议。
它们不是目前可以请求或调用的能力。

先经至少一个兼容 SDK 版本提供旧 review 符号的 DeprecationWarning、替代协议与下个 major
说明，再进行 major 切换；本次文档不是该兼容版本的交付证据。切换时要求 major-1 的
Wrapper 必须握手失败；旧无版本 approve/return 即使留作诊断 stub 也必须明确拒绝，
不能自动读取最新版并批准。major-1 中不得静默加入必填参数或改变审批成功语义。
兼容期旧调用仍为 known_gap，直到迁移、切换、拒绝旧调用及 SDK-01 全部验收才可关闭。

W1 的 SDK、CLI、MCP 完整签名、DTO/错误码以
[v2 接口冻结合同](./生命周期一致性v2接口冻结合同-2026-10.md)为准；SDK-01 验证版本/能力握手、兼容期
弃用证据、旧入口拒绝、精确版本 review 和无私有 import 回退。版本号变更、弃用发行及
切换均属后续实施，不由文档改动执行。

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
- W2 保留 v1 durable replay key 的原材料及编码，以继续识别已落盘的 active operation，
  防止仅因 public plan id 加强而绕过旧记录、重复调用 Provider。这不是 v2 replay 迁移：
  分域 identity/schema、`budget_check_period`（timezone/local_day/local_month）、publish-only
  operation 身份和 ledger v3 仍属 W4 待实现目标。
- execute 是一次确认的 network mutation：独立 `ai.embedding_rebuild` hard cap 和 durable
  reservation/usage ledger 先于 Provider；Provider 仅在 root writer lease 外对受限 private stage
  写入，最终 generation 经 validate + pointer CAS 才 READY。当前 v1 的 durable replay
  实现过宽，存在预算拒绝后无法重新申请和 D 竞争后持续 processing 的 known_gap；
  目标防重复规则按 active/awaiting_publish/completed/零 Provider 拒绝/未知外部影响分类，
  不将所有历史终态视为永久 consumed。
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

### rebuild v2 目标：只继续发布，不重复 Provider

以下均为 W1 冻结、W4 待实现的 v2 规则，不是上述 v1 身份安全修复的交付范围。
新网络 plan 须绑定当前 `budget_check_period`；publish-only plan 绑定原 operation 与当前
发布材料，不产生新的网络预算预留。

v2 增加只读 operation inspect、publish plan，以及显式确认的 publish-only execute。
名称和完整签名已在 [W1 接口合同](./生命周期一致性v2接口冻结合同-2026-10.md)冻结；
每个写请求必须绑定原 operation_id、根、当前 plan 与完整
stage proof。它不接管 Q2 task，也不授权新的 Provider 调用或第二次预留相同费用。

A 争锁是零 Provider/零 stage 的前置拒绝。D 争锁在完整 stage 已封存后投影
`awaiting_publish`；不能无 lease 改共享 ledger，effective state 来自验证后的私有 proof
与 pointer，下次 lease 内再协调持久状态。proof 的产生、旧 grant 到期后的重新采用、
漂移终结及 usage 处理见 [R4-P1](../../overview/R4-P1-公共Embedding重建适配器实施计划-2026-09.md)。
无完整 proof 的 B/C 强杀恢复继续后置；v1 不支持这些新公开入口。

预算拒绝且零 Provider effect、零 reservation 时不消费执行资格。用户新的显式提交应在
当前 policy/timezone 的日/月周期重新检查额度；publish-only 继续沿用原 operation 的
费用事实，不作为新 network attempt。force 和预算期间不进入 material-transition replay
key，跨期不能让同一 active/awaiting_publish 工作重复调用 Provider。ER 用例未通过前
不得宣称 v2 可用。

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
brief = outcomes.render_research_brief(topic_id=topic.topic_id)
# 当前 v1 的无版本批准存在已知缺口；此示例到生成/审阅 DTO 为止。
# 不得读取“当前最新版”后替用户构造审阅确认。
```

- `outcomes` 是 body-free、Provider-free 公开模块：DTO 只包含 user-authored Topic/Claim、
  canonical `pkv://` locator、captured digest、状态、limitation 与相对 Markdown pointer；
  不暴露 source body、原始网页/凭据、绝对路径、Store、Layout、lease、Provider 或内部 service。
  Topic/question/Claim/BM25 query/候选标题中的绝对路径、`file:` URI 或带凭据 URL 均在此边界
  fail-closed，不会被持久化、渲染或回显。
- 只读 `get_outcome` / `get_research_brief` / `get_topic` / `list_topics` / `suggest_evidence`
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

### outcome v2 目标：输入事务、发布与版本审阅

输入 mutation、Topic `input_revision/input_digest` 更新、已有 brief 的 `needs_review`
必须同事务提交。brief 保存 `rendered_input_revision/rendered_input_digest`；只读 bundle
使用一致 SQLite snapshot，再核验外部 source/projection，任何输入绑定失配都不得维持
approved。任意 revision 的不可变 publication identity 和 orphan 恢复遵循
[P1-B 目标合同](../../overview/P1-B-可追溯知识成果最小合同实施计划-2026-09.md)，不局限首次 render。

v2 approve/return 请求必须提交用户实际审阅 DTO 中的以下元组；完整新增 Python 签名见
[W1 接口合同](./生命周期一致性v2接口冻结合同-2026-10.md)，当前 v1 函数尚未实现此协议：

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
兼容迁移按上节执行，OC 与 SDK-01 共同构成关闭条件。

## 非目标

K1a/K2/K1b 都不上传 PyPI、不改变 release hold、默认数据根、迁移授权或 MCP stdio-only
合同。K1b 的离线本地 wheel/clean-install 证据是在预置第三方依赖的解释器上证明源码隔离；
它不是 wheelhouse、依赖闭包、锁文件或正式发布声明。
