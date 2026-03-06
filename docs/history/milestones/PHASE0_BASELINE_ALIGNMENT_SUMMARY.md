# Phase 0 基线校准总结

> 文档类型：阶段总结
> 阶段名称：`Phase 0 - Baseline Alignment / Context Freeze`
> 完成日期：2026-03-06
> 目标：校准当前代码与核心文档，冻结后续开发共用的静态上下文

---

## 1. 阶段目标

Phase 0 只解决一件事：

- 在继续推进关系层和推理型 MCP 之前，先把“当前事实”固定住

对应目标分为三项：

1. 校验当前代码与核心文档是否匹配
2. 建立统一的当前事实基线
3. 输出后续 Phase 可持续增量维护的真相源体系

---

## 2. 本阶段产出

Phase 0 已新增或明确以下核心文档：

- `docs/overview/当前事实基线-2026-03.md`
- `docs/overview/阶段开发路线与依赖-2026-03.md`
- `docs/overview/文档与代码差异清单-2026-03.md`

同时完成以下真相源入口调整：

- `docs/README.md` 已加入当前高频入口
- `docs/overview/当前战略与路线收敛-2026-03.md` 已引用基线与路线文档
- `docs/overview/开发计划.md` 已明确降级为历史参考

---

## 3. 已完成的核心校准

### 3.1 版本口径统一

- 当前仓库基线统一为 `v0.8.0-alpha`
- `README.md`、`src/__init__.py`、`src/main.py` 已完成对齐
- `docs/operations/CHANGELOG.md` 已补齐 `v0.7.0` 与 `v0.8.0-alpha`

### 3.2 历史里程碑文档校注

- `docs/history/milestones/M12_COMPLETION_REPORT.md` 已补充 Phase 0 校注
- 已移除或改写会误导当前开发的错误文件路径、错误 CLI 能力声明和过时架构映射

### 3.3 README 基线修正

- README 中的测试资产描述已调整为 `2026-03-06` 仓库快照口径
- 旧的固定覆盖率结论已从核心状态区移除，改为待重新统计

### 3.4 当前事实基线补强

- 已补入当前 CLI / MCP / GUI 接口快照
- 已明确当前真实存在但边界有限的能力
- 已明确当前未实现项，避免把路线目标误写成已落地能力

---

## 4. 已关闭的 mismatch

截至本阶段结束，以下高优先级差异已处理完成：

- `M-001`：版本号体系不一致
- `M-002`：M12 报告引用不存在的当前仓库文件
- `M-003`：M12 报告错误声明存在 `pkv chat ...` CLI 命令
- `M-004`：README 测试清单与当前仓库结构不一致
- `M-005`：CHANGELOG 未覆盖 README 与里程碑文档声明的后续版本

仍保留但已降低风险的记录项：

- `M-006`：早期开发计划与当前技术基线不一致
- `M-007`：当前真相源入口曾经不够集中

这两项已通过真相源分层和入口收敛完成风险缓释，当前不阻塞后续 Phase。

---

## 5. 伴随修正

Phase 0 不只做了文档校准，还顺手修正了一个会影响基线核验的运行时问题：

- `src/main.py` 已改为按需加载 CLI 子命令
- `pkv --version` 不再因为重量级依赖导入失败而中断

对应黑盒验证已通过：

- `pytest tests/blackbox/test_cli_basic.py -k version_command -q`
- `pytest tests/blackbox/test_cli_blackbox.py -k version_command -q`

---

## 6. 阶段退出判断

Phase 0 的退出条件已满足：

- 核心文档与代码的关键冲突已被显式标记并完成高优先级修正
- 当前事实基线已冻结，可作为 Phase A 输入
- 真相源层级已明确
- 后续增量更新规则已建立

当前状态可以收敛为一句话：

**项目已具备进入 `Phase A - Relation Foundation` 的稳定上下文。**

---

## 7. 下一阶段建议

Phase A 应严格沿以下顺序展开：

1. 定义 `relation schema / relation types`
2. 明确关系抽取来源与落库边界
3. 建立 relation query service
4. 再进入推理型 MCP tool 设计

本阶段不再建议继续扩大 GUI 范围，也不建议引入新的平行路线文档。
