# Personal Knowledge Vault - Phase 2 开发 Prompt

> 用于 AI Agent（Claude Code/CodeX）的 Phase 2 扩展开发启动指令
>
> **版本**: 1.1
> **创建日期**: 2026-02-16
> **适用对象**: Claude Code、CodeX、GitHub Copilot Workspace 等 AI 开发工具
> **前置条件**: Phase 1 (v0.6.1) 已全部完成

---

## 🎯 Phase 2 概述

基于 Phase 1 的坚实基础，扩展 Personal Knowledge Vault 的能力边界：

**Phase 2 核心目标**:
1. 🔌 **MCP 服务** (v0.7.0) — 让 AI Agent 直接访问知识库
2. 🖥️ **GUI 桌面应用** (v0.8.0) — 图形化界面 + 内置 AI 交互
3. 🎬 **视频流支持** (v0.9.0) — B站/YouTube 视频转录与归档

**关键设计理念**:
- **AI 交互独立化** — 软件内置 AI 对话能力，不依赖 Claude Code 等外部工具
- **后端可替换** — GUI 前端与后端解耦，更换引擎不影响界面
- **渐进式扩展** — 每个版本独立交付，不阻塞其他功能

---

## 📚 必读文档

### Phase 1 核心文档（了解现有架构）
- [`docs/core/PHASE1_DEV_PROMPT.md`](./PHASE1_DEV_PROMPT.md) - Phase 1 开发记录（已归档）
- [`docs/core/personal-knowledge-vault-prd.md`](./personal-knowledge-vault-prd.md) - **核心需求文档**
- [`docs/core/架构设计.md`](./架构设计.md) - 工作流驱动架构
- [`docs/core/技术选型.md`](./技术选型.md) - 技术栈选型

### Phase 2 设计文档
- [`docs/design/MCP_SERVICE_DESIGN.md`](../design/MCP_SERVICE_DESIGN.md) - **MCP 服务技术设计**
- [`docs/design/GUI_FRAMEWORK_ANALYSIS.md`](../design/GUI_FRAMEWORK_ANALYSIS.md) - **GUI 框架选型分析**

### 接口规范（复用现有模块）
- [`docs/refactor/Entry数据模型规范.md`](../refactor/Entry数据模型规范.md) - 知识条目数据结构
- [`docs/refactor/Processors接口规范.md`](../refactor/Processors接口规范.md) - 内容处理器接口
- [`docs/refactor/Storage接口规范.md`](../refactor/Storage接口规范.md) - 三层存储架构
- [`docs/refactor/Retrieval检索引擎规范.md`](../refactor/Retrieval检索引擎规范.md) - 检索策略设计
- [`docs/refactor/WorkflowEngine接口规范.md`](../refactor/WorkflowEngine接口规范.md) - 工作流引擎接口

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

## 🏗️ Phase 2 里程碑

> **起点**: v0.6.1（Phase 1 完成，M1-M7）
> **目标**: v1.0.0（Phase 2 完成）
>
> **拆分原则**: 每个 Milestone 独立可测试、独立可交付、测试压力可控。

### 里程碑总览

| Milestone | 版本 | 主题 | 核心产出 | 预估规模 |
|-----------|------|------|---------|---------|
| **M8** | v0.7.0-alpha | MCP 只读服务 | 5 个只读 Tool + 4 个 Resource + 测试 | 小 |
| **M9** | v0.7.0 | MCP 写入 + Prompts | 3 个写入 Tool + 3 个 Prompt + 安全加固 | 小 |
| **M10** | v0.8.0-alpha | GUI 基础框架 | 主窗口 + 知识浏览 + 搜索 | 中 |
| **M11** | v0.8.0-beta | GUI 归档 + 设置 | 归档界面 + 设置界面 + 统计面板 | 中 |
| **M12** | v0.8.0 | AI 对话交互 | 聊天界面 + 对话服务 + 对话存储 | 大 |
| **M13** | v0.8.1 | GUI 打包与集成测试 | 打包分发 + E2E 测试 + 文档 | 小 |
| **M14** | v0.9.0 | 视频流支持（可选） | 视频下载/转录/分析/归档 | 大（优先级低） |

---

### Milestone 8: MCP 只读服务 (v0.7.0-alpha)

**目标**: 搭建 MCP Server 框架，实现所有只读查询能力

**技术方案**: 详见 [MCP_SERVICE_DESIGN.md](../design/MCP_SERVICE_DESIGN.md)

#### 技术基础（已验证）

- **SDK**: `mcp` (Python SDK v1.x)，从 `mcp.server.fastmcp` 导入 `FastMCP`
- **注册方式**: `@mcp.tool()` / `@mcp.resource()` / `@mcp.prompt()` 装饰器
- **同步支持**: Tool handler 可以是普通 `def`（非 async），SDK 自动处理
- **传输方式**: `mcp.run(transport="stdio")` 或 `mcp.run(transport="streamable-http")`
- **现有 API**: 检索/存储/AI 模块均为同步方法，可直接在 Tool handler 中调用

#### Tool 详细设计与可行性

##### Tool 1: `search_knowledge` — 搜索知识库 ✅ 低成本

```python
@mcp.tool(annotations=ToolAnnotations(title="搜索知识库", read_only_hint=True))
def search_knowledge(query: str, strategy: str = "auto", top_k: int = 5) -> dict:
```

