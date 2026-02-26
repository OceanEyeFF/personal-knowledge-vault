# Personal Knowledge Vault - M14 用户审核系统开发 Prompt

> 审核系统开发执行指令（M14）
>
> **版本**: 1.1
> **创建日期**: 2026-02-23
> **适用对象**: Claude Code、CodeX 等 AI 开发工具
> **前置条件**: M12 已完成（GUI + AI 对话）；M13 已跳过；M14 在现有基础上继续推进
> **核心文档**: [M14 Product Requirements Document](../../docs/review-system-prd.md)
> **总览文档**: [PHASE2_DEV_PROMPT.md](./PHASE2_DEV_PROMPT.md)

---

## 🎯 M14 目标

实现 **AI 生成内容的用户审核与修改工作流**，让用户对 AI 输出拥有完整掌控权。

**这是所有工作流的必经步骤**：
- 网页归档 → 审核 → 入库
- 文本归档 → 审核 → 入库
- AI 对话 → 审核 → 保存到知识库
- ...（所有处理流程）

**交付版本**: v0.9.0（Phase 2 的最后一个里程碑）

---

## 📚 必读文档

- [M14 Product Requirements Document](../../docs/review-system-prd.md) — **完整需求文档**（已在 2026-02-23 生成）
- [PHASE2_DEV_PROMPT.md](./PHASE2_DEV_PROMPT.md) — Phase 2 总览
- [PHASE2B_GUI_PROMPT.md](./PHASE2B_GUI_PROMPT.md) — M10-M13 GUI 开发参考（架构模式可借鉴）

---

## 🧭 当前代码基线（先核验，再修改 Prompt）

在继续修订 M14 前，先以代码为准确认以下事实（避免文档漂移）：

1. `ReviewManager` 已在 `src/storage/review_manager.py` 落地，接口为**同步方法**。
2. `ReviewStep` 已在 `src/workflow/steps.py` 落地，并通过 `WorkflowEngine` 注册为 `review_entry`。
3. `archive-url.yaml` 与 `archive-text.yaml` 已注入 `review_entry`，且 `required` 分别为 `true/false`。
4. `review-drafts` CLI 命令组、`archive --no-review` 目前仍是增强项（默认未交付）。
5. M13 打包为“已跳过里程碑”，当前事实以 Phase 总览文档为准。

建议先执行以下核验命令，再继续编辑文档：

```bash
rg -n "class ReviewManager|class ReviewStep|review_entry|record_regeneration|list_drafts|restore_draft" src config -g '*.py' -g '*.yaml'
rg -n "M13 被跳过|M14" docs/core/PHASE2_DEV_PROMPT.md docs/core/PHASE2B_GUI_PROMPT.md -g '*.md'
```

---

## ⚙️ 技术方案

### 数据库设计

新增两张表（migration 文件已在 `scripts/migrations/005_add_review_system.sql` 创建）：

```sql
-- 审核队列表
CREATE TABLE review_queue (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ai_generated_summary TEXT NOT NULL,
    ai_generated_tags TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'unknown',
    ai_cleaned_content TEXT NOT NULL DEFAULT '',
    ai_generation_model TEXT NOT NULL DEFAULT 'deepseek-chat',
    original_content_preview TEXT NOT NULL DEFAULT '',
    source_url TEXT,
    knowledge_id INTEGER,

    user_summary TEXT,
    user_tags TEXT,
    user_comments TEXT,

    regeneration_count INTEGER NOT NULL DEFAULT 0,
    regeneration_prompts TEXT NOT NULL DEFAULT '[]',

    review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(review_status IN ('pending', 'approved', 'rejected', 'draft')),
    review_version INTEGER NOT NULL DEFAULT 1,

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 审核历史表（记录每个操作）
CREATE TABLE review_history (
    history_id INTEGER PRIMARY KEY,
    review_id INTEGER NOT NULL,
    action TEXT,                             -- init/modify_summary/modify_tags/regenerate/approve/reject
    details TEXT NOT NULL DEFAULT '',        -- JSON 字符串
    operator TEXT NOT NULL DEFAULT 'user',   -- user/system
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (review_id) REFERENCES review_queue(review_id)
);
```

