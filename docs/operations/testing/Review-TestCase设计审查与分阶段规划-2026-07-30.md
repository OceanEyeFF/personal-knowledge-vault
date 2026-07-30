# Review TestCase 设计审查与分阶段规划

> 日期：2026-07-30
> 基线：`e771ba0a671e2046ced85d77eac8f9eaa0431122`
> 最近已知全量结果：`1471 passed, 1 skipped`（由任务背景提供）
> 范围：测试设计、测试代码、fixture、离线评测与测试门禁；不包含功能实现、真实数据测试或开发 Vault 重建

## 1. 审查目标

本审查不以“增加测试文件数量”为目标，而是检查每个关键契约是否：

1. 有唯一、明确的测试层级负责；
2. 能区分正确实现与常见错误实现，避免 false-green；
3. 使用可重复、互不污染的 fixture；
4. 在默认离线门禁、显式联网门禁和固定 MCP 评测之间边界清晰；
5. 具有可执行的验证方法和完成定义。

## 2. 当前测试体系结论

### 2.1 已具备的可靠基础

- `pytest.ini` 默认排除 `manual` 与 `network`，并启用 strict markers。
- CI 将运行时路径放在 runner 临时目录；MCP 模块具有 95% 覆盖率门槛。
- MCP 固定评测已形成 gold/proposal 物理分离、16 条任务、119 项检查、错误 Tool/参数/query 反例和阈值退出码门禁。
- MCP 的 Tool、Resource、Prompt 已覆盖单元、FastMCP 进程内调用和 stdio 协议三层。
- 大部分存储、检索和工作流测试使用 `tmp_path`、临时 SQLite 或 mock，不依赖生产数据。

### 2.2 P0 级设计缺口

#### A. 默认离线与隔离契约缺少行为级自测

`scripts/run-test.ps1` 承担路径包含关系、重解析点、硬链接、危险脚本、迁移参数、环境变量恢复和敏感值脱敏等关键门禁，但现有测试主要通过脚本文本片段和顺序断言验证。这类断言对重构敏感，却无法证明门禁行为真实有效。

#### B. MCP/CLI 边界存在宽松断言和 live TestCase 漂移

- 若干测试接受 `total_entries` 或 `total` 任一字段，未固定公开 schema。
- 部分 CLI 黑盒用例仅断言“输出非空”或命中多个候选字符串之一。
- live 归档 TestCase 仍读取 `file_path`，而加固后的 MCP 写入返回使用 `entry_locator`；该用例因默认排除 `network` 而不会在已知全量结果中失败。

#### C. 评测器自身的反例空间不足

- taskset 校验对 assertion operator、权重、阈值范围和字段类型的错误输入覆盖不足。
- scorer 对集合重复项及 Python 宽松相等语义缺少明确合同。
- 场景 oracle 的负向校验主要集中在 chunk 与 relation locator，bridge、timeline、contrast、citation provenance 缺少系统 mutation matrix。

#### D. 主存储和跨存储失败语义缺少直接合同

- `MarkdownStore` 的 round-trip、缺失文件和生命周期主要被间接覆盖。
- `StoreStep` 缺少“Markdown 成功后 SQLite 失败”“文档向量成功后 chunk 向量失败”“重试后状态”等失败矩阵。
- GUI 删除流程未固定 Markdown、SQLite、Vector 三层的调用和部分失败可观察性。

### 2.3 P1 级设计缺口

- Prompt 内容在单元、FastMCP、stdio 和所谓 E2E 中重复验证，责任边界不清。
- `test_mcp_client_simulation.py` 位于 blackbox 但实际是进程内；`test_cli_e2e.py` 使用 `CliRunner` 而非子进程。
- SQLite、BM25、迁移相关 additional/management/runtime 文件存在重复 happy path 与异常路径。
- 关系最小回归数据集在一个长测试中顺序执行并共享可变状态，YAML 重排可能造成级联结果。
- GUI `ChatView`、保存对话到知识库、流式渲染错误路径缺少高价值行为流。
- Processor 覆盖不均衡，缺少跨处理器的解析 conformance matrix。
- 默认套件内存在机器时间 `<100ms` 断言、真实 `sleep` 和未显式验证 `strategy_used` 的检索测试。

