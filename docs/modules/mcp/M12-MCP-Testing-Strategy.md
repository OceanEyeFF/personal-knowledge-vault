# M12 MCP 分层测试方案（默认离线）

> 三层递进式测试框架：可选 Inspector 探索 → 进程内行为集成 → stdio/E2E 长期回归；真实数据与真实 Provider 属于独立后续流程

**核心理念**：默认自动化只使用工作树内 `.data-test/`、显式 base-only Config、合成数据和 fake provider；不得读取 `config/local.yaml`。真实 Provider/真实 URL 仅属于独立的后续 live 流程，不是默认 TestCase 的完成条件。

当前状态补注（2026-07-31）：

- 当前代码基线共 `14` 个 Tool，其中 `query_subgraph`、`explain_relation`、`collect_evidence` 已可调用
- `find_bridges`、`timeline_of`、`contrast` 也已接入 MCP，但仍属于 partial implementation
- 当前自动化验证已经覆盖 unit / integration / blackbox；完整 E2E 仍需继续补齐

---

## 一、测试层次设计

### 层 1：MCP Inspector 快速验证（5-10 分钟）

**目标**：快速确认 MCP Server 启动、Tool/Resource/Prompt 注册成功

**工具**：`@modelcontextprotocol/inspector`（MCP 官方 CLI）

**当前用法**：Inspector 属于可选人工探索，不进入本轮默认 TestCase 或完成定义。当前协议合同以 `tests/blackbox/test_mcp_blackbox.py` 的 offline stdio 子进程为准。若后续真实数据流程需要 Inspector，应先提供受审计的启动脚本，显式设置 base-only/live sentinel 与目标工作树根；不要在文档中硬编码另一个 checkout，也不要直接启动会合并 local config 的默认入口。

**检查清单**：

- [ ] 14 个 Tool 全部列出 (12 只读 + 2 写入)
- [ ] 2 个静态 Resource 与 7 个 Resource Template 全部列出，并分别通过 `list_resources` / `list_resource_templates` 发现
- [ ] 3 个 Prompt 全部列出
- [ ] `search_knowledge "AI"` → 返回搜索结果
- [ ] `get_entry {valid_id}` → 返回知识条目
- [ ] `query_subgraph {knowledge_id}` → 返回 nodes / edges / truncated
- [ ] `explain_relation {source_id, target_id}` → 返回 summary / path
- [ ] `collect_evidence {question}` → 返回 seed / evidence[]
- [ ] `find_bridges {seed_id}` → 返回 `implementation_level=partial`，且 `evidence_sources` 中包含 `graph_bridge_signal`
- [ ] `timeline_of {topic}` → 当存在真实时间字段时返回 `inferred_time_field=event_time` 或 `published_at`，并包含 `structured_time_fields`
- [ ] `contrast {topic_a, topic_b}` → 返回 `shared_tags / only_a_tags / only_b_tags`，且 `comparison_dimensions` 中包含 `relation_graph_signal`
- [ ] `archive_url` 私网/非法输入在 Workflow/网络前拒绝，SQLite/Vault 均无副作用；真实 URL 留给独立 live 流程
- [ ] 无效参数测试 → 返回合理的错误信息
- [ ] 大数据量返回 → 不超时不崩溃

**优势**：直观、快速、官方工具、无需写代码

---

### 层 2：进程内 MCP 行为集成测试（5-15 分钟）

**目标**：通过注册后的 FastMCP Tool 验证 MCP 适配、序列化、搜索/详情衔接和生产引用卡片 formatter。该层的归档场景使用 fake `WorkflowEngine`，只计作 MCP 适配覆盖，不宣称真实 YAML/Processor/StoreStep 集成。

**环境**：`scripts/run-test.ps1` 强制的 `.data-test` 根、base-only Config、合成 SQLite/Vault 与 fake provider；无真实网络。

**脚本位置**：`tests/integration/test_mcp_client_simulation.py`

**5 个行为用例**：

1. 搜索后按返回 ID 获取详情，并验证稳定 schema。
2. fake Workflow 归档成功后可被搜索到，验证 MCP 响应适配与索引可见性。
3. 无效/私网 URL 在 Workflow 调用前拒绝。
4. 关联查询使用生产 `build_knowledge_reference()` 与 `format_reference_card_html()` 生成引用卡片。
5. 无结果返回稳定的空集合合同。

Prompt 模板的生产内容由 `tests/e2e/test_mcp_e2e_knowledge_qa.py` 等注册级/stdio 用例主责，本层不复制 Prompt 组装实现。

**执行方式**：

