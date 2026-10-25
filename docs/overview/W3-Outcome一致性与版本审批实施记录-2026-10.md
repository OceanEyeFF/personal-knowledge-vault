# W3：Outcome 一致性、版本审批与合成迁移

开始日期：2026-10-25。基线提交：`009c48d`。

状态：W3 范围内实施与相关回归已完成。R-03/R-05 闭环，v2 版本审批与兼容入口
OC-10a 验收通过；R-04 旧无版本审批仍留待 W5。W4–W5 未推进，项目完整默认测试仍保留五个已知红项。

本阶段按[分阶段计划](./生命周期一致性修复设计与分阶段计划-2026-10.md)、
[v2 接口冻结合同](../specs/interfaces/生命周期一致性v2接口冻结合同-2026-10.md)与
[迁移冻结方案](../specs/database/生命周期一致性迁移冻结方案-2026-10.md)推进。
范围是 Outcome 输入事务、任意版本发布、版本绑定审批及 SQLite 014 的合成迁移。
W4 manual rebuild 恢复/预算、W5 API-major-2 切换均未纳入本阶段。

## 1. 实现与兼容边界

- 输入行及关系边、input revision/digest 和旧 brief 的失效标记在同一事务内提交。
  规范化后没有实际变化的请求不推进输入版本；读取使用一致的 SQLite snapshot。
- publication identity 绑定 outcome、candidate revision 和 input digest。新输入使用不同文件名；
  同输入仅可采用字节一致的完整文件。pointer 与捕获输入/旧 pointer 一起进行 CAS。
- 新 v2 审批使用 outcome ID、brief revision、content SHA 和 input revision 的完整确认元组；
  state 与 review receipt 同事务，精确重放不能撤销后续退回或审批新版本。
- 014 / schema 1.2.8 在 private work database 完成 DDL、规范输入回填和验证，再原子发布。
  SQL 与 hook/identity 实现共同进入迁移清单摘要。旧 brief 保留持久 state、版本、pointer
  和文件字节，但 binding 为 legacy_unverified，必须重新生成和审阅，不制造历史凭据。
- SDK 0.9.0 是本地兼容 build，API 保持 1.0.0；新增独立 Outcome v2 DTO/入口。
  旧审批符号保留签名和既有语义并发出弃用警告。R-04 的旧入口缺陷只在 W5 关闭。

## 2. 验收范围

| 验收族 | W3 要求 |
| --- | --- |
| OC-01/02/11 | 各类输入 mutation 与 no-op；事务切点回滚、commit 后新进程读取；一致 read snapshot。 |
| OC-03/09 | legacy/input/source/projection 复核；过期或不可验证绑定拒绝，未知 I/O fail-closed。 |
| OC-04/05/06 | r1/r2/rN 孤儿文件、同输入重试、改输入与 A→B→A、并发 CAS、commit 后响应中断。 |
| OC-07/08/10a | v2 四元组审批、receipt 原子性与幂等、旧入口弃用；真实 CLI/MCP 新进程交互。 |
| OC-12 / MIG-01–05 | 1.2.7 合成升级、work-copy 故障、发布后重入、伪 schema/坏 binding 拒绝。 |
| SDK 兼容 build | 新旧符号、required_api_major 门禁、离线 wheel 与源码隔离安装。 |

具体 nodeid、命令、冻结指纹与实际结果已登记。原 W1 R-03/R-05 的三项用例
全部通过，其中两项由红转绿；R-04 的两项旧无版本审批、R-02 的 D 争锁和 R-06 日/月预算共五项仍准确失败。
不使用 skip/xfail 或默认收集过滤消除这些阶段外失败。

## 3. 验证记录

全部测试与合成迁移只经 `scripts/run-test.ps1` 和独立 `.data-test`；不读取用户 Vault/凭据，
不运行真实迁移或真实 Provider。Windows P0/MCP 通过 `scripts/test-conda.ps1`
调用该包装器。组件结果不相加冒充一次全量运行。

