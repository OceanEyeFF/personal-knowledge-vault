# Entry 数据模型规范

> **版本**: 1.0
> **创建日期**: 2026-02-15
> **文件位置**: `src/storage/markdown_store.py`
> **作用**: 知识条目的核心数据结构，贯穿整个系统的数据流

---

## 📋 完整字段定义

### 基础元数据 (必填)

| 字段名 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `title` | `str` | ✅ | 无 | 知识条目标题，不能为空 |
| `source_type` | `str` | ✅ | 无 | 来源类型：`wechat`/`zhihu`/`bilibili`/`pdf`/`personal`/`ai_chat`/`text` |
| `source_url` | `Optional[str]` | ❌ | `None` | 来源 URL，可为空（如个人笔记） |
| `archived_at` | `Optional[str]` | ❌ | 当前时间 | 归档时间戳，格式：`YYYY-MM-DD HH:MM:SS` |

**约束规则**:
- `title` 不能为空字符串
- `source_type` 必须是预定义的类型之一
- `archived_at` 如果未提供，会在 `__post_init__` 中自动填充为当前时间

---

### 内容分析 (必填但可为默认值)

| 字段名 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `tags` | `list` | ✅ | `[]` | 标签列表（AI 提取或手动添加） |
| `keywords` | `list` | ✅ | `[]` | 关键词列表（AI 提取） |
| `abstract` | `str` | ✅ | `""` | 内容摘要 |

**约束规则**:
- `tags` 和 `keywords` 必须是字符串列表
- `abstract` 可以为空字符串

---

### 多层次摘要 (必填但可为默认值)

| 字段名 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `summary_one_sentence` | `str` | ✅ | `""` | 一句话摘要 |
| `summary_100_words` | `str` | ✅ | `""` | 100 字摘要 |

**约束规则**:
- 由 AI 服务 (DeepSeek) 生成
- 可以为空字符串（如果 AI 服务失败或被跳过）

---

### 检索配置 (必填但可为默认值)

| 字段名 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `search_strategy` | `str` | ✅ | `"keyword"` | 检索策略：`keyword`/`hybrid`/`vector`/`structured` |
| `word_count` | `int` | ✅ | `0` | 字数统计（中英文混合） |

**约束规则**:
- `search_strategy` 决定了该条目的检索方式
- `word_count` 如果为 0 且 `content` 不为空，会在 `__post_init__` 中自动计算

**自动计算逻辑**:
```python
if self.word_count == 0 and self.content:
    self.word_count = TextProcessor.calculate_word_count(self.content)
```

---

### 关联信息 (可选)

| 字段名 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `related_docs` | `list` | ❌ | `[]` | 相关文档列表 |

**约束规则**:
- 列表元素类型未严格定义（可以是文件路径、knowledge_id 或 URL）
- 目前主要由用户手动维护

---

### 个人标注 (可选)

| 字段名 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `reading_status` | `str` | ❌ | `""` | 阅读状态：`unread`/`reading`/`completed` 等 |
| `rating` | `int` | ❌ | `0` | 评分（1-5 星） |
| `notes` | `str` | ❌ | `""` | 个人笔记/思考 |

**约束规则**:
- `rating` 取值范围未强制校验，建议 0-5
- `notes` 由 IdeaSharpenStep 工作流步骤填充

---

### 正文内容

| 字段名 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `content` | `str` | ✅ | `""` | Markdown 格式的正文内容 |

**约束规则**:
- 存储在 Markdown 文件的 Body 部分（不在 Front Matter 中）
- 可以为空字符串（如纯元数据条目）

---

## 🔄 与 Markdown Front Matter 的映射关系

### 序列化规则 (Entry → Markdown)

**Front Matter 内容** (`to_dict()` 方法):
```python
def to_dict(self) -> Dict[str, Any]:
    """转换为字典 (不包含 content)"""
    data = asdict(self)
    data.pop("content", None)  # content 单独存储在 Body 中
    return data
```