```powershell
# 自动化模式（不需要启动服务）
.\scripts\run-test.ps1 -DataRoot .data-test\mcp-simulation -Direct -Command @("python", "-m", "pytest", "tests/integration/test_mcp_client_simulation.py", "-v")

# 协议级调试由 stdio 黑盒用例负责；不要直接执行测试模块
.\scripts\run-test.ps1 -DataRoot .data-test\mcp-blackbox -Direct -Command @("python", "-m", "pytest", "tests/blackbox/test_mcp_blackbox.py", "-v")
```

**输出示例**：

```
✓ 场景 1: 知识库搜索 - schema、排序与详情一致
✓ 场景 2: 模拟归档 URL + 即刻搜索 - 无真实网络
✓ 场景 3: 关联查询 + 生产引用卡片格式化
✓ 错误路径: SSRF 输入拒绝、搜索无结果
```

---

### 层 3：pytest E2E 套件长期维护（30-60 分钟初建，后续自动化）

**目标**：完整的回归测试套件，可持续运行，检测 MCP 功能退化

**文件结构**：

- `tests/e2e/conftest.py`：构造隔离 `TestEnv` 与带超时的 `MCPTestClient`。
- `tests/e2e/fixture_utils.py`：在任何目录/SQLite 写入前校验路径，只允许当前工作树 `.data-test`。
- `tests/e2e/test_mcp_e2e_search.py`：搜索、过滤、分页和稳定响应合同。
- `tests/e2e/test_mcp_e2e_archive.py`：默认离线的输入拒绝/无副作用合同；真实 URL 用例仅在独立 live lane 选择。
- `tests/e2e/test_mcp_e2e_knowledge_qa.py`：生产 Prompt 注册内容与真实检索上下文传递。

**核心 Fixture 合同**：

- 子进程只经 `tests/offline_entrypoint.py mcp` 启动，不存在可直接实例化的 `MCPServer` 测试类。
- 默认模式必须同时满足 `PKV_TEST_OFFLINE=1`、`PKV_TEST_LOAD_LOCAL=0`、`PKV_RUN_LIVE=0`。
- 所有运行路径先规范化并验证位于 `.data-test`，再创建目录或初始化 SQLite。
- 子进程环境剥离 Provider secret/proxy，阻断 DNS 与非 loopback raw socket；该 Python guard 不等价于 OS 网络沙箱。
- 成功 Tool 响应必须是单一 JSON `TextContent` 且 `isError=false`，禁止忽略额外 content block。

**执行与报告**：

```powershell
# 运行所有 E2E 测试
.\scripts\run-test.ps1 -DataRoot .data-test\mcp-e2e -Direct -Command @("python", "-m", "pytest", "tests/e2e", "-v", "--tb=short")

# 仅运行搜索相关
.\scripts\run-test.ps1 -DataRoot .data-test\mcp-e2e -Direct -Command @("python", "-m", "pytest", "tests/e2e/test_mcp_e2e_search.py", "-v")

# 生成覆盖率报告
.\scripts\run-test.ps1 -DataRoot .data-test\mcp-e2e -Direct -Command @("python", "-m", "pytest", "tests/e2e", "--cov=src.mcp", "--cov-report=term-missing")
```

---

## 二、测试数据库准备

### 使用现有测试 Fixtures

不要复制一套只存在于文档的 `TEXT_SAMPLES/process_wechat/process_zhihu` 伪实现。进程内 MCP 合成数据由 `tests/integration/test_mcp_client_simulation.py` 主责；stdio/E2E 数据由 `tests/e2e/conftest.py` 和 `tests/e2e/fixture_utils.py` 主责。二者都必须先验证 `.data-test` containment，再初始化 SQLite/Vault，并由 fixture 负责状态恢复。

### 一键生成测试数据库

新建辅助脚本 `scripts/setup-test-db.py`：

```powershell
# 生成包含 20 条样本条目的隔离测试数据库
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\mcp-seed -Command @("python", "scripts/setup-test-db.py", "--seed", "42", "--count", "20", "--output", ".data-test/mcp-seed/db/knowledge_vault.db")

# 如需离线生成确定维度的测试向量索引，仍须经同一包装器
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\mcp-seed-vectors -Command @("python", "scripts/setup-test-db.py", "--seed", "42", "--count", "20", "--embedding-dim", "2560")
```

未传 `--embedding-dim` 时，脚本只生成 SQLite/Vault 测试数据并跳过随机向量
索引；它不会读取本机 Provider 配置、生产 `.data`，也不会调用真实 Embedding 服务。
本测试策略不使用外部输出能力；产品运行数据与 pytest temp/cache 必须留在当前工作树 `.data-test`。CI 报告和 Windows 包装器的命令桥接 payload 可位于受控 runner/系统临时目录，不得被产品代码作为数据根。

