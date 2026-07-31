# Review TestCase 设计审查与分阶段规划

> 日期：2026-07-30
> 审查基线：`master@e771ba0a671e2046ced85d77eac8f9eaa0431122`
> 当前分支：`codex/review-testcase-repair`
> 当前离线复验：2026-07-31 09:35:13 +08:00，S4c 全部通过
> 范围：TestCase 设计、测试代码、fixture、离线评测、测试门禁与活跃测试文档
> 明确排除：产品功能实现、真实数据测试流程、开发 Vault 重建、任何生产 `.data` 操作

## 1. 范围与安全护栏

本审查关注“测试能否杀死错误实现”，而不是测试文件数量。每个关键合同都应有唯一主责层、可重复 fixture、稳定 oracle、故障注入与明确完成定义。

本批工作遵守以下硬边界：

- 不读取、枚举、复制或修改生产/开发 `.data`；不读取 `config/local.yaml`。
- 默认测试只使用 `.data-test`、`tmp_path`、合成 HTML/YAML/SQLite、确定性向量和 fake transport。
- 所有 pytest 命令只能经 `scripts/run-test.ps1` 执行；不得用测试模块内的 `pytest.main()` 绕过包装器。
- `network`、`manual` 与真实 Provider 用例不属于本批验证或完成定义。
- 不修改 `src/**/*.py` 产品实现；正确 TestCase 暴露出的产品缺陷进入独立后续窗口。
- 本次续作不新增提交、不推送；当前 HEAD 已包含任务内早期提交 `0687f74`，保留用户对其保留、拆分或回退的处置权。

状态词统一为：

| 状态 | 含义 |
|---|---|
| `DONE_VERIFIED` | 实现已完成，且有与当前改动相关的可追溯测试证据 |
| `IMPLEMENTED_PENDING_VERIFY` | 已修改，但当前 dirty tree 尚未完成所需复验 |
| `CHARACTERIZATION_ONLY` | 仅记录现状，不代表目标产品合同 |
| `BLOCKED_CONTRACT_DECISION` | 继续写最终 oracle 前必须先决定产品语义 |
| `PLANNED` | 尚未实施 |

## 2. 可追溯验证快照

| 快照 | 代码状态 | 命令/来源 | 结果 | 证据用途 |
|---|---|---|---|---|
| S0 | `master@e771ba0` | 用户提供 | `1471 passed, 1 skipped` | 仅作进入本任务前基线；本轮未复验 |
| S1 | 当前 HEAD `0687f74` 的提交前检查点 | 旧规划记录 | 曾记录 `1514 passed, 1 skipped, 10 deselected`；MCP `340 passed, 5 deselected, 97.04%` | 仅作早期提交证据；不能代表当前 dirty tree |
| S2 | `0687f74` 后的较早 dirty-tree 检查点（未保留时间/指纹/日志） | 包装器定向 `--collect-only` | `422` 个目标节点中 `417 collected, 5 deselected` | 仅证明当时可收集；可追溯性不足，不作为当前 DoD |
| S3 | 后续 dirty-tree 中间态（未保留时间/指纹/日志） | 包装器运行 6 个定向模块，排除 `network` | `100 passed, 4 deselected, 3 failed` | 3 个失败定位为 Windows asyncio socketpair 与早期 raw-socket guard 冲突；不可替代修正后复验 |
| S4a | `0687f74` 后的 dirty-tree 检查点；晚于 S3、早于最新 root Config/fixture/CI 修复 | 包装器定向运行 9 个模块，排除 `network` | `137 passed, 5 deselected, 1 failed`；唯一失败是嵌套包装器的 Conda 激活瞬时失败，其余定向用例通过 | 仅证明当时大部分定向合同；不代表最新 dirty tree |
| S4b | 与 S4a 同期 | 用新 `.data-test` 根单独重跑失败的 wrapper TestCase | `1 passed` | 支持“瞬时环境失败”判断，但不能替代整组复验 |
| S4c | `HEAD 0687f744535d2ab94db248103ca1098698c72ef7`；46 条 status；status SHA-256 `a18ac4c38a4bde2a1ce7d2a77b6ecc9d0453e954ba9d226762f2225040d1b378`；2026-07-31 09:35:13 +08:00 | 全部经 `scripts/run-test.ps1`；定向→unit→integration→E2E→blackbox→full default→MCP coverage | 定向 `149 passed, 5 deselected`；unit `1279 passed, 1 skipped`；integration `178 passed`；E2E `22 passed, 9 deselected`；blackbox `77 passed`；全量 `1560 passed, 1 skipped, 9 deselected`；MCP `361 passed, 1205 deselected, 96.88%` | 当前 dirty tree 的默认离线 DoD 已满足；过程中发现并修复空 `PYTEST_ADDOPTS` 被 Conda 丢弃、Config singleton 跨测试泄漏两项测试基础设施缺口 |

因此，当前 dirty tree 在仓库默认离线 lane 中已经全量绿色，MCP 覆盖率超过 95%。所有测试数据根均在当前工作树的 `.data-test` 下；未执行 live/network。S4c 不代表后续真实数据流程、Linux CI 运行时矩阵或 P0 产品语义缺口已经完成。