| 项目 | 说明 |
|------|------|
| **调用链** | `QueryRouter.search(query, limit=top_k)` — 已有 sync API |
| **实现成本** | 约 15 行，直接调用现有 `QueryRouter` |
| **客户端兼容** | ✅ Claude Code / Cursor 均支持带参数的 Tool 调用 |
| **风险** | 无，核心路径已在 CLI 中验证过 |

##### Tool 2: `get_entry` — 获取条目详情 ✅ 低成本

```python
@mcp.tool(annotations=ToolAnnotations(title="获取知识条目", read_only_hint=True))
def get_entry(knowledge_id: int) -> dict:
```

| 项目 | 说明 |
|------|------|
| **调用链** | `SQLiteStore.query_by_id(knowledge_id)` + `MarkdownStore.load(file_path)` — 均已有 |
| **实现成本** | 约 20 行，查元数据 + 读 Markdown 全文 |
| **客户端兼容** | ✅ 标准单参数 Tool |
| **风险** | 无 |

##### Tool 3: `list_tags` — 列出标签 ⚠️ 需要新增 SQLiteStore 方法

```python
@mcp.tool(annotations=ToolAnnotations(title="列出知识库标签", read_only_hint=True))
def list_tags() -> dict:
```

| 项目 | 说明 |
|------|------|
| **调用链** | `SQLiteStore.get_all_tags_with_count()` — **需新增** |
| **新增 SQL** | `SELECT t.name, COUNT(*) as cnt FROM tags t JOIN knowledge_tags kt ON ... GROUP BY t.name ORDER BY cnt DESC` |
| **实现成本** | SQLiteStore 新增约 15 行 + Tool handler 约 10 行 |
| **客户端兼容** | ✅ 无参数 Tool，所有客户端均支持 |
| **风险** | 低，标准 SQL 聚合查询 |

##### Tool 4: `list_entries` — 浏览条目列表 ⚠️ 需要新增 SQLiteStore 方法

```python
@mcp.tool(annotations=ToolAnnotations(title="浏览知识条目列表", read_only_hint=True))
def list_entries(page: int = 1, per_page: int = 20, source_type: str = "") -> dict:
```

| 项目 | 说明 |
|------|------|
| **调用链** | `SQLiteStore.list_entries(limit, offset, ...)` + `SQLiteStore.count_entries(...)` — **均需新增** |
| **新增 SQL** | `SELECT ... FROM knowledge_items WHERE ... ORDER BY archived_at DESC LIMIT ? OFFSET ?` |
| **实现成本** | SQLiteStore 新增约 30 行 + Tool handler 约 15 行 |
| **客户端兼容** | ✅ 带默认值参数，Claude Code 可传也可不传 |
| **风险** | 低，标准分页查询 |

##### Tool 5: `get_stats` — 知识库统计 ⚠️ 需要新增 SQLiteStore 方法

```python
@mcp.tool(annotations=ToolAnnotations(title="知识库统计", read_only_hint=True))
def get_stats() -> dict:
```

| 项目 | 说明 |
|------|------|
| **调用链** | `SQLiteStore.get_statistics()` — **需新增** |
| **新增 SQL** | `SELECT COUNT(*), source_type, AVG(word_count) FROM knowledge_items GROUP BY ...` |
| **实现成本** | SQLiteStore 新增约 25 行 + Tool handler 约 5 行 |
| **客户端兼容** | ✅ 无参数 Tool |
| **风险** | 低 |

#### Resource 详细设计与可行性

##### Resource 1-2: `pkv://entries/{id}` 和 `pkv://entries/{id}/metadata` ✅ 低成本

```python
@mcp.resource("pkv://entries/{knowledge_id}")
def get_entry_content(knowledge_id: int) -> str:
    """返回 Markdown 全文"""

@mcp.resource("pkv://entries/{knowledge_id}/metadata")
def get_entry_metadata(knowledge_id: int) -> str:
    """返回 JSON 格式的元数据"""
```

| 项目 | 说明 |
|------|------|
| **调用链** | 与 `get_entry` Tool 相同，`query_by_id()` + `load()` |
| **实现成本** | 每个约 10 行 |
| **客户端兼容** | ✅ `@mcp.resource()` 支持 URI 模板参数 |
| **注意** | Resource 返回 `str`（文本），Tool 返回 `dict`（结构化数据），两者互补 |

##### Resource 3-4: `pkv://tags` 和 `pkv://stats` ✅ 低成本

```python
@mcp.resource("pkv://tags")
def get_tags_resource() -> str:
    """返回 JSON 格式的标签列表"""

@mcp.resource("pkv://stats")
def get_stats_resource() -> str:
    """返回 JSON 格式的统计信息"""
```

| 项目 | 说明 |
|------|------|
| **调用链** | 复用 `list_tags` / `get_stats` 的 SQLiteStore 方法，输出 `json.dumps()` |
| **实现成本** | 每个约 8 行 |
| **客户端兼容** | ✅ 静态 URI Resource，Claude Code 可通过 `read_resource` 读取 |

#### 前置工作：SQLiteStore 扩展（M8 核心工作量）

M8 最大的实现工作不是 MCP 框架搭建，而是补全 SQLiteStore 的查询能力：

