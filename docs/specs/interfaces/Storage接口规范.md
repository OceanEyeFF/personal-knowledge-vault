# Storage 接口规范（简化版）

> **版本**: 1.0
> **创建日期**: 2026-02-15
> **文件位置**: `src/storage/`
> **作用**: 知识库存储层的核心接口

---

## 📦 三大存储后端

| 存储后端 | 文件 | 作用 | 数据格式 |
|---------|------|------|---------|
| **MarkdownStore** | `markdown_store.py` | 人类可读的主存储 | YAML Front Matter + Markdown |
| **SQLiteStore** | `sqlite_store.py` | 元数据索引和 FTS5 全文搜索 | SQLite 数据库 |
| **VectorStore** | `vector_store.py` | 向量索引（语义搜索） | hnswlib 索引文件 |

---

## R4 内容提交与派生写入边界

schema `1.2.6` 新增的 `ingress_tasks`、`content_mutation_tasks`、handoff、Q2 derivation/
reservation/usage 表是私有 lifecycle ledger，不保存正文、API key 或 Provider 输出正文。原始
request、`PreparedDocument` 与 `DerivationPatch` 仅存在受身份/hash 校验的 private spool。

- Q1′/`StorageCoordinator` 是 Markdown + SQLite 内容与 patch 的唯一 writer，并保留
  operation-bound proof；Q0 crawler/parser 与 Q2 Provider 不得直接改写这些后端。
- Q2 在 current claim/fence、source/target revision、policy 与预算预留全部通过后才可运行，且
  Provider output 必须作为 immutable patch 回到 Q1′。unknown Provider usage 保持 SQL `NULL`。
- 启用 R4 内部自动化的产品路径不写历史 flat `VectorStore`；generation 的 staged publish 与
  ready pointer 由 embedding lifecycle 管理，非 ready binding 绝不可读取旧 flat artifact。

---

## P1-B 可追溯成果边界

W3/schema `1.2.8` 的 `outcome_*` 表保存 Topic、Evidence、Claim、Claim↔Evidence 边与
`research_brief` pointer。其 authority 与 R4 content ledger 分离：它不复制 Entry 正文、不进入
FTS/vector generation，也不改变 archive 的 Q0/Q1′/Q2 写入顺序。

- Evidence 仅保存受限 `pkv://entries/...`、`pkv://entries/.../chunks/...` 或
  `pkv://relations/...` locator、captured source revision digest、状态和 limitation；删除/漂移
  时不得自动改绑新 source。
- `OutcomeStore` 是内部 SQLite port，所有 mutation 都需要 `knowledge_outcomes` writer lease；
  它不迁移 schema、不解析 source、不读取正文或写 Markdown。
- `OutcomeService` 先将 deterministic、不可覆盖的 Markdown revision 发布到 layout-owned
  `outcomes/`，再将 relative path/digest 写入 `outcome_briefs`。reader 只跟随已提交 pointer，
  因而 pre-commit orphan 不会被误报为有效成果。
- source revalidation 在 read/render/review 中都不自行修改 input binding；持久输入状态变化
  须经显式 input mutation 原子推进版本和 brief 失效。全线无 Provider、Embedding、网络或隐藏 test mode。

### 2026-10 W3 一致性合同与实现边界

历史 schema `1.2.7` 存在输入 mutation 与 brief 失效分开提交、r2+ orphan 阻塞重试，以及 review
未绑定被审阅版本的缺口。W3 已实现下列 Store 事务、发布身份与新版本审阅协议；major-1 旧
无版本入口仍按兼容计划保留，不能据此宣称 R04 已关闭。后续边界由
[生命周期一致性修复总计划](../../overview/生命周期一致性修复设计与分阶段计划-2026-10.md)控制。

W1 的具体持久合同已冻结于
[迁移冻结方案](../database/生命周期一致性迁移冻结方案-2026-10.md)：014/schema 1.2.8、
canonical input/publication/receipt hash、完整列约束与合成迁移矩阵。公开签名与错误见
[v2 接口冻结合同](./生命周期一致性v2接口冻结合同-2026-10.md)。本次仅验证合成库，未迁移用户 Vault。

**结构门禁。** Store 不依赖调用方曾经 bootstrap；在当前连接的 read transaction 内复用迁移
校验，比较冻结 DDL/索引/外键与跨 Topic 关系，并核对 locator 与其派生 ID。声明 `1.2.8` 或
存在同名列不能替代这些检查。旧库返回 `database_upgrade_required`；结构/绑定不符合声明时
返回 `database_schema_drift`，只读入口不创建、升级或修复 schema。

