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

- 默认自动化只经 `scripts/run-test.ps1` 启动。本地 pytest 作为其 Direct 子命令运行，包装器把调用路由到 `tests/offline_entrypoint.py pytest`，并把继承的 `PYTEST_ADDOPTS` 收敛为 `--strict-markers`；bootstrap 在 pytest/plugin 导入前建立 G0，根 `tests/conftest.py` 再维持逐用例隔离。`--noconftest`、`--confcutdir`、`-c`、`--rootdir`、外部 collection target 和前置 plugin 都会被拒绝。
- CLI/MCP 离线子进程由 `tests/offline_entrypoint.py` 启动。
- Direct Python（FT7）只允许仓库 `python -m <module>` 或仓库 `.py`，拒绝 `-c`、stdin 与解释器 flags；入口用同进程 `runpy`，在产品导入前清理 live/secret/proxy、安装 base-only Config、网络 guard 与子进程 guard。
- FT7 是 Python 进程内 guard，不是 OS sandbox。非 Python Direct 仍须经过 wrapper，但不属于 Python G0、也不保证离线，必须单独审查。
- `scripts/setup-test-db.py` 只能经 FT7 运行，输出必须精确位于本次所选 `DATA_DIR`（默认 `DB_PATH`）。
- `scripts/rebuild-dev-vault.py` 同样要求 FT7 runtime attestation，且 `--root` 必须位于本次所选 `DATA_DIR`；裸启动在产品 import 前 fail-closed。
- `run-test.ps1` 对 `migrate.py` fail-closed 并返回 exit 2；真实迁移仍受 U1/G8/FT5 user-only gate 阻塞，尚未执行真实数据迁移。
- `network` 表示会访问外部网络、真实 API 或可能产生费用；默认排除。
- `manual` 表示需要人工、GUI、凭据或主观判断；默认排除。
- `slow` 只表示耗时，不授予联网权限。
- 测试模块在收集/import 阶段不得联网。
- 仓库 `tests/` 默认 lane 的 pytest 父进程在 pytest/plugin 导入前安装经过路径验证的 base-only Config 与网络 guard；父环境的 plugin 注入被清除，但 Conda 环境内已安装的 autoload plugin 属于受信运行时边界。非 Python Direct 无等价 Python G0，不得据此宣称安全。
- wrapper 在启动 Conda/Python 前清除可引发 Python/coverage 预启动注入的父环境变量，并把 `TEMP`/`TMP`/`TMPDIR` 与 `COVERAGE_FILE` 固定到 selected DataRoot。Conda 环境本身及仓库根（包括其 Python startup 文件）是受信工具链/工作树边界，不是 G0 对抗对象。
- CI 显式设置 `PKV_RUN_LIVE=0`；运行数据与 pytest temp/cache 位于 checkout 的 `.data-test/ci/*`，报告可位于 runner 临时目录。

### Phase B/Phase C MCP 最小评测闭环

- `evals/mcp_quality/tasks.v1.yaml` 固定 16 条离线推理任务。
- gold taskset 与 `proposals.baseline.v1.yaml` 物理分离，反例测试会注入错误
  Tool、参数和 chunk query 并确认评分失败。
- runner 经过 FastMCP `list_tools` / `call_tool`，使用临时 SQLite/Vault 与确定性检索 fixture。
- `tests/unit/test_mcp_quality_scorer.py` 固定任务/评分契约。
- `tests/integration/test_mcp_quality_eval.py` 固定 16 条任务、119 项版本化声明式检查、32 项自动公开 envelope 检查、报告 schema 与 `targets_met` 门禁；当前总数为 151。2026-07-31 的 119-check 结果保留为历史声明式基线。
- 当前策略为 `threshold_enforced`；CLI 使用 `--enforce-thresholds`
  将任务集阈值作为退出码门禁。
- 所有公开评测路径入口都会在读写或创建前拒绝生产 `.data`。
- 运行方式见 `docs/operations/MCP最小评测闭环.md`；临时 JSON 结果只写入 `.data-test`。

