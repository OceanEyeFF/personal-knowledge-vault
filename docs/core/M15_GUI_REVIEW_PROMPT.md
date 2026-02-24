# Personal Knowledge Vault - M16 GUI 审核层设计

> GUI 审核界面设计文档（M16 前置规划）
>
> **版本**: 1.0
> **创建日期**: 2026-02-24
> **适用对象**: Claude Code、CodeX 等 AI 开发工具
> **前置条件**: M14 完成（CLI 审核工作流）；M12 完成（GUI 框架 + AI 服务）
> **目标版本**: v1.0.0（Phase 3 首个 GUI 功能扩展）
> **总览文档**: [PHASE2_DEV_PROMPT.md](./PHASE2_DEV_PROMPT.md)

---

## 🎯 M16 目标

将 M14 实现的「CLI 审核工作流」迁移到 GUI 界面，让用户在桌面应用中完成审核交互。

**核心体验**：
- 归档后无缝进入 ReviewPanel（同一视图区域，无跳转感）
- 可视化对比 AI 原始输出 vs 用户修改版本
- 草稿区作为独立导航视图，随时可访问

---

## 🏗️ 架构设计

### 文件清单（最小变更集）

```
src/gui/
├── views/
│   ├── archive_view.py          # 修改：检测 review_id → 显示 ReviewPanel
│   ├── review_panel.py          # 新增：可复用审核面板（ArchiveView + DraftsView 共用）
│   └── review_drafts_view.py    # 新增：草稿管理视图（导航索引 6）
├── viewmodels/
│   ├── archive_viewmodel.py     # 修改：result_ready 携带 review_id
│   └── review_viewmodel.py      # 新增：ReviewManager 的 Qt 包装层
└── main_window.py               # 修改：注册 ReviewDraftsView (_NAV_REVIEW_DRAFTS = 6)
```

---

## 📐 ReviewViewModel 接口设计

```python
# src/gui/viewmodels/review_viewmodel.py

class ReviewViewModel(QObject):
    """审核状态机，管理 ReviewManager 的所有 Qt 交互。

    设计原则：
    - update_*/add_comment/approve/reject → 同步调用（ReviewManager 是同步 SQLite）
    - regenerate_with_ai → @asyncSlot()（DeepSeek API 调用，与 ChatViewModel 模式一致）
    """

    # ── Signals ──────────────────────────────────────────────────────────────
    # 数据加载
    review_loaded = Signal(dict)            # 审核项加载完成 {review_id, ai_summary, ai_tags, ...}
    history_loaded = Signal(list)           # 历史记录 [{"action": ..., "details": ..., ...}]

    # 用户操作响应
    summary_updated = Signal(str)           # 摘要修改成功，携带新摘要
    tags_updated = Signal(list)             # 标签修改成功，携带 List[str]
    comment_added = Signal(str)             # 评论添加成功

    # AI 重新生成
    ai_regen_started = Signal()             # 开始（显示 loading 状态）
    ai_regen_completed = Signal(str, list)  # 完成 (new_summary, new_tags_list)
    ai_regen_failed = Signal(str)           # 失败，携带错误信息

    # 终态
    review_approved = Signal(int)           # 审核通过，携带 knowledge_id
    review_rejected = Signal(int)           # 拒绝（存入草稿区），携带 review_id
    review_cancelled = Signal()             # 用户取消

    # 错误
    error_occurred = Signal(str)

    # ── 方法 ─────────────────────────────────────────────────────────────────
    def __init__(self, parent=None): ...
    def load_review(self, review_id: int) -> None
    def update_summary(self, summary: str) -> None
    def update_tags(self, tags: List[str]) -> None
    def add_comment(self, comment: str) -> None
    @asyncSlot()
    async def regenerate_with_ai(self, prompt: str) -> None
    def load_history(self) -> None
    def approve(self) -> None
    def reject(self) -> None
```

**关键设计决策**：
- `update_summary/tags/add_comment` 是**同步调用**，直接在主线程执行（SQLite 操作足够快，无需线程池）
- `regenerate_with_ai` 是 `@asyncSlot()`，与 ChatViewModel 的流式输出模式完全一致
- 一个 ReviewViewModel 实例被 ArchiveView（嵌入模式）和 ReviewDraftsView（草稿模式）共用

---

## 🖼️ ReviewPanel 布局设计