### 2.4 P2 级设计缺口

- 缺少针对分页、locator、路径规范化、RRF 和 schema 序列化的 property/metamorphic 测试。
- Windows 特有的 junction、路径大小写和 PowerShell 包装器行为缺少专属 CI lane。
- 缺少独立的并发、性能与顺序随机化测试 lane。
- 测试数量、模块覆盖说明和 fixture 目录文档存在漂移，缺少自动一致性检查。

## 3. 契约—层级—责任原则

| 契约 | 唯一主责层 | 辅助层 | 不应重复的内容 |
|---|---|---|---|
| 纯函数与错误映射 | 单元 | 无 | 跨协议重复穷举参数 |
| SQLite/Markdown/Vector 协作 | 集成 | 单元故障注入 | GUI/stdio 重复内部调用细节 |
| FastMCP 注册与返回 schema | 进程内集成 | 单元验证 handler 分支 | E2E 重复 Prompt 全文 |
| JSON-RPC/stdio 序列化 | 黑盒 | 无 | 进程内测试冒充黑盒 |
| CLI 退出码、stdout/stderr | 子进程黑盒 | `CliRunner` 组件测试 | 仅断言输出非空 |
| 固定 Agent 决策与证据合同 | MCP 离线评测 | scorer/runner 元测试 | 用真实数据改写 gold |
| 真实第三方兼容性 | 后续 real-data/live 流程 | 离线 fixture | 默认 CI 联网 |

## 4. 分阶段执行计划

### P0：先消除安全门禁空洞和 false-green

#### P0-TC01 测试包装器与默认离线门禁行为合同

- 目标：用真实子进程行为证明包装器拒绝危险路径/命令、隔离全部运行时路径并恢复父进程环境。
- 涉及层级：脚本行为测试、pytest 元测试、Windows CI。
- 验证方法：
  - 为包装器提供临时仓库、伪生产根和 sentinel secret；
  - 验证越界路径、重解析点/硬链接、危险脚本和不安全迁移参数被拒绝；
  - 验证成功命令只看到隔离路径，失败输出不泄露 secret，运行后环境恢复；
  - 验证默认收集不包含 `network`/`manual`。
- 完成定义：
  - 关键安全分支均由行为结果、退出码和副作用断言覆盖；
  - 文本实现细节断言仅保留最小 smoke contract；
  - Windows lane 能执行 PowerShell 行为测试。
- 依赖：可用 PowerShell；CI runner 支持 Windows。
- 风险：平台路径/链接语义不同。测试必须按平台显式 skip，而不是弱化断言。

#### P0-TC02 MCP/CLI 公开边界消除 false-green

- 目标：固定 canonical 返回 schema、退出码和可观察副作用。
- 涉及层级：MCP 进程内集成、stdio 黑盒、CLI 子进程黑盒、live TestCase 收集合同。
- 验证方法：
  - 用精确字段集合、字段类型和 locator 格式替代 `A or B`；
  - 写入成功统一验证 `entry_locator`，不得依赖本地 `file_path`；
  - 错误路径同时验证错误类型/错误码和“无写入”；
  - live 测试只做收集/门禁静态验证，不在默认套件联网。
- 完成定义：公开边界不再存在已知宽松 schema 断言；live 用例与当前合同一致；三层各自只负责本层。
- 依赖：当前 P1 MCP 证据合同。
- 风险：历史字段若仍被外部调用方依赖，应另开兼容性决策，不在测试中双重接受。

#### P0-TC03 MCP 评测器元测试与 oracle mutation matrix

- 目标：证明评测框架能拒绝坏 taskset、坏 proposal 和伪造证据。
- 涉及层级：scorer 单元、runner/taskset 单元、离线评测集成。
- 验证方法：
  - 参数化非法 operator、字段类型、权重、阈值、重复 ID 和缺失字段；
  - 固定集合重复项与 bool/int 等边界语义；
  - 分别篡改 bridge、timeline、contrast、chunk citation/provenance；
  - 确认每次 mutation 只击中预期维度且使阈值失败。
