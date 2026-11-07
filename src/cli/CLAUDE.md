# CLI 模块

[根目录](../../CLAUDE.md) > [src](../) > **cli**

## 模块职责

`src/cli/` 提供基于 Click 与 Rich 的命令行适配层。运行入口是 `src.main`；正式的 `python -m src.cli.commands` 也委托该入口，共用 `LazyCLIGroup` 和叶子门禁。`LazyCLIGroup` 按需加载 `archive`、`archive-text`、`search`、`show`、`list`、`tags`、`related`、`outcome`、`config`、`inspect`、`setup`、`repair`、`embedding` 与 `stats`，避免标准入口的 `--version` 导入重依赖。

关键调用关系与应满足的接口合同：

- `--help`、`--version` 与 Click 参数校验必须先于任何 runtime 读取或检查。业务叶子命令在成功
  解析后才检查同一 immutable Config snapshot：`archive` / `archive-text` / 当前 `outcome` 叶子必须
  `READY`；`search`、`show`、`list`、`tags`、`related`、`stats` 可在 `DEGRADED`
  状态读取已提交数据，但不会隐式初始化、迁移、恢复、探测 Provider 或打开文件日志。
  `setup_required`、`repair_required`、`upgrade_required` 仍返回稳定 readiness 错误。
- `inspect` 永远只读地输出 runtime inspection 与 plan；`setup` / `repair` 默认
  同样只输出计划，执行必须同时提供 `--apply --confirm PLAN_ID`，涉及 Provider
  探测时还必须显式 `--allow-network`。CLI 只负责这些确认边界、参数/本地文件能力
  校验与渲染，不自行装配 Store、Workflow、Retriever 或 Provider。
- `archive` 调用 `application.archive_cli_input()`；`archive-text` 始终按字面
  纯文本调用 `application.archive_text()`，路径形状文本不会触发本地文件读取。
- 两个 archive 命令在内部走 R4 Q0 admission → Q1′ core commit/handoff → Q2 AI
  derivation。只有 `core_committed` 后才投影为已保存；Q2 的
  `retry_required` / `budget_paused` / `authorization_required` 是可观察的
  degraded 状态。`embedding` 的 v1/v2 叶子只经 `pkv_kernel.embedding` 执行独立、明确确认的
  generation maintenance；v2 可继续发布原操作的完整封存产物，不直接 drain 或 resume 内部 Q2 task。
- `search` 调用 `application.search()`，并原样消费五态 `SearchResponse`；BM25
  路径不提前创建 Provider。
- `show`、`list`、`tags`、`stats` 与 `related` 都通过 application 的领域操作；
  `related` 使用严格只读向量入口，索引或条目向量缺失时明确返回 `degraded`。
- `config show/get` 读取配置并遮罩敏感值；`config set` 会写唯一用户配置
  `%USERPROFILE%\.pkv\config.yaml`，而非 data root 内的 runtime snapshot。

## 安全运行边界

自动测试和 AI 协作只使用隔离包装器：

```powershell
.\scripts\run-test.ps1 --help
.\scripts\run-test.ps1 --version
.\scripts\run-test.ps1 inspect
.\scripts\run-test.ps1 setup
.\scripts\run-test.ps1 config show
.\scripts\run-test.ps1 config get storage.data_root
```

包装器把非 `-Direct` 命令接到 `tests/offline_entrypoint.py cli`，只加载 base config，并把产品运行路径限制在本次 `.data-test` 根内。默认离线入口安装 Python 进程内 socket guard，阻断 DNS 与非 loopback 连接，但允许 loopback/AF_UNIX，且不等价于 OS 网络沙箱；因此真实 URL 归档不属于这些示例。

`config set` 会修改真实本机用户配置，测试包装器会在创建运行时目录前拒绝。只有用户明确授权时才由用户直接编辑 Git 忽略的 `%USERPROFILE%\.pkv\config.yaml`；AI 和默认测试不执行该写入。生产 Vault 查询、真实 URL、真实 Provider 与费用也不属于默认 TestCase lane。

