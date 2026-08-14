# 文档总览

`docs/` 现在按抽象层级和使用目的组织，避免把时间维度混进主目录结构。

## 目录说明

- `overview/`：项目定位、战略、PRD、架构总览、技术选型、结构说明
- `modules/`：按模块拆分的设计文档，例如 `mcp/`、`workflow/`、`review/`；GUI 模块文档已迁至独立 `pkv-GUI` 仓库
- `specs/`：接口、数据模型、Schema、数据流等细颗粒规范
- `operations/`：安装、使用、维护、迁移、测试环境等操作型文档
- `history/`：历史 Prompt、讨论、里程碑、问题记录、复盘材料

## 短期高频使用

- [当前战略与路线收敛-2026-03.md](./overview/当前战略与路线收敛-2026-03.md)
- [当前事实基线-2026-03.md](./overview/当前事实基线-2026-03.md)
- [阶段开发路线与依赖-2026-03.md](./overview/阶段开发路线与依赖-2026-03.md)
- [后M13开发路线-2026-08.md](./overview/后M13开发路线-2026-08.md)（内部自测封包、知识成果与可选 Node）
- [PhaseB-推理型MCP路线图-2026-03.md](./overview/PhaseB-推理型MCP路线图-2026-03.md)
- [文档与代码差异清单-2026-03.md](./overview/文档与代码差异清单-2026-03.md)
- [personal-knowledge-vault-prd.md](./overview/personal-knowledge-vault-prd.md)
- [架构设计.md](./overview/架构设计.md)
- [RELATION_LAYER_DESIGN.md](./modules/relations/RELATION_LAYER_DESIGN.md)
- [Relations接口规范.md](./specs/interfaces/Relations接口规范.md)
- [QUICKSTART.md](./operations/QUICKSTART.md)
- [维护指南.md](./operations/维护指南.md)
- [关系回填质量验证指南.md](./operations/关系回填质量验证指南.md)
- [MCP 最小评测闭环](./operations/MCP最小评测闭环.md)
- [MCP 最小评测基线（2026-07-29）](./operations/MCP最小评测基线-2026-07-29.md)
- [真实数据验证 Runbook（P2）](./operations/testing/真实数据验证Runbook.md)（G0 完整离线通道；真实快照需 U1/G8 user-only launcher；写入仅 writable clone）
- [测试环境隔离指南](./operations/testing/测试环境隔离指南.md)

## 长期参考

- [技术选型.md](./overview/技术选型.md)
- [项目结构说明.md](./overview/项目结构说明.md)
- [modules/relations/RELATION_LAYER_DESIGN.md](./modules/relations/RELATION_LAYER_DESIGN.md)
- [specs/interfaces/Relations接口规范.md](./specs/interfaces/Relations接口规范.md)
- [modules/](./modules/)
- [specs/](./specs/)
- [history/README.md](./history/README.md)

## 使用原则

- 当前真相源优先看 `overview/`
- `overview/开发计划.md` 属于早期路线文档，默认不作为当前执行依据
- 具体模块行为优先看 `modules/` 与 `specs/`
- 操作步骤和维护流程优先看 `operations/`
- `history/` 仅用于追溯背景，不作为当前开发的默认依据
