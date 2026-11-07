# W4：Embedding 继续发布与预算恢复实施记录（2026-10）

实施日期：2026-10-25–26；本次记录更新：2026-10-26。当前状态：**W4 范围验收完成**。
Embedding v2 已实现并声明，ER-01–10 / MIG-06–09 与新增 363 项 W4 用例全部通过；MCP、
Phase C 与致命 Ruff 已通过。P0 原轮 exit 1 保留；三轮按节点去重后为 4133 passed、
21 skipped、2 项 W5 普通失败、0 errors，共 4156 selected，不称单轮全仓全绿。

本轮依据 [W0–W5 分阶段计划](./生命周期一致性修复设计与分阶段计划-2026-10.md)、
[v2 接口冻结合同](../specs/interfaces/生命周期一致性v2接口冻结合同-2026-10.md)及
[迁移冻结方案](../specs/database/生命周期一致性迁移冻结方案-2026-10.md)实施 W4。
本记录只处理 R-02 的 D 争锁/继续发布、R-06 的预算恢复及相应公开协议、合成迁移和回归。
W5 的 API-major-2、旧无版本 Outcome 拒绝及 MIG-10 未推进；R-04 仍是兼容期缺口。
精确 nodeid、完整命令、退出码、日志 SHA 和各轮源文件清单统一见
[W4 机器证据登记](../../tests/contracts/w4_lifecycle_repairs.v1.json)；本页不修改历史 W1–W3 登记。

## 1. 版本、范围与兼容边界

| 项目 | W4 合同 / 当前状态 |
| --- | --- |
| 本地 SDK / Kernel API | `0.10.0` / `1.0.0`；保留全部 v1 符号、DTO 形状与 capability |
| Embedding v2 SDK | 同一 `pkv_kernel.embedding` 模块增加 8 个函数、7 个 opaque handle；封闭签名按冻结 §3 |
| CLI | 增加 `embedding inspect-v2 / rebuild-v2 / operation-v2 / publish-v2` 四个叶子 |
| MCP | 增加 4 只读 + 2 写入 Tool，当前总数 41（23 只读 + 18 写入）；仍为 stdio-only |
| Capability | `kernel.embedding-rebuild.v2` 已实现、声明并完成 W4 范围验收；按冻结 §6 作为兼容增量保留 v1 |
| 持久化 | manual ledger 3、private grant 2、sealed proof `pkv.embedding.publish-ready.v1` |
| SQLite | 保持 W3 `014` / schema `1.2.8`；不改 013、不借 Outcome migration 升级 JSON ledger |
| 验证授权 | 仅 wrapper、合成 `.data-test`、进程外随机 numeric-loopback deterministic Provider；无真实用户数据/凭据/迁移 |
| 发布状态 | 不构成 wheelhouse、完整 PyInstaller、Artifact、安装器、PyPI 或正式发布验收；held candidate 和 compliance hold 不变 |

v2 的完整函数为 `inspect / plan / confirm / execute_embedding_rebuild_v2`、
`inspect_embedding_operation_v2` 以及 `plan / confirm / execute_embedding_publication_v2`。
七种 handle 为 `EmbeddingRebuildInspectionV2`、`EmbeddingRebuildPlanV2`、
`EmbeddingRebuildConfirmationV2`、`EmbeddingRebuildExecutionV2`、
`EmbeddingOperationInspectionV2`、`EmbeddingPublicationPlanV2`、
`EmbeddingPublicationConfirmationV2`。它们只由 façade 创建；展示 DTO 不可恢复为执行能力。

## 2. 实施规则与验收口径

1. **区分 A/D 争锁。** A 未接受 operation 时返回 `write_busy`，不创建 reservation、Provider、
   stage 或业务 audit。C 已完整封存而 D 取锁失败时返回 `embedding_publication_pending`，
   携带原 operation、awaiting_publish、Provider completed、publish-only required 的安全 details；
   不在无 lease 时写共享 ledger。v1 保留旧响应形状并透传 pending 信息。
2. **原操作只继续发布。** 新进程只读复核 ledger、proof、grant 和 pointer，随后展示当前
   publication plan；明确确认后在 lease 内采用完整封存产物，零 Provider、零新 reservation。
   grant 过期禁止续写，但不单独否决有效期内已封存产物的只读采用。无完整 proof 不自动重试。
