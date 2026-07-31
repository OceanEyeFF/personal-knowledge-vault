# Tests 测试模块

[根目录](../CLAUDE.md) > **tests**

---

## 模块职责

**全面测试覆盖**:提供单元测试、集成测试、E2E测试、黑盒测试和手动测试,确保系统质量。

### 测试层次

- **单元测试** (`unit/`): 模块级测试，Mock 外部依赖
- **集成测试** (`integration/`): 模块间协作测试，默认离线
- **E2E 测试** (`e2e/`): 端到端测试；默认离线，真实服务用例标记为 `network`
- **黑盒测试** (`blackbox/`): CLI/MCP 黑盒测试，默认离线
- **手动测试** (`manual_test_*.py`, `manual_test_m12/`): 真实环境或人工验证，不进入默认 pytest 收集

### 默认安全契约

- `python -m pytest` 只运行离线自动化测试。
- `network` 表示会访问外部网络、真实 API 或可能产生费用；默认排除。
- `manual` 表示需要人工、GUI、凭据或主观判断；默认排除。
- `slow` 只表示耗时，不授予联网权限。
- 测试模块在收集/import 阶段不得联网。
- CI 显式设置 `PKV_RUN_LIVE=0`，并把所有运行时路径放在 runner 临时目录。

### Phase B/Phase C MCP 最小评测闭环

- `evals/mcp_quality/tasks.v1.yaml` 固定 16 条离线推理任务。
- gold taskset 与 `proposals.baseline.v1.yaml` 物理分离，反例测试会注入错误
  Tool、参数和 chunk query 并确认评分失败。
- runner 经过 FastMCP `list_tools` / `call_tool`，使用临时 SQLite/Vault 与确定性检索 fixture。
- `tests/unit/test_mcp_quality_scorer.py` 固定任务/评分契约。
- `tests/integration/test_mcp_quality_eval.py` 固定 16 条任务、119 项检查全通过、
  报告 schema 和 `targets_met=true`。
- 当前策略为 `threshold_enforced`；CLI 使用 `--enforce-thresholds`
  将任务集阈值作为退出码门禁。
- 所有公开评测路径入口都会在读写或创建前拒绝生产 `.data`。
- 运行方式见 `docs/operations/MCP最小评测闭环.md`；临时 JSON 结果只写入 `.data-test`。

---

## 测试文件清单

### 单元测试 (unit/)

| 文件 | 测试模块 | 测试数量 |
| ------ | ---------- | --------- |
| `test_processors_*.py` (7个) | 内容处理器 | 50+ |
| `test_ai_*.py` (3个) | AI 服务 | 20+ |
| `test_retrieval_*.py` (3个) | 检索引擎 | 30+ |
| `test_workflow_*.py` (3个) | 工作流引擎 | 25+ |
| `test_cli_*.py` (3个) | CLI 命令 | 17+ |
| `test_mcp_tools.py` | MCP Tool handler | ~40 |
| `test_mcp_resources.py` | MCP Resource handler | ~15 |
| `test_mcp_prompts.py` | MCP Prompt 模板 | ~15 |
| `test_mcp_security.py` | MCP 安全验证 (SSRF/Auth) | ~30 |

**覆盖率**: 约 85% (核心模块)

---

### 集成测试 (integration/)

| 文件 | 测试场景 |
| ------ | ---------- |
| `test_retrieval_integration.py` | 检索引擎端到端 |
| `test_workflow_integration.py` | 工作流引擎集成 |
| `test_cli_e2e.py` | CLI 端到端 |
| `test_mcp_functional.py` | MCP 进程内功能测试 (Layer 2) -- 经 FastMCP 调用 ~50 tests |
| `test_mcp_integration.py` | MCP 真实 SQLiteStore 集成 ~15 tests |

---

### E2E 测试 (e2e/)

| 文件 | 测试场景 |
|------|----------|
| `test_real_api_workflow.py` | 真实 API 环境工作流 |

**注意**: E2E 默认运行离线用例。真实 API 用例带 `network` marker，必须同时显式选择 `-m network`、设置 `PKV_RUN_LIVE=1`，并在 `config/local.yaml` 配置真实 API Key。

---

### 黑盒测试 (blackbox/)

