# Workflow 模块

[根目录](../../CLAUDE.md) > [src](..) > **workflow**

---

## 模块职责

**工作流编排引擎**：提供 YAML 配置驱动的步骤编排、进度追踪、错误处理与状态管理。

### 核心理念

- **配置驱动**: 工作流只从真实、带 `schema_version` 的 YAML 文件加载；不存在内嵌 steps 回退
- **步骤编排**: 顺序执行步骤，支持条件跳过和错误处理
- **上下文传递**: 通过 `WorkflowContext` 在步骤间传递数据
- **可观测性**: 通过 `terminal/errors/warnings/issues/logs` 区分成功、降级和失败

M13 当前只支持 `archive-url.yaml` 与 `archive-text.yaml`。`search.yaml` 不受支持；搜索由 Retrieval 层及其 CLI/MCP adapter 直接执行。YAML 缺失、版本/字段非法、未知 step 或不可执行 condition 会在任何 step 副作用前 fail-closed。

R4 的真实公开 archive **不执行**这些 YAML step list：CLI/MCP 由 `KnowledgeApplication`
直接进入 `R4IngressLifecycle`，完成 Q0 admission/PreparedDocument，再由 Q1′/Q2 处理
core mutation、Provider、patch 和 generation。Q0 URL 路径可复用 `FetchStep` 的抓取实现，并可
传入 claim-fenced task-private temporary-asset spool 给愿意消费它的 processor；它不是通用写入
能力，不能触及共享 `tmp/` 或其他产品路径。该路径不执行 `archive-url.yaml` 的 `AnalyzeStep` /
`StoreStep` 管线。

因此本模块和两个 YAML 是保留的 WorkflowEngine 兼容/characterization 合同。直接执行
`AnalyzeStep` / `StoreStep` 仍可能走其遗留的 Provider/三层存储行为；它们不是 R4 产品路径，
也不能用作 R4 source acceptance 的替代证据。

---

## 入口与启动

`WorkflowEngine` 是 Core 内部编排组件，不是 `pkv_kernel` 的公开 Wrapper
接口。CLI、MCP 和外部 Wrapper 不得自行构造 Engine、Store、Provider 或 Step；它们
分别经 Application 组合边界或 `pkv_kernel` 的稳定公开接口进入归档流程。这样一次
操作始终绑定到同一份不可变 Config snapshot，并由外层统一处理 lifecycle、writer
lease 与审计。

### 快速使用

```python
from pkv_kernel import get_kernel

# 外部 Wrapper：只在 runtime lifecycle 已报告 READY 后取得公开 Kernel。
kernel = get_kernel()

# 归档在 Application 的 R4 Q0 → Q1′ → Q2 lifecycle 中执行；不会执行 archive-url.yaml。
result = await kernel.archive_url({"url": "https://mp.weixin.qq.com/xxx"})

if result.terminal in {"success", "degraded"}:
    print("成功:", result.data)
    print("日志:", result.logs)
    print("警告:", result.warnings)
else:
    print("失败:", result.errors)
print("机器可读问题:", result.issues)
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
    def __init__(self, reload_config: bool = False, step_registry=None):
        """
        初始化工作流引擎

        Args:
            reload_config: 是否每次执行都重新加载配置
            step_registry: 可选的实例级 step registry；不会污染其他 Engine
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
    WorkflowEngine 兼容路径输出: Entry。

    R4 Q0 可复用抓取实现后再构造 PreparedDocument，但不会执行 YAML 中后续的
    AnalyzeStep / StoreStep。
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
    兼容组件步骤；不是 R4 archive 的 Provider 写入路径。

    R4 中摘要/标签由 Q2 产生 immutable DerivationPatch，再由 Q1′ 原子应用。
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
    遗留 WorkflowEngine 组件；不是 R4 公开 archive 的存储实现。

    直接执行本 step 会保留 Markdown/SQLite/flat-vector 的历史行为。R4 中 Q1′
    通过 StorageCoordinator 提交 Markdown + SQLite，Q2 仅在 patch 完成后
    stage/validate/pointer-CAS generation；公开 R4 archive 不调用本 step。
    """
    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        entry = context.state.get("entry")

        # Legacy component interface. It is intentionally not a public R4
        # archive path; Application owns Q1′/Q2 for the real product flow.
        md_path = md_store.save(entry)
        sql_store.save_entry(entry)
        await vec_store.add_entry(entry)
        return {"markdown_path": str(md_path), "knowledge_id": entry.knowledge_id}
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

当前已有 2 个受支持的**遗留 WorkflowEngine**配置:

| 工作流 | 配置文件 | 用途 | 调用者 |
|--------|---------|------|--------|
| `archive-url` | `archive-url.yaml` | 兼容网页流程 | 直接 WorkflowEngine/兼容测试 |
| `archive-text` | `archive-text.yaml` | 兼容纯文本流程 | 直接 WorkflowEngine/兼容测试 |

每个文件必须包含受支持的 `schema_version`、与文件名一致的 `name` 和非空 `steps`。schema 对顶层、step、config、condition 与 trigger 字段执行严格校验；未知字段不会被静默忽略。

**YAML 兼容流程的区别**:
- `archive-text` 不含 URL fetch step；`archive-url` 包含 `fetch_content`
- 这些配置不等于真实 R4 Q0/Q1′/Q2 路径，且不能证明 public archive 的 lease、usage 或 generation 合同

真实 R4 `archive_text` 将字面文本作为 Q0 输入（路径形状文本不读取本地文件）；`archive_url`
在 admission 后经 SafeFetcher/processor。两者随后进入 Q1′ core commit/handoff 和 Q2 lifecycle。

---

## 数据模型

### WorkflowContext (上下文)

在步骤间传递数据:

```python
class WorkflowContext:
    state: State                # 当前状态容器
    logs: List[str]             # 日志记录

    def log(self, message: str):
        """记录日志"""
        self.logs.append(message)