### M13 W2 source verification

- `tests/contracts/m13_w2.v1.yaml` 是详细路线的机器可检查投影：受支持、`partial-v1` 与明确 unsupported 的源代码能力为 `source_verified`；W3 外置 `chat.loopback_harness.v1` 为 `packaging_contract_verified`，仍是 deferred、无产品 surface 的 payload 外 E2E 输入。
- `mcp.http.v1` 仍为 `defined`；`m13_w4_handoffs.v1.yaml` 中全部场景必须保持 `artifact_pending` 作为声明，源码树或 packaging-contract 测试不得升级为 Artifact 证据。
- Workflow、Retrieval、MCP、GUI Chat 分别由唯一 owner、版本化 fixture、故障注入与稳定 oracle 覆盖；adapter 必须保留下游 `invalid/error/degraded`，不得改写为空结果或成功。
- Chat 的 W4 测试响应只能来自 W3 交付、release payload 外的 deterministic loopback harness，并经正常 Provider 配置接入；源码和 Artifact 不得包含隐藏 fake/test mode。
- 2026-08-07 fresh 默认离线全量为 `3475 passed, 20 skipped, 9 deselected`；skip 均为当前 Windows 主机缺少 symlink 权限或 POSIX-only 合同，deselect 为离线入口排除 live 用例。
- 同一收口工作树的 MCP coverage gate 为 `755 passed, 5 deselected`，`src.mcp` 为 1671 statements / 78 miss / `95.33%`，达到 `95%` 门槛。
- 2026-08-07 Phase C fresh run 为 16/16 tasks、151/151 checks（119 项声明式 + 32 项自动公开 envelope）、`overall=1.0`、全部维度 `1.0`、0 failed、`targets_met=true`。
- W2 四条冻结工作流的独立复审未发现确定性 P0/P1；W3 打包链与 W4 artifact-only 功能验证已完成，但候选仍为合规 hold。
- 2026-08-10 W3-T0 已冻结 `source` / `packaging-contract` / `artifact-only` 三条 lane；默认 source lane 排除 Artifact，显式 Artifact lane 缺任一输入、路径链接状态不可判定或超时时必须失败，禁止 skip/xfail 与源码回退。
- W2 handoff 与 W4 evidence 已拆分为独立 registry；10 个 scenario 覆盖详细路线的 11 行 lifecycle matrix。handoff 全部保持 `artifact_pending`，而外部 artifact-only `w4-53a45ed` 的 10 条 evidence record 已为 `artifact_verified`；状态 validator 拒绝 source/packaging-contract 伪造、缺 identity/hash/evidence 及 Chat 缺 harness。
- W3-T0 根运行证据：定向 contract `19 passed`；QSettings/Qt 全局状态 `45 passed, 1 skipped`；合成显式 Artifact lane `12 passed`；最终默认离线全量 `3492 passed, 20 skipped, 21 deselected`。
- 同一冻结实现的 MCP coverage 为 `758 passed, 2759 deselected`、`src.mcp=95.33%`（门槛 `95%`）；Phase C 为 16/16 tasks、151/151 checks、全部维度 `1.0`、0 failed、`targets_met=true`。独立 CodeReview 为 P0=0、P1=0；W4 最终外部运行结果为 10/11/10/0/0、`functional_verified=true`，但 `release_eligible=false`、`decision=hold`。

---

## 测试文件清单

### 单元测试 (unit/)

| 文件/族 | 主责合同 |
|------|----------|
| `test_processors_*.py` | 内容解析、输入分类与 Processor 适配 |
| `test_ai_*.py` | Provider 客户端、重试与响应转换 |
| `test_retrieval_*.py` | BM25/向量/混合检索与路由 |
| `test_workflow_*.py` | 工作流引擎、步骤与状态传播 |
| `test_cli_*.py` | CLI 参数分支与输出适配 |
| `test_mcp_tools.py` | MCP Tool handler |
| `test_mcp_resources.py` | MCP Resource/Template handler |
| `test_mcp_prompts.py` | MCP Prompt 模板 |
| `test_mcp_security.py` | MCP 安全验证（SSRF/Auth） |
| `test_w2_contract_registry.py` | W2 capability state、fixture 与 W4 Artifact handoff 边界 |