## 3. 模块—契约—覆盖方式矩阵

| 模块 | 当前主责层与 oracle | 主要 fixture/故障注入 | 当前强项 | 主要残余风险 |
|---|---|---|---|---|
| Storage | unit + 临时 SQLite/Markdown/Vector integration；状态、行数、文件与向量 ID | `tmp_path`、阶段异常、真实临时库 | Vector 删除、Markdown round-trip、基础 CRUD | migration fail-open、Vault containment、FTS/rechunk、跨存储一致性 |
| Retrieval | unit + 确定性临时索引；结果顺序、分数与 metadata | fake embedder、固定向量、临时 SQLite | QueryRouter 分支唯一调用、chunk 映射 | outage 与 no-hit 混同、RRF 缺精确 oracle、过滤时序 |
| Workflow | step unit + fake-step integration；state/data/errors/logs | 显式 base Config、步骤异常 | 基础编排和存储接线 | YAML `on_error`、`trigger_rules`、processor 选择未被真实配置驱动 |
| Processors | parser unit；Entry 字段 | 合成 HTML/文本、mock 请求 | 各处理器局部解析 | 缺跨处理器 conformance、selector drift 与 YAML→processor seam |
| AI | mock provider unit | 固定 response/error | 基础 happy/error path | batch cardinality、顺序、NaN/零向量/维度、边界长度 |
| Relation | service unit/integration + YAML cases；节点/边集合 | 每 case 新鲜 DB/Vault、合成拓扑 | cycle/reverse/disconnected 与固定回归集 | tie、截断无悬空边、分页/filter/rollback/concurrency |
| MCP | handler unit + FastMCP integration + stdio blackbox + fixed eval | 临时 store、offline child、固定 taskset/scenario | 三层边界、严格 schema、评测 allowlist 与 evidence 合同 | provider wiring、HTTP auth 真接线、SSRF DNS/redirect、stdio 调用矩阵 |
| CLI | command unit + `CliRunner` integration + 子进程 blackbox | 显式 base Config、临时 store、offline child | JSON schema、退出码、search→show 真实 ID | limit/order、debug/verbose 行为、重复 help/stats、离线 archive seam |
| GUI | ViewModel unit + pytest-qt；公开 model/signal/state | mock stores、临时 Config、隔离 QSettings | `EntryTableModel` 已改测生产类；基础窗口/对话流 | 会话切换/双发/取消/保存原子性、真实 qasync wiring、QSettings 全局恢复 |
| Scripts/Migration | PowerShell 行为测试 + migration unit | 临时仓库/DB、伪危险路径、失败注入 | wrapper 路径/参数/脱敏覆盖增强 | direct migration 脚本仍会默认读 local config，当前 lane 阻断；另有 junction/硬链接平台矩阵、备份失败后继续、损坏库冒充 fresh |
| Test infra/eval | pytest 元测试 + scorer/runner unit/integration | env scrub、路径 containment、fixed proposal/taskset | parent base-only Config、bool/int/float 严格性、只读 Tool allowlist、早期校验 | 直接构造 `Config()` 的产品入口仍须显式 patch；任意 `tests/` 外 Direct pytest 与第三方 plugin autoload 发生在安全 hook 之前，不在当前保证内；网络守卫不是 OS 沙箱；nested wildcard |

## 4. Fixture 与隔离所有权矩阵

| 状态/资源 | 唯一所有者 | 默认策略 | 已知限制/后续动作 |
|---|---|---|---|
| `DATA_DIR/DB_PATH/VAULT_DIR/VECTOR_DIR/LOG_DIR/TMP_DIR` | `run-test.ps1` + `tests/offline_runtime.py` | 必须位于显式测试根；拒绝项目根、生产 `.data` 及 sibling path | 完成 Windows symlink/junction 行为复验 |
| Config singleton | 根 `tests/conftest.py` + 触达直接构造器的模块 fixture | repository `tests/` 默认 lane 中，launcher 路径先校验，再在 collection 前安装单一 base-only Config，结束恢复；直接 `Config()` 入口仍显式 patch | `tests/` 外显式 pytest target、plugin autoload、direct migration/运维脚本尚未接入同等边界；live lane 独立 |
| Provider secret/proxy | offline child env builder | 大小写无关清理已知和启发式 secret；清除 proxy | 仍是 denylist；P1 评估最小环境 allowlist |
| 子进程入口 | `tests/offline_entrypoint.py` | 校验 target、project-root sentinel、路径 containment 后才创建目录/导入产品入口 | Windows asyncio 要保留 raw socket；需 OS/CI 级网络隔离补强 |
| Parent pytest 网络 | 根 `tests/conftest.py` | collection 前清理 live/secret/proxy，阻断 DNS；raw socket 仅允许 loopback/AF_UNIX，默认跳过 network | 不覆盖预连接 socket、孙进程、plugin autoload 或 OS 层网络；process-wide patch 不支持嵌入式 `pytest.main()`，禁止过度宣称 |
| SQLite/Markdown/Vector | 各测试 fixture | 新鲜临时根，禁止共享生产路径 | 跨存储目标合同尚待决策 |
| Relation YAML case | 参数化 case 自身 | 每 case 新库/新 Vault | 后续加入插入顺序随机化与 tie oracle |
| Qt/QSettings | GUI 模块 fixture | 临时 INI、逐测试清理 | default format/path 的完整恢复仍待补 |
| MCP fixed eval | 固定 taskset + deterministic scenario | gold/proposal 分离、六个只读 Tool allowlist | allowlist 变更需先审查同名 handler 副作用 |
| live/network | 后续独立流程 | 默认选择排除且不运行；双 sentinel + marker 才可进入现有 opt-in fixture | 不作为本任务 DoD，不在本任务配置或执行真实 Provider |