| 运行 | 实测结果与证据边界 |
| --- | --- |
| Store 最终组件 | `65 passed`，186.44s；同一 snapshot node 强化 oracle 后复跑 `1 passed`，6.20s，独立用例仍为 65。 |
| Outcome service/process/旧合同/Kernel/W1 | `69 passed, 2 failed`，308.97s；两个失败均为 R-04 旧无版本审批。运行期间 Store 校验曾加强，故仅作组件证据。 |
| OC-02 输入 commit 后响应丢失 | `1 passed`，14.96s；真实 writer 子进程在 Store 返回后 `os._exit(73)`，全新 reader 经 public v2 确认只提交一次、读取零写。 |
| 合成迁移最终组件 | `23 passed`，54.25s；含历史 brief 回填、资源清单漂移、DDL/绑定检查、work-copy 故障与发布后重入。 |
| SDK 与 Wrapper 边界 | `84 passed`，20.80s；75 个新 SDK 用例与 9 个边界用例，含伪 schema 的零写拒绝。 |
| OC-03 合成升级后的公开审批 | `1 passed`，8.09s；历史 approved brief 仍有 current evidence、supported claim 与可用投影，升级后仅因 legacy binding 拒绝 v2 审批，DB/Markdown 零写且 receipt 为零。 |
| 新旧 Outcome CLI/MCP 黑盒 | `21 passed`，230.03s；运行中 CLI 的 stage 展示曾微调，故最终同树确认交给完整 P0/MCP。 |
| 既有迁移/运行时兼容组件 | 372 个独立通过用例、5 个既有平台 skip；两个因并行资源修改而拒绝的用例在资源冻结后复跑 `2 passed`。不是一次全量结果。 |
| MCP Phase C | 16/16 tasks、151/151 checks，overall 与各维度均为 1.0，`targets_met=true`、`thresholds_met=true`，exit 0。 |
| 静态严重错误检查 | 修改范围 `ruff --select E9,F63,F7,F82` 通过，exit 0。 |
| 离线代理隔离与 MCP 失败复验 | `69 passed`，150.45s；63 个离线入口测试、2 个代理隔离新例、4 个原失败黑盒。限选集合不代表完整覆盖率门禁。 |
| MCP coverage 最终门禁 | `835 passed, 5 deselected`，865.58s，exit 0；2085 statements / 93 miss，`95.54%` ≥ 95%。 |
| 默认 P0 | `2852 passed, 71 failed, 798 errors, 11 skipped, 219 deselected`，2414.36s，exit 1。磁盘满造成大面积失败；另有新增 wheel 探针错误预期，不能将此轮记为通过。 |
| P0 异常文件补跑 | 原全部 869 个 FAILED/ERROR 节点所在的 46 个完整文件，`1059 passed, 5 failed, 12 skipped, 6 deselected, 2 warnings`，1498.35s，exit 1；五项失败严格等于既定 R-02/R-04/R-06，零错误。 |
| 全仓去重联合核验 | 默认收集的 3732 个节点全部覆盖：`3706 passed, 21 skipped, 5 known failures, 0 errors`。这是原轮与补跑的去重结果，不是单轮全量通过。 |
| 新增 W3 / 原 W1 | 新增 229 项全部通过（原轮 20、补跑 209）；原 W1 的 34 项为 `29 passed, 5 known failures`，无 skip/error。 |

去重时，未重跑的 2656 个原节点保留 `2647 passed, 9 skipped`；恢复运行的 1076 个节点
整体采用最终结果。恢复集与原 46 文件的默认收集节点严格一致，完整覆盖全部 869 个坏结果。
21 个跳过均为既有环境边界：15 个 symlink 不可用、5 个 POSIX-only、1 个未提供真实
PyInstaller closure；它们不计为通过，精确 nodeid 与原因见 JSON。离线 source-free wheel
在恢复运行中通过，验证 SDK 0.9.0/API 1.0.0、新旧公开接口和弃用警告。

