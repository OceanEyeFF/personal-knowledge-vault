# Personal Knowledge Vault - 完整开发 Starter Prompt

> 用于 AI Agent（Claude Code/CodeX）的项目开发启动指令
>
> **版本**: 1.0
> **创建日期**: 2026-02-14
> **适用对象**: Claude Code、CodeX、GitHub Copilot Workspace 等 AI 开发工具

---

## 🎯 项目概述

基于完整的需求文档和技术设计，开发一个 **AI-First 的个人知识库系统**。

**核心功能**:
- 智能内容归档（网页、聊天记录、新闻）
- 混合检索引擎（BM25 + 向量语义）
- AI 驱动的摘要和标签提取
- 工作流驱动的可扩展架构

**技术栈**:
- Python 3.11+
- SQLite + FTS5 (全文搜索)
- hnswlib (向量检索)
- DeepSeek API (摘要生成)
- OpenAI Embedding (向量化)

---

## 📚 必读文档

在开始开发前，**必须完整阅读**以下文档（按顺序）：

### 1. 战略和产品层
- [`docs/项目立项文档.md`](docs/项目立项文档.md) - 项目愿景和设计理念
- [`docs/personal-knowledge-vault-prd.md`](docs/personal-knowledge-vault-prd.md) - **核心需求文档**（最重要）

### 2. 架构和设计层
- [`docs/架构设计.md`](docs/架构设计.md) - **工作流驱动架构**（核心设计）
- [`docs/技术选型.md`](docs/技术选型.md) - 技术栈决策和检索策略
- [`docs/数据库Schema设计.md`](docs/数据库Schema设计.md) - 数据模型
- [`docs/数据规范.md`](docs/数据规范.md) - Markdown + YAML Front Matter 规范

### 3. 开发指南层
- [`docs/开发环境搭建.md`](docs/开发环境搭建.md) - 环境配置
- [`docs/项目结构说明.md`](docs/项目结构说明.md) - 代码组织
- [`docs/API文档.md`](docs/API文档.md) - 接口定义
- [`docs/工作流开发指南.md`](docs/工作流开发指南.md) - 扩展指南

---

## ⚠️ 关键约束和要求

### 环境保护规则（必须严格遵守）

1. **不污染默认 Python 环境**
   - ✅ 必须使用虚拟环境（`.venv/` 或 conda）
   - ❌ 禁止 `sudo pip install` 或全局安装
   - ✅ 所有依赖写入 `requirements.txt`

2. **不修改系统配置**
   - ❌ 不修改 `~/.bashrc` 或 `~/.zshrc`
   - ❌ 不修改系统级 Python 路径
   - ✅ 项目级配置放在 `.env` 和 `config/` 下

3. **不产生垃圾文件**
   - ✅ 临时文件放在 `.data/tmp/`（已在 `.gitignore`）
   - ✅ 日志文件放在 `.data/logs/`
   - ✅ 测试数据放在 `tests/fixtures/`

4. **Git 仓库清洁**
   - ✅ 遵守 `.gitignore` 规则
   - ❌ 不提交 `.data/`、`.venv/`、`.env` 等
   - ✅ 提交前检查 `git status`

---

### 代码质量要求

1. **遵循 KISS、DRY、SOLID 原则**（见 PRD）
2. **类型注解**：所有函数必须有 type hints
3. **文档字符串**：公共 API 必须有 docstring
4. **错误处理**：优雅降级，不允许裸 `except:`
5. **日志记录**：关键操作必须记录日志

---

## 🏗️ 开发里程碑（Milestones）

> **当前进度**: ✅ M1-M3 已完成 | 🚧 M3.5 进行中 | ⏳ M4-M7 待开始

### ✅ Milestone 1: 基础设施层 (Week 1, Day 1-3) - **已完成**

**目标**: 搭建数据存储和配置基础

