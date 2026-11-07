# W1–W3 CodeReview 修复与补充验收

日期：2026-10-25。评审基线：`b725deb2fd0c6a1294f2fac9bca122b3a0635b0f`。

状态：三项修复与相关回归已完成。新增 56 项全部通过；最终联合运行是
`136 passed, 5 known failures, 1 skipped`，没有新增失败，不是全仓全绿。
本记录不替代先前冻结的 W1/W2/W3 历史运行证据。

本次仅处理 W1–W3 累计评审确认的三项缺陷。W4 的 Embedding 发布恢复与预算重入、
W5 的 API-major-2 切换、真实用户数据迁移及发布均不在范围内。SDK `0.9.0`、
API `1.0.0`、SQLite `1.2.8` 和既有 capability 保持不变。

## 1. 缺陷与验收要求

| ID | 评审缺陷 | 必须证明的修复结果 |
| --- | --- | --- |
| CR-01 / P1 | 迁移清单读取原始 `.py`，现有 PyInstaller payload 未包含所需文件。 | 构建从同一冻结输入生成两个模块的严格摘要资源，不夹带被 payload policy 禁止的原始源码；冻结目录的 fresh/READY 检查成功，指纹与源码布局一致；缺失/畸形资源拒绝、资源变更改变指纹。 |
| CR-02 / P1 | 正式 `python -m src.cli.commands` 入口绕过 `src.main` 的 readiness gate。 | 原生模块与标准入口共享叶子门禁；非 READY 不写业务数据；READY 可写且新进程可读；帮助和参数错误先于 runtime，v2 错误 envelope 保持一致。 |
| CR-03 / P2 | Q1 的 `repair_required` 任务在原操作提交已恢复可证明后，仍永久阻塞同目标。 | root lease 内只协调原 operation 的已验证提交事实；同步/异步均能继续，patch/proof 至多一次、Provider 零新增；缺失、模糊或不匹配的证明仍阻挡同目标，独立任务保持有限进展。 |

CR-01 验收属于 packaging-contract：使用实际资源清单和冻结加载器布局，不声称已经重新
构建完整 PyInstaller 包，也不改变历史 held Artifact 的 release/compliance hold。
CR-02 必须执行受 G0 保护的原生模块路径，不能由测试入口先改派到 `src.main` 来替代它。
CR-03 只协调已有提交，不授权对未知外部影响或缺失产物重新调用 Provider。

## 2. 实现边界

CR-01 在 `runtime-resources.json` 明确两个代码输入，`pkv.spec` 在 Analysis 前捕获源码
SHA256，之后再次核对 SHA256 与 `Analysis.pure` 中模块的实际来源，再生成
`src/migration-code-fingerprints.json`。不复制原始 `.py`。冻结运行时严格验证版本、完整
资源集合、重复键、字段、摘要格式和大小；源码及 wheel 仍读取源码。最终迁移清单沿用
相同逻辑资源名与规范序列化，故同一构建输入的计划身份不因布局变化而改变。指纹漂移
使已有确认失效；不跳过代码绑定或回退到仅 SQL 的清单。

CR-02 的模块 `__main__` 委托 `src.main.main`，使用同一命令树和门禁。`src.cli` 对 `cli`
改为惰性导出，保留 Click Group 与缺依赖 fallback，避免包初始化先导入被 `-m` 执行的
模块，产生污染 JSON stderr 的重复导入警告。`tests/offline_entrypoint.py`、网络 guard
和配置隔离均未修改；native 黑盒使用其 `python` 路径实际执行模块。

CR-03 把 repair 纳入只协调、不领取执行 claim 的恢复候选。Store 在 root lease 内的
同一事务比较当前 task/fence、journal 与原 operation 的 SQLite proof：action、target、
path 及前后 revision 必须匹配。验证成功直接推进 `core_committed`，递增 fence、清除
claim/error/backoff，不增加 attempt_count；随后复用既有 completion/handoff。存在
互相矛盾的 journal revision 时拒绝；既有 `journal_created` 证明采用路径允许省略
resulting 字段，但仍必须与 SQLite 的 resulting revision 精确绑定。无证明或读取失败
保留 repair/退避，同目标不能越过，独立目标可以继续。没有重执行 patch 或新增 Provider attempt。

## 3. 运行与证据

所有测试经 `scripts/run-test.ps1`，仅使用本次独立 `.data-test`、合成数据及受控故障注入。
组件计数不相加冒充一次全仓运行。完整命令、精确 nodeid、日志 SHA256 与运行边界见
[`w1_w3_review_repairs.v1.json`](../../tests/contracts/w1_w3_review_repairs.v1.json)。