```

---

### WorkflowResult (输出)

工作流执行结果:

```python
@dataclass
class WorkflowResult:
    success: bool               # terminal != "error"
    terminal: str               # success / degraded / error
    data: Dict[str, Any]        # 输出数据（State 的最终状态）
    errors: List[str]           # 仅致命错误
    warnings: List[str]         # on_error: continue 等已继续问题
    issues: List[dict]          # 稳定 code/message/severity/recoverable 等字段
    logs: List[str]             # 日志记录
```

`success` 只是与终态一致的兼容布尔值；adapter 必须以 `terminal` 和 `issues` 做分支。`degraded` 表示工作流继续并产生可用数据，但绝不能被展示成无警告的完整成功。

---

### State (状态字典)

封装内部字典并提供 `get/set/has/to_dict`，用于在步骤间传递数据:

```python
class State:
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
        return dict(self._data)
```

详细规范: [docs/specs/models/Workflow数据模型规范.md](../../docs/specs/models/Workflow数据模型规范.md)

---

## 遗留 WorkflowEngine 编排示例

### `archive-url.yaml`（不是公开 R4 archive）

```
输入: {"url": "https://mp.weixin.qq.com/xxx"}

步骤 1: FetchStep / Processor
  → 输出 legacy Entry

步骤 2: AnalyzeStep / IdeaSharpenStep（按 YAML）
  → 兼容行为

步骤 3: StoreStep
  → 遗留 Markdown/SQLite/vector 写入

最终输出: WorkflowResult(
    success=True,
    terminal="success",  # 若 continue 路径产生 warning，则为 degraded
    data={
        "url": "...",
        "entry": ...,
        "markdown_path": "...",
        "knowledge_id": "..."
    },
    errors=[],
    warnings=[],
    issues=[],
    logs=[...]
)
```

### `archive-text.yaml`（不是公开 R4 archive）

```
输入: {"text": "..."}

步骤 1: AnalyzeStep（按 YAML）
  → 兼容行为

步骤 2: StoreStep
  → 遗留 Markdown/SQLite/vector 写入

最终输出: WorkflowResult(
    success=True,
    terminal="success" 或 "degraded",
    warnings=[...],
    issues=[...],
)
```

---

## 测试与质量

### 单元测试

```powershell
# 运行所有工作流测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\workflow-unit -Command @("pytest", "tests/unit", "-k", "workflow", "-v")

# 测试工作流引擎
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\workflow-engine -Command @("pytest", "tests/unit/test_workflow_engine.py", "-v")

# 测试数据模型
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\workflow-models -Command @("pytest", "tests/unit/test_workflow_models.py", "-v")

# 测试步骤
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\workflow-steps -Command @("pytest", "tests/unit/test_workflow_steps.py", "-v")
```

### 集成测试

```powershell
# 端到端工作流测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\workflow-integration -Command @("pytest", "tests/integration/test_workflow_integration.py", "-v")
```

### 手动测试

手动工作流脚本不属于默认自动化；其中真实环境 E2E 会加载 Provider/网络，当前受
U1/G8 阻塞。其余交互脚本也只由用户按文件说明手动执行，不提供 Agent 裸跑命令。

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
# 1. 创建严格 v1 配置文件 config/workflows/my-workflow.yaml
schema_version: 1
name: my-workflow
description: "我的自定义工作流"
steps:
  - id: step1
    type: fetch_content
    config: {processor: auto, url_key: url, timeout: 30, retry: 0}
    on_error: fail
  - id: step2
    type: my_custom_step
    config: {}
    on_error: fail
```