### 架构层

**核心组件**：

1. **ReviewManager** （新增）— 审核队列管理
   - 位置: `src/storage/review_manager.py`
   - 职责：审核队列的 CRUD、历史记录、版本管理
   - 接口：
     ```python
     class ReviewManager:
         def create_review(review_item: ReviewItem) -> int  # 返回 review_id
         def get_review(review_id: int) -> ReviewItem
         def update_user_summary(review_id, summary) -> bool
         def update_user_tags(review_id, tags) -> bool
         def add_user_comment(review_id, comment) -> bool
         def record_regeneration(review_id, prompt, new_summary, new_tags) -> bool
         def approve_review(review_id) -> bool
         def reject_review(review_id) -> bool
         def get_history(review_id) -> List[dict]
         def list_drafts() -> List[ReviewItem]
         def restore_draft(review_id) -> bool
     ```
   - 调用约定：`ReviewManager` 为同步实现，在异步工作流中通过 `asyncio.to_thread(...)` 调用。

2. **ReviewStep** （新增工作流步骤）
   - 位置: `src/workflow/steps.py` 中新增
   - 职责：在工作流中插入审核阶段
   - 特点：**可选/强制配置** 通过 workflow YAML 的 `required: true/false` 控制

3. **CLI 审核界面** （改造 `archive` 命令）
   - 位置: `src/cli/commands.py` 修改 `archive()` 函数
   - 当前状态：`archive` 命令已接入审核步骤；`--no-review` 作为增强项待补充
   - 交互式菜单：修改摘要 → 修改标签 → 添加评论 → 重新生成 → 最终决定

4. **AI 重新生成服务**
   - 当前实现位置: `src/workflow/steps.py::_call_ai_regenerate()`
   - 复用 `DeepSeekClient`，通过结构化 Prompt 执行重生成
   - 已支持多轮 Prompt 记录；会话级 session 复用作为后续增强项

---

## 📋 开发清单

### Phase 1: 数据库 + 核心存储层（Week 1）

- [ ] **执行数据库迁移**
  - [ ] 运行 `python scripts/migrate.py` 应用 005_add_review_system.sql
  - [ ] 验证表结构正确

- [ ] **创建 ReviewManager 类** (`src/storage/review_manager.py`)
  - [ ] 实现 CRUD 方法（create, get, update, delete）
  - [ ] 实现历史记录管理（add_history, get_history）
  - [ ] 实现版本管理（revert_to_version）
  - [ ] 单元测试 (30+ 测试用例)

### Phase 2: 工作流集成（Week 1-2）

- [ ] **新增 ReviewStep 工作流步骤**
  - [ ] 位置: `src/workflow/steps.py`
  - [ ] 类定义: `class ReviewStep(WorkflowStep)`
  - [ ] 实现 `async def execute()` 方法
  - [ ] 在所有工作流配置中集成（archive-url.yaml, archive-text.yaml）
  - [ ] 单元测试 (10+ 测试用例)

### Phase 3: CLI 交互式审核（Week 2）

- [ ] **改造 CLI archive 命令**
  - [ ] 新增 `--no-review` 参数（快速保存选项）
  - [ ] 审核菜单实现：
    - [ ] 显示内容预览（去广告、去尾端）
    - [ ] 修改摘要（支持控制台多行 + 编辑器）
    - [ ] 修改标签（逗号分隔）
    - [ ] 添加评论（多行文本）
    - [ ] AI 重新生成菜单
    - [ ] 最终决定菜单（通过/拒绝/修改后再审）
  - [ ] 集成测试 (20+ 测试用例)