| 运行 | 实测结果 | 证据边界 |
| --- | --- | --- |
| CR-01 原缺陷红例 | 2 failed，4.44s | fresh/READY 冻结布局均缺资源；另 10 项未选。 |
| CR-01 加迁移相关回归 | 50 passed、1 skipped，69.99s | 含 22 个新例与原 23 项合成迁移；后续 spec 补 namespace 模块兼容。 |
| CR-01 最终打包合同 | 28 passed、1 skipped，16.44s | 22 新例全部通过，实际 spec 资源生成、manifest 与 payload policy 回归通过；无完整 Artifact 构建。 |
| CR-02 精确红例 → 新增全绿 | 6 failed / 7 passed，119.32s → 15 passed，117.53s | 后加两个 package 导出兼容控制；unit 执行 helper 去除测试缓存干扰。九个原生模块黑盒的断言未修改。 |
| CR-02 既有相关回归 | 159 passed，300.50s | W2 leaf/runtime 门禁、W1 CLI 三状态、W3 CLI 和 offline runtime 五文件。 |
| CR-03 核心红例 → 转绿 | 3 failed，17.86s → 3 passed，18.33s | sync/async/recovery 下真实提交与短暂证明读取失败。 |
| CR-03 组件及追加校验 | 43 passed，305.63s；追加 2 passed，17.35s 与 1 passed，11.06s | 43 项在最后 journal revision 等值校验前运行；该矛盾先补 1 failed 红例，再修复并验证兼容。最终同树结果以下一行为准。 |
| 最终联合 14 文件 | **136 passed、5 failed、1 skipped、0 errors**，583.43s，exit 1 | 收集 142 项，全部 56 新例通过；R4 content/patch/coordinator、真实 R4 CLI/MCP、W3 Outcome CLI/MCP、原 W1 embedding/outcome 及 Kernel boundary 均纳入。 |
| 静态与独立复审 | `ruff --select E9,F63,F7,F82`、`git diff --check` 通过 | 改动范围无严重静态错误；独立复核没有剩余可确认问题。 |

最终联合的五个失败严格等于既定阶段外缺口：R-02 发布争锁、R-06 日/月预算恢复、
R-04 旧无版本 approve/return。没有 skip/xfail 或默认过滤这些红项。唯一联合 skip 是
`test_journal_constructor_rejects_redirected_ancestor`：本机不允许创建目录 symlink。
打包组件另有既有的 `test_real_analysis_and_payload_have_no_build_only_module_members`
跳过，原因为未提供真实 PyInstaller closure；该项不计为通过，也不并入联合运行计数。

本轮没有重跑完整 P0、完整 MCP coverage 或完整 PyInstaller 构建。W3 原有全仓/MCP
证据仍只属于 `b725deb` 历史快照；本轮结果只说明上述修复和选定相关回归通过。
CLI 最初诊断轮的 missing fixture 未预热本地分词缓存，导致 readiness 被其他问题覆盖；
修正 fixture 后重新取得精确红例，诊断日志保留但不作为正式红转绿证明。

## 4. 冻结与范围核对

产品 7 文件和新增测试 4 文件的清单为
`.data-test/w1-w3-review-fixes-20261025-evidence/reports/source-tests.sha256`，SHA256：
`2086722c512f0e62fb61a1d28c3c310ef41b2ac2632e99b42f839daf5c8b8ba2`。
联合运行覆盖全部 56 个新增 nodeid；包含最后补充的 `unreadable_proof`，不是组件拼合结果。
清单不含文档或验收 JSON，避免自引用；结束后再次逐文件核对。

原 W1 红例、W1/W2/W3 历史 JSON、SQL 013/014、SDK/API/capability、Embedding 生命周期、
ledger/grant 和离线测试入口均未修改。Q1 只补已有提交的本地证明协调；没有引入新的
公开恢复操作、费用重试或 W4/W5 实现。用户数据、真实 Provider 与 held Artifact 均未操作。

## 5. 关联历史记录

- [W2 身份、队列与 CLI 门禁修复记录](./W2-身份队列与CLI门禁修复记录-2026-10.md)
- [W3 Outcome 一致性与版本审批实施记录](./W3-Outcome一致性与版本审批实施记录-2026-10.md)
- [生命周期一致性分阶段计划](./生命周期一致性修复设计与分阶段计划-2026-10.md)
