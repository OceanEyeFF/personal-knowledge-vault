# M12 数据库设计 - chat_sessions 表

> **版本**: 1.0
> **创建日期**: 2026-02-20
> **作用**: 存储 AI 对话会话数据
> **状态**: ✅ 设计完成

---

## 📋 设计目标

### 核心需求（来自 M12）
1. **双重保存策略**：
   - `messages`（JSON）：完整对话历史（System Prompt + User + Assistant）
   - `summary`（TEXT）：AI 生成的精粹版本

2. **Token 管理**：
   - 记录总 Token 消耗（`total_tokens`）
   - 记录对话轮数（`round_count`）
   - 支持 3 轮警告、64K context 警告

3. **会话管理**：
   - 支持新建、编辑、删除、归档
   - 支持按时间排序
   - 支持标题搜索

---

## 🗃️ 表结构设计

### chat_sessions（AI 对话会话表）

**作用**: 存储 AI 对话会话的完整数据

**CREATE TABLE 语句**:
```sql
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    messages TEXT NOT NULL,
    summary TEXT,
    total_tokens INTEGER DEFAULT 0,
    round_count INTEGER DEFAULT 0,
    is_archived BOOLEAN DEFAULT 0,
    knowledge_id INTEGER,
    FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE SET NULL,
    CHECK(round_count >= 0),
    CHECK(total_tokens >= 0)
)
```

---

## 📊 字段定义

