# 更新日志 (Changelog)

所有重要的项目变更都将记录在此文件中喵～ ฅ'ω'ฅ

---

> 版本口径说明（2026-03-06）：
> - 当前仓库基线版本：`v0.8.0-alpha`
> - `v0.6.0` 表示 CLI 入口首次稳定引入
> - `v0.7.0` 表示 MCP 能力层首次稳定引入
> - `history/` 下的 `v0.8.0-beta / v0.8.0` 里程碑文档保留阶段性背景，不直接作为当前仓库发布标签

## [Unreleased] - 2026-03-11 (Phase A 收尾 / Phase B 推理基线推进)

### ✨ 新增功能

- ✅ **关系层基础骨架**
  - `src/relations/models.py`
  - `src/storage/relation_store.py`
  - `scripts/migrations/006_add_relations_foundation.sql`

- ✅ **低歧义关系抽取与安全回填**
  - `src/relations/extractors.py`
  - `scripts/backfill_relations.py`
  - `RelationStore.delete_outgoing_relations_for_knowledge()`

- ✅ **一跳关系查询与验证闭环**
  - `src/relations/query_service.py`
  - `RelationQueryResult.grouped_items`
  - `tests/unit/test_relation_query_service.py`
  - `tests/integration/test_relation_query_pipeline.py`

- ✅ **内部多跳子图查询基础**
  - `RelationQueryService.query_subgraph()`
  - `RelationSubgraphNode` / `RelationSubgraphResult`

- ✅ **最小关系解释基础**
  - `RelationQueryService.explain_relation()`
  - 直接关系优先，找不到时降级到受限跳数内的最短解释路径

- ✅ **最小推理型 MCP Tool**
  - `query_subgraph`
  - `explain_relation`
  - `collect_evidence`
  - `find_bridges` (partial)
  - `timeline_of` (partial)
  - `contrast` (partial)

- ✅ **迁移链健康检查**
  - `scripts/migrate.py --health-check`
  - `MigrationManager.run_health_check()`

- ✅ **Phase B 5.4 回填质量验证基线**
  - `backfill_quality_report.v1`
  - `scripts/backfill_relations.py` 质量门禁参数
  - `tests/fixtures/phase_b_5_4_min_regression.yaml`
  - `docs/operations/关系回填质量验证指南.md`

### 📝 变更

- `004_add_chat_sessions.sql` 标准化为 `v1.1.1`
- `005_add_review_system.sql` 标准化为 `v1.1.2`
- `006_add_relations_foundation.sql` 作为 `v1.2.0` 接入当前有效迁移链
- Batch2 当前覆盖低歧义关系：Markdown 显式链接、Front Matter `related_docs`，以及 Front Matter 关系字段 `children` / `version_of`
- `scripts/backfill_relations.py` 默认 `dry-run`，仅在显式传入 `--apply` 时写入关系表
- 当前已补上内部 `query_subgraph` 多跳子图遍历基础，并将 `query_subgraph` / `explain_relation` 暴露为最小推理型 MCP Tool，但仍未进行真实库正式回填执行
- 当前已补上 `collect_evidence(question, include_chunks=False/True)` 的 chunk-aware 证据包聚合路径，并将其接入只读 MCP Tool；默认仍保持文档级兼容行为
- 当前已补上 `collect_evidence` 的 `chunk_retrieval_status` 固定合同：`not_requested` / `success` / `no_hits` / `path_unavailable` / `search_error`
- 当前已补上 `find_bridges`、`timeline_of`、`contrast` 的 partial implementation，并在返回结构中显式标注 `implementation_level=partial`
- 当前已增强 `find_bridges`、`timeline_of`、`contrast` 的探索深度：桥接结果新增局部图桥接信号 `graph_bridge_score` / `graph_bridge_signal`，时间线新增 `structured_time_fields` 证据来源并优先使用真实时间字段，对比结果新增 `relation_graph_signal` 以及候选级 `relation_signal_score` / `relation_types`
- 当前 `timeline_of` 在多时间源并列主导场景下返回 `inferred_time_field=mixed`，避免单一高优先级字段导致整体偏乐观
- 当前已补上关系回填质量报告结构与可选质量门禁参数，可在测试副本库执行 `--apply` 演练并输出 JSON/YAML/Markdown 报告
- 当前已补上最小关系推理回归样例集，并通过数据文件驱动的集成测试固定 Phase B 5.4 基线
- 查询结果当前优先按 `relation_type` 分组，组内按 `weight DESC -> updated_at DESC -> relation_id ASC` 稳定排序
- `README.md`、当前事实基线、阶段路线与差异清单同步更新为 `Phase A / T1+T5` 口径

