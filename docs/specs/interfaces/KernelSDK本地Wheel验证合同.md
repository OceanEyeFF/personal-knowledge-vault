# Kernel SDK 本地 wheel 验证合同（K1b）

> 范围：`pkv-kernel` 的离线本地构建与 clean-install 兼容验证。
> 非范围：PyPI 上传、签名、installer、release Artifact、release hold 或默认数据根变更。

## 分发边界

- distribution 名称为 `pkv-kernel`；外部集成只使用 `pkv_kernel` 包（含明确承诺的
  `pkv_kernel.contracts` 与 `pkv_kernel.lifecycle` 子模块）。完整 API 以三个模块各自的 `__all__` 和
  [Kernel SDK 公开合同](./KernelSDK公开合同.md) 为准。
- wheel 只携带实现 `src` 的 Kernel 所需闭包；不携带 `src.cli`、`src.mcp`、`src.gui`、
  `PySide6`、`qasync` 或任何 GUI entrypoint。
- `src` 在 wheel 内只是 `pkv_kernel` 的私有实现依赖。外部 Wrapper 不得导入、monkeypatch
  或以该命名空间作为兼容接口。
- SDK build version 必须与 wheel metadata version 相同；版本/能力握手继续由
  `require_kernel_compatibility()` 执行。

## 运行时资源

源码 checkout 仍以仓库根作为资源根。wheel 安装后，`RuntimeLayout` 自动改用
`pkv_kernel/_resources/`，其中只允许：

- 基础配置、`archive-url` / `archive-text` 工作流和自定义词典；
- 当前受控 SQL migration 资源；
- 摘要和标签 Prompt。

构建器使用显式 allowlist，绝不复制 `config/local.yaml`、`.env`、Vault、数据库、日志或
任何用户数据。wheel 的最小启动在合成 `PKV_DATA_ROOT` 中建立 fresh 数据库；这不是历史库
migration，更不是对真实数据的迁移授权。

## 可执行验证

唯一自动化合同位于 `tests/unit/test_local_wheel_contract.py`，须通过默认测试入口运行：

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\k1b-wheel -Command @(
  "python", "-m", "pytest", "tests\unit\test_local_wheel_contract.py", "-q"
)
```

该测试使用 `pip wheel --no-index --no-deps --no-build-isolation` 在仓库外临时工作区构建本地
wheel，并在新的 source-free venv 中以 `pip install --no-index --no-deps` 安装。该 venv 有意
继承已预置的离线测试解释器第三方依赖；PKV 运行数据仍严格留在 `.data-test`。因此它证明项目
wheel、资源定位和源码隔离，不是零依赖 clean OS 验证，也不证明 wheelhouse、依赖闭包、依赖
索引、锁文件、跨平台可用性或在线安装。

隔离子进程以 `-I` 启动，强制使用合成 `HOME` / `USERPROFILE` / AppData / 临时目录，并在导入
SDK 前安装 child-process socket network guard。它必须同时证明：

1. `pkv_kernel` 与其内部 Kernel facade 都来自 venv 的 site-packages；
2. version/capability handshake 成功；
3. `pkv_kernel.lifecycle.inspect_runtime()` / `plan_runtime()` 可在合成根零写、零网络运行，
   `bootstrap_kernel()` 仅作为兼容最小启动 smoke；
4. 资源根来自 wheel 内 `_resources`，而非相邻源码 checkout；
5. 公开 payload 与导入路径不含 CLI、MCP 或 GUI 实现。
6. `Path.home()`、Profile config、runtime snapshot 和 data root 都不落入真实用户 profile，且
   probe 期间没有任何网络尝试或真实 `%USERPROFILE%\\.pkv` 访问。

通过该合同只能表述为“本地 clean-install 兼容验证通过”。它不改变 M13 held test candidate
的合规 blocker、MCP stdio-only 合同或任何正式发布状态。
