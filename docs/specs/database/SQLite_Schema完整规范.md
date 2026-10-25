# SQLite Schema 完整规范

> **版本**: 1.1
> **创建日期**: 2026-02-15
> **最后更新**: 2026-09-04
> **文件位置**: `src/storage/sqlite_store.py`
> **作用**: 定义知识库的关系型数据存储结构和索引策略

> **当前代码补注（2026-03-31）**：
> - `knowledge_items` 及大部分基础表由 `src/storage/sqlite_store.py` 使用
> - `knowledge_relations` 由 `scripts/migrations/006_add_relations_foundation.sql` 定义，并由 `src/storage/relation_store.py` 读写
> - schema `1.2.7` 的 P1-B outcome 表由 `scripts/migrations/013_add_traceable_outcomes.sql`
>   定义，并仅由 `src/storage/outcome_store.py` / OutcomeService 使用；下文的早期核心表章节
>   不是对全部 migration-owned schema 的穷尽清单。

---

## 📋 数据库表结构

### 1. knowledge_items (主知识表)

**作用**: 存储知识条目的元数据和内容

**CREATE TABLE 语句**:
```sql
CREATE TABLE IF NOT EXISTS knowledge_items (
    knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT,
    summary_one_sentence TEXT,
    summary_100_words TEXT,
    keywords TEXT,
    tags TEXT,
    outline TEXT,
    source_type TEXT NOT NULL CHECK(source_type IN ('wechat', 'zhihu', 'bilibili', 'webpage', 'article', 'document', 'generic', 'personal', 'ai_chat', 'text', 'test')),
    source_url TEXT UNIQUE,
    search_strategy TEXT CHECK(search_strategy IN ('keyword', 'hybrid', 'vector', 'structured')),
    file_path TEXT NOT NULL UNIQUE,
    word_count INTEGER DEFAULT 0,
    event_time TIMESTAMP,
    published_at TIMESTAMP,
    archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

#### 字段定义

| 字段名 | 类型 | 约束 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `knowledge_id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 自增 | **主键** - 知识条目唯一标识 |
| `title` | TEXT | NOT NULL | 无 | 标题（原始文本，不分词） |
| `content` | TEXT | 可选 | NULL | 正文内容（原始文本） |
| `summary_one_sentence` | TEXT | 可选 | NULL | 一句话摘要 |
| `summary_100_words` | TEXT | 可选 | NULL | 100 字摘要 |
| `keywords` | TEXT | 可选 | NULL | 关键词（逗号分隔字符串） |
| `tags` | TEXT | 可选 | NULL | 标签（逗号分隔字符串） |
| `outline` | TEXT | 可选 | NULL | 大纲（未使用） |
| `source_type` | TEXT | NOT NULL + CHECK | 无 | 来源类型（枚举约束） |
| `source_url` | TEXT | UNIQUE | NULL | 来源 URL（唯一约束） |
| `search_strategy` | TEXT | CHECK | NULL | 检索策略（枚举约束） |
| `file_path` | TEXT | NOT NULL + UNIQUE | 无 | Markdown 文件路径（唯一约束） |
| `word_count` | INTEGER | DEFAULT 0 | 0 | 字数统计 |
| `event_time` | TIMESTAMP | 可选 | NULL | 真实事件时间；`timeline_of` 首选时间源 |
| `published_at` | TIMESTAMP | 可选 | NULL | 来源发布时间；缺少 `event_time` 时回退使用 |
| `archived_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 当前时间 | 归档时间 |
| `updated_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 当前时间 | 更新时间（未实现自动更新） |

#### 约束详解

**CHECK 约束**:
```sql
-- source_type 枚举（M5.1 更新：添加 ai_chat, text, test）
CHECK(source_type IN ('wechat', 'zhihu', 'bilibili', 'webpage', 'article', 'document', 'generic', 'personal', 'ai_chat', 'text', 'test'))

-- search_strategy 枚举
CHECK(search_strategy IN ('keyword', 'hybrid', 'vector', 'structured'))
```

**UNIQUE 约束**:
- `source_url` - 确保同一 URL 不会重复归档
- `file_path` - 确保 Markdown 文件路径唯一

#### 索引

