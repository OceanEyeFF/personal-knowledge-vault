# 个人知识库系统

> **AI-First Knowledge Workflow System**
> 一个以 AI 工作流驱动的个人知识管理系统

## ✨ 核心特点

- 🤖 **AI 协作式处理**: 以 Claude Code/CodeX 作为智能协作伙伴，支持人机协作的知识处理
- 🔍 **智能混合检索**: 根据内容特点自动选择 BM25/向量/混合检索策略，精确又高效
- ⚡ **工作流驱动**: 一切操作皆工作流，可编排、可观测、可中断
- 🔒 **本地优先**: 数据完全掌控，隐私安全
- 💰 **成本可控**: 智能策略节省 85% API 成本

## 🎯 核心功能

### 内容归档
- 📱 **微信文章**: 保留格式，自动提取正文
- 💬 **知乎内容**: 问题、回答、专栏一键归档
- 🎬 **B站视频**: 音频转录 + 评论分析 + 时间轴索引
- 📚 **PDF 书籍**: OCR + 章节分析 + 知识图谱（计划中）

### 智能检索
- 关键词精确查询（BM25）
- 语义理解查询（向量检索）
- 混合策略自动选择
- 结构化内容定位（视频时间点跳转）

### 工作流命令
```bash
# 归档网页
/kb:archive-url https://mp.weixin.qq.com/xxx

# 处理B站视频
/kb:archive-bilibili https://www.bilibili.com/video/BVxxx

# 智能检索
/kb:search "关键词"

# RAG 问答
/kb:ask "这个概念是什么意思？"
```

## 📂 项目结构

```
personal-knowledge-vault/
├── README.md                    # 本文件
├── docs/                        # 📚 设计文档
│   ├── 项目立项文档.md          # 愿景与核心决策
│   ├── 架构设计.md              # 系统架构与数据流
│   ├── 数据规范.md              # Markdown Front Matter 标准
│   ├── 技术选型.md              # 技术栈与检索策略
│   ├── 数据库Schema设计.md      # SQLite + hnswlib 完整设计
│   └── 开发计划.md              # 里程碑与风险管理
├── src/                         # 源代码
├── pkv/                         # Python 包
│   ├── storage/                 # 数据存储模块
│   │   ├── db_init.py          # 数据库初始化
│   │   ├── vector_index.py     # 向量索引管理
│   │   └── vector_operations.py # 向量操作接口
│   ├── retrieval/               # 检索引擎
│   └── utils/                   # 工具函数
├── .claude/                     # Claude Code 工作流配置
├── config/                      # 配置文件
├── pkv_index.db                 # SQLite 索引数据库（运行时生成）
└── pkv_vectors/                 # 向量索引目录（运行时生成）
    ├── doc_vectors.idx          # 文档级向量
    └── chunk_vectors.idx        # 分块级向量
```

## 🚀 快速开始

### 环境要求

**推荐配置** (避免兼容性问题):
- **Conda** (Miniconda 或 Anaconda) 🌟
- Python 3.11 (通过 Conda 自动创建)
- SQLite 3.35+ (支持 FTS5，通常系统自带)

**或传统配置**:
- Python 3.11 (⚠️ 不推荐 3.13，存在依赖兼容性问题)
- SQLite 3.35+ (支持 FTS5，通常系统自带)

**必需 API Keys**:
- DeepSeek API (推荐，用于摘要生成)
- OpenAI API (用于 Embedding)
- Whisper API (可选，用于视频转录)

### 快速安装 (推荐：Conda 方式)

```powershell
# 1. 克隆仓库
git clone https://github.com/yourusername/personal-knowledge-vault.git
cd personal-knowledge-vault

# 2. 运行 Conda 安装脚本（自动创建 Python 3.11 环境并安装依赖）
.\scripts\setup-conda.ps1

# 3. 配置 API Keys
notepad .env
# 填入你的 DeepSeek 和 OpenAI API Keys

# 4. 运行验证测试
.\scripts\test-conda.ps1

# 5. 每次使用前激活环境
conda activate pkv-py311
```

### 传统安装 (venv 方式)

```powershell
# 1. 克隆仓库
git clone https://github.com/yourusername/personal-knowledge-vault.git
cd personal-knowledge-vault

# 2. 运行安装脚本
.\scripts\setup.ps1

# 3. 配置 API Keys
notepad .env

# 4. 运行验证测试
.\scripts\test.ps1
```

