# WorkflowEngine 接口规范（M5 核心）

> **版本**: 1.0
> **创建日期**: 2026-02-15
> **文件位置**: `src/workflow/`
> **作用**: M5 工作流引擎的核心接口和步骤规范

---

## 🎯 架构概览

### 核心组件

```
WorkflowEngine          # 引擎核心
    ├── BaseStep        # 步骤抽象基类
    ├── FetchStep       # 内容抓取（集成 Processors）
    ├── AnalyzeStep     # AI 分析（集成 DeepSeek）
    ├── IdeaSharpenStep # 人机交互（Rich UI）
    └── StoreStep       # 多后端存储（Markdown + SQLite + Vector）
```

---

## 📦 WorkflowEngine (引擎核心)

**文件**: `src/workflow/engine.py`

### 核心方法

#### 1. execute_async(workflow_name, input_data) -> WorkflowResult

**签名**:
```python
async def execute_async(
    self,
    workflow_name: str,
    input_data: Dict[str, Any]
) -> WorkflowResult:
    """异步执行工作流"""
```

**输入**:
- `workflow_name: str` - 工作流名称（对应 `config/workflows/{name}.yaml`）
- `input_data: Dict[str, Any]` - 输入数据（如 `{"url": "https://..."}`)

**输出**:
- `WorkflowResult` - 执行结果对象

**执行流程**:
```python
# 1. 创建上下文
context = WorkflowContext(input_data)

# 2. 加载配置
workflow_config = self._load_workflow_config(workflow_name)
steps = self._normalize_steps(workflow_config.get("steps"))

# 3. 逐步执行
for step_config in steps:
    step_type = step_config.get("type")
    step_class = _STEP_REGISTRY[step_type]

    # 关键修复：传递 config 字段而非整个 step_config
    step = step_class(step_id=step_id, config=step_config.get("config", {}))
    result = await step.execute(context)

    # 合并结果到 state
    for key, value in result.items():
        context.state.set(key, value)

# 4. 返回结果
return WorkflowResult(
    success=len(errors) == 0,
    data=context.state.to_dict(),
    errors=errors,
    logs=context.logs
)
```

---

#### 2. execute(workflow_name, input_data) -> WorkflowResult

**作用**: 同步包装器（内部调用 `asyncio.run`）

**注意**:
- ⚠️ 不能在已运行的事件循环中调用
- ⚠️ 如果已有事件循环，抛出 `RuntimeError`

---

### 步骤注册表

**全局注册表**:
```python
_STEP_REGISTRY: Dict[str, Type[BaseStep]] = {
    "fetch_content": FetchStep,
    "ai_analyze": AnalyzeStep,
    "idea_sharpen": IdeaSharpenStep,
    "store_entry": StoreStep,
}
```

**扩展新步骤**:
```python
engine = WorkflowEngine()
engine.register_step("custom_step", MyCustomStep)
```

---

### 配置加载和缓存

```python
def _load_workflow_config(self, workflow_name: str) -> Dict[str, Any]:
    """加载工作流配置（带缓存）"""
    if self._reload_config or workflow_name not in self._config_cache:
        self._config_cache[workflow_name] = get_workflow_config(workflow_name)
    return self._config_cache[workflow_name]
```

**配置路径**: `config/workflows/{workflow_name}.yaml`

---

## 🔧 BaseStep (步骤抽象基类)

**文件**: `src/workflow/steps.py`

### 接口定义

```python
class BaseStep(ABC):
    """工作流步骤基类"""

    def __init__(self, step_id: str, config: Dict[str, Any]) -> None:
        """初始化步骤"""
        self.step_id = step_id
        self.config = config

    @abstractmethod
    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        """执行步骤逻辑（返回字典会合并到 state）"""

    def _log(self, context: WorkflowContext, message: str) -> None:
        """记录步骤日志"""
        context.log(f"[{self.step_id}] {message}")
```

### 约定

| 约定 | 说明 |
|------|------|
| **输入** | 通过 `context.state` 读取数据 |
| **输出** | 返回字典，引擎会合并到 `context.state` |
| **错误** | 返回 `{"errors": [...]}` 列表 |
| **日志** | 使用 `self._log(context, message)` |

---

## 📌 核心步骤实现

### 1. FetchStep (内容抓取)

**类型**: `fetch_content`

**作用**: 集成 Processors 模块，抓取 URL 内容

**配置**:
```yaml
- type: fetch_content
  config:
    url_key: url        # 从 state 读取 URL 的键名（默认 "url"）
    processor: auto     # 处理器选择（默认 "auto" 自动选择）
    retry: 3            # 重试次数（默认 0）
```

**State 数据流**:
```
输入: state.get("url")
输出: {
    "entry": Entry对象,
    "content": str,
    "title": str,
    "source_type": str,
    "source_url": str
}
```

---

### 2. AnalyzeStep (AI 分析)

**类型**: `ai_analyze`

**作用**: 集成 DeepSeek API，生成摘要和标签

**配置**:
```yaml
- type: ai_analyze
  config:
    tasks: [summarize, extract_tags]  # 执行的任务列表
    max_words: 300                    # 摘要最大字数
```

**State 数据流**:
```
输入: state.get("entry") 或 state.get("content")
输出: {
    "summary": str,      # 摘要（如果 tasks 包含 summarize）
    "tags": List[str]    # 标签（如果 tasks 包含 extract_tags）
}
```