**交付物**:
- [x] `src/storage/markdown_store.py` - Markdown 文件管理
- [x] `src/storage/sqlite_store.py` - SQLite 数据库封装
- [x] `src/storage/vector_store.py` - hnswlib 向量索引
- [x] `src/utils/config.py` - 配置加载器
- [x] `config/config.yaml` - 主配置文件
- [x] `.env.example` - 环境变量模板
- [x] `tests/unit/test_storage_*.py` - 存储层单元测试

**验收标准**:
```python
# 测试脚本示例
from src.storage.markdown_store import MarkdownStore

store = MarkdownStore(vault_dir=".data/vault")
entry = Entry(title="测试", content="# 内容")
path = store.save(entry)
assert path.exists()

loaded = store.load(path)
assert loaded.title == "测试"
```

**白盒测试检查点**:
1. Markdown 文件生成格式正确（YAML Front Matter + 内容）
2. SQLite 数据库 Schema 符合设计文档
3. hnswlib 索引可以正常初始化和查询
4. 配置文件加载无误，环境变量正确读取

---

### ✅ Milestone 2: AI 服务封装 (Week 1, Day 3-4) - **已完成**

**目标**: 封装 DeepSeek 和 OpenAI API

**交付物**:
- [x] `src/ai/deepseek_client.py` - DeepSeek API 封装
- [x] `src/ai/openai_client.py` - OpenAI Embedding 封装
- [x] `src/ai/embedder.py` - 统一向量化接口
- [x] `src/ai/prompts/summarize.txt` - 摘要生成提示词
- [x] `src/ai/prompts/extract_tags.txt` - 标签提取提示词
- [x] `tests/unit/test_ai_*.py` - AI 服务单元测试

**验收标准**:
```python
from src.ai.deepseek_client import DeepSeekClient

client = DeepSeekClient()
summary = client.summarize("长文本内容...", max_words=300)
assert len(summary) <= 500  # 摘要合理长度
assert summary  # 非空

tags = client.extract_tags("文本内容...")
assert isinstance(tags, list)
assert len(tags) >= 3 and len(tags) <= 5
```

**白盒测试检查点**:
1. API 调用成功，返回格式正确
2. 错误处理完善（网络错误、API 限流、无效响应）
3. API Key 从环境变量正确加载
4. 成本控制：单次调用 token 数量合理

---

### ✅ Milestone 3: 内容处理器 (Week 1, Day 5 - Week 2, Day 1) - **已完成**

**目标**: 实现微信、知乎、通用网页处理器

**交付物**:
- [x] `src/processors/base.py` - 处理器基类
- [x] `src/processors/wechat_processor.py` - 微信文章处理器
- [x] `src/processors/zhihu_processor.py` - 知乎内容处理器
- [x] `src/processors/generic_processor.py` - 通用网页处理器
- [x] `src/processors/chat_processor.py` - 聊天记录处理器
- [x] `src/processors/__init__.py` - 处理器注册
- [x] `tests/unit/test_processors_*.py` - 处理器单元测试
- [x] `tests/fixtures/sample_*.html` - 测试用 HTML 样本

**验收标准**:
```python
from src.processors import get_processor

# 微信文章
processor = get_processor("https://mp.weixin.qq.com/s/xxx")
assert processor.__class__.__name__ == "WechatProcessor"

content = await processor.process(url)
assert content.title
assert content.content  # Markdown 格式
assert content.metadata["source"] == url
```

**白盒测试检查点**:
1. 每个处理器的 `can_handle()` 逻辑正确
2. HTML 转 Markdown 保留关键格式（标题、列表、代码块）
3. 元数据提取完整（作者、发布时间、来源）
4. 异常处理：网络错误、解析失败、反爬虫

**已知限制**:
- ❌ 知乎问答页面（question+answer 格式）暂不支持（动态跳转+严格反爬虫）
- ⚠️ 建议使用知乎专栏链接（zhuanlan.zhihu.com）替代

---