**文件结构**:
```markdown
---
title: "示例标题"
source_type: "wechat"
source_url: "https://example.com"
archived_at: "2026-02-15 14:30:00"
tags: ["AI", "知识管理"]
keywords: ["向量检索", "工作流"]
abstract: "这是摘要"
summary_one_sentence: "一句话总结"
summary_100_words: "100 字总结..."
search_strategy: "hybrid"
word_count: 1500
related_docs: []
reading_status: "reading"
rating: 4
notes: "个人思考..."
---

# 正文内容

这里是 Markdown 格式的正文...
```

---

### 反序列化规则 (Markdown → Entry)

**加载逻辑** (`load()` 方法):
```python
# 解析 frontmatter
post = frontmatter.load(f)
metadata = post.metadata
content = post.content

# 构建 Entry 对象
entry = Entry(
    title=metadata.get("title", ""),
    source_type=metadata.get("source_type", "personal"),
    source_url=metadata.get("source_url"),
    # ... 其他字段
    content=content,
)
```

**默认值处理**:
- 缺失字段会使用 Entry 类的默认值
- `source_type` 默认为 `"personal"`
- 列表字段默认为 `[]`
- 字符串字段默认为 `""`

---

## ✅ 字段验证规则

### 当前验证机制

**自动验证**:
1. `archived_at` 自动填充当前时间（如果为 `None`）
2. `word_count` 自动计算（如果为 0 且有内容）

**缺失的验证**:
- ⚠️ **没有** `title` 非空检查
- ⚠️ **没有** `source_type` 枚举验证
- ⚠️ **没有** `rating` 范围验证
- ⚠️ **没有** `search_strategy` 枚举验证

### 建议增强验证

```python
def __post_init__(self):
    """初始化后处理"""
    # 现有逻辑
    if self.archived_at is None:
        self.archived_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if self.word_count == 0 and self.content:
        self.word_count = TextProcessor.calculate_word_count(self.content)

    # 建议添加的验证
    if not self.title or not self.title.strip():
        raise ValueError("title 不能为空")

    valid_source_types = {"wechat", "zhihu", "bilibili", "pdf", "personal", "ai_chat", "text"}
    if self.source_type not in valid_source_types:
        raise ValueError(f"无效的 source_type: {self.source_type}")

    if self.rating < 0 or self.rating > 5:
        raise ValueError(f"rating 必须在 0-5 之间，当前值: {self.rating}")
```

---

## 🔗 数据流中的角色

### 输入来源

1. **Processors 模块** (`src/processors/`)
   - `BaseProcessor.process(url)` 返回 Entry 对象
   - 填充字段：`title`, `content`, `source_type`, `source_url`

2. **AI 服务模块** (`src/ai/`)
   - DeepSeek 填充：`summary_100_words`, `tags`, `keywords`, `abstract`
   - Embedder 不直接修改 Entry，但会使用 `content` 生成向量

3. **工作流步骤** (`src/workflow/steps/`)
   - `IdeaSharpenStep` 填充：`notes` (用户交互后)
   - `AnalyzeStep` 填充：AI 分析结果

### 输出去向

1. **MarkdownStore** (`src/storage/markdown_store.py`)
   - `save(entry)` → 序列化为 Markdown 文件
   - 文件名：`{safe_title}.md` 或 `{safe_title}-{timestamp}.md`

2. **SQLiteStore** (`src/storage/sqlite_store.py`)
   - `insert_entry(entry, file_path)` → 插入数据库
   - 映射字段到 `knowledge_items` 表

3. **VectorStore** (`src/storage/vector_store.py`)
   - 提取 `content` 生成向量并存储
   - 关联 `knowledge_id`

---

## 📊 字段使用统计

### 高频使用字段 (几乎每个条目都有)

- `title` ✅
- `content` ✅
- `source_type` ✅
- `archived_at` ✅ (自动填充)
- `word_count` ✅ (自动计算)