| 新增方法 | SQL 复杂度 | 估算行数 |
|---------|-----------|---------|
| `list_entries(limit, offset, source_type, tag)` | 中（动态 WHERE + JOIN） | ~30 行 |
| `count_entries(source_type, tag)` | 低（COUNT + WHERE） | ~15 行 |
| `get_all_tags_with_count()` | 低（GROUP BY + ORDER BY） | ~15 行 |
| `get_statistics()` | 中（多个聚合查询） | ~25 行 |
| **合计** | | **~85 行** |

这些全是标准 SQL，基于现有 Schema 即可实现，不涉及任何表结构变更。

#### 交付文件清单

- [ ] `src/mcp/__init__.py` - MCP 模块初始化
- [ ] `src/mcp/server.py` - FastMCP 主入口（stdio + streamable-http）
- [ ] `src/mcp/tools.py` - 5 个只读 Tool
- [ ] `src/mcp/resources.py` - 4 个 Resource
- [ ] `src/mcp/utils.py` - 序列化和错误处理
- [ ] `src/storage/sqlite_store.py` - 补充 4 个查询方法
- [ ] `tests/unit/test_mcp_tools.py` - Tool 单元测试
- [ ] `tests/unit/test_mcp_resources.py` - Resource 单元测试
- [ ] `tests/unit/test_sqlite_store_new.py` - 新增 SQLiteStore 方法测试
- [ ] `tests/integration/test_mcp_integration.py` - MCP 集成测试

**新增依赖**:
```txt
mcp[cli]>=1.12.0      # MCP Python SDK（含 CLI 调试工具）
```

**验收标准**:
```bash
# stdio 模式启动
python -m src.mcp.server

# HTTP 模式启动
python -m src.mcp.server --transport streamable-http --port 3000

# MCP Inspector 测试（可视化验证所有 Tool/Resource）
npx @modelcontextprotocol/inspector python -m src.mcp.server
```

**验收检查点**:
1. MCP Inspector 中可发现所有 5 个 Tool 和 4 个 Resource
2. `search_knowledge` 返回正确结果，支持策略切换
3. `get_entry` 返回完整条目（含 Markdown 全文）
4. stdio 和 HTTP 两种传输方式均正常工作
5. 单元测试全部通过，覆盖率 ≥ 85%

---

### Milestone 9: MCP 写入 + Prompts (v0.7.0)

**目标**: 补全 MCP 写入能力、Prompt 模板和安全加固，交付完整 v0.7.0

**前置**: M8 完成

#### Tool 详细设计与可行性

##### Tool 6: `archive_url` — 归档网页 ✅ 低成本

```python
@mcp.tool(annotations=ToolAnnotations(title="归档网页"))
def archive_url(url: str) -> dict:
```

| 项目 | 说明 |
|------|------|
| **调用链** | `WorkflowEngine.execute("archive-url", {"url": url})` — 已有 sync API |
| **实现成本** | 约 20 行（调用 + 结果序列化 + 错误处理） |
| **客户端行为** | Claude Code 调用后等待返回（归档可能需 10-30 秒），属正常 Tool 调用流程 |
| **风险** | 归档耗时较长，但 MCP 协议无超时限制，客户端自行管理等待 |
| **安全** | 需验证 URL 格式，拒绝 `127.0.0.1` / `localhost` / `10.*` / `192.168.*` 内网地址 |

##### Tool 7: `archive_text` — 归档文本 ✅ 低成本

```python
@mcp.tool(annotations=ToolAnnotations(title="归档文本"))
def archive_text(text: str, title: str = "") -> dict:
```

| 项目 | 说明 |
|------|------|
| **调用链** | `WorkflowEngine.execute("archive-text", {"text": text, "title": title})` — **需确认是否已有 "archive-text" 工作流** |
| **实现成本** | 若工作流已有：约 15 行；若需新增工作流：约 50 行（含 YAML 配置） |
| **客户端兼容** | ✅ Claude Code 可直接传递文本内容 |
| **安全** | 文本长度限制 100,000 字符 |
| **注意** | 现有工作流只有 `archive-url`，可能需要新增 `archive-text` 工作流或复用通用文本处理流程 |

##### Tool 8: `get_related` — 获取关联知识 ✅ 中等成本

```python
@mcp.tool(annotations=ToolAnnotations(title="获取关联知识", read_only_hint=True))
def get_related(knowledge_id: int, limit: int = 5) -> dict:
```

| 项目 | 说明 |
|------|------|
| **调用链** | 读取条目 → 取其 embedding → `VectorRetriever` 做相似度搜索 |
| **实现成本** | 约 25 行（取已有 embedding + 向量搜索 + 排除自身） |
| **客户端兼容** | ✅ 标准 Tool |
| **风险** | 依赖向量索引已建立；若条目无 embedding 需优雅降级 |

#### Prompt 详细设计与可行性

> **关键说明**: MCP Prompt 不是"AI 系统提示词"，是**用户可通过客户端 UI 选择的提示词模板**。
> Claude Code 中用户可以在 Prompt 列表里选择一个模板，填入参数，模板会生成一段引导文本发送给 AI。
> 这完全是客户端标准功能，不需要魔改任何客户端。

##### Prompt 1: `search_and_summarize` ✅ 零成本

```python
@mcp.prompt()
def search_and_summarize(query: str) -> str:
    return f"请在知识库中搜索「{query}」，总结找到的内容，指出最相关的条目标题和关键信息。"
```