Store 的事务切点使用 SQLite authorizer 拒绝语句；snapshot 用独立连接/WAL 并发。
发布的 9 个真实子进程用例在 r1/r2/r4 的明确 seam 注入异常，再由新解释器读取或重试；
它们不是 OS 强杀模拟。OC-02 单独使用真实进程退出。迁移的发布后重入采用同进程新连接，
不冒充断电或新 OS 进程验收。

完整 nodeid、命令和证据路径登记于
[`w3_lifecycle_repairs.v1.json`](../../tests/contracts/w3_lifecycle_repairs.v1.json)。
主要本地证据位于 `.data-test/w3-20261025-evidence/reports/`；运行日志及临时数据库不进入产品包。

## 4. 冻结与后续边界

基线为 `009c48d`；本记录及指纹在提交前冻结，提交标识以 Git 历史为准。最终产品 diff 的 Git blob 为
`1f1f92f1708b84d703ace8278779b29525aeb8b4`，包含新增文件的
`final-product-files.sha256` 清单摘要为
`be93a9bc696ac042865a2beee2139837852d04a437da58acfa794487f80e9720`。
加入 OC-02/03、强化 snapshot、修正旧 MCP 数量断言、测试代理隔离及 wheel 错误码预期后，
`final-source-tests-v4.sha256` 摘要为
`e0fca65767ab302cfbec27a0d9b824674be5b091664e34cd9edcf4b41699d895`；
该清单不含本验收 JSON，避免自引用。
v3→v4 唯一变化是该 wheel 测试断言，产品字节完全相同；v3 清单与全部历史运行日志保留。

W1 原测试/JSON、W2 JSON、SQL 013 与范围外 Embedding/预算/Q1 产品文件均通过基线指纹核对。
早期组件运行中遇到的迁移资源清单漂移，在实现冻结后均单独复验。

首轮 MCP 为 `830 passed, 5 failed, 5 deselected`、95.54% coverage，exit 1，不能记为门禁通过。
其中一个旧测试遗漏了 Tool 数量从 30 到 35 的变化，修正后单点 `1 passed`；另四个场景
在隔离复跑时再次失败，harness request count 为零。根因是 HTTPX 从 Windows 注册表重新
发现系统代理，loopback 请求转到代理端口后被测试精确网络 guard 拒绝。仅测试入口在验证
合成 Provider 配置并完成环境清理后设 `NO_PROXY=127.0.0.1,localhost`；产品代理策略与
精确 socket 白名单不变。新增测试确认普通子进程、其他端口与外部地址仍被阻断。
上述 69 项复验通过后重跑完整门禁。

两次 P0 诊断运行分别因补 OC-02/snapshot oracle，以及后续测试代理隔离与 OC-03 补充
主动中止，均无最终结果。完整 P0 `j-xzsc95` 执行至 100%，但 E 盘耗尽导致 SQLite、
临时目录及 pytest cache 写入失败。另一个较早的独立失败来自新增 wheel 探针：未生成 brief
时，原合同与当前产品都返回 `outcome_approval_blocked`，探针却期待 `outcome_not_found`；
修正仅涉及测试，不改变旧接口语义。

恢复空间时，只清理十个明确属于本轮的测试根内 505 份正常大小的 `jieba.cache`，
释放 4,674,179,072 bytes。数据库、投影、日志及历史测试根均保留；绝对路径清单与清理脚本
进入证据目录。因为磁盘满使 pytest cache 也未能写出，恢复选择依据原日志中全部
71 FAILED + 798 ERROR 摘要生成，并扩展到其所在的 46 个完整文件，没有漏掉已知红项。
补跑 `j-tnwjm0` 保持原离线 marker 规则并已完成；完整 MCP `j-j2zbrm` 已通过。
P0 原轮与异常文件补跑分别记录，不将重叠用例数相加冒充一次全量通过。
MCP 运行于 v3，v3→v4 唯一变化是该 lane 不收集的 wheel 测试断言；MCP 测试与产品字节均未变化。
最终 v4 源码/测试指纹与范围外基线复核均通过。

旧 held Artifact、真实维护入口、配置/数据根政策和 release hold 保持原状态。
本地兼容 wheel 不能替代 W5 major 切换或 installed-Artifact 发布验收。