```python
# 2. 注册自定义步骤（如果需要）
from src.workflow import WorkflowEngine

engine = WorkflowEngine()
engine.register_step("my_custom_step", MyCustomStep)

# 3. 执行
result = await engine.execute_async("my-workflow", {"url": "..."})
```

M13 发布物只承诺 bundled 的 `archive-url` / `archive-text`；自定义 workflow 属开发扩展，仍必须通过同一个 fail-closed schema，不能依赖内嵌或默认配置回退。

### Q2: 如何跳过某个步骤？

引擎没有通用 `should_skip()` 或顶层 step `condition` 合同。当前条件执行由具体步骤负责：`idea_sharpen` 可在其 `config` 内使用安全表达式 `condition` 或 `trigger_rules`；`review_entry` 由 adapter 通过 `skip_review` 显式跳过。

```yaml
  - id: idea_sharpen
    type: idea_sharpen
    config:
      condition: "content_length > 1000"
    on_error: continue
```

### Q3: 如何处理步骤执行失败？

引擎会按 `on_error` 把异常聚合为致命 error 或可恢复 warning，并始终保留稳定 issue:

```python
result = await engine.execute_async("archive-url", {"url": "..."})

if result.terminal == "error":
    print("错误:", result.errors)
elif result.terminal == "degraded":
    print("降级:", result.warnings)
print("机器可读问题:", result.issues)
print("日志:", result.logs)
```

配置级别的错误处理 (`on_error`):
- `fail`: 步骤失败则终止整个工作流
- `continue`: 兼容 workflow step 失败时记录稳定 warning/issue，继续后续步骤，并以 `degraded` 终态返回；R4 的 Provider retry/budget 状态由 Q2 durable lifecycle 而不是 YAML `on_error` 决定

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

### Q5: MCP 写入 Tool 如何调用 WorkflowEngine?

不会。MCP `archive_url` 和 `archive_text` Tool 调用共享的 `KnowledgeApplication`，但真实
R4 archive 直接进入 `R4IngressLifecycle`，不构造或执行 `WorkflowEngine` 的 YAML pipeline。
Application 在短 Q0 admission 和持久 transition 时取 data-root writer lease；crawler/processor
和 Provider 都在该长时 lease 外执行。竞争时将 `write_busy` 投影给 MCP：

```python
# archive_url Tool (简化示意)
application = get_application()
result = await application.archive_url({"url": url})

# archive_text Tool (简化示意)
application = get_application()
result = await application.archive_text(text, title=title)
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
| `config_schema.py` | 版本化 YAML 严格 schema 与 preflight 校验 |

### 配置文件

| 文件 | 说明 |
|------|------|
| `config/workflows/archive-url.yaml` | 遗留网页 WorkflowEngine 配置 |
| `config/workflows/archive-text.yaml` | 遗留纯文本 WorkflowEngine 配置 (M9 新增) |

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
| [docs/specs/interfaces/WorkflowEngine接口规范.md](../../docs/specs/interfaces/WorkflowEngine接口规范.md) | 引擎接口规范 |
| [docs/specs/models/Workflow数据模型规范.md](../../docs/specs/models/Workflow数据模型规范.md) | 数据模型规范 |
| [docs/modules/workflow/工作流开发指南.md](../../docs/modules/workflow/工作流开发指南.md) | 工作流开发指南 |
| [docs/history/milestones/M5_COMPLETION_SUMMARY.md](../../docs/history/milestones/M5_COMPLETION_SUMMARY.md) | M5 完成报告 |

---

## 变更记录 (Changelog)

### 2026-09-03 (R4)
- 明确公开 archive 的实际路径为 Application-owned Q0 → Q1′ → Q2；WorkflowEngine/YAML
  仍是遗留兼容合同，其直接 Analyze/Store 行为不能冒充该产品路径。
- Q2 Provider、usage/reservation、DerivationPatch 和 generation READY 保持 Application
  内部合同，不新增 daemon、public rebuild 或 MCP resume Tool。

### 2026-08-21 (K2/R3)
- 明确 WorkflowEngine 仅由 Core Application 组合；外部 Wrapper 仅使用
  `pkv_kernel`，CLI/MCP 不再直接创建 Engine。
- 补充归档流程的 Config snapshot、writer lease 与 `write_busy` 边界说明。

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
**最后更新**: 2026-09-03

*本文档由 Claude Code 自动生成*