### 📦 当前工作区增量（2026-03-10 ~ 2026-03-31）

- `src/relations/models.py` 当前新增 `RelationSubgraphNode` 与 `RelationSubgraphResult`，作为内部多跳子图查询的统一返回结构
- `src/relations/query_service.py` 当前新增 `query_subgraph(seed_knowledge_id, depth, ...)`，基于现有一跳查询服务做受限 BFS 子图扩展
- `src/relations/query_service.py` 当前新增 `explain_relation(a, b, ...)`，优先返回直接关系解释，失败时降级为最短路径解释
- `src/relations/evidence_service.py` 当前新增 `collect_evidence(question, ...)`，围绕问题聚合 chunk-aware / 文档级兼容证据包，并为候选条目补充相对种子条目的关系解释
- `src/relations/exploration_service.py` 当前增强 `find_bridges(seed_knowledge_id, ...)`、`timeline_of(topic, ...)` 与 `contrast(topic_a, topic_b, ...)`：桥接结果新增局部图桥接信号，时间线优先使用结构化真实时间字段并支持 `inferred_time_field=mixed`，对比结果新增跨主题显式关系路径信号
- `src/mcp/tools.py` 当前新增 `query_subgraph`、`explain_relation`、`collect_evidence`、`find_bridges`、`timeline_of` 与 `contrast`，把 Phase B 的最小关系推理与受限探索能力接入 MCP
- `tests/unit/test_relation_query_service.py` 当前补充两跳子图查询与深度限制断言
- `tests/integration/test_relation_query_pipeline.py` 当前补充 `backfill -> query_subgraph` 与 `backfill -> explain_relation` 联通验证，并把样例图扩展到 `Alpha -> Gamma -> Delta`
- `tests/unit/test_relation_exploration_service.py` 与 `tests/integration/test_relation_query_pipeline.py` 当前补充探索能力增强后的断言，覆盖图桥接信号、真实时间优先级与关系图对比信号
- 当前工作区文档同步范围已覆盖 `README.md`、`当前事实基线-2026-03.md`、`当前战略与路线收敛-2026-03.md`、`PhaseB-推理型MCP路线图-2026-03.md`、`docs/modules/relations/RELATION_LAYER_DESIGN.md`、`docs/specs/interfaces/Relations接口规范.md` 与 `docs/modules/mcp/` 下的相关文档
- 当前语义上应理解为 `Phase A closeout with Phase B closeout pending`：主归属仍是 `Phase A` 收尾，Phase B 核心能力已到位，剩余项集中在 partial 收口与验收

### 🧪 测试

- 新增 `tests/unit/test_relation_store.py`
- 新增 `tests/integration/test_relations_migration.py`
- 新增 `tests/unit/test_relation_extractors.py`
- 新增 `tests/integration/test_relation_backfill.py`
- 新增 `tests/unit/test_relation_query_service.py`
- 新增 `tests/integration/test_relation_query_pipeline.py`
- 新增 `tests/unit/test_migration_manager_versions.py`
- 新增 `tests/unit/test_migration_health_check.py`
- `Phase A` 收尾回归建议命令：
  `pytest tests/integration/test_relations_migration.py tests/unit/test_relation_store.py tests/unit/test_relation_extractors.py tests/integration/test_relation_backfill.py tests/unit/test_relation_query_service.py tests/integration/test_relation_query_pipeline.py tests/unit/test_migration_manager_versions.py tests/unit/test_migration_health_check.py -q`

