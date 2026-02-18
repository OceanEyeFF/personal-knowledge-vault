# Personal Knowledge Vault - Phase 2 开发总览

> Phase 2 共享前言：背景、约束与开发原则
>
> **版本**: 2.0
> **创建日期**: 2026-02-16
> **最后更新**: 2026-02-18
> **适用对象**: Claude Code、CodeX、GitHub Copilot Workspace 等 AI 开发工具
> **前置条件**: Phase 1 (v0.6.1) 已全部完成

---

## 🎯 Phase 2 概述

基于 Phase 1 的坚实基础，扩展 Personal Knowledge Vault 的能力边界：

**Phase 2 核心目标**:
1. 🔌 **MCP 服务** (v0.7.0) — 让 AI Agent 直接访问知识库
2. 🖥️ **GUI 桌面应用** (v0.8.x) — 图形化界面 + 内置 AI 交互

**关键设计理念**:
- **AI 交互独立化** — 软件内置 AI 对话能力，不依赖 Claude Code 等外部工具
- **后端可替换** — GUI 前端与后端解耦，更换引擎不影响界面
- **渐进式扩展** — 每个版本独立交付，不阻塞其他功能

---

## 📚 必读文档

### Phase 1 核心文档（了解现有架构）
- [`docs/archive/PHASE1_DEV_PROMPT.md`](../archive/PHASE1_DEV_PROMPT.md) - Phase 1 开发记录（已归档）
- [`docs/core/personal-knowledge-vault-prd.md`](./personal-knowledge-vault-prd.md) - **核心需求文档**
- [`docs/core/架构设计.md`](./架构设计.md) - 工作流驱动架构
- [`docs/core/技术选型.md`](./技术选型.md) - 技术栈选型

### Phase 2 开发 Prompt（按阶段分别执行）
- [`docs/core/PHASE2A_MCP_PROMPT.md`](./PHASE2A_MCP_PROMPT.md) - **MCP 服务开发** (M8+M9, v0.7.0)
- [`docs/core/PHASE2B_GUI_PROMPT.md`](./PHASE2B_GUI_PROMPT.md) - **GUI 应用开发** (M10~M13, v0.8.x)

### Phase 2 设计文档
- [`docs/design/MCP_SERVICE_DESIGN.md`](../design/MCP_SERVICE_DESIGN.md) - MCP 服务技术设计（配合 Phase2A 使用）
- [`docs/design/GUI_FRAMEWORK_ANALYSIS.md`](../design/GUI_FRAMEWORK_ANALYSIS.md) - GUI 框架选型分析（配合 Phase2B 使用）

### 接口规范（复用现有模块）
- [`docs/refactor/Entry数据模型规范.md`](../refactor/Entry数据模型规范.md)
- [`docs/refactor/Processors接口规范.md`](../refactor/Processors接口规范.md)
- [`docs/refactor/Storage接口规范.md`](../refactor/Storage接口规范.md)
- [`docs/refactor/Retrieval检索引擎规范.md`](../refactor/Retrieval检索引擎规范.md)
- [`docs/refactor/WorkflowEngine接口规范.md`](../refactor/WorkflowEngine接口规范.md)

---

## ⚠️ 关键约束

### 继承 Phase 1 所有约束

- 环境保护规则（虚拟环境、不修改系统配置、不产生垃圾文件）
- 代码质量要求（KISS/DRY/SOLID、类型注解、docstring、错误处理）
- Git 仓库清洁规则

### Phase 2 新增约束

1. **不破坏 CLI** — Phase 2 的 GUI 和 MCP 是新增入口，不影响现有 CLI 功能
2. **向后兼容** — 新增的数据库字段/表必须通过增量迁移实现
3. **依赖最小化** — 每个新功能引入的依赖尽量少
4. **许可证合规** — PySide6 (LGPL)、MCP SDK (MIT) 均兼容项目使用

---

## 🏗️ Phase 2 里程碑总览

> **起点**: v0.6.1（Phase 1 完成，M1-M7）
> **目标**: v0.8.1（Phase 2 核心完成）

| Milestone | 版本 | 主题 | 核心产出 | Prompt 文档 |
|-----------|------|------|---------|------------|
| **M8** | v0.7.0-alpha | MCP 只读服务 | 5 个只读 Tool + 4 个 Resource | [Phase2A](./PHASE2A_MCP_PROMPT.md) |
| **M9** | v0.7.0 | MCP 写入 + Prompts | 3 个写入 Tool + 3 个 Prompt | [Phase2A](./PHASE2A_MCP_PROMPT.md) |
| **M10** | v0.8.0-alpha | GUI 基础框架 | 主窗口 + 知识浏览 + 搜索 | [Phase2B](./PHASE2B_GUI_PROMPT.md) |
| **M11** | v0.8.0-beta | GUI 归档 + 设置 | 归档界面 + 设置界面 + 统计面板 | [Phase2B](./PHASE2B_GUI_PROMPT.md) |
| **M12** | v0.8.0 | AI 对话交互 | 聊天界面 + 对话服务 + 对话存储 | [Phase2B](./PHASE2B_GUI_PROMPT.md) |
| **M13** | v0.8.1 | GUI 打包与集成测试 | 打包分发 + E2E 测试 + 文档 | [Phase2B](./PHASE2B_GUI_PROMPT.md) |

---

## ⚡ 开发原则

继承 Phase 1 所有原则，并新增：

1. **不破坏现有功能** — Phase 2 是增量扩展，CLI 功能必须保持完整
2. **前后端解耦** — GUI 通过 ViewModel 层调用 Service，不直接访问存储
3. **AI 交互自主** — 软件内置 AI 对话，不强制要求 Claude Code
4. **渐进式交付** — 6 个 Milestone 各自独立可测试、独立可交付
5. **用户体验优先** — GUI 设计关注易用性，降低使用门槛

---

## 🔮 Phase 2 之后的展望

Phase 2 核心完成后（v0.8.1），系统将具备：
- ✅ CLI + GUI + MCP 三种交互方式
- ✅ 内置 AI 对话能力（不依赖外部工具）
- ✅ 完整的知识管理生命周期

**Phase 3 方向（待评估）**:
- 视频内容归档（B站/YouTube 转录）
- 知识图谱可视化（D3.js / Cytoscape）
- PDF 书籍处理（OCR + 章节提取）
- 多设备同步（加密）
- 性能优化（大规模知识库）

---

**文档版本**: v2.0
**创建日期**: 2026-02-16
**最后更新**: 2026-02-18 (v2.0: 拆分为 Phase2A/Phase2B 两份执行 Prompt)
