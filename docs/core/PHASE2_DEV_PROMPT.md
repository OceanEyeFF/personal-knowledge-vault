# Personal Knowledge Vault - Phase 2 开发总览

> Phase 2 共享前言：背景、约束与开发原则
>
> **版本**: 2.3
> **创建日期**: 2026-02-16
> **最后更新**: 2026-02-20
> **适用对象**: Claude Code、CodeX、GitHub Copilot Workspace 等 AI 开发工具
> **前置条件**: Phase 1 (v0.6.1) 已全部完成
>
> **⚠️ 进度状态（2026-02-24）**: Phase 2A (M8+M9) 已全部完成（v0.7.0）。
> Phase 2B M10+M11+M12 已完成（v0.8.0，218 tests 全通过）。
> **M13 被跳过**，直接推进 **M14 用户审核系统**（v0.9.0）。
> 详见 [PHASE2B_GUI_PROMPT.md](./PHASE2B_GUI_PROMPT.md)、[M12 完成报告](../milestones/M12_COMPLETION_REPORT.md)。

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
> **目标**: v0.9.0（Phase 2 核心完成 + 用户审核工作流）

| Milestone | 版本 | 状态 | 主题 | 核心产出 | Prompt 文档 |
|-----------|------|------|------|---------|------------|
| **M8** | v0.7.0-alpha | ✅ **已完成** | MCP 只读服务 | 5 个只读 Tool + 4 个 Resource + 单元/集成测试 | [Phase2A](./PHASE2A_MCP_PROMPT.md) |
| **M9** | v0.7.0 | ✅ **已完成** | MCP 写入 + Prompts | 3 个写入 Tool + 3 个 Prompt + 安全加固 + 三层测试 203 个 | [Phase2A](./PHASE2A_MCP_PROMPT.md) |
| **M10** | v0.8.0-alpha | ✅ **已完成** | GUI 基础框架 | 主窗口 + 知识浏览 + 搜索 + 130 测试 | [Phase2B](./PHASE2B_GUI_PROMPT.md) |
| **M11** | v0.8.0-beta | ✅ **已完成** | GUI 归档 + 设置 + 额外加固 | 归档界面 + 设置界面 + 统计面板 + 知乎登录墙 + 删除功能 + Embedding 可配置 | [Phase2B](./PHASE2B_GUI_PROMPT.md) |
| **M12** | v0.8.0 | ✅ **已完成** | AI 对话交互 | 聊天界面 + 对话服务 + 对话存储 + 知识引用 + Token 控制 + 218 tests | [Phase2B](./PHASE2B_GUI_PROMPT.md) |
| **M13** | v0.8.1 | ⏭️ **被跳过** | ~~GUI 打包与集成测试~~ | **决策**: 打包是工程细节，应在工作流完整性验证后进行 | [Phase2B](./PHASE2B_GUI_PROMPT.md) |
| **M14** | v0.9.0 | 🔲 **待开始** | **用户审核系统** ⭐ | CLI 审核流程 + 历史追踪 + 所有工作流集成 | [M14 PRD](../../docs/review-system-prd.md) |

---

## ⚡ 开发原则

继承 Phase 1 所有原则，并新增：

1. **不破坏现有功能** — Phase 2 是增量扩展，CLI 功能必须保持完整
2. **前后端解耦** — GUI 通过 ViewModel 层调用 Service，不直接访问存储
3. **AI 交互自主** — 软件内置 AI 对话，不强制要求 Claude Code
4. **渐进式交付** — 6 个 Milestone 各自独立可测试、独立可交付
5. **用户体验优先** — GUI 设计关注易用性，降低使用门槛

---

## 🏗️ Milestone 14: 用户审核系统 (v0.9.0)

**目标**: 实现 AI 生成内容的用户审核与修改工作流，让用户对 AI 输出拥有完整掌控权

**位置**: Phase 2 最后一个里程碑，独立开发不阻塞 M12-M13 GUI 进度

**前置**: M11 完成（因为审核系统需要与工作流/处理器/存储层集成）

