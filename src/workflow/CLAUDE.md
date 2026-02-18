# Workflow 模块

[根目录](../../CLAUDE.md) > [src](..) > **workflow**

---

## 模块职责

**工作流编排引擎**：提供 YAML 配置驱动的步骤编排、进度追踪、错误处理与状态管理。

### 核心理念

- **配置驱动**: 工作流定义在 YAML 文件中，易于扩展和修改
- **步骤编排**: 顺序执行步骤，支持条件跳过和错误处理
- **上下文传递**: 通过 `WorkflowContext` 在步骤间传递数据
- **可观测性**: 记录详细日志，支持进度追踪

---

## 入口与启动

### 快速使用

```python
from src.workflow import WorkflowEngine

engine = WorkflowEngine()

# 执行工作流
result = await engine.execute_async(
    workflow_name="archive-url",
    input_data={"url": "https://mp.weixin.qq.com/xxx"}
)

if result.success:
    print("成功:", result.data)
    print("日志:", result.logs)
else:
    print("失败:", result.errors)
```

### 同步执行

```python
# 同步执行（内部调用 asyncio.run）
result = engine.execute(
    workflow_name="archive-url",
    input_data={"url": "https://example.com"}
)
```

---

## 对外接口

### WorkflowEngine (核心引擎)

```python
class WorkflowEngine:
    def __init__(self, reload_config: bool = False):
        """
        初始化工作流引擎

        Args:
            reload_config: 是否每次执行都重新加载配置
        """

    async def execute_async(
        self,
        workflow_name: str,
        input_data: Dict[str, Any]
    ) -> WorkflowResult:
        """异步执行工作流"""

    def execute(
        self,
        workflow_name: str,
        input_data: Dict[str, Any]
    ) -> WorkflowResult:
        """同步执行工作流（包装 execute_async）"""

    def register_step(
        self,
        step_type: str,
        step_class: Type[BaseStep]
    ):
        """注册自定义步骤类型"""
```

---

### BaseStep (步骤基类)

所有工作流步骤继承此基类:

```python
class BaseStep(ABC):
    @abstractmethod
    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        """
        执行步骤逻辑

        Args:
            context: 工作流上下文

        Returns:
            步骤输出数据（会更新到 context.state）
        """
        pass

    def should_skip(self, context: WorkflowContext) -> bool:
        """
        是否跳过此步骤（可选重写）

        Returns:
            True: 跳过，False: 执行
        """
        return False
```

---

### 内置步骤

#### 1. FetchStep (内容抓取)

```python
class FetchStep(BaseStep):
    """
    抓取内容步骤

    输入: context.state["url"]
    输出: context.state["entry"] (Entry 数据类)
    """
    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        url = context.state.get("url")
        processor = get_processor(url)
        entry = await processor.process(url)
        return {"entry": entry}
```

---

#### 2. AnalyzeStep (AI 分析)

```python
class AnalyzeStep(BaseStep):
    """
    AI 分析步骤（可选）

    输入: context.state["entry"]
    输出: 更新 entry 的 tags, abstract, summary_* 字段
    """
    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        entry = context.state.get("entry")

        # 调用 AI 服务生成摘要和标签
        if not entry.abstract:
            entry.abstract = await deepseek.summarize(entry.content, max_words=300)
        if not entry.tags:
            entry.tags = await deepseek.extract_tags(entry.content)

        return {"entry": entry}
```

---

#### 3. IdeaSharpenStep (交互式确认)

```python
class IdeaSharpenStep(BaseStep):
    """
    idea Sharpen 步骤（人机交互）

    根据配置条件触发交互式对话，让用户确认或补充信息。
    """
    def should_skip(self, context: WorkflowContext) -> bool:
        """
        跳过条件:
        - content_length < 3000
        - concept_count < 5
        """
        entry = context.state.get("entry")
        if not entry:
            return True
        if entry.word_count < 3000:
            return True
        return False

    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        # 触发交互式对话（CLI 或 Rich Console）
        questions = [
            "这篇内容的核心价值是什么？",
            "你想记住哪些关键点？",
        ]
        answers = await self._prompt_user(questions)
        return {"user_notes": answers}
```

---

#### 4. StoreStep (存储)

```python
class StoreStep(BaseStep):
    """
    存储步骤

    输入: context.state["entry"]
    输出: 保存到 Markdown + SQLite + Vector
    """
    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        entry = context.state.get("entry")

        # 1. 保存到 Markdown
        md_path = md_store.save(entry)

        # 2. 保存到 SQLite
        sql_store.save_entry(entry)

        # 3. 保存到向量索引
        await vec_store.add_entry(entry)

        return {
            "markdown_path": str(md_path),
            "knowledge_id": entry.knowledge_id
        }
```

---

## 关键依赖与配置

### 依赖模块

- `src.processors`: 内容处理器
- `src.storage`: 存储层
- `src.ai`: AI 服务
- `src.utils.config`: 配置加载