**输入事务。** Topic 的 `input_revision/input_digest` 表示完整 body-free render input。所有
实际改变 Topic/Evidence/Claim/边或其持久 binding state 的操作，必须在一个 SQLite 事务内
完成输入修改、input revision 恰好递增一次、digest 更新及已有 brief=`needs_review`。
Store 不得提前提交输入、再由另一次连接标记失效；root writer lease 与 audit 均不能替代这个
原子提交点。规范化后的 no-op 不递增、不额外失效；render 与 review state 不属于 input mutation。
Evidence 删除及其级联边只计一次输入变化；批量 evidence state 更新每个受影响 Topic 只推进一次。
Evidence/Claim/边变更不增加 Topic 自身字段 revision。修改前须验证原始持久输入，新摘要也从
同连接的原始字段生成，不能借模型的类型转换、字段重建或 JSON 归一化掩盖损坏并重新签名。

digest 的 canonical 字段、排序和 renderer 合同版本已由 W1 冻结，包含 `input_revision` 与
所有影响渲染字节的输入；不含 source body、绝对路径、审计/观察时间或 brief review state。
模板/renderer 变化须显式更新 input binding。详见
[P1-B 计划 3.5](../../overview/P1-B-可追溯知识成果最小合同实施计划-2026-09.md)。
Topic 必须持久保存并在捕获输入中携带 `render_contract_version`；对应 renderer 不可用时拒绝
发布，不能在重启/升级时静默选择新版本渲染器。
普通 render 使用 Topic 绑定版本，render/read 不自行推进 input revision；升级后版本不可用
必须明确拒绝并进入受控输入合同迁移/重新生成计划，不能更换 renderer 却沿用旧 digest。

**发布事务。** Topic 创建时先持久保留稳定的 opaque `outcome_id`；这个身份不是成功 brief。
render 从一致输入与已提交 pointer 计算 `candidate_revision = committed_revision + 1`，
没有 pointer 时前者为 1。`publication_id` 由带域标识的 canonical
`(outcome_id, candidate_revision, input_digest)` 决定；不可覆盖路径必须包含此 identity。
对任意 r1/r2/rN，相同输入重试只可复用字节完全相同的文件；不同输入使用不同 identity，旧
orphan 保留、不覆盖，不能因旧 orphan 占据 revision 名称而永久失败。

文件发布后，在 writer lease 下复核 source/投影，再由一个 SQLite pointer CAS 事务比较旧
pointer 全元组 `(outcome_id, revision, markdown_relative_path, content_sha256, publication_id)`
或首次 absent，以及捕获的 `(input_revision, input_digest, render_contract_version)`，共同提交新 relative path、`publication_id`、
`content_sha256`、`rendered_input_revision/rendered_input_digest`、`awaiting_review` 与新
committed revision。仅该 commit 能推进 brief revision；失败时不改变当前已提交 pointer，
包括竞争者已发布的版本，也不得退回捕获时的旧 pointer。文件保留；仅未被已提交 pointer 引用
的 publication 才是 orphan，同输入竞争者已引用的同一 publication 仍是有效投影。pointer
commit 后响应前中断，新进程直接读取已提交版本。

**读取与 review。** Store 连接显式 `BEGIN`，bundle 的输入、边、pointer 与 receipt 校验来自同一个
SQLite read transaction；除 source 和投影复核外，
还必须比较 rendered input binding 与当前 input binding。缺失或不匹配时 effective state 为
`needs_review`，不更改 stored state 或文件，也不误报为投影字节损坏。外部 source 漂移不一定
推进 Topic revision，故 source revalidation 仍是独立必需条件。

review 的 Storage 请求必须携带 `outcome_id`、`expected_revision`、`content_sha256`、
`expected_input_revision`。在同一 writer-owned SQLite 事务中核对这些字段、current/rendered
input binding 与允许状态，再 CAS 更新 review state；不匹配一律零 review state 写入，不允许
读取 latest 后代填 expected 字段。同一元组且当前仍为该动作目标状态时可幂等返回，不能撤销
后来的状态转换。公开 major-2/v2 迁移
与旧无版本入口的 fail-closed 策略由总计划和 Kernel SDK 合同规定，不能由 Store 另设兼容旁路。
具体协议已在 W1 文档冻结；W3 的新协议/弃用准备不能证明旧入口已安全，必须到 W5 实际切换 major 2
并通过 OC-10b 后，才能结合其他验收关闭 R04。
`returned` 对同一 revision 保持终态，重新 render 后才可审阅下一版本；不允许同元组反复切换
approved/returned，以免旧 review 重放改变后续决定。
review state 与 `outcome_review_receipts` 在同一事务提交。receipt 保存精确确认元组、动作和
稳定 reason_code；重复 return 的 reason 也必须相同。旧 brief 使用 `binding_state=legacy_unverified`，
publication/rendered binding 保持 NULL，不为历史 state 合成有效 receipt。

