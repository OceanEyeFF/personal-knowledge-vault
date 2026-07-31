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

评测经过真实的 FastMCP `list_tools` / `call_tool` / `read_resource`
链路。关系查询、证据聚合和探索服务使用真实实现；数据来自临时
SQLite/Markdown 场景，检索与 chunk 命中使用最小确定性 fixture，fixture
返回的 chunk id 同时存在于隔离 SQLite 的 `content_chunks` 中。

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
- 集成回归固定 schema、16 条任务、119 项期望检查与独立 proposals；是否全通过只由对应工作树的受控运行证据决定
- evaluation 运行时合同在 119 项评分之外逐项读取所有 citation locator；
  校验 bridge 路径连续性、完整候选邻接/断连子图和 semantic provenance，
  timeline 物理字段及 legacy 持久性，contrast provenance 完整性，并递归
  拒绝 Phase B 新公开响应中的绝对本机路径
- CLI 通过 `--enforce-thresholds` 把任务集内的目标阈值升级为退出码门禁
- 2026-07-29 基线快照为 119/119、`citability=100%`、`targets_met=true`，兼容字段 `thresholds_met=true`；2026-07-31 的当前 TestCase dirty tree 已通过 S4c 定向、integration、全量和 MCP coverage 复验
- gold call、baseline proposals、119 项断言及各维度阈值均未因本次收口降低或改写

## 5. 版本化资产

- `evals/mcp_quality/tasks.v1.yaml`：16 条固定任务、gold call、断言和阈值
- `evals/mcp_quality/proposals.baseline.v1.yaml`：与 gold 独立的 baseline calls
- `evals/mcp_quality/scenario.py`：隔离场景与确定性 fixture
- `evals/mcp_quality/safety.py`：所有公开路径入口的生产 `.data` 前置护栏
- `evals/mcp_quality/scorer.py`：路径选择与通用断言评分
- `evals/mcp_quality/runner.py`：FastMCP 执行、聚合与 CLI
- `tests/unit/test_mcp_quality_scorer.py`：任务/评分器契约
- `tests/integration/test_mcp_quality_eval.py`：119 项期望检查、报告 schema 与门禁回归

后续变更必须保持 16 条任务、独立 proposals、119 项检查和现有阈值；
不得通过修改 gold/proposal、删除断言或改写离线 fixture 掩盖回归。

当前精确引用 URI 规范：

- `pkv://entries/{id}/chunks/{chunk_id}`
- `pkv://entries/{id}/chunk-index/{chunk_index}`
- `pkv://entries/{id}/metadata/{time_field}`
- `pkv://relations/{relation_id}`
- `pkv://relations/by-edge/{source_id}/{target_id}/{relation_type}/{source_type}`

这些 URI 均是注册的 MCP Resource，不使用不可解析的 fragment。

`timeline_of` 只在真实持久时间字段存在时使用
`pkv://entries/{id}/metadata/{time_field}`。若候选没有任何持久时间字段，
item 必须输出空 `time_value/time_source_field`、
`time_source/time_precision=unavailable`，并回退可读的
`pkv://entries/{id}` 与公开 limitation。

Tool 和 Resource 的公开负载会清空盘符、UNC、POSIX 绝对路径及 `file:`
URI 形式的 `source_url`，相应来源回退 entry Resource；relation evidence
中的本地引用按值递归脱敏。固定评测启动预检与真实 FastMCP 集成回归均覆盖
这些负向 fixture，且不改变 16 条任务、119 项评分检查或阈值。

entry、chunk 与 frontmatter metadata-field Resource 只允许读取/返回父 entry
的 canonical resolved path 位于当前隔离 vault 内的普通文件；symlink escape、
vault 外文件、UNC、
目录和缺失文件均受控拒绝。无效/缺失 ID、loader 异常同样返回不含本机路径的
MCP 错误，而非普通 Markdown/JSON“错误页”。评测 `read_resource` 会拒绝空
内容、错误对象和历史伪成功错误文本，因此只有真正成功的 Resource 才计入可引用。
同一边界校验也用于 `get_entry` 的正文加载及 `collect_evidence` 的
`content_preview`：越界条目不会被工具旁路读取，前者降级为不含路径的
“内容不可用”，后者同时过滤文档与 chunk 检索候选并公开 vault 边界
limitation，越界 chunk 的文本不会进入公开证据。
`timeline_of` 与 `contrast` 也会排除无法由 entry Resource 回读的越界候选，
避免为其生成表面合法、实际必然失败的 fallback locator。

固定 runtime contract 递归收集全部 `*_locator`，并把公开字段中完整的
`pkv://` source/fallback 也视为 Resource 引用逐项读取；entry、chunk、
metadata field、relation Resource 还会核对端点、持久 ID/索引、必要字段和
canonical locator，不能以非空错误文本或错位 Resource 冒充可引用成功。
