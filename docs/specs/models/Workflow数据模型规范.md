# Workflow 数据模型规范

> **版本**: 1.1
> **创建日期**: 2026-02-15
> **最后更新**: 2026-08-07（M13 W2 结果合同对齐）
> **文件位置**: `src/workflow/models.py`
> **作用**: 定义工作流执行过程中的状态管理、上下文和结果对象

> **当前合同**：adapter 必须按 `WorkflowResult.terminal` 区分 `success`、`degraded`、`error`。`success=True` 同时覆盖完整成功和可恢复降级，不能单独用于判断“无警告成功”；机器处理使用 `issues`，人类展示使用 `errors` / `warnings`。

---

## 📋 数据类定义

### 1. State (状态容器)

**作用**: 提供简单的 get/set 接口管理工作流状态

**类型**: 普通类（非 dataclass）

#### 构造函数

```python
def __init__(self, initial: Optional[Dict[str, Any]] = None) -> None:
    """
    初始化 State。

    Args:
        initial: 初始状态字典
    """
    self._data: Dict[str, Any] = dict(initial or {})
```

#### 字段定义

| 字段名 | 类型 | 可见性 | 说明 |
|--------|------|--------|------|
| `_data` | `Dict[str, Any]` | 私有 | 内部存储字典 |

#### 方法定义

**get(key, default=None) -> Any**

```python
def get(self, key: str, default: Any = None) -> Any:
    """获取状态值"""
    return self._data.get(key, default)
```

**set(key, value) -> None**

```python
def set(self, key: str, value: Any) -> None:
    """设置状态值"""
    self._data[key] = value
```

**has(key) -> bool**

```python
def has(self, key: str) -> bool:
    """判断是否包含指定键"""
    return key in self._data
```

**to_dict() -> Dict[str, Any]**

```python
def to_dict(self) -> Dict[str, Any]:
    """转换为字典副本"""
    return dict(self._data)
```

#### 使用示例

```python
# 创建空状态
state = State()
state.set("url", "https://example.com")
state.set("entry", Entry(...))

# 从初始字典创建
state = State(initial={"url": "https://example.com"})

# 读取状态
url = state.get("url")
entry = state.get("entry", default=None)

# 检查键存在性
if state.has("knowledge_id"):
    knowledge_id = state.get("knowledge_id")

# 导出为字典
state_dict = state.to_dict()
```

---

### 2. WorkflowContext (工作流上下文)

**作用**: 包含状态和日志，贯穿整个工作流执行过程

**类型**: 普通类（非 dataclass）

#### 构造函数

```python
def __init__(self, initial_state: Optional[Dict[str, Any]] = None) -> None:
    """
    初始化上下文。

    Args:
        initial_state: 初始状态
    """
    self.state = State(initial_state)
    self.logs: List[str] = []
```

#### 字段定义

| 字段名 | 类型 | 可见性 | 说明 |
|--------|------|--------|------|
| `state` | `State` | 公有 | 状态容器 |
| `logs` | `List[str]` | 公有 | 日志记录列表 |

#### 方法定义

**log(message) -> None**

```python
def log(self, message: str) -> None:
    """
    记录日志。

    Args:
        message: 日志内容
    """
    if not message:
        return
    self.logs.append(message)
    logger.info(message)
```

**特性**:
- 空消息会被忽略
- 日志会同时记录到 `self.logs` 和全局 logger
- 使用 `logger.info` 级别

#### 生命周期

**创建时机**:
```python
# 工作流引擎创建上下文
context = WorkflowContext(initial_state={"url": input_url})
```

**传递路径**:
```
WorkflowEngine.execute()
    -> 创建 WorkflowContext
    -> 传递给每个 Step.execute(context)
    -> 步骤通过 context.state.get/set 共享数据
```

**销毁时机**:
```python
# 工作流执行完成后返回结果
result = WorkflowResult(
    success=True,
    terminal="success",
    data=context.state.to_dict(),
    logs=context.logs
)
```

#### 使用示例