3. **保存 pointer 成功事实。** CAS 已提交但 audit/ledger 完成未落盘时返回 ready 与 pending
   警告；后续只对齐原 operation 的 completion，不重复 Provider 或 CAS。已完全完成的操作
   不再提供新的发布资格。漂移、冲突或无法核验的证据保持明确 stale/repair。
4. **重新检查预算。** 网络 plan 绑定 policy timezone/local day/local month；material replay
   排除 force 和期间，仍在 active/awaiting_publish 的同一工作跨期不能重复执行。零 Provider、
   零 reservation 的预算拒绝不消费资格，重新确认后按当前日/月上限检查。
   policy fingerprint 仅绑定身份/确认/replay，修改 cap/model/endpoint 不清空期间成本；历史
   reservation 的原时区日/月与当前预算期间转为 UTC 半开区间后有重叠，就保守计入整笔。
   没有精确调用时刻时不分摊，不因 fingerprint/timezone 变化排除 reserved/settled 用量。
5. **费用事实不丢失。** publish-only 沿用原 reservation/期间；partial usage 保存已报告调用
   的已知合计作为下界，完全未知维度为 null。跨进程保守结算取 reserved 与已知 reported
   total 的较大值，保留已知超额，不用 0 冒充未知或重复计费。
6. **迁移只随明确 mutation。** legacy 1/2 只读严格分类，plan 绑定原始文件 SHA 和计数；
   A 内重新验证后原子升级 ledger 3。历史已结算 completed/failed 保留费用事实；未知未完成
   记录阻断新网络工作；native unknown 未结算也阻断该根的新网络工作，不能用不同 material
   绕过。legacy schema 1 缺 public_plan_id，迁移/公开 original_plan_id 保持
   null，禁止用私有 Core plan_id 代填。旧 v1 写入口不隐式升级 legacy ledger。

## 3. ER 验收矩阵

下表均已完成 **W4 范围验收**。表内代表节点和 run ID 对应第 5 节的真实日志；
完整 selector（含参数化实例）由 JSON 的 `cases` 保存。历史 W2、组件轮与最终运行分别登记，
重叠节点不相加，运行中或混合工作树的结果不升级为最终同树证据。

| Case | 代表 pytest 节点 / 实际边界 | 组件 run ID |
| --- | --- | --- |
| ER-01 | `tests/unit/test_w4_embedding_public.py::test_real_v2_confirmation_is_bound_to_root_before_provider_use`：实际 SDK 两根 plan 不同，交叉 confirmation stale；W1/W2 的 v1 根绑定证据另保留。 | `sdk-final`、`w1-embedding` |
| ER-02 | `tests/unit/test_w4_embedding_public.py::test_real_public_plan_uses_frozen_hash_material_and_reads_do_not_write`：完整冻结 hash 材料与零写；历史 `test_w2_embedding_identity.py::test_er02_full_snapshot_change_invalidates_public_confirmation` 验证 snapshot 原字节漂移。下述 CLI/MCP 节点在独立宿主重算 publication identity。 | `sdk-final`、`blackbox-component`；W2 历史记录 |
| ER-03 | `tests/unit/test_w4_embedding_core.py::test_public_v2_a_real_writer_contention_has_no_operation_or_side_effects`：真实竞争 writer 下 A 返回 write_busy，账本/stage/snapshot/audit 不变、Provider factory 零调用；释放后配对成功。 | `core-a-busy` |
| ER-04 | `tests/unit/test_w4_embedding_publication.py::test_lookup_and_plan_after_d_busy_are_read_only_and_publish_reuses_reservation`；真实 CLI/MCP 的 `test_*_sealed_pending_restarts_across_day_and_month_without_provider`：C seal、D 争锁、awaiting_publish 只读投影、显式 publish-only。 | `publication-repaired`、`w1-embedding`、`blackbox-component` |
| ER-05 | 同上真实 CLI/MCP 节点先退出原宿主，再从新进程继续；`tests/unit/test_w4_embedding_expiry_execution.py::test_public_publication_adopts_expired_legitimate_seal_without_renewing_grant` 单独验证 grant 到期后的实际 SDK 发布，原 marker/proof 字节不变。 | `blackbox-component`、`grant-final`、`expiry-execution` |
| ER-06 | `tests/unit/test_w4_embedding_publication.py::test_snapshot_drift_rejects_publication_and_conservatively_settles`、`test_current_contract_drift_keeps_original_proof_valid_but_refuses_publication`、`test_in_process_publication_handle_detects_config_file_change_without_old_hmac_replay`；proof 字段/artifact 篡改由 stage-proof 族验证。 | `publication-repaired`、`publication-config`、`grant-final` |
| ER-07 | W1 `test_r06_er07_budget_rejection_can_be_reauthorized_after_period_recovery[day/month]`；`tests/unit/test_w4_embedding_ledger.py::test_monthly_cap_remains_after_day_rollover_and_resets_only_next_month`；真实 CLI/MCP `test_*_budget_period_recovery_requires_new_public_confirmation[day/month]`。 | `ledger-final`、`blackbox-component` |
| ER-08 | `tests/unit/test_w4_embedding_ledger.py::test_same_material_remains_consumed_across_month_boundary`、`test_unsettled_effect_blocks_different_material_after_restart`；publication `test_sealed_work_is_pending_in_network_inspection_and_blocks_force`；真实 CLI/MCP 跨月发布沿用原 reservation/usage，force 不增加 Provider 请求。 | `ledger-final`、`publication-repaired`、`blackbox-component` |
| ER-09 | `tests/unit/test_w4_embedding_ledger.py::test_unknown_effect_requires_live_no_start_proof_to_release`、`test_reported_overrun_counts_before_final_settlement_after_restart`；publication `test_partial_provider_usage_survives_seal_and_settles_known_overrun`。ledger seal seam 不冒充真实 Provider 闭环。 | `ledger-final`、`publication-repaired` |
| ER-10 | publication 的 post-CAS audit/ledger failure、pointer response-loss 节点；`tests/unit/test_w4_embedding_recovery_process.py::test_completion_recovers_after_real_worker_exit` 的五个边界使用 15 个独立 worker。该 worker 使用空 source，非零 Provider 请求 oracle 由真实黑盒另证。 | `publication-repaired`、`recovery-process`、`publication-import-diagnostic` 中已通过的 audit 节点 |

