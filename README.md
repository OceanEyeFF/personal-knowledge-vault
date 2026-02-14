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
├── README.md                 # 本文件
├── docs/                     # 📚 设计文档
│   ├── 项目立项文档.md       # 愿景与核心决策
│   ├── 架构设计.md           # 系统架构与数据流
│   ├── 数据规范.md           # Markdown Front Matter 标准
│   ├── 技术选型.md           # 技术栈与检索策略
│   └── 开发计划.md           # 里程碑与风险管理
├── src/                      # 源代码
├── .claude/                  # Claude Code 工作流配置
└── config/                   # 配置文件
```

## 🚀 快速开始

### 环境要求
- Python 3.11+
- PostgreSQL 15+ (带 pgvector 扩展)
- 必需 API Keys:
  - DeepSeek API (推荐)
  - OpenAI API (embedding)
  - Whisper API (可选，视频转录)

### 安装步骤

```bash
# 1. 克隆仓库
git clone https://github.com/yourusername/personal-knowledge-vault.git
cd personal-knowledge-vault

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置数据库
createdb knowledge_vault
psql knowledge_vault -c "CREATE EXTENSION vector;"

# 4. 配置 API Keys
cp config/example.env config/.env
# 编辑 .env 填入你的 API Keys

# 5. 运行
python src/main.py
```

## 📖 文档索引

| 文档 | 内容 | 适合读者 |
|------|------|---------|
| [项目立项文档](docs/项目立项文档.md) | 为什么做？怎么做？核心理念 | 决策者、回顾愿景 |
| [架构设计](docs/架构设计.md) | 工作流架构、目录结构、数据流 | 开发者 |
| [数据规范](docs/数据规范.md) | Markdown Front Matter 标准、文件命名、目录结构 | 开发者、内容编辑 |
| [技术选型](docs/技术选型.md) | 技术栈对比、检索策略、数据模型 | 开发者 |
| [开发计划](docs/开发计划.md) | 里程碑、成本估算、风险管理 | 项目管理者 |

## 💡 设计亮点

### 双 Git 仓库策略
- **主仓库**: 代码 + 配置（可公开分享）
- **内容仓库** (`docs/`): 个人知识内容（独立管理）
- 完全隔离，各自独立版本控制

### 智能检索策略
| 内容类型 | 策略 | 原因 |
|---------|------|------|
| 短文本（< 2000字） | BM25 | 精确快速，零成本 |
| 中等文本 | 混合检索 | 兼顾精确与语义 |
| 长文本/书籍 | 向量分块 | 语义理解强 |
| 视频内容 | 结构化索引 | 支持时间点定位 |

**成本对比**: 混合策略比纯向量方案节省 **85% API 成本**

## 📊 项目状态

**当前阶段**: 📝 设计阶段（Phase 0）

- [x] 完成立项文档
- [x] 完成架构设计
- [ ] Phase 0: 原型验证（3-5天）
- [ ] Phase 1: 网页归档 MVP（2周）
- [ ] Phase 2: B站视频处理（2周）
- [ ] Phase 3: 智能检索优化（2周）

## 🤝 贡献指南

本项目目前处于个人开发阶段，暂不接受外部贡献。
如有建议或问题，欢迎提 Issue。

## 📄 开源协议

MIT License

---

**项目代号**: Personal Knowledge Vault
**创建日期**: 2026-01-27
**文档版本**: v1.3
