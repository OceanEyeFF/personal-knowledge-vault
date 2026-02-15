# 更新日志 (Changelog)

所有重要的项目变更都将记录在此文件中喵～ ฅ'ω'ฅ

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
- ✅ [RUN_ME_FIRST.md](RUN_ME_FIRST.md) - 快速开始指南
- ✅ [scripts/README.md](scripts/README.md) - 脚本详细说明
- ✅ [QUICKSTART.md](QUICKSTART.md) - 安装教程
- ✅ [docs/开发环境搭建.md](docs/开发环境搭建.md) - 详细环境搭建指南

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