---

## 三、持续集成建议

### CI/CD 流水线

CI 必须复用与本地相同的 fail-closed 合同：数据根位于 checkout 的 `.data-test`，pytest `--basetemp` 位于该数据根，默认排除 `network/manual`，不注入 Provider secret。Windows 本地验证直接调用 `scripts/run-test.ps1`；现有 Linux workflow 暂以受控步骤显式设置同等路径/marker，P1 应抽出 POSIX wrapper 并增加跨平台 wrapper contract，避免安全逻辑长期复制。本文不提供面向开发者的裸 `pytest` 示例。

建议三个独立 gate：MCP unit、进程内 simulation、stdio/E2E offline；最后以 `--cov=src.mcp --cov-fail-under=95` 收口。live smoke 进入独立手动工作流，不作为 PR 必过项。

---

## 四、快速开始指南

### 第一步：自动化协议快速验证

先经包装器运行 `tests/blackbox/test_mcp_blackbox.py`，确认 offline entrypoint、握手、发现与关键调用。Inspector 仅在独立人工流程有明确目标时使用，不是自动化前置条件。

**预期结果**：当前 manifest 的 Tools/Resources/Prompts 全部可发现；精确数量只由单一注册合同主责。

---

### 第二天：Python 脚本全流程（30 分钟）

```powershell
# 1. 创建测试数据库
.\scripts\run-test.ps1 -DataRoot .data-test\mcp-simulation -Direct -Command @("python", "scripts/setup-test-db.py", "--output", ".data-test/mcp-simulation/db/knowledge_vault.db", "--count", "20")

# 2. 运行模拟客户端测试
.\scripts\run-test.ps1 -DataRoot .data-test\mcp-simulation -Direct -Command @("python", "-m", "pytest", "tests/integration/test_mcp_client_simulation.py", "-v", "-s")

# 3. 查看完整日志
```

**预期结果**：5 个进程内行为场景全部通过 ✅

---

### 第三天：建立 E2E 套件（60 分钟）

```powershell
# 1. 编写 E2E 测试用例（参考上述代码）
# 2. 运行 E2E 测试
.\scripts\run-test.ps1 -DataRoot .data-test\mcp-e2e -Direct -Command @("python", "-m", "pytest", "tests/e2e", "-v")

# 3. 查看覆盖率报告
.\scripts\run-test.ps1 -DataRoot .data-test\mcp-e2e -Direct -Command @("python", "-m", "pytest", "tests/e2e", "--cov=src.mcp", "--cov-report=term-missing")
```

**预期结果**：该层全部离线用例通过；覆盖率以跨 unit/integration/blackbox/E2E 的 `src.mcp >=95%` 单一 gate 为准，不为单层另设冲突阈值。

---

## 五、避坑指南

### 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| `ModuleNotFoundError: No module named 'mcp'` | MCP SDK 未安装 | `pip install mcp>=0.1.0` |
| `SSRF 防护拒绝本地 URL` | 安全验证生效 | 使用合成公开 URL + fake processor；不得关闭 SSRF 防护 |
| Inspector 连接超时 | MCP Server 未启动 | 检查 stdio 通信 |
| 搜索结果为空 | 合成 fixture 未填充 | 由当前测试 fixture 在 `.data-test` 内显式 seed；不要直接运行默认配置脚本 |
| Token 限制告警 | 测试数据过大 | 减少 `--count` 参数 |

---

## 六、验收标准

当前默认离线测试完成的标志：

- [ ] stdio 黑盒 discovery 与代表性 invoke 全部通过离线入口，且无真实网络、Provider 或本机配置读取
- [ ] 5 个进程内行为用例全部通过，并明确不把 fake Workflow 计作真实归档工作流集成
- [ ] 默认离线 E2E 全部通过，所有运行时路径位于本次 `.data-test/<case>` 根内
- [ ] 跨 unit/integration/blackbox/E2E 的单一覆盖门禁满足 `src.mcp >=95%`
- [ ] Inspector、真实 Provider、真实网页与真实数据均不属于本任务完成定义
- [ ] 并发、压力和长时间运行属于 P2 独立 lane，具备专用隔离与资源预算后再启用

---

## 七、后续扩展

- **压力测试**：Apache JMeter / Locust 模拟 100+ 并发
- **集成测试**：与真实 Claude Code MCP 配置集成
- **性能基准**：建立 Tool 响应时间基准
- **自动化 CI**：GitHub Actions / GitLab CI 每次 PR 自动运行