| 项目 | 说明 |
|------|------|
| **本质** | 纯字符串模板，返回 `str`，由客户端发送给 AI |
| **实现成本** | 5 行 |
| **客户端行为** | 用户在 Prompt 列表选择 → 填入 query → 客户端把生成的文本作为用户消息发出 → AI 自行决定调用 `search_knowledge` Tool |
| **不需要** | 不需要服务端主动调用 Tool，Prompt 只是生成引导文本 |

##### Prompt 2: `knowledge_qa` ✅ 零成本

```python
@mcp.prompt()
def knowledge_qa(question: str) -> str:
    return f"请基于知识库回答：{question}\n请先搜索相关条目，再基于内容回答，引用具体标题。"
```

| 项目 | 说明 |
|------|------|
| **实现成本** | 5 行 |
| **客户端行为** | 同上，AI 会自行调用 `search_knowledge` + `get_entry` |

##### Prompt 3: `idea_sharpen` ✅ 零成本

```python
@mcp.prompt()
def idea_sharpen(content: str) -> str:
    return f"对以下内容进行思想磨砺：\n\n{content[:2000]}\n\n请提问核心价值、关键观点、知识关联和应用场景。"
```

| 项目 | 说明 |
|------|------|
| **实现成本** | 5 行 |
| **客户端行为** | 用户粘贴一段内容 → Prompt 生成引导文本 → AI 自行展开对话 |
| **注意** | 截断至 2000 字符，避免 Prompt 过长 |

#### 安全加固

| 安全措施 | 实现方式 | 成本 |
|---------|---------|------|
| URL 格式验证 | `urllib.parse.urlparse()` + 正则匹配 | ~10 行 |
| 拒绝内网地址 | 检查 IP 是否为 `127.*` / `10.*` / `192.168.*` / `172.16-31.*` | ~15 行 |
| 文本长度限制 | `len(text) > 100000` → 返回错误 | ~5 行 |
| 参数范围限制 | `top_k = min(top_k, 50)`, `per_page = min(per_page, 100)` | ~5 行（已含在 Tool 中） |

**合计约 35 行安全代码**，放在 `src/mcp/utils.py` 中。

#### 与客户端的兼容性验证

| 客户端 | Tool 调用 | Resource 读取 | Prompt 选择 | 传输方式 |
|--------|----------|--------------|-------------|---------|
| **Claude Code** | ✅ 自动发现并调用 | ✅ 支持 `read_resource` | ✅ 支持 Prompt 列表 | stdio |
| **Claude Desktop** | ✅ | ✅ | ✅ | stdio |
| **Cursor** | ✅ | ✅ | ⚠️ Prompt 支持有限 | stdio / HTTP |
| **MCP Inspector** | ✅ 可视化测试 | ✅ | ✅ | stdio |

> **结论**: 所有设计均使用 MCP 标准协议能力，**不需要魔改任何客户端**。
> Tool 是 AI 自主调用，Resource 是数据读取，Prompt 是用户选择模板——三者各有其标准用途。

#### 交付文件清单

- [ ] `src/mcp/tools.py` 补充 3 个写入 Tool（archive_url, archive_text, get_related）
- [ ] `src/mcp/prompts.py` - 3 个 Prompt 模板
- [ ] `src/mcp/utils.py` 补充安全验证函数
- [ ] `config/workflows/archive-text.yaml` - 文本归档工作流（若不存在）
- [ ] `tests/unit/test_mcp_prompts.py` - Prompt 模板测试
- [ ] `tests/unit/test_mcp_security.py` - 安全验证测试
- [ ] `docs/MCP_INTEGRATION_GUIDE.md` - Claude Desktop / Cursor 配置文档
- [ ] 更新 README.md、CHANGELOG.md

**验收检查点**:
1. Claude Code 能通过 MCP 归档 URL 和文本
2. MCP Prompt 模板在 MCP Inspector 中正确显示并可填入参数
3. 安全验证：`archive_url("http://127.0.0.1/admin")` 被拒绝
4. 完整的配置文档（用户可照做集成到 Claude Desktop / Cursor）
5. 所有测试通过，MCP 模块覆盖率 ≥ 85%

---

### Milestone 10: GUI 基础框架 + 知识浏览 (v0.8.0-alpha)

**目标**: 搭建 PySide6 GUI 框架，实现知识库浏览和搜索两大核心只读界面

**技术选型**: PySide6 (Qt 6.8 LTS) — 详见 [GUI_FRAMEWORK_ANALYSIS.md](../design/GUI_FRAMEWORK_ANALYSIS.md)

**前置**: M8 完成（共享 Service Layer，但 GUI 不依赖 MCP）

**交付物**:

- [ ] `src/gui/__init__.py` - GUI 模块初始化
- [ ] `src/gui/app.py` - QApplication 主入口（事件循环、异常处理）
- [ ] `src/gui/main_window.py` - 主窗口
  - [ ] QMainWindow 框架（菜单栏 + 工具栏 + 状态栏）
  - [ ] 侧边导航栏（视图切换）
  - [ ] 全局快捷键注册
- [ ] `src/gui/styles/` - QSS 样式表
  - [ ] `light.qss` - 明亮主题
  - [ ] `dark.qss` - 暗色主题
- [ ] `src/gui/assets/` - 图标和资源文件
- [ ] `src/gui/views/browser_view.py` - 知识库浏览界面
  - [ ] 左侧：标签树 / 来源分类（QTreeView）
  - [ ] 中间：条目列表（QTableView + 自定义 Model）
  - [ ] 右侧：Markdown 预览（QWebEngineView 或 QTextEdit）