## 当前公开命令合同

| 命令 | 主要参数/选项 | 当前接线与稳定边界 |
|---|---|---|
| `archive URL_OR_PATH` | `--skip-sharpen`、`--tags`、`--quiet`、`--type auto\|webpage\|chat\|news` | 经 `KnowledgeApplication.archive_cli_input()`；真实网络/Provider 仅后续显式 live 流程 |
| `archive-text TEXT` | `--title`、`--format table\|json` | 字面纯文本 → `KnowledgeApplication.archive_text()`；Q0/Q1′ 未完成时明确 processing，core commit 后输出条目定位，Q2 状态不伪装为完整成功 |
| `search QUERY` | `--strategy auto\|bm25\|vector\|hybrid`、`--limit`、`--format table\|json\|markdown` | 输出公开实际执行策略及 `success/no_hits/invalid/error/degraded`；JSON 含 `query/status/strategy/total/issues/results` |
| `show [ID_OR_URL]` | `--url`、`--raw` | ID 或 URL 至少提供一个；`--raw` 经 `MarkdownStore.load()` 做 Vault containment 校验，不直接读取 DB 中的任意路径 |
| `list` | `--tag`、`--sort time\|title\|id`、`--desc`、`--limit` | 从 SQLite 查询并以 Rich 表格输出；排序/tie 与非法 limit 仍需目标合同 |
| `tags` | `--limit 1..200`、`--format table\|json` | 有上界的只读 SQLite 标签计数；当前不提供跨 Markdown/SQLite 的标签写入 |
| `related KNOWLEDGE_ID` | `--limit 1..20`、`--format table\|json` | 已有文档向量索引的近邻查询；通过严格只读索引入口，不构造 Provider，索引/向量缺失显式 `degraded` |
| `inspect` | 无 | 只读输出 runtime inspection 与 plan；任何 readiness 都可用 |
| `setup` / `repair` | `--apply`、`--confirm PLAN_ID`、`--allow-network` | 默认仅展示计划；只有精确确认的计划才可执行，网络探测另需明确许可 |
| `embedding inspect` | 无 | 只读输出 generation binding 与独立 manual-rebuild policy；不创建 Provider、stage 或 lease |
| `embedding rebuild` | `--force`、`--apply --confirm PLAN_ID --allow-network` | 只经公开 Kernel façade 执行；当前 Config/policy drift、replay 与并发都 fail-closed，Provider 仅在 writer lease 外运行 |
| `embedding inspect-v2` | 无 | 只读 v2 inspection，展示当前预算期间与 legacy ledger 升级影响 |
| `embedding rebuild-v2` | `--force`、`--apply --confirm PLAN_ID --allow-network` | 默认只展示网络计划；执行须精确确认并授权网络 |
| `embedding operation-v2 OPERATION_ID` | 原 operation 标识 | 只读投影封存证明、pointer 与账本所支持的 effective state，不对齐持久状态 |
| `embedding publish-v2 OPERATION_ID` | `--apply --confirm PLAN_ID` | 默认只展示 publish-only/完成对齐计划；不接受 force 或 network 选项，零 Provider、零新预留 |
| `outcome create-topic/get/list` | Topic 标题、问题或分页参数 | `pkv_kernel.outcomes` 的 Provider-free Topic 公开投影；`get` 仅 revalidate，不隐式写入 |
| `outcome attach-evidence/detach-evidence/suggest-evidence` | canonical `pkv://` locator、BM25 query | 只允许 entry/chunk/relation locator；绑定 captured digest，删除不推断替代来源；suggest 只走 BM25 |
| `outcome create-claim/revise-claim/link-evidence/unlink-evidence` | statement、state、revision、support/counter | 显式维护 Claim 与 Evidence 边；没有有效证据的 Claim 对外显示 `insufficient` |
| `outcome render/approve/return` | `topic_id` | 旧 v1 入口保留；approve/return 已弃用但仍按既有语义执行，不具有 v2 的调用方版本确认保证 |
| `config show/get` | `get KEY` | 读取并遮罩敏感配置；默认自动化只允许只读子命令 |
| `config set KEY VALUE` | YAML 点号键 | 写唯一用户配置；不属于自动化/AI 运行边界 |
| `stats` | 无 | 汇总条目、来源、标签与存储大小 |