### 工作流配置

配置文件位置: `config/workflows/<workflow-name>.yaml`

当前已有 3 个工作流配置:

| 工作流 | 配置文件 | 用途 | 调用者 |
|--------|---------|------|--------|
| `archive-url` | `archive-url.yaml` | 归档网页 | CLI `archive` + MCP `archive_url` |
| `archive-text` | `archive-text.yaml` | 归档纯文本 (M9 新增) | MCP `archive_text` |
| `search` | `search.yaml` | 搜索知识库 | CLI `search` + MCP `search_knowledge` |

**archive-text 与 archive-url 的区别**:
- `archive-text` 跳过 `fetch_content` 步骤(文本由 MCP Tool 层预构建 Entry)
- `archive-text` 跳过 `idea_sharpen` 步骤(MCP 场景无终端交互)
- `archive-text` 的 `ai_analyze` 设置 `on_error: continue`(AI 失败不阻断)

---

## 数据模型

### WorkflowContext (上下文)

在步骤间传递数据:

```python
@dataclass
class WorkflowContext:
    state: State                # 当前状态（字典）
    logs: List[str]             # 日志记录
    initial_input: Dict[str, Any]  # 初始输入

    def log(self, message: str):
        """记录日志"""
        self.logs.append(message)

    def update(self, data: Dict[str, Any]):
        """更新状态"""
        self.state.update(data)
```

---

### WorkflowResult (输出)

工作流执行结果:

```python
@dataclass
class WorkflowResult:
    success: bool               # 是否成功
    data: Dict[str, Any]        # 输出数据（State 的最终状态）
    errors: List[str]           # 错误信息
    logs: List[str]             # 日志记录
```

---

### State (状态字典)

继承自 `dict`，用于在步骤间传递数据:

```python
class State(dict):
    """
    工作流状态字典

    典型数据流:
    - 初始: {"url": "https://..."}
    - 步骤 1 (Fetch): {"url": "...", "entry": Entry(...)}
    - 步骤 2 (Analyze): {"url": "...", "entry": Entry(...)}
    - 步骤 3 (Store): {"url": "...", "entry": ..., "knowledge_id": "..."}
    """
    def to_dict(self) -> Dict[str, Any]:
        """转换为普通字典"""
        return dict(self)
```

详细规范: [docs/refactor/Workflow数据模型规范.md](../../docs/refactor/Workflow数据模型规范.md)

---

## 工作流编排示例

### 归档工作流 (archive-url)

```
输入: {"url": "https://mp.weixin.qq.com/xxx"}

步骤 1: FetchStep
  → 调用 WechatProcessor
  → 输出: {"entry": Entry(...)}

步骤 2: AnalyzeStep (可选)
  → 调用 DeepSeek 生成摘要
  → 输出: {"entry": Entry(...)} (更新 tags/abstract)

步骤 3: IdeaSharpenStep (条件触发)
  → 如果 word_count > 3000，触发交互对话
  → 输出: {"user_notes": "..."}

步骤 4: StoreStep
  → 保存到 Markdown/SQLite/Vector
  → 输出: {"markdown_path": "...", "knowledge_id": "..."}

最终输出: WorkflowResult(
    success=True,
    data={
        "url": "...",
        "entry": ...,
        "markdown_path": "...",
        "knowledge_id": "..."
    },
    logs=[...]
)
```

### 归档文本工作流 (archive-text, M9 新增)

```
输入: {"entry": Entry(...)}  # MCP Tool 层已构建好 Entry

步骤 1: AnalyzeStep (on_error: continue)
  → 调用 DeepSeek 生成摘要和标签
  → 失败时保留 TextFallbackProcessor 的默认摘要

步骤 2: StoreStep
  → 保存到 Markdown/SQLite/Vector
  → 输出: {"markdown_path": "...", "knowledge_id": "..."}

最终输出: WorkflowResult(success=True, ...)
```

---

## 测试与质量

### 单元测试

```bash
# 运行所有工作流测试
python -m pytest tests/unit/test_workflow_*.py -v

# 测试工作流引擎
python -m pytest tests/unit/test_workflow_engine.py -v

# 测试数据模型
python -m pytest tests/unit/test_workflow_models.py -v

# 测试步骤
python -m pytest tests/unit/test_workflow_steps.py -v
```

### 集成测试

```bash
# 端到端工作流测试
python -m pytest tests/integration/test_workflow_integration.py -v
```

### 手动测试

```bash
# 真实环境 E2E 测试
python tests/manual_test_e2e_workflow.py

# 简化版工作流测试
python tests/manual_test_simplified.py

# 工作流配置测试
python tests/manual_test_workflow_config.py
```

### 测试覆盖

- `test_workflow_engine.py`: 引擎核心逻辑
- `test_workflow_models.py`: State/Context/Result 数据模型
- `test_workflow_steps.py`: 各步骤单元测试
- `test_workflow_integration.py`: 端到端集成测试
- 手动测试脚本: 真实环境验证

