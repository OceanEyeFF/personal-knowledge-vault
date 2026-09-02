# Personal Knowledge Vault

> **AI-First Knowledge Workflow System**
> 工作流驱动的个人知识管理系统

[![Version](https://img.shields.io/badge/version-0.8.1-blue.svg)](./docs/operations/CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.11+-green.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-orange.svg)](./LICENSE)
[![Status](https://img.shields.io/badge/status-alpha_development-yellow.svg)](./docs/history/milestones/)

> 当前仓库基线：`0.8.1`
> 说明：`v0.6.0` 是 CLI 首次稳定引入版本，`v0.7.0` 是 MCP 首次稳定引入版本；桌面 GUI 已迁至独立的 `pkv-GUI` 仓库，本仓库只维护无头 Kernel、CLI 与 MCP。
> 命名说明：当前路线里提到的“当前开发 Phase 1”实际对应 `Phase A：Relation Foundation`；历史文档中的旧 `Phase 1` 已归档完成，两者不是同一时间轴。
> 2026-07-31 离线收口：Phase C 固定评测为 16 tasks / 119 checks，`overall=1.0`、`citability=1.0`、0 failed、`thresholds_met=true`；三个探索 Tool 按 `partial-v1` 口径交付，公开合同仍诚实声明 `implementation_level=partial`。
> 2026-08-07 M13 W1 安全复审：统一 runtime layout/bootstrap、Vault containment、跨存储补偿/repair 终态及 fail-closed migration 已完成；SQLite 事务提交凭据、Markdown identity + SHA-256 和 Vector 持久 pair marker 已补齐。W1 冻结时离线验收为 unit `1583 passed, 19 skipped`、integration/blackbox/e2e `277 passed, 9 deselected`。
> 2026-08-07 M13 W2 收口：Workflow、Retrieval、MCP 与当时的 GUI Chat 源代码合同均已 `source_verified`；GUI 后续已从本仓库拆出。
> 2026-08-11 M13 W3/W4：可复现打包链、payload 外 deterministic loopback harness 与 installed-Artifact 全矩阵已完成。外部 artifact-only 运行 `w4-53a45ed` 得到 10 个 scenario / 11 行 matrix / 10 verified / 0 failed / 0 pending，`functional_verified=true`；这是拆分前 held candidate 的历史证据。当前 headless payload 仍为 `test_candidate`，当前合规合同的 3 项 blocker 使其保持 `release_eligible=false`、`decision=hold`，不得作为正式发布。
> 2026-08-19 K1a/K2/K1b：`pkv_kernel` 已冻结公开 API、版本/能力握手和 reload snapshot 合同，并完成本地离线 wheel/clean-install 验证；仓库外 clean venv 仅通过已安装的 `pkv_kernel` 启动，资源与合成数据根均受合同约束。默认离线全量为 `2954 passed, 21 skipped, 219 deselected`；这不是 PyPI 或正式发布结论。
> 2026-08-21 R1–R4：个人配置/数据布局、inspect → plan → confirm → execute、受支持业务/数据写入的 lease / `write_busy` / 审计，以及 staged Embedding generation 的限定内部合同已由最终隔离全量 `3182 passed, 21 skipped, 219 deselected, 35 warnings` 验证。R2 CLI 已补 nested fresh-root adapter 回归，覆盖 setup plan、plan-only 零写及 PLAN_ID / `--allow-network` gate；R4 不公开 rebuild action；R3.1 与 public rebuild adapter 仍为后续 P1 gate，不改变 M13 release hold、PyPI、默认数据根或 MCP stdio-only 合同。

## ✨ 核心特点

- 🤖 **AI-First 设计**: 以 Claude Code/CodeX 作为智能协作伙伴，支持人机协作的知识处理
- 🔌 **MCP 服务**: 标准 MCP 协议集成，AI Agent 可直接搜索、归档、浏览知识库
- 🔍 **智能混合检索**: 自动路由 BM25/向量/混合检索策略，精确高效零成本浪费
- ⚡ **工作流驱动归档**: 真实版本化 YAML 编排归档，终态与问题可观测
- 🔒 **本地优先**: 数据完全掌控，Markdown 主存储，SQLite+向量辅助索引
- 💰 **成本可控**: BM25 路径不构造 Provider，语义能力按需启用
- 🛡️ **AI 安全**: 内置安全规范，测试环境完全隔离，数据备份自动化

> **当前支持边界**：本仓库的发布面只包含 Windows-first 的 CLI 与 MCP stdio；桌面 GUI 是单独仓库中的外部 Kernel Wrapper。向量/混合检索属于 CLI/MCP 的显式策略能力并依赖正常 Provider 配置。MCP HTTP transport 与 Bearer 合同不在本次发布面。默认自动化全程离线，只使用合成数据和可控 doubles，不读取真实 API key、真实 Provider 或真实 Vault。

## 🎯 核心功能

### 📥 内容归档 (已实现)
- 📱 **微信文章**: 保留格式，自动提取正文和元数据
- 💬 **知乎内容**: 问题、回答、专栏，支持评论区归档
- 🌐 **通用网页**: 智能正文提取，去除广告和无关内容
- 💬 **聊天记录**: 微信聊天导出（邮件格式、文本格式）
- 🤖 **AI 对话**: ChatGPT/DeepSeek 对话历史（HTML/Markdown 格式）
- 📝 **纯文本**: Fallback 处理器，任何文本内容都能归档

### 🔍 智能检索 (已实现)
- **BM25 检索**: 短查询（<5 tokens）精确关键词匹配
- **向量检索**: CLI/MCP 可显式选择的语义检索
- **混合检索**: 长查询（≥5 tokens）的默认自动路由，使用加权 RRF (k=60)
- **显式结果**: `SearchResponse` 区分 `success/no_hits/invalid/error/degraded`
- **中文优化**: jieba 分词 + 自定义词典支持

### 🤖 AI 能力 (已实现)
- **智能摘要**: 三层摘要（一句话/100字/详细版）
- **标签提取**: 自动生成标签和关键词
- **内容分析**: 识别内容类型、评估字数复杂度
- **Embedding**: 默认 OpenAI text-embedding-3-small；模型/维度与向量索引绑定，不建议随意更换
- **成本控制**: DeepSeek API 低成本方案

### 💻 CLI 命令

源码树使用模块入口；Windows held test candidate 安装后才提供同名
pkv.exe 可执行文件。

~~~bash
# 归档 URL 和纯文本
python -m src.main archive https://mp.weixin.qq.com/xxx
python -m src.main archive-text "一条本地笔记" --title "示例笔记"

# 搜索、浏览、标签和已有向量近邻
python -m src.main search "关键词"
python -m src.main show <knowledge_id>
python -m src.main list --limit 20
python -m src.main tags --format json
python -m src.main related <knowledge_id> --format json

# 只读配置与统计
python -m src.main config show
python -m src.main stats
~~~

### 🔌 MCP 服务 (v0.7.0 新增)

AI Agent（Claude Code、Cursor 等）通过 MCP 协议直接操作知识库：

```powershell
# 启动 MCP Server（stdio 模式，供 Claude Code/Cursor 集成）
.\scripts\run-windows.ps1 python -m src.mcp
```

M13 只支持 `stdio`。`streamable-http` 与 Bearer Token 认证均未发布；服务入口会在初始化配置、数据或监听端口前拒绝非 stdio transport。

**15 个 Tool（13 个只读 + 2 个写入）**:
- `search_knowledge` — 智能搜索（BM25/向量/混合）
- `get_entry` / `list_entries` / `list_tags` / `get_stats` — 只读浏览
- `get_runtime_status` — 只读检查运行时 readiness 与下一步计划，不初始化、修复或探测 Provider
- `archive_url` — 归档网页（SSRF 防护）
- `archive_text` — 归档纯文本
- `get_related` — 关联知识推荐（向量相似度）
- `query_subgraph` — 关系子图查询
- `explain_relation` — 关系解释
- `collect_evidence` — 证据包聚合
- `find_bridges` — 桥接候选发现（partial-v1）
- `timeline_of` — 弱时间线重建（partial-v1）
- `contrast` — 主题对比（partial-v1）

**3 个 Prompt 模板**: `search_and_summarize` / `knowledge_qa` / `idea_sharpen`

**9 个 Resource**: 条目全文/元数据、精确 chunk、时间字段、关系边、标签列表与统计信息

**安全加固**: URL 抓取在 DNS、连接目标、重定向与子资源阶段执行 SSRF 重校验；文本输入有长度限制。M13 不发布网络 MCP transport，因此没有 Bearer Token 发布合同。

### 🕸️ 关系层基础 (Phase A / T1+T5 已实现)

- 已新增 `src/relations/models.py`，定义低歧义关系的类型、方向、来源和查询结果结构
- 已新增 `src/storage/relation_store.py`，提供 `knowledge_relations` 的最小读写能力
- 已新增 `scripts/migrations/006_add_relations_foundation.sql`，显式引入关系表与索引
- 已新增 `src/relations/extractors.py`，当前支持 Markdown 显式链接、Front Matter `related_docs`，以及 Front Matter 关系字段 `children` / `version_of` 的低歧义关系抽取
- 历史 `scripts/backfill_relations.py` 入口已停用：除 `--help` 外的裸脚本调用会在读取 `Config` 或数据根前 fail-closed（exit 2）；它不能绕过 lifecycle 或单写者 lease
- 已扩展 `src/relations/query_service.py`，当前提供一跳关系查询、内部 `query_subgraph` 多跳子图遍历基础、最小 `explain_relation` 能力、关系类型分组和稳定排序能力
- 已新增 `src/relations/evidence_service.py`，当前提供文档级 `collect_evidence` 证据包聚合 v1
- 已新增 `src/relations/exploration_service.py`，当前提供 `find_bridges`、`timeline_of`、`contrast` 的受限版本
- 当前已把 `query_subgraph`、`explain_relation` 与 `collect_evidence` 暴露为只读 MCP Tool；其中 `collect_evidence` 默认保持文档级证据包兼容行为，显式传入 `include_chunks=true` 时可返回 chunk 级证据字段，并执行近重复去重与多因子排序
- 当前已把 `find_bridges`、`timeline_of`、`contrast` 暴露为只读 MCP Tool，并按 `partial-v1` 口径完成最小可用交付；公开响应仍声明 `implementation_level=partial`，不等同于 full implementation：`find_bridges` 当前结合显式关系子图、局部图桥接信号与轻量文本重合；`timeline_of` 当前优先使用 `event_time > published_at > archived_at` 的结构化真实时间字段；`contrast` 当前在稳定对比维度之外补入跨主题显式关系路径信号与候选级 `relation_signal_score` / `relation_types`

## 📂 项目结构

```
personal-knowledge-vault/
├── README.md                          # 项目说明
├── CLAUDE.md                          # AI 协作上下文（根级索引）
├── RUN_ME_FIRST.md                    # 快速开始指南
├── config/config.yaml                 # bundled 默认配置（只读，不含私钥）
├── requirements.txt                   # Python 依赖
│
├── src/                               # 源代码（以下为核心结构，非完整文件清单）
│   ├── main.py                        # CLI 入口
│   ├── cli/                           # CLI 系统 (v0.6.0)
│   │   ├── commands.py                # 9 个公开命令
│   │   ├── ui.py                      # Rich Console 界面
│   │   └── formatters.py              # 输出格式化
│   ├── runtime/                       # 资源/用户路径、bootstrap 与启动保护
│   ├── application/                   # 共享依赖 composition 与领域操作
│   ├── kernel/                        # 稳定的无头 Kernel facade
│   ├── mcp/                           # MCP 服务 (v0.7.0)
│   │   ├── server.py                  # FastMCP 主入口 (M13: stdio only)
│   │   ├── tools.py                   # 15 个 Tool handler
│   │   ├── resources.py               # 9 个 Resource handler
│   │   ├── prompts.py                 # 3 个 Prompt 模板
│   │   └── utils.py                   # 安全验证 + 序列化
│   ├── processors/                    # 内容处理器
│   │   ├── wechat_processor.py        # 微信文章
│   │   ├── zhihu_processor.py         # 知乎内容
│   │   ├── chat_processor.py          # 聊天记录
│   │   ├── ai_chat_processor.py       # AI 对话
│   │   ├── generic_processor.py       # 通用网页
│   │   └── text_fallback_processor.py # 纯文本
│   ├── relations/                     # 关系模型与类型定义 (Phase A)
│   │   ├── models.py                  # 关系记录 / 查询结果模型
│   │   ├── extractors.py              # 低歧义关系抽取与回填服务
│   │   ├── query_service.py           # 一跳关系查询与分组服务
│   │   ├── evidence_service.py        # 文档级证据包
│   │   ├── exploration_service.py     # 受限探索服务
│   │   └── citations.py               # 引用投影
│   ├── storage/                       # 三层存储
│   │   ├── markdown_store.py          # Markdown 主存储
│   │   ├── sqlite_store.py            # SQLite 元数据索引
│   │   ├── vector_store.py            # hnswlib 向量索引
│   │   ├── relation_store.py          # knowledge_relations 存储
│   │   ├── migration_manager.py       # 数据库迁移与健康检查
│   │   ├── coordinator.py             # 跨存储终态协调
│   │   ├── sqlite_connection.py       # SQLite 连接与事务边界
│   │   └── vault_paths.py             # Vault containment
│   ├── retrieval/                     # 检索引擎 (6 个)
│   │   ├── bm25_retriever.py          # BM25 关键词检索
│   │   ├── vector_retriever.py        # 向量语义检索
│   │   ├── hybrid_retriever.py        # 混合检索 (RRF)
│   │   └── query_router.py            # 智能路由
│   ├── workflow/                      # 工作流引擎
│   │   ├── engine.py                  # 编排引擎
│   │   ├── steps.py                   # 工作流步骤
│   │   └── models.py                  # 数据模型
│   ├── ai/                            # AI 服务
│   │   ├── deepseek_client.py         # DeepSeek 摘要
│   │   ├── embedder.py                # OpenAI Embedding
│   │   └── prompts/                   # 提示词模板
│   └── utils/                         # 工具函数
│
├── pkv_kernel/                        # 外部 Wrapper 唯一可导入的 Kernel API
│
├── config/                            # 配置文件
│   ├── config.yaml                    # 主配置
│   ├── workflows/                     # 工作流定义
│   │   ├── archive-url.yaml           # URL 归档工作流
│   │   └── archive-text.yaml          # 文本归档工作流 (v0.7.0)
│   └── custom_dict.txt                # 自定义分词词典
│
├── scripts/                           # 运维脚本 (v0.6.0)
│   ├── setup-conda.ps1                # Conda 环境安装
│   ├── test-conda.ps1                 # 环境验证
│   ├── backup-data.ps1                # 数据备份
│   ├── restore-data.ps1               # 数据恢复
│   ├── backfill_relations.py          # 已停用的历史关系回填入口（fail-closed）
│   ├── migrate.py                     # 已停用的历史 Schema 迁移入口（fail-closed）
│   ├── run-test.ps1                   # 默认隔离测试入口
│   ├── build-release.ps1              # W3 可复现 held-candidate 构建入口
│   └── migrations/                    # SQL 迁移脚本
│
├── tests/                             # 测试套件（精确 lane/证据见 tests/CLAUDE.md）
│   ├── unit/                          # 单元测试
│   ├── integration/                   # 集成测试
│   ├── e2e/                           # E2E 测试
│   ├── blackbox/                      # 黑盒测试
│   ├── fixtures/                      # 测试数据
│   └── manual_test_*.py               # 手动测试脚本
│
├── docs/                              # 文档
│   ├── overview/                      # 项目定位、PRD、架构、技术选型
│   ├── modules/                       # 模块级设计文档
│   ├── specs/                         # 接口、模型、Schema、数据流
│   ├── operations/                    # 安装、使用、维护、迁移、测试环境
│   ├── history/                       # 历史 Prompt、问题、讨论、里程碑
│   └── README.md                      # 文档总索引
│
%USERPROFILE%/.pkv/                    # 用户 profile（不在仓库或 payload 中）
├── config.yaml                        # 唯一可编辑配置，可含 Provider Key/Cookie
└── data/                              # 默认运行数据根（PKV_DATA_ROOT 可覆盖）
    ├── config/local.yaml              # PKV 管理的无密钥 runtime snapshot
    ├── db/knowledge_vault.db          # SQLite 数据库
    ├── vectors/                       # 向量索引 generation
    ├── vault/                         # Markdown 文件存储
    └── logs/                          # 日志与本地审计轨迹
```

## 🚀 快速开始

### 环境要求

**推荐配置** (避免兼容性问题):
- **Conda** (Miniconda 或 Anaconda) 🌟 [推荐]
- Python 3.11+ (通过 Conda 自动创建)
- SQLite 3.35+ (支持 FTS5，通常系统自带)

**Provider 配置**：个人运行模式当前要求同时配置 LLM 与 Embedding Provider；首次初始化会先展示 endpoint、发送内容和潜在费用，并在用户确认后做最小健康探测。唯一可编辑文件是 `%USERPROFILE%\.pkv\config.yaml`；`<data-root>\config\local.yaml` 是无密钥运行态快照，不能拿来保存 Key。任何自动化/Agent 验证都不得使用真实 key、真实 Provider 或真实数据。

### Codex/Claude 专用环境

为 AI 协作者准备的隔离环境（推荐）：

```bash
conda create -y -n py311-private python=3.11
conda install -y -n py311-private -c conda-forge hnswlib=0.8.0
conda run -n py311-private python -m pip install -r requirements.txt
conda activate py311-private
```

### ⚡ 3 步安装 (推荐：Conda 方式)

```powershell
# 1️⃣ 克隆仓库
git clone https://github.com/OceanEyeFF/personal-knowledge-vault.git
cd personal-knowledge-vault

# 2️⃣ 运行自动安装脚本（创建 Python 3.11 环境 + 安装依赖）
.\scripts\setup-conda.ps1

# 3️⃣ 配置唯一的用户私有文件（首次 setup 前由用户手工完成）
notepad $env:USERPROFILE\.pkv\config.yaml

# ✅ 验证安装
.\scripts\test-conda.ps1

# 🎉 开始使用
.\scripts\run-windows.ps1 python -m src.cli.commands --help
```

> **Configuration migration:** 唯一用户业务配置是 `%USERPROFILE%\.pkv\config.yaml` 的 `ai.llm.*` / `ai.embedding.*` 键；`PKV_DATA_ROOT` 与 `PKV_LOG_LEVEL` 是正式环境覆盖。`<data-root>\config\local.yaml` 不参与业务配置 merge，只保存 PKV 管理的无密钥 runtime snapshot。不要恢复旧 Provider 环境变量、`.env` 加载或第二个用户配置源；外部集成应使用 `pkv_kernel` 的稳定公开接口。

### 📚 详细指南

| 指南 | 内容 | 适合场景 |
|------|------|----------|
| [RUN_ME_FIRST.md](RUN_ME_FIRST.md) | 3 步快速开始 | 首次使用 |
| [docs/operations/QUICKSTART.md](docs/operations/QUICKSTART.md) | 详细安装指南 | 遇到问题时参考 |
| [docs/operations/使用手册.md](docs/operations/使用手册.md) | 用户手册 | 日常使用参考 |
| [docs/operations/API文档.md](docs/operations/API文档.md) | API 参考 | 开发集成 |
| [docs/operations/数据库迁移指南.md](docs/operations/数据库迁移指南.md) | 数据库升级 | 版本更新时 |

## 📖 文档索引

### 🚀 快速开始

| 文档 | 说明 | 时长 |
|------|------|------|
| [RUN_ME_FIRST.md](RUN_ME_FIRST.md) | 3 步快速开始 | 5 分钟 |
| [docs/operations/QUICKSTART.md](docs/operations/QUICKSTART.md) | 详细安装指南 | 15 分钟 |
| [docs/operations/使用手册.md](docs/operations/使用手册.md) | 用户手册（含示例） | 30 分钟 |
| [docs/operations/API文档.md](docs/operations/API文档.md) | API 参考文档 | 按需查阅 |

### 📐 架构设计

| 文档 | 内容 | 适合读者 |
|------|------|---------|
| [CLAUDE.md](CLAUDE.md) | 项目索引（AI 协作上下文） | AI 协作者 |
| [docs/overview/personal-knowledge-vault-prd.md](docs/overview/personal-knowledge-vault-prd.md) | 产品需求文档 | 产品经理、决策者 |
| [docs/overview/架构设计.md](docs/overview/架构设计.md) | 系统架构与数据流 | 架构师、开发者 |
| [docs/overview/技术选型.md](docs/overview/技术选型.md) | 技术栈对比与决策 | 技术决策者 |
| [docs/overview/项目结构说明.md](docs/overview/项目结构说明.md) | 目录结构详解 | 新手开发者 |

### 🔧 接口规范 (11 份核心文档)

| 文档 | 内容 |
|------|------|
| [docs/specs/models/Entry数据模型规范.md](docs/specs/models/Entry数据模型规范.md) | 知识条目数据结构 |
| [docs/specs/interfaces/Processors接口规范.md](docs/specs/interfaces/Processors接口规范.md) | 内容处理器接口 |
| [docs/specs/interfaces/Storage接口规范.md](docs/specs/interfaces/Storage接口规范.md) | 三层存储架构 |
| [docs/specs/interfaces/Retrieval检索引擎规范.md](docs/specs/interfaces/Retrieval检索引擎规范.md) | 检索策略设计 |
| [docs/specs/interfaces/WorkflowEngine接口规范.md](docs/specs/interfaces/WorkflowEngine接口规范.md) | 工作流引擎接口 |
| [查看完整列表](docs/README.md) | 当前文档总索引 |

### 🎯 里程碑报告

| 里程碑 | 状态 | 文档 |
|--------|------|------|
| M1: 基础设施层 | ✅ 完成 | [MILESTONE1_COMPLETE.md](docs/history/milestones/MILESTONE1_COMPLETE.md) |
| M2: AI 服务层 | ✅ 完成 | [MILESTONE2_COMPLETE.md](docs/history/milestones/MILESTONE2_COMPLETE.md) |
| M3: 内容处理器 | ✅ 完成 | [MILESTONE3_COMPLETE.md](docs/history/milestones/MILESTONE3_COMPLETE.md) |
| M3.5: AI 对话处理器 | ✅ 完成 | [MILESTONE3_5_COMPLETE.md](docs/history/milestones/MILESTONE3_5_COMPLETE.md) |
| M4: 检索引擎 | ✅ 完成 | [M4_COMPLETION_REPORT.md](docs/history/milestones/M4_COMPLETION_REPORT.md) |
| M5: 工作流引擎 | ✅ 完成 | [M5_COMPLETION_SUMMARY.md](docs/history/milestones/M5_COMPLETION_SUMMARY.md) |
| M5.1: Bug 修复 | ✅ 完成 | [M5_1_BUGFIX_COMPLETE.md](docs/history/milestones/M5_1_BUGFIX_COMPLETE.md) |
| M6+M7: CLI 与文档 | ✅ 完成 | 参考 [CHANGELOG.md](docs/operations/CHANGELOG.md) |
| M8: MCP 只读服务 | ✅ 完成 | 5 Tool + 4 Resource (v0.7.0-alpha) |
| M9: MCP 写入+安全 | ✅ 完成 | 3 写入 Tool + 3 Prompt + 安全加固 (v0.7.0) |

### 🛠️ 运维指南 (v0.6.0+)

| 文档 | 内容 |
|------|------|
| [docs/operations/testing/测试环境演示.md](docs/operations/testing/测试环境演示.md) | 完整环境演示案例 |
| [docs/operations/数据库迁移指南.md](docs/operations/数据库迁移指南.md) | 数据库升级与迁移 |
| [docs/operations/testing/测试环境隔离指南.md](docs/operations/testing/测试环境隔离指南.md) | 测试环境管理 |
| [scripts/README.md](scripts/README.md) | 运维脚本使用说明 |

### 📊 项目管理

| 文档 | 内容 |
|------|------|
| [docs/history/prompts/PHASE1_DEV_PROMPT.md](docs/history/prompts/PHASE1_DEV_PROMPT.md) | Phase 1 开发计划（已归档） |
| [docs/operations/CHANGELOG.md](docs/operations/CHANGELOG.md) | 更新日志 |
| [docs/README.md](docs/README.md) | 当前文档总索引 |
| [docs/history/reviews/Bug修复记录.md](docs/history/reviews/Bug修复记录.md) | 问题跟踪 |

## 💡 设计亮点

### 🏗️ 三层存储架构

**双重存储 + 完全可重建**:

| 层级 | 技术栈 | 职责 | 特点 |
|------|--------|------|------|
| **主存储** | Markdown + YAML Front Matter | 数据主权、人类可读 | Git 友好、可编辑 |
| **元数据索引** | SQLite FTS5 + jieba 分词 | 全文检索、关系查询 | 精确关键词匹配 |
| **向量索引** | hnswlib (HNSW 算法) | 语义检索 | 高效近似最近邻 |

**核心优势**:
- 所有数据可从 Markdown 完全重建
- SQLite + 向量仅为缓存，可随时删除重建
- 数据主权完全掌控，避免厂商锁定

**Embedding 约束**:
- Embedding 模型和维度是向量索引契约；更换模型或维度后，旧向量不再可信。
- 如需切换模型，应作为索引迁移处理：重建向量索引，并重新生成文档级和分块级 Embedding。
- 如果只是临时试用本地 OpenAI-compatible 服务，优先保持与现有索引一致的模型和维度。

### 🧠 智能检索路由

`QueryRouter` 自动根据查询特征选择最优策略:

| 查询类型 | 策略 | 触发条件 | 优势 |
|---------|------|----------|------|
| 短查询 | **BM25** | tokens < 5 | 精确快速，不构造 Provider |
| 长查询 | **Hybrid (加权 RRF k=60)** | tokens ≥ 5 | BM25 + 向量并行，保留分支降级状态 |
| 向量模式 | **Vector** | CLI/MCP 显式选择 | 按需创建 Embedding Provider |

**成本边界**: BM25 与被参数校验拒绝的路径不会创建或调用 Embedding Provider；向量/混合路径需要有效的 Provider 配置。

### ⚡ 工作流驱动架构

归档由严格版本化工作流编排；搜索直接由 Retrieval 层及 adapter 执行，不存在 `search.yaml`:

```yaml
# config/workflows/archive-url.yaml
schema_version: 1
name: archive-url
description: "智能归档网页内容工作流"
steps:
  - id: fetch_content
    type: fetch_content
    config: {processor: auto, url_key: url, timeout: 30, retry: 3}
    on_error: fail
  - id: ai_analyze
    type: ai_analyze
    config: {tasks: [summarize, extract_tags], max_words: 300, num_tags: 5}
    on_error: continue
  - id: idea_sharpen
    type: idea_sharpen
    config:
      questions: ["这篇内容与你现有知识中的哪些观点有关？"]
      trigger_rules: [{content_length_gt: 3000}]
    on_error: continue
  - id: review_entry
    type: review_entry
    config: {required: true, max_regenerations: 3, preview_chars: 500}
    on_error: continue
  - id: store_entry
    type: store_entry
    config: {targets: [markdown, sqlite, vector_index]}
    on_error: fail
```

**优势**:
- 可编排：YAML 定义，灵活配置
- 可观测：公开 `success/degraded/error`、稳定 issues 与日志
- 错误策略：每步显式声明 `on_error: fail|continue`，配置在副作用前 fail-closed

### 🛡️ AI 安全与测试隔离 (v0.6.1)

**内置安全规范** ([.ai-safety-rules.md](.ai-safety-rules.md)):
- 禁止 AI 执行的危险操作清单
- 用户确认机制（删除、提交、推送）
- 安全测试脚本（无真实 API 调用）

**测试环境完全隔离**:
- 用户数据默认根：`%USERPROFILE%\.pkv\data`（`PKV_DATA_ROOT` 覆盖 `config.yaml` 的 `storage.data_root`）
- 测试环境：`.data-test/`
- 自动备份：`scripts/backup-data.ps1`

### 🔄 数据库增量迁移 (v0.6.1)

**版本化 Schema 管理**:

遗留 `scripts/migrate.py` 当前不是用户维护 API：除 `--help` 外，裸脚本调用会在读取
`Config` 或数据库前 fail-closed（exit 2）；`run-test.ps1` 也会明确拒绝该入口。它不能
用于升级、dry-run、版本查询或健康检查。自动化只运行使用临时 SQLite 的迁移单元/集成
测试。真实快照迁移仍须等待未来的 user-only lifecycle 入口、FT5、U1/G8 和用户明确授权，
并且只能在 disposable writable clone 中执行。

**特点**:
- 版本号追踪（存储在 `schema_version` 表）
- 增量 SQL 脚本（`scripts/migrations/*.sql`）
- 迁移链健康检查（脚本头、版本递增、表结构与版本记录漂移）
- 向前/向后兼容

## 📊 项目状态

### 🎉 当前仓库基线: 0.8.1

**发布状态**: 🧪 拆分前的 M13 W1/W2/W3/W4 Artifact 证据已归档；当前 headless payload 仍是合规 hold 的 test candidate，尚未具备正式发布资格，真实数据验收也未执行。

| 指标 | 数值 | 说明 |
|------|------|------|
| **项目版本** | 0.8.1 | 当前运行时、CLI 和 held test candidate 基线 |
| **稳定能力基线** | v0.7.0 | MCP 服务 (8 Tool + 4 Resource + 3 Prompt) |
| **开发进度** | M13 W4（hold） | 历史 W4 10/11 Artifact matrix 已归档；当前 3 项合规 blocker 使 headless candidate 保持 hold |
| **源代码文件** | 动态维护 | 以仓库树和模块索引为准 |
| **测试文件** | 动态维护 | 精确测试 lane 与冻结证据见 `tests/CLAUDE.md` |
| **测试覆盖率** | 门禁化维护 | 不在 README 固化易漂移的静态百分比 |
| **MCP 测试** | 多层覆盖 | 相关用例分布在 unit / integration / blackbox / e2e |
| **文档数量** | 动态维护 | 文档索引与模块入口见 `CLAUDE.md` / `AGENTS.md` |

### ✅ 里程碑完成情况

| 里程碑 | 状态 | 内容 | 完成时间 |
|--------|------|------|----------|
| **M1** | ✅ | 基础设施层（存储、配置、向量、FTS5） | 2026-02-09 |
| **M2** | ✅ | AI 服务层（DeepSeek、OpenAI、Embedding） | 2026-02-10 |
| **M3** | ✅ | 内容处理器（微信、知乎、通用网页、聊天） | 2026-02-11 |
| **M3.5** | ✅ | AI 对话处理器 + 文本 Fallback | 2026-02-11 |
| **M4** | ✅ | 检索引擎（BM25、向量、混合、路由） | 2026-02-12 |
| **M5** | ✅ | 工作流引擎（编排、步骤、上下文） | 2026-02-13 |
| **M5.1** | ✅ | Bug 修复（配置字段、引擎传参） | 2026-02-14 |
| **M6+M7** | ✅ | CLI 入口 + 文档完善 (v0.6.0) | 2026-02-15 |
| **v0.6.1** | ✅ | AI 安全 + 数据库迁移 + 文档整理 | 2026-02-16 |
| **M8** | ✅ | MCP 只读服务（5 Tool + 4 Resource） | 2026-02-18 |
| **M9** | ✅ | MCP 写入 + Prompts + 安全加固 (v0.7.0) | 2026-02-19 |
| **M13 W1** | ✅ | Runtime layout、Vault containment、跨存储终态与 fail-closed migration | 2026-08-02 |
| **M13 W2** | ✅ | Workflow、Retrieval、MCP 与当时 GUI Chat 源代码功能合同与独立复审；GUI 后续已拆出 | 2026-08-07 |
| **M13 W3** | ✅ | 可复现 Windows test-candidate 打包链与 payload 外 loopback harness | 2026-08-11 |
| **M13 W4** | 🟡 | 拆分前 installed-Artifact 10 scenario / 11 行 matrix 通过；当前 headless candidate 仍需独立 Artifact 证据 | 2026-08-11 |
| **P1-A** | ✅ | 共享 `KnowledgeApplication` / Kernel 边界与 INTERNAL TEST ONLY headless onedir/ZIP；仓库外合成 smoke 通过 | 2026-08-13 |

### 🚀 核心能力矩阵

| 能力 | 状态 | 说明 |
|------|------|------|
| **内容归档** | 🧪 已实现（alpha） | 6 种处理器；真实数据验收尚未执行 |
| **智能检索** | 🧪 已实现（alpha） | BM25/向量/混合，自动路由 |
| **AI 分析** | 🧪 已实现（alpha） | 三层摘要、标签提取、成本优化 |
| **CLI 交互** | 🧪 已实现（alpha） | 9 个公开命令，Rich Console 界面 |
| **工作流引擎** | 🧪 已实现（alpha） | YAML 配置，可编排/可观测 |
| **数据迁移** | 🧪 W1 安全合同通过 | fresh/off-path 初始化与升级拒绝已验证；历史原地升级仍不在 M13 默认范围 |
| **AI 安全** | 🧪 CAT-0 已验证 | 离线入口与测试隔离；不等同于 OS sandbox |
| **MCP 服务** | 🧪 Artifact 路径已验证（hold） | 固定评测 16/16、151/151；stdio-only，W4 candidate 已验，真实快照未验收 |

### 📈 技术债务与改进方向

**当前优先级**:

1. **可追溯知识成果**：K1b payload 闭合与重验完成后，以现有检索、关系和证据能力形成可审阅、可回链的成果工作流；再按真实需求逐 Tool 扩展 full 语义。
2. **M13 release hold**：许可证、native provenance 与 notices 仍是未来重开发布时的 blocker；P1-A 的内部自测不改变该结论。
3. **真实数据与后置增强**：真实数据验收仍需用户授权与 U1/G8、FT5 等独立前置；POSIX/CI、性能、多模态和重交互体验不阻塞当前路线。

## 🧪 测试体系

### 测试布局

说明：

- 文件数量和覆盖率不在 README 固化；以 [tests/CLAUDE.md](tests/CLAUDE.md)
  中对应冻结工作树的门禁与证据为准。

| 测试类型 | 覆盖范围 | 运行方式 |
|---------|----------|----------|
| **单元测试** | 核心业务逻辑、MCP、处理器、存储层 | run-test.ps1 -Direct ... pytest tests/unit/ |
| **集成测试** | 跨模块集成、MCP 功能、工作流、审核链路 | run-test.ps1 -Direct ... pytest tests/integration/ |
| **E2E 测试** | 默认离线端到端与 MCP 服务；network/manual 排除 | run-test.ps1 -Direct ... pytest tests/e2e/ |
| **黑盒测试** | CLI + MCP stdio 协议 | run-test.ps1 -Direct ... pytest tests/blackbox/ |
| **手动验证** | 核心迁移/恢复等明确授权的人工验证 | 用户手动、非默认自动化；按文件说明执行 |

**MCP 多层测试结构**:

| 层级 | 当前文件 | 说明 | 验证方式 |
|------|----------|------|----------|
| Layer 1 | `tests/unit/test_mcp_tools.py` / `test_mcp_resources.py` / `test_mcp_prompts.py` / `test_mcp_security.py` | handler 级单元测试 | 直接调用函数 + mock 隔离 |
| Layer 2 | `tests/integration/test_mcp_functional.py` | FastMCP 进程内功能验证 | `call_tool` / `get_prompt` / `read_resource` |
| Layer 3 | `tests/blackbox/test_mcp_blackbox.py` + `tests/e2e/test_mcp_e2e_*.py` | stdio 子进程与端到端验证 | 真实协议 / 子进程 / E2E 工作流 |

**快速测试**:

```powershell
# CLI 帮助也经受控入口，使用独立场景 DataRoot
.\scripts\run-test.ps1 -DataRoot .data-test\readme-help -Command @("--help")

# 运行所有单元测试；pytest 为非 CLI 命令，使用 -Direct 与显式 -Command 数组
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\readme-unit -Command @("pytest", "tests/unit/", "-v")

# 运行特定模块测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\readme-unit-filter -Command @("pytest", "tests/unit/", "-k", "processors", "-v")

# 代码覆盖率
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\readme-coverage -Command @("pytest", "tests/unit/", "--cov=src", "--cov-report=term-missing")

# 验证环境；Python 脚本同样使用 -Direct 与显式 -Command 数组
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\readme-verify -Command @("python", "src/utils/verify_setup.py")
```

`-Direct` Python 当前经 FT7 显式离线测试 allowlist 执行，不是通用仓库脚本运行器：只允许
pytest、`src.cli.commands`、`src.mcp.server`、`src.utils.verify_setup`、`evals.mcp_quality`，以及
`scripts/setup-test-db.py`、`scripts/rebuild-dev-vault.py`、受控
`scripts/check_chunk_index_consistency.py` 和 `src/utils/verify_setup.py`；完整参数与路径规则见
[scripts/CLAUDE.md](scripts/CLAUDE.md)。其余目标、`-c`、stdin 与解释器 flags 会 fail-closed。入口清理
live/secret/proxy 环境并安装 Python 级网络与子进程 guard，但它不是 OS sandbox，
也不覆盖非 Python 的 `-Direct` 命令。

**Phase C MCP 最小离线评测**:

```powershell
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\mcp-quality -Command @(
  "python", "-m", "evals.mcp_quality",
  "--enforce-thresholds",
  "--output", ".data-test/mcp-quality/result.json"
)
```

当前 fresh run 为 16/16 tasks、151/151 checks（119 项版本化声明式检查 +
32 项自动公开 envelope 检查）、`overall=1.0`、全部维度 `1.0`、0 failed 且
`targets_met=true`。2026-07-31 的 119/119 是历史声明式基线，未被降低或删除。固定任务、评分维度与
隔离边界见
[MCP 最小评测闭环](docs/operations/MCP最小评测闭环.md)。

开发 vault 重建入口 `scripts/rebuild-dev-vault.py` 已完成合成演练：
`rebuilt -> up_to_date -> checked`，目标 schema `1.2.6`、11 个迁移、3 条 seed。
这只证明离线合成开发基线可重建；真实快照仍受 U1/G8 与迁移 FT5 阻塞，
本轮未读取或执行真实数据。M13 W1/W2/W3 与 W4 installed-Artifact 功能验证均在该离线基线上完成；W4 候选仍受合规 hold，且这不等于真实数据验收。

详细说明：[tests/CLAUDE.md](tests/CLAUDE.md)

## 🤝 贡献指南

### 编码规范

- **类型提示**: 所有函数必须有完整的类型注解
- **文档字符串**: 公开 API 必须有 docstring（Google 风格）
- **错误处理**: 优雅降级，禁止裸 `except:`
- **命名规范**:
  - 数据库列名使用领域专用名称（`knowledge_id` 而非 `id`）
  - 文件名使用蛇形命名法 `snake_case`
  - 类名使用大驼峰 `PascalCase`

### 贡献流程

本项目目前处于个人开发阶段，暂不接受外部代码贡献。

**欢迎的贡献方式**:
- 📝 提交 Issue 反馈问题或建议
- 📖 完善文档或修正错误
- 🌟 Star 项目支持开发

如有问题或建议，请在 [GitHub Issues](https://github.com/OceanEyeFF/personal-knowledge-vault/issues) 中提出。

## 📄 开源协议

本项目采用 [MIT License](LICENSE) 开源协议。

## 🙏 致谢

**核心技术栈**:
- [DeepSeek](https://www.deepseek.com/) - 低成本 AI 摘要生成
- [OpenAI](https://openai.com/) - Embedding 模型
- [FastMCP](https://github.com/jlowin/fastmcp) - MCP 服务框架 (Model Context Protocol)
- [hnswlib](https://github.com/nmslib/hnswlib) - 高效向量检索
- [jieba](https://github.com/fxsjy/jieba) - 中文分词
- [Rich](https://github.com/Textualize/rich) - 终端界面

**灵感来源**:
- [obsidian-clipper](https://github.com/jplattel/obsidian-clipper) - 内容剪藏
- [Logseq](https://logseq.com/) - 知识图谱
- [Fabric](https://github.com/danielmiessler/fabric) - AI 工作流

---

<div align="center">

**项目代号**: Personal Knowledge Vault
**当前版本**: 0.8.1
**创建日期**: 2026-01-27
**文档版本**: v4.3
**最后更新**: 2026-08-21

---

**✨ 工作流驱动的 AI-First 知识管理系统 ✨**

[📖 开始使用](RUN_ME_FIRST.md) · [📚 查看文档](docs/) · [🐛 反馈问题](https://github.com/OceanEyeFF/personal-knowledge-vault/issues) · [⭐ Star 支持](https://github.com/OceanEyeFF/personal-knowledge-vault)

</div>