全局选项由 `src/main.py` 定义：`--verbose`、`--debug`、`--version`。

## W4 Embedding v2 与继续发布（已完成范围验收）

当前本地兼容 build 为 SDK `0.10.0` / API `1.0.0`，全部 v1 入口继续保留。
Embedding v2 按冻结合同 §6 作为兼容增量实现，`kernel.embedding-rebuild.v2` 已声明；公开
链路已取得组件证据，W4 范围验收完成。W5 的 API-major-2 和旧 Outcome 拒绝尚未推进。当前证据统一见
[W4 主记录](../../docs/overview/W4-Embedding继续发布与预算恢复实施记录-2026-10.md)。

四个新叶子均属于 lifecycle，不套普通业务 READY 门禁。Click 先校验形状、必填参数和
未知选项；错误 exit 2，不读取 runtime。operation ID 必须满足
`embedding-rebuild-[0-9a-f]{32}`，确认 ID 是 64 位小写十六进制。跨进程执行重新检查当前
根和材料并重算 plan，精确比较提交的 ID 后才由 façade 创建确认；不以 operation ID 单独授权。

| 叶子 | 固定 envelope.operation / 对应 MCP Tool |
| --- | --- |
| `inspect-v2` | `inspect_embedding_rebuild_v2` |
| `rebuild-v2` 默认 / `--apply` | `plan_embedding_rebuild_v2` / `execute_embedding_rebuild_v2` |
| `operation-v2` | `inspect_embedding_operation_v2` |
| `publish-v2` 默认 / `--apply` | `plan_embedding_publication_v2` / `execute_embedding_publication_v2` |

新叶子共用 `pkv.lifecycle-response.v2`：成功 JSON 到 stdout、exit 0；业务错误 JSON 到
stderr、exit 1。成功 result 的 completion-pending 警告必须保留在 issues，不能清空后称完全
收口。A 争锁返回 `write_busy`；完整 C 封存后 D 争锁返回 `embedding_publication_pending`，
details 包含原 operation、`effective_state=awaiting_publish`、`provider_effect=completed`
和 `publish_only_required=true`。v1 rebuild 也透传此 code 与原顶层 details，v1 不新增 lookup/
publish；操作者转用 v2 inspect operation → plan → confirm → publish，不能重跑 Provider。

只读入口不迁移 ledger。legacy 1/2 的 v1 mutation 返回 `embedding_ledger_upgrade_required`；
v2 的升级影响绑定 plan，确认后在 A 的 lease 内先原子升级 ledger 3。封存 proof 与 grant 2
允许新进程验证原产物；publish-only 不申请新的网络预算，旧 grant 到期不能授权续写。

## W3 Outcome v2 与兼容期边界

W3 的历史兼容 build 为 SDK `0.9.0` / API `1.0.0`，当时仅新增
`kernel.traceable-outcomes.v2` 并保留全部 v1 capability。当前 W4 版本与 Embedding 增量见上节，
W5 API-major-2 仍未切换。Outcome 实现与 W3 最终同树验收状态见
[W3 主记录](../../docs/overview/W3-Outcome一致性与版本审批实施记录-2026-10.md)，
公开字段以 [v2 接口冻结合同](../../docs/specs/interfaces/生命周期一致性v2接口冻结合同-2026-10.md) 为准。

以下五个叶子只调用 `pkv_kernel.outcomes` 的对应 `*_v2` 函数；包括只读叶子在内均要求
`READY`，不扩大 W2 的 `DEGRADED` 允许列表。get/brief 零写、零 lease、零 Provider；
render/review 的事务与 writer lease 由 Kernel/Application 执行。