```python
# 创建上下文
context = WorkflowContext(initial_state={"url": "https://example.com"})

# 记录日志
context.log("开始处理 URL")
context.log("解析完成")

# 步骤间数据传递
context.state.set("entry", entry)
next_step_entry = context.state.get("entry")

# 导出最终状态
final_state = context.state.to_dict()
all_logs = context.logs
```

---

### 3. WorkflowResult (工作流执行结果)

**作用**: 封装工作流执行的最终结果

**类型**: dataclass

#### 字段定义

```python
@dataclass
class WorkflowResult:
    """工作流执行结果。"""

    success: bool
    data: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    issues: List[Dict[str, Any]] = field(default_factory=list)
    terminal: Optional[str] = None
```

#### 字段详解

| 字段名 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `success` | `bool` | ✅ | 无 | `terminal != "error"`；兼容布尔字段，不区分完整成功与降级 |
| `data` | `Dict[str, Any]` | ❌ | `{}` | 执行结果数据（成功时包含所有状态） |
| `errors` | `List[str]` | ❌ | `[]` | 致命错误的人类可读消息 |
| `logs` | `List[str]` | ❌ | `[]` | 执行日志列表 |
| `warnings` | `List[str]` | ❌ | `[]` | `on_error: continue` 等可恢复问题的人类可读消息 |
| `issues` | `List[Dict[str, Any]]` | ❌ | `[]` | 稳定、机器可读的问题合同 |
| `terminal` | `Optional[str]` | ❌ | 自动推导 | 只能是 `success` / `degraded` / `error` |

#### 字段语义

**success**:
- `True` - 终态为 `success` 或 `degraded`
- `False` - 终态为 `error`
- `success` 必须始终满足 `success == (terminal != "error")`

**terminal**:
- `success` - 无致命错误且没有 warning
- `degraded` - 没有致命错误，但存在继续执行的 warning / issue
- `error` - 配置预检或 `on_error: fail` 路径失败

**data**:
- 成功时：包含 `context.state.to_dict()` 的所有数据
- 常见键：`url`, `entry`, `knowledge_id`, `file_path`, `vector_ids`
- 失败时：可能为空字典或包含部分数据

**errors**:
- 只存储导致 `terminal="error"` 的人类可读消息
- 空列表只能说明没有致命错误；仍需检查 `terminal` / `warnings` 判断是否降级

**warnings**:
- 存储继续执行但需要用户注意的消息
- 非空时终态必须为 `degraded`

**issues**:
- 每项至少包含稳定 `code`、公开 `message`、`severity` 与 `recoverable`
- 可附带 `stage`、`step_id`、`cause_type`；不得依赖底层异常原文作为公开合同

**logs**:
- 包含所有步骤的日志消息
- 与 `context.logs` 内容一致
- 用于调试和审计

#### 创建方式

**成功结果**:
```python
result = WorkflowResult(
    success=True,
    terminal="success",
    data=context.state.to_dict(),
    logs=context.logs
)
```

**降级结果**:
```python
result = WorkflowResult(
    success=True,
    terminal="degraded",
    data=context.state.to_dict(),
    warnings=["步骤 ai_analyze 执行失败"],
    issues=[{
        "code": "workflow_step_failed",
        "message": "步骤 ai_analyze 执行失败",
        "severity": "warning",
        "recoverable": True,
        "step_id": "ai_analyze",
    }],
    logs=context.logs,
)
```

**失败结果**:
```python
result = WorkflowResult(
    success=False,
    terminal="error",
    data=context.state.to_dict(),  # 可能包含部分数据
    errors=["步骤 fetch_content 执行失败"],
    issues=[{
        "code": "workflow_step_failed",
        "message": "步骤 fetch_content 执行失败",
        "severity": "error",
        "recoverable": False,
        "step_id": "fetch_content",
    }],
    logs=context.logs
)
```

#### 使用示例