- 完成定义：任务集 schema 和 operator 语义有直接负向测试；五类 evidence path 均有 mutation 反例；v1 的 16/119 基线不变。
- 依赖：固定 v1 taskset 与 deterministic scenario。
- 风险：过度绑定 fixture 内部实现。反例应从公开结果结构修改，而不是调用私有构造细节。

#### P0-TC04 主存储与跨存储故障语义

- 目标：固定 Markdown 主存储 round-trip 及多存储部分失败的可观察语义。
- 涉及层级：storage 单元、workflow 集成、GUI ViewModel 单元。
- 验证方法：
  - Markdown save/load/list/delete round-trip，覆盖 Unicode、日期、列表和缺失文件；
  - StoreStep 对每个写入阶段注入异常，断言调用顺序、异常传播、已发生副作用和未发生副作用；
  - 删除流程验证三层调用、Vault 范围和部分失败反馈。
- 完成定义：当前既定的 atomic 或 best-effort 语义被明确记录并测试；暴露出的产品问题另开功能修复任务。
- 依赖：先确认跨存储事务语义。
- 风险：当前实现可能无回滚。测试不得擅自定义新功能行为。

### P1：收敛重复并补高价值行为流

#### P1-TC01 契约—层级—唯一负责人矩阵

- 合并 Prompt、SQLite、BM25 和迁移的重复断言。
- 将进程内 MCP/CLI 用例归入 integration/component，将真正的 stdio/CLI 子进程留在 blackbox。
- 验证：删减或迁移后，以 mutation 检查证明错误实现仍会被唯一主责层捕获。
- 完成定义：每个公开契约有唯一主责层；不得降低 MCP 95% 门槛。

#### P1-TC02 关系回归独立化与拓扑多样化

- 将长循环拆成独立 case 或声明显式依赖，每个 case 使用新鲜状态。
- 增加 cycle、diamond/tie、反向边、重复边、截断和断连图。
- 验证：随机重排 case，结果稳定；单一失败不污染后续 case。
- 完成定义：Phase B 最小回归顺序无关，且不修改 MCP v1 gold。

#### P1-TC03 高价值 GUI、Processor 与检索行为流

- 覆盖 ChatView 流式成功/错误/取消、保存到知识库 worker、malformed JSON 和 system message 排除。
- 建立 Processor conformance matrix：必填字段、空正文、截断 HTML、编码和 selector drift。
- 检索用例必须断言 `strategy_used`、降级原因及结果排序，不只比较首条 ID。
- 完成定义：每个关键用户流至少有一个成功路径和一个可恢复失败路径。

#### P1-TC04 确定性、性能与覆盖治理

- 将默认 `<100ms` 断言移至性能 lane；单元测试使用 fake clock，移除真实 `sleep`。
- 引入固定随机种子和顺序随机化 lane。
- 对 storage/workflow/relation/retrieval 逐模块引入风险导向 branch coverage 基线。
- 完成定义：默认离线套件不依赖共享机器时延；新增门槛有记录的起始值和提升策略。

### P2：扩展稳健性与长期治理

1. Property/metamorphic：分页分割不变性、locator round-trip、RRF 单调性、schema 序列化。
2. 平台/并发/性能 lane：Windows 路径、并发写入、索引规模与 GUI 冷启动。
3. Fixture 与文档治理：fixture 所有权、过期检测、任务数/文档自动同步。

## 5. 实施顺序

建议后续任务按以下顺序独立拆分：

1. `P0-TC01`
2. `P0-TC03`
3. `P0-TC02`
4. `P0-TC04`
5. `P1-TC01`
6. `P1-TC02`
7. `P1-TC03`
8. `P1-TC04`
9. P2 各 lane 独立立项

每个任务必须记录：契约 ID、主责层、输入 fixture、断言 schema、允许误差、副作用、隔离策略、验证命令和完成定义。

## 6. 与后续工作的边界和衔接

### 6.1 “真实数据测试流程”

本规划只交付：

- TestCase ID 与优先级；
- 输入需求和脱敏要求；
- 断言 schema、容差、预期副作用；
- 隔离、报告和退出码格式。