### Milestone 3.5: AI 对话处理器与文本 Fallback (新增)

**目标**: 支持 AI 对话导出格式和纯文本 Fallback 机制

**背景**:
- DeepSeek/ChatGPT 对话导出是知识整理的重要来源
- 需要 Fallback 机制处理直接复制粘贴的文本内容

**交付物**:
- [ ] `src/processors/ai_chat_processor.py` - AI 对话处理器
  - 支持 ChatGPT HTML 导出格式（包含 `data-turn` 属性）
  - 支持 ChatGPT Markdown 导出格式（`**You:**` / `**ChatGPT:**`）
  - 支持 ChatGPT TXT 导出格式
  - 支持 DeepSeek HTML 导出格式（包含 `message user`/`assistant` 类）
  - 支持 DeepSeek Markdown 导出格式（`### 用户` / `### DeepSeek AI`）
  - 支持 DeepSeek TXT 导出格式（`user:` / `assistant:`）
- [ ] `src/processors/text_fallback_processor.py` - 纯文本 Fallback 处理器
  - 智能检测文本类型（对话 vs 文章）
  - 处理直接复制粘贴的内容
  - 提取关键信息（无严格格式要求）
  - 优雅降级（格式不明确时）
- [ ] `tests/fixtures/AI_ChatContexts/` - AI 对话样本数据
- [ ] `tests/unit/test_ai_chat_processor.py` - AI 对话处理器单元测试
- [ ] `tests/unit/test_text_fallback_processor.py` - 文本 Fallback 单元测试
- [ ] `tests/fixtures/test_urls.json` - 更新测试配置（新增 AI 对话测试用例）

**验收标准**:
```python
from src.processors import get_processor

# AI 对话处理器 - ChatGPT HTML
processor = get_processor("path/to/chatgpt_export.html")
assert processor.__class__.__name__ == "AIChatProcessor"

entry = await processor.process("path/to/chatgpt_export.html")
assert entry.title  # 从对话第一条或 <title> 提取
assert entry.content  # Markdown 格式的对话记录
assert "ChatGPT" in entry.metadata.get("ai_platform", "")

# AI 对话处理器 - DeepSeek Markdown
processor = get_processor("path/to/deepseek_export.md")
entry = await processor.process("path/to/deepseek_export.md")
assert "DeepSeek" in entry.metadata.get("ai_platform", "")

# 文本 Fallback 处理器
processor = get_processor("直接复制的文本内容...")
assert processor.__class__.__name__ == "TextFallbackProcessor"

entry = await processor.process("一段没有明确格式的文本...")
assert entry.content  # 原文本内容
assert entry.abstract or entry.summary_100_words  # AI 生成摘要
```

**白盒测试检查点**:
1. AI 对话处理器能正确识别 6 种格式（ChatGPT/DeepSeek × HTML/MD/TXT）
2. 对话角色提取准确（User/Assistant）
3. 对话轮次分隔正确
4. 标题生成合理（从第一条用户消息或文件标题提取）
5. 文本 Fallback 处理器能智能区分对话 vs 文章
6. Fallback 处理器能处理无格式文本
7. 所有处理器集成到 `get_processor()` 路由

**实现优先级**:
1. **高优先级**: AI 对话处理器（B）
2. **高优先级**: 文本 Fallback 处理器（D）
3. **中优先级**: 更新文档标注知乎限制（C）

---

### Milestone 4: 检索引擎 (Week 2, Day 2-3)

**目标**: 实现 BM25、向量、混合检索

**交付物**:
- [ ] `src/retrieval/bm25_search.py` - BM25 关键词检索
- [ ] `src/retrieval/vector_search.py` - 向量语义检索
- [ ] `src/retrieval/hybrid_search.py` - 混合检索
- [ ] `src/retrieval/query_router.py` - 查询路由器
- [ ] `tests/unit/test_retrieval_*.py` - 检索单元测试
- [ ] `tests/integration/test_search_accuracy.py` - 检索准确率测试