- [ ] `src/gui/views/search_view.py` - 搜索界面
  - [ ] 搜索框 + 策略选择（BM25/向量/混合/自动）
  - [ ] 结果列表 + 高亮匹配
  - [ ] 快捷键支持（Ctrl+K 全局搜索）
- [ ] `src/gui/models/` - Qt MVC 数据模型
  - [ ] `entry_model.py` - 知识条目数据模型
  - [ ] `tag_model.py` - 标签数据模型
- [ ] `tests/unit/test_gui_models.py` - 数据模型单元测试

**新增依赖**:
```txt
PySide6>=6.8.0        # Qt for Python (LTS)
PySide6-Addons>=6.8.0 # QWebEngineView 等
```

**验收检查点**:
1. `python -m src.gui.app` 启动后显示主窗口（无崩溃）
2. 知识库浏览：标签树正确显示 → 点击标签筛选列表 → 点击条目预览 Markdown
3. 搜索功能：输入关键词 → 返回结果 → 点击查看详情
4. 明亮/暗色主题可切换
5. 窗口关闭不产生资源泄漏

---

### Milestone 11: GUI 归档 + 设置 (v0.8.0-beta)

**目标**: 实现 GUI 写入能力（归档界面）和用户配置管理

**前置**: M10 完成

**交付物**:

- [ ] `src/gui/views/archive_view.py` - 归档界面
  - [ ] URL 归档表单（输入框 + 归档按钮）
  - [ ] 文本归档编辑器（多行文本框 + 可选标题）
  - [ ] 进度显示（QProgressBar + 状态文字）
  - [ ] 结果预览和确认（归档完成后跳转查看）
- [ ] `src/gui/viewmodels/archive_viewmodel.py` - 归档 ViewModel
  - [ ] 异步工作流调用（不阻塞 UI 线程）
  - [ ] 进度信号发射
- [ ] `src/gui/views/stats_view.py` - 统计面板
  - [ ] 知识库概况（条目总数、标签分布、来源分布）
  - [ ] 简单图表展示（可选：matplotlib 或纯 Qt 绘制）
- [ ] `src/gui/views/settings_view.py` - 设置界面
  - [ ] API Key 配置（DeepSeek、OpenAI）
  - [ ] 检索策略默认值配置
  - [ ] 主题切换
  - [ ] 数据目录设置
- [ ] `src/gui/viewmodels/settings_viewmodel.py` - 设置 ViewModel
- [ ] `tests/unit/test_gui_archive.py` - 归档流程测试

**验收检查点**:
1. 输入 URL → 点击归档 → 进度条显示 → 完成后可在浏览界面查看
2. 输入文本 → 归档为知识条目 → 搜索可命中
3. 统计面板正确显示数据概况
4. 设置修改后立即生效（无需重启）
5. 归档过程 UI 不冻结（异步执行）

---

### Milestone 12: AI 对话交互 (v0.8.0)

**目标**: 实现内置 AI 对话能力，包含聊天界面、对话服务、对话记录存储

**前置**: M11 完成

**交付物**:

- [ ] `src/gui/views/chat_view.py` - AI 聊天界面
  - [ ] 聊天消息列表（QListView + 自定义 Delegate）
  - [ ] 用户输入框（QTextEdit + 发送按钮）
  - [ ] 流式输出显示（逐字显示 AI 回复）
  - [ ] 消息气泡样式（用户/AI 区分）
  - [ ] 会话管理（新建/切换/删除会话）
- [ ] `src/gui/viewmodels/chat_viewmodel.py` - 聊天 ViewModel
  - [ ] 消息发送与接收信号
  - [ ] 流式输出状态管理
- [ ] `src/gui/services/ai_chat_service.py` - AI 对话服务
  - [ ] 直接调用 DeepSeek API（不依赖 Claude Code）
  - [ ] 流式响应处理（SSE / async generator）
  - [ ] 对话预设模板加载（宽松预设 + 场景自适应，见设计决策 2）
- [ ] `src/gui/services/knowledge_context.py` - 知识上下文管理
  - [ ] 自动检索相关知识作为对话背景
  - [ ] 上下文窗口管理（token 预算控制）
- [ ] `src/ai/chat_presets.py` - 对话预设模板（具体场景在本里程碑中明确）
- [ ] 数据库迁移: `scripts/migrations/003_add_chat_sessions.sql`
  - [ ] `chat_sessions` 表（见设计决策 1）
  - [ ] 相关索引
- [ ] `tests/unit/test_ai_chat_service.py` - 对话服务测试
- [ ] `tests/unit/test_chat_sessions.py` - 对话记录存储测试
- [ ] 更新 README.md、CHANGELOG.md

**验收检查点**:
1. 输入问题 → 流式显示 AI 回复（逐字输出，不卡顿）
2. AI 回复自动引用知识库中的相关条目
3. 对话记录正确存储到 SQLite 并可恢复
4. 新建/切换/删除会话正常工作
5. 网络异常时优雅降级（显示错误提示，不崩溃）
6. 所有测试通过

---

### Milestone 13: GUI 打包与集成验证 (v0.8.1)

**目标**: 完成 GUI 应用的打包分发、E2E 测试和用户文档

