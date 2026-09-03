# CLI 模块

[根目录](../../CLAUDE.md) > [src](../) > **cli**

## 模块职责

`src/cli/` 提供基于 Click 与 Rich 的命令行适配层。运行入口是 `src.main`；`LazyCLIGroup` 按需加载 `archive`、`archive-text`、`search`、`show`、`list`、`tags`、`related`、`config`、`inspect`、`setup`、`repair` 与 `stats`，避免 `--version` 导入重依赖。

当前实现的关键调用关系：

- `--help`、`--version` 与 Click 参数校验不会读取或检查 runtime。业务命令在成功
  解析后才检查同一 immutable Config snapshot：`archive` / `archive-text` 必须
  `READY`；`search`、`show`、`list`、`tags`、`related`、`stats` 可在 `DEGRADED`
  状态读取已提交数据，但不会隐式初始化、迁移、恢复、探测 Provider 或打开文件日志。
  `setup_required`、`repair_required`、`upgrade_required` 仍返回稳定 readiness 错误。
- `inspect` 永远只读地输出 runtime inspection 与 plan；`setup` / `repair` 默认
  同样只输出计划，执行必须同时提供 `--apply --confirm PLAN_ID`，涉及 Provider
  探测时还必须显式 `--allow-network`。CLI 只负责这些确认边界、参数/本地文件能力
  校验与渲染，不自行装配 Store、Workflow、Retriever 或 Provider。
- `archive` 调用 `application.archive_cli_input()`；`archive-text` 始终按字面
  纯文本调用 `application.archive_text()`，路径形状文本不会触发本地文件读取。
- 两个 archive 命令在内部走 R4 Q0 admission → Q1′ core commit/handoff → Q2 AI
  derivation。只有 `core_committed` 后才投影为已保存；Q2 的
  `retry_required` / `budget_paused` / `authorization_required` 是可观察的
  degraded 状态。CLI 不直接 drain、resume 或 rebuild。
- `search` 调用 `application.search()`，并原样消费五态 `SearchResponse`；BM25
  路径不提前创建 Provider。
- `show`、`list`、`tags`、`stats` 与 `related` 都通过 application 的领域操作；
  `related` 使用严格只读向量入口，索引或条目向量缺失时明确返回 `degraded`。
- `config show/get` 读取配置并遮罩敏感值；`config set` 会写唯一用户配置
  `%USERPROFILE%\.pkv\config.yaml`，而非 data root 内的 runtime snapshot。

## 安全运行边界

自动测试和 AI 协作只使用隔离包装器：

```powershell
.\scripts\run-test.ps1 --help
.\scripts\run-test.ps1 --version
.\scripts\run-test.ps1 inspect
.\scripts\run-test.ps1 setup
.\scripts\run-test.ps1 config show
.\scripts\run-test.ps1 config get storage.data_root
```

包装器把非 `-Direct` 命令接到 `tests/offline_entrypoint.py cli`，只加载 base config，并把产品运行路径限制在本次 `.data-test` 根内。默认离线入口安装 Python 进程内 socket guard，阻断 DNS 与非 loopback 连接，但允许 loopback/AF_UNIX，且不等价于 OS 网络沙箱；因此真实 URL 归档不属于这些示例。

`config set` 会修改真实本机用户配置，测试包装器会在创建运行时目录前拒绝。只有用户明确授权时才由用户直接编辑 Git 忽略的 `%USERPROFILE%\.pkv\config.yaml`；AI 和默认测试不执行该写入。生产 Vault 查询、真实 URL、真实 Provider 与费用也不属于默认 TestCase lane。

## 当前公开命令合同

