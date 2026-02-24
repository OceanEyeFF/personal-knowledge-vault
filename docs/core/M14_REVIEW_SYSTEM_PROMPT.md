# Personal Knowledge Vault - M14 用户审核系统开发 Prompt

> 审核系统开发执行指令（M14）
>
> **版本**: 1.0
> **创建日期**: 2026-02-23
> **适用对象**: Claude Code、CodeX 等 AI 开发工具
> **前置条件**: M11 完成（GUI 基础设施）；M12-M13 可并行开发
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

## ⚙️ 技术方案

### 数据库设计

新增两张表（migration 文件已在 `scripts/migrations/005_add_review_system.sql` 创建）：

```sql
-- 审核队列表
CREATE TABLE review_queue (
    review_id INTEGER PRIMARY KEY,
    knowledge_id INTEGER,                    -- 可为空（新条目）
    review_status TEXT DEFAULT 'pending',    -- pending/approved/rejected/needs_revision

    -- AI 生成的原始内容
    ai_generated_summary TEXT,
    ai_generated_tags TEXT,
    ai_cleaned_content TEXT,

    -- 用户审核意见
    user_summary TEXT,
    user_tags TEXT,
    user_comments TEXT,                      -- ⭐ 个人评论

    -- AI 重新生成
    regeneration_count INTEGER DEFAULT 0,
    regeneration_prompts TEXT,               -- JSON 格式的历次 Prompt
    regeneration_session_id TEXT,            -- 关联的 AI Session

    -- 时间戳和追踪
    created_at TIMESTAMP,
    approved_at TIMESTAMP,
    review_version INTEGER DEFAULT 1,

    FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id),
    FOREIGN KEY (regeneration_session_id) REFERENCES chat_sessions(session_id)
);

-- 审核历史表（记录每个操作）
CREATE TABLE review_history (
    history_id INTEGER PRIMARY KEY,
    review_id INTEGER NOT NULL,
    action TEXT,                             -- init/modify_summary/modify_tags/regenerate/approve/reject
    details JSON,                            -- 变更详情
    operator TEXT DEFAULT 'user',            -- user/ai
    created_at TIMESTAMP,

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
         async def create_review(review_item: ReviewItem) -> int  # 返回 review_id
         async def get_review(review_id: int) -> ReviewItem
         async def update_user_summary(review_id, summary) -> bool
         async def update_user_tags(review_id, tags) -> bool
         async def add_user_comment(review_id, comment) -> bool
         async def add_regeneration_prompt(review_id, prompt, ai_session_id) -> bool
         async def approve_review(review_id, final_version) -> bool
         async def reject_review(review_id) -> bool
         async def get_history(review_id) -> List[ReviewHistoryItem]
         async def revert_to_version(review_id, version) -> bool
     ```

2. **ReviewStep** （新增工作流步骤）
   - 位置: `src/workflow/steps.py` 中新增
   - 职责：在工作流中插入审核阶段
   - 特点：**可选/强制配置** 通过环境变量 `REVIEW_REQUIRED=true/false`

3. **CLI 审核界面** （改造 `archive` 命令）
   - 位置: `src/cli/commands.py` 修改 `archive()` 函数
   - 新增参数: `--no-review`（跳过审核）
   - 交互式菜单：修改摘要 → 修改标签 → 添加评论 → 重新生成 → 最终决定

4. **AI 重新生成服务**
   - 位置: `src/gui/services/ai_chat_service.py`（已存在 M12）
   - 复用现有的 DeepSeekClient 和 stream_chat 功能
   - 保持同一 session 中的上下文

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
  - [ ] 拒绝的条目存入 review_queue（status='rejected'）
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

**环境变量控制**:
```bash
REVIEW_REQUIRED=true   # 强制审核（生产环境推荐）
REVIEW_REQUIRED=false  # 可选审核（开发/快速测试）
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

AI 能看到完整的修改历史和反馈，生成更一致的结果。

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
- [x] AI 重新生成服务（保持 Session 上下文）
- [x] 文本编辑器集成
- [x] 版本回溯与历史追踪
- [x] 草稿区管理
- [x] 90+ 单元/集成/E2E 测试
- [x] 完整的用户文档和 API 文档

---

## 🔌 扩展预留

- **GUI 审核界面** — 后续可在 Phase 3 中针对 GUI 实现
- **审核指标仪表板** — 统计审核通过率、修改率、重新生成次数等
- **智能建议** — AI 自动检测潜在问题并建议修改（如标签太长）
- **多用户协作审核** — 支持团队协作的审核工作流

---

**文档版本**: v1.0
**创建日期**: 2026-02-23
**适用里程碑**: M14 - 用户审核系统 (v0.9.0)
**预计周期**: 4 周（可与 M12-M13 GUI 开发并行）