- [ ] **文本编辑器集成**
  - [ ] 使用 `tempfile` + `subprocess` 调用系统编辑器（vim/nano）
  - [ ] 支持跨平台（Windows/Linux/macOS）
  - [ ] 编辑失败时降级到控制台输入

### Phase 4: AI 重新生成（Week 2-3）

- [ ] **AI 重新生成服务**
  - [ ] 在 ReviewStep 中调用 AI 服务
  - [ ] **使用相同 AI Session 保持上下文**（关键！）
  - [ ] 记录用户的 Prompt 指导
  - [ ] 多轮迭代支持
  - [ ] 失败降级（给出错误提示，允许用户手工修改）

### Phase 5: 预览模块（Week 3）

- [ ] **内容预览与清理显示**
  - [ ] 展示原始内容的预览片段（前 500 字）
  - [ ] 展示 AI 去广告、去尾端后的效果
  - [ ] 展示来源URL、标题、元数据

### Phase 6: 历史与版本管理（Week 3）

- [ ] **版本回溯功能**
  - [ ] CLI 中展示版本历史列表
  - [ ] 支持回溯到任意版本（包括 AI 重新生成的版本）
  - [ ] 回溯操作本身也被记录
  - [ ] 单元测试 (15+ 测试用例)

### Phase 7: 草稿管理（Week 3-4）

- [ ] **拒绝条目的草稿存储**
  - [ ] 拒绝的条目存入 review_queue（status='draft'）
  - [ ] 用户可以查看草稿区
  - [ ] 支持恢复或永久删除草稿
  - [ ] CLI 命令: `pkv review-drafts list/show/restore/delete`

### Phase 8: 全工作流集成（Week 4）

- [ ] **所有处理器集成审核**
  - [ ] 修改 `BaseProcessor.process()` 返回前调用 ReviewStep
  - [ ] 确保 **所有工作流都通过审核**（无论来源）
  - [ ] 整合测试 (E2E 场景: 网页 → 审核 → 入库)

### Phase 9: 完整测试与文档（Week 4）

- [ ] **单元 + 集成 + E2E 测试**
  - [ ] 目标: ≥ 90% 代码覆盖率
  - [ ] CLI 交互式审核的脚本化测试
  - [ ] 工作流集成的完整场景测试

- [ ] **更新文档**
  - [ ] README.md：添加审核工作流说明
  - [ ] CHANGELOG.md：v0.9.0 变更记录
  - [ ] 用户指南：审核命令的使用说明

---

## 🧩 关键设计决策

### 决策 1: 审核是可选/强制的

**配置方式（当前实现）**:
```yaml
# config/workflows/archive-url.yaml
review_entry:
  config:
    required: true

# config/workflows/archive-text.yaml
review_entry:
  config:
    required: false
```

不同工作流可以有不同的配置。例如：
- AI 对话 → **强制审核**（确保入库前确认）
- 文本归档 → **可选审核**（允许快速入库）
- 网页归档 → **强制审核**（质量把关）

### 决策 2: 同一 Session 上下文保持

当用户选择"AI 重新生成"时：
```
AI Session:
[消息 1] 原始内容 + AI 初始输出
[消息 2] 用户修改历史
[消息 3] 用户 Prompt: "标签太多了，帮我精简"
[消息 4] AI 重新生成结果
[消息 5] 用户修改 / 再次 Prompt ...
```

当前实现通过 `regeneration_prompts` 累积记录多轮用户指导；
“与 chat_sessions 绑定的会话级上下文”属于后续增强项。

### 决策 3: 拒绝 → 草稿区 而非永久删除

**原因**：
- 用户可能改主意，后续恢复
- 完整的审核历史便于追踪
- 草稿区有定期清理提示

---

## 🧪 测试要求