**前置**: M12 完成

**交付物**:

- [ ] 打包配置
  - [ ] PyInstaller spec 文件（或 Nuitka 配置）
  - [ ] 打包脚本（`scripts/build-gui.ps1`）
  - [ ] 打包产物验证（启动测试、资源完整性）
- [ ] E2E 测试
  - [ ] `tests/e2e/test_gui_e2e.py` - GUI 端到端测试（pytest-qt）
  - [ ] 覆盖：启动 → 浏览 → 搜索 → 归档 → 聊天 完整流程
- [ ] 用户文档
  - [ ] 安装指南（含打包产物使用说明）
  - [ ] GUI 使用手册（截图 + 操作说明）
  - [ ] 更新 README.md、CHANGELOG.md、使用手册

**打包命令**:
```bash
# 使用 PyInstaller 打包
pyinstaller --onedir --windowed src/gui/app.py --name "PKV"

# 或使用 Nuitka (更好性能)
nuitka --standalone --enable-plugin=pyside6 src/gui/app.py
```

**验收检查点**:
1. 打包产物可在干净环境中启动运行
2. E2E 测试覆盖核心用户流程
3. 用户文档清晰完整，新用户可照做使用
4. 打包体积在合理范围（< 200MB）

---

### Milestone 14: 视频流支持 (v0.9.0) — 优先级低，可选

**目标**: 支持 B站/YouTube 视频转录、章节摘要和归档

> **优先级说明**: M14 为 Phase 2 的可选扩展。MCP (v0.7.0) 和 GUI (v0.8.x) 是核心交付，
> 视频流支持不阻塞主流程，可根据实际需要决定是否实现。

**前置**: M9 完成（需要工作流引擎支持）

**交付物**:

#### M14.1: 视频下载与转录

- [ ] `src/processors/video_processor.py` - 视频处理器基类
- [ ] `src/processors/bilibili_processor.py` - B站视频处理器
  - [ ] 视频信息获取（标题、简介、弹幕）
  - [ ] 音频提取（FFmpeg）
  - [ ] 语音转文字（Whisper API 或本地模型）
- [ ] `src/processors/youtube_processor.py` - YouTube 视频处理器
  - [ ] 字幕获取（优先官方字幕）
  - [ ] Whisper 转录（无字幕时）

#### M14.2: 视频内容分析

- [ ] 章节分割（基于时间戳和内容变化）
- [ ] 章节摘要生成（每章 100-200 字）
- [ ] 全文摘要生成（1000-2000 字 "菁萃文本"）
- [ ] 关键时间点标注

#### M14.3: 存储与检索

- [ ] 视频 Markdown 模板（含时间戳、章节、弹幕分析）
- [ ] `video_timestamps` 表激活（已在 Schema 中预留）
- [ ] 支持按时间点检索和跳转

**新增依赖**:
```txt
yt-dlp>=2024.0        # 视频下载
openai-whisper>=20240  # 语音转文字（可选本地模型）
ffmpeg-python>=0.2.0   # 音视频处理
```

**验收标准**:
1. B站视频：输入链接 → 下载 → 转录 → 生成章节摘要 → 归档
2. YouTube 视频：输入链接 → 获取字幕/转录 → 生成摘要 → 归档
3. 视频条目在搜索中可被检索到
4. 支持按章节/时间点浏览内容

---

## 🧪 测试要求

### 每个 Milestone 的测试标准

| 测试类型 | 最低覆盖率 | 工具 |
|---------|-----------|------|
| 单元测试 | 85% | pytest + mock |
| 集成测试 | 关键路径 100% | pytest |
| GUI 测试 | 核心流程 | pytest-qt |
| MCP 测试 | 所有 Tool/Resource | MCP Inspector |

### GUI 测试策略

```python
# 使用 pytest-qt 测试 GUI
def test_search_view(qtbot):
    view = SearchView()
    qtbot.addWidget(view)

    # 模拟搜索
    qtbot.keyClicks(view.search_input, "分布式系统")
    qtbot.mouseClick(view.search_button, Qt.LeftButton)

    # 验证结果
    assert view.result_table.rowCount() > 0
```

---

## 📦 最终交付清单

### v0.7.0-alpha 交付 (M8)
- [ ] `src/mcp/` - MCP 只读服务模块（5 个文件）
- [ ] 5 个只读 Tool + 4 个 Resource
- [ ] 单元测试 + 集成测试
- [ ] stdio + HTTP 双传输支持

### v0.7.0 交付 (M9)
- [ ] 3 个写入 Tool + 3 个 Prompt 模板
- [ ] 安全加固（输入验证、长度限制）
- [ ] Claude Desktop / Cursor 配置文档
- [ ] 更新 README.md、CHANGELOG.md

### v0.8.0-alpha 交付 (M10)
- [ ] GUI 基础框架（主窗口 + 明暗主题）
- [ ] 知识库浏览界面（标签树 + 列表 + 预览）
- [ ] 搜索界面（关键词搜索 + 结果展示）

### v0.8.0-beta 交付 (M11)
- [ ] 归档界面（URL + 文本归档 + 进度显示）
- [ ] 设置界面（API Key、主题、检索策略）
- [ ] 统计面板

### v0.8.0 交付 (M12)
- [ ] AI 聊天界面（流式输出 + 消息气泡 + 会话管理）
- [ ] AI 对话服务（DeepSeek API 直调 + 知识上下文注入）
- [ ] 对话记录存储（chat_sessions 迁移）
- [ ] 对话预设模板
- [ ] 更新 README.md、CHANGELOG.md