```python
# src/gui/views/review_panel.py

class ReviewPanel(QWidget):
    """
    可复用审核面板。被 ArchiveView 和 ReviewDraftsView 两处使用。

    布局（从上到下）：
    ┌──────────────────────────────────────────────────────┐
    │ [折叠] 内容预览对比                                    │
    │ ┌─────────────────┬────────────────────────────────┐ │
    │ │ 原始内容预览     │ AI 清理后预览                   │ │
    │ └─────────────────┴────────────────────────────────┘ │
    ├──────────────────────────────────────────────────────┤
    │ 摘要                                                  │
    │ [AI 原始摘要（灰色底）]                                │
    │ [用户修改摘要（白色底，可编辑）]  [✏ 编辑]            │
    ├──────────────────────────────────────────────────────┤
    │ 标签                                                  │
    │ [tag1] [tag2] [tag3]  [✏ 编辑标签]                   │
    ├──────────────────────────────────────────────────────┤
    │ 个人评论（可折叠）                                     │
    │ [多行文本输入框]  [添加评论]                           │
    ├──────────────────────────────────────────────────────┤
    │ [折叠] AI 重新生成                                    │
    │ [Prompt 输入框]  [🔄 重新生成]                        │
    ├──────────────────────────────────────────────────────┤
    │ [折叠] 历史版本                                       │
    │ 版本 1 → 版本 2 (当前)  [查看详情]                   │
    ├──────────────────────────────────────────────────────┤
    │ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  │
    │  [✅ 入库确认]   [💾 存为草稿]   [❌ 拒绝]   [取消]  │
    └──────────────────────────────────────────────────────┘

    向上传递的 Signals：
    - approve_requested = Signal()
    - draft_requested = Signal()
    - reject_requested = Signal()
    - cancel_requested = Signal()
    - summary_edit_submitted = Signal(str)
    - tags_edit_submitted = Signal(list)
    - comment_submitted = Signal(str)
    - ai_regen_requested = Signal(str)       # prompt 文本
    - history_requested = Signal()
    """
```

---

## 🔌 ReviewStep GUI 模式适配（关键阻断项）

当前 ReviewStep 是阻塞式 CLI 交互，在 GUI 的 QThread 中运行会阻塞 UI。

**解决方案**：在 `ReviewStep.execute()` 中增加 GUI 模式检测：

```python
# src/workflow/steps.py → ReviewStep.execute() 修改

async def execute(self, context) -> Dict[str, Any]:
    # ... 构建 ReviewItem ...

    # GUI 模式：创建记录后立即返回，不阻塞等待用户输入
    gui_mode = context.params.get("gui_mode") or os.environ.get("PKV_GUI_MODE")
    if gui_mode:
        review_id = await asyncio.to_thread(review_manager.create_review, item)
        context.state["pending_review_id"] = review_id
        # GUI 模式下不阻止 StoreStep（先入库，用户可在 ReviewPanel 修改）
        return {"review_id": review_id, "gui_mode": True}

    # CLI 模式：原有阻塞式交互逻辑
    return await self._interactive_review(context, review_manager, item)
```

### GUI 模式下的「先入库 vs 审核后入库」决策

| 维度 | 方案 A：先入库 | 方案 B：审核后入库 |
|------|--------------|----------------|
| **语义** | 入库后可修改 review_queue | 审核通过才真正入库 |
| **实现复杂度** | 低（StoreStep 正常执行） | 高（需要延迟 StoreStep） |
| **用户体验** | 「归档成功，可进一步修改 AI 摘要」 | 「等待审核后才算归档」 |
| **数据一致性** | 可能有「未审核的入库条目」 | 入库条目都是已审核的 |

**推荐方案 B**（审核后入库），实现方式：

```python
# GUI 模式下 ReviewStep 设置 review_rejected = True 阻止 StoreStep
# ReviewViewModel.approve() 时手动调用 StoreStep 或直接写入 SQLite
context.state["review_rejected"] = True  # 暂时阻止入库
context.state["pending_review_id"] = review_id
```

> ⚠️ **方案 B 实现难点**：approve() 时需要重新触发入库操作，
> 需要在 ReviewViewModel 中持有工作流上下文数据（entry dict）。
> 设计时需要将 entry 序列化存入 review_queue 的某个字段（如 `ai_cleaned_content`）。

---

## 🔄 信号流全景图