历史 brief 不能通过把当前 input binding 回填为 rendered binding 来保留 approved；无法证明
时保留旧文件/pointer 并外显 `needs_review`，等待显式重新 render/review。迁移字段与约束见
[SQLite Schema 规范](../database/SQLite_Schema完整规范.md)。Store 专项已在
`tests/unit/test_w3_outcome_store.py` 验证 **65 passed**（2026-10-25，exit 0），包括输入/失效
SQL 切点、完整 CAS、receipt/state 双切点与重放、独立 WAL 连接 snapshot、伪 schema 和异常原始
输入零写拒绝。命令为 `scripts/run-test.ps1 -Direct -DataRoot .data-test/w3-store-final-v3 pytest
tests/unit/test_w3_outcome_store.py -vv --tb=short`，逐 case 日志为
`.data-test/w3-outcome-store-evidence/reports/final-65-passed.log`。
snapshot barrier 成功断言随后移到 SQLite trace callback 外；相同 nodeid 在
`.data-test/w3-store-snapshot-final` 单独重跑 **1 passed**，证据为
`.data-test/w3-outcome-store-evidence/reports/snapshot-oracle-1-passed.log`（仍为上述 65 个独立用例）。
`OC-01`–`OC-12` 的跨进程、公开接口和完整集成状态以
[W3 实施记录](../../overview/W3-Outcome一致性与版本审批实施记录-2026-10.md)为准；此处不预写全量通过。

---

## 🔧 MarkdownStore

### 核心方法

#### save(entry, subdir) → Path

```python
def save(self, entry: Entry, subdir: Optional[str] = None) -> Path:
    """保存知识条目为 Markdown 文件"""
```

**输入**:
- `entry: Entry` - 知识条目对象
- `subdir: str` - 子目录（默认使用 `entry.source_type`）

**输出**:
- `Path` - 保存的文件路径

**文件命名**:
```python
# 1. 安全化标题
safe_title = TextProcessor.sanitize_filename(entry.title)

# 2. 生成文件名
filename = f"{safe_title}.md"

# 3. 如果文件已存在，添加时间戳
if file_path.exists():
    filename = f"{safe_title}-{timestamp}.md"
```

**文件结构**:
```markdown
---
title: "示例标题"
source_type: "wechat"
tags: ["AI", "知识管理"]
...
---

# 正文内容

这里是 Markdown 格式的正文...
```

---

#### load(file_path) → Entry

```python
def load(self, file_path: Path) -> Entry:
    """加载 Markdown 文件"""
```

**解析过程**:
1. 读取文件（使用 `frontmatter` 库）
2. 提取 YAML Front Matter → 元数据
3. 提取 Markdown Body → 正文
4. 构建 Entry 对象

---

## 🔧 SQLiteStore

### 核心方法

#### initialize()

```python
def initialize(self):
    """初始化数据库 Schema"""
```

**执行步骤**:
1. 创建主表（`knowledge_items`, `tags`, `content_chunks` 等）
2. 创建索引
3. 创建 FTS5 虚拟表和触发器
4. 验证完整性

---

#### insert_entry(entry, file_path) → int

```python
def insert_entry(self, entry: Entry, file_path: str) -> int:
    """插入知识条目"""
```

**返回值**: `knowledge_id` (主键)

**特殊处理**:
```python
# 1. 插入主表（原始数据）
cursor.execute("INSERT INTO knowledge_items (...) VALUES (...)", (...))
knowledge_id = cursor.lastrowid

# 2. 手动更新 FTS5 表（分词后的数据）
fts5_data = self.text_processor.prepare_fts5_data(...)
conn.execute("DELETE FROM knowledge_items_fts WHERE rowid = ?", (knowledge_id,))
conn.execute("INSERT INTO knowledge_items_fts (...) VALUES (...)", (...))

# 3. 插入标签关联
self._insert_tags(conn, knowledge_id, entry.tags)
```