| CLI | 主要参数 | 固定 envelope.operation / 对应 MCP Tool |
| --- | --- | --- |
| `outcome get-v2 TOPIC_ID` | Topic 标识 | `get_outcome_v2` |
| `outcome brief-v2` | `--topic-id ID` / `--outcome-id ID`，必须且只能提供一个 | `get_outcome_research_brief_v2` |
| `outcome render-v2 TOPIC_ID` | Topic 标识 | `render_outcome_research_brief_v2` |
| `outcome approve-v2 OUTCOME_ID` | 必填 `--expected-revision N --content-sha256 H --expected-input-revision N` | `approve_outcome_research_brief_v2` |
| `outcome return-v2 OUTCOME_ID` | 上述四元组，另必填 `--reason-code needs_revision\|insufficient_evidence\|other` | `return_outcome_research_brief_v2` |

标识满足 `[a-z][a-z0-9_-]{0,95}`；SHA256 是 64 位小写十六进制；两项 revision 为
`1..2**63-1` 的整数。Click 先完成形状、selector、必填项和未知选项校验，错误 exit 2，
不加载 runtime。review 原样传递用户已展示并确认的四元组，不读取 latest 自动补齐；
精确同动作重放复用 receipt，旧确认、退回后批准或不同退回原因重放被拒绝。

五个 v2 命令与 MCP 共用封闭响应：
`{schema:"pkv.lifecycle-response.v2",status,terminal,operation,result,issues}`。
`status` 与 `terminal` 为 `success` 或 `error`；成功 result 是公开 DTO 或合法 lookup 的
null，issues 为空；失败 result 为 null，issues 保留稳定 code/message/stage/recoverable。
operation 固定取上表名称，不含用户标识。成功 JSON 写 stdout、exit 0；业务和 readiness
错误 JSON 写 stderr、exit 1，不回显 source body、私有路径、凭据或异常原文。

旧 `outcome approve/return TOPIC_ID` 保留签名和成功语义。成功时 stdout 仍是旧 JSON，
stderr 提示替代 `approve-v2/return-v2` 及 API major 2；业务失败仍输出单个 stderr JSON，
额外带固定 `deprecation={replacement,removal_api_major:2}`。SDK 同时发出
`DeprecationWarning`。因此 R-04 旧无版本审批仍是 `known_gap`，只由 W5 的旧入口拒绝
和 major 切换关闭；不能把 v2 通过写成旧 v1 已获得版本绑定保证。

组件黑盒 `tests/blackbox/test_w3_outcome_cli_blackbox.py` 的
`test_outcome_cli_v2_exact_review_replay_and_v1_deprecation` 覆盖精确审批、旧确认拒绝、
退回/幂等 receipt、新进程读取及旧入口弃用；其余节点覆盖解析优先和未 READY envelope。
该文件与 W3 MCP、既有 Outcome CLI/MCP 四文件合跑为 **21 passed / 230.03s，exit 0**，
见 [组件日志](../../.data-test/w3-adapter-20261025-full/pytest.log)。这次运行包含启动错误
stage 透传微调前后的进程，属于组件证据；最终冻结同树结果只按上述 W3 主记录登记。
该计数不能替代默认全量、MCP coverage 或 Phase C 门禁，也不改变 release hold。

## 命令树解析与运行时门禁（W2 已实现）

W2（2026-10-24 开始）已修复 `outcome` Group 提前执行 runtime 门禁的问题：
`src/main.py` 递归只包装叶子，执行时从 Click Context 链取得完整命令路径，解析成功后
才按显式 policy 执行门禁。未知叶子在读取 Config 前拒绝；共享 Command 的别名按本次
调用路径判权。Outcome 只读命令仍要求 `READY`，没有扩大 `DEGRADED` 权限。
原 W1 的 16 项解析回归已全部通过，本轮新增覆盖及证据见下表。修复次序见
[生命周期一致性修复设计与分阶段计划](../../docs/overview/生命周期一致性修复设计与分阶段计划-2026-10.md)，
本轮主记录见 [W2 身份队列与 CLI 门禁修复记录](../../docs/overview/W2-身份队列与CLI门禁修复记录-2026-10.md)。
这里的 W2 完成结论仅适用于命令解析与门禁，不能替代上节 W3 的 Outcome 版本审批验收。

