# Milestone 5: 工作流引擎最小变更实现方案

## 设计原则

- **KISS 原则**: 复用现有抽象，最小化新文件
- **遵循现有模式**: 参考 processors 的工厂模式和 BaseProcessor 抽象
- **最小依赖**: 优先使用标准库，避免引入新依赖

## 1. 文件修改清单

### 1.1 新建文件（共 4 个核心文件 + 3 个测试文件）

#### 核心实现文件

1. **`src/workflow/models.py`** (约 80 行)
   - `State`: 类型安全的状态管理类
   - `WorkflowContext`: 工作流上下文（包含 state 和 logs）
   - `WorkflowResult`: 工作流执行结果（@dataclass）

2. **`src/workflow/steps.py`** (约 180 行)
   - `BaseStep`: 抽象基类（定义 execute 接口）
   - `FetchStep`: 调用 `get_processor(url).process()`
   - `AnalyzeStep`: 调用 `DeepSeekClient` 生成摘要和标签
   - `IdeaSharpenStep`: 使用 `rich.prompt` + `asyncio.wait_for` 的交互式优化
   - `StoreStep`: 调用 `MarkdownStore` + `SQLiteStore` + `VectorStore`

3. **`src/workflow/engine.py`** (约 120 行)
   - `WorkflowEngine`: 核心引擎类
   - `execute_async(workflow_name, input_data)`: 异步执行工作流
   - 步骤注册表 `_STEP_REGISTRY`（类似 `_PROCESSORS`）
   - 工作流配置加载（从 `config.yaml` 读取）

4. **`src/workflow/factory.py`** (约 60 行)
   - `get_workflow_engine()`: 单例工厂方法
   - `load_workflow_config(workflow_name)`: 从 YAML 加载配置

#### 测试文件

5. **`tests/unit/test_workflow_models.py`** (约 100 行)
   - State 类的 get/set 测试
   - WorkflowContext 的日志记录测试
   - WorkflowResult 的数据类验证

6. **`tests/unit/test_workflow_steps.py`** (约 150 行)
   - 各 Step 的单元测试（使用 Mock）
   - FetchStep: Mock processor.process()
   - AnalyzeStep: Mock DeepSeekClient
   - IdeaSharpenStep: Mock rich.Prompt.ask()
   - StoreStep: Mock 存储层调用

7. **`tests/integration/test_workflow_integration.py`** (约 120 行)
   - 完整 `archive_url` 工作流测试（需要 API Key）
   - 超时场景测试
   - 错误处理测试

### 1.2 修改现有文件（共 3 个）

8. **`src/workflow/__init__.py`** (修改)
   - 添加公共接口导出:
     ```python
     from src.workflow.engine import WorkflowEngine
     from src.workflow.models import WorkflowResult, WorkflowContext
     from src.workflow.factory import get_workflow_engine

     __all__ = ["WorkflowEngine", "WorkflowResult", "WorkflowContext", "get_workflow_engine"]
     ```

9. **`config/config.yaml`** (已有 workflows 配置段，无需修改)
   - 现有配置已满足需求（config.yaml:90-102）

10. **`src/utils/config.py`** (小修改，约 +10 行)
    - 添加方法: `get_workflow_config(workflow_name: str) -> Dict[str, Any]`
    - 用于读取 `workflows.{workflow_name}` 配置

---

## 2. 实现顺序（Build Sequence）

### Phase 1: 基础设施 (约 1 小时)

**Step 1.1**: 创建数据模型
```bash
# 文件: src/workflow/models.py
- 实现 State 类（get/set 方法）
- 实现 WorkflowContext（state + logs）
- 定义 WorkflowResult dataclass

# 测试: tests/unit/test_workflow_models.py
pytest tests/unit/test_workflow_models.py -v
```

**Step 1.2**: 修改配置工具类
```bash
# 文件: src/utils/config.py
- 添加 get_workflow_config() 方法

# 验证: 手动测试读取 workflows.archive_url
python -c "from src.utils.config import get_config; print(get_config().get_workflow_config('archive_url'))"
```

### Phase 2: 步骤实现 (约 2 小时)

**Step 2.1**: 实现 BaseStep 抽象类
```bash
# 文件: src/workflow/steps.py
- 定义 BaseStep(ABC) 接口
- 添加 execute(context) 抽象方法
```