### v0.8.1 交付 (M13)
- [ ] 打包脚本（PyInstaller / Nuitka）
- [ ] E2E 测试套件（pytest-qt）
- [ ] 用户安装指南 + GUI 使用手册

### v0.9.0 交付 (M14, 可选)
- [ ] `src/processors/video_processor.py` - 视频处理器
- [ ] `src/processors/bilibili_processor.py` - B站处理器
- [ ] `src/processors/youtube_processor.py` - YouTube 处理器
- [ ] 视频 Markdown 模板
- [ ] 更新文档

---

## 🧩 关键设计决策

> 以下设计决策在 Phase 2 规划阶段讨论确认，作为后续实现的指导约束。

### 决策 1: 人机对话记录 — SQLite JSON 存储

**背景**: 人机对话记录与知识条目不同，是结构化的增量追加数据，不适合 Markdown 主存储模式。

**决策**: 采用 SQLite `chat_sessions` 表 + JSON `messages` 列。

```sql
-- 增量迁移: 003_add_chat_sessions.sql
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,              -- UUID
    title TEXT,                               -- 会话标题（AI 自动生成或用户命名）
    session_type TEXT NOT NULL,               -- 会话类型（具体场景待定）
    context_entry_id INTEGER,                 -- 关联的知识条目（可选）
    messages TEXT NOT NULL,                   -- JSON 格式的对话记录
    message_count INTEGER DEFAULT 0,
    model_used TEXT,                          -- 使用的 AI 模型
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (context_entry_id)
        REFERENCES knowledge_items(knowledge_id) ON DELETE SET NULL
);
```

**messages JSON 格式**（与 DeepSeek/OpenAI API 对齐）:
```json
[
  {"role": "system",    "content": "...", "timestamp": "..."},
  {"role": "user",      "content": "...", "timestamp": "..."},
  {"role": "assistant", "content": "...", "timestamp": "...", "context_refs": ["KID-001"]}
]
```

**理由**:
- SQLite `json_extract()` 原生支持查询 JSON 列
- 与 AI API 的 message 数组格式零转换成本
- 增量追加高效，无需改写文件
- 对话记录是软件交互数据，不需要 Markdown "数据主权"模式

### 决策 2: AI 对话初始提示词 — 宽松预设 + 场景自适应

**背景**: 内置 AI 对话需要初始 system prompt，但不应过度限制。

**决策**: 采用"分场景宽松预设"模式。

**设计模式**:
```python
CHAT_PRESETS = {
    "<场景名>": {
        "name": "显示名称",
        "system_prompt": "宽松的角色描述 + 动态上下文注入（{entry_count}, {top_tags}）",
        "temperature": 0.3 ~ 0.7,  # 随场景调整
    },
    # ... 具体场景在 M9.4 实现时明确
}
```

**设计原则**:
- **宽松而非空白** — 有基本引导但不限制自由度
- **动态上下文** — 运行时注入知识库概况（条目数、热门标签等）
- **用户可自定义** — 设置界面提供修改入口
- **不做的事**: 不强制角色扮演、不写冗长规则列表、不留完全空白

**具体场景列表**: 待 M9.4（AI 聊天交互）实现时根据实际需求明确。

### 决策 3: TUI/GUI 与 MCP 的集成策略 — 分步走

**背景**: MCP 解决跨进程/跨工具通信，GUI 内部不需要这层抽象。

**决策**: 三个入口共享 Service Layer，各自独立演进。

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│   CLI 入口   │  │  MCP Server │  │  GUI 应用    │
│  (v0.6.x)   │  │  (v0.7.0)   │  │  (v0.8.0)   │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘
       │                │                │
       └────────────────┼────────────────┘
                        ↓
              ┌──────────────────┐
              │  Service Layer    │
              │  (共享核心逻辑)    │
              │  • WorkflowEngine │
              │  • RetrievalEngine│
              │  • AI Services    │
              │  • Storage Layer  │
              └──────────────────┘
```

**分步计划**:
1. **v0.7.0**: MCP Server 作为独立进程（stdio/HTTP），不依赖 GUI
2. **v0.8.0**: GUI 通过 Python import 直接调用 Service Layer，**不经过 MCP**
3. **远期**: 按需集成（GUI 内嵌 MCP Server / GUI 作为 MCP Client 调用外部服务）

**理由**:
- 进程内调用（~0ms）远优于 MCP 协议通信（~50ms + 序列化开销）
- Service Layer 复用是"后端可替换"的真正实现
- MCP 用于外部 AI Agent 接入，GUI 内部没必要绕这一层

### 决策 4: AI 对话笔记能力 — MCP 轻量方案 + GUI 完整方案

**背景**: 用户希望在阅读归档文章后，通过 AI 对话记录自己的想法和进一步探索，并将这些对话保存到知识库。

**核心矛盾**: MCP 协议的定位是"AI 的工具箱"，不是"对话管道"。

```
Claude Code 客户端                          MCP Server（我们的服务）
     │                                          │
     │  ──── 调用 Tool ──────────────────→      │  ← 仅在 Tool 调用时我们能记录
     │  ←─── 返回结果 ───────────────────       │
     │                                          │
     │  用户和 AI 的自由对话                      │  ← 这部分我们完全看不到
     │  （全在客户端内部）                        │  ← MCP Server 无法感知对话流
     │                                          │