| 命令 | 主要参数/选项 | 当前接线与稳定边界 |
|---|---|---|
| `archive URL_OR_PATH` | `--skip-sharpen`、`--tags`、`--quiet`、`--type auto\|webpage\|chat\|news` | 经 `KnowledgeApplication.archive_cli_input()`；真实网络/Provider 仅后续显式 live 流程 |
| `archive-text TEXT` | `--title`、`--format table\|json` | 字面纯文本 → `KnowledgeApplication.archive_text()`；Q0/Q1′ 未完成时明确 processing，core commit 后输出条目定位，Q2 状态不伪装为完整成功 |
| `search QUERY` | `--strategy auto\|bm25\|vector\|hybrid`、`--limit`、`--format table\|json\|markdown` | 输出公开实际执行策略及 `success/no_hits/invalid/error/degraded`；JSON 含 `query/status/strategy/total/issues/results` |
| `show [ID_OR_URL]` | `--url`、`--raw` | ID 或 URL 至少提供一个；`--raw` 经 `MarkdownStore.load()` 做 Vault containment 校验，不直接读取 DB 中的任意路径 |
| `list` | `--tag`、`--sort time\|title\|id`、`--desc`、`--limit` | 从 SQLite 查询并以 Rich 表格输出；排序/tie 与非法 limit 仍需目标合同 |
| `tags` | `--limit 1..200`、`--format table\|json` | 有上界的只读 SQLite 标签计数；当前不提供跨 Markdown/SQLite 的标签写入 |
| `related KNOWLEDGE_ID` | `--limit 1..20`、`--format table\|json` | 已有文档向量索引的近邻查询；通过严格只读索引入口，不构造 Provider，索引/向量缺失显式 `degraded` |
| `inspect` | 无 | 只读输出 runtime inspection 与 plan；任何 readiness 都可用 |
| `setup` / `repair` | `--apply`、`--confirm PLAN_ID`、`--allow-network` | 默认仅展示计划；只有精确确认的计划才可执行，网络探测另需明确许可 |
| `config show/get` | `get KEY` | 读取并遮罩敏感配置；默认自动化只允许只读子命令 |
| `config set KEY VALUE` | YAML 点号键 | 写唯一用户配置；不属于自动化/AI 运行边界 |
| `stats` | 无 | 汇总条目、来源、标签与存储大小 |

全局选项由 `src/main.py` 定义：`--verbose`、`--debug`、`--version`。

## 实现索引

### `commands.py`

- `cli()`：直接模块入口；常规启动由 `src.main.LazyCLIGroup` 负责。
- `archive()` / `archive_text()`：验证 CLI 输入、调用 application 异步归档并渲染结果。
- `search()`：调用 application 搜索、输出 table/JSON/Markdown。
- `show()` / `list_entries()` / `tags()` / `related()` / `stats()`：调用 application
  领域操作并渲染可观察结果。
- `inspect()` / `setup()` / `repair()`：inspect → plan → explicit-confirmation 生命周期适配。
- `config_show()` / `config_get()` / `config_set()`：唯一用户配置读写与敏感值遮罩。
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
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\cli-blackbox -Command @("python", "-m", "pytest", "tests/blackbox/test_cli_basic.py", "tests/blackbox/test_cli_blackbox.py", "tests/blackbox/test_r4_cli_fullflow.py", "-m", "not network and not manual", "-v")
```

黑盒用例只经 `tests/offline_entrypoint.py cli` 启动真实子进程，主责退出码、stdout/stderr、JSON 边界和临时存储副作用；不使用进程内 `CliRunner` 代替协议边界。

## TestCase 设计注意事项

- `search` 的 `strategy` 字段表示实际执行策略；适配器必须逐一保留五态和稳定 issue，只有 `invalid` / `error` 使用非零退出码，`degraded` 必须显式展示警告。
- `show --raw` 的 TestCase 应继续用外部 sentinel 锁定 `MarkdownStore.load()` 的 canonical containment，防止后续回归成任意路径读取。
- `archive` 的真实网页、真实 Provider 和费用不进入默认回归；离线 integration 只能计为 adapter seam，不能冒充真实工作流 E2E。
- `archive-text`、`tags` 和 `related` 必须有真实离线子进程覆盖：至少验证 `archive-text → tags` 串联、无向量索引的明确降级、固定本地向量的自排除近邻结果，以及 `related` 前后向量树零改写。
- R4 public-process 用例还必须验证真实 `archive-text` 后的 Q0/Q1′/Q2 durable ledger、
  settled reservation/provider-reported usage、READY generation，以及关闭原 CLI 后由新 CLI
  执行 vector/hybrid search 命中同一条目；不得用 `CliRunner`、Application patch 或旧 flat
  vector 目录伪造该闭环。
- `config set`、生产数据查询和开发 Vault 重建不作为 pytest fixture 或完成定义。
- CLI help/Click validation 与生命周期命令必须在未 READY 根仍可执行；stats/search 等业务命令的黑盒 fixture 必须先写入匹配的无密钥 runtime snapshot，不能以隐式 bootstrap 绕过门禁。若归档已留下 `DEGRADED` journal，黑盒应验证读取仍可用、后续写入仍被 readiness 门禁拒绝。

## 相关文档

- [API 文档](../../docs/operations/API文档.md)
- [使用手册](../../docs/operations/使用手册.md)
- [Retrieval 检索引擎规范](../../docs/specs/interfaces/Retrieval检索引擎规范.md)
- [TestCase 设计审查与分阶段规划](../../docs/operations/testing/Review-TestCase设计审查与分阶段规划-2026-07-30.md)

**当前版本**：`0.8.1`

**最后核对**：2026-09-03（含 R4 source blackbox 合同）
