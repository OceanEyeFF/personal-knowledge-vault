# Kernel SDK 公开合同（K1a/K2）

> 范围：`pkv_kernel` 的外部 Wrapper 编程边界。
> 状态：K1a/K2 公开合同；K1b 的本地安装证据见
> [Kernel SDK 本地 wheel 验证合同](./KernelSDK本地Wheel验证合同.md)。

## 唯一导入面

外部 GUI、Web 或自动化 Wrapper 只能导入 `pkv_kernel`（包括明确承诺的
`pkv_kernel.contracts` 与 `pkv_kernel.lifecycle` 子模块）。`src.*`、CLI、MCP、Store、
Workflow、Processor、Provider、日志和文件打开实现对象均是 Core 内部细节，不能作为
集成依赖或 monkeypatch 目标。

`pkv_kernel.__all__` 是 API major 1 的完整、可执行**包根**公开清单；
`pkv_kernel.contracts.__all__` 与 `pkv_kernel.lifecycle.__all__` 是仅有的两个承诺嵌套
公开清单。除这三处以外，安装 wheel 中可被 Python 解析到的模块或属性均为实现细节，
不构成 Wrapper 依赖。三个清单的冻结断言位于
`tests/unit/test_kernel_wrapper_boundary.py`。

### 获取 Kernel 与高级 SDK 类型

`KnowledgeKernel` 是 factory-only 的公开操作端口：正常 Wrapper 只能通过已确认的
`pkv_kernel.lifecycle.RuntimeExecution.open_kernel()`（或
`open_kernel_from_execution()`）取得它；不得直接调用 `KnowledgeKernel(...)`，也不得构造
或保留 `src.application.KnowledgeApplication`。直接构造会抛出 `TypeError`。

`bootstrap_kernel()`、`configure_kernel()`、`get_kernel()`、`get_config()` 和
`reset_kernel()` 为 API-major-1 的兼容/内部测试符号。其中 `bootstrap_kernel()` 会调用历史
mutating bootstrap，`get_kernel()` 的旧 lazy 路径也不等同 R2 检查；它们可保留给 K1b
离线 smoke、既有嵌入式进程或已完成 lifecycle 的兼容代码，但不得作为新的 Wrapper 首次
启动协议。新的 Wrapper 必须先走下节的 `pkv_kernel.lifecycle`。

`Config`、结果/错误 DTO、Provider 设置与流类型，以及 `__all__` 中其他较低层的公开辅助类型
属于 API-major-1 的高级 SDK 类型。Wrapper 可以只经其 `pkv_kernel` 名称使用文档化语义；这不
授权导入、monkeypatch 或依赖它们在 `src.*` 中的来源、构造图或私有属性。`src.*` 始终是
不受支持的实现私有命名空间。

## 版本与能力握手

```python
from pkv_kernel import require_kernel_compatibility

capabilities = require_kernel_compatibility(
    minimum_sdk_version="0.8.1",
    maximum_sdk_version="0.8.1",
    required_capabilities=(
        "kernel.archive.v1",
        "kernel.configuration-snapshot-reload.v1",
    ),
)
```

- `__version__` 是 SDK build 版本，采用 `MAJOR.MINOR.PATCH`。
- `KERNEL_API_VERSION` 是公开 Python surface 的协议版本；当前为 `1.0.0`。
- `get_kernel_capabilities()` 返回不可变的 SDK 版本、API 版本、能力集合、Python 范围和平台范围。
- `require_kernel_compatibility()` 对 Wrapper 的包含式最低/最高 SDK 范围及所需能力 fail-closed；
  不匹配时抛出 `KernelCompatibilityError`，Wrapper 不得猜测或回退到 `src.*`。
- 当前 K1a 支持范围为 CPython `>=3.11,<3.13`、Windows。完整跨平台 wheel/安装与测试工程
  留待 Rust 代码版本再重新评估，不由本合同预支。

能力标识在同一 API major 内只可新增，当前承诺：

- `kernel.lifecycle.v1`
- `kernel.runtime-lifecycle.v1`
- `kernel.archive.v1`
- `kernel.retrieval.v1`
- `kernel.entries.v1`
- `kernel.chat-sessions.v1`
- `kernel.configuration-snapshot-reload.v1`