门禁须按完整命令路径绑定到叶子，不能假定 root 的直接 child 都是可执行叶子：

1. 任意深度的 Group callback 只负责命令分派和解析上下文，不加载用户 Config、不 inspect
   runtime、不装配 Application、不取得 lease，也不配置或打开文件日志。增加一个 Group
   必须检查其全部后代；禁止通过包裹 Group callback 给后代统一加业务门禁。
2. Click 完成所选叶子的 eager option、必填参数、参数类型与未知选项校验后，才允许叶子
   callback 检查一次已捕获的 Config snapshot，并将同一 snapshot 交给业务操作。
3. 任意命令深度的帮助和参数错误路径都不得触碰 runtime。已注册位置的 `--version`
   正常输出版本；未注册位置仍由 Click 返回用法错误，同样不得读取 runtime。本要求不
   自动给每个子命令增加 `--version` 选项。
4. 每个叶子须显式登记只读、mutation、lifecycle 或 config 类别以及允许的 readiness。
   未分类的新叶子在业务执行时 fail-closed，但不能阻断其帮助或参数校验。只读分类并不
   自动授予 `DEGRADED` 访问权；扩展允许列表必须有对应数据可读性证据。

| 叶子类别 | 命令路径 | 解析成功后的门禁 |
| --- | --- | --- |
| 只读、已有 degraded 合同 | `search`、`show`、`list`、`tags`、`related`、`stats` | 允许 `READY` / `DEGRADED`；不恢复、不迁移、不隐式写入。 |
| Outcome 只读 | `outcome get`、`outcome list`、`outcome suggest-evidence`，以及 W3 的 `get-v2/brief-v2` | 保留 `READY` 要求；门禁只在叶子执行，不由父 Group 判断。 |
| 业务 mutation | `archive`、`archive-text`，以及 `outcome` 的 create、attach/detach、revise、link/unlink、render、approve、return 叶子和 W3 的 `render-v2/approve-v2/return-v2` | 必须 `READY`；writer lease 与领域校验仍由既有 Application/Kernel 合同执行。 |
| Lifecycle | `inspect`、`setup`、`repair`、`embedding inspect/rebuild` 及 `embedding inspect-v2/rebuild-v2/operation-v2/publish-v2` | 不套普通业务 READY 门禁；各叶子执行自己的 inspect/plan/confirm/execute 合同。 |
| Config | `config show`、`config get`、`config set` | 只在成功解析后进入配置合同；`config set` 仍不属于默认自动化授权范围。 |

### CLI 修复验收用例

以下 ID 已绑定 W2 的持久测试节点与实际运行证据。新增命令仍必须纳入同一命令树
枚举与分类检查；不能只对固定几个根命令抽样后宣称任意深度覆盖。表内函数级 nodeid
包含其全部参数化实例；完整参数 nodeid、命令与结果均保存在对应日志中。