```
用户点击「归档」
    ↓
ArchiveView._on_archive_url()
    ↓
ArchiveViewModel.archive_url()          # 设置 gui_mode=True 到工作流参数
    ↓
ArchiveWorker (QThread).run()
    → WorkflowEngine.execute_async()
      → FetchContentStep
      → AIAnalyzeStep
      → ReviewStep (GUI 模式: 创建 review_queue 记录, review_rejected=True)
      → StoreStep (被 review_rejected 阻止，跳过)
    ← 返回 {"review_id": Y, "pending_entry": {...}}
    ↓
ArchiveViewModel._on_worker_ok(data)
    ↓ result_ready.emit(data)
ArchiveView._on_result_ready(data)
    ↓ 检测到 data["review_id"] → 切换到 ReviewPanel 显示
ReviewPanel.load(review_id)
    ↓
ReviewViewModel.load_review(review_id)  # 从 ReviewManager 加载审核数据
    ↓ review_loaded.emit(review_item_dict)
ReviewPanel 填充 AI 摘要/标签/预览内容

用户修改/审核通过
    ↓
ReviewViewModel.approve()
    → ReviewManager.approve_review()
    → 触发实际入库（调用 SQLiteStore / MarkdownStore）
    ↓ review_approved.emit(knowledge_id)
ArchiveView 显示「入库成功」
```

---

## 🖥️ ReviewDraftsView（草稿管理视图）

```
导航索引 6：ReviewDraftsView

┌──────────────────────────────────────────────────────────┐
│ 待审核草稿  [🔄 刷新]  [徽章: 3 条待审核]               │
├──────────────────────────────────────────────────────────┤
│ [全部] [pending] [rejected]     [🔍 搜索]                │
├─────────────────┬────────────────────────────────────────┤
│ 草稿列表        │ 审核面板（复用 ReviewPanel）            │
│ ┌─────────────┐ │                                        │
│ │ #1 网页归档  │ │  ← 选中左侧条目后，右侧展示 ReviewPanel │
│ │ 2026-02-23  │ │                                        │
│ │ [pending]   │ │                                        │
│ └─────────────┘ │                                        │
│ ┌─────────────┐ │                                        │
│ │ #2 文本归档  │ │                                        │
│ │ 2026-02-22  │ │                                        │
│ │ [rejected]  │ │                                        │
│ └─────────────┘ │                                        │
└─────────────────┴────────────────────────────────────────┘
```

**MainWindow 变更**：

```python
_NAV_REVIEW_DRAFTS = 6   # 新增

def _init_ui(self):
    # ... 现有 5 个视图 ...
    self._review_drafts_view = ReviewDraftsView(self)
    self._stacked.addWidget(self._review_drafts_view)  # 索引 6

def switch_to_review_drafts(self) -> None:
    """切换到草稿管理视图（Ctrl+R 快捷键建议）。"""
    self._stacked.setCurrentIndex(_NAV_REVIEW_DRAFTS)
```

导航栏新增「草稿」图标，可显示 badge（待审核数量）。

---

## 🧪 测试要求

| 组件 | 单元测试 | 集成测试 |
|------|---------|---------|
| ReviewViewModel | ✅ 20+ (Mock ReviewManager) | ✅ 信号验证 |
| ReviewPanel | ✅ 10+ (Widget 显示/交互) | ✅ ViewModel 联动 |
| ReviewDraftsView | ✅ 10+ (列表/选中/刷新) | - |
| ReviewStep GUI 模式 | ✅ 5+ (gui_mode 分支) | ✅ 工作流 E2E |

---

## 📦 交付清单

- [ ] `src/gui/viewmodels/review_viewmodel.py` — ReviewViewModel 类
- [ ] `src/gui/views/review_panel.py` — ReviewPanel 可复用 Widget
- [ ] `src/gui/views/review_drafts_view.py` — ReviewDraftsView
- [ ] `src/gui/views/archive_view.py` — 修改：嵌入 ReviewPanel
- [ ] `src/gui/viewmodels/archive_viewmodel.py` — 修改：传递 review_id
- [ ] `src/gui/main_window.py` — 修改：注册新视图
- [ ] `src/workflow/steps.py` — 修改：ReviewStep GUI 模式适配
- [ ] `tests/unit/test_review_viewmodel.py` — 20+ 单元测试
- [ ] `tests/unit/test_review_panel.py` — 10+ 单元测试

---

## 🔌 扩展预留

- **AI 流式输出**：重新生成时可接入流式 token 显示（与 ChatView 一致）
- **审核统计 Badge**：导航图标显示待审核数量
- **键盘快捷键**：`Ctrl+R` 进入草稿管理，`Y/N` 快速审核通过/拒绝
- **批量审核**：草稿列表支持多选 + 批量操作

---

**文档版本**: v1.0
**创建日期**: 2026-02-24
**适用里程碑**: M16 - GUI 审核界面（Phase 3 规划）
**前置里程碑**: M14（CLI 审核）已完成；M12（GUI 框架）已完成
**设计者**: 浮浮酱 (猫娘工程师)