## 兼容与废弃

- API major 1 中，已在 `__all__` 出现的符号不能移除、重命名或进行不兼容的参数/返回语义变化。
- 新增公开符号或能力是向后兼容的；Wrapper 应仅按自己声明的所需能力启用功能。
- 移除前必须至少经过一个兼容 SDK 版本：保留原符号、发出可观察的 `DeprecationWarning`、
  文档给出替代与移除的下一个 API major。没有该阶段不得移除。
- 新 API major 必须更新 `KERNEL_API_VERSION`、能力集合、此文档和 executable boundary tests。

## K2 配置快照与 reload

一个 `KnowledgeKernel` 绑定一个不可替换的 Application config graph。`configuration_generation`
是该图的单调递增、可观察 generation：

1. 已启动操作捕获旧 Kernel/Application；其 Workflow、steps、Store、Provider、VectorStore
   始终只使用旧 config snapshot，即使此时完成 reload。
2. `reload_kernel()` 原子发布新的 legacy `Config` identity、`KnowledgeApplication` 和
   `KnowledgeKernel`；其返回对象与之后取得的默认 Kernel 使用新 snapshot。
3. 当前 process-default Kernel 的 `update_local_config(updates)` 在同一生命周期锁内完成
   受控本机配置写入并发布新的 default Kernel；过期的 former default 在写入前被拒绝，调用者
   必须使用返回的新 Kernel 进行后续操作。
4. 显式 `get_kernel(config_b)` 是隔离的操作图，不改变全局 Config A；其
   `update_local_config(updates)` 只写入并重载 B、返回新的 isolated B Kernel，绝不发布默认
   Kernel。默认组合、Workflow 和所有默认 steps 都显式传入 B。
5. 缺失 vector index 不会被负缓存：读取到无索引后会重探测。归档创建索引后，后续
   related/delete 路径可打开并缓存已存在的同一 snapshot 索引。

这只是单进程、embedded Kernel 语义，不承诺跨进程单写者、VaultLease 或 Node；这些仍受
后续门禁约束。

## R2 公开 runtime lifecycle

需要首次启动、健康检查或 setup 的 Wrapper 只使用下列 `pkv_kernel.lifecycle` 操作：

```python
from pkv_kernel import Config, lifecycle

config = Config()
inspection = lifecycle.inspect_runtime(config)  # 只读、零网络、零初始化
plan = lifecycle.plan_runtime(inspection)       # 只读、可安全展示 to_dict()

# 仅在操作者明确确认展示出的 plan 后；allow_network 默认为 False。
confirmation = lifecycle.confirm_runtime_plan(
    plan,
    allow_network=True,
)
execution = lifecycle.execute_runtime_plan(plan, confirmation)
kernel = execution.open_kernel()  # 或 lifecycle.open_kernel_from_execution(execution)
```

- `RuntimeInspection`、`RuntimePlan`、`RuntimeConfirmation` 与 `RuntimeExecution` 是
  opaque、进程内句柄；只能由对应 lifecycle 函数创建。它们的 `to_dict()` 是可展示/传输的
  无密钥 DTO，不能反序列化或伪造为可执行计划。
- `inspect_runtime` 和 `plan_runtime` 不创建目录、数据库、索引、锁或 Provider；
  `execute_runtime_plan` 只接受由本 façade 生成的确认，沿用 R2 的 revision 复检和 R3
  writer lease。缺确认或网络授权时投影稳定的 `PKVRuntimeError`。
- 成功 `RuntimeExecution` 私有地保存已确认的 immutable Config snapshot，并且只通过
  `open_kernel()` 组合 Kernel。`isolated=True` 可获得 K2 的显式 Config B 图，而不发布
  默认 Config A。
- 该接口不导出 `RuntimeContext`、`RuntimeLayout`、Store、Provider、lease、路径或 R4
  Embedding generation 机制；R4 public rebuild 继续是单独的 gated 工作包。

## 非目标

K1a/K2/K1b 都不上传 PyPI、不改变 release hold、默认数据根、迁移授权或 MCP stdio-only
合同。K1b 的离线本地 wheel/clean-install 证据是在预置第三方依赖的解释器上证明源码隔离；
它不是 wheelhouse、依赖闭包、锁文件或正式发布声明。