| 用例 ID | 设计要求与场景 | 必须观察的结果 | 当前状态 / 运行证据 |
| --- | --- | --- | --- |
| `CLI-01` | 根、Group 与叶子帮助；已支持和不支持位置的版本选项；至少覆盖 `outcome --help`、`outcome get --help` 与更深层合成命令树 | 注册帮助/版本 exit 0；不支持选项 exit 2；Config loader、inspect、Application、lease、文件日志均无调用；被测数据根无产品写入。 | `passed`：`tests/unit/test_w2_cli_leaf_gate.py::test_deep_help_and_validation_never_load_even_a_broken_config`；真实子进程例 `tests/blackbox/test_w1_cli_parse_regressions.py::test_outcome_parse_precedes_runtime_gate[unready-absent-root-CLI-01-read-leaf-help]`，并覆盖三状态全部 `CLI-01-*` 参数。见 [focused 日志](../../.data-test/w2-cli-20261024-focused/pytest.log)。 |
| `CLI-02` | 任意深度缺必填参数、非法选项、非法参数类型，包含 `outcome get`、`outcome list --limit invalid` | Click exit 2 和对应参数错误优先；没有 readiness envelope，也不触碰 runtime。 | `passed`：同一深树 unit 节点覆盖错误类型、未知命令及缺少子命令；`tests/blackbox/test_w1_cli_parse_regressions.py::test_outcome_parse_precedes_runtime_gate[unready-absent-root-CLI-02-missing-required-argument]` 及三状态全部 `CLI-02-*` 参数通过。见 [focused 日志](../../.data-test/w2-cli-20261024-focused/pytest.log)。 |
| `CLI-03` | 叶子成功解析后的类别/readiness 判定，含只读、mutation、lifecycle、config 与未分类叶子 | 门禁只在实际叶子执行前发生；同一 snapshot；不因 Group 深度漏门禁或重复装配；只读/degraded 与 mutation/READY 允许列表保持明确。 | `passed`：`tests/unit/test_w2_cli_leaf_gate.py::test_deep_leaf_uses_its_policy_and_one_config_snapshot`、`test_unclassified_leaf_fails_closed_after_successful_parsing`、`test_shared_command_resolves_each_invocation_path_before_degraded_access`、`test_special_leaves_keep_their_own_contract_without_business_ready_gate`、`test_all_product_leaves_have_explicit_policies_and_degraded_reads_stay_narrow`；另有 `tests/unit/test_cli_runtime_gate.py` 的只读允许与 mutation/Outcome 拒绝实例。见 [focused 日志](../../.data-test/w2-cli-20261024-focused/pytest.log)。 |
| `CLI-04` | 将 `CLI-01` / `CLI-02` 的 `outcome` 路径分别运行于未 READY 根、合成损坏配置、READY 根；另用合法调用验证 `CLI-03` | 三状态中的帮助/解析错误一致且零 runtime；合法业务调用才返回各自 readiness/配置错误或进入业务。真实 CLI 子进程核验 exit code、stdout/stderr 与新进程可观察状态。 | `passed`（损坏配置的黑盒范围为 runtime snapshot）：`tests/blackbox/test_w1_cli_parse_regressions.py::test_outcome_parse_precedes_runtime_gate` 的 `unready-absent-root`、`damaged-runtime-snapshot`、`ready-control` 三组，以及 `tests/blackbox/test_w1_cli_parse_regressions.py::test_valid_outcome_leaf_observes_runtime_readiness` 的三状态 read/mutation 实例。坏用户 Config loader 仅有 unit 证据；边界见下文。见 [focused 日志](../../.data-test/w2-cli-20261024-focused/pytest.log)。 |

“合成损坏配置”只能在本次 `.data-test` 内构造。Unit 可用会抛异常的 Config loader
证明帮助/校验不调用它；黑盒仍须先满足离线入口的路径与凭据隔离，不能用真实用户配置，
也不能放宽 G0。若测试入口在进入产品 parser 前已解析并拒绝该 fixture，应记录为
fixture/入口阻塞并补受控测试方案，不能把入口拒绝当作 `CLI-04` 通过。

W2 黑盒在隔离根构造损坏的 `<data-root>/config/local.yaml`，关闭 synthetic READY
自动建快照开关；公开 `inspect` 先证明 `runtime_snapshot_invalid` 与 `repair_required`，
随后真实 `offline_entrypoint.py cli` 子进程验证帮助、参数错误以及合法叶子调用。比较的是
fixture 建立后的数据根指纹；离线入口自身的隔离初始化不计为产品解析行为。
坏用户配置 YAML 会被现有 G0 入口在产品 parser 前解析拒绝，因此未把它作为黑盒通过证据，
也未改 G0。对应 unit 使用会抛错的 Config loader，证明帮助和参数校验不调用该 loader；
该结果不能扩大为坏用户配置的真实子进程结论。