真实公开协议的完整节点为：

- `tests/blackbox/test_w4_embedding_cli_blackbox.py::test_cli_sealed_pending_restarts_across_day_and_month_without_provider`
- `tests/blackbox/test_w4_embedding_mcp_blackbox.py::test_mcp_sealed_pending_restarts_across_day_and_month_without_provider`
- `tests/blackbox/test_w4_embedding_cli_blackbox.py::test_cli_budget_period_recovery_requires_new_public_confirmation[day]` / `[month]`
- `tests/blackbox/test_w4_embedding_mcp_blackbox.py::test_mcp_budget_period_recovery_requires_new_public_confirmation[day]` / `[month]`

每种协议的 sealed 场景精确完成 document/chunk 两次 Provider 请求，publish-only 增量为 0；
每个日/月恢复场景前后两次 rebuild 合计四次请求，被拒的预算、stale 和未授权网络请求增量为 0。
外置时钟只推进预算时间，不改 lease/grant/global clock。grant expiry 的实际发布是独立
`expiry-execution` 补充，未包含在原 438 项发现轮中，不能以“跨日”替代 grant 到期证据。

## 4. 合成迁移验收矩阵

MIG-06–09 均已取得以下 **合成组件证据并完成 W4 范围验收**；没有执行真实用户数据迁移。

| Case | 代表 pytest 节点 / 实际边界 | 组件 run ID |
| --- | --- | --- |
| MIG-06 | `tests/unit/test_w4_embedding_ledger.py::test_legacy_readonly_classification_preserves_original_facts`、`test_explicit_upgrade_is_atomic_and_old_confirmation_becomes_stale`、`test_upgrade_cas_binds_raw_bytes_not_just_equivalent_json`、`test_legacy_underreported_settlement_is_not_a_paid_terminal_exception`；extended 的 legacy completed reconcile 与 unbound stage 拒绝。 | `ledger-final`、`core-extended` |
| MIG-07 | ledger `test_upgrade_replace_interruption_retains_complete_old_or_new_document`、`test_upgrade_rechecks_original_bytes_at_atomic_replace_boundary`；stage-proof `test_interrupted_exclusive_seal_revokes_capability_without_adoptable_proof`；publication `test_seal_fsync_response_loss_recovers_exact_proof_without_second_provider`。均为明确文件边界故障注入。 | `ledger-final`、`grant-final`、`publication-repaired` |
| MIG-08 | `tests/unit/test_w4_embedding_stage_proof.py::test_v2_marker_and_seal_have_exact_canonical_raw_digests`、`test_seal_revokes_every_later_write_and_cannot_be_overwritten`、`test_seal_outside_original_grant_lifetime_is_refused` 与 nofollow 网关；expiry-execution 的实际 SDK 到期采用单列。 | `grant-final`、`expiry-execution` |
| MIG-09 | ledger `test_native_stage_effect_proof_and_original_period_survive_restart`、`test_policy_changes_do_not_erase_tokens_already_spent_in_current_period`、`test_timezone_change_conservatively_counts_overlapping_original_period`；partial usage、真实 worker exit 与真实 CLI/MCP 跨期发布分别证明费用、完成对齐、公开协议。 | `ledger-final`、`publication-repaired`、`recovery-process`、`blackbox-component` |
| MIG-10 | API-major-2 与旧 Outcome 拒绝 | W5 未实施，本轮范围外 |