## [v0.8.0-alpha] - 2026-03-06 (当前仓库基线对齐)

### ✨ 新增功能

- ✅ **当前事实基线文档**
  - `docs/overview/当前事实基线-2026-03.md`
  - `docs/overview/阶段开发路线与依赖-2026-03.md`
  - `docs/overview/文档与代码差异清单-2026-03.md`

### 📝 变更

- 统一仓库当前基线版本为 `v0.8.0-alpha`
- `README.md` 当前版本口径改为“当前仓库基线”
- `src/__init__.py` 与 `src/main.py` 版本号对齐到 `0.8.0-alpha`
- CLI 黑盒测试中的版本断言同步更新

### 📚 文档更新

- 补齐版本口径说明，区分“当前仓库基线”和“能力首次引入版本”
- 明确 `history/` 下的后续里程碑文档不直接承担当前发布标签职责

---

## [v0.7.0] - 2026-02-19 (M8+M9 MCP 服务)

### ✨ 新增功能

- ✅ **MCP 只读服务**
  - `search_knowledge`
  - `get_entry`
  - `list_entries`
  - `list_tags`
  - `get_stats`

- ✅ **MCP 写入与关联能力**
  - `archive_url`
  - `archive_text`
  - `get_related`

- ✅ **MCP Prompt / Resource**
  - 3 个 Prompt 模板
  - 4 个 Resource

- ✅ **安全加固**
  - SSRF 拦截
  - 文本长度限制
  - Bearer Token 认证

### 📝 变更

- `README.md` 中的 MCP 功能说明与当前真相源保持一致
- `src/mcp/` 模块成为当前一等交互面

---

## [v0.6.1] - 2026-02-16 (AI 安全 + 数据库迁移)

### ✨ 新增功能

#### AI 安全测试功能 ✅

- ✅ **AI 安全规则** ([.ai-safety-rules.md](../../.ai-safety-rules.md))
  - 禁止操作生产数据目录 (.data/)
  - 强制使用测试环境 (run-test.ps1)
  - 备份要求和典型场景处理

- ✅ **Claude 忽略文件** ([.claudeignore](../../.claudeignore))
  - 防止 AI 访问 .data/ 生产数据
  - 防止 AI 访问 .env 敏感配置
  - 防止 AI 修改 .data-backup/ 备份

- ✅ **环境检测脚本** ([scripts/check-environment.ps1](../../scripts/check-environment.ps1))
  - 自动检测生产/测试环境
  - 显示数据库统计信息
  - 给出安全建议

- ✅ **测试环境文档**
  - [docs/operations/testing/AI安全与数据库升级完整方案.md](./testing/AI安全与数据库升级完整方案.md) - 总结文档
  - [docs/operations/testing/测试环境隔离指南.md](./testing/测试环境隔离指南.md) - 完整指南
  - [docs/operations/testing/测试环境快速开始.md](./testing/测试环境快速开始.md) - 3 分钟入门
  - [docs/operations/testing/测试环境演示.md](./testing/测试环境演示.md) - 完整演示

#### 数据库增量迁移系统 ✅

- ✅ **迁移管理器** ([src/storage/migration_manager.py](src/storage/migration_manager.py))
  - 获取当前数据库版本
  - 扫描待执行的迁移脚本
  - 自动执行迁移（支持自动备份）
  - 语义化版本号比较

- ✅ **命令行迁移工具** ([scripts/migrate.py](scripts/migrate.py))
  - 交互式升级（python scripts/migrate.py）
  - 自动升级（--auto）
  - Dry-run 模式（--dry-run）
  - 版本查看（--version）