| 文件 | 测试方法 |
| ------ | ---------- |
| `test_cli_basic.py` | CLI 基础黑盒测试 |
| `test_cli_blackbox.py` | CLI 完整黑盒测试 |
| `test_mcp_blackbox.py` | MCP stdio 协议级黑盒测试 (Layer 3) -- ~40 tests |

**MCP 黑盒测试**: 启动 `python -m src.mcp.server` 子进程,经 JSON-RPC over stdio 端到端验证。验证:

- 服务启动与协议初始化(MCP 握手)
- 功能发现 (list_tools=8, list_prompts=3, list_resources)
- 只读 Tool 端到端调用 (含分页/过滤)
- 写入 Tool 安全拦截 (SSRF/空文本/超长文本)
- Prompt 端到端调用
- Resource 端到端读取
- 跨功能端到端场景 (list -> get -> read -> stats)

---

### 手动测试脚本

| 文件 | 测试目的 |
| ------ | ---------- |
| `manual_test_ai_services.py` | AI 服务真实环境测试 |
| `manual_test_processors.py` | 内容处理器真实环境测试 |
| `manual_test_e2e_workflow.py` | E2E 工作流测试 |
| `manual_test_real_workflow.py` | 真实工作流测试 |
| `manual_test_simplified.py` | 简化工作流测试 |
| `manual_test_text_archive_safe.py` | 纯文本归档安全测试 |

**用途**:

- 需要人工判断结果
- 需要真实 API Keys
- AI 安全测试 (不影响生产数据)

这些脚本不由默认 pytest 或 CI 收集，只能通过隔离包装器显式执行。

---

## MCP 三层测试体系 (M8+M9)

MCP 测试采用三层递进架构,共 203 个测试用例:

```
Layer 1: 单元测试 (最快, Mock 隔离)
    tests/unit/test_mcp_tools.py        -- Tool handler 函数直接调用
    tests/unit/test_mcp_resources.py    -- Resource handler 函数直接调用
    tests/unit/test_mcp_prompts.py      -- Prompt 模板参数和输出
    tests/unit/test_mcp_security.py     -- validate_url, is_private_ip, validate_text_length, validate_http_auth

Layer 2: 进程内集成 (中速, FastMCP)
    tests/integration/test_mcp_functional.py  -- mcp.call_tool(), mcp.read_resource()
    tests/integration/test_mcp_integration.py -- 真实 SQLiteStore + MarkdownStore

Layer 3: stdio 黑盒 (最慢, 子进程)
    tests/blackbox/test_mcp_blackbox.py  -- stdio_client + ClientSession + JSON-RPC
```

**Layer 2 vs Layer 3 对比**:

- Layer 2: 进程内调用,快速调试,但跳过 JSON-RPC 序列化
- Layer 3: 跨进程通信,验证完整协议链路,但启动慢

---

## 运行测试

### 默认离线测试

```powershell
# 默认命令遵循 pytest.ini：排除 manual 与 network
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\default -Command @("python", "-m", "pytest", "-q")

# 仅验证收集契约，不执行测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\collect -Command @("python", "-m", "pytest", "--collect-only", "-q")
```

### 按测试层运行

```powershell
# 单元与基础语法
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\unit -Command @("python", "-m", "pytest", "tests/test_basic_syntax.py", "tests/unit", "-v")

# 集成测试（默认离线，不需要 API Key）
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\integration -Command @("python", "-m", "pytest", "tests/integration", "-v")

# 黑盒测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\blackbox -Command @("python", "-m", "pytest", "tests/blackbox", "-v")

# 离线 E2E
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\e2e-offline -Command @("python", "-m", "pytest", "tests/e2e", "-m", "not network and not manual", "-v")
```

### 显式真实联网测试

真实联网测试可能访问第三方站点、消耗 API 配额或产生费用，绝不进入默认 CI。运行前必须确认 `DataRoot` 位于 `.data-test` 下：

```powershell
$env:PKV_RUN_LIVE = "1"
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\live -Command @("python", "-m", "pytest", "tests/e2e", "tests/integration/test_cli_e2e.py", "-m", "network", "-v")
Remove-Item Env:PKV_RUN_LIVE
```

### 手动测试脚本

```powershell
# 手动脚本必须逐个显式运行；以下命令不会由 pytest/CI 自动触发
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\manual-text -Command @("python", "tests/manual_test_text_archive_safe.py")
```

---

## 测试数据 (fixtures/)

### 微信样本