**覆盖率**: 不在静态文档中维护易漂移的百分比；以当前受控覆盖率命令及其报告为准。

---

### 集成测试 (integration/)

| 文件 | 测试场景 |
| ------ | ---------- |
| `test_retrieval_integration.py` | 检索引擎端到端 |
| `test_workflow_integration.py` | 工作流引擎集成 |
| `test_cli_inprocess.py` | CLI 进程内命令集成 |
| `test_mcp_functional.py` | MCP 进程内功能测试（Layer 2），经 FastMCP 调用 |
| `test_mcp_integration.py` | MCP 真实 SQLiteStore 集成 |

---

### E2E 测试 (e2e/)

| 文件 | 测试场景 |
|------|----------|
| `test_fixture_isolation.py` | 离线 fixture、路径与配置隔离合同 |
| `test_mcp_e2e_archive.py` | MCP 归档协议与副作用合同；默认用例离线 |
| `test_mcp_e2e_search.py` | MCP 搜索协议与结果结构；默认用例离线 |
| `test_mcp_e2e_knowledge_qa.py` | MCP 知识问答与引用结构；默认用例离线 |
| `test_real_api_workflow.py` | 真实 API 工作流，仅 `network` opt-in |

**注意**: E2E 默认仅运行 base-only、合成数据的离线用例。带 `network` marker 的真实 API 用例属于独立后续“真实数据测试流程”；本 TestCase 修复与默认回归不得选择它们，也不得读取 `config/local.yaml`。

---

### 黑盒测试 (blackbox/)

| 文件 | 测试方法 |
| ------ | ---------- |
| `test_cli_basic.py` | CLI 基础黑盒测试 |
| `test_cli_blackbox.py` | CLI 完整黑盒测试 |
| `test_mcp_blackbox.py` | MCP stdio 协议级黑盒测试（Layer 3） |

**MCP 黑盒测试**: 仅经 `tests/offline_entrypoint.py mcp` 启动 base-only 子进程，并由 MCP SDK 经 JSON-RPC over stdio 端到端验证；不得直接启动默认配置入口。验证:
- 服务启动与协议初始化(MCP 握手)
- 功能发现（当前 manifest：`list_tools=14`、`list_prompts=3`、2 个静态 Resource、7 个 Resource Template；后两者分别由 `list_resources` / `list_resource_templates` 发现）
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

MCP 测试采用三层递进架构。精确用例数以当前 `--collect-only` 证据为准，不在此处固化会漂移的总数：