**Step 2.2**: 实现具体步骤（顺序：简单 → 复杂）
```bash
# 1) FetchStep - 最简单，直接调用现有 get_processor()
# 2) StoreStep - 调用现有存储层
# 3) AnalyzeStep - 调用 DeepSeekClient（已有接口）
# 4) IdeaSharpenStep - 最复杂，需要处理 rich.prompt + asyncio.wait_for

# 测试: tests/unit/test_workflow_steps.py
pytest tests/unit/test_workflow_steps.py -v
```

### Phase 3: 引擎实现 (约 1.5 小时)

**Step 3.1**: 创建工作流引擎
```bash
# 文件: src/workflow/engine.py
- 实现 WorkflowEngine 类
- 实现 execute_async() 方法
- 实现步骤注册表 _STEP_REGISTRY
```

**Step 3.2**: 创建工厂方法
```bash
# 文件: src/workflow/factory.py
- 实现 get_workflow_engine() 单例
- 实现 load_workflow_config()
```

**Step 3.3**: 更新模块导出
```bash
# 文件: src/workflow/__init__.py
- 添加公共接口导出
```

### Phase 4: 集成测试 (约 1 小时)

**Step 4.1**: 端到端测试
```bash
# 文件: tests/integration/test_workflow_integration.py
- 测试完整 archive_url 工作流
- 测试超时场景
- 测试错误处理

pytest tests/integration/test_workflow_integration.py -v
```

**Step 4.2**: 手动验证
```bash
# 创建临时测试脚本
python tests/manual_test_workflow.py
```

---

## 3. 核心代码示例

### 3.1 State 类设计（models.py）

```python
from typing import Any, Dict, Optional

class State:
    """类型安全的状态管理"""

    def __init__(self, initial: Dict[str, Any]):
        self._data = initial.copy()

    def get(self, key: str, default: Any = None) -> Any:
        """获取状态值"""
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """设置状态值"""
        self._data[key] = value

    def has(self, key: str) -> bool:
        """检查键是否存在"""
        return key in self._data

    def to_dict(self) -> Dict[str, Any]:
        """导出为字典"""
        return self._data.copy()
```

### 3.2 IdeaSharpenStep 实现（steps.py）

```python
import asyncio
from typing import Dict, Any
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from src.workflow.models import WorkflowContext
from src.workflow.steps import BaseStep

class IdeaSharpenStep(BaseStep):
    """Idea Sharpen 交互式优化步骤"""

    def __init__(self, timeout: int = 300):
        self.timeout = timeout
        self.console = Console()

    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        """
        展示 AI 分析结果并收集用户反馈

        Returns:
            {"user_confirmed": bool, "user_insights": str}
        """
        # 获取 AI 分析结果
        summary = context.state.get("summary_100_words", "")
        tags = context.state.get("tags", [])

        # 展示分析结果
        self.console.print(Panel(
            f"**摘要**: {summary}\n\n"
            f"**标签**: {', '.join(tags)}",
            title="[bold cyan]AI 分析结果[/bold cyan]",
            border_style="cyan"
        ))

        # 异步超时问答
        try:
            user_input = await self._ask_with_timeout(
                "这篇内容的核心价值是什么？（回车跳过）",
                timeout=self.timeout
            )

            if user_input and user_input.strip():
                context.state.set("user_insights", user_input.strip())
                context.log(f"用户补充洞察: {user_input[:50]}...", "INFO")
                return {"user_confirmed": True, "user_insights": user_input.strip()}
            else:
                context.log("用户跳过 idea sharpen，使用 AI 默认分析", "INFO")
                return {"user_confirmed": False, "user_insights": ""}

        except asyncio.TimeoutError:
            self.console.print("[yellow]⏱ 超时，使用 AI 默认分析[/yellow]")
            context.log(f"Idea Sharpen 超时 ({self.timeout}s)", "WARNING")
            return {"user_confirmed": False, "user_insights": ""}

    async def _ask_with_timeout(self, question: str, timeout: int) -> str:
        """异步超时问答"""
        async def ask_user():
            # 在线程池中运行同步的 Prompt.ask()
            return await asyncio.to_thread(Prompt.ask, question, default="")

        return await asyncio.wait_for(ask_user(), timeout=timeout)
```

### 3.3 WorkflowEngine 实现（engine.py）