### 详细指南

- 📖 [RUN_ME_FIRST.md](RUN_ME_FIRST.md) - 快速开始（推荐首次使用）
- 📖 [docs/QUICKSTART.md](docs/QUICKSTART.md) - 详细安装指南
- 📖 [docs/VALIDATION_REPORT.md](docs/VALIDATION_REPORT.md) - 验证报告

## 📖 文档索引

### 快速开始
| 文档 | 内容 |
|------|------|
| [RUN_ME_FIRST.md](RUN_ME_FIRST.md) | 3 步快速开始 |
| [QUICKSTART.md](docs/QUICKSTART.md) | 详细安装指南 |

### 设计文档
| 文档 | 内容 | 适合读者 |
|------|------|---------|
| [项目立项文档](docs/项目立项文档.md) | 愿景与核心理念 | 决策者 |
| [架构设计](docs/架构设计.md) | 系统架构与数据流 | 开发者 |
| [数据规范](docs/数据规范.md) | Markdown 标准 | 开发者 |
| [技术选型](docs/技术选型.md) | 技术栈对比 | 开发者 |
| [数据库Schema](docs/数据库Schema设计.md) | SQLite + hnswlib | 开发者 |
| [开发环境搭建](docs/开发环境搭建.md) | 环境配置 | 开发者 |

### 项目管理
| 文档 | 内容 |
|------|------|
| [STARTER_PROMPT.md](docs/STARTER_PROMPT.md) | 完整开发计划 |
| [CHANGELOG.md](docs/CHANGELOG.md) | 更新日志 |
| [MILESTONE1_COMPLETE.md](docs/MILESTONE1_COMPLETE.md) | Milestone 1 完成报告 |
| [VALIDATION_REPORT.md](docs/VALIDATION_REPORT.md) | 验证测试报告 |

## 💡 设计亮点

### 双 Git 仓库策略
- **主仓库**: 代码 + 配置（可公开分享）
- **内容仓库** (`docs/`): 个人知识内容（独立管理）
- 完全隔离，各自独立版本控制

### 双重存储策略
- **主存储**: Markdown + YAML Front Matter（数据主权，人类可读）
- **辅助存储**: SQLite + hnswlib（元数据索引，全文搜索，向量检索）
- **技术栈**:
  - SQLite FTS5 + jieba 中文分词（关键词检索）
  - hnswlib HNSW 算法（向量检索，独立文件存储）
  - 所有数据可从 Markdown 完全重建，辅助存储仅为缓存

### 智能检索策略
| 内容类型 | 策略 | 原因 |
|---------|------|------|
| 短文本（< 2000字） | BM25 | 精确快速，零成本 |
| 中等文本 | 混合检索 | 兼顾精确与语义 |
| 长文本/书籍 | 向量分块 | 语义理解强 |
| 视频内容 | 结构化索引 | 支持时间点定位 |

**成本对比**: 混合策略比纯向量方案节省 **85% API 成本**

## 📊 项目状态

**当前阶段**: ✅ Milestone 1 完成 - 基础设施层

**Milestone 1: 基础设施层** ✅ (已完成)
- [x] 配置系统 (YAML + 环境变量)
- [x] Markdown 存储 (YAML Front Matter)
- [x] SQLite 存储 (完整 Schema + FTS5)
- [x] 向量存储 (hnswlib HNSW)
- [x] 文本处理 (jieba 中文分词)
- [x] 安装脚本 (Conda + venv 双方案)
- [x] 验证测试

**后续里程碑**:
- [ ] Milestone 2: AI 服务层（DeepSeek、OpenAI）
- [ ] Milestone 3: 内容处理器（微信、知乎、通用网页）
- [ ] Milestone 4: 检索引擎（BM25、向量、混合检索）
- [ ] Milestone 5: 工作流引擎
- [ ] Milestone 6: CLI 入口
- [ ] Milestone 7: 文档和交付

## 🤝 贡献指南

本项目目前处于个人开发阶段，暂不接受外部贡献。
如有建议或问题，欢迎提 Issue。

## 📄 开源协议

MIT License

---

**项目代号**: Personal Knowledge Vault
**创建日期**: 2026-01-27
**文档版本**: v1.5
**最后更新**: 2026-02-14
- Milestone 1 基础设施层完成
- 新增 Conda 安装方案（解决 Python 3.13 兼容性问题）
- 完成配置、存储、向量、文本处理模块