- ✅ **迁移脚本目录** ([scripts/migrations/](scripts/migrations/))
  - 001_initial_schema.sql (v1.0.0) - M1 初始 Schema
  - 002_add_cli_tables.sql (v1.1.0) - M6 CLI 统计表
  - README.md - 迁移脚本说明

- ✅ **Schema 版本管理表**
  - schema_version 表追踪升级历史
  - 记录版本号、描述、应用时间

- ✅ **数据库迁移文档** ([docs/operations/数据库迁移指南.md](./数据库迁移指南.md))
  - 版本管理机制
  - 迁移脚本标准
  - 完整升级流程
  - 回滚程序

#### 测试脚本 ✅

- ✅ **手动测试脚本** ([tests/manual_test_text_archive_safe.py](tests/manual_test_text_archive_safe.py))
  - 环境检测演示
  - 纯文本归档测试（知乎回答样本）
  - 生产环境验证

### 🐛 Bug 修复

- ✅ **TextFallbackProcessor** ([src/processors/text_fallback_processor.py](src/processors/text_fallback_processor.py))
  - 修复 source_type：`"text_fallback"` → `"text"`
  - 符合 M5.1 Bug 修复规范

### 📝 变更

- .gitignore - 添加 .env.test, .data-test/, .data-backup/ 排除
- docs/README.md - 更新文档总索引与分类说明

### 📚 文档更新

- 新增 7 份核心文档（测试环境 + 数据库迁移）
- 更新文档分类清单（从 53 份增至 60 份）

---

## [v0.6.0] - 2026-02-16 (M6+M7)

### ✨ 新增功能

#### Milestone 6: CLI 入口与交互界面 ✅

- ✅ **CLI 入口** ([src/main.py](src/main.py))
  - Click 命令组
  - 全局参数（--verbose, --debug）
  - 版本信息（0.6.0）

- ✅ **6 个核心命令** ([src/cli/commands.py](src/cli/commands.py))
  - pkv archive - 归档内容（集成 WorkflowEngine）
  - pkv search - 搜索知识库（集成 QueryRouter）
  - pkv show - 显示条目详情
  - pkv list - 列出所有条目
  - pkv config - 配置管理（show/get/set）
  - pkv stats - 统计信息

- ✅ **终端 UI 组件** ([src/cli/ui.py](src/cli/ui.py))
  - Rich.Progress 进度条
  - Rich.Table 表格
  - Rich.Panel 面板
  - Rich.Confirm 确认对话框

- ✅ **输出格式化器** ([src/cli/formatters.py](src/cli/formatters.py))
  - JSON 格式化
  - Markdown 格式化
  - 搜索结果表格
  - 条目详情面板

- ✅ **测试覆盖**
  - tests/unit/test_cli_commands.py（17 个单元测试）
  - tests/unit/test_cli_ui.py（UI 组件测试）
  - tests/unit/test_cli_formatters.py（格式化器测试）
  - tests/integration/test_cli_e2e.py（端到端测试）
  - 测试覆盖率 ≥ 80%

#### Milestone 7: 文档完善与交付 ✅

- ✅ **使用手册** ([docs/operations/使用手册.md](./使用手册.md))
  - 快速开始
  - 核心功能详解
  - 配置和定制
  - 故障排查

- ✅ **维护指南** ([docs/operations/维护指南.md](./维护指南.md))
  - 系统架构
  - 日常维护
  - 性能监控
  - 扩展开发

- ✅ **环境变量模板** ([.env.example](.env.example))
  - DEEPSEEK_API_KEY
  - OPENAI_API_KEY

### 📝 变更

- requirements.txt - 添加 Click 依赖
- docs/operations/API文档.md - 补充 CLI 命令参考

### 🧪 测试

- 单元测试: 17+ 个测试用例
- 集成测试: 端到端测试通过
- 测试覆盖率: ≥ 80%

### 📦 依赖

- click>=8.0.0（新增）

---

## [v0.1.0] - 2026-02-14

### ✨ 新增功能

#### Milestone 1: 基础设施层 ✅