```
Layer 1: 单元测试 (最快, Mock 隔离)
    tests/unit/test_mcp_tools.py        -- Tool handler 函数直接调用
    tests/unit/test_mcp_resources.py    -- Resource handler 函数直接调用
    tests/unit/test_mcp_prompts.py      -- Prompt 模板参数和输出
    tests/unit/test_mcp_security.py     -- URL preflight/SSRF/公开脱敏/文本长度（无 HTTP auth API）
    tests/unit/test_safe_fetch.py       -- DNS pinning、redirect、peer IP、Host/SNI/hostname 与大小上限
    tests/unit/test_mcp_server_w2.py    -- stdio-only 参数与 bootstrap/bind 前拒绝

Layer 2: 进程内集成 (中速, FastMCP)
    tests/integration/test_mcp_functional.py  -- mcp.call_tool(), mcp.read_resource()
    tests/integration/test_mcp_integration.py -- 真实 SQLiteStore + MarkdownStore
    tests/integration/test_mcp_client_simulation.py -- 进程内客户端行为场景

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

真实联网测试可能访问第三方站点、消耗 API 配额或产生费用，绝不进入默认 CI/G0。当前包装器固定离线，本节不提供可复制的 live 命令；后续只能在用户专属门禁与授权流程交付后由用户执行。截至当前未执行真实数据或 live 验证。

### 手动测试脚本

```powershell
# 手动脚本必须逐个显式运行；以下命令不会由 pytest/CI 自动触发
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\manual-text -Command @("python", "tests/manual_test_text_archive_safe.py")
```

---

## 测试数据 (fixtures/)

### 微信样本

- `fixtures/wechat_sample.html` - 微信文章 HTML 样本
- `fixtures/wechat_chat_sample.txt` - 微信聊天文本样本

### 知乎样本

- `fixtures/zhihu_sample.html` - 知乎内容 HTML 样本
- `fixtures/zhihu_login_wall.html` - 登录墙页面样本
- `fixtures/zhihu/zhihu reply sample.txt` - 知乎回复文本样本

### AI 聊天样本

- `fixtures/ai_chat/chatgpt_export.md` - ChatGPT 导出样本
- `fixtures/ai_chat/deepseek_export.md` - DeepSeek 导出样本

### 测试 URL

- `fixtures/test_urls.json` - 真实 URL 列表，仅供 `manual_test_*` / 后续显式 live 流程；默认 integration/E2E 不读取或访问这些 URL

详见: [fixtures/README.md](./fixtures/README.md)

---

## 测试环境隔离

**重要**: 所有测试必须使用隔离路径，不得读取或写入生产 `.data`。

```powershell
# pytest 必须经测试包装器运行；DataRoot 只能位于仓库 .data-test 下
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\default -Command @("python", "-m", "pytest", "-q")
```

- 本地包装器会隔离 `DATA_DIR`、`DB_PATH`、`VAULT_DIR`、`VECTOR_DIR`、`LOG_DIR` 和 `TMP_DIR`。
- CI 将运行数据路径和 pytest `--basetemp` 指向 checkout 内 `.data-test/ci/*`；JUnit/coverage 报告可单独写入 runner 临时报告目录。
- CLI/MCP 黑盒测试与 E2E fixture 使用 `tmp_path`/`tmp_path_factory`，不再硬编码或继承生产数据目录。

详见: [测试环境隔离指南](../docs/operations/testing/测试环境隔离指南.md)

### 真实数据验证（规划中，授权后执行）

- [真实数据验证 Runbook](../docs/operations/testing/真实数据验证Runbook.md) 定义未来在
  用户明确授权后的小样本真实数据验证流程（P0 预演 / P1 受控评测 / P2 定期回归）；
  CAT-0 依赖完整离线 G0，真实快照 CAT-U/CAT-C 在 U1/G8 交付前保持阻塞。
- 截至当前只完成合成数据/离线预演，尚未执行真实数据验证或真实迁移。
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

覆盖率、收集数和通过数都属于运行证据，不在本说明中维护静态数字。默认离线全量、分层结果及 MCP 覆盖率门禁必须记录同一工作树指纹和完整命令；真实 API 用例不计入默认完成定义。

### 生成覆盖率报告

```powershell
# 终端报告；coverage 数据由包装器写入本次 DataRoot/reports
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\coverage -Command @("python", "-m", "pytest", "tests/unit", "--cov=src", "--cov-report=term-missing")
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
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\my-module -Command @("python", "-m", "pytest", "tests/unit/test_my_module.py", "-v")
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

### 2026-08-11 (M13 W3 打包链与 W4 held-candidate Artifact E2E)

- W3 交付可复现 Windows test candidate 与 release payload 外 deterministic loopback harness；W2 registry 中仅 `chat.loopback_harness.v1` 升为 `packaging_contract_verified`，仍为 deferred、无产品 surface。
- 外部 artifact-only 运行 `w4-53a45ed` 完成 10 个 scenario / 11 行 matrix，结果为 10 `artifact_verified` / 0 failed / 0 pending、`functional_verified=true`；完整 record 和 final-run binder 位于 `tests/contracts/m13_w4_evidence.v1.yaml`。
- 运行仍是 `test_candidate`：`release_eligible=false`、`decision=hold`。未关闭 blocker 为 conda/native、html2text GPL、MSVC provenance、Qt 对应源码/链接替换性/模块审计及 notices；不得把本结果表述为正式发布。
- 全部验证保持离线、合成数据和隔离路径；未读取真实 Provider、API key 或 Vault。

### 2026-08-10 (M13 W3-T0 短期 Test 治理收口)

- 新增 `m13_test_lanes.v1.yaml`，冻结三条 lane 的 owner、允许输入/输出及禁止替代；`pytest.ini` 默认排除 `artifact`，packaging-contract 继续进入默认离线门禁。
- 新增 fail-closed `scripts/run-artifact-e2e.ps1` T0 preflight 与显式 Artifact tests。runner/目标进程从仓库外 cwd 执行，清理 Python/Conda/source 注入；缺输入、repo containment、junction/reparse point、hardlink、harness 与 process-tree timeout 均有负例。
- W4 handoff 声明与运行 evidence 分离，10 个 scenario 精确覆盖 11 行 lifecycle matrix；跨 lane 晋级、缺字段、非法 SHA、空证据和 Chat 缺 harness 均被 executable validator 拒绝。
- `test_gui_main_window.py` 不再修改不可恢复的 `QSettings.setPath()`；default format 及 Qt organization/application name/domain/version 在 pass、fail、skip 后逐用例恢复。
- 最终证据：contract `19 passed`；Qt/QSettings `45 passed, 1 skipped`；显式 Artifact `12 passed`；默认全量 `3492 passed, 20 skipped, 21 deselected`；MCP `758 passed, 2759 deselected`、`95.33%`；Phase C 16/151 全部通过。独立复审 P0=0、P1=0。
- W3-T0 已完成，正式 W3 可以开始；W3 Artifact、外置 harness 与 W4 installed-Artifact evidence 尚未完成，全部记录保持 `artifact_pending`。

### 2026-08-07 (M13 W2 源代码功能合同收口)

- Workflow、Retrieval、MCP、GUI Chat 四线已完成 source verification 与独立 P0/P1 复审。
- capability registry 的源代码能力升级为 `source_verified`；HTTP/harness deferred 能力仍为 `defined`，W4 handoff 仍为 `artifact_pending`。
- fresh 默认离线全量为 `3475 passed, 20 skipped, 9 deselected`；MCP coverage gate 为 `755 passed, 5 deselected`、`src.mcp=95.33%`（门槛 `95%`）。
- Phase C fresh run 达到 `16/16 tasks`、`151/151 checks`（119 声明式 + 32 自动公开 envelope）、`overall=1.0`、全部维度 `1.0`、0 failed、`targets_met=true`。
- 默认验证继续经 `scripts/run-test.ps1` 使用独立 `.data-test`，未选择 network/manual，未连接真实 Provider，未读取真实 API key 或 Vault。
- W2 已完成，W3 可以开始；W3/W4 与 Artifact E2E 尚未完成。

### 2026-07-31 (P0 主线离线收口最终证据)

- 冻结工作树的默认离线全量经 `scripts/run-test.ps1` 通过：`1684 passed, 1 skipped, 9 deselected`。
- MCP 门禁经同一受控入口通过：`364 passed, 1326 deselected`，`src.mcp` 覆盖率 `96.88%`（门槛 `95%`）。
- Phase C 固定评测通过 `16/16 tasks`、`119/119 checks`，`overall=1.0`、`citability=1.0`、0 failed、`thresholds_met=true`。
- 合成 seed 与 dev-vault 演练均位于独立 `.data-test` DataRoot；未选择 `network/manual`，未读取或执行真实数据。

### 2026-07-31 (Review TestCase S4c)
- 包装器使用可跨 Conda 传播的安全 `PYTEST_ADDOPTS`，根 fixture 在每个离线用例前后恢复 base-only Config singleton。
- 当时 dirty tree 默认离线全量为 `1560 passed, 1 skipped, 9 deselected`；MCP 覆盖率为 `96.88%`。
- 全部验证经 `scripts/run-test.ps1`，未选择 live/network。

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
**最后更新**: 2026-08-07

*本文档由 Claude Code 自动生成*