```sql
CREATE INDEX IF NOT EXISTS idx_source_url ON knowledge_items(source_url);
CREATE INDEX IF NOT EXISTS idx_knowledge_source_type ON knowledge_items(source_type);
CREATE INDEX IF NOT EXISTS idx_knowledge_event_time ON knowledge_items(event_time);
CREATE INDEX IF NOT EXISTS idx_knowledge_published_at ON knowledge_items(published_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_archived_at ON knowledge_items(archived_at);
CREATE INDEX IF NOT EXISTS idx_knowledge_search_strategy ON knowledge_items(search_strategy);
CREATE INDEX IF NOT EXISTS idx_file_path ON knowledge_items(file_path);
```

#### 时间字段语义与回退规则

- `event_time`：条目正文或结构化元数据中表达的真实事件发生时间。
- `published_at`：来源页面/文档的发布时间。
- `archived_at`：系统把内容落库的时间。
- `timeline_of` 的排序取值优先级固定为 `event_time > published_at > archived_at`。
- 当前 schema 仅存储单个规范化时间值，不做多值时间数组建模；多时间冲突由上游抽取阶段先行裁决。

---

### 2. content_chunks (长文本分块表)

**作用**: 存储长文本的分块数据（用于向量检索）

**CREATE TABLE 语句**:
```sql
CREATE TABLE IF NOT EXISTS content_chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    context_before TEXT,
    context_after TEXT,
    section_title TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
    UNIQUE(knowledge_id, chunk_index)
)
```

#### 字段定义