历史 MIG-01–04 属于 W3 Outcome 合成迁移，MIG-05 属于既有 Store 合同；原证据保持不变。

## 5. 运行证据与最终核对

所有产品执行由 `scripts/run-test.ps1` 或其 `test-conda.ps1` 调用链建立独立 `.data-test` 根。
下列运行各自独立，**通过数不相加**。原 R-02 一项、R-06 的 day/month 两项红例已在
`w1-embedding` 与 `ledger-final` 转绿；ER-08 原本就是 W1 绿色控制，不称新红转绿。

### 5.1 组件命令与结果

下表 pytest 命令统一以此精确前缀执行，`DataRoot`、目标与末尾参数按表中拼接；完整展开的
命令、源目录和每份日志 SHA 同时保存在 JSON 对应 run。默认工作目录为本仓库 E 盘根。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run-test.ps1 -Direct -DataRoot <DataRoot> pytest <目标和参数>
```

| run ID / DataRoot | 目标和参数 | 实际结果与日志 |
| --- | --- | --- |
| `w1-embedding` / `.data-test/w4-core-w1` | `tests/unit/test_w1_embedding_regressions.py -q --tb=short` | 6 passed / 24.73s，exit 0；[日志](../../.data-test/w4-20261025-evidence/reports/component-core-w1.log) |
| `ledger-final` / `.data-test/w4-ledger-e` | `tests/unit/test_w4_embedding_ledger.py tests/unit/test_embedding_rebuild_ledger.py tests/unit/test_w1_embedding_regressions.py -q --tb=short` | 85 passed / 48.04s，exit 0；71 新 ledger + 8 既有 ledger + 6 W1，包含上一行；[日志](../../.data-test/w4-20261025-evidence/reports/component-ledger-final.log) |
| `core-existing` / `.data-test/w4-core-existing` | `tests/unit/test_embedding_lifecycle.py -q --tb=short` | 26 passed / 210.37s，exit 0；[日志](../../.data-test/w4-20261025-evidence/reports/component-core-existing.log) |
| `core-component` / `.data-test/w4-core-copy-final` | `tests/unit/test_w4_embedding_core.py -q --tb=short` | 5 passed / 36.41s，exit 0；早于新增 ER-03 节点；[日志](../../.data-test/w4-20261025-evidence/reports/component-core-copy-final.log) |
| `core-a-busy` / `.data-test/w4-core-a-busy` | `tests/unit/test_w4_embedding_core.py::test_public_v2_a_real_writer_contention_has_no_operation_or_side_effects -q --tb=short` | 1 passed / 13.07s，exit 0；[日志](../../.data-test/w4-20261025-evidence/reports/component-core-a-busy.log) |
| `core-extended` / `.data-test/w4-core-extended` | `tests/unit/test_w4_embedding_publication_extended.py -q --tb=short` | 5 passed / 71.31s，exit 0；[日志](../../.data-test/w4-20261025-evidence/reports/component-core-extended.log) |
| `publication-repaired` / `.data-test/w4-publication-r4` | `tests/unit/test_w4_embedding_publication.py -q --tb=short` | 19 passed / 192.54s，exit 0；[日志](../../.data-test/w4-20261025-evidence/reports/component-publication-r4.log) |
| `publication-config` / `.data-test/w4-publication-config` | `tests/unit/test_w4_embedding_publication.py -k 'current_contract_drift or in_process_publication_handle' -q --tb=short` | 2 passed / 19 deselected / 22.45s，exit 0；后加的配置漂移节点；[日志](../../.data-test/w4-20261025-evidence/reports/component-publication-config.log) |
| `publication-import-diagnostic` / `.data-test/w4-publication-r3` | `tests/unit/test_w4_embedding_publication.py tests/unit/test_w4_embedding_completion_audit.py -q --tb=short` | 47 passed / 1 failed / 151.54s，exit 1；32 个 audit 节点全部通过，唯一失败为测试 import 拼写，后续修正；**本轮整体不称通过**；[日志](../../.data-test/w4-20261025-evidence/reports/component-publication-import-diagnostic.log) |
| `recovery-process` / `.data-test/w4-recovery-process-final` | `tests/unit/test_w4_embedding_recovery_process.py -vv --tb=short` | 5 passed / 144.55s，exit 0；15 个独立 worker、空 source；[日志](../../.data-test/w4-20261025-evidence/reports/recovery-process-final.log) |
| `grant-final` / `.data-test/w4-grant-final2` | `tests/unit/test_w4_embedding_stage_proof.py tests/unit/test_generation_stage.py -q --tb=short` | 53 passed / 31.21s，exit 0；50 新 proof + 3 既有 grant；[日志](../../.data-test/w4-20261025-evidence/reports/component-grant-final.log) |
| `expiry-execution` / `.data-test/w4-expiry-execution` | `tests/unit/test_w4_embedding_expiry_execution.py -q --tb=short` | 1 passed / 12.65s，exit 0；原冻结后独立补充，未改产品；[日志](../../.data-test/w4-20261025-evidence/reports/component-expiry-execution.log) |
| `blackbox-component` / `.data-test/w4-blackbox-final1` | `tests/unit/test_w4_embedding_clock_harness.py tests/blackbox/test_w4_embedding_cli_blackbox.py tests/blackbox/test_w4_embedding_mcp_blackbox.py -vv --tb=short` | 52 passed / 369.89s，exit 0；46 clock guard + 6 真实黑盒。运行期间调整了测试入口 clock 安装次序，最终同树轮优先；[日志](../../.data-test/w4-20261025-evidence/reports/component-blackbox-final.log) |
| `sdk-final` / `.data-test/w4-sdk-20261025-release-candidate` | `tests/unit/test_w4_embedding_public.py tests/unit/test_kernel_wrapper_boundary.py tests/unit/test_local_wheel_contract.py -q --tb=short` | 65 passed / 57.05s，exit 0；覆盖 SDK、Wrapper boundary 与 source-free wheel；目录命名不改变 release hold；[日志](../../.data-test/w4-20261025-evidence/reports/component-sdk-final.log) |
| `mcp-pending-repaired` / `.data-test/w4-adapter-pending-fixed` | `tests/unit/test_w4_embedding_adapters.py tests/unit/test_w2_cli_leaf_gate.py tests/unit/test_w3_packaging_entrypoints.py tests/integration/test_mcp_functional.py::TestToolRegistration -q --tb=short` | 123 passed / 7.37s，exit 0；修复真实 v1 MCP pending details 投影缺陷后的回归；[日志](../../.data-test/w4-20261025-evidence/reports/adapter-pending-fixed.log) |
| `w3-public-oracle-repaired` / `.data-test/w4-sdk-20261026-w3-public-regression` | `tests/unit/test_w3_outcome_public.py -vv --tb=short` | 76 passed / 22.45s，exit 0；旧 W3 阶段 capability 断言一行修正后的完整文件回归，产品无变更；[日志](../../.data-test/w4-20261025-evidence/reports/w4-sdk-w3-public-regression-20261026.log) |

source-free wheel 经正式 `tzdata` 依赖声明，并使用受信 Conda 2026c 的 606 个文件逐 size/SHA
验证后作为离线依赖 fixture；包 SHA 为 `52adc4b0a9cce2ffb8ad319d54b7c940ae7961f7caf7ce6851eec4b3f542c368`。
它证明本地安装后的公开 SDK/资源边界，不证明 PyPI resolver、网络安装、依赖闭包或发布。

### 5.2 最终门禁与工作树边界

产品修复后的验证清单为 [final-source-tests-verified.sha256](../../.data-test/w4-20261025-evidence/reports/final-source-tests-verified.sha256)，
SHA `efb7f24ba76d6184408f1a8553065e973125b53b765433385fe5de54bc94c924`。
旧 MCP 测试 oracle 最终修正后的 [accepted 清单](../../.data-test/w4-20261025-evidence/reports/final-source-tests-accepted.sha256)
SHA 为 `1a4c3f9bbdcf3fdcb5f820cd81fe92990cfcd00184ab0dcdb318843d97d62026`。
[逐文件对照](../../.data-test/w4-20261025-evidence/reports/acceptance-source-delta.json)确认两个清单各
456 项，其中 454 项相同，只有两份旧 MCP 黑盒测试变化，**产品变化为 0**。P0 使用原 verified
C 盘树执行，accepted 全 MCP 使用 E 盘树；不能把两树称为同一轮运行。
随后 W3 public 测试修正形成 [complete 清单](../../.data-test/w4-20261025-evidence/reports/final-source-tests-complete.sha256)，
SHA `72136533e0c0bd99cc8adf996a09d094b52090e65225004dc15c38b57057333a`。
[三文件对照](../../.data-test/w4-20261025-evidence/reports/complete-source-delta.json)确认相对 C 盘原树
仍为 456 项，其中 453 项相同，恰好三份测试文件变化、产品变化为 0。第三份为
`tests/unit/test_w3_outcome_public.py`；accepted MCP 的清单和成功结果原样保留，不追溯改名为 complete 轮。
它与最初 447 项清单、加入 evals/expiry 补充的 extended 清单分开；原 438 项联合是混合
工作树发现轮，`435 passed / 3 failed`（一个已修 MCP 产品缺陷、两个 W5 普通失败），不作为
最终同树结果。原日志与完整选择命令保存在 JSON 的 `combined-discovery`，不将其通过节点
加到组件计数或最终结果上。

| 门禁 | 当前实际结果 | 证据 / 限制 |
| --- | --- | --- |
| Phase C | **passed**：16/16 tasks、151/151 checks、overall 与全部维度 1.0，thresholds met，exit 0 | `j-69lazu`，[验证日志](../../.data-test/w4-20261025-evidence/reports/final-phase-c-verified.log)、[报告](../../.data-test/w4-20261025-evidence/reports/phase-c-verified.json)；既有固定 gold taskset，不称 151 个 pytest 用例或新增 W4 taskset |
| 致命 Ruff | **passed**：`E9,F63,F7,F82`，exit 0 | `j-noqaur`，[complete 日志](../../.data-test/w4-20261025-evidence/reports/final-ruff-complete.log)；已覆盖最终三份测试 oracle 修改。verified/accepted 轮另存历史日志 |
| P0 原轮 | **exit 1**：4129 passed / 6 failed / 21 skipped / 219 deselected / 37 warnings，4071.72s，0 errors | `j-n812cv`，[原日志](../../.data-test/w4-20261025-evidence/reports/final-p0.log)；C 盘 verified 树完整执行 4156 selected。六项失败为四项旧测试 oracle 与两项 W5 普通失败；原轮不改记通过 |
| 三轮节点闭环 | **W4 范围验收完成**：4156 selected = 4133 passed + 21 skipped + 2 项 W5 普通失败，0 errors | 原 P0、accepted MCP 与 W3 public 完整文件按精确 nodeid 去重；后两轮均为 P0 子集，三份变动测试文件的全部节点覆盖，四项旧 oracle 失败关闭；新增 W4 363 项全部通过 |
| MCP coverage | **passed**：908 passed / 5 deselected / 1078.87s，exit 0；覆盖率 **95.64%** | accepted 全量 `j-6uy0f4`，[最终日志](../../.data-test/w4-20261025-evidence/reports/final-mcp-accepted.log)；2178 statements / 95 miss，达到 95% 阈值。独立 collect `j-796q66` 确认相同 marker 下 913 总项、908 selected / 5 deselected；首轮失败另留历史记录 |
| 独立 review / 文档与证据核对 | 组件问题已修复并独立复核，节点闭环核验通过 | 冻结清单、最终节点集和证据 SHA 见机器登记；原轮、修正轮与最终去重状态分别保留 |

Phase C、Ruff 和最终 MCP 的完整命令如下；先前轮次仍保留为历史组件/诊断证据：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run-test.ps1 -Direct -DataRoot .data-test/w4-phase-c-verified-20261025 python -m evals.mcp_quality --output .data-test/w4-phase-c-verified-20261025/reports/phase-c.json --enforce-thresholds
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run-test.ps1 -Direct -DataRoot .data-test/w4-static-complete ruff check src pkv_kernel scripts tests --select E9,F63,F7,F82 > .data-test/w4-20261025-evidence/reports/final-ruff-complete.log 2>&1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/test-conda.ps1 -EnvironmentName py311-private -Suite MCP > .data-test/w4-20261025-evidence/reports/final-mcp-accepted.log 2>&1
```

