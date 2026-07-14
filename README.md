# Personal Knowledge Vault

> **AI-First Knowledge Workflow System**
> 工作流驱动的个人知识管理系统

[![Version](https://img.shields.io/badge/version-0.8.0--alpha-blue.svg)](./docs/operations/CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.11+-green.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-orange.svg)](./LICENSE)
[![Status](https://img.shields.io/badge/status-production_ready-brightgreen.svg)](./docs/history/milestones/)

> 当前仓库基线：`v0.8.0-alpha`
> 说明：`v0.6.0` 是 CLI 首次稳定引入版本，`v0.7.0` 是 MCP 首次稳定引入版本；当前仓库在此基础上继续合入后续 GUI 与文档收敛工作。
> 命名说明：当前路线里提到的“当前开发 Phase 1”实际对应 `Phase A：Relation Foundation`；历史文档中的旧 `Phase 1` 已归档完成，两者不是同一时间轴。

## ✨ 核心特点

- 🤖 **AI-First 设计**: 以 Claude Code/CodeX 作为智能协作伙伴，支持人机协作的知识处理
- 🔌 **MCP 服务**: 标准 MCP 协议集成，AI Agent 可直接搜索、归档、浏览知识库
- 🔍 **智能混合检索**: 自动路由 BM25/向量/混合检索策略，精确高效零成本浪费
- ⚡ **工作流驱动**: 一切操作皆工作流，可编排、可观测、可中断恢复
- 🔒 **本地优先**: 数据完全掌控，Markdown 主存储，SQLite+向量辅助索引
- 💰 **成本可控**: 智能策略节省 85% API 成本，支持自定义成本阈值
- 🛡️ **AI 安全**: 内置安全规范，测试环境完全隔离，数据备份自动化

## 🎯 核心功能

### 📥 内容归档 (已实现)
- 📱 **微信文章**: 保留格式，自动提取正文和元数据
- 💬 **知乎内容**: 问题、回答、专栏，支持评论区归档
- 🌐 **通用网页**: 智能正文提取，去除广告和无关内容
- 💬 **聊天记录**: 微信聊天导出（邮件格式、文本格式）
- 🤖 **AI 对话**: ChatGPT/DeepSeek 对话历史（HTML/Markdown 格式）
- 📝 **纯文本**: Fallback 处理器，任何文本内容都能归档

### 🔍 智能检索 (已实现)
- **BM25 检索**: 短查询（<10 tokens）精确关键词匹配
- **向量检索**: 长查询（≥10 tokens）语义理解
- **混合检索**: RRF (k=60) 融合算法，兼顾精确与语义
- **自动路由**: `QueryRouter` 根据查询特征自动选择最优策略
- **中文优化**: jieba 分词 + 自定义词典支持

### 🤖 AI 能力 (已实现)
- **智能摘要**: 三层摘要（一句话/100字/详细版）
- **标签提取**: 自动生成标签和关键词
- **内容分析**: 识别内容类型、评估字数复杂度
- **Embedding**: 默认 OpenAI text-embedding-3-small；模型/维度与向量索引绑定，不建议随意更换
- **成本控制**: DeepSeek API 低成本方案

### 💻 CLI 命令 (v0.6.0 新增)
```bash
# 归档 URL
pkv archive https://mp.weixin.qq.com/xxx

# 智能搜索
pkv search "关键词"

# 查看详情
pkv show <knowledge_id>

# 列表浏览
pkv list --limit 20

# 系统配置
pkv config

# 统计信息
pkv stats
```

### 🔌 MCP 服务 (v0.7.0 新增)

AI Agent（Claude Code、Cursor 等）通过 MCP 协议直接操作知识库：

```bash
# 启动 MCP Server（stdio 模式，供 Claude Code/Cursor 集成）
python -m src.mcp

# 启动 HTTP 模式（远程访问）
python -m src.mcp.server --transport streamable-http --port 3000
```

**14 个 Tool**:
- `search_knowledge` — 智能搜索（BM25/向量/混合）
- `get_entry` / `list_entries` / `list_tags` / `get_stats` — 只读浏览
- `archive_url` — 归档网页（SSRF 防护）
- `archive_text` — 归档纯文本
- `get_related` — 关联知识推荐（向量相似度）
- `query_subgraph` — 关系子图查询
- `explain_relation` — 关系解释
- `collect_evidence` — 证据包聚合
- `find_bridges` — 桥接候选发现（partial）
- `timeline_of` — 弱时间线重建（partial）
- `contrast` — 主题对比（partial）

**3 个 Prompt 模板**: `search_and_summarize` / `knowledge_qa` / `idea_sharpen`

**4 个 Resource**: 条目全文、元数据、标签列表、统计信息

**安全加固**: SSRF 拦截（内网地址过滤）、文本长度限制、Bearer Token 认证

### 🕸️ 关系层基础 (Phase A / T1+T5 已实现)

- 已新增 `src/relations/models.py`，定义低歧义关系的类型、方向、来源和查询结果结构
- 已新增 `src/storage/relation_store.py`，提供 `knowledge_relations` 的最小读写能力
- 已新增 `scripts/migrations/006_add_relations_foundation.sql`，显式引入关系表与索引
- 已新增 `src/relations/extractors.py`，当前支持 Markdown 显式链接、Front Matter `related_docs`，以及 Front Matter 关系字段 `children` / `version_of` 的低歧义关系抽取
- 已新增 `scripts/backfill_relations.py`，默认 `dry-run`，仅在显式传入 `--apply` 时写入关系表
- 已扩展 `src/relations/query_service.py`，当前提供一跳关系查询、内部 `query_subgraph` 多跳子图遍历基础、最小 `explain_relation` 能力、关系类型分组和稳定排序能力
- 已新增 `src/relations/evidence_service.py`，当前提供文档级 `collect_evidence` 证据包聚合 v1
- 已新增 `src/relations/exploration_service.py`，当前提供 `find_bridges`、`timeline_of`、`contrast` 的受限版本
- 当前已把 `query_subgraph`、`explain_relation` 与 `collect_evidence` 暴露为只读 MCP Tool；其中 `collect_evidence` 默认保持文档级证据包兼容行为，显式传入 `include_chunks=true` 时可返回 chunk 级证据字段，并执行近重复去重与多因子排序
- 当前已把 `find_bridges`、`timeline_of`、`contrast` 暴露为只读 MCP Tool，但它们仍是 partial implementation：`find_bridges` 当前结合显式关系子图、局部图桥接信号与轻量文本重合；`timeline_of` 当前优先使用 `event_time > published_at > archived_at` 的结构化真实时间字段；`contrast` 当前在稳定对比维度之外补入跨主题显式关系路径信号与候选级 `relation_signal_score` / `relation_types`

## 📂 项目结构

```
personal-knowledge-vault/
├── README.md                          # 项目说明
├── CLAUDE.md                          # AI 协作上下文（根级索引）
├── RUN_ME_FIRST.md                    # 快速开始指南
├── .env.example                       # 环境变量模板
├── requirements.txt                   # Python 依赖
│
├── src/                               # 源代码 (40+ 个文件)
│   ├── main.py                        # CLI 入口
│   ├── cli/                           # CLI 系统 (v0.6.0)
│   │   ├── commands.py                # 6 个核心命令
│   │   ├── ui.py                      # Rich Console 界面
│   │   └── formatters.py              # 输出格式化
│   ├── mcp/                           # MCP 服务 (v0.7.0)
│   │   ├── server.py                  # FastMCP 主入口 (stdio/HTTP)
│   │   ├── tools.py                   # 14 个 Tool handler
│   │   ├── resources.py               # 4 个 Resource handler
│   │   ├── prompts.py                 # 3 个 Prompt 模板
│   │   └── utils.py                   # 安全验证 + 序列化
│   ├── processors/                    # 内容处理器 (7 个)
│   │   ├── wechat_processor.py        # 微信文章
│   │   ├── zhihu_processor.py         # 知乎内容
│   │   ├── chat_processor.py          # 聊天记录
│   │   ├── ai_chat_processor.py       # AI 对话
│   │   └── text_fallback_processor.py # 纯文本
│   ├── relations/                     # 关系模型与类型定义 (Phase A)
│   │   ├── models.py                  # 关系记录 / 查询结果模型
│   │   ├── extractors.py              # 低歧义关系抽取与回填服务
│   │   └── query_service.py           # 一跳关系查询与分组服务
│   ├── storage/                       # 三层存储
│   │   ├── markdown_store.py          # Markdown 主存储
│   │   ├── sqlite_store.py            # SQLite 元数据索引
│   │   ├── vector_store.py            # hnswlib 向量索引
│   │   ├── relation_store.py          # knowledge_relations 存储
│   │   └── migration_manager.py       # 数据库迁移与健康检查
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
├── config/                            # 配置文件
│   ├── config.yaml                    # 主配置
│   ├── workflows/                     # 工作流定义
│   │   ├── archive-url.yaml           # URL 归档工作流
│   │   ├── archive-text.yaml          # 文本归档工作流 (v0.7.0)
│   │   └── search.yaml                # 搜索工作流
│   └── custom_dict.txt                # 自定义分词词典
│
├── scripts/                           # 运维脚本 (v0.6.0)
│   ├── setup-conda.ps1                # Conda 环境安装
│   ├── test-conda.ps1                 # 环境验证
│   ├── backup-data.ps1                # 数据备份
│   ├── restore-data.ps1               # 数据恢复
│   ├── backfill_relations.py          # 关系回填脚本（默认 dry-run）
│   ├── migrate.py                     # 数据库迁移工具
│   └── migrations/                    # SQL 迁移脚本
│
├── tests/                             # 测试套件（2026-03-06 仓库快照：96 个文件）
│   ├── unit/                          # 单元测试（40 个文件）
│   ├── integration/                   # 集成测试（8 个文件）
│   ├── e2e/                           # E2E 测试（6 个文件）
│   ├── blackbox/                      # 黑盒测试（5 个文件）
│   ├── fixtures/                      # 测试数据
│   ├── manual_test_m12/               # M12 专项手动验证（7 个文件）
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
└── .data/                             # 运行时数据（已忽略）
    ├── db/knowledge_vault.db          # SQLite 数据库
    ├── vectors/*.idx                  # 向量索引
    ├── vault/                         # Markdown 文件存储
    └── logs/                          # 日志文件
```

## 🚀 快速开始

### 环境要求

**推荐配置** (避免兼容性问题):
- **Conda** (Miniconda 或 Anaconda) 🌟 [推荐]
- Python 3.11+ (通过 Conda 自动创建)
- SQLite 3.35+ (支持 FTS5，通常系统自带)

**必需 API Keys**:
- **DeepSeek API** (摘要生成、标签提取) - [获取 API Key](https://platform.deepseek.com/)
- **OpenAI API** (Embedding) - [获取 API Key](https://platform.openai.com/)

### Codex/Claude 专用环境

为 AI 协作者准备的隔离环境（推荐）：

```bash
conda create -y -n pkv-py311-codex python=3.11
conda install -y -n pkv-py311-codex -c conda-forge hnswlib=0.8.0
conda run -n pkv-py311-codex python -m pip install -r requirements.txt
conda activate pkv-py311-codex
```

### ⚡ 3 步安装 (推荐：Conda 方式)

```powershell
# 1️⃣ 克隆仓库
git clone https://github.com/yourusername/personal-knowledge-vault.git
cd personal-knowledge-vault

# 2️⃣ 运行自动安装脚本（创建 Python 3.11 环境 + 安装依赖）
.\scripts\setup-conda.ps1

# 3️⃣ 配置 API Keys
cp .env.example .env
notepad .env  # 填入你的 PKV_LLM_API_KEY 和 PKV_EMBD_API_KEY

# ✅ 验证安装
.\scripts\test-conda.ps1

# 🎉 开始使用
conda activate pkv-py311
python src/main.py --help
```

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
| 短查询 | **BM25** | tokens < 10 | 精确快速，零 API 成本 |
| 长查询 | **向量检索** | tokens ≥ 10 | 语义理解强 |
| 混合模式 | **Hybrid (RRF k=60)** | 需要精确+语义 | 兼顾精确与语义 |

**成本优化**: 智能路由比纯向量方案节省 **85% API 成本**

### ⚡ 工作流驱动架构

**一切操作皆工作流**:

```yaml
# config/workflows/archive-url.yaml
name: archive-url
steps:
  - id: fetch          # 抓取内容
    type: fetch_content
  - id: analyze        # AI 分析（摘要、标签）
    type: ai_analyze
  - id: store          # 三层存储
    type: store_entry
```

**优势**:
- 可编排：YAML 定义，灵活配置
- 可观测：每步进度追踪，日志记录
- 可中断：支持失败重试、断点续跑

### 🛡️ AI 安全与测试隔离 (v0.6.1)

**内置安全规范** ([.ai-safety-rules.md](.ai-safety-rules.md)):
- 禁止 AI 执行的危险操作清单
- 用户确认机制（删除、提交、推送）
- 安全测试脚本（无真实 API 调用）

**测试环境完全隔离**:
- 生产环境：`.data/`
- 测试环境：`.data-test/`
- 自动备份：`scripts/backup-data.ps1`

### 🔄 数据库增量迁移 (v0.6.1)

**版本化 Schema 管理**:

```bash
# 迁移工具
python scripts/migrate.py                # 交互式升级
python scripts/migrate.py --auto         # 自动升级
python scripts/migrate.py --dry-run      # 仅检查待执行迁移
python scripts/migrate.py --version      # 查看当前数据库版本
python scripts/migrate.py --health-check # 只读检查迁移链健康度
```

**特点**:
- 版本号追踪（存储在 `schema_version` 表）
- 增量 SQL 脚本（`scripts/migrations/*.sql`）
- 迁移链健康检查（脚本头、版本递增、表结构与版本记录漂移）
- 向前/向后兼容

## 📊 项目状态

### 🎉 当前仓库基线: v0.8.0-alpha

**发布状态**: ✅ M1-M9 稳定能力已完成，仓库当前基线继续包含后续 GUI / 聊天相关工件与文档收敛结果

| 指标 | 数值 | 说明 |
|------|------|------|
| **项目版本** | v0.8.0-alpha | 当前仓库基线 |
| **稳定能力基线** | v0.7.0 | MCP 服务 (8 Tool + 4 Resource + 3 Prompt) |
| **开发进度** | M1-M9 | Phase 1 + Phase 2A 完成 |
| **源代码文件** | 40+ 个 | Python 模块 |
| **测试文件** | 96 个 | 2026-03-06 仓库快照（tracked files） |
| **测试覆盖率** | 待重新统计 | README 不再保留未经重新验证的旧覆盖率数字 |
| **MCP 测试** | 多层覆盖 | 相关用例分布在 unit / integration / blackbox / e2e |
| **文档数量** | 60+ | Markdown 文档 |

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

### 🚀 核心能力矩阵

| 能力 | 状态 | 说明 |
|------|------|------|
| **内容归档** | ✅ 生产就绪 | 6 种处理器（微信/知乎/网页/聊天/AI 对话/文本） |
| **智能检索** | ✅ 生产就绪 | BM25/向量/混合，自动路由 |
| **AI 分析** | ✅ 生产就绪 | 三层摘要、标签提取、成本优化 |
| **CLI 交互** | ✅ 生产就绪 | 6 个核心命令，Rich Console 界面 |
| **工作流引擎** | ✅ 生产就绪 | YAML 配置，可编排/可观测 |
| **数据迁移** | ✅ 生产就绪 | 版本化 Schema，增量迁移 |
| **AI 安全** | ✅ 生产就绪 | 安全规范、测试隔离、自动备份 |
| **MCP 服务** | ✅ 生产就绪 | 8 Tool + 4 Resource + 3 Prompt，SSRF 防护 |

### 📈 技术债务与改进方向

**当前优先级**:

1. **性能优化** (中优先级)
   - 向量索引批量更新
   - SQLite 查询优化
   - 长文档分块策略

2. **功能增强** (中优先级)
   - RAG 问答能力
   - B站视频处理器
   - PDF 书籍处理器

3. **用户体验** (低优先级)
   - 交互式配置向导
   - 更丰富的终端 UI
   - Web 界面（计划中）

## 🧪 测试体系

### 测试资产快照（2026-03-06）

说明：

- 下表使用仓库文件快照统计（基于 tracked files），不是测试用例数
- 覆盖率数字需要重新执行覆盖率统计后再更新

| 测试类型 | 文件数 | 覆盖范围 | 运行方式 |
|---------|--------|----------|----------|
| **单元测试** | 40 个 | 核心业务逻辑、GUI、MCP、处理器、存储层 | `pytest tests/unit/ -v` |
| **集成测试** | 8 个 | 跨模块集成、MCP 功能、工作流、审核链路 | `pytest tests/integration/ -v` |
| **E2E 测试** | 6 个 | 端到端流程、MCP 服务与真实工作流验证 | `pytest tests/e2e/ -v` |
| **黑盒测试** | 5 个 | CLI + MCP stdio 协议 | `pytest tests/blackbox/ -v` |
| **根目录测试/辅助文件** | 9 个 | 基础语法、手动验证脚本、测试说明 | 按文件说明执行 |
| **M12 专项手动验证** | 7 个 | qasync、流式输出、线程/异步集成 | `python tests/manual_test_m12/<file>.py` |

**MCP 多层测试结构**:

| 层级 | 当前文件 | 说明 | 验证方式 |
|------|----------|------|----------|
| Layer 1 | `tests/unit/test_mcp_tools.py` / `test_mcp_resources.py` / `test_mcp_prompts.py` / `test_mcp_security.py` | handler 级单元测试 | 直接调用函数 + mock 隔离 |
| Layer 2 | `tests/integration/test_mcp_functional.py` | FastMCP 进程内功能验证 | `call_tool` / `get_prompt` / `read_resource` |
| Layer 3 | `tests/blackbox/test_mcp_blackbox.py` + `tests/e2e/test_mcp_e2e_*.py` | stdio 子进程与端到端验证 | 真实协议 / 子进程 / E2E 工作流 |

**快速测试**:

```bash
# 激活环境
conda activate pkv-py311

# 运行所有单元测试
pytest tests/unit/ -v

# 运行特定模块测试
pytest tests/unit/test_processors_*.py -v

# 代码覆盖率
pytest tests/unit/ --cov=src --cov-report=term-missing

# 验证环境
python src/utils/verify_setup.py
```

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

如有问题或建议，请在 [GitHub Issues](https://github.com/yourusername/personal-knowledge-vault/issues) 中提出。

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
**当前版本**: v0.8.0-alpha
**创建日期**: 2026-01-27
**文档版本**: v4.0
**最后更新**: 2026-03-06

---

**✨ 工作流驱动的 AI-First 知识管理系统 ✨**

[📖 开始使用](RUN_ME_FIRST.md) · [📚 查看文档](docs/) · [🐛 反馈问题](https://github.com/yourusername/personal-knowledge-vault/issues) · [⭐ Star 支持](https://github.com/yourusername/personal-knowledge-vault)

</div>