**关键特性**:
- ✅ **CLI 审核流程**：所有工作流的必经步骤（可选/强制配置）
- ✅ **内容预览**：展示 AI 去广告、去尾端后的效果
- ✅ **摘要/标签修改**：支持直接编辑和系统编辑器两种方式
- ✅ **AI 重新生成**：用 Prompt 指导 AI，保持同一 session 上下文
- ✅ **个人评论**：用户可添加不属于摘要和标签的自由笔记
- ✅ **版本追踪**：完整的修改历史和版本回溯功能
- ✅ **草稿管理**：拒绝的条目存入草稿区，支持后续恢复

**核心架构**:
- 新增 `review_queue` 和 `review_history` 数据库表
- 新增 `ReviewStep` 工作流步骤（插入所有 workflow 中）
- 改造 CLI `archive` 命令支持交互式审核
- 改造所有 Processor 接入审核流程

**文档与交付**:
- [M14 Product Requirements Document](../../docs/review-system-prd.md) — 完整需求文档
- [M14 Development Prompt](./M14_REVIEW_SYSTEM_PROMPT.md) — 开发执行指令（待新建）

---

## 📋 关键决策：M13 被跳过 — 为什么直接推进 M14

> **决策日期**: 2026-02-24
> **决策者**: 主人（基于工作流完整性评估）

### 问题背景

在完成 M12 AI 对话功能后，发现了一个 **架构层级的核心缺陷**：

**整个系统缺少用户对 AI 生成内容的审核与修改阶段**。

表现形式：
- 用户无法编辑 AI 生成的摘要和标签
- 用户无法对 AI 输出进行评论或反馈
- 用户无法指导 AI 重新生成（保持上下文）
- 所有工作流都缺少这个必经步骤

### 为什么不继续 M13（打包）

**M13 打包的本质**：工程发布细节，包括 PyInstaller 配置、E2E 测试套件、用户文档

**关键洞察**：
1. **工作流不完整** — 打包一个不允许用户审核的系统，用户体验不完整
2. **用户价值单调** — M13 只是让现有功能可独立运行，没有新功能
3. **商业价值排序** — 审核系统 + 多端支持（OpenClaw、移动应用）比打包优先级更高
4. **架构重要性** — M14 定义了后续所有客户端的核心工作流

### 推进 M14 的理由

| 对比维度 | M13（打包） | M14（审核系统） |
|--------|----------|----------|
| **用户价值** | 能独立运行现有功能 | 让用户对 AI 输出有完整掌控权 |
| **工作流完整性** | 不改变工作流 | **补齐核心缺失环节** |
| **后续扩展** | 与多端无关 | 多端客户端必须基于此架构 |
| **技术复杂度** | 配置级（中） | 功能级（高）|
| **测试覆盖** | 打包验证 + E2E | 90+ 单元测试 + 工作流集成 |

### M13 后续计划

打包并未被永久放弃，只是调整优先级：

- **选项 A**：M14 完成后作为 **M15 轻量级打包里程碑**
- **选项 B**：与 M14 合并，作为 **v0.9.0 统一交付的打包部分**
- **选项 C**：多端客户端开发中统一处理打包（OpenClaw 的打包策略）

---

Phase 2 完成后（v0.9.0），系统将具备：
- ✅ CLI + GUI + MCP 三种交互方式
- ✅ 内置 AI 对话能力（不依赖外部工具）
- ✅ **AI-Human 协作的审核与修改工作流**
- ✅ 完整的知识管理生命周期（从生成到审核到入库）

**Phase 3 方向（待评估）**:
- 视频内容归档（B站/YouTube 转录）
- 知识图谱可视化（D3.js / Cytoscape）
- PDF 书籍处理（OCR + 章节提取）
- 多设备同步（加密）
- 性能优化（大规模知识库）

---

**文档版本**: v2.5
**创建日期**: 2026-02-16
**最后更新**: 2026-02-24 (v2.5: 标记 M13 被跳过的决策理由，M14 成为 Phase 2 最后一个里程碑)
**最后更新**: 2026-02-23 (v2.4: 新增 M14 里程碑 — 用户审核系统 v0.9.0，独立后续开发，不阻塞 M12-M13 GUI 进度)