Phase C 日志 SHA 为 `fcda6f651b0a2804d473353efd9f05875d3849ef1e57b0bdde9482e87dccddf0`，
报告 SHA 为 `cd2c0d5004b6aed1c5c01c40a5b24d02a8d4c47e069971e29a344bfe704d9583`；Ruff 日志
SHA 为 `0caecc10af3c8b3b28df38c0cc6a74129cf9359db89c63a714e342e2af673281`。
MCP 最终日志 SHA 为 `948efc51f63572e22bd89b5d0a0967e84734c62fe5a61946755f3a84e67861f1`，
[独立 collect 日志](../../.data-test/w4-20261025-evidence/reports/final-mcp-accepted-collection.log) SHA
为 `c7ff547641d7878c8878b7bcf7aae6960af39c98fbab840ba9b8a4af8e7298b6`。collect 不执行测试，
不能另加 908 次通过。成功 MCP suite 已自动清理本轮 DataRoot，归档日志与 frozen manifest 保留。

首轮 C 盘 MCP `j-ibrbx5` 为 906 passed / 2 failed / 5 deselected / 905.29s，exit 1；95.64%
覆盖率达标但测试门禁失败，[原日志](../../.data-test/w4-20261025-evidence/reports/final-mcp.log)保留。
两项失败是旧测试 oracle：一项仍要求 manual rebuild 后整个 staging 为空，
与 W4 保留原 sealed stage 的合同冲突；另一项 discovery 缺少六个新增 v2 Tool。E 盘只改测试
断言，产品字节及当时正在运行的 C 盘 P0 树不变。`old-mcp-oracle-verified` 的 4 passed / 134.92s
与 `mcp-discovery-oracle-verified` 的 1 passed / 13.45s 保留为中间修正证据。补齐 Q2 patchspool/
vector-root 精确不变断言后，`old-mcp-oracle-preserved`（`j-v30fto`）为 **4 passed / 137.30s，
exit 0**，独立复核确认原断言未削弱，见 [最终 oracle 日志](../../.data-test/w4-20261025-evidence/reports/old-mcp-oracle-preserved.log)。
完整命令如下，日志 SHA 为 `2e4d1154e5314158b0151b3e826a82e436f5309e6f126024b313eb0c6cad04fb`：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run-test.ps1 -Direct -DataRoot .data-test/w4-old-mcp-oracle-preserved pytest tests/blackbox/test_embedding_rebuild_mcp_blackbox.py tests/blackbox/test_r4_mcp_fullflow.py -vv --tb=short
```

此前启动后终止的 E 盘 MCP 运行不登记终态通过/失败数，不与首轮或 accepted 轮相加。

P0 进一步暴露旧 W3 阶段的两项 capability oracle；独立复现为 2 failed / 74 deselected，
exit 1。`test_handshake_keeps_major1_compatibility_and_sdk_bounds` 仍要求 Embedding v2 不在
能力集合，与 W4 的 API-major-1 兼容增量冲突。仅把该断言 `not in` 改为 `in`，API 1、全部
v1 能力和 SDK 上下界断言原样保留；完整文件 `j-7umpgs` 已取得 76 passed / 22.45s，exit 0，
日志 SHA 为 `6cdb3866a2e0cbfdb1f5cd057a84996e62b10a82fbc0223a9f14ae3bad63de3f`。这是测试
oracle 更新，没有改产品或推进 W5；三轮严格节点集合核验已通过。

P0 原轮实际失败节点严格为以下六项，没有 error：

- `tests/blackbox/test_embedding_rebuild_mcp_blackbox.py::test_mcp_stdio_public_rebuild_activates_and_searches_after_new_server`
- `tests/blackbox/test_mcp_blackbox.py::TestEndToEnd::test_discovery_then_invoke`
- `tests/unit/test_w3_outcome_public.py::test_handshake_keeps_major1_compatibility_and_sdk_bounds[None]`
- `tests/unit/test_w3_outcome_public.py::test_handshake_keeps_major1_compatibility_and_sdk_bounds[1]`
- `tests/unit/test_w1_outcome_regressions.py::test_r04_unversioned_review_cannot_change_unreviewed_replacement[approve]`
- `tests/unit/test_w1_outcome_regressions.py::test_r04_unversioned_review_cannot_change_unreviewed_replacement[return]`

前四项分别已有 accepted 完整 MCP 908 项和 W3 public 完整文件 76 项的绿色证据；不能把
这些通过数直接加到原轮 4129 上。21 个 skip 属于既有平台/权限分支及缺真实 PyInstaller
closure 输入，不是为本轮红例添加的 skip/xfail。原 P0 在
`C:/Users/fdch0/.codex/worktrees/pkv-w4-validation-20261025` 执行的完整命令为：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/test-conda.ps1 -EnvironmentName py311-private -Suite P0 > E:/repos/personal/personal-knowledge-vault/.data-test/w4-20261025-evidence/reports/final-p0.log 2>&1
```

