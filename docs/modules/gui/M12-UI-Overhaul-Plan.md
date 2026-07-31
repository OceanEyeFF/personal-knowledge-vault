# PKV GUI 全面 UI 翻新方案

> 本文档供后续 Gemini 执行 UI 改动时使用，包含完整的分析和实施指引。
>
> **当前执行边界**：GUI 启动会加载正常本机配置并可能初始化数据库，只由用户在本机手动执行；Agent 只通过 `scripts/run-test.ps1` 运行离线单元测试。

## 1. 问题诊断

### 1.1 内联样式清单（需清理的硬编码颜色）

共 **9 个 Python 文件、40+ 处** 内联 `setStyleSheet()` 或 HTML 内联 `style=`：

**chat_view.py（最严重，~15 处）**
- `render_markdown()`：消息气泡颜色 `#F5F5F5`/`#E3F2FD`/`#4CAF50`/`#2196F3`/`#757575`/`#272822`/`#f8f8f2`
- TokenPanel 标题：`font-weight: bold; font-size: 14px`
- TokenPanel 警告标签：`background-color: #FFF3E0; color: #E65100`
- SessionSidebar 会话列表：`background-color: #F5F5F5`/`#E3F2FD`/`#E0E0E0`
- ChatArea message_display：`background-color: #FFFFFF`
- 6 处状态消息 HTML 内联颜色（保存/归档/错误提示）

**browser_view.py（4 处）**
- 3 处标题 `font-weight: bold; padding: 2px 0`
- 发送到对话按钮 `background-color: #4CAF50`（~17 行大块 QSS）

**archive_view.py（4 处）**：进度标签、结果标题/ID/路径标签

**settings_view.py（6 处）**：路径标签 ×3、状态标签 ×3（gray/green/red 切换）

**search_view.py（2 处）**：结果数量、预览标题

**stats_view.py（5 处）**：占位符 ×3、错误标签 ×2

**autocomplete_popup.py（1 处大块）**：~20 行完整 setStyleSheet

**knowledge_ref.py（1 处）**：`format_reference_card_html()` 引用卡片 HTML

**main_window.py（1 处）**：nav_panel border-right

### 1.2 暗色主题缺失

以上所有内联样式均为 light-only 颜色，切换暗色主题后全部失效。

### 1.3 现有 QSS 覆盖情况

`light.qss`/`dark.qss`（各 ~390 行）已覆盖：
- 基础控件：QMainWindow, QWidget, QPushButton, QLineEdit, QComboBox, QLabel
- 表格/树：QTableView, QTreeView, QHeaderView
- 列表：QListWidget
- 文本：QTextEdit, QPlainTextEdit
- 菜单：QMenuBar, QMenu
- 其他：QScrollBar, QGroupBox, QTabWidget, QProgressBar, QStatusBar, QToolBar

**缺失的组件样式**：导航面板、会话侧栏、Token 面板、消息显示区、聊天输入框、自动补全弹窗、归档结果框、按钮变体（绿色/红色）、属性选择器（text-muted 等）

---

## 2. 解决方案：3 层架构

### 层 1：theme_colors.py（新建文件）

**路径**：`src/gui/styles/theme_colors.py`（~80 行）

解决 QTextBrowser 中的 HTML 内联颜色问题（QTextBrowser 渲染的是 HTML，不受 QSS 控制）。

包含：
- `THEME_COLORS["light"]` / `THEME_COLORS["dark"]` — 语义化颜色字典
- `get_current_colors() -> dict` — 获取当前主题颜色
- `set_current_theme(theme: str)` — 主题切换时调用

颜色键值表：

| 键名 | 用途 | Light 值 | Dark 值 |
|------|------|----------|---------|
| `user_bg` | 用户消息气泡背景 | `#E3F2FD` | `#1A3A5C` |
| `user_border` | 用户消息左边框 | `#2196F3` | `#42A5F5` |
| `assistant_bg` | AI 消息气泡背景 | `#F5F5F5` | `#2D2D30` |
| `assistant_border` | AI 消息左边框 | `#4CAF50` | `#66BB6A` |
| `role_label` | 角色标签颜色 | `#757575` | `#9E9E9E` |
| `code_bg` | 代码块背景 | `#272822` | `#1E1E1E` |
| `code_fg` | 代码块文字 | `#f8f8f2` | `#D4D4D4` |
| `msg_fg` | 消息正文颜色 | `#2c2c2c` | `#D4D4D4` |
| `ref_card_bg` | 引用卡片背景 | `#E8F5E9` | `#1B3D1B` |
| `ref_card_border` | 引用卡片边框 | `#4CAF50` | `#66BB6A` |
| `ref_card_meta` | 引用元信息 | `#666666` | `#9E9E9E` |
| `ref_card_summary` | 引用摘要 | `#555555` | `#B0B0B0` |
| `status_info` | 信息提示 | `#1976D2` | `#42A5F5` |
| `status_success` | 成功提示 | `#4CAF50` | `#66BB6A` |
| `status_error` | 错误提示 | `#F44336` | `#EF5350` |
| `status_warning` | 警告提示 | `#FF9800` | `#FFA726` |
| `status_progress` | 进度提示 | `#666666` | `#9E9E9E` |
| `warning_bg` | 警告面板背景 | `#FFF3E0` | `#3E2723` |
| `warning_fg` | 警告面板文字 | `#E65100` | `#FFB74D` |
| `display_bg` | 消息显示区背景 | `#FFFFFF` | `#1E1E1E` |