**注意**:
- ⚠️ FTS5 同步逻辑：先删除触发器自动插入的原始数据，再插入分词后的数据

---

#### query_by_id(knowledge_id) → Dict

```python
def query_by_id(self, knowledge_id: int) -> Optional[Dict[str, Any]]:
    """根据 ID 查询知识条目"""
```

**返回值**: 字典形式的条目数据（`sqlite3.Row` 转换）

---

## 🔧 VectorStore

### 核心方法

#### add_doc_vector(knowledge_id, vector) → int

```python
def add_doc_vector(self, knowledge_id: int, vector: np.ndarray) -> int:
    """添加文档级向量"""
```

**输入**:
- `knowledge_id: int` - 知识条目 ID
- `vector: np.ndarray` - 当前 Embedding 模型真实维度的向量

**输出**:
- `int` - 内部向量 ID（hnswlib label）

**数据流**:
```python
# 1. 生成内部 ID
internal_id = self._next_id
self._next_id += 1

# 2. 添加到 hnswlib 索引
self.index.add_items(vector, internal_id)

# 3. 保存双向映射
self._id_mapping[internal_id] = knowledge_id
self._reverse_mapping[knowledge_id] = internal_id
```

---

#### add_entry(entry, knowledge_id) → List[int]

```python
def add_entry(self, entry: Entry, knowledge_id: int) -> List[int]:
    """添加 Entry（自动分块向量化）"""
```

**处理流程**:
1. 提取正文内容
2. 使用 `Embedder.embed_chunks()` 分块向量化
3. 逐个添加分块向量到索引
4. 返回向量 ID 列表

---

#### search(query_vector, top_k) → List[Tuple[int, float]]

```python
def search(self, query_vector: np.ndarray, top_k: int = 10) -> List[Tuple[int, float]]:
    """向量检索"""
```

**返回值**: `[(knowledge_id, similarity_score), ...]`

**实现**:
```python
# 1. hnswlib 近似最近邻搜索
labels, distances = self.index.knn_query(query_vector, k=top_k)

# 2. 转换内部 ID → knowledge_id
results = []
for label, distance in zip(labels[0], distances[0]):
    knowledge_id = self._id_mapping.get(label)
    similarity = 1.0 / (1.0 + distance)  # 距离 → 相似度
    results.append((knowledge_id, similarity))
```

---

## 🔗 三者协作

### StoreStep 中的使用

```python
# 1. Markdown 存储
file_path = markdown_store.save(entry, subdir="wechat")

# 2. SQLite 存储
knowledge_id = sqlite_store.insert_entry(entry, file_path)

# 3. 向量存储
vector_ids = vector_store.add_entry(entry, knowledge_id)

# 返回结果
return {
    "file_path": file_path,
    "knowledge_id": knowledge_id,
    "vector_ids": vector_ids
}
```

### 数据关联

```
Entry (对象)
    ↓
┌─────────────┬──────────────┬─────────────┐
↓             ↓              ↓             ↓
Markdown      SQLite        Vector       ID映射
.md 文件      knowledge_id  internal_id  双向
(主存储)      (元数据)      (向量)       关联
```

---

## ⚠️ 关键问题

### 问题 1: `knowledge_id` 命名不一致

**问题**: 数据库使用 `knowledge_id`，但部分代码期望 `id`

**优先级**: 中

---

### 问题 2: `keywords` 字段类型不一致

**问题**: Entry 中为 `list`，SQLite 中为逗号分隔字符串

**优先级**: 低

---

### 问题 3: 向量存储缺少持久化

**问题**: ID 映射需要手动保存/加载

**优先级**: 中

---

## 🎯 总结

### 三层存储架构

| 层次 | 后端 | 特点 |
|------|------|------|
| **主存储** | MarkdownStore | 人类可读、Git 友好 |
| **索引层** | SQLiteStore | 快速查询、FTS5 全文搜索 |
| **语义层** | VectorStore | 向量检索、语义搜索 |

### 核心方法

| 后端 | 核心方法 | 返回值 |
|------|---------|--------|
| MarkdownStore | `save()` | `file_path` |
| SQLiteStore | `insert_entry()` | `knowledge_id` |
| VectorStore | `add_entry()` | `vector_ids` |

---

**文档维护者**: AI Agent
**最后更新**: 2026-02-15