## 5. 设计审查结论

### 5.1 需要阻止“测试锁定错误实现”的 P0 合同

1. Migration：损坏/查询失败与未初始化库都可能映射成 `0.0.0`；自动备份失败后仍可能继续迁移。现有/历史 characterization 不能作为目标合同。
2. Workflow：真实 YAML 的 `on_error`、`trigger_rules`/`condition` 和 processor 选择未被运行时忠实执行；fake-step happy path 无法发现配置漂移。
3. Cross-store：Markdown、SQLite/FTS/chunk、Vector 的写入/删除没有明确事务、补偿或 repair-queue 语义；禁止继续用“部分提交也算成功”的断言伪装绿色。
4. Retrieval：Vector/Hybrid/Router 把异常吞成 `[]`，上层无法区分无结果与 outage；RRF 缺精确排序、tie、去重和 metadata merge oracle。
5. Security：Vault canonical containment、MCP HTTP Bearer 真 transport、SSRF DNS/redirect/子资源重校验缺失。
6. MCP provider wiring：`OpenAIClient(config)` 形态可能把 Config 当 API key；mock 调用形态测试会锁定错误接线。
7. GUI Chat：流中切会话、双重发送、停止、provider/save 失败缺原子状态矩阵，可能跨会话污染或留下孤立 user turn。

### 5.2 P1/P2 设计问题

- Prompt、SQLite、BM25、migration、CLI help/stats、字面 SSRF 在多层重复；应指定行为 owner，较高层只留协议 sentinel。
- 脆弱断言集中在完整中文文案、魔法数量、私有方法、默认排序/tie、机器时延和未设 seed 的随机向量。
- MCP stdio 的“注册存在”多于“逐 Tool 协议调用”；SSRF 只断言 `success=false` 可能被网络守卫造成 false-green。
- GUI 仍有私有属性/控件存在性断言，缺真实 signal sequence；Relation 缺 filter/pagination/FK/rollback/concurrent upsert。
- 测试文档数量和路径会漂移；本批已修活跃文档的两处层级重命名，但历史报告保持历史原文。

## 6. 本轮 TestCase 修复台账

| 项目 | 状态 | 本轮结果 | 尚需证据/限制 |
|---|---|---|---|
| MCP scorer 严格类型比较 | `DONE_VERIFIED`（S4c） | bool/int、`1`/`1.0`、set/frozenset、empty `contains_all` 负例 | nested wildcard 留 P2 |
| MCP runner 固定只读 allowlist/先验校验 | `DONE_VERIFIED`（S4c） | 非六个只读 Tool 和坏 task/proposal 在 scenario 构造前拒绝 | 同名 handler 语义变更需人工审查 |
| Offline env scrub、路径 containment 与 parent Config | `DONE_VERIFIED`（S4c 默认 lane） | 拒绝生产 `.data`、sibling storage、未知/重复 override；两阶段 lexical/canonical 校验；collection 前安装 base-only Config；每个测试前后恢复同一个 base-only singleton | 直接 `Config()` 入口、plugin 预加载与 migration lane 另行治理 |
| Windows asyncio 与 child guard | `DONE_VERIFIED`（S4c/Windows） | loopback/AF_UNIX 留给 event-loop socketpair，字面外部 IP/DNS/datagram/sendmsg 阻断 | 明确不是 OS 级网络沙箱 |
| E2E live marker/双 sentinel/timeout | `DONE_VERIFIED`（S4c 默认排除） | live fixture 强制 marker；`temporary_test_config` 同时复验 env、dataclass 与 effective Config；timeout 透传 | live 用例本批未执行；仅验证默认不进入 |
| CLI/MCP 测试层级归属 | `DONE_VERIFIED`（S4c） | `CliRunner`→integration；进程内 MCP simulation→integration；blackbox 仅子进程协议 | 后续按 owner 清单继续去重 |
| 自证/错误 characterization 清理 | `DONE_VERIFIED`（S4c） | GUI model 改测生产类；删除 QueryRouter 吞异常、跨存储部分提交、取消混合状态和自写 Prompt 上下文等错误 oracle；引用卡改用生产 formatter | 产品语义决策项仍按 P0 独立窗口推进 |
| Config/fixture 隔离修复 | `DONE_VERIFIED`（S4c） | root parent 使用已验证 base-only singleton；workflow/vector/delete/verify_setup/GUI 继续使用显式临时 Config；新增字段篡改/no-probe、effective drift-before-write 与跨测试 singleton 恢复 | migration/其他直接默认 `Config()` 脚本不纳入安全声明 |
| Relation case 独立化 | `DONE_VERIFIED`（S4c） | 12 个 YAML case 参数化并使用新鲜状态；补 cycle/reverse/disconnected | tie/truncation 强 oracle 留后续 |
| Wrapper/CI launcher 安全 | `DONE_VERIFIED`（S4c Windows + CI 静态） | reparse/containment/cleanup 顺序、pytest trusted args/`--` 终止符、basetemp/cache/coverage、非空安全 `PYTEST_ADDOPTS`、base-only CLI 与 CI `.data-test`/sentinel 接线 | Linux CI 运行时矩阵与 Direct allowlist 留 P1；direct migration 继续 fail-closed 阻断 |
| 活跃测试文档路径 | `DONE_VERIFIED`（S4c 静态） | 更新 CLI in-process 与 MCP simulation 层级/命令；移除直接执行自动测试模块的说明 | 历史目录与 CHANGELOG 保持历史原文 |
| 产品功能代码 | 不适用 | 未修改 `src/**/*.py` | 所有红色目标合同转后续功能窗口 |