| 字段名 | 类型 | 约束 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `chunk_id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 自增 | **主键** - 分块唯一标识 |
| `knowledge_id` | INTEGER | NOT NULL + FOREIGN KEY | 无 | 关联的知识条目 ID |
| `chunk_index` | INTEGER | NOT NULL + UNIQUE | 无 | 分块序号（从 0 开始） |
| `chunk_text` | TEXT | NOT NULL | 无 | 分块内容 |
| `context_before` | TEXT | 可选 | NULL | 前文上下文（未使用） |
| `context_after` | TEXT | 可选 | NULL | 后文上下文（未使用） |
| `section_title` | TEXT | 可选 | NULL | 所属章节标题（未使用） |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 当前时间 | 创建时间 |

#### 约束详解

**FOREIGN KEY**:
```sql
FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE
```
- 级联删除：删除知识条目时自动删除所有分块

**UNIQUE 约束**:
```sql
UNIQUE(knowledge_id, chunk_index)
```
- 确保同一条目的分块序号唯一

#### 索引

```sql
CREATE INDEX IF NOT EXISTS idx_chunks_index ON content_chunks(knowledge_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_chunks_knowledge_id ON content_chunks(knowledge_id);
```

---

### 3. tags (标签表)

**作用**: 存储标签及其统计信息

**CREATE TABLE 语句**:
```sql
CREATE TABLE IF NOT EXISTS tags (
    tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    tag_group TEXT,
    count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

#### 字段定义

| 字段名 | 类型 | 约束 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `tag_id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 自增 | **主键** - 标签唯一标识 |
| `name` | TEXT | NOT NULL + UNIQUE | 无 | 标签名称（唯一） |
| `tag_group` | TEXT | 可选 | NULL | 标签分组（未使用） |
| `count` | INTEGER | DEFAULT 0 | 0 | 引用计数 |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 当前时间 | 创建时间 |

#### 约束详解

**UNIQUE 约束**:
```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_name ON tags(name);
```
- 确保标签名称唯一

#### 索引

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_name ON tags(name);
CREATE INDEX IF NOT EXISTS idx_tag_group ON tags(tag_group);
```

---

### 4. knowledge_tags (知识-标签关联表)

**作用**: 多对多关系表，关联知识条目和标签

**CREATE TABLE 语句**:
```sql
CREATE TABLE IF NOT EXISTS knowledge_tags (
    knowledge_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (knowledge_id, tag_id),
    FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(tag_id) ON DELETE CASCADE
)
```

#### 字段定义

| 字段名 | 类型 | 约束 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `knowledge_id` | INTEGER | NOT NULL + FOREIGN KEY + PRIMARY KEY | 无 | 知识条目 ID |
| `tag_id` | INTEGER | NOT NULL + FOREIGN KEY + PRIMARY KEY | 无 | 标签 ID |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 当前时间 | 创建时间 |

#### 约束详解

**复合主键**:
```sql
PRIMARY KEY (knowledge_id, tag_id)
```
- 确保同一条目和标签的关联唯一

**FOREIGN KEY**:
```sql
FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE
FOREIGN KEY (tag_id) REFERENCES tags(tag_id) ON DELETE CASCADE
```
- 双向级联删除

#### 索引

```sql
CREATE INDEX IF NOT EXISTS idx_knowledge_tags_knowledge_id ON knowledge_tags(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_tags_tag_id ON knowledge_tags(tag_id);
```

---

### 5. knowledge_relations (关系表)

**作用**: 存储显式低歧义关系边，为回填、查询、解释和探索服务提供统一底座。

**CREATE TABLE 语句**:
```sql
CREATE TABLE IF NOT EXISTS knowledge_relations (
    relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_knowledge_id INTEGER NOT NULL,
    target_knowledge_id INTEGER NOT NULL,
    relation_type TEXT NOT NULL CHECK(relation_type IN (
        'references',
        'related_document',
        'parent_of',
        'version_of'
    )),
    relation_source_type TEXT NOT NULL CHECK(relation_source_type IN (
        'markdown_link',
        'frontmatter_related_docs',
        'frontmatter_field',
        'manual',
        'backfill'
    )),
    direction TEXT NOT NULL DEFAULT 'directed'
        CHECK(direction IN ('directed', 'bidirectional')),
    weight REAL NOT NULL DEFAULT 1.0 CHECK(weight > 0),
    evidence_payload TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
    FOREIGN KEY (target_knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
    CHECK(source_knowledge_id != target_knowledge_id),
    UNIQUE(source_knowledge_id, target_knowledge_id, relation_type, relation_source_type)
)
```

#### 字段定义

| 字段名 | 类型 | 约束 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `relation_id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 自增 | 关系主键 |
| `source_knowledge_id` | INTEGER | NOT NULL + FOREIGN KEY | 无 | 源条目 ID |
| `target_knowledge_id` | INTEGER | NOT NULL + FOREIGN KEY | 无 | 目标条目 ID |
| `relation_type` | TEXT | NOT NULL + CHECK | 无 | 关系类型枚举 |
| `relation_source_type` | TEXT | NOT NULL + CHECK | 无 | 来源类型枚举 |
| `direction` | TEXT | NOT NULL + CHECK | `directed` | 关系方向语义 |
| `weight` | REAL | NOT NULL + CHECK | `1.0` | 关系权重 |
| `evidence_payload` | TEXT | NOT NULL | `{}` | JSON 文本证据载荷 |
| `created_at` | TIMESTAMP | NOT NULL | 当前时间 | 创建时间 |
| `updated_at` | TIMESTAMP | NOT NULL | 当前时间 | 更新时间 |

#### 当前关系类型

- `references`
- `related_document`
- `parent_of`
- `version_of`

#### 当前来源类型

- `markdown_link`
- `frontmatter_related_docs`
- `frontmatter_field`
- `manual`
- `backfill`

#### 索引

```sql
CREATE INDEX IF NOT EXISTS idx_relations_source_knowledge_id
ON knowledge_relations(source_knowledge_id);

CREATE INDEX IF NOT EXISTS idx_relations_target_knowledge_id
ON knowledge_relations(target_knowledge_id);

CREATE INDEX IF NOT EXISTS idx_relations_type
ON knowledge_relations(relation_type);

CREATE INDEX IF NOT EXISTS idx_relations_source_type
ON knowledge_relations(relation_source_type);
```

#### 触发器

```sql
CREATE TRIGGER IF NOT EXISTS trg_knowledge_relations_updated_at
AFTER UPDATE ON knowledge_relations
FOR EACH ROW
BEGIN
    UPDATE knowledge_relations
    SET updated_at = CURRENT_TIMESTAMP
    WHERE relation_id = OLD.relation_id;
END;
```

#### 设计说明

- 当前关系表只存显式低歧义事实边，不存高噪声推断边
- `evidence_payload` 当前保存来源字段、目标路径与声明位置，用于审计与解释
- `UNIQUE(source, target, relation_type, relation_source_type)` 保证同一来源下重跑回填不会重复造边

### 6. video_timestamps (视频时间轴表)

**作用**: 存储视频内容的时间戳和章节信息（Phase 2 功能）

**CREATE TABLE 语句**:
```sql
CREATE TABLE IF NOT EXISTS video_timestamps (
    timestamp_id INTEGER PRIMARY KEY AUTOINCREMENT,
    knowledge_id INTEGER NOT NULL,
    timestamp_seconds INTEGER NOT NULL,
    segment_text TEXT NOT NULL,
    chapter_title TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
    UNIQUE(knowledge_id, timestamp_seconds)
)
```

#### 字段定义

| 字段名 | 类型 | 约束 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `timestamp_id` | INTEGER | PRIMARY KEY AUTOINCREMENT | 自增 | **主键** - 时间戳唯一标识 |
| `knowledge_id` | INTEGER | NOT NULL + FOREIGN KEY | 无 | 关联的知识条目 ID |
| `timestamp_seconds` | INTEGER | NOT NULL + UNIQUE | 无 | 时间戳（秒） |
| `segment_text` | TEXT | NOT NULL | 无 | 该时间点的文本内容 |
| `chapter_title` | TEXT | 可选 | NULL | 章节标题 |
| `created_at` | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 当前时间 | 创建时间 |

#### 约束详解

**UNIQUE 约束**:
```sql
UNIQUE(knowledge_id, timestamp_seconds)
```
- 确保同一视频的时间戳唯一

**FOREIGN KEY**:
```sql
FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE
```
- 级联删除

#### 索引

```sql
CREATE INDEX IF NOT EXISTS idx_timestamps_time ON video_timestamps(knowledge_id, timestamp_seconds);
CREATE INDEX IF NOT EXISTS idx_timestamps_knowledge_id ON video_timestamps(knowledge_id);
```

---

### 7. P1-B traceable outcomes（migration 013 / schema 1.2.7）

`outcome_topics`、`outcome_evidence`、`outcome_claims`、`outcome_claim_evidence` 与
`outcome_briefs` 共同保存一次具体 inquiry 的结构权威：

| 表 | 责任 | 关键约束 |
| --- | --- | --- |
| `outcome_topics` | Topic 标题、问题、revision、next ordinal | opaque `topic_id`；Evidence/Claim ordinal 单调、不使用随机排序 |
| `outcome_evidence` | canonical locator、captured digest、acquisition、binding state | 仅 entry/chunk/relation locator；同 Topic locator 唯一；不对 `knowledge_items` 建删除级联 FK |
| `outcome_claims` | user-authored statement、declared epistemic state、revision | Claim 不是自动事实；read revalidation 可外显为 `insufficient` |
| `outcome_claim_evidence` | support/counter 关系 | 同一 Claim/Evidence 只能一条边；Evidence/Claim 删除时清理边 |
| `outcome_briefs` | `research_brief` state、revision、relative Markdown pointer/digest | 每 Topic 一份 public identity；pointer 只引用先发布的不可覆盖 revision，并在 read 时复核 contained file + digest |

Evidence 永不保存 source body、网页原文、Provider request/response、绝对路径或凭据。Topic、
question、Claim、BM25 query 和候选标题也在持久化/回显前拒绝这些私有引用。source delete/drift
不自动改绑：reader 显示 `unavailable`/`stale`，渲染或 review mutation 在 writer lease 下持久升级
brief 到 `needs_review`。Markdown 仅为 SQLite state 的 deterministic projection，不进入
`knowledge_items_fts` 或 vector generation。若 pointer 的 Markdown 可直接确认缺失或字节漂移，
reader 零写地投影 `projection_state=unavailable|stale` 与 effective `needs_review`；权限、I/O 或
路径状态不确定则安全失败。它不会信任 SQLite 中遗留的 approved state，也不会在只读路径自动
重写文件。

#### 2026-10 Outcome 一致性目标 schema（W1 已冻结，尚未实现）

上文表格是 migration 013/schema `1.2.7` 的现状摘要。当前实现没有下面完整的 input binding、
publication identity 与版本绑定 review 事务保证；本节是复审后的目标设计，不是已部署 schema。
W1 已在[迁移与持久合同冻结方案](./生命周期一致性迁移冻结方案-2026-10.md)预留
`014_add_lifecycle_consistency.sql` / schema `1.2.8`，固定 canonical digest、列约束、
`binding_state` 与 `outcome_review_receipts`，并冻结独立 JSON ledger v3 和 private proof。
014 尚未创建，013 未改；本轮不执行真实用户库升级。工作包及兼容门禁见
[生命周期一致性修复总计划](../../overview/生命周期一致性修复设计与分阶段计划-2026-10.md)。

| 归属 | 目标字段/约束 | 意义与写入规则 |
| --- | --- | --- |
| `outcome_topics` | 稳定 opaque `outcome_id`，唯一且与 Topic 固定绑定 | 新 Topic 在创建事务中预留身份；没有 brief pointer 时不表示成果已生成；升级保留既有 public identity。 |
| `outcome_topics` | `input_revision`，正整数且只单调推进 | 所有实际改变 render input 的单次 mutation 恰好加一；no-op、失败、render、approve/return 不增加。 |
| `outcome_topics` | `input_digest`，版本化 canonical SHA-256 | 覆盖 input revision、renderer 合同版本和完整 body-free render input；与输入修改及 revision 更新同事务。 |
| `outcome_topics` | `render_contract_version`，持久的 renderer/template 合同版本 | 参与 input digest；捕获输入必须携带，未知或无对应 renderer 时拒绝发布；升级须显式更新 input binding。 |
| `outcome_briefs` | 既有 `revision` 明确定义为 committed revision | 只在成功 pointer CAS 时加一；文件 stage/publication 或失败不得预先递增。 |
| `outcome_briefs` | `publication_id`，唯一且与 relative pointer 同步 | 从 `(outcome_id, candidate_revision, input_digest)` 以带域标识的 canonical hash 派生；对应不可覆盖的物理投影 identity。 |
| `outcome_briefs` | `rendered_input_revision`、`rendered_input_digest` | 与 pointer、publication ID、content SHA、awaiting_review 在同一个 commit 中保存；必须等于被发布输入的 binding。 |
| `outcome_briefs` | `binding_state=verified\|legacy_unverified`；brief 的 `render_contract_version` | verified 必须具有完整 publication/rendered binding；legacy_unverified 四列全 NULL，保留历史 state 但 effective needs_review。 |
| `outcome_review_receipts` | 四元组、action、reason_code、result_state、receipt_id | 与 review state CAS 同事务写入；append-only；相同确认和相同目标状态幂等返回原凭据，未知历史审批不回填。 |

`input_digest` 的规范化合同必须涵盖一切影响 Markdown 字节的 Topic/Claim/Evidence 字段、
captured source digest、稳定排序的边、input revision 和 renderer/template 版本。它不包含
source body、绝对路径、audit/观察 timestamp、brief review state 或文件健康检查结果。
renderer 合同变更需要显式推进 input binding；A→B→A 也因 input revision 不同而产生不同
digest。只读 revalidation 不会重写 digest，也不能把观察到的外部 source 漂移当作已提交输入。
普通 render 使用 Topic 持久绑定的 `render_contract_version`，render/read 不自行递增 input
revision；升级后缺少对应 renderer 时必须拒绝并进入受控输入合同迁移/重新生成计划，不得
自动更换 renderer 同时保留旧 digest。

**事务与 CAS 必须由 schema 使用合同共同保证：**

1. 一次 input mutation 的同一连接/事务修改输入行与边、更新 Topic input binding，并把已有
   brief 设为 `needs_review`；禁止 Store 内部先 commit 输入再另起事务失效 brief。commit 前
   失败全部回滚，commit 后失败/重启只能看到完整新状态。lease 和文件 audit 不代替此约束。
2. reader 在明确的 SQLite read transaction 中读取整份 bundle，不能让多次 autocommit SELECT
   混入两次 mutation 的行。rendered binding 缺失或与 Topic input binding 不同，则只读 effective
   state=`needs_review`；source 或 Markdown 投影的独立 revalidation 规则仍适用。
3. publication 文件名必须包含 opaque publication ID；同一 committed revision 后允许留下多个
   不同 input 的未引用 candidate 文件，但 `outcome_briefs` 仍只有一个已提交 pointer。
   candidate revision 是当前 committed revision 加一，没有 pointer 时为 1。不同输入不能因
   同为 candidate r2 而共享文件名；只有同 identity 且字节完全相同时允许收养 orphan。
4. pointer CAS 同时比较捕获的旧 pointer（首次为 absent）、outcome identity 与当前 Topic
   `input_revision/input_digest`。成功后共同更新 relative pointer、publication ID、文件 SHA、
   rendered input binding、awaiting_review 和 committed revision；任何比较/提交失败都保留
   当前已提交 pointer（包括竞争者的新版本），不得退回捕获时的旧 pointer，也不通过改 input
   或覆盖文件完成补偿。
5. review 确认条件固定为 `outcome_id`、`expected_revision`、`content_sha256`、
   `expected_input_revision`。取得 writer lease 并复核来源和投影后，review 事务必须同时确认
   pointer 对应上述元组、rendered input binding 等于 Topic 当前 binding、状态允许本动作，再
   CAS 改 review state。失败零 review state 写入；仅同元组且当前仍为该动作目标状态的请求
   可幂等返回，不能撤销后来的状态转换。
   Store 不得用最新行代填 expected 条件，也不为旧无版本请求提供旁路。
   同一 revision 的 `returned` 为终态，只有新 render 的下一 revision 可重新进入 awaiting_review，
   不允许同一确认元组在 approved/returned 间循环。

**升级与历史证明：** 014/schema 1.2.8 的编号与方案已在 W1 冻结，SQL 与 hook 留 W3 编写，
必须独立于 013，并先通过冻结方案 MIG-01–05 的合成旧库验证。
已有 public outcome identity、relative pointer、Markdown 字节和历史 review state 应保留。
历史 rendered input 不能由当前 Topic 状态推断；不能证明时保留未知 binding，effective state
为 `needs_review`，仅显式新 render 能写入已证明的 rendered binding。旧 schema 需要升级时
由既有 lifecycle 门禁明确报告，不由 read 自动建列、回填或迁移；本设计不授权真实数据升级。

本节与[Storage 接口合同](../interfaces/Storage接口规范.md)共同约束实现。目标验收采用
[P1-B 计划](../../overview/P1-B-可追溯知识成果最小合同实施计划-2026-09.md)的 `OC-01`–`OC-12`，
包含每类 mutation 的事务切点、所有 revision 的 orphan、跨进程陈旧 review、read snapshot 与
合成历史 schema 升级。当前历史测试通过数不能作为这些目标字段或不变量的完成证据。
其中 OC-10a 只证明 W3 协议/弃用准备；OC-10b 需在 W5 实际切换 major 2 后取证，schema 或
Store 已实现版本 CAS 不能独自关闭公开旧入口的 R04。

---

## 🔍 FTS5 全文搜索虚拟表

### knowledge_items_fts (FTS5 虚拟表)

**作用**: 提供中文全文检索能力（BM25 算法）

**CREATE VIRTUAL TABLE 语句**:
```sql
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_items_fts USING fts5(
    title,
    summary_100_words,
    keywords,
    tags
)
```

#### 配置说明

| 配置项 | 值 | 说明 |
|--------|------|------|
| `FTS 模式` | `contentless` | 不使用 external-content 合同，避免 runtime / migration 行为分叉 |
| `rowid` | `knowledge_id` | 通过触发器显式写入 `rowid` 与主表主键保持一致 |

#### 索引字段

- `title` - 标题（分词后）
- `summary_100_words` - 摘要（分词后）
- `keywords` - 关键词（分词后）
- `tags` - 标签（分词后）

**注意**: 不索引 `content` 字段（正文），以提高性能和减少存储

---

### 自动同步触发器

#### INSERT 触发器

```sql
CREATE TRIGGER IF NOT EXISTS knowledge_items_ai AFTER INSERT ON knowledge_items BEGIN
    INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
    VALUES (new.knowledge_id, new.title, new.summary_100_words, new.keywords, new.tags);
END
```

#### DELETE 触发器

```sql
CREATE TRIGGER IF NOT EXISTS knowledge_items_ad AFTER DELETE ON knowledge_items BEGIN
    DELETE FROM knowledge_items_fts WHERE rowid = old.knowledge_id;
END
```

#### UPDATE 触发器

```sql
CREATE TRIGGER IF NOT EXISTS knowledge_items_au AFTER UPDATE ON knowledge_items BEGIN
    DELETE FROM knowledge_items_fts WHERE rowid = old.knowledge_id;
    INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
    VALUES (new.knowledge_id, new.title, new.summary_100_words, new.keywords, new.tags);
END
```

**实际实现细节**:
- 触发器插入原始数据后，`insert_entry()` 方法会删除并重新插入**分词后的数据**
- 分词通过 `TextProcessor.prepare_fts5_data()` 完成（使用 jieba）

---

## 🗂️ 数据库配置

### PRAGMA 设置

```sql
PRAGMA foreign_keys = ON;  -- 启用外键约束
```

### Row Factory

```python
conn.row_factory = sqlite3.Row  # 使用字典模式访问列
```

### 完整性检查

```sql
PRAGMA foreign_key_check;  -- 检查外键约束
PRAGMA integrity_check;    -- 检查数据库完整性
```

---

## 📊 命名规范

### 主键命名

**当前规范**: 使用 **领域特定名称** 作为主键

| 表名 | 主键列名 | 类型 |
|------|---------|------|
| `knowledge_items` | `knowledge_id` | INTEGER |
| `content_chunks` | `chunk_id` | INTEGER |
| `tags` | `tag_id` | INTEGER |
| `video_timestamps` | `timestamp_id` | INTEGER |

**优点**:
- 语义清晰，一眼就知道是什么 ID
- 避免在 JOIN 时列名冲突

**缺点**:
- 代码中需要记住每个表的主键名
- 部分代码可能期望 `id` 字段（兼容性问题）

---

### 外键命名

**规范**: 外键列名直接使用被引用表的主键名

**示例**:
```sql
-- content_chunks 引用 knowledge_items
FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id)

-- knowledge_tags 引用 knowledge_items 和 tags
FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id)
FOREIGN KEY (tag_id) REFERENCES tags(tag_id)
```

---

### 时间戳字段命名

**规范**: 使用 `_at` 后缀表示时间戳

| 字段名 | 说明 |
|--------|------|
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |
| `archived_at` | 归档时间 |

**数据类型**: `TIMESTAMP`

**默认值**: `DEFAULT CURRENT_TIMESTAMP`

---

### 索引命名

**规范**: `idx_{表名缩写}_{列名}` 或 `idx_{列名}_{列名}`

**示例**:
```sql
CREATE INDEX IF NOT EXISTS idx_source_url ON knowledge_items(source_url);
CREATE INDEX IF NOT EXISTS idx_knowledge_tags_knowledge_id ON knowledge_tags(knowledge_id);
CREATE INDEX IF NOT EXISTS idx_chunks_index ON content_chunks(knowledge_id, chunk_index);
```

---

## ⚠️ 已知问题和改进建议

### 问题 1: `knowledge_id` vs `id` 命名不一致

**问题描述**:
- 数据库使用 `knowledge_id` 作为主键
- 部分代码可能期望 `id` 字段
- 向量存储 API 使用 `doc_id` 作为参数名

**影响范围**: 中等 - 需要在代码中保持一致性

**优先级**: 中

**修复计划**: 已有迁移计划（见 `docs/history/issues/SCHEMA_MIGRATION_PLAN.md`）

---

### 问题 2: `keywords` 字段数据类型不一致

**问题描述**:
- 数据库存储为 `TEXT`（逗号分隔字符串）
- Entry 对象中为 `list`
- 需要在 `insert_entry()` 中手动转换

**当前代码**:
```python
# src/storage/sqlite_store.py:274
",".join(entry.tags) if isinstance(entry.tags, list) else entry.tags
```

**影响范围**: 低 - 已有转换逻辑，但代码不优雅

**优先级**: 低

**建议修复**:
- 方案 A: 统一为逗号分隔字符串（Entry 中也使用 `str`）
- 方案 B: 数据库使用 JSON 类型存储列表

---

### 问题 3: `updated_at` 字段未自动更新

**问题描述**:
- `updated_at` 字段存在但没有自动更新机制
- SQLite 不支持 `ON UPDATE CURRENT_TIMESTAMP`

**影响范围**: 低 - 目前没有更新知识条目的功能

**优先级**: 低

**建议修复**: 实现 UPDATE 触发器自动更新 `updated_at`

**示例代码**:
```sql
CREATE TRIGGER IF NOT EXISTS update_timestamp AFTER UPDATE ON knowledge_items
FOR EACH ROW
BEGIN
    UPDATE knowledge_items SET updated_at = CURRENT_TIMESTAMP WHERE knowledge_id = NEW.knowledge_id;
