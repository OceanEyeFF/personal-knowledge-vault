# WorkflowEngine 接口规范（M5 核心）

> **版本**: 1.1
> **创建日期**: 2026-02-15
> **最后更新**: 2026-08-07（M13 W2 契约对齐）
> **文件位置**: `src/workflow/`
> **作用**: M5 工作流引擎的核心接口和步骤规范

> **M13 发布边界**：运行时只加载磁盘上的真实、版本化 YAML，目前仅支持 `archive-url.yaml` 与 `archive-text.yaml`。缺失、未知、版本错误或 schema 非法的配置必须在构造任何步骤前失败；不允许内嵌字典、`config.raw.workflows` 或默认工作流回退，`search.yaml` 不受支持。默认验证使用临时配置、替身 Provider 与合成数据，禁止真实密钥、真实 Provider 和真实 Vault 数据。
>
> **R4 内部 lifecycle 边界（2026-09-01）**：本规范中的 `FetchStep`/`StoreStep` 仍是历史
> workflow compatibility 合同。已启用且已确认 R4 自动化的 production archive 先走 Q0
> ingress，再由 Q1′ 独占 Markdown/SQLite 写入；workflow 不得绕过该链路直接创建 Q2 Provider、
> 写 `DerivationPatch`、写 usage ledger 或写历史 flat vector。既有 CLI/MCP 只投影结果状态，
> 不因此新增 workflow 名称或 Tool。

---

## 🎯 架构概览

### 核心组件

```
WorkflowEngine          # 引擎核心
    ├── BaseStep        # 步骤抽象基类
    ├── FetchStep       # 内容抓取（集成 Processors）
    ├── AnalyzeStep     # AI 分析（集成 DeepSeek）
    ├── IdeaSharpenStep # 人机交互（Rich UI）
    ├── ReviewStep      # CLI 人工审核 / 非交互入口显式跳过
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

# 2. 加载并一次性严格校验完整配置
workflow_config = self._load_workflow_config(workflow_name)
steps = validate_workflow_config(
    workflow_name,
    workflow_config,
    self._step_registry,
)

# 3. 逐步执行
for step_config in steps:
    step_type = step_config["type"]
    step_class = self._step_registry[step_type]

    step = step_class(step_id=step_config["id"], config=step_config["config"])
    try:
        result = await step.execute(context)
    except Exception:
        # on_error=fail → error 并终止；continue → warning 并继续
        ...

# 4. 分离 errors / warnings / issues，再合并普通结果到 state
    step_result = dict(result)
    step_errors = step_result.pop("errors", [])
    step_warnings = step_result.pop("warnings", [])
    step_issues = step_result.pop("issues", [])
    # 引擎按 on_error 聚合上述控制字段
    for key, value in step_result.items():
        context.state.set(key, value)

# 5. 输出统一终态
return WorkflowResult(
    success=not errors,
    terminal="error" if errors else ("degraded" if warnings else "success"),
    data=context.state.to_dict(),
    errors=errors,
    warnings=warnings,
    issues=issues,
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
    "review_entry": ReviewStep,
    "store_entry": StoreStep,
}
```

**扩展新步骤**:
```python
engine = WorkflowEngine()  # 每个实例复制默认注册表
engine.register_step("custom_step", MyCustomStep)
```

注册表是实例级状态；对一个 `WorkflowEngine` 注册自定义步骤不会污染其他实例。

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

仅发布 `archive-url` 与 `archive-text`。加载后必须通过 `validate_workflow_config()` 的 v1 fail-closed 校验；未知顶层字段、未知步骤字段、重复 ID、未知步骤类型或非法 `on_error` 都会返回 `terminal="error"`，且不会执行任何步骤。

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
| **错误** | 可返回 `errors` / `warnings` / `issues` 控制字段；普通字段才合并到 state |
| **日志** | 使用 `self._log(context, message)` |

`on_error: fail` 的步骤错误终止工作流；`on_error: continue` 的步骤错误转为 warning，最终终态为 `degraded`，不得伪装成 `success`。

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

**作用**: 集成 OpenAI-compatible LLM API，生成摘要和标签

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
schema_version: 1
name: archive-url
description: "智能归档网页内容工作流"

steps:
  - id: fetch_content
    type: fetch_content
    config:
      retry: 3
    on_error: fail

  - id: ai_analyze
    type: ai_analyze
    config:
      tasks: [summarize, extract_tags]
      max_words: 300
      num_tags: 5
    on_error: continue

  - id: idea_sharpen
    type: idea_sharpen
    config:
      questions: ["这篇内容与你现有知识中的哪些观点有关？"]
      trigger_rules:
        - content_length_gt: 3000
    on_error: continue

  - id: review_entry
    type: review_entry
    config:
      required: true
      max_regenerations: 3
      preview_chars: 500
    on_error: continue

  - id: store_entry
    type: store_entry
    config:
      targets: [markdown, sqlite, vector_index]
    on_error: fail
```

### 严格 v1 规则

- 根节点只允许 `schema_version`、`name`、`description`、`steps`。
- `schema_version` 必须为整数 `1`，`name` 必须与请求名称完全一致。
- 每个步骤必须显式提供唯一 `id`、已注册的 `type`、映射类型 `config` 与 `on_error: fail|continue`。
- 字符串步骤简写、未知字段和隐式默认补齐均不属于发布契约；保留的 `_normalize_steps()` 只是历史导入兼容助手，不参与 v1 执行。

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
    state.get("entry") → OpenAI-compatible LLM API
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
    terminal: degraded  # 存在 continue warning；无 warning 时为 success
    data: {
        url, entry, summary, tags, notes,
        file_path, knowledge_id, vector_ids
    }
    errors: []
    warnings: [...]
    issues: [...]       # 稳定 code/message/severity/recoverable/stage/step_id
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
✅ `success` / `degraded` / `error` 统一终态与机器可读 issue
✅ 同步/异步两种调用方式

### 核心步骤

| 步骤 | 类型 | 作用 |
|------|------|------|
| FetchStep | `fetch_content` | 集成 Processors 抓取内容 |
| AnalyzeStep | `ai_analyze` | 集成 DeepSeek AI 分析 |
| IdeaSharpenStep | `idea_sharpen` | 人机交互收集笔记 |
| ReviewStep | `review_entry` | 审核、有限次重生成或显式跳过 |
| StoreStep | `store_entry` | 多后端存储 |

---

**文档维护者**: AI Agent
**最后更新**: 2026-08-07
