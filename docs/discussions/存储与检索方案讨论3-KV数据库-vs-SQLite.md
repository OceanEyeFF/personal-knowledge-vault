# 存储与检索方案讨论3：KV 数据库 vs SQLite

> Personal Knowledge Vault - 嵌入式 Key-Value 数据库调研
> 创建日期: 2026-02-02
> 状态: 调研完成，结论明确

---

## 📋 目录

1. [调研背景](#调研背景)
2. [嵌入式 KV 数据库概览](#嵌入式-kv-数据库概览)
3. [各 KV 数据库详细评估](#各-kv-数据库详细评估)
4. [KV 数据库 vs SQLite 对比](#kv-数据库-vs-sqlite-对比)
5. [PKV 需求适配分析](#pkv-需求适配分析)
6. [结论](#结论)
7. [参考资料](#参考资料)

---

## 调研背景

在确定方案3（SQLite + hnswlib + jieba）后，进一步探讨：**是否有 KV 数据库比 SQLite 更适合 PKV？**

KV 数据库以**高性能读写**著称，但 PKV 的需求不仅仅是简单的 key-value 存取。本文档记录 KV 数据库的调研过程和结论。

---

## 嵌入式 KV 数据库概览

### DB-Engines 排名对比

| 数据库 | 类型 | 总排名 | 细分排名 | 开发者 | 初始发布 |
|--------|------|-------|---------|--------|---------|
| **SQLite** | 关系型 | **#10** | #7 RDBMS | D. Richard Hipp | 2000 |
| RocksDB | KV | #82 | #11 KV | Facebook | 2013 |
| LevelDB | KV | #112 | #18 KV | Google | 2011 |
| LMDB | KV | #124 | #20 KV | Symas | 2011 |

来源：[DB-Engines LMDB vs RocksDB vs SQLite](https://db-engines.com/en/system/LMDB%3BRocksDB%3BSQLite)

**关键发现**：SQLite 排名 #10，远超所有 KV 数据库（最高的 RocksDB 也只排 #82）。这反映了社区活跃度、文档完善度、生态成熟度的巨大差距。

### 主流嵌入式 KV 数据库一览

| 数据库 | 开发者 | 存储引擎 | 特点 | Python 支持 |
|--------|--------|---------|------|-------------|
| **LevelDB** | Google | LSM 树 | 有序 key-value，基础稳定 | plyvel |
| **RocksDB** | Facebook | LSM 树 | LevelDB 改进版，优化 SSD | rocksdict, python-rocksdb |
| **LMDB** | Symas | B+ 树 + mmap | 内存映射，读性能极高 | lmdb |
| **UnQLite** | Symisc | 混合 | KV + 文档存储，类 MongoDB | unqlite |
| **TinyDB** | 社区 | JSON 文件 | 纯 Python，极简 | 内置 |
| **Vedis** | Symisc | 内存/磁盘 | Redis-like 嵌入式 | vedis |

---

## 各 KV 数据库详细评估

### LevelDB

| 维度 | 评估 |
|------|------|
| **开发者** | Google |
| **存储引擎** | LSM 树（Log-Structured Merge-tree） |
| **特点** | 有序 key-value 存储，支持范围查询 |
| **Python 库** | `plyvel`（`pip install plyvel`） |
| **优势** | 稳定、成熟、Google 背书 |
| **劣势** | 功能基础，无事务，无压缩选项 |
| **适用场景** | 简单 KV 存储、日志存储 |

### RocksDB

| 维度 | 评估 |
|------|------|
| **开发者** | Facebook（基于 LevelDB） |
| **存储引擎** | LSM 树（优化版） |
| **特点** | 针对 SSD/Flash 优化，高写入吞吐 |
| **Python 库** | `rocksdict`、`python-rocksdb` |
| **优势** | 写性能极强，压缩选项丰富，事务支持 |
| **劣势** | 配置复杂，内存占用较高 |
| **适用场景** | 高吞吐写入、时序数据、大规模 KV |

### LMDB

| 维度 | 评估 |
|------|------|
| **开发者** | Symas（OpenLDAP 项目） |
| **存储引擎** | B+ 树 + 内存映射（mmap） |
| **特点** | 读性能极高，ACID 事务，copy-on-write |
| **Python 库** | `lmdb`（`pip install lmdb`） |
| **优势** | 读取速度极快，零拷贝，崩溃安全 |
| **劣势** | 数据库大小需预先设定，写入较慢 |
| **适用场景** | 读密集型、缓存、嵌入式系统 |

### TinyDB

| 维度 | 评估 |
|------|------|
| **开发者** | 社区 |
| **存储引擎** | JSON 文件 |
| **特点** | 纯 Python，1800 行代码，文档型 |
| **Python 库** | `tinydb`（`pip install tinydb`） |
| **优势** | 极简、零配置、学习成本低 |
| **劣势** | 🔴 **性能差**，10k-50k 记录后明显下降 |
| **并发** | 🔴 不支持多进程/多线程 |
| **适用场景** | 原型、配置存储、小型项目 |

来源：[SQLite vs TinyDB](https://medium.com/data-science/sqlite-vs-tinydb-7d6a6a42cb97)

### UnQLite

| 维度 | 评估 |
|------|------|
| **开发者** | Symisc Software |
| **存储引擎** | 混合（KV + 文档） |
| **特点** | 两层 API：底层 KV + 上层文档存储 |
| **Python 库** | `unqlite`（`pip install unqlite`） |
| **优势** | 灵活（KV 和文档都支持）、性能不错 |
| **劣势** | 社区较小，文档不完善 |
| **适用场景** | 需要 NoSQL 灵活性的嵌入式场景 |

### Vedis

| 维度 | 评估 |
|------|------|
| **开发者** | Symisc Software |
| **存储引擎** | 内存/磁盘混合 |
| **特点** | Redis-like API 的嵌入式版本 |
| **Python 库** | `vedis` |
| **优势** | 支持丰富的数据结构（list, set, hash 等） |
| **劣势** | 社区小，文档少 |
| **适用场景** | 需要 Redis 数据结构但不想运行服务器 |

---

## KV 数据库 vs SQLite 对比

### Python 嵌入式数据库性能基准

根据 [charlesleifer 的基准测试](https://charlesleifer.com/blog/completely-un-scientific-benchmarks-of-some-embedded-databases-with-python/) 和 [romnovi 的测试](https://romnovi.dev/notes/python_1/)：

| 排名 | 数据库 | 纯 KV 性能 | 备注 |
|------|--------|-----------|------|
| 1 | Vedis | ⭐⭐⭐⭐⭐ | Redis-like，性能最强 |
| 2 | UnQLite | ⭐⭐⭐⭐⭐ | KV + 文档，第二快 |
| 3 | LMDB | ⭐⭐⭐⭐ | 读性能极强 |
| 4 | RocksDB | ⭐⭐⭐⭐ | 写性能强 |
| 5 | LevelDB | ⭐⭐⭐ | 基础稳定 |
| 6 | SQLite | ⭐⭐⭐ | 关系型，非纯 KV 场景 |
| 7 | TinyDB | ⭐ | 太慢，被移出图表 |
| 8 | PickleDB | ⭐ | 太慢，被移出图表 |

**注意**：这个基准测试针对的是**纯 KV 操作**（get/put）。SQLite 在这个场景下排名中等，但它的优势在于**查询能力**，而不是纯 KV 性能。

### 功能对比

| 功能 | KV 数据库 | SQLite |
|------|----------|--------|
| **Key-Value 存取** | ✅ 原生支持，性能最优 | ✅ 可模拟（表结构） |
| **范围查询** | 🟡 有序 KV 支持（LevelDB/RocksDB） | ✅ SQL WHERE 原生 |
| **复杂过滤** | 🔴 需自建二级索引 | ✅ SQL WHERE/AND/OR |
| **多条件查询** | 🔴 需自己实现 | ✅ SQL 原生 |
| **JOIN 关联** | 🔴 不支持 | ✅ SQL 原生 |
| **聚合统计** | 🔴 需自己实现 | ✅ SQL COUNT/SUM/AVG |
| **全文搜索** | 🔴 不支持 | ✅ FTS5 |
| **事务** | 🟡 部分支持（LMDB、RocksDB） | ✅ 完整 ACID |
| **SQL 标准** | 🔴 不支持 | ✅ 完整支持 |

---

## PKV 需求适配分析

### PKV 的核心查询场景

```sql
-- 场景1：时间范围查询
-- "找出最近一周的微信文章"
SELECT * FROM items
WHERE source = 'wechat' AND created_at > date('now', '-7 days')

-- 场景2：多标签交集查询
-- "找出同时有 '分布式' 和 '一致性' 标签的笔记"
SELECT * FROM items WHERE id IN (
  SELECT item_id FROM item_tags
  WHERE tag IN ('分布式', '一致性')
  GROUP BY item_id HAVING COUNT(*) = 2
)

-- 场景3：全文搜索
-- "搜索包含 '向量数据库' 的内容"
SELECT * FROM items_fts WHERE items_fts MATCH '向量数据库'

-- 场景4：组合查询
-- "最近一个月的技术文章中，包含 'RAG' 关键词的"
SELECT * FROM items
WHERE type = 'article'
  AND created_at > date('now', '-30 days')
  AND id IN (SELECT rowid FROM items_fts WHERE items_fts MATCH 'RAG')
```

### KV 数据库如何实现这些查询？

| 查询场景 | KV 实现方式 | 复杂度 | 性能 |
|---------|------------|--------|------|
| 时间范围 | 按时间戳作为 key 前缀，范围扫描 | 🟡 中 | 🟡 中 |
| 多标签交集 | 自建倒排索引，读取多个 key 后计算交集 | 🔴 高 | 🔴 差 |
| 全文搜索 | **完全不支持**，需外挂搜索引擎 | 🔴 极高 | - |
| 组合查询 | 多个索引结果合并计算 | 🔴 极高 | 🔴 差 |

### 结论：KV 数据库不适合 PKV

```
PKV 需要的核心能力：
  ✅ 复杂元数据查询（标签、时间、类型组合过滤）
  ✅ 全文搜索（中文 BM25）
  ✅ 关联查询（标签关系、语义组）

KV 数据库的能力：
  ✅ 高性能 Key-Value 存取
  🟡 有限的范围查询
  🔴 不支持复杂过滤
  🔴 不支持全文搜索
  🔴 不支持关联查询

结论：能力不匹配
```

---

## 结论

### KV 数据库的定位

```
KV 数据库适合的场景：
  - 简单的 key → value 存储
  - 缓存（Session、临时数据）
  - 高吞吐量写入（日志、时序数据）
  - 数据结构简单的应用

KV 数据库不适合的场景：
  - 复杂查询（需要自建多个二级索引）
  - 全文搜索（完全不支持）
  - 需要 JOIN/聚合的场景
  - 需要 SQL 灵活性的场景
```

### 对 PKV 的最终建议

| 方案 | 评估 | 原因 |
|------|------|------|
| **纯 KV 数据库** | 🔴 不推荐 | 无法满足复杂查询和全文搜索需求 |
| **KV + SQLite 混用** | 🟡 过度复杂 | 增加架构复杂度，收益不明显 |
| **SQLite 单独使用** | ✅ **推荐** | 查询能力强、FTS5 全文搜索、极度稳定 |

### 为什么 SQLite 是更好的选择

| 维度 | SQLite 优势 |
|------|------------|
| **查询能力** | 完整 SQL，复杂过滤、JOIN、聚合全支持 |
| **全文搜索** | FTS5 内置，配合 jieba 支持中文 |
| **稳定性** | 2000 年发布，世界上部署最广的数据库 |
| **生态** | DB-Engines #10，文档/工具链极其完善 |
| **PKV 架构** | 辅助存储定位，损坏可重建，SQLite 完美匹配 |

**KV 数据库在纯 key-value 性能上更强，但 PKV 的查询需求远超 KV 数据库的能力边界。**

---

## 参考资料

### 数据库对比
- [DB-Engines: LMDB vs RocksDB vs SQLite](https://db-engines.com/en/system/LMDB%3BRocksDB%3BSQLite)
- [DB-Engines: LevelDB vs LMDB vs RocksDB](https://db-engines.com/en/system/LMDB%3BLevelDB%3BRocksDB)
- [Top 8 Embedded SQL Databases in 2025](https://www.explo.co/blog/embedded-sql-databases)

### 性能基准
- [charlesleifer - Embedded Databases Benchmarks](https://charlesleifer.com/blog/completely-un-scientific-benchmarks-of-some-embedded-databases-with-python/)
- [romnovi - Python KV Comparison](https://romnovi.dev/notes/python_1/)
- [KeyValueStoreBenchmark GitHub](https://github.com/jesse-r-s-hines/KeyValueStoreBenchmark)

### TinyDB
- [SQLite vs TinyDB - Medium](https://medium.com/data-science/sqlite-vs-tinydb-7d6a6a42cb97)
- [TinyDB Documentation](https://tinydb.readthedocs.io/en/latest/intro.html)
- [TinyDB - The Blue Book](https://lyz-code.github.io/blue-book/coding/python/tinydb/)

### 各数据库官方
- [LevelDB GitHub](https://github.com/google/leveldb)
- [RocksDB](https://rocksdb.org/)
- [LMDB](https://www.symas.com/symas-embedded-database-lmdb)
- [UnQLite](https://unqlite.org/)

### 上游文档
- [存储与检索方案讨论](./存储与检索方案讨论.md) - 主讨论文档
- [LanceDB vs SQLite 调研](./存储与检索方案讨论2-LanceDB-vs-SQLite.md) - 非传统数据库方案

---

**文档结束**

*本文档为 KV 数据库调研记录，结论支持继续采用 SQLite 作为辅助存储方案*