W2 实际运行均使用 `scripts/run-test.ps1 -Direct` 与独立 `.data-test` 根，未使用真实用户
配置、Vault 或 Provider：

- [focused 日志](../../.data-test/w2-cli-20261024-focused/pytest.log)：运行
  `tests/unit/test_w2_cli_leaf_gate.py`、`tests/unit/test_cli_runtime_gate.py`、
  `tests/blackbox/test_w1_cli_parse_regressions.py`；**80 passed / 168.69s，exit 0**。
  其中新叶子门禁 unit 32 项、既有及补充 readiness unit 18 项、三状态黑盒 30 项。
- [既有 CLI 回归日志](../../.data-test/w2-cli-20261024-regression/pytest.log)：运行
  `tests/unit/test_cli_commands.py`、`tests/integration/test_cli_inprocess.py`、
  `tests/blackbox/test_cli_basic.py`、`tests/blackbox/test_cli_blackbox.py`、
  `tests/blackbox/test_outcome_cli_blackbox.py`、`tests/blackbox/test_r4_cli_fullflow.py`；
  **282 passed / 336.29s，exit 0**。这是兼容性回归证据，不能代替上述 case 逐项验收，
  也不作为 W3 审批修复完成的证据。

## 实现索引

### `commands.py`

- `cli()`：内部命令注册组；原生模块执行与常规启动均由 `src.main.LazyCLIGroup` 分派，不能直接执行注册组来绕过门禁。
- `archive()` / `archive_text()`：验证 CLI 输入、调用 application 异步归档并渲染结果。
- `search()`：调用 application 搜索、输出 table/JSON/Markdown。
- `show()` / `list_entries()` / `tags()` / `related()` / `stats()`：调用 application
  领域操作并渲染可观察结果。
- `inspect()` / `setup()` / `repair()`：inspect → plan → explicit-confirmation 生命周期适配。
- `embedding()`：仅投影 `pkv_kernel.embedding` 的 inspect → plan → confirmation → execute 合同；不导入 Provider、Store 或 lease。
- `outcome_*()`：只投影 `pkv_kernel.outcomes` 的 Topic/Evidence/Claim/research brief 合同；不直接组合 Store、Layout、lease 或 Provider。
- `config_show()` / `config_get()` / `config_set()`：唯一用户配置读写与敏感值遮罩。
- `_render_search_table()`、`_render_list_table()`、`_render_entry_panel()`：命令内 Rich 渲染辅助函数。

### `ui.py`

公开辅助 API：

- `ProgressHandle`
- `show_progress()`
- `format_table()`
- `show_panel()`
- `confirm_action()`

### `formatters.py`

公开格式化 API：

- `format_as_json()`
- `format_as_markdown()`
- `format_search_results()`
- `format_entry_detail()`

不要引用已不存在的 `ProgressTracker`、`TableFormatter`、`PanelFormatter`、`ConfirmDialog`、`OutputFormatter` 或 `_register_commands`。

## 测试分层

### Unit

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\cli-unit -Command @("python", "-m", "pytest", "tests/unit/test_cli_commands.py", "tests/unit/test_cli_ui.py", "tests/unit/test_cli_formatters.py", "-m", "not network and not manual", "-v")
```

主责：参数分支、纯格式化、稳定结构和错误处理。避免把完整中文文案或 Rich 内部结构作为唯一 oracle。

### In-process integration

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\cli-integration -Command @("python", "-m", "pytest", "tests/integration/test_cli_inprocess.py", "-m", "not network and not manual", "-v")
```

当前主责：临时 SQLite/Vault 上的 BM25 搜索、`list`→`show` 协作，以及临时项目根内的配置读取/写入。它使用 `CliRunner`，不声称覆盖 OS 子进程边界或真实归档工作流。