后续真实数据流程独立负责：

- 数据选择和授权；
- 脱敏、凭据、网络与费用审批；
- 运行窗口和结果留存策略；
- 真实服务波动的重试与容差。

真实数据流程不得：

- 让默认 pytest 指向开发或生产 Vault；
- 为适配真实数据而修改固定 v1 gold；
- 将真实内容或运行结果直接提交到版本库。

### 6.2 “开发 Vault 重建”

本规划可向重建任务提供：

- 条目数、关系数、向量数一致性检查；
- Markdown/SQLite/Vector locator 可读性；
- 搜索 smoke case 和回滚验收项。

开发 Vault 重建独立负责备份、复制、迁移、重建索引、回滚和人工确认。本规划及其 TestCase 不读取、枚举或修改本地开发/生产 `.data`。

### 6.3 固定 MCP 评测的角色

固定合成评测继续作为离线回归门禁；真实数据测试负责外部有效性；开发 Vault 重建负责数据运维。三者共享“契约名称和报告格式”，不共享数据根、gold 或执行权限。

## 7. 决策日志

| 决策 | 结论 |
|---|---|
| 是否修改功能实现 | 否；TestCase 暴露的功能缺陷另立任务 |
| 是否运行 live/network | 否；默认只验证收集与门禁 |
| 是否访问 `.data` 或 `config/local.yaml` | 否 |
| 是否推进真实数据流程 | 否 |
| 是否推进开发 Vault 重建 | 否 |
| 是否改变 MCP v1 gold/阈值 | 否，除非独立评测版本升级任务明确授权 |

## 8. 本轮实施结果

本轮已在 `codex/review-testcase-hardening` 分支完成 P0 TestCase 修复，未修改产品功能代码。

### 8.1 已完成

| 任务 | 实施结果 |
|---|---|
| `P0-TC01` | 新增 `run-test.ps1` 行为测试：隔离路径、越界根、`config set`、备份/恢复脚本、迁移 `--no-backup`、敏感参数脱敏、退出码和 Windows 硬链接。 |
| `P0-TC02` | MCP stats/search 改为 canonical schema 断言；CLI 黑盒固定退出码、JSON schema、结果内容和过滤副作用；live 归档改用 `entry_locator`，并为每个归档 case 使用独立数据库/Vault。 |
| `P0-TC03` | taskset/proposal 校验新增类型、阈值、维度、operator、权重、唯一 ID、expected、priority/impact 等负例；scorer 消除 bool/int 宽松相等；bridge/timeline/contrast/chunk 增加 oracle mutation 反例。 |
| `P0-TC04` | 新增 MarkdownStore 生命周期/round-trip；固定 StoreStep 当前 best-effort 部分失败语义；固定 GUI 删除三层调用与部分失败反馈。 |
| 确定性修复 | 移除默认 E2E 的 `<100ms` 机器时延断言，改为重复调用结果稳定；移除 timeout 测试的真实 `sleep`；QSettings 改写到 pytest 临时 INI 路径。 |

### 8.2 验证结果

- 全量默认离线：`1514 passed, 1 skipped, 10 deselected`。
- MCP 覆盖率门禁：`340 passed, 5 deselected`，`src.mcp` 覆盖率 `97.04%`，高于 95% 门槛。
- live/network 测试未执行。
- 所有 pytest 命令均通过 `scripts/run-test.ps1`，数据根位于仓库 `.data-test`。

### 8.3 明确留给后续任务

以下事项会改变产品行为或需要独立基础设施决策，本轮没有实施：

1. `MarkdownStore` 对 `subdir`、`load`、`delete` 的 Vault containment，以及同秒第三次同名保存的碰撞策略；
2. GUI 删除中相对路径 containment、三层删除的事务/回滚策略；
3. MCP `search_knowledge.strategy_used` 是否应返回路由后的实际策略；
4. Windows wrapper CI lane，以及对子进程也有效的默认网络阻断机制；
5. P1 的测试层级去重、关系 case 独立化、ChatView/Processor conformance 与风险导向 branch coverage；
6. P2 的 property、并发、性能和跨平台扩展。