## 7. 可拆分任务卡

### P0 任务

#### P0-TC00 当前 TestCase 修复闭环

- 状态：`DONE_VERIFIED`（S4c）。
- 目标合同：证明当前 dirty tree 可收集、分层绿色、全量默认离线绿色，且 MCP 覆盖率仍不低于 95%。
- 主责层：test infra/全层回归；辅助层：活跃文档静态检查。
- 可杀死错误：未跟踪 fixture 遗漏、导入/收集失败、Windows asyncio 回归、层级迁移断链、测试间污染。
- Fixture/隔离：每条命令使用新的 `.data-test/review-testcase-*`；定向与分层命令显式 `-m "not network and not manual"`，full default 刻意复验 `pytest.ini` 的默认排除合同。
- 验证：先重跑 S3，再 unit/integration/e2e-offline/blackbox，随后 full default、MCP 95% gate、`git diff --check` 与旧路径 grep；旧路径检查显式排除 `docs/history/**` 和 `docs/operations/CHANGELOG.md` 的历史记录。
- DoD：所有命令退出码 0；没有 network/manual 执行；记录精确 passed/skipped/deselected/coverage；无 `src/**/*.py`、`.data` 或 local config 变更。
- 依赖：用户已于本轮明确允许离线复验；Windows Conda 环境可用。非登录 shell 首次找不到 Conda 的启动失败未进入 pytest，随后以登录 shell和新数据根重试。
- 风险：全量耗时与 subprocess teardown；按层拆跑并保留第一失败证据，禁止绕过包装器。
- 越界：不修产品实现，不跑 live。

#### P0-TC01 自动化测试安全合同

- 状态：`DONE_VERIFIED`（S4c 默认 lane 第一批）。
- 目标合同：默认测试无法落到生产/开发路径或读取 local config，父/子进程不继承凭据，错误 target/路径在产品导入或目录创建前失败。
- 主责层：wrapper/CI launcher 行为测试 + root conftest/offline runtime unit + CLI/MCP subprocess sentinel。
- 可杀死错误：`.data`/项目根写入、sibling DB、junction/symlink 越界、secret/proxy 继承、缺/伪 project-root sentinel、`--noconftest`/`--confcutdir`/`-c`/`--rootdir` 截断安全 hook、默认 `Config()` fallback、非法 target 先创建目录。
- Fixture/隔离：合成临时项目、外部 sentinel、无真实 secret；parent 安装 base-only Config；网络仅 fake/local seam。
- 验证：参数化 unsafe path/env；dataclass/env/effective Config 三重漂移；普通 processor 构造不得触发 default Config；比较执行前后文件树/父环境；Windows + Linux CI launcher 矩阵。
- DoD：所有危险输入 fail-closed 且不产生产品数据副作用；产品运行数据与 pytest basetemp/cache 只写 checkout `.data-test`；CI 报告和 Windows command bridge 临时载荷可写受控系统临时目录；direct migration 在拥有 base-only entrypoint 前保持阻断；文档明确 Python guard 非 OS 沙箱。
- 依赖：P0-TC00；若要完整网络隔离，需 CI/OS firewall 设计。
- 风险：Windows socketpair、junction 权限和路径大小写；第三方 pytest plugin 可在 root hook 前 autoload，且 `tests/` 外任意 Direct target 不在当前保证内；不得用宽松 skip 隐藏受支持平台失败。
- 越界：不接入真实 Provider，不读取 local config。

#### P0-TC02 Migration fail-closed

