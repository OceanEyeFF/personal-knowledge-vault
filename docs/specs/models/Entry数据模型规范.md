# Entry 数据模型规范

> **版本**: 1.1
> **创建日期**: 2026-02-15
> **最后更新**: 2026-03-31
> **文件位置**: `src/storage/markdown_store.py`
> **作用**: 知识条目的核心数据结构，贯穿整个系统的数据流

> **当前代码补注（2026-03-31）**：
> - `Entry` dataclass 当前只内建 `related_docs`，不内建 `children` / `version_of`
> - `children` / `version_of` 当前属于原始 Markdown Front Matter 扩展字段，由 `src/relations/extractors.py` 直接解析
> - 当前自动回填只消费显式低歧义关系字段，不消费纯语义推断信号

---

## 📋 完整字段定义

### 基础元数据 (必填)

| 字段名 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `title` | `str` | ✅ | 无 | 知识条目标题，不能为空 |
| `source_type` | `str` | ✅ | 无 | 来源类型：`wechat`/`zhihu`/`bilibili`/`pdf`/`personal`/`ai_chat`/`text` |
| `source_url` | `Optional[str]` | ❌ | `None` | 来源 URL，可为空（如个人笔记） |
| `event_time` | `Optional[str]` | ❌ | `None` | 条目描述的事件发生时间；优先用于时间线排序 |
| `published_at` | `Optional[str]` | ❌ | `None` | 来源内容发布时间；当缺少 `event_time` 时回退使用 |
| `archived_at` | `Optional[str]` | ❌ | 当前时间 | 归档时间戳，格式：`YYYY-MM-DD HH:MM:SS` |

**约束规则**:
- `title` 不能为空字符串
- `source_type` 必须是预定义的类型之一
- `event_time` / `published_at` / `archived_at` 只保留单个规范化值；若传入列表，取第一个非空值
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
| `related_docs` | `list[str]` | ❌ | `[]` | 显式相关文档相对路径列表，对应 `related_document` |

**约束规则**:
- `related_docs` 当前约束为 `list[str]`
- 列表元素使用 vault 内相对路径字符串
- 当前自动回填会把 `related_docs` 抽取为 `related_document`

### Front Matter 扩展关系字段（非 Entry 标准字段）

下列字段当前**不属于** `Entry` dataclass 的标准属性，但关系层会直接从 Markdown 原文 Front Matter 中读取：

| 字段名 | 类型 | 当前自动映射 | 说明 |
|--------|------|--------------|------|
| `children` | `list[str]` | `parent_of` | 当前文档声明的子文档列表 |
| `version_of` | `str` | `version_of` | 当前文档的版本基线文档 |

说明：

- 这两个字段当前由 `src/relations/extractors.py` 通过 `parse_front_matter()` 直接解析
- `MarkdownStore.load()` / `MarkdownStore.save()` 当前不会把它们 round-trip 到 `Entry`
- 若后续要进入 `Entry` dataclass，必须先更新 `src/storage/markdown_store.py` 与相关工作流

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
event_time: "2026-02-13 20:00:00"
published_at: "2026-02-14 08:30:00"
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
- 时间字段回退优先级为 `event_time > published_at > archived_at`

---

## ✅ 字段验证规则

### 当前验证机制

**自动验证**:
1. `event_time` / `published_at` / `archived_at` 会被规整为单个字符串值
2. `archived_at` 自动填充当前时间（如果为 `None`）
3. `word_count` 自动计算（如果为 0 且有内容）

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
- `event_time` / `published_at` ⭕（仅在来源可解析出真实时间时填充）
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

### 问题 5: 关系字段当前只支持白名单与相对路径

**问题描述**: 关系层当前只对白名单字段 `related_docs` / `children` / `version_of` 执行自动抽取，且 `children` / `version_of` 还未进入 `Entry` dataclass
**影响范围**: `parent`、URL、`knowledge_id`、别名字段不会被自动回填；`MarkdownStore.save()` 也不会主动写出这两个扩展字段
**优先级**: 中
**建议**: 若后续需要扩展，必须先定义方向语义、证据结构和幂等清理合同

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
⚠️ 关系字段当前仍限于白名单，不支持别名自动映射

---

**文档维护者**: AI Agent
**最后更新**: 2026-02-15