**验收标准（关键）**:
```python
from src.retrieval.hybrid_search import HybridSearch

searcher = HybridSearch()

# 准备测试数据（50+ 条）
# 执行测试查询（20+ 个）
results = searcher.search("分布式系统的权衡", top_k=5)

# 验证准确率 >= 85%（见 PRD 中的测试方法）
accuracy = calculate_accuracy(results, expected_results)
assert accuracy >= 0.85
```

**白盒测试检查点**:
1. jieba 分词正确，中文查询支持良好
2. BM25 参数调优（k1=1.5, b=0.75）
3. 向量检索 Top K 准确
4. 混合检索权重合理（BM25: 0.4, Vector: 0.6）
5. 查询路由器策略正确（短查询用 BM25，长查询用向量）

---

### Milestone 5: 工作流引擎 (Week 2, Day 4-5)

**目标**: 实现工作流编排和 idea Sharpen

**交付物**:
- [ ] `src/workflow/engine.py` - 工作流引擎核心
- [ ] `src/workflow/context.py` - 工作流上下文
- [ ] `src/workflow/commands.py` - Slash Commands 注册
- [ ] `src/workflow/steps/fetch_step.py` - 内容抓取步骤
- [ ] `src/workflow/steps/analyze_step.py` - AI 分析步骤
- [ ] `src/workflow/steps/sharpen_step.py` - idea Sharpen 步骤
- [ ] `src/workflow/steps/store_step.py` - 存储步骤
- [ ] `config/workflows/archive-url.yaml` - 归档 URL 工作流
- [ ] `config/workflows/search.yaml` - 搜索工作流
- [ ] `tests/integration/test_workflow_*.py` - 工作流集成测试

**验收标准（端到端）**:
```python
from src.workflow.engine import WorkflowEngine

engine = WorkflowEngine()

# 完整归档流程
result = await engine.execute_async(
    workflow_name="archive-url",
    input_data={"url": "https://example.com/article"}
)

assert result.success
assert result.data["entry_id"]
assert result.data["file_path"].exists()

# 验证存储正确
entry = store.load(result.data["file_path"])
assert entry.summary  # AI 生成的摘要
assert len(entry.tags) >= 3  # 标签提取
```

**白盒测试检查点**:
1. YAML 工作流配置正确加载
2. 步骤按顺序执行，上下文正确传递
3. idea Sharpen 触发条件正确（见 PRD 附录）
4. 错误处理：步骤失败时的重试和降级
5. 日志记录完整，可追溯

---

### Milestone 6: CLI 入口和命令行界面 (Week 2, Day 6)

**目标**: 实现用户友好的命令行工具

**交付物**:
- [ ] `src/main.py` - CLI 入口（使用 Click 框架）
- [ ] `src/cli/commands.py` - 命令定义
- [ ] `src/cli/ui.py` - 终端 UI（使用 Rich 库）
- [ ] `tests/integration/test_cli.py` - CLI 集成测试

**验收标准**:
```bash
# 归档 URL
python src/main.py archive-url "https://example.com/article"
# 预期输出：
# ✅ 已归档: 20260214-article-title
# 📂 路径: .data/vault/2026/02/20260214-article-title.md

# 搜索
python src/main.py search "分布式系统"
# 预期输出：
# 🔍 找到 5 个结果
# 1. 分布式系统设计 (score: 0.92)
#    摘要: ...
```

**白盒测试检查点**:
1. 命令参数解析正确
2. 错误提示友好（带颜色和 emoji）
3. 进度条显示（长时间操作）
4. 交互式提问（idea Sharpen）体验流畅

---

### Milestone 7: 文档和交付 (Week 2, Day 7)

**目标**: 完成使用文档和长期维护文档