| 组件 | 单元测试 | 集成测试 | E2E 测试 |
|------|---------|---------|---------|
| ReviewManager | ✅ 30+ | ✅ 核心路径 | - |
| ReviewStep | ✅ 10+ | ✅ 工作流集成 | - |
| CLI 审核 | ✅ 20+ | ✅ 脚本化测试 | ✅ 手工测试 |
| AI 重新生成 | ✅ 15+ | ✅ Session 管理 | - |
| 版本回溯 | ✅ 15+ | ✅ 历史追踪 | - |

**总目标**: ≥ 90% 代码覆盖率，所有关键路径 100% 覆盖

---

## 🛡️ 风险与缓解

| 风险 | 概率 | 缓解 |
|------|------|------|
| AI Session 上下文溢出 | 中 | 实现 token 预算控制，超出时清理历史 |
| 编辑器集成失败 | 低 | 完整的错误处理和降级方案 |
| 数据库迁移失败 | 低 | 充分测试，保留回滚脚本 |
| 用户混淆审核流程 | 中 | 清晰的菜单和提示，详细的文档 |

---

## ✅ 验收检查点

1. ✅ `python -m src.main archive "https://example.com"` 进入审核流程
2. ✅ 用户可以修改摘要、标签、添加评论
3. ✅ 用户可以用 Prompt 指导 AI 重新生成（保持上下文）
4. ✅ 用户可以查看版本历史和回溯
5. ✅ 拒绝的条目存入草稿区，可恢复
6. ✅ 所有工作流都通过审核阶段
7. ✅ 单元测试 ≥ 90% 覆盖率
8. ✅ E2E 完整场景测试通过
9. ✅ 文档完整且清晰

---

## 📦 交付清单

### v0.9.0 正式交付

- [x] 数据库迁移 (005_add_review_system.sql)
- [x] ReviewManager 类（审核队列管理）
- [x] ReviewStep 工作流步骤
- [x] CLI 交互式审核界面
- [x] AI 重新生成服务（多轮 Prompt 记录）
- [x] 文本编辑器集成
- [x] 草稿区管理
- [x] M14 基础单元/集成测试
- [ ] `archive --no-review` 参数（可选增强）
- [ ] `pkv review-drafts list/show/restore/delete` 命令组
- [ ] 会话级上下文复用（chat_sessions 关联）
- [ ] 完整 v0.9.0 用户文档与 API 文档

> 说明：上面的勾选表示“代码库当前状态”，不是对未来工作的封版承诺；
> 在大仓迭代中，M14 允许继续修订，按“代码事实 → Prompt 更新”循环推进。

---

## 🔌 扩展预留

- **GUI 审核界面** — 后续可在 Phase 3 中针对 GUI 实现
- **审核指标仪表板** — 统计审核通过率、修改率、重新生成次数等
- **智能建议** — AI 自动检测潜在问题并建议修改（如标签太长）
- **多用户协作审核** — 支持团队协作的审核工作流

---

## 🔍 与 M13 相关文档的对接校验（执行前必读）

为避免误把历史“计划项”当成“已交付项”，在执行 M14 前需先做一次文档对账：

1. **以 Phase 总览为最终状态源**
   - `PHASE2_DEV_PROMPT.md` 明确：M13 已跳过，M14 为 Phase 2 最后里程碑。
2. **以 Phase2B 文档确认 M13 的处理结论**
   - `PHASE2B_GUI_PROMPT.md`（v1.5）明确写了 M13 被跳过，打包后移。
3. **里程碑完成报告按“历史快照”读取**
   - 早期 M10/M11/M12 完成报告中可能仍含“下一步 M13 打包”描述，这是当时计划，不代表当前状态。

> 执行规则：若文档冲突，**以最新更新日期的 Phase 总览文档为准**，再回写到当前 M14 Prompt，保持单一事实源。

---

**文档版本**: v1.1
**创建日期**: 2026-02-23
**适用里程碑**: M14 - 用户审核系统 (v0.9.0)
**预计周期**: 4 周（基于 M12 已完成、M13 已跳过的主线继续推进）