- 状态：`PLANNED`；源修复预计必需。
- 目标合同：fresh、缺表、损坏/不可读版本表严格可分；备份或任一步迁移失败不得继续/部分提交。
- 主责层：Storage unit + 临时 SQLite integration。
- 可杀死错误：异常→`0.0.0`、备份失败继续、半迁移、两个 manager 重复应用同版本。
- Fixture/隔离：临时 DB、损坏 schema、SQL 第 N 步注入、并发 barrier。
- 验证：失败前后 schema/data/version 等价；备份失败 SQL 调用数为 0；并发只提交一次。
- DoD：目标错误类型/状态稳定；rollback/锁合同可观察；旧 fail-open characterization 删除。
- 依赖：先确认损坏库错误码与并发锁策略；为 migration 增加显式 base-only config/entrypoint 后才能进入自动 lane；随后另开 `src` 功能修复窗口。
- 风险：SQLite DDL 事务与历史迁移兼容性；当前 `scripts/migrate.py` 默认 Config 会尝试合并 local config，禁止把仅隔离 DATA_DIR 误当成完整安全接线。
- 越界：不用真实 Vault/备份。

#### P0-TC03 真实 YAML 工作流语义

- 状态：`PLANNED`；源修复预计必需。
- 目标合同：版本化 YAML 的 step 顺序、`on_error`、trigger rule、condition 与 processor 选择被运行时忠实执行。
- 主责层：Workflow YAML-to-pipeline integration；辅助层：step pure unit。
- 可杀死错误：所有错误都继续、soft error 与 exception 混同、忽略 `trigger_rules`、总选默认 processor。
- Fixture/隔离：真实仓库 YAML + fake processor/AI/storage adapter；无网络。
- 验证：`fail/continue × exception/returned-errors` 矩阵；sentinel step 是否执行；最终 state/errors/logs 精确断言。
- DoD：每个生产 YAML 分支可达；配置字段漂移会失败；fake-step 重复用例收敛。
- 依赖：确认 soft-error 合同和 trigger schema；源修复另窗。
- 风险：测试过度绑定 YAML 文本；oracle 应绑定语义而非序列化格式。
- 越界：不归档真实内容。

#### P0-TC04 跨存储一致性与 Vault containment

- 状态：`BLOCKED_CONTRACT_DECISION`。
- 目标合同：为 Markdown 主存储、SQLite/FTS/chunk、Vector 的写删确定 transaction、compensation 或 repair-queue 状态机；所有文件路径 canonical 后必须在 Vault 内。
- 主责层：Storage/Workflow fault-injection integration；辅助层：CLI/GUI/MCP adapter sentinel。
- 可杀死错误：主文件失败后仍建索引、SQLite 删除失败后删主文件、chunk/vector 半完成标成功、绝对路径/`..`/junction 越界。
- Fixture/隔离：三层真实临时 store、每阶段失败、外部 sentinel、Windows capability gate。
- 验证：成功时四类记录全一致；失败时只允许决策表中的状态；重试/重复删除幂等；外部 sentinel 永不读删。
- DoD：无“SQLite 声称存在但 Markdown 已丢失”；degraded 状态可观察且可修复；路径 containment 跨入口一致。
- 依赖：先完成产品语义决策，再写最终 TestCase 与 `src` 修复。
- 风险：错误设计会永久锁定数据丢失行为。
- 越界：不使用开发 Vault。

#### P0-TC05 Retrieval 可判别结果与精确 RRF

- 状态：`BLOCKED_CONTRACT_DECISION`。
- 目标合同：`success/no_hits/invalid/error/degraded` 可区分；RRF 的分数、tie、去重、metadata merge 和排序可精确复算。
- 主责层：Retrieval unit oracle + 确定性 temp integration；辅助层：Evidence/MCP contract。
- 可杀死错误：异常吞成 `[]`、单侧失败冒充完整成功、tie 随插入顺序漂移、重复 ID/metadata 覆盖错误。
- Fixture/隔离：固定 ranked lists、固定向量、fake provider、临时 FTS。
- 验证：四策略状态矩阵；逐条 query 断言，不用“总体准确率 ≥50%”；mutation 打乱分数/顺序必须失败。
- DoD：统一响应模型、稳定 tie-break、上层不会把 outage 表述为“无证据”。
- 依赖：先定响应类型与 degraded 策略；源修复另窗。
- 风险：公开 API 兼容；需要迁移计划而非测试同时接受两套 schema。
- 越界：不调用真实 embedding。

#### P0-TC06 MCP provider、transport 与安全边界

- 状态：`PLANNED`；部分子项源修复预计必需。
- 目标合同：provider 按配置字段/工厂正确构造；HTTP Bearer 在真实 transport 生效；URL 每次 DNS/redirect/子资源都重校验；stdio 保持兼容。
- 主责层：construction integration + 本地 HTTP blackbox + pure security unit。
- 可杀死错误：`OpenAIClient(config)`、短 BM25 仍构造 provider、孤立 auth helper 未接线、DNS→私网、公开 URL→私网重定向、网络 guard 造成 SSRF false-green。
- Fixture/隔离：fake provider factory、本地 fake resolver/transport、数据库/Vault 前后快照；不访问外网。
- 验证：无/错/对 token 握手矩阵；每种 SSRF 拒绝稳定错误码且无写入；当前 manifest 全部 12 个只读 Tool 的 stdio 调用矩阵。
- DoD：测试直接捕获错误构造参数；auth 在初始化前拒绝；SSRF 失败原因不是 socket guard；MCP 95% gate 通过。
- 依赖：稳定错误码和 HTTP auth 部署策略；源修复另窗。
- 风险：本地 HTTP 仍需 OS 网络策略允许 loopback；不得改成真实站点。
- 越界：不使用真实 API key/费用。

