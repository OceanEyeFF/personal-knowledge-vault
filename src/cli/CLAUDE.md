# CLI 模块

[根目录](../../CLAUDE.md) > [src](../) > **cli**

## 模块职责

`src/cli/` 提供基于 Click 与 Rich 的命令行适配层。运行入口是 `src.main`；`LazyCLIGroup` 按需加载 `archive`、`search`、`show`、`list`、`config` 与 `stats`，避免 `--version` 导入重依赖。

当前实现的关键调用关系：

- `archive` 通过 `WorkflowEngine.execute_async("archive-url", input_data)` 执行归档工作流。
- `search --strategy auto` 通过 `QueryRouter.search()` 路由；显式策略直接构造 BM25、Vector 或 Hybrid retriever。
- `show`、`list`、`stats` 读取 SQLite/Markdown 等已配置存储。
- `config show/get` 读取配置并遮罩敏感值；`config set` 会写 `config/local.yaml`。

## 安全运行边界

自动测试和 AI 协作只使用隔离包装器：

```powershell
.\scripts\run-test.ps1 --help
.\scripts\run-test.ps1 --version
.\scripts\run-test.ps1 --verbose stats
.\scripts\run-test.ps1 search "synthetic query" --strategy bm25 --format json
.\scripts\run-test.ps1 list --limit 10
.\scripts\run-test.ps1 config show
.\scripts\run-test.ps1 config get storage.vault_dir
```

包装器把非 `-Direct` 命令接到 `tests/offline_entrypoint.py cli`，只加载 base config，并把产品运行路径限制在本次 `.data-test` 根内。默认离线入口安装 Python 进程内 socket guard，阻断 DNS 与非 loopback 连接，但允许 loopback/AF_UNIX，且不等价于 OS 网络沙箱；因此真实 URL 归档不属于这些示例。

`config set` 会修改真实本机配置，测试包装器会在创建运行时目录前拒绝。只有用户明确授权时才由用户直接编辑 Git 忽略的 `config/local.yaml`；AI 和默认测试不执行该写入。生产 Vault 查询、真实 URL、真实 Provider 与费用也不属于默认 TestCase lane。

## 当前公开命令合同

| 命令 | 主要参数/选项 | 当前接线与稳定边界 |
|---|---|---|
| `archive URL_OR_PATH` | `--skip-sharpen`、`--tags`、`--quiet`、`--type auto\|webpage\|chat\|news` | 调用 `execute_async("archive-url", ...)`；真实网络/Provider 仅后续显式 live 流程 |
| `search QUERY` | `--strategy auto\|bm25\|vector\|hybrid`、`--limit`、`--format table\|json\|markdown` | JSON 输出含 `query/strategy/total/results`；TestCase 需区分请求策略与实际路由语义 |
| `show [ID_OR_URL]` | `--url`、`--raw` | ID 或 URL 至少提供一个；当前 `--raw` 尚未验证 DB `file_path` 的 Vault containment，这是待独立源修复的 P0 缺口 |
| `list` | `--tag`、`--sort time\|title\|id`、`--desc`、`--limit` | 从 SQLite 查询并以 Rich 表格输出；排序/tie 与非法 limit 仍需目标合同 |
| `config show/get` | `get KEY` | 读取并遮罩敏感配置；默认自动化只允许只读子命令 |
| `config set KEY VALUE` | YAML 点号键 | 写 local config；不属于自动化/AI 运行边界 |
| `stats` | 无 | 汇总条目、来源、标签与存储大小 |

全局选项由 `src/main.py` 定义：`--verbose`、`--debug`、`--version`。

## 实现索引

### `commands.py`

- `cli()`：直接模块入口；常规启动由 `src.main.LazyCLIGroup` 负责。
- `archive()`：组装工作流输入、运行异步归档并渲染结果。
- `search()`：选择 retriever、执行搜索、输出 table/JSON/Markdown。
- `show()` / `list_entries()` / `stats()`：读取存储并渲染可观察结果。
- `config_show()` / `config_get()` / `config_set()`：配置读写与敏感值遮罩。
- `_render_search_table()`、`_render_list_table()`、`_render_entry_panel()`：命令内 Rich 渲染辅助函数。

### `ui.py`

公开辅助 API：

- `ProgressHandle`
- `show_progress()`
- `format_table()`
- `show_panel()`
- `confirm_action()`

### `formatters.py`

公开格式化 API：

- `format_as_json()`
- `format_as_markdown()`
- `format_search_results()`
- `format_entry_detail()`

不要引用已不存在的 `ProgressTracker`、`TableFormatter`、`PanelFormatter`、`ConfirmDialog`、`OutputFormatter` 或 `_register_commands`。

## 测试分层

### Unit

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\cli-unit -Command @("python", "-m", "pytest", "tests/unit/test_cli_commands.py", "tests/unit/test_cli_ui.py", "tests/unit/test_cli_formatters.py", "-m", "not network and not manual", "-v")
```

主责：参数分支、纯格式化、稳定结构和错误处理。避免把完整中文文案或 Rich 内部结构作为唯一 oracle。

### In-process integration

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\cli-integration -Command @("python", "-m", "pytest", "tests/integration/test_cli_inprocess.py", "-m", "not network and not manual", "-v")
```

当前主责：临时 SQLite/Vault 上的 BM25 搜索、`list`→`show` 协作，以及临时项目根内的配置读取/写入。它使用 `CliRunner`，不声称覆盖 OS 子进程边界或真实归档工作流。

### Subprocess blackbox

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\cli-blackbox -Command @("python", "-m", "pytest", "tests/blackbox/test_cli_basic.py", "tests/blackbox/test_cli_blackbox.py", "-m", "not network and not manual", "-v")
```

黑盒用例只经 `tests/offline_entrypoint.py cli` 启动真实子进程，主责退出码、stdout/stderr、JSON 边界和临时存储副作用；不使用进程内 `CliRunner` 代替协议边界。

## TestCase 设计注意事项

- `search` 的 `strategy` 字段当前存在“请求值/实际路由值”语义风险，最终 oracle 需先做产品合同决策。
- `show --raw` 当前只检查 DB `file_path` 是否存在后直接读取，未做 Vault canonical containment；它是待独立源修复的 P0 缺口，目标 TestCase 必须用外部 sentinel 证明不越界读取。
- `archive` 的真实网页、真实 Provider 和费用不进入默认回归；离线 integration 只能计为 adapter seam，不能冒充真实工作流 E2E。
- `config set`、生产数据查询和开发 Vault 重建不作为 pytest fixture 或完成定义。
- CLI help/stats/search 在 unit/integration/blackbox 有重复，后续按“行为 owner + 高层协议 sentinel”收敛。

## 相关文档

- [API 文档](../../docs/operations/API文档.md)
- [使用手册](../../docs/operations/使用手册.md)
- [Retrieval 检索引擎规范](../../docs/specs/interfaces/Retrieval检索引擎规范.md)
- [TestCase 设计审查与分阶段规划](../../docs/operations/testing/Review-TestCase设计审查与分阶段规划-2026-07-30.md)

**当前版本**：`0.8.0-alpha`

**最后核对**：2026-07-30（以当前源码公开符号为准）