**交付物**:
- [ ] `README.md` - 项目主文档（更新）
- [ ] `docs/使用手册.md` - **用户使用文档**（新增）
- [ ] `docs/维护指南.md` - **长期维护文档**（新增）
- [ ] `docs/API变更日志.md` - API 变更记录（新增）
- [ ] `CHANGELOG.md` - 版本变更日志（新增）

---

## 📖 交付文档要求

### 1. 使用手册 (docs/使用手册.md)

**必须包含的内容**:

```markdown
# 使用手册

## 快速开始
- 5 分钟上手教程
- 第一个归档示例
- 第一次搜索示例

## 核心功能
- 归档网页内容
  - 支持的网站列表
  - 命令格式和参数
  - 常见问题
- 归档聊天记录
  - 格式要求
  - 示例
- 归档新闻
  - 特殊处理说明
- 搜索和检索
  - 搜索技巧
  - 高级查询语法
  - 结果排序

## idea Sharpen 使用指南
- 什么时候会触发
- 如何回答 AI 的问题
- 如何跳过（批量模式）

## 配置和定制
- 配置文件说明
- 环境变量配置
- 自定义词典
- 调整检索策略

## 故障排查
- 常见错误和解决方案
- 日志查看方法
- 如何报告 Bug

## 最佳实践
- 如何组织标签
- 如何利用关联推荐
- 备份和导出数据
```

---

### 2. 维护指南 (docs/维护指南.md)

**必须包含的内容**:

```markdown
# 长期维护指南

## 系统架构概述
- 核心组件依赖关系
- 数据流图
- 关键设计决策回顾

## 日常维护
- 数据库维护
  - SQLite 数据库优化（VACUUM）
  - FTS5 索引重建
  - 向量索引重建
- 日志管理
  - 日志轮转策略
  - 日志清理脚本
- 备份策略
  - 自动备份脚本
  - 恢复流程

## 性能监控
- 关键性能指标（KPI）
  - 归档耗时
  - 查询响应时间
  - 内存使用
- 性能分析工具
- 性能优化建议

## 扩展和升级
- 如何添加新的内容处理器
- 如何添加新的工作流
- 如何升级依赖库
- 数据库 Schema 迁移

## 问题诊断
- 日志分析方法
- 常见性能瓶颈
- 内存泄漏排查
- API 调用成本监控

## 版本升级
- 升级前检查清单
- 数据迁移步骤
- 回滚流程

## 应急响应
- 数据损坏恢复
- API 服务中断应对
- 系统崩溃恢复

## 技术债务管理
- 已知技术债务列表
- 优先级排序
- 清理计划
```

---

## 🧪 测试要求

### 单元测试覆盖率
- **最低要求**: 70%
- **目标**: 85%+
- 核心模块（存储、检索、AI）必须 90%+

### 集成测试
- 每个 Milestone 完成后必须通过集成测试
- 端到端测试（归档 → 存储 → 检索）必须通过

### 白盒测试检查点
- 每个 Milestone 都有明确的白盒测试检查点
- 必须在代码审查时验证

---

## 📦 最终交付清单

### 代码交付
- [ ] 完整的 `src/` 源代码
- [ ] 完整的 `tests/` 测试代码
- [ ] `requirements.txt` 依赖清单
- [ ] `.env.example` 环境变量模板
- [ ] `config/` 配置文件

### 文档交付
- [ ] `README.md` - 项目概览
- [ ] `docs/使用手册.md` - **用户文档**
- [ ] `docs/维护指南.md` - **维护文档**
- [ ] `docs/API变更日志.md` - API 文档
- [ ] `CHANGELOG.md` - 版本历史

**Milestone 完成报告（已完成）**:
- [x] `docs/MILESTONE1_COMPLETE.md` - M1 完成报告
- [x] `docs/MILESTONE2_COMPLETE.md` - M2 完成报告
- [x] `docs/MILESTONE2_REVIEW.md` - M2 审查报告
- [x] `docs/MILESTONE3_COMPLETE.md` - M3 完成报告
- [x] `docs/MILESTONE3_REVIEW.md` - M3 审查报告
- [x] `docs/MILESTONE3_TEST_RESULTS.md` - M3 集成测试结果报告
- [x] `docs/MILESTONE3_TESTING_GUIDE.md` - M3 测试指南

