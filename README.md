# Personal Knowledge Vault

> **AI-First Knowledge Workflow System**
> 工作流驱动的个人知识管理系统

[![Version](https://img.shields.io/badge/version-0.6.1-blue.svg)](./docs/CHANGELOG.md)
[![Python](https://img.shields.io/badge/python-3.11+-green.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-orange.svg)](./LICENSE)
[![Status](https://img.shields.io/badge/status-production_ready-brightgreen.svg)](./docs/milestones/)

## ✨ 核心特点

- 🤖 **AI-First 设计**: 以 Claude Code/CodeX 作为智能协作伙伴，支持人机协作的知识处理
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
- **Embedding**: OpenAI text-embedding-3-small 模型
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

## 📂 项目结构

```
personal-knowledge-vault/
├── README.md                          # 项目说明
├── CLAUDE.md                          # AI 协作上下文（根级索引）
├── RUN_ME_FIRST.md                    # 快速开始指南
├── .env.example                       # 环境变量模板
├── requirements.txt                   # Python 依赖
│
├── src/                               # 源代码 (35 个文件)
│   ├── main.py                        # CLI 入口
│   ├── cli/                           # CLI 系统 (v0.6.0)
│   │   ├── commands.py                # 6 个核心命令
│   │   ├── ui.py                      # Rich Console 界面
│   │   └── formatters.py              # 输出格式化
│   ├── processors/                    # 内容处理器 (7 个)
│   │   ├── wechat_processor.py        # 微信文章
│   │   ├── zhihu_processor.py         # 知乎内容
│   │   ├── chat_processor.py          # 聊天记录
│   │   ├── ai_chat_processor.py       # AI 对话
│   │   └── text_fallback_processor.py # 纯文本
│   ├── storage/                       # 三层存储
│   │   ├── markdown_store.py          # Markdown 主存储
│   │   ├── sqlite_store.py            # SQLite 元数据索引
│   │   ├── vector_store.py            # hnswlib 向量索引
│   │   └── migration_manager.py       # 数据库迁移 (v0.6.1)
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
│   │   ├── archive-url.yaml           # 归档工作流
│   │   └── search.yaml                # 搜索工作流
│   └── custom_dict.txt                # 自定义分词词典
│
├── scripts/                           # 运维脚本 (v0.6.0)
│   ├── setup-conda.ps1                # Conda 环境安装
│   ├── test-conda.ps1                 # 环境验证
│   ├── backup-data.ps1                # 数据备份
│   ├── restore-data.ps1               # 数据恢复
│   ├── migrate.py                     # 数据库迁移工具
│   └── migrations/                    # SQL 迁移脚本
│
├── tests/                             # 测试套件 (30 个文件, 85% 覆盖率)
│   ├── unit/                          # 单元测试 (18 个)
│   ├── integration/                   # 集成测试 (2 个)
│   ├── e2e/                           # E2E 测试
│   ├── blackbox/                      # 黑盒测试
│   ├── fixtures/                      # 测试数据
│   └── manual_test_*.py               # 手动测试脚本
│
├── docs/                              # 文档 (60+ 文件)
│   ├── core/                          # 核心文档
│   │   ├── (归档至 docs/archive/)     # Phase 1 开发计划（已归档）
│   │   ├── personal-knowledge-vault-prd.md  # PRD
│   │   ├── 架构设计.md                # 架构设计
│   │   ├── QUICKSTART.md              # 详细指南
│   │   └── ...
│   ├── refactor/                      # 接口规范 (11 份)
│   ├── milestones/                    # 里程碑文档
│   ├── prompts/                       # 开发 Prompt
│   ├── CHANGELOG.md                   # 更新日志
│   ├── API文档.md                     # API 参考
│   └── 快速用户手册.md                # 用户手册
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

### ⚡ 3 步安装 (推荐：Conda 方式)

```powershell
# 1️⃣ 克隆仓库
git clone https://github.com/yourusername/personal-knowledge-vault.git
cd personal-knowledge-vault

# 2️⃣ 运行自动安装脚本（创建 Python 3.11 环境 + 安装依赖）
.\scripts\setup-conda.ps1

# 3️⃣ 配置 API Keys
cp .env.example .env
notepad .env  # 填入你的 DeepSeek 和 OpenAI API Keys

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
| [docs/core/QUICKSTART.md](docs/core/QUICKSTART.md) | 详细安装指南 | 遇到问题时参考 |
| [docs/快速用户手册.md](docs/快速用户手册.md) | 用户手册 | 日常使用参考 |
| [docs/API文档.md](docs/API文档.md) | API 参考 | 开发集成 |
| [docs/数据库迁移指南.md](docs/数据库迁移指南.md) | 数据库升级 | 版本更新时 |

## 📖 文档索引

### 🚀 快速开始

| 文档 | 说明 | 时长 |
|------|------|------|
| [RUN_ME_FIRST.md](RUN_ME_FIRST.md) | 3 步快速开始 | 5 分钟 |
| [docs/core/QUICKSTART.md](docs/core/QUICKSTART.md) | 详细安装指南 | 15 分钟 |
| [docs/快速用户手册.md](docs/快速用户手册.md) | 用户手册（含示例） | 30 分钟 |
| [docs/API文档.md](docs/API文档.md) | API 参考文档 | 按需查阅 |

### 📐 架构设计

| 文档 | 内容 | 适合读者 |
|------|------|---------|
| [CLAUDE.md](CLAUDE.md) | 项目索引（AI 协作上下文） | AI 协作者 |
| [docs/core/personal-knowledge-vault-prd.md](docs/core/personal-knowledge-vault-prd.md) | 产品需求文档 | 产品经理、决策者 |
| [docs/core/架构设计.md](docs/core/架构设计.md) | 系统架构与数据流 | 架构师、开发者 |
| [docs/core/技术选型.md](docs/core/技术选型.md) | 技术栈对比与决策 | 技术决策者 |
| [docs/core/项目结构说明.md](docs/core/项目结构说明.md) | 目录结构详解 | 新手开发者 |

### 🔧 接口规范 (11 份核心文档)

| 文档 | 内容 |
|------|------|
| [docs/refactor/Entry数据模型规范.md](docs/refactor/Entry数据模型规范.md) | 知识条目数据结构 |
| [docs/refactor/Processors接口规范.md](docs/refactor/Processors接口规范.md) | 内容处理器接口 |
| [docs/refactor/Storage接口规范.md](docs/refactor/Storage接口规范.md) | 三层存储架构 |
| [docs/refactor/Retrieval检索引擎规范.md](docs/refactor/Retrieval检索引擎规范.md) | 检索策略设计 |
| [docs/refactor/WorkflowEngine接口规范.md](docs/refactor/WorkflowEngine接口规范.md) | 工作流引擎接口 |
| [查看完整列表](docs/refactor/文档分类清单.md) | 11 份规范文档索引 |

### 🎯 里程碑报告

| 里程碑 | 状态 | 文档 |
|--------|------|------|
| M1: 基础设施层 | ✅ 完成 | [MILESTONE1_COMPLETE.md](docs/milestones/MILESTONE1_COMPLETE.md) |
| M2: AI 服务层 | ✅ 完成 | [MILESTONE2_COMPLETE.md](docs/milestones/MILESTONE2_COMPLETE.md) |
| M3: 内容处理器 | ✅ 完成 | [MILESTONE3_COMPLETE.md](docs/milestones/MILESTONE3_COMPLETE.md) |
| M3.5: AI 对话处理器 | ✅ 完成 | [MILESTONE3_5_COMPLETE.md](docs/milestones/MILESTONE3_5_COMPLETE.md) |
| M4: 检索引擎 | ✅ 完成 | [M4_COMPLETION_REPORT.md](docs/milestones/M4_COMPLETION_REPORT.md) |
| M5: 工作流引擎 | ✅ 完成 | [M5_COMPLETION_SUMMARY.md](docs/milestones/M5_COMPLETION_SUMMARY.md) |
| M5.1: Bug 修复 | ✅ 完成 | [M5_1_BUGFIX_COMPLETE.md](docs/milestones/M5_1_BUGFIX_COMPLETE.md) |
| M6+M7: CLI 与文档 | ✅ 完成 | 参考 [CHANGELOG.md](docs/CHANGELOG.md) |

### 🛠️ 运维指南 (v0.6.0+)

| 文档 | 内容 |
|------|------|
| [docs/环境演示.md](docs/环境演示.md) | 完整环境演示案例 |
| [docs/数据库迁移指南.md](docs/数据库迁移指南.md) | 数据库升级与迁移 |
| [docs/测试环境隔离指南.md](docs/测试环境隔离指南.md) | 测试环境管理 |
| [scripts/README.md](scripts/README.md) | 运维脚本使用说明 |

### 📊 项目管理

| 文档 | 内容 |
|------|------|
| [docs/archive/PHASE1_DEV_PROMPT.md](docs/archive/PHASE1_DEV_PROMPT.md) | Phase 1 开发计划（已归档） |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | 更新日志 |
| [docs/文档分类清单.md](docs/文档分类清单.md) | 60+ 文档索引 |
| [docs/refactor/Bug修复记录.md](docs/refactor/Bug修复记录.md) | 问题跟踪 |

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
python scripts/migrate.py upgrade    # 升级到最新版本
python scripts/migrate.py rollback   # 回滚到上一版本
python scripts/migrate.py history    # 查看迁移历史
```

**特点**:
- 版本号追踪（存储在 `schema_version` 表）
- 增量 SQL 脚本（`scripts/migrations/*.sql`）
- 向前/向后兼容

## 📊 项目状态

### 🎉 当前版本: v0.6.1 (生产就绪)

**发布状态**: ✅ 所有里程碑已完成，项目处于可交付状态

| 指标 | 数值 | 说明 |
|------|------|------|
| **项目版本** | v0.6.1 | AI 安全测试 + 数据库迁移 |
| **开发进度** | 100% | M1-M7 全部完成 |
| **源代码文件** | 35 个 | Python 模块 |
| **测试文件** | 30 个 | 单元/集成/E2E/黑盒测试 |
| **测试覆盖率** | 85% | 核心业务逻辑高覆盖 |
| **文档覆盖率** | 100% | 所有模块有 CLAUDE.md |
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

### 测试覆盖率: 85%

**5 层测试体系**:

| 测试类型 | 文件数 | 覆盖范围 | 运行方式 |
|---------|--------|----------|----------|
| **单元测试** | 18 个 | 核心业务逻辑 | `pytest tests/unit/ -v` |
| **集成测试** | 2 个 | 跨模块集成 | `pytest tests/integration/ -v` |
| **E2E 测试** | 1 个 | 端到端流程 | `pytest tests/e2e/ -v` |
| **黑盒测试** | 2 个 | CLI 用户行为 | `pytest tests/blackbox/ -v` |
| **手动测试** | 5 个 | 真实环境验证 | `python tests/manual_test_*.py` |

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
**当前版本**: v0.6.1
**创建日期**: 2026-01-27
**文档版本**: v3.0
**最后更新**: 2026-02-16

---

**✨ 工作流驱动的 AI-First 知识管理系统 ✨**

[📖 开始使用](RUN_ME_FIRST.md) · [📚 查看文档](docs/) · [🐛 反馈问题](https://github.com/yourusername/personal-knowledge-vault/issues) · [⭐ Star 支持](https://github.com/yourusername/personal-knowledge-vault)

</div>
