# MCP 最小评测闭环

> 状态：Phase B citation 合同完成 / threshold-enforced 离线闭环
>
> 任务集：`pkv.mcp_quality_tasks.v1`
> 基线：见 [MCP最小评测基线-2026-07-29.md](./MCP最小评测基线-2026-07-29.md)

## 1. 评测范围

本闭环用 16 条固定、可版本控制的任务验证当前推理型 MCP：

- `query_subgraph`：多跳节点/边、关系类型过滤、参数上限
- `explain_relation`：直接关系、两跳路径、不可达关系
- `collect_evidence`：文档证据、chunk 相关性与可引用性、无命中/路径不可用/检索异常降级
- `find_bridges`、`timeline_of`、`contrast`：partial 标记、限制说明、证据来源与逐项 provenance
- Tool 发现/选择、gold 参数一致性、FastMCP schema 接受性和输入验证

评测经过真实的 FastMCP `list_tools` / `call_tool` 链路。关系查询、证据聚合和探索服务使用真实实现；数据来自临时 SQLite/Markdown 场景，检索与 chunk 命中使用最小确定性 fixture。

本闭环不评估外部 LLM 的自主 Tool 路由质量。gold `expected_call` 只保存在
`tasks.v1.yaml`，被评测的 `proposed_call` 独立保存在
`proposals.baseline.v1.yaml`；runner 按 `task_id` 对齐后分别评分。因此错误
Tool、错误参数或未来 Agent 预测不会与 gold 共用 YAML anchor/对象，也不能自证通过。

## 2. 离线与隔离契约

- 不需要真实 API key，不读取 `config/local.yaml`
- 不访问网络；固定来源使用保留域名 `example.test`
- 不读取或写入生产 `.data/`
- CLI 必须经 `scripts/run-test.ps1` 执行
- runner 在包装器提供的 `TMP_DIR` 下创建并自动清理临时数据库/Vault
- 可选 JSON 结果应写入 `.data-test/<scenario>/`，该临时产物不纳入 Git
- `taskset_path`、`proposals_path`、公共 `work_dir`、CLI 临时目录和 JSON
  输出都会在任何读取、`mkdir` 或写入前拒绝生产 `.data`

## 3. 运行命令

运行基线并把精简 JSON 留在隔离测试目录：

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\mcp-quality -Command @(
  "python", "-m", "evals.mcp_quality",
  "--enforce-thresholds",
  "--output", ".data-test/mcp-quality/result.json"
)
```

查看完整 Tool 输出时追加 `--include-outputs`。`--check-targets` 保留为
`--enforce-thresholds` 的兼容别名。

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

目标阈值由任务集本身版本控制，并报告为 `target_thresholds` /
`targets_met`。Phase B citation 合同完成后的策略是 `threshold_enforced`：

- 默认 CI 必须运行 `tests/integration/test_mcp_quality_eval.py`
- 集成回归固定 schema、16 条任务、119 项检查、独立 proposals 和全通过结果
- CLI 通过 `--enforce-thresholds` 把任务集内的目标阈值升级为退出码门禁
- 当前基线为 119/119，`citability=100%`、`targets_met=true`，兼容字段
  `thresholds_met=true`
- gold call、baseline proposals、119 项断言及各维度阈值均未因本次收口降低或改写

## 5. 版本化资产

- `evals/mcp_quality/tasks.v1.yaml`：16 条固定任务、gold call、断言和阈值
- `evals/mcp_quality/proposals.baseline.v1.yaml`：与 gold 独立的 baseline calls
- `evals/mcp_quality/scenario.py`：隔离场景与确定性 fixture
- `evals/mcp_quality/safety.py`：所有公开路径入口的生产 `.data` 前置护栏
- `evals/mcp_quality/scorer.py`：路径选择与通用断言评分
- `evals/mcp_quality/runner.py`：FastMCP 执行、聚合与 CLI
- `tests/unit/test_mcp_quality_scorer.py`：任务/评分器契约
- `tests/integration/test_mcp_quality_eval.py`：119/119 基线与门禁回归

后续变更必须保持 16 条任务、独立 proposals、119 项检查和现有阈值；
不得通过修改 gold/proposal、删除断言或改写离线 fixture 掩盖回归。