#### P0-TC07 GUI Chat 异步状态原子性

- 状态：`BLOCKED_CONTRACT_DECISION`。
- 目标合同：请求绑定发起 session；切换、双发、停止、provider/save 失败均有互斥终态和明确持久化语义。
- 主责层：ViewModel async state-machine unit；辅助层：一条 pytest-qt/qasync wiring test。
- 可杀死错误：回复写入当前而非发起会话、重复发送、停止后 partial 内容错误进入下一轮、孤立 user turn、stream/client 多次关闭。
- Fixture/隔离：可控 async stream、barrier、fake session store、真实 Qt signal spy；不构造真实 provider。
- 验证：事件序列/状态矩阵；completed/stopped/error 互斥；A 流中切 B；save failure rollback/visible error。
- DoD：内存与持久化一致；无跨会话污染；公开 signal 与按钮状态可观察；删除未决取消 characterization。
- 依赖：先决定 stop 后 partial assistant 的保留/落库策略。
- 风险：qasync/Qt 调度 flake；用 barrier/信号等待，禁止真实 sleep。
- 越界：不运行真实 Chat API。

### P1 任务

#### P1-TC01 SQLite/FTS/rechunk 一致性

- 状态：`PLANNED`；层级：Storage integration。
- 目标/错误实现：insert→update 后旧词仍命中、新词不命中；重复/空 tag 计数；rechunk 留 stale SQLite/vector chunk。
- Fixture/验证：临时 DB + 固定中文分词 + deterministic vector；比较 FTS、tag、chunk/vector ID 前后集合。
- DoD：update/rechunk 后无 stale 项，rollback/幂等明确。
- 依赖/风险：先定 rechunk 原子性；中文 tokenizer 版本漂移。
- 越界：不使用真实 Vault。

#### P1-TC02 ReviewManager 状态机与并发

- 状态：`PLANNED`；层级：unit + temp SQLite concurrency integration。
- 目标/错误实现：非法转换、重复 approve/reject、history 写失败、并发覆盖。
- Fixture/验证：固定 state table、CAS/version、barrier 和故障注入；每次转换断言 state/history 一致。
- DoD：非法/重复操作稳定拒绝；并发只有一个胜者；失败无半状态。
- 依赖/风险：需定义幂等与冲突错误码；SQLite 锁时序。
- 越界：不接开发审核数据。

#### P1-TC03 AI 数值与批处理合同

- 状态：`PLANNED`；层级：AI unit/provider adapter integration。
- 目标/错误实现：batch 乱序/缺项/重复 index、空白映射、零向量/NaN/维度错、8000/8001 边界。
- Fixture/验证：纯 fake responses；按输入 index/cardinality 精确断言并参数化数值异常。
- DoD：所有非法 response fail-closed；批量输出与输入一一对应。
- 依赖/风险：定义可重试和不可重试错误；避免绑定 SDK 内部类。
- 越界：不调用真实模型。

#### P1-TC04 CLI 行为与唯一职责

- 状态：`PLANNED`；层级：command unit/`CliRunner` integration/子进程 blackbox 分工。
- 目标/错误实现：`limit<=0`、默认排序、debug/verbose 被忽略、短 auto 错建 provider、JSON/退出码/脱敏不一致。
- Fixture/验证：同一合成数据；integration 负责 wiring，blackbox 只负责进程/stdio；search→show 必须提取真实 ID。
- DoD：每个公开行为只有一个 owner；help/stats 重复删除且 mutation 仍被捕获。
- 依赖/风险：先定非法 limit 与默认排序；文案断言迁到结构化错误。
- 越界：archive 只用 fake processor，不联网。

#### P1-TC05 MCP 语义与协议矩阵

- 状态：`PLANNED`；层级：handler/in-process/stdio 分层。
- 目标/错误实现：filter 在 top-k 后造成漏召回、`strategy_used` 仅回显请求、capabilities 非空即通过、只注册不调用。
- Fixture/验证：固定 manifest 生成逐 Tool/Resource/Prompt sentinel；构造过滤命中在 top-k 外的 case；验证实际 route/degraded 字段。
- DoD：stdio 矩阵覆盖当前 manifest 全部 12 个只读 Tool；每层不重复全文 Prompt；超时/teardown 统一。
- 依赖/风险：先决定 `strategy_used` 定义；协议启动耗时。
- 越界：写入 Tool 只测本地 fake/拒绝路径。