---

## 常见问题 (FAQ)

### Q1: 如何添加新的工作流？

```yaml
# 1. 创建配置文件 config/workflows/my-workflow.yaml
name: my-workflow
description: "我的自定义工作流"
steps:
  - id: step1
    type: fetch_content
  - id: step2
    type: my_custom_step

# 2. 注册自定义步骤（如果需要）
from src.workflow import WorkflowEngine

engine = WorkflowEngine()
engine.register_step("my_custom_step", MyCustomStep)

# 3. 执行
result = await engine.execute_async("my-workflow", {"url": "..."})
```

### Q2: 如何跳过某个步骤？

方法 1: 在步骤类中重写 `should_skip()`:

```python
class MyStep(BaseStep):
    def should_skip(self, context: WorkflowContext) -> bool:
        return context.state.get("skip_my_step", False)
```

方法 2: 在配置中使用 `condition`:

```yaml
steps:
  - id: analyze
    type: ai_analyze
    condition: "word_count > 1000"
```

### Q3: 如何处理步骤执行失败？

引擎会捕获异常并记录到 `WorkflowResult.errors`:

```python
result = await engine.execute_async("archive-url", {"url": "..."})

if not result.success:
    print("错误:", result.errors)
    print("日志:", result.logs)
```

配置级别的错误处理 (`on_error`):
- `fail` (默认): 步骤失败则终止整个工作流
- `continue`: 步骤失败时跳过，继续后续步骤（archive-text 的 ai_analyze 使用此策略）

### Q4: 如何在步骤间传递数据？

通过 `context.state` 传递:

```python
class Step1(BaseStep):
    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        return {"data": "hello"}

class Step2(BaseStep):
    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        data = context.state.get("data")  # "hello"
        return {"result": data.upper()}
```

### Q5: MCP 写入 Tool 如何调用工作流?

MCP `archive_url` 和 `archive_text` Tool 直接调用 `WorkflowEngine.execute_async()`:

```python
# archive_url Tool (简化示意)
engine = WorkflowEngine()
result = await engine.execute_async("archive-url", {"url": url})

# archive_text Tool (简化示意)
engine = WorkflowEngine()
result = await engine.execute_async("archive-text", {"entry": entry})
```

---

## 相关文件清单

### 核心文件

| 文件 | 说明 |
|------|------|
| `__init__.py` | 工作流模块入口 |
| `engine.py` | 工作流引擎核心 |
| `models.py` | State/Context/Result 数据模型 |
| `steps.py` | 内置步骤实现 |

### 配置文件

| 文件 | 说明 |
|------|------|
| `config/workflows/archive-url.yaml` | 归档网页工作流配置 |
| `config/workflows/archive-text.yaml` | 归档文本工作流配置 (M9 新增) |
| `config/workflows/search.yaml` | 搜索工作流配置 |

### 测试文件

| 文件 | 说明 |
|------|------|
| `tests/unit/test_workflow_engine.py` | 引擎测试 |
| `tests/unit/test_workflow_models.py` | 数据模型测试 |
| `tests/unit/test_workflow_steps.py` | 步骤测试 |
| `tests/integration/test_workflow_integration.py` | 集成测试 |
| `tests/manual_test_*.py` | 手动测试脚本（5 个） |

### 文档

| 文件 | 说明 |
|------|------|
| [docs/refactor/WorkflowEngine接口规范.md](../../docs/refactor/WorkflowEngine接口规范.md) | 引擎接口规范 |
| [docs/refactor/Workflow数据模型规范.md](../../docs/refactor/Workflow数据模型规范.md) | 数据模型规范 |
| [docs/design/工作流开发指南.md](../../docs/design/工作流开发指南.md) | 工作流开发指南 |
| [docs/milestones/M5_COMPLETION_SUMMARY.md](../../docs/milestones/M5_COMPLETION_SUMMARY.md) | M5 完成报告 |

---

## 变更记录 (Changelog)

### 2026-02-19 00:58 (M9)
- 新增 `archive-text` 工作流配置 (MCP archive_text Tool 专用)
- 文档补充 MCP 与 Workflow 集成说明
- 补充 `on_error: continue` 错误处理策略说明

### 2026-02-16
- 生成模块级 CLAUDE.md 文档
- 添加导航面包屑
- 补充工作流配置和步骤详细说明

### 2026-02-15 (M5.1)
- 修复引擎传参错误（step_config 未传递）
- 修复配置字段名不匹配问题

### 2026-02-14 (M5)
- 完成工作流引擎核心实现
- 完成 4 个内置步骤 (Fetch/Analyze/Sharpen/Store)
- 完成 YAML 配置加载
- 完成所有单元测试和集成测试

---

**模块维护者**: AI Agent
**最后更新**: 2026-02-19 00:58:06

*本文档由 Claude Code 自动生成*