原始混合编码日志 SHA 为 `64c60cf8150a9a14b7d33494955ddd0088ad24937bec232acbb685d8e9a2cb86`。
[ASCII 审阅副本](../../.data-test/w4-20261025-evidence/reports/final-p0.ascii-review.log)仅将非 ASCII
字节替换以便审阅，SHA 为 `12346351f1ec5cb07dc3dad2394b8658b9aeb4ef7b0945df4c3587c2b813fe50`；
它不是新的运行、不能替代原日志；ASCII 节点名、计数和覆盖率文本保持一致。

W5 保留两个正常收集的失败节点：
`tests/unit/test_w1_outcome_regressions.py::test_r04_unversioned_review_cannot_change_unreviewed_replacement[approve]`
及 `[return]`。不添加 skip/xfail；包含它们的 P0 不称“全绿”。最终 MCP 已按 accepted 清单
登记通过；P0 原 exit 1 保留，三轮严格去重最终为 **4133 passed / 21 skipped / 2 项 W5 普通失败 /
0 errors**。MCP 908 项与 W3 public 76 项均为 P0 的 4156 selected 子集，不能相加为新的通过数。
新增 W4 精确 363 项全部通过，其中 `test_w4_embedding_public.py` 为 50 项；组件与最终轮重叠不重复计数。