```python
# 执行工作流
result = workflow_engine.execute("archive-url", input_data={"url": "https://example.com"})

# 检查三态结果；不要只检查 result.success
if result.terminal == "success":
    print(f"✅ 工作流成功！")
    print(f"知识条目 ID: {result.data.get('knowledge_id')}")
    print(f"文件路径: {result.data.get('file_path')}")
elif result.terminal == "degraded":
    print("⚠️ 工作流降级完成")
    for warning in result.warnings:
        print(f"  - {warning}")
else:  # error
    print(f"❌ 工作流失败！")
    for error in result.errors:
        print(f"  - {error}")

# 查看日志
for log in result.logs:
    print(log)
```

---

## 🔗 数据流和关系

### State 与 WorkflowContext 的关系

```
WorkflowContext
    ├── state: State            (数据存储)
    └── logs: List[str]          (日志记录)
```

**职责分离**:
- `State` - 只负责数据存储（纯粹的 key-value 容器）
- `WorkflowContext` - 负责状态 + 日志管理

**优点**:
- State 可以独立使用
- 清晰的关注点分离

---

### WorkflowContext 与 WorkflowResult 的关系

```
WorkflowContext (执行期)  →  WorkflowResult (返回值)
    ├── state.to_dict()   →  result.data
    ├── logs              →  result.logs
    └── step outcomes     →  errors / warnings / issues / terminal
```

**转换时机**:
```python
# 工作流引擎最后一步
def execute(self, workflow_name, input_data):
    context = WorkflowContext(initial_state=input_data)
    errors, warnings, issues = [], [], []

    # 执行所有步骤...

    # 转换为结果对象
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

## 📊 State 数据键名约定

### 标准键名

虽然 State 允许任意键名，但工作流步骤间有约定俗成的键名：

| 键名 | 类型 | 来源步骤 | 使用步骤 | 说明 |
|------|------|---------|---------|------|
| `url` | `str` | 输入 | FetchStep | 待处理的 URL |
| `entry` | `Entry` | FetchStep | AnalyzeStep, IdeaSharpenStep, StoreStep | 知识条目对象 |
| `knowledge_id` | `int` | StoreStep (SQLite) | StoreStep (向量存储) | 知识条目数据库 ID |
| `file_path` | `str` | StoreStep (Markdown) | StoreStep (SQLite) | Markdown 文件路径 |
| `vector_ids` | `List[int]` | StoreStep (向量存储) | - | 向量 ID 列表 |

### 键名规范建议

**命名约定**:
- 使用 snake_case 命名
- 语义清晰（`entry` 而非 `e`）
- 避免缩写（`knowledge_id` 而非 `kid`）

**类型约定**:
- 明确数据类型（文档注释或类型注解）
- 避免使用 `Any` 作为值类型

---

## ⚠️ 已知问题和改进建议

### 问题 1: State 键名无类型检查

**问题描述**:
- State 允许任意键名和类型
- 步骤间键名约定没有强制检查
- 拼写错误会导致运行时错误

**示例**:
```python
# FetchStep 设置
context.state.set("entry", entry)