#### P1-TC06 Processor conformance 与离线真实 pipeline seam

- 状态：`PLANNED`；层级：processor unit + YAML pipeline integration。
- 目标/错误实现：必填字段缺失、空正文、截断 HTML、编码、selector drift、错误 processor 被选中。
- Fixture/验证：版本化合成 HTML/text corpus；共享 conformance 参数；一条 YAML→dispatch→Entry→review→temp store 流。
- DoD：所有 processor 通过统一最小合同；selector drift 有明确 fixture 更新流程。
- 依赖/风险：先定 fallback/拒绝语义；HTML 样本版权与体积。
- 越界：不抓真实网页。

#### P1-TC07 GUI 真实 wiring 与全局状态恢复

- 状态：`PLANNED`；层级：pytest-qt component。
- 目标/错误实现：ArchiveWorker signal 顺序、renderer/inputbox、preview loader containment、stores reset、QSettings 全局 path/format 污染。
- Fixture/验证：Qt signal spy、临时 INI、外部 sentinel、显式恢复全局设置；禁止访问私有控件作为唯一 oracle。
- DoD：成功/可恢复失败各一条；测试顺序重排稳定；进程全局状态恢复。
- 依赖/风险：Qt 平台插件差异；需 capability gate。
- 越界：不启动真实 GUI/provider。

#### P1-TC08 重复契约收敛

- 状态：`PLANNED`；层级：测试治理。
- 目标/错误实现：重复 happy path 增量不杀新 bug，反而锁定文案/实现细节。
- Fixture/验证：为 Prompt/SQLite/BM25/migration/CLI/SSRF 建 owner 清单；删除前做 mutation 或等价错误注入。
- DoD：每个公开合同唯一 owner；高层只保留序列化/接线 sentinel；覆盖门槛不下降。
- 依赖/风险：P0 合同稳定后执行；不能以“去重”为由删掉唯一负例。
- 越界：不改变产品行为。

#### P1-TC09 Pytest 预入口、Direct 命令与 Config 构造期隔离

- 状态：`PLANNED`；层级：wrapper/offline entrypoint/CI launcher contract。
- 目标/错误实现：第三方 pytest plugin 在 root hook 前读取 secret/proxy；`-Direct` 任意命令或 `--junitxml`/插件报告路径写出受控根；`Config` 构造期间在路径复验前读取或清理 embedding 维度缓存。
- Fixture/验证：导入期读取环境的 fake plugin、外部输出 sentinel、Config 缓存读写 spy；比较 pytest 导入前后 env/文件树，并覆盖 Windows/Linux launcher。
- DoD：pytest 导入前已清理敏感环境并禁用自动插件或只启用显式 allowlist；Direct 命令/输出路径具备明确 allowlist 与 fail-before-write；Config 构造前无持久化缓存读取/清理。文档继续声明 Python guard 与 wrapper 不是 OS/文件系统沙箱。
- 依赖/风险：先盘点必需 pytest plugins、合法 Direct 脚本和 CI 报告根；收紧过快可能破坏 Qt/coverage 插件或受控运维脚本，应分入口而不是隐式放宽。
- 越界：不加载真实 secret/local config，不执行真实运维或 live 命令。

### P2 任务

#### P2-TC01 Property/metamorphic

- 状态：`PLANNED`；层级：pure unit + deterministic integration。
- 目标/验证：分页分割不变性、locator round-trip、RRF 单调性、schema 序列化、Relation 图重标号不变性。
- DoD：固定 seed、失败可缩减、用例预算可控；不得掩盖明确示例合同。
- 依赖/风险：先完成 P0 响应/排序合同；生成器复杂度。

#### P2-TC02 平台、并发与性能 lane

- 状态：`PLANNED`；层级：独立 Windows/concurrency/performance CI。
- 目标/验证：junction/大小写、并发 migration/review/upsert、向量规模、GUI 冷启动；性能用统计预算而非默认 `<100ms`。
- DoD：默认离线 suite 无机器时延断言；lane 失败与功能失败可区分；有基线和退化阈值。
- 依赖/风险：专用 runner/capability；避免共享机器噪声。

#### P2-TC03 Fixture、清单与文档治理

- 状态：`PLANNED`；层级：静态检查/CI。
- 目标/验证：自动核对测试层级路径、marker、fixture owner、taskset digest、文档中的节点数/命令；禁止自动测试模块直接 `pytest.main()`；为 `tests/` 外 target 和 pytest plugin autoload 制定显式 allowlist/safety-plugin 策略。
- DoD：重命名或 manifest 变化会使单一静态门禁失败并给出修复位置。
- 依赖/风险：P1 owner 清单；避免扫描历史文档造成误报。

## 8. 执行顺序与新窗口交付

建议按以下窗口拆分，不把多个产品决策混在同一 PR：