[节点闭环报告](../../.data-test/w4-20261025-evidence/reports/acceptance-node-closure.json) SHA 为
`66bc4a1e5d12b5a09533510ec8a71b68f8efc8e70119b9037e7c1c0d3f60b734`；
[最终验收摘要](../../.data-test/w4-20261025-evidence/reports/final-gate-accepted-summary.json) SHA 为
`88f382707a25f088d1683e84eff2240a300ce1d33dd123137d8dd45b77597d94`。
两份报告将最终完整文件节点覆盖、三份测试差异和原日志关联到各自清单；它们不改写历史运行的退出码。
[原 P0 逐节点结果](../../.data-test/w4-20261025-evidence/reports/final-p0-node-outcomes.json)由 4156 个
进度字符映射，并校验逐行百分比与最终失败集合，SHA 为
`100aa778c2a56cd7a81e100069a5a49374f215bc21a7b32b8bb5c94f210dcafa`；随后仅用后两轮明确通过的
节点覆盖旧 oracle 结果，21 个原 skip 与两项 W5 普通失败保持可见。

## 6. 仍然保留的边界

- W5 未实施：旧无版本 Outcome approve/return 继续原签名/语义并发出弃用警告，R-04 与
  OC-10b/SDK-01 的 major 切换部分仍待 W5。
- 未完整封存且 Provider 开始/完成事实未知的 B/C 强杀，不自动释放预算、重调 Provider、
  伪造 proof 或恢复；该独立 repair/cleanup 合同继续后置。
- 不公开 Q2 task resume/retry、后台 daemon、rollback 或 generation cleanup；本轮仅处理
  manual rebuild 的预算恢复与已完整封存产物的公开继续发布。
- 未执行真实用户 Vault、真实 Provider、用户配置修改或真实迁移；不改变个人运行布局、
  MCP stdio-only、GUI 独立仓库和 Artifact/release hold。

## 7. 历史证据

- [W1 红测试与协议迁移冻结](./W1-红测试与协议迁移冻结记录-2026-10.md)
- [W2 身份、队列与 CLI 修复](./W2-身份队列与CLI门禁修复记录-2026-10.md)
- [W3 Outcome 一致性与版本审批](./W3-Outcome一致性与版本审批实施记录-2026-10.md)
- [W1–W3 CodeReview 修复验收](./W1-W3-CodeReview修复验收记录-2026-10.md)
- [R4-P1 原设计与 ER 合同](./R4-P1-公共Embedding重建适配器实施计划-2026-09.md)