**任务类型**:
- `summarize` - 生成摘要（调用 `DeepSeekClient.summarize`）
- `extract_tags` - 提取标签（调用 `DeepSeekClient.extract_tags`）

---

### 3. IdeaSharpenStep (人机交互)

**类型**: `idea_sharpen`

**作用**: 与用户交互，收集个人思考和笔记

**配置**:
```yaml
- type: idea_sharpen
  config:
    skip_conditions:
      - word_count < 500  # 跳过条件（如文章字数过少）
    prompt: "请输入您的个人思考"
    timeout: 300         # 超时时间（秒）
```

**State 数据流**:
```
输入: state.get("entry")
输出: {
    "notes": str  # 用户输入的笔记（更新 entry.notes）
}
```

**特性**:
- ✅ 使用 Rich 库的交互式 UI
- ✅ 支持跳过条件（如短文章自动跳过）
- ✅ 超时处理

---

### 4. StoreStep (多后端存储)

**类型**: `store_entry`

**作用**: 保存 Entry 到多个存储后端

**配置**:
```yaml
- type: store_entry
  config:
    targets: [markdown, sqlite, vector]  # 存储目标列表
```

**State 数据流**:
```
输入: state.get("entry")
输出: {
    "file_path": str,          # Markdown 文件路径
    "knowledge_id": int,       # SQLite 主键
    "vector_ids": List[int]    # 向量 ID 列表
}
```

**存储目标**:
- `markdown` - Markdown 文件存储（`MarkdownStore`）
- `sqlite` - SQLite 数据库存储（`SQLiteStore`）
- `vector` - 向量索引存储（`VectorStore`）

---

## 📄 配置文件规范

### 完整格式

```yaml
# config/workflows/archive-url.yaml
steps:
  - id: fetch_content
    type: fetch_content
    config:
      retry: 3

  - id: ai_analyze
    type: ai_analyze
    config:
      tasks: [summarize, extract_tags]
      max_words: 300

  - id: idea_sharpen
    type: idea_sharpen
    config:
      skip_conditions:
        - word_count < 500

  - id: store_entry
    type: store_entry
    config:
      targets: [markdown, sqlite, vector]
```

### 简化格式（自动规范化）

```yaml
steps:
  - fetch_content      # 使用默认配置
  - ai_analyze
  - idea_sharpen
  - store_entry
```

**规范化规则**:
```python
def _normalize_steps(self, steps):
    """将简化格式转换为完整格式"""
    normalized = []
    for step in steps:
        if isinstance(step, str):
            # 简化格式 → 完整格式
            normalized.append({"type": step, "config": {}})
        else:
            normalized.append(step)
    return normalized
```

---

## 🔄 数据流示例

### archive-url 工作流

```
用户输入: {"url": "https://example.com"}
    ↓
【FetchStep】
    state.get("url") → Processor.process() → Entry
    state.set("entry", entry)
    ↓
【AnalyzeStep】
    state.get("entry") → DeepSeek API
    - summarize() → summary
    - extract_tags() → tags
    entry.summary_100_words = summary
    entry.tags = tags
    state.set("entry", entry)
    ↓
【IdeaSharpenStep】
    state.get("entry") → Rich UI 交互
    用户输入 → notes
    entry.notes = notes
    state.set("entry", entry)
    ↓
【StoreStep】
    state.get("entry") → 多后端存储
    - MarkdownStore.save() → file_path
    - SQLiteStore.insert_entry() → knowledge_id
    - VectorStore.add_entry() → vector_ids
    state.set("file_path", file_path)
    state.set("knowledge_id", knowledge_id)
    state.set("vector_ids", vector_ids)
    ↓
WorkflowResult:
    success: true
    data: {
        url, entry, summary, tags, notes,
        file_path, knowledge_id, vector_ids
    }
    errors: []
    logs: [...]
```

---

## ⚠️ 已修复的 Bug

### Bug #1: 配置字段名不匹配

**问题**: StoreStep 配置使用 `storage_backends` 但代码期望 `targets`

**修复**: 统一为 `targets` 字段名

**文件**: `config/workflows/archive-url.yaml:55`

---

### Bug #2: 引擎传参错误

**问题**: 传递整个 `step_config` 给步骤构造函数，而非只传递 `config` 字段

**修复**:
```python
# 错误
step = step_class(step_id=step_id, config=step_config)

# 正确
step = step_class(step_id=step_id, config=step_config.get("config", {}))
```

**文件**: `src/workflow/engine.py:92`

---

## 🎯 总结

### 设计优点

✅ 清晰的步骤抽象（BaseStep）
✅ 配置驱动（YAML）
✅ 步骤注册表（可扩展）
✅ State 数据流（步骤间传递）
✅ 统一的错误处理
✅ 同步/异步两种调用方式

### 核心步骤

| 步骤 | 类型 | 作用 |
|------|------|------|
| FetchStep | `fetch_content` | 集成 Processors 抓取内容 |
| AnalyzeStep | `ai_analyze` | 集成 DeepSeek AI 分析 |
| IdeaSharpenStep | `idea_sharpen` | 人机交互收集笔记 |
| StoreStep | `store_entry` | 多后端存储 |

---

**文档维护者**: AI Agent
**最后更新**: 2026-02-15