```python
from typing import Dict, Any, List
import asyncio

from src.workflow.models import WorkflowContext, WorkflowResult
from src.workflow.steps import BaseStep, FetchStep, AnalyzeStep, IdeaSharpenStep, StoreStep
from src.utils.config import get_config
from src.utils.logger import get_logger

logger = get_logger(__name__)

# 步骤注册表（类似 processors 的 _PROCESSORS）
_STEP_REGISTRY: Dict[str, type[BaseStep]] = {
    "fetch": FetchStep,
    "analyze": AnalyzeStep,
    "sharpen": IdeaSharpenStep,
    "store": StoreStep,
}

class WorkflowEngine:
    """工作流引擎"""

    def __init__(self):
        self.config = get_config()

    async def execute_async(
        self,
        workflow_name: str,
        input_data: Dict[str, Any]
    ) -> WorkflowResult:
        """
        执行工作流

        Args:
            workflow_name: 工作流名称（如 "archive_url"）
            input_data: 输入数据（如 {"url": "https://..."}）

        Returns:
            WorkflowResult: 执行结果
        """
        context = WorkflowContext(initial_state=input_data)
        context.log(f"开始执行工作流: {workflow_name}", "INFO")

        try:
            # 加载工作流配置
            workflow_config = self._load_workflow_config(workflow_name)
            step_names = workflow_config.get("steps", [])

            # 顺序执行步骤
            for step_name in step_names:
                step = self._create_step(step_name)
                context.log(f"执行步骤: {step_name}", "INFO")

                result = await step.execute(context)
                context.state.set(f"{step_name}_result", result)

            # 返回成功结果
            return WorkflowResult(
                success=True,
                data=context.state.to_dict(),
                errors=[],
                logs=context.logs
            )

        except Exception as e:
            logger.error(f"工作流执行失败: {e}", exc_info=True)
            context.log(f"工作流失败: {e}", "ERROR")

            return WorkflowResult(
                success=False,
                data=context.state.to_dict(),
                errors=[str(e)],
                logs=context.logs
            )

    def _load_workflow_config(self, workflow_name: str) -> Dict[str, Any]:
        """加载工作流配置"""
        workflows = self.config.raw.get("workflows", {})
        if workflow_name not in workflows:
            raise ValueError(f"未找到工作流配置: {workflow_name}")
        return workflows[workflow_name]

    def _create_step(self, step_name: str) -> BaseStep:
        """创建步骤实例"""
        if step_name not in _STEP_REGISTRY:
            raise ValueError(f"未注册的步骤: {step_name}")
        return _STEP_REGISTRY[step_name]()
```

---

## 4. 测试计划

### 4.1 单元测试（快速，无需 API）

```bash
# 测试数据模型
pytest tests/unit/test_workflow_models.py -v
# 覆盖: State.get/set, WorkflowContext.log, WorkflowResult dataclass

# 测试步骤（使用 Mock）
pytest tests/unit/test_workflow_steps.py -v
# 覆盖:
# - FetchStep: Mock get_processor().process()
# - AnalyzeStep: Mock DeepSeekClient.summarize()
# - IdeaSharpenStep: Mock Prompt.ask() + 超时测试
# - StoreStep: Mock 存储层调用

# 测试引擎（使用 Mock 步骤）
pytest tests/unit/test_workflow_engine.py -v
# 覆盖:
# - 工作流配置加载
# - 步骤注册表
# - 错误处理
```

### 4.2 集成测试（需要 API Key）

```bash
# 完整工作流测试
pytest tests/integration/test_workflow_integration.py -v
# 场景:
# 1. 正常归档流程（微信公众号文章）
# 2. 超时场景（IdeaSharpenStep timeout）
# 3. 错误处理（无效 URL）

# 手动测试
python tests/manual_test_workflow.py
```

### 4.3 测试命令总览

```bash
# 仅运行工作流相关测试
pytest tests/unit/test_workflow*.py tests/integration/test_workflow*.py -v

# 带覆盖率报告
pytest tests/unit/test_workflow*.py --cov=src/workflow --cov-report=term-missing

# 运行全部测试（确保不破坏现有功能）
python -m pytest tests/unit/ -v
```

---

## 5. 风险和缓解措施

### 5.1 技术风险

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|----------|
| **asyncio.wait_for() 在 Windows 上的兼容性问题** | 高 | 中 | 1) 优先在 Windows 环境测试<br>2) 准备 fallback: threading.Timer |
| **rich.Prompt.ask() 阻塞事件循环** | 中 | 高 | 使用 `asyncio.to_thread()` 包装（Python 3.9+） |
| **IdeaSharpenStep 超时逻辑复杂** | 中 | 中 | 充分单元测试 + 集成测试 |
| **工作流配置加载失败** | 低 | 低 | 配置验证 + 友好错误提示 |