```

**决策**: 采用**两阶段方案**，MCP 先做轻量版，GUI 再做完整版。

#### 阶段 1: MCP "手动保存笔记" (M9, v0.7.0)

利用现有 MCP Tool + Prompt 组合，实现 70% 可用的对话笔记体验：

**典型使用流程**:
```
1. 用户选择 Prompt "knowledge_qa" 或 "idea_sharpen"
   → 自动注入文章上下文（AI 调用 get_entry Tool）
2. 用户与 AI 自由讨论文章内容
   → 过程中 AI 可调用 search_knowledge / get_related 拉取关联知识
3. 讨论结束时用户说"总结我们的对话并保存到知识库"
   → AI 调用 archive_text(text="对话摘要...", title="XXX文章笔记")
```

| 能力 | 支持情况 | 说明 |
|------|---------|------|
| AI 读取已归档文章 | ✅ 完全支持 | `get_entry` Tool |
| 对话中拉取关联知识 | ✅ 完全支持 | `search_knowledge` / `get_related` Tool |
| 用户主动保存笔记 | ✅ 完全支持 | 用户指示 AI 调用 `archive_text` |
| 自动记录完整对话 | ❌ 无法实现 | MCP Server 看不到客户端内部对话流 |
| 对话结束自动保存 | ❌ 无法实现 | MCP 无生命周期钩子 |

**M9 无需额外开发** — 以上能力完全由已设计的 Tool + Prompt 组合覆盖，
用户只需在对话结束时主动要求保存即可。

#### 阶段 2: GUI "完整对话记录" (M12, v0.8.0)

GUI 内完全控制对话流，可实现自动记录：

```python
# GUI 内我们掌控整个对话生命周期
class ChatView:
    def on_send_message(self, user_msg):
        # 1. 记录用户消息
        self.session.append({"role": "user", "content": user_msg})
        # 2. 发给 AI 并流式接收
        response = ai_service.chat_stream(self.session.messages)
        # 3. 自动记录 AI 回复
        self.session.append({"role": "assistant", "content": response})
        # 4. 实时持久化到 chat_sessions 表
        self.session.save()

    def on_session_end(self):
        # 自动生成摘要并可选归档为知识条目
        summary = ai_service.summarize(self.session.messages)
        self.session.update(title=summary.title)
```

| 能力 | 支持情况 | 说明 |
|------|---------|------|
| 完整对话自动记录 | ✅ 完全支持 | 每条消息实时写入 `chat_sessions` |
| 对话结束自动保存 | ✅ 完全支持 | `on_session_end` 钩子 |
| 历史会话恢复 | ✅ 完全支持 | 从 SQLite 加载 |
| 对话摘要自动生成 | ✅ 完全支持 | AI 总结 + 归档为知识条目 |
| 关联知识自动注入 | ✅ 完全支持 | `knowledge_context.py` 管理上下文 |

#### 两阶段对比

| 维度 | MCP 方案 (M9) | GUI 方案 (M12) |
|------|--------------|----------------|
| **对话记录** | 手动触发保存 | 完全自动 |
| **体验流畅度** | ⭐⭐⭐ 够用 | ⭐⭐⭐⭐⭐ 最佳 |
| **额外开发成本** | 零（复用已有 Tool） | 含在 M12 交付中 |
| **适用场景** | 快速笔记、偶尔记录 | 深度探索、系统性学习 |
| **用户群体** | 习惯 Claude Code 的开发者 | 所有用户 |

**结论**: 两者不冲突，MCP 版是 GUI 版的轻量前置方案。
M9 即可使用"对话+笔记"功能（手动保存），M12 提供完整自动化体验。

---

## ⚡ 开发原则提醒

继承 Phase 1 所有原则，并新增：

1. **不破坏现有功能** — Phase 2 是增量扩展，CLI 功能必须保持完整
2. **前后端解耦** — GUI 通过 ViewModel 层调用 Service，不直接访问存储
3. **AI 交互自主** — 软件内置 AI 对话，不强制要求 Claude Code
4. **渐进式交付** — 7 个 Milestone 各自独立可测试、独立可交付
5. **用户体验优先** — GUI 设计关注易用性，降低使用门槛

---

## 🔮 Phase 2 之后的展望

Phase 2 核心完成后（v0.8.1），系统将具备：
- ✅ CLI + GUI + MCP 三种交互方式
- ✅ 内置 AI 对话能力（不依赖外部工具）
- ✅ 文字 + 视频内容归档
- ✅ 完整的知识管理生命周期

**Phase 3 方向（待评估）**:
- 知识图谱可视化（D3.js / Cytoscape）
- PDF 书籍处理（OCR + 章节提取）
- 人物分析和事件梳理
- 多设备同步（加密）
- 性能优化（大规模知识库）

---

**准备好了吗？让我们继续构建这个 AI-First 的知识管理系统！** 🚀

Phase 2 的关键词：**MCP 连接 · GUI 可视化 · AI 自主交互**

---

**文档版本**: v1.1
**创建日期**: 2026-02-16
**最后更新**: 2026-02-16 (v1.1: 里程碑细粒度拆分 M8-M14)
**作者**: OceanEye (Product Owner) & 幽浮喵 (猫娘工程师)