- `fixtures/wechat_articles/*.html` - 微信文章 HTML 样本

### 知乎样本

- `fixtures/zhihu_content/*.html` - 知乎问答/专栏样本

### AI 聊天样本

- `fixtures/ai_chat/chatgpt_export.md` - ChatGPT 导出样本
- `fixtures/ai_chat/deepseek_export.md` - DeepSeek 导出样本

### 测试 URL

- `fixtures/test_urls.json` - 真实 URL 列表(用于集成测试)

详见: [fixtures/README.md](./fixtures/README.md)

---

## 测试环境隔离

**重要**: 所有测试必须使用隔离路径，不得读取或写入生产 `.data`。

```powershell
# pytest 必须经测试包装器运行；DataRoot 只能位于仓库 .data-test 下
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\default -Command @("python", "-m", "pytest", "-q")
```

- 本地包装器会隔离 `DATA_DIR`、`DB_PATH`、`VAULT_DIR`、`VECTOR_DIR`、`LOG_DIR` 和 `TMP_DIR`。
- CI 将这些路径以及 pytest `--basetemp` 指向 `$RUNNER_TEMP`。
- CLI/MCP 黑盒测试与 E2E fixture 使用 `tmp_path`/`tmp_path_factory`，不再硬编码或继承生产数据目录。

详见: [测试环境隔离指南](../docs/operations/testing/测试环境隔离指南.md)

### 真实数据验证（规划中，授权后执行）

- [真实数据验证 Runbook](../docs/operations/testing/真实数据验证Runbook.md) 定义未来在
  用户明确授权后的小样本真实数据验证流程（P0 预演 / P1 受控评测 / P2 定期回归）。
- 真实数据进入测试环境的唯一通道是用户手动执行的"授权快照"（脱敏/假名化后放入
  `.data-test/<scenario>/`）；AI/自动化永不直接访问 `.data/` 或 `config/local.yaml`。
- **双通道执行模型**：Agent-safe 通道只做不接触快照、不加载 local.yaml 的静态/合成验证；
  所有读取真实快照或可能加载 local.yaml 的实际 CLI/MCP/migrate 命令（离线与 live）均由
  用户手动执行，Agent 只接收脱敏摘要。
- archive 与 `search --strategy vector/hybrid/auto` 本身会触发真实抓取/LLM/Embedding
  HTTP，属于需单独授权的 live/数据出境阶段，不是默认离线步骤；`PKV_RUN_LIVE` 仅是
  测试收集开关，不是网络开关。
- 空白记录模板见
  [真实数据验证记录模板](../docs/operations/testing/templates/真实数据验证记录模板.md)。

---

## 关键配置

### pytest.ini (项目根目录)

```ini
[pytest]
testpaths = tests
python_files = test_*.py
norecursedirs = manual_test* *.egg .* _darcs build CVS dist node_modules venv {arch}
addopts = --strict-markers -ra -m "not manual and not network"
markers =
    network: performs or enables outbound network or paid API calls; opt-in only
    manual: requires an operator, GUI interaction, credentials, or subjective validation
    slow: runtime classification only; does not grant network access
```

### E2E conftest.py

`e2e/conftest.py` 中的公共 fixture 永远离线，并且只通过
`TestEnv.env` 配置 MCP 子进程，不修改 pytest 父进程环境。真实 embedding
仅存在于显式选择的 live fixture；其进程内配置覆盖会在构建结束后立即恢复。

---

## 测试覆盖率报告

### 当前覆盖率

- **整体覆盖率**: 约 85% (核心模块)
- **单元测试**: 245+ 测试用例 (含 MCP 100+)
- **集成测试**: 完整覆盖检索/工作流/CLI/MCP
- **E2E 测试**: 真实 API 环境验证
- **MCP 三层测试**: 203 测试用例

### 生成覆盖率报告

```bash
# 生成 HTML 报告
.\scripts\run-test.ps1 -Direct -Command @("pytest", "tests/unit/", "--cov=src", "--cov-report=html")

# 查看报告
open htmlcov/index.html
```

---

## 常见问题 (FAQ)

### Q1: 如何添加新测试?

1. 在对应目录创建测试文件:

```python
# tests/unit/test_my_module.py
import pytest
from src.my_module import MyClass

def test_my_function():
    result = MyClass().my_function()
    assert result == expected
```

1. 运行测试:

```powershell
.\scripts\run-test.ps1 -Direct -Command @("pytest", "tests/unit/test_my_module.py", "-v")
```

---

### Q2: 如何 Mock 外部依赖?

```python
from unittest.mock import patch, Mock

@patch('src.ai.deepseek_client.DeepSeekClient')
def test_with_mock(mock_client):
    mock_client.return_value.summarize.return_value = "测试摘要"
    # 测试逻辑
```

---

### Q3: 如何声明需要 API Key 或真实网络的测试?

必须同时使用 `network` marker 和运行时门禁；只有 `skipif` 不足以保护默认 CI：

```python
import os
import pytest

@pytest.mark.network
@pytest.mark.skipif(
    os.getenv("PKV_RUN_LIVE") != "1",
    reason="需要 PKV_RUN_LIVE=1 才运行真实 API 测试",
)
def test_with_api():
    # 真实联网逻辑
```

---

### Q4: MCP 黑盒测试启动很慢怎么办?

MCP 黑盒测试需要启动子进程并完成 MCP 协议握手,每个测试约 1-2 秒。建议:

- 开发时优先运行 Layer 1/2 测试
- CI/CD 或提交前运行完整三层测试
- 使用 `-k` 过滤特定测试类:

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\blackbox-debug -Command @("python", "-m", "pytest", "tests/blackbox/test_mcp_blackbox.py", "-k", "TestReadonlyTools", "-v")
```

---

## 相关文件

| 文件 | 说明 |
| ------ | ------ |
| [fixtures/README.md](./fixtures/README.md) | 测试数据说明 |
| [pytest.ini](../pytest.ini) | 默认收集、marker 与排除契约 |
| [e2e/conftest.py](./e2e/conftest.py) | 离线及显式 live E2E fixtures |
| [测试环境隔离指南](../docs/operations/testing/测试环境隔离指南.md) | 测试环境文档 |

---

## 变更记录 (Changelog)

### 2026-07-28 (P0 CI 与测试契约)

- 默认 pytest 排除人工与真实联网测试，并启用严格 marker。
- E2E 公共 fixture 改为纯离线，真实向量构建独立 opt-in。
- CI 改为 master 项目级离线测试与显式 MCP 覆盖率门槛。
- 测试数据统一写入 pytest/runner 临时目录。

### 2026-07-29 (Phase C 最小评测闭环)

- 新增 16 条固定离线 MCP 推理任务及可复现 runner/scorer。
- 新增 Tool/参数/结果、chunk 相关性/可引用性和 partial/degraded 契约评分。
- 固定当前 115/119 基线与 4 项 Phase B 可引用性失败矩阵。
- gold/proposals 分离；增加错误 Tool/参数/chunk query 与生产路径前置拒绝反例。
- 明确采用 baseline-only CI，Phase B 完成后再启用 citation 阻断目标。

### 2026-07-29 (Phase B citation 合同收口)

- chunk citation、bridge evidence path、timeline locator 与 contrast provenance
  已进入正式生产返回结构。
- 固定离线评测达到 119/119，citability 100%，阈值门禁已激活。
- 三个探索 Tool 继续保持 partial 标记、限制说明和证据来源。

### 2026-02-19 00:58 (M8+M9)

- 新增 MCP 单元测试: `test_mcp_tools.py`, `test_mcp_resources.py`, `test_mcp_prompts.py`, `test_mcp_security.py`
- 新增 MCP 集成测试: `test_mcp_functional.py`, `test_mcp_integration.py`
- 新增 MCP 黑盒测试: `test_mcp_blackbox.py`
- MCP 测试总计 203 个用例,三层递进架构
- 更新测试覆盖率统计

### 2026-02-16 18:51

- 生成 Tests 模块 CLAUDE.md 文档
- 补充测试覆盖率和测试环境隔离说明

### 2026-02-16 (v0.6.1)

- 新增 `manual_test_text_archive_safe.py` (纯文本归档安全测试)
- 新增 CLI 黑盒测试 (`tests/blackbox/`)
- 新增 E2E 真实 API 测试 (`tests/e2e/`)

### 2026-02-14 (M1-M5)

- 完成核心模块单元测试 (142+ 测试用例)
- 完成检索引擎集成测试
- 完成工作流引擎集成测试

---

**模块维护者**: AI Agent
**最后更新**: 2026-07-28

*本文档由 Claude Code 自动生成*