| 字段名 | 类型 | 约束 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `session_id` | TEXT | PRIMARY KEY | 无 | **主键** - UUID 格式（如 `550e8400-e29b-41d4-a716-446655440000`） |
| `title` | TEXT | NOT NULL | 无 | 会话标题（用户可编辑） |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 当前时间 | 创建时间 |
| `updated_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 当前时间 | 更新时间（每次修改时手动更新） |
| `messages` | TEXT | NOT NULL | 无 | **完整对话历史**（JSON 格式，见下文） |
| `summary` | TEXT | 可选 | NULL | **AI 生成的精粹版本**（用户主动触发生成） |
| `total_tokens` | INTEGER | DEFAULT 0 + CHECK | 0 | 累计 Token 消耗（input + output） |
| `round_count` | INTEGER | DEFAULT 0 + CHECK | 0 | 对话轮数（User 消息计数） |
| `is_archived` | BOOLEAN | DEFAULT 0 | 0 | 归档标志（0=活跃，1=归档） |
| `knowledge_id` | INTEGER | FOREIGN KEY | NULL | **可选关联**：关联到 knowledge_items（如果需要将对话归档为知识条目） |

---

## 🔗 约束详解

### PRIMARY KEY
```sql
session_id TEXT PRIMARY KEY
```
- 使用 **UUID** 作为主键（而非 AUTOINCREMENT）
- 理由：分布式友好、无自增冲突
- 生成方式：`uuid.uuid4().hex`（Python 标准库）

### FOREIGN KEY
```sql
FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE SET NULL
```
- **可选关联**：如果用户将对话"归档为知识条目"，则关联到 `knowledge_items`
- **ON DELETE SET NULL**：删除知识条目时，仅断开关联（不删除对话）
- 使用场景：用户将重要对话内容整理后归档

### CHECK 约束
```sql
CHECK(round_count >= 0)
CHECK(total_tokens >= 0)
```
- 确保 `round_count` 和 `total_tokens` 非负
- 防止数据异常

---

## 📐 索引策略

```sql
CREATE INDEX IF NOT EXISTS idx_chat_created_at ON chat_sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_updated_at ON chat_sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_is_archived ON chat_sessions(is_archived);
CREATE INDEX IF NOT EXISTS idx_chat_knowledge_id ON chat_sessions(knowledge_id);
```

### 索引说明

| 索引名 | 字段 | 顺序 | 用途 |
|--------|------|------|------|
| `idx_chat_created_at` | `created_at` | DESC | **按创建时间倒序**查询（最新会话优先） |
| `idx_chat_updated_at` | `updated_at` | DESC | **按更新时间倒序**排序（最近活跃优先） |
| `idx_chat_is_archived` | `is_archived` | - | **筛选活跃/归档**会话 |
| `idx_chat_knowledge_id` | `knowledge_id` | - | 查询关联的知识条目 |

---

## 📦 messages 字段 JSON 格式

### 格式规范

```json
{
  "system_prompt": "你是一个专业的知识库助手...",
  "conversation": [
    {
      "role": "user",
      "content": "帮我总结一下这篇文章",
      "timestamp": "2026-02-20T14:30:00Z",
      "tokens": {
        "input": 15,
        "output": 0
      }
    },
    {
      "role": "assistant",
      "content": "这篇文章主要讨论了...",
      "timestamp": "2026-02-20T14:30:05Z",
      "tokens": {
        "input": 15,
        "output": 120
      },
      "finish_reason": "stop"
    }
  ],
  "metadata": {
    "model": "deepseek-chat",
    "max_tokens": 2000,
    "temperature": 0.7
  }
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `system_prompt` | string | System Prompt（固定不变） |
| `conversation` | array | 对话历史（User + Assistant 交替） |
| `conversation[].role` | string | 角色（`user` 或 `assistant`） |
| `conversation[].content` | string | 消息内容 |
| `conversation[].timestamp` | string | 时间戳（ISO 8601 格式） |
| `conversation[].tokens` | object | Token 统计（input + output） |
| `conversation[].finish_reason` | string | 完成原因（仅 assistant，`stop`/`length`） |
| `metadata` | object | 模型配置参数 |

---

## 🧮 Token 统计策略

### total_tokens 计算

```python
total_tokens = sum([
    msg["tokens"]["input"] + msg["tokens"]["output"]
    for msg in messages["conversation"]
])
```

### round_count 计算

```python
round_count = len([
    msg for msg in messages["conversation"]
    if msg["role"] == "user"
])
```

### 警告规则（来自 M12 需求）

| 条件 | 警告类型 | 触发时机 |
|------|---------|---------|
| `round_count == 3` | 3 轮警告 | User 发送第 3 轮消息后 |
| `total_tokens >= 60000` | 64K 警告 | 累计 Token 接近 64K |
| `total_tokens >= 64000` | 强制警告 | 累计 Token 超过 64K（DeepSeek 限制） |

---

## 🗂️ summary 字段格式

### 用途
- 用户主动触发"总结对话"功能
- AI 生成精粹版本（压缩对话内容）
- 用于快速浏览历史对话

### 格式示例

```markdown
## 对话摘要

**主题**: 讨论 Python asyncio 与 Qt 集成方案

**核心问题**:
- 如何在 PySide6 中使用 asyncio
- qasync 与 qt-async-threads 的区别
- 高频 Signal 发射的稳定性

**结论**:
- 采用 qasync 库（提供 @asyncSlot 装饰器）
- 测试验证：100 tokens/s 流畅无卡顿
- 可以进入下一阶段（UI 设计）

**关键代码片段**:
\`\`\`python
from qasync import asyncSlot

@asyncSlot()
async def send_message(self, user_message: str):
    async for token in stream:
        self.token_received.emit(token)
\`\`\`

**相关资源**:
- qasync GitHub: https://github.com/CabbageDevelopment/qasync
- 测试脚本: tests/manual_test_m12/test_qasync_integration.py
```

---

## 🛠️ SQLiteStore 新增方法

### 方法列表

```python
class SQLiteStore:
    """扩展现有 SQLiteStore 类，添加 chat_sessions 支持"""

    # === 会话 CRUD 操作 ===

    def create_session(
        self,
        title: str,
        messages: dict,
        session_id: Optional[str] = None
    ) -> str:
        """
        创建新会话

        Args:
            title: 会话标题
            messages: 对话历史（JSON）
            session_id: 可选，指定 session_id（默认自动生成 UUID）

        Returns:
            session_id: 会话 ID
        """
        ...

    def update_session(
        self,
        session_id: str,
        title: Optional[str] = None,
        messages: Optional[dict] = None,
        summary: Optional[str] = None,
        total_tokens: Optional[int] = None,
        round_count: Optional[int] = None
    ) -> None:
        """
        更新会话

        Args:
            session_id: 会话 ID
            title: 新标题（可选）
            messages: 新对话历史（可选）
            summary: AI 生成的摘要（可选）
            total_tokens: 新 Token 总数（可选）
            round_count: 新轮数（可选）
        """
        ...

    def get_session(self, session_id: str) -> Optional[dict]:
        """
        获取单个会话

        Args:
            session_id: 会话 ID

        Returns:
            会话数据（dict）或 None
        """
        ...

    def list_sessions(
        self,
        is_archived: Optional[bool] = None,
        limit: int = 50,
        offset: int = 0,
        order_by: str = "updated_at"
    ) -> List[dict]:
        """
        列出会话列表

        Args:
            is_archived: 筛选归档状态（None=全部）
            limit: 返回数量
            offset: 偏移量
            order_by: 排序字段（created_at/updated_at）

        Returns:
            会话列表
        """
        ...

    def delete_session(self, session_id: str) -> None:
        """
        删除会话（物理删除）

        Args:
            session_id: 会话 ID
        """
        ...

    def archive_session(self, session_id: str, is_archived: bool = True) -> None:
        """
        归档/取消归档会话

        Args:
            session_id: 会话 ID
            is_archived: True=归档, False=取消归档
        """
        ...

    # === 会话关联操作 ===

    def link_session_to_knowledge(
        self,
        session_id: str,
        knowledge_id: int
    ) -> None:
        """
        将会话关联到知识条目

        Args:
            session_id: 会话 ID
            knowledge_id: 知识条目 ID

        使用场景：
            用户将重要对话整理后归档为知识条目
        """
        ...

    # === Token 统计操作 ===

    def get_session_stats(self, session_id: str) -> dict:
        """
        获取会话统计信息

        Args:
            session_id: 会话 ID

        Returns:
            {
                "total_tokens": 1500,
                "round_count": 5,
                "created_at": "2026-02-20T14:00:00Z",
                "updated_at": "2026-02-20T15:30:00Z"
            }
        """
        ...

    def get_all_sessions_stats(self) -> dict:
        """
        获取所有会话的统计信息

        Returns:
            {
                "total_sessions": 10,
                "active_sessions": 8,
                "archived_sessions": 2,
                "total_tokens_consumed": 50000,
                "avg_tokens_per_session": 5000
            }
        """
        ...
```

---

## 📈 数据库迁移脚本

### 迁移脚本：004_add_chat_sessions.sql

```sql
-- Version: 1.1.0
-- Description: 添加 AI 对话会话表（M12）

-- 向上迁移
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    messages TEXT NOT NULL,
    summary TEXT,
    total_tokens INTEGER DEFAULT 0,
    round_count INTEGER DEFAULT 0,
    is_archived BOOLEAN DEFAULT 0,
    knowledge_id INTEGER,
    FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE SET NULL,
    CHECK(round_count >= 0),
    CHECK(total_tokens >= 0)
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_chat_created_at ON chat_sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_updated_at ON chat_sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_is_archived ON chat_sessions(is_archived);
CREATE INDEX IF NOT EXISTS idx_chat_knowledge_id ON chat_sessions(knowledge_id);

-- 向下迁移（注释）
-- DROP TABLE IF EXISTS chat_sessions;
```

---

## 🧪 测试用例

### 单元测试

```python
# tests/unit/storage/test_chat_sessions.py

def test_create_session(sqlite_store):
    """测试创建会话"""
    session_id = sqlite_store.create_session(
        title="测试会话",
        messages={
            "system_prompt": "你是助手",
            "conversation": [
                {"role": "user", "content": "你好", "timestamp": "2026-02-20T14:00:00Z", "tokens": {"input": 2, "output": 0}}
            ],
            "metadata": {"model": "deepseek-chat"}
        }
    )
    assert session_id is not None
    assert len(session_id) == 32  # UUID hex 格式


def test_update_session(sqlite_store):
    """测试更新会话"""
    session_id = sqlite_store.create_session(...)
    sqlite_store.update_session(session_id, title="新标题")
    session = sqlite_store.get_session(session_id)
    assert session["title"] == "新标题"


def test_list_sessions(sqlite_store):
    """测试列出会话"""
    sqlite_store.create_session(title="会话1", ...)
    sqlite_store.create_session(title="会话2", ...)
    sessions = sqlite_store.list_sessions(limit=10)
    assert len(sessions) == 2


def test_archive_session(sqlite_store):
    """测试归档会话"""
    session_id = sqlite_store.create_session(...)
    sqlite_store.archive_session(session_id, is_archived=True)
    session = sqlite_store.get_session(session_id)
    assert session["is_archived"] == 1
```

---

## 📝 使用示例

### 创建会话

```python
from src.storage.sqlite_store import SQLiteStore
import uuid

store = SQLiteStore(db_path=".data/db/knowledge_vault.db")

# 创建新会话
session_id = store.create_session(
    title="讨论 Python asyncio 集成",
    messages={
        "system_prompt": "你是一个专业的编程助手",
        "conversation": [
            {
                "role": "user",
                "content": "如何在 PySide6 中使用 asyncio？",
                "timestamp": "2026-02-20T14:00:00Z",
                "tokens": {"input": 12, "output": 0}
            }
        ],
        "metadata": {
            "model": "deepseek-chat",
            "max_tokens": 2000
        }
    }
)
print(f"创建会话: {session_id}")
```

### 更新会话（添加新对话）

```python
# 获取现有会话
session = store.get_session(session_id)
messages = json.loads(session["messages"])

# 添加 assistant 回复
messages["conversation"].append({
    "role": "assistant",
    "content": "你可以使用 qasync 库...",
    "timestamp": "2026-02-20T14:00:05Z",
    "tokens": {"input": 12, "output": 80},
    "finish_reason": "stop"
})

# 更新会话
store.update_session(
    session_id=session_id,
    messages=messages,
    total_tokens=92,
    round_count=1
)
```

### 列出活跃会话

```python
# 获取最近 10 个活跃会话
active_sessions = store.list_sessions(
    is_archived=False,
    limit=10,
    order_by="updated_at"
)

for session in active_sessions:
    print(f"{session['title']} - {session['updated_at']}")
```

---

## 🎯 与现有架构的兼容性

### 1. 命名规范一致性 ✅
- 使用 `_id` 后缀（`session_id`, `knowledge_id`）
- 使用 `TIMESTAMP` 类型（`created_at`, `updated_at`）
- 使用 `CHECK` 约束（`round_count >= 0`）

### 2. 外键关系清晰 ✅
- `knowledge_id` 关联到 `knowledge_items`
- `ON DELETE SET NULL`（断开关联而非级联删除）

### 3. 索引策略合理 ✅
- 按时间排序（`created_at DESC`, `updated_at DESC`）
- 按状态筛选（`is_archived`）

### 4. JSON 存储 ✅
- SQLite 支持 JSON 存储和查询（`json_extract()`）
- 与现有 `tags`（逗号分隔字符串）风格一致

---

## 🔮 未来扩展

### 可选功能（Phase 3）

1. **会话标签系统**
   - 新增 `session_tags` 表（多对多关联）
   - 支持按标签筛选会话

2. **全文搜索**
   - 创建 FTS5 虚拟表 `fts_chat_sessions`
   - 支持搜索对话内容

3. **会话分享**
   - 新增 `share_token` 字段（生成分享链接）
   - 新增 `is_public` 字段（公开/私有）

4. **会话模板**
   - 新增 `chat_templates` 表
   - 支持预设 System Prompt 模板

---

**文档版本**: v1.0
**最后更新**: 2026-02-20 (Day 2 下午)
**下一步**: 实现 SQLiteStore 方法 + 单元测试