**核心模块**:
- ✅ **配置系统** ([src/utils/config.py](src/utils/config.py))
  - YAML 配置文件支持
  - 环境变量支持
  - 单例模式实现
  - 自动创建数据目录

- ✅ **Markdown 存储** ([src/storage/markdown_store.py](src/storage/markdown_store.py))
  - YAML Front Matter 解析
  - Entry 数据类定义
  - 文件保存和加载
  - 文件名自动生成

- ✅ **SQLite 存储** ([src/storage/sqlite_store.py](src/storage/sqlite_store.py))
  - 完整 Schema 设计（5 张表）
  - FTS5 全文搜索支持
  - jieba 中文分词集成
  - 自动触发器同步

- ✅ **向量存储** ([src/storage/vector_store.py](src/storage/vector_store.py))
  - hnswlib HNSW 算法
  - 文档级和分块级向量索引
  - 高效近似最近邻搜索

- ✅ **文本处理** ([src/utils/text_utils.py](src/utils/text_utils.py))
  - jieba 中文分词
  - 文件名清理
  - 字数统计

**开发工具**:
- ✅ **验证脚本** ([src/utils/verify_setup.py](src/utils/verify_setup.py))
  - 全面的系统验证
  - 模块测试
  - 详细日志记录

**安装脚本** ([scripts/](scripts/)):
- ✅ **Conda 方案** (推荐) ⭐
  - `setup-conda.ps1` - 自动创建 Python 3.11 环境
  - `test-conda.ps1` - Conda 环境测试
  - 完整测试验证通过

- 📦 **Legacy venv 方案** (已归档)
  - 移动到 `scripts/legacy/` 目录
  - 不推荐使用（Python 3.13 兼容性问题）
  - 详见 `scripts/legacy/README.md`

**文档**:
- ✅ [RUN_ME_FIRST.md](../../RUN_ME_FIRST.md) - 快速开始指南
- ✅ [scripts/README.md](../../scripts/README.md) - 脚本详细说明
- ✅ [QUICKSTART.md](QUICKSTART.md) - 安装教程
- ✅ [docs/operations/开发环境搭建.md](./开发环境搭建.md) - 详细环境搭建指南

### 🐛 问题修复

- ✅ 修复 pytest 版本冲突（8.0.0 → 7.4.4，兼容 pytest-asyncio）
- ✅ 升级 lxml 到 5.3.0（支持 Python 3.13）
- ✅ 升级 playwright 到 1.48.0（支持 Python 3.13）

### ⚠️ 已知问题

- Python 3.13 存在依赖兼容性问题（`lxml`、`greenlet` 编译失败）
  - **解决方案**: 使用 Conda 创建 Python 3.11 环境
  - **影响**: 仅影响使用 Python 3.13 的用户

### 📝 技术栈

**核心依赖**:
- Python 3.11
- SQLite 3.35+ (FTS5)
- hnswlib 0.8.0
- jieba 0.42.1
- python-frontmatter 1.1.0
- pyyaml 6.0.1

**AI 服务** (Milestone 2):
- DeepSeek API (摘要生成)
- OpenAI API (Embedding)

**开发工具**:
- pytest 7.4.4
- black 24.1.1
- mypy 1.8.0

### 🎯 下一步计划

**Milestone 2: AI 服务层** (计划中):
- [ ] DeepSeek 客户端封装
- [ ] OpenAI 客户端封装
- [ ] Embedder 向量化服务
- [ ] Prompt 模板管理

**Milestone 3: 内容处理器** (计划中):
- [ ] 微信文章处理器
- [ ] 知乎内容处理器
- [ ] 通用网页处理器

---

## 版本说明

版本格式: `v主版本.次版本.修订版本`

- **主版本**: 重大架构变更或不兼容更新
- **次版本**: 新功能添加（向后兼容）
- **修订版本**: Bug 修复和小改进

---

*作者: 幽浮酱 ฅ'ω'ฅ*
*项目代号: Personal Knowledge Vault*