### Subprocess blackbox

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\cli-blackbox -Command @("python", "-m", "pytest", "tests/blackbox/test_cli_basic.py", "tests/blackbox/test_cli_blackbox.py", "tests/blackbox/test_r4_cli_fullflow.py", "tests/blackbox/test_embedding_rebuild_cli_blackbox.py", "tests/blackbox/test_outcome_cli_blackbox.py", "-m", "not network and not manual", "-v")
```

标准入口黑盒经 `tests/offline_entrypoint.py cli` 启动真实子进程；原生模块入口回归经该受保护入口的 `python -m src.cli.commands` 路径，由 `runpy` 实际执行模块，不先重映射到标准入口。两者主责退出码、stdout/stderr、JSON 边界和临时存储副作用；不使用进程内 `CliRunner` 代替协议边界。

W1–W3 评审补充要求两种入口具有相同的 readiness、解析优先与 v2 envelope 行为；验收见
[CodeReview 修复记录](../../docs/overview/W1-W3-CodeReview修复验收记录-2026-10.md)。历史 W2
黑盒只覆盖标准入口，其计数不能作为原生模块入口的既有验证证据。

## TestCase 设计注意事项

- `search` 的 `strategy` 字段表示实际执行策略；适配器必须逐一保留五态和稳定 issue，只有 `invalid` / `error` 使用非零退出码，`degraded` 必须显式展示警告。
- `show --raw` 的 TestCase 应继续用外部 sentinel 锁定 `MarkdownStore.load()` 的 canonical containment，防止后续回归成任意路径读取。
- `archive` 的真实网页、真实 Provider 和费用不进入默认回归；离线 integration 只能计为 adapter seam，不能冒充真实工作流 E2E。
- `archive-text`、`tags` 和 `related` 必须有真实离线子进程覆盖：至少验证 `archive-text → tags` 串联、无向量索引的明确降级、固定本地向量的自排除近邻结果，以及 `related` 前后向量树零改写。
- R4 public-process 用例还必须验证真实 `archive-text` 后的 Q0/Q1′/Q2 durable ledger、
  settled reservation/provider-reported usage、READY generation，以及关闭原 CLI 后由新 CLI
  执行 vector/hybrid search 命中同一条目；不得用 `CliRunner`、Application patch 或旧 flat
  vector 目录伪造该闭环。
- P1-B public-process 用例必须从真实 CLI 子进程完成 Topic → Evidence → Claim → render → review，并由新进程读取同一 durable revision；无 source body、绝对路径或 locator 中的 URL 凭据可出现在 public envelope。它不调用 Provider、Embedding 或 QueryRouter。
- `config set`、生产数据查询和开发 Vault 重建不作为 pytest fixture 或完成定义。
- CLI help/Click validation 与生命周期命令必须在未 READY 根仍可执行；stats/search 等业务命令的黑盒 fixture 必须先写入匹配的无密钥 runtime snapshot，不能以隐式 bootstrap 绕过门禁。若归档已留下 `DEGRADED` journal，黑盒应验证读取仍可用、后续写入仍被 readiness 门禁拒绝。
- 命令树门禁修复必须完成 `CLI-01`–`CLI-04`；记录 requirement → case ID → pytest nodeid → 当前状态 → 同一工作树运行证据。新增表格、历史总通过数或只有 READY fixture 的 Outcome 成功闭环都不能代替这四项验收。

## 相关文档

- [API 文档](../../docs/operations/API文档.md)
- [使用手册](../../docs/operations/使用手册.md)
- [Retrieval 检索引擎规范](../../docs/specs/interfaces/Retrieval检索引擎规范.md)
- [TestCase 设计审查与分阶段规划](../../docs/operations/testing/Review-TestCase设计审查与分阶段规划-2026-07-30.md)

**当前源码/SDK 兼容 build**：`0.10.0`；**Kernel API**：`1.0.0`

**最后核对**：2026-10-26（W4 四个 Embedding v2 叶子已完成范围验收；W1–W3 历史运行记录保留）