### 层 2：QSS 扩展

两个 QSS 文件各追加 ~130 行，覆盖以下 objectName / 属性选择器：

```
/* 新增组件样式 */
#nav_panel, #nav_list           — 导航面板
#session_sidebar, #session_list — 会话侧栏
#token_panel, #token_panel_title, #token_warning — Token 面板
#message_display                — 消息显示区
#chat_input                     — 聊天输入框
#btn_send_to_chat               — 绿色按钮变体
#btn_stop                       — 红色按钮变体
#autocomplete_popup             — 自动补全弹窗
#archive_result_frame, #archive_result_title — 归档结果
QTextBrowser                    — 通用规则
QLabel[class="panel-header"]    — 面板标题
QLabel[class="text-muted"]      — 辅助文本
QLabel[status="success/error/muted"] — 动态状态
```

### 层 3：Python 文件改造

所有 `setStyleSheet()` 调用替换为 `setObjectName()` 或 `setProperty()`：

| 文件 | 关键改动 |
|------|---------|
| `main_window.py` | 删除 nav_panel 内联样式；nav_list 设 objectName；导航项加 emoji；宽度→130px；`apply_theme()` 集成 `set_current_theme()` |
| `chat_view.py` | `render_markdown()` 从 theme_colors 读颜色；TokenPanel/SessionSidebar/ChatArea 设 objectName 删内联样式；6 处状态 HTML 主题化；新增 `on_theme_changed()` |
| `browser_view.py` | 标题→`property("class","panel-header")`；按钮→`objectName("btn_send_to_chat")` |
| `archive_view.py` | 标签→`property("class","text-muted")`；结果框→objectName |
| `search_view.py` | 标签→text-muted/panel-header |
| `settings_view.py` | 路径标签→text-muted；状态标签→`property("status","success/error")` + unpolish/polish |
| `stats_view.py` | 占位符→text-muted；错误→status error |
| `autocomplete_popup.py` | 设 objectName，删 ~20 行 setStyleSheet |
| `knowledge_ref.py` | `format_reference_card_html()` 从 theme_colors 读颜色 |

---

## 3. 执行顺序

```
步骤 1+2: 基础设施 (theme_colors.py + QSS 扩展)
    ↓
步骤 3:   main_window.py (导航面板 + 主题集成)
    ↓
步骤 4:   chat_view.py (最大改动量)
    ↓
步骤 5:   autocomplete_popup.py + knowledge_ref.py (chat 相关)
    ↓
步骤 6:   browser_view.py → archive_view.py → search_view.py → settings_view.py → stats_view.py
    ↓
步骤 7:   验证 (6 视图 × 2 主题 + 单元测试)
```

## 4. 工作量估算

| 类别 | 行数 |
|------|------|
| 新建 theme_colors.py | +80 |
| QSS 追加（light+dark） | +260 |
| Python 修改 | ~130 改 |
| Python 删除（内联样式） | ~70 删 |
| **净增** | **~400 行** |

## 5. 验证清单

- [ ] 用户在本机手动启动 `python -m src.gui`（Agent 不执行）
- [ ] Light 主题下检查全部 6 个视图
- [ ] Dark 主题下检查全部 6 个视图
- [ ] Chat 消息气泡双主题可读性
- [ ] 代码块双主题显示效果
- [ ] 引用卡片双主题效果
- [ ] 自动补全弹窗双主题效果
- [ ] Token 警告面板双主题效果
- [ ] 导航栏 emoji + 选中/hover 效果
- [ ] `.\scripts\run-test.ps1 -Direct -DataRoot .data-test\gui-ui -Command @("pytest", "tests/unit/test_chat_viewmodel.py", "tests/unit/test_knowledge_ref.py", "-v")` 全部通过