### 5.2 架构风险

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|----------|
| **过度抽象导致复杂度增加** | 中 | 低 | 遵循 KISS 原则，避免过早优化 |
| **与现有 processors 模式不一致** | 低 | 低 | 参考 processors 的工厂模式设计 |
| **状态管理 State 类功能不足** | 低 | 中 | 初始版本保持简单，后续迭代扩展 |

### 5.3 测试风险

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|----------|
| **集成测试依赖 API Key** | 中 | 高 | 1) 单元测试优先<br>2) CI 环境配置 secrets |
| **超时测试不稳定** | 低 | 中 | 使用 `pytest-timeout` 插件 |
| **Mock 过度导致测试失真** | 中 | 中 | 保持合理的集成测试覆盖 |

### 5.4 用户体验风险

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|----------|
| **IdeaSharpen 超时时间过短** | 低 | 中 | 默认 300s，可在配置中调整 |
| **rich.Panel 展示信息不足** | 低 | 低 | 充分展示摘要和标签 |
| **错误信息不友好** | 中 | 低 | 使用 rich 格式化错误输出 |

---

## 6. 配置示例

### 6.1 workflows 配置（config/config.yaml）

```yaml
workflows:
  # 归档 URL 工作流
  archive_url:
    steps:
      - fetch      # 调用 get_processor(url).process()
      - analyze    # 调用 DeepSeekClient 生成摘要/标签
      - sharpen    # idea Sharpen 交互式优化
      - store      # 保存到 Markdown/SQLite/Vector

    # 步骤配置
    sharpen:
      timeout: 300          # 超时时间（秒）
      skip_if_short: true   # 短文本跳过交互
      min_length: 500       # 最小触发长度
```

### 6.2 使用示例

```python
import asyncio
from src.workflow.factory import get_workflow_engine

async def main():
    engine = get_workflow_engine()

    result = await engine.execute_async(
        workflow_name="archive_url",
        input_data={"url": "https://mp.weixin.qq.com/s/xxx"}
    )

    if result.success:
        print(f"✓ 归档成功: {result.data.get('title')}")
    else:
        print(f"✗ 归档失败: {result.errors}")

asyncio.run(main())
```

---

## 7. 总结

### 7.1 最小变更清单

- **新建文件**: 4 个核心 + 3 个测试 = 7 个文件
- **修改文件**: 2 个（`__init__.py`, `config.py`）
- **总代码量**: ~800 行（含测试）

### 7.2 复用现有架构

- ✅ 复用 `BaseProcessor` 的抽象模式（`BaseStep`）
- ✅ 复用 `get_processor()` 的工厂模式（`_STEP_REGISTRY`）
- ✅ 复用 `Entry` dataclass 模式（`WorkflowResult`）
- ✅ 复用现有存储层（`MarkdownStore`, `SQLiteStore`, `VectorStore`）
- ✅ 复用 AI 服务（`DeepSeekClient`, `Embedder`）

### 7.3 关键设计决策

1. **State 类**: 简单的字典包装，支持类型提示
2. **asyncio.wait_for**: 实现超时，使用 `asyncio.to_thread` 包装 rich.Prompt
3. **步骤注册表**: 类似 processors，支持动态扩展
4. **错误处理**: 步骤失败不中断，记录到 WorkflowResult.errors
5. **日志记录**: WorkflowContext 统一管理日志

### 7.4 后续扩展空间

- [ ] 条件步骤（if/else）
- [ ] 并行步骤（parallel）
- [ ] 步骤重试（retry）
- [ ] 工作流可视化（Mermaid 图）
- [ ] 步骤依赖声明（DAG）

---

## 附录 A: 依赖关系图

```
WorkflowEngine
├── WorkflowContext (State + logs)
├── _STEP_REGISTRY
│   ├── FetchStep → get_processor()
│   ├── AnalyzeStep → DeepSeekClient
│   ├── IdeaSharpenStep → rich.Prompt + asyncio.wait_for
│   └── StoreStep → MarkdownStore + SQLiteStore + VectorStore
└── config.yaml (workflows 配置)
```

## 附录 B: 实现时间估算

| 阶段 | 任务 | 时间估算 |
|------|------|----------|
| Phase 1 | 基础设施（models + config） | 1 小时 |
| Phase 2 | 步骤实现（4 个 Step） | 2 小时 |
| Phase 3 | 引擎实现（engine + factory） | 1.5 小时 |
| Phase 4 | 集成测试 | 1 小时 |
| **总计** | | **5.5 小时** |

*注: 时间估算基于熟悉现有代码库的前提*