# 下一个步骤拼写错误
entry = context.state.get("enty")  # 返回 None！
```

**影响范围**: 中等 - 容易引入拼写错误

**优先级**: 中

**建议修复**:
- 方案 A: 定义常量键名（推荐）
  ```python
  class StateKeys:
      URL = "url"
      ENTRY = "entry"
      KNOWLEDGE_ID = "knowledge_id"
      FILE_PATH = "file_path"

  # 使用
  context.state.set(StateKeys.ENTRY, entry)
  ```
- 方案 B: 使用 TypedDict 定义 State 结构
- 方案 C: 添加运行时键名验证

---

### 问题 2: 人类消息与机器合同必须同步维护

**当前状态**:
- `errors` / `warnings` 仍是面向人的字符串
- `issues` 已提供机器可读的稳定结构
- 新步骤必须让 message、severity 与 `on_error` 聚合语义保持一致

**机器合同示例**:
```python
issues = [{
    "code": "workflow_step_failed",
    "message": "步骤 fetch_content 执行失败",
    "severity": "error",
    "recoverable": False,
    "stage": "workflow_step",
    "step_id": "fetch_content",
}]
```

**影响范围**: 低 - 主要用于人类阅读

**优先级**: 低

**维护要求**:
- adapter 分支使用 `terminal`，机器逻辑使用 `issues`
- 底层异常全文只进入私有日志；公开 issue 使用稳定消息
- 不得把 `degraded` 显示成无警告的完整成功

---

### 问题 3: logs 缺少时间戳

**问题描述**:
- logs 只存储消息字符串
- 缺少时间戳信息
- 难以分析执行时序

**当前格式**:
```python
logs = [
    "开始执行 FetchStep",
    "URL 解析完成",
    "开始执行 AnalyzeStep"
]
```

**影响范围**: 低 - 主要用于调试

**优先级**: 低

**建议修复**:
- 在 `context.log()` 中自动添加时间戳：
  ```python
  def log(self, message: str) -> None:
      timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
      log_entry = f"[{timestamp}] {message}"
      self.logs.append(log_entry)
      logger.info(message)
  ```

---

### 问题 4: WorkflowResult 缺少执行时长

**问题描述**:
- 无法从结果对象获取工作流执行时长
- 难以进行性能分析

**影响范围**: 低 - 性能监控需求

**优先级**: 低

**建议修复**: 添加字段
```python
@dataclass
class WorkflowResult:
    success: bool
    data: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0  # 新增字段
```

---

### 问题 5: State 不支持嵌套路径访问

**问题描述**:
- 无法使用点号路径访问嵌套数据
- 需要多次调用 get()

**示例**:
```python
# 期望
summary = state.get("entry.summary_100_words")

# 实际
entry = state.get("entry")
summary = entry.summary_100_words if entry else None
```

**影响范围**: 低 - 代码稍显繁琐

**优先级**: 低

**建议修复**: 添加点号路径支持（可选）
```python
def get(self, key: str, default: Any = None) -> Any:
    if "." in key:
        parts = key.split(".")
        value = self._data
        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            elif hasattr(value, part):
                value = getattr(value, part)
            else:
                return default
        return value
    return self._data.get(key, default)
```

---

## 🎯 总结

### 设计优点

✅ 简洁的数据模型（仅 3 个类，108 行代码）
✅ 清晰的职责分离（State、Context、Result）
✅ 灵活的 State 容器（支持任意类型数据）
✅ 日志自动记录（双重记录：logs + logger）
✅ 三态终态与机器可读 issues

### 需要改进

⚠️ 缺少 State 键名约定和类型检查
⚠️ 新步骤需持续保持人类消息与 issues 一致
⚠️ logs 缺少时间戳
⚠️ 缺少性能监控字段（执行时长）
⚠️ State 不支持嵌套路径访问

---

## 📝 使用模式示例

### 完整工作流执行流程

```python
# 1. 工作流引擎创建上下文
context = WorkflowContext(initial_state={"url": "https://example.com"})

# 2. FetchStep 执行
context.log("开始执行 FetchStep")
entry = processor.process(context.state.get("url"))
context.state.set("entry", entry)
context.log(f"解析完成: {entry.title}")

# 3. AnalyzeStep 执行
context.log("开始执行 AnalyzeStep")
entry = context.state.get("entry")
summary = ai_client.summarize(entry.content)
entry.summary_100_words = summary
context.state.set("entry", entry)
context.log("AI 分析完成")

# 4. StoreStep 执行
context.log("开始执行 StoreStep")
entry = context.state.get("entry")
file_path = markdown_store.save(entry)
knowledge_id = sqlite_store.insert_entry(entry, file_path)
context.state.set("file_path", file_path)
context.state.set("knowledge_id", knowledge_id)
context.log(f"存储完成: ID={knowledge_id}")

# 5. 构建结果对象
result = WorkflowResult(
    success=True,
    terminal="success",
    data=context.state.to_dict(),
    logs=context.logs
)

# 6. 返回结果
return result
```

---

**文档维护者**: AI Agent
**最后更新**: 2026-08-07