### 数据和脚本
- [ ] `scripts/init_db.py` - 数据库初始化脚本
- [ ] `scripts/backup.sh` - 备份脚本
- [ ] `scripts/verify_setup.py` - 安装验证脚本

### 测试和验证
- [ ] 单元测试覆盖率报告
- [ ] 集成测试通过证明
- [ ] 检索准确率测试结果（≥85%）

---

## 🎯 开始开发

### Step 1: 环境准备

```bash
# 创建虚拟环境
python3.11 -m venv .venv
source .venv/bin/activate  # macOS/Linux
# .venv\Scripts\activate   # Windows

# 创建基础目录结构（按照 docs/项目结构说明.md）
mkdir -p src/{workflow,processors,retrieval,storage,ai,utils,cli}
mkdir -p config/workflows
mkdir -p tests/{unit,integration,fixtures}
mkdir -p .data/{db,vectors,vault,logs,tmp}
```

### Step 2: 按 Milestone 顺序开发

从 **Milestone 1: 基础设施层** 开始，逐个完成。

### Step 3: 持续验证

每完成一个 Milestone：
1. 运行单元测试
2. 完成白盒测试检查点
3. 更新 CHANGELOG.md
4. Commit 代码（清晰的 commit message）

---

## ⚡ 开发原则提醒

1. **先读文档，后写代码** - 所有设计决策都在文档中
2. **遵循 KISS 原则** - 简单优于复杂
3. **遵循 DRY 原则** - 不要重复自己
4. **遵循 YAGNI 原则** - 只实现当前需要的功能
5. **测试先行** - 关键功能先写测试
6. **小步快跑** - 每个 Milestone 独立交付
7. **代码审查** - 每个 Milestone 完成后自我审查

---

## 🆘 遇到问题时

1. **优先查阅文档** - 大部分问题在设计文档中有答案
2. **查看故障排查** - `docs/开发环境搭建.md` 有常见问题
3. **检查日志** - 启用 DEBUG 日志级别
4. **降级处理** - 优雅降级而非失败

---

## ✅ 验收标准总结

**当前已完成（M1-M3）**:
1. ✅ 基础设施层：存储、数据库、配置（M1）
2. ✅ AI 服务封装：DeepSeek、OpenAI、向量化（M2）
3. ✅ 内容处理器：微信、知乎专栏、通用网页、聊天记录（M3）
4. ✅ 单元测试覆盖率 96.7%（M3，64 个测试全部通过）
5. ✅ 简单网页处理成功率 81.8%（9/11）

**进行中（M3.5）**:
- 🚧 AI 对话处理器（ChatGPT/DeepSeek 导出格式）
- 🚧 纯文本 Fallback 处理器

**待完成（M4-M7 - MVP 完成标志）**:
1. ⏳ 混合检索准确率 ≥ 85%
2. ⏳ 整理时间 ≤ 5 分钟（人机对话部分）
3. ⏳ 查询时间 ≤ 1 分钟
4. ⏳ 使用文档和维护文档完整

---

## 📝 开发日志模板

建议每天记录开发日志：

```markdown
# 开发日志 - 2026-02-XX

## 今日进度
- Milestone X: YYY 模块开发完成
- 通过单元测试 ZZZ

## 遇到的问题
- 问题描述
- 解决方案

## 明日计划
- 完成 Milestone X
- 开始 Milestone Y

## 技术债务记录
- [可选] 需要优化的地方
```

---

**准备好了吗？开始构建这个 AI-First 的知识管理系统吧！** 🚀

记住：质量优于速度，测试先行，文档完善，环境清洁！