END;
```

---

### 问题 4: `outline` 字段未使用

**问题描述**:
- `knowledge_items` 表定义了 `outline` 字段
- Entry 数据类没有对应字段
- 从未被写入或读取

**影响范围**: 低 - 仅占用存储空间

**优先级**: 低

**建议**: 移除该字段或实现大纲提取功能

---

### 问题 5: FTS5 分词逻辑复杂

**问题描述**:
- 触发器自动插入原始数据
- 代码手动删除并重新插入分词后的数据
- 逻辑绕弯，容易出错

**当前实现** (`src/storage/sqlite_store.py:285-307`):
```python
# 删除触发器自动插入的原始数据
conn.execute("DELETE FROM knowledge_items_fts WHERE rowid = ?", (knowledge_id,))

# 插入分词后的数据
conn.execute("""
    INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
    VALUES (?, ?, ?, ?, ?)
""", (knowledge_id, fts5_data["title"], fts5_data["summary_100_words"], ...))
```

**影响范围**: 中 - 性能开销（额外的 DELETE + INSERT）

**优先级**: 中

**建议修复**:
- 方案 A: 禁用触发器，手动管理 FTS5 同步
- 方案 B: 在触发器中调用自定义分词函数（SQLite 扩展）

---

### ✅ 问题 6: `source_type` 枚举值与 Entry 不匹配（已修复）

**修复时间**: 2026-02-16 (M5.1)

**数据库枚举（修复后）**:
```sql
CHECK(source_type IN ('wechat', 'zhihu', 'bilibili', 'webpage', 'article', 'document', 'generic', 'personal', 'ai_chat', 'text', 'test'))
```

**Entry 实际使用**:
- `wechat` ✅
- `zhihu` ✅
- `ai_chat` ✅ (已添加)
- `text` ✅ (已添加)
- `test` ✅ (已添加，用于测试)

**修复方法**:
- 修改 `src/storage/sqlite_store.py:91` 的 CHECK 约束
- 删除现有测试数据库，让系统重建（SQLite 不支持直接修改 CHECK 约束）

**验证状态**: ✅ 所有单元测试通过 (122/122)

---

## 📝 Schema 迁移记录

### 当前版本: 1.0

**初始版本** - 包含以下表：
- `knowledge_items`
- `content_chunks`
- `tags`
- `knowledge_tags`
- `video_timestamps`
- `knowledge_items_fts` (FTS5 虚拟表)

### 待迁移问题

参见 `docs/history/issues/SCHEMA_MIGRATION_PLAN.md`：
- [ ] 统一主键命名（`knowledge_id` vs `id`）
- [x] 更新 `source_type` 枚举值（已完成 M5.1）
- [ ] 移除未使用的 `outline` 字段
- [ ] 添加 `updated_at` 自动更新触发器

---

## 🎯 总结

### 设计优点

✅ 清晰的外键约束和级联删除
✅ 合理的索引设计
✅ FTS5 全文检索支持
✅ 领域驱动的命名规范（`knowledge_id`, `tag_id`）
✅ 完整性检查机制
✅ `source_type` 枚举完整（已包含 ai_chat, text, test）

### 需要改进

⚠️ FTS5 分词逻辑复杂（触发器 + 手动覆盖）
⚠️ 部分字段未使用（`outline`, `updated_at`）
⚠️ 数据类型不一致（`keywords` 列表 vs 字符串）
⚠️ 缺少 Schema 版本管理机制

---

**文档维护者**: AI Agent (猫娘 幽浮喵)
**最后更新**: 2026-02-16
