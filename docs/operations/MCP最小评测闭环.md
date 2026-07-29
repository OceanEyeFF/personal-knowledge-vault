# MCP 最小评测闭环

> 状态：Phase C / P1 最小闭环
>
> 任务集：`pkv.mcp_quality_tasks.v1`
> 基线：见 [MCP最小评测基线-2026-07-29.md](./MCP最小评测基线-2026-07-29.md)

## 1. 评测范围

本闭环用 16 条固定、可版本控制的任务验证当前推理型 MCP：

- `query_subgraph`：多跳节点/边、关系类型过滤、参数上限
- `explain_relation`：直接关系、两跳路径、不可达关系
- `collect_evidence`：文档证据、chunk 相关性与可引用性、无命中/路径不可用/检索异常降级
- `find_bridges`、`timeline_of`、`contrast`：partial 标记、限制说明、证据来源、当前结果与待补的引用契约
- Tool 发现/选择、gold 参数一致性、FastMCP schema 接受性和输入验证

评测经过真实的 FastMCP `list_tools` / `call_tool` 链路。关系查询、证据聚合和探索服务使用真实实现；数据来自临时 SQLite/Markdown 场景，检索与 chunk 命中使用最小确定性 fixture。

本闭环不评估外部 LLM 的自主 Tool 路由质量。任务文件把自然语言请求、`proposed_call` 和 `expected_call` 一并版本化，因此当前验证的是 MCP 面向 Agent 的发现/调用契约；后续可用 Agent 预测替换 `proposed_call`，继续复用同一 scorer。

## 2. 离线与隔离契约

- 不需要真实 API key，不读取 `config/local.yaml`
- 不访问网络；固定来源使用保留域名 `example.test`
- 不读取或写入生产 `.data/`
- CLI 必须经 `scripts/run-test.ps1` 执行
- runner 在包装器提供的 `TMP_DIR` 下创建并自动清理临时数据库/Vault
- 可选 JSON 结果应写入 `.data-test/<scenario>/`，该临时产物不纳入 Git

## 3. 运行命令

运行基线并把精简 JSON 留在隔离测试目录：

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\mcp-quality -Command @(
  "python", "-m", "evals.mcp_quality",
  "--output", ".data-test/mcp-quality/result.json"
)
```

查看完整 Tool 输出时追加 `--include-outputs`。验证目标阈值时追加 `--enforce-thresholds`；当前基线因 4 个已知可引用性缺口会返回退出码 `1`，这是预期信号，不是 runner 异常。

运行相称回归测试：

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\mcp-quality-tests -Command @(
  "python", "-m", "pytest",
  "tests/unit/test_mcp_quality_scorer.py",
  "tests/integration/test_mcp_quality_eval.py",
  "-q"
)
```

## 4. 评分

每条任务固定评分检查及权重。总分按所有检查的加权通过率计算，并分维度报告：

- `tool_selection`
- `parameters`
- `result`
- `evidence_relevance`
- `citability`
- `degradation`

目标阈值由任务集本身版本控制。只要任一维度低于阈值，`thresholds_met=false`；`--enforce-thresholds` 据此返回非零退出码。

## 5. 版本化资产

- `evals/mcp_quality/tasks.v1.yaml`：16 条固定任务、gold call、断言和阈值
- `evals/mcp_quality/scenario.py`：隔离场景与确定性 fixture
- `evals/mcp_quality/scorer.py`：路径选择与通用断言评分
- `evals/mcp_quality/runner.py`：FastMCP 执行、聚合与 CLI
- `tests/unit/test_mcp_quality_scorer.py`：任务/评分器契约
- `tests/integration/test_mcp_quality_eval.py`：当前基线与失败矩阵回归

当 Phase B 修复使已知失败转为通过时，应同步更新基线报告和集成测试中的失败集合；不要降低阈值来掩盖改善或回归。