### 中频使用字段 (AI 步骤成功时有)

- `tags` 🔄
- `keywords` 🔄
- `summary_100_words` 🔄
- `abstract` 🔄

### 低频使用字段 (用户手动维护)

- `notes` 📝
- `rating` 📝
- `reading_status` 📝
- `related_docs` 📝

### 极少使用字段 (功能未完善)

- `summary_one_sentence` ⚠️ (DeepSeek 目前只生成 100 字摘要)
- `search_strategy` ⚠️ (Query Router 根据查询自动选择，条目级配置未使用)

---

## ⚠️ 已知问题和改进建议

### 问题 1: 缺少必填字段验证

**问题描述**: `title` 和 `source_type` 声明为必填，但没有运行时验证
**影响范围**: 可能创建无效的 Entry 对象
**优先级**: 中
**建议修复**: 在 `__post_init__` 中添加验证逻辑

---

### 问题 2: `keywords` 字段类型不一致

**问题描述**: Entry 定义为 `list`，但 SQLite 存储为逗号分隔字符串
**影响范围**: `StoreStep` 需要手动转换类型
**优先级**: 低
**建议修复**: 统一为逗号分隔字符串，或在 SQLite 中使用 JSON 类型

**当前代码** (`src/workflow/steps/store_step.py`):
```python
# 手动转换列表为字符串
keywords_str = ",".join(entry.keywords) if entry.keywords else ""
```

---

### 问题 3: `summary_one_sentence` 未被使用

**问题描述**: 字段定义了但 DeepSeek 只生成 `summary_100_words`
**影响范围**: 字段始终为空字符串
**优先级**: 低
**建议**: 移除字段或实现一句话摘要功能

---

### 问题 4: `search_strategy` 语义模糊

**问题描述**: 字段存在但检索引擎根据查询动态决策，条目级配置未使用
**影响范围**: 字段无实际作用
**优先级**: 低
**建议**:
- 方案 A: 移除该字段（推荐）
- 方案 B: 实现条目级检索策略覆盖

---

### 问题 5: `related_docs` 元素类型未定义

**问题描述**: 列表元素类型不明确（文件路径？knowledge_id？URL？）
**影响范围**: 无法自动关联相关文档
**优先级**: 低
**建议**: 定义为 `list[str]` 并明确存储 `knowledge_id`

---

## 📝 使用示例

### 创建 Entry

```python
from src.storage.markdown_store import Entry

# 方式 1: 最小化创建
entry = Entry(
    title="测试文章",
    source_type="wechat",
    content="# 正文内容"
)

# 方式 2: 完整创建
entry = Entry(
    title="完整示例",
    source_type="zhihu",
    source_url="https://zhihu.com/p/123456",
    tags=["AI", "技术"],
    keywords=["深度学习", "向量检索"],
    abstract="这是一篇关于 AI 的文章",
    summary_100_words="详细摘要...",
    content="# 正文\n\n这里是内容..."
)
```

### 序列化与反序列化

```python
from src.storage.markdown_store import MarkdownStore
from pathlib import Path

# 保存
store = MarkdownStore(vault_dir=Path(".data/vault"))
file_path = store.save(entry, subdir="wechat")
# 文件路径: .data/vault/wechat/测试文章.md

# 加载
loaded_entry = store.load(file_path)
assert loaded_entry.title == entry.title
```

---

## 🎯 总结

### 设计优点

✅ 清晰的字段分组（元数据、内容、标注）
✅ 合理的默认值处理
✅ Front Matter 映射规则明确
✅ 自动计算字段（archived_at, word_count）

### 需要改进

⚠️ 缺少字段验证机制
⚠️ 部分字段未被实际使用
⚠️ 类型不一致问题（keywords）
⚠️ 缺少字段语义文档（related_docs）

---

**文档维护者**: AI Agent
**最后更新**: 2026-02-15