1. `W0 / P0-TC00`：只做当前 TestCase 修复复验与缺陷收口；不改产品代码。
2. `W1 / P0-TC01`：测试安全门禁和 Windows 行为；输出明确能力边界。
3. `W2 / P0-TC02 + P0-TC03`：分别先写迁移与 Workflow 目标合同，再开对应源修复；可拆成两个 PR。
4. `W3 / P0-TC04`：先产出跨存储状态决策表，用户确认后写测试和源修复。
5. `W4 / P0-TC05`：先定统一检索响应与 tie-break，再写 exact oracle/源修复。
6. `W5 / P0-TC06`：MCP provider、HTTP auth、SSRF 各自独立提交，最后跑统一协议矩阵。
7. `W6 / P0-TC07`：先确认 Chat stop/partial 持久化语义，再做状态机测试/源修复。
8. P1 每张卡独立窗口；P2 各 lane 独立立项。

每个新窗口必须带入：任务 ID、目标合同、允许修改层、fixture 根、稳定错误码/容差、副作用矩阵、验证命令、DoD、明确越界项。

## 9. 与后续真实数据和 Vault 工作的边界

### 9.1 真实数据测试流程

本规划只向后续流程提供 TestCase manifest/schema、脱敏字段要求、容差、报告格式和离线先决门禁。后续流程独立负责数据授权、脱敏、凭据、网络/费用审批、运行窗口、重试和结果留存。

衔接门槛：

- 默认合成离线套件与相关目标合同先绿；真实数据结果不作为本任务 DoD。
- 真实流程不得复用本任务的数据根或 fixed v1 gold，只复用契约名称/schema。
- 真实内容、secret、原始输出不得提交版本库。
- 当前保留的 opt-in `network` fixture 仅是既有测试入口的门禁加固；本任务不配置、不选择、不执行它。

### 9.2 开发 Vault 重建

重建任务只能在迁移、containment、跨存储一致性和 smoke checklist 通过后消费这些验收项。备份、授权、执行、回滚与人工确认全部属于独立任务。

开发/生产 Vault 永不作为 pytest fixture；本规划不读取、枚举或修改它们，也不以重建成功作为 TestCase 修复完成条件。

## 10. 可复制验证命令与证据记录

以下命令均必须从目标工作树运行；每次使用新的 `.data-test` 子目录：

```powershell
# S4c 定向复验复现命令
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\review-testcase-s4c-targeted -Command @("python", "-m", "pytest", "tests/unit/test_offline_runtime.py", "tests/e2e/test_fixture_isolation.py", "tests/integration/test_mcp_client_simulation.py", "tests/unit/test_mcp_quality_scorer.py", "tests/integration/test_mcp_quality_eval.py", "tests/e2e/test_mcp_e2e_archive.py", "tests/e2e/test_mcp_e2e_search.py", "tests/e2e/test_mcp_e2e_knowledge_qa.py", "tests/unit/test_run_test_wrapper.py", "-m", "not network and not manual", "--tb=short", "-q")

# 分层离线
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\review-testcase-s4c-unit -Command @("python", "-m", "pytest", "tests/unit", "-m", "not network and not manual", "--tb=short", "-q")
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\review-testcase-s4c-integration -Command @("python", "-m", "pytest", "tests/integration", "-m", "not network and not manual", "--tb=short", "-q")
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\review-testcase-s4c-e2e -Command @("python", "-m", "pytest", "tests/e2e", "-m", "not network and not manual", "--tb=short", "-q")
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\review-testcase-s4c-blackbox -Command @("python", "-m", "pytest", "tests/blackbox", "-m", "not network and not manual", "--tb=short", "-q")

# 全量默认离线
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\review-testcase-s4c-full -Command @("python", "-m", "pytest", "-q")

# MCP 覆盖率门禁（复用全层离线选择）
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\review-testcase-s4c-mcp-cov -Command @("python", "-m", "pytest", "tests/unit", "tests/integration", "tests/blackbox", "tests/e2e", "-k", "mcp", "-m", "not network and not manual", "--cov=src.mcp", "--cov-report=term-missing", "--cov-fail-under=95", "-q")
```

每次记录：HEAD/dirty 状态、命令、时间、passed/failed/skipped/deselected、coverage、首个失败与 `.data-test` 根。禁止用 S0/S1 的数字替代当前结果。

## 11. 决策与治理说明

| 项目 | 结论 |
|---|---|
| 是否修改产品实现 | 否；本批没有修改 `src/**/*.py` |
| 是否运行 live/network | 否 |
| 是否访问生产 `.data` 或 `config/local.yaml` | 否 |
| 是否推进真实数据流程/开发 Vault 重建 | 否 |
| 是否修改 fixed MCP v1 gold/阈值 | 否 |
| 提交/推送状态 | 任务内已存在当前 HEAD 提交 `0687f74`；本次续作未新增提交；未推送，待用户决定保留、拆分或回退 |

治理偏差必须如实披露：规划文档及早期修复已存在于当前 HEAD 提交 `0687f74`，这超出了最初“先建议位置、不要自行写入/提交”的约束。本次续作只产生未提交工作树改动，没有新增提交或推送；最终应由用户决定保留、拆分或回退 `0687f74`。建议规划文档继续位于本文件，因为它与现有 `docs/operations/testing/` 结构一致。
