# Discussion Archive

> **归档目录** - 已整合到正式文档的讨论记录

---

## 📋 归档说明

此目录存放已完成整合的讨论文档，内容已整理到项目正式文档中。

保留原始讨论记录是为了：
- ✅ 方便回溯决策过程
- ✅ 保留 AI 对话历史
- ✅ 记录技术选型演变

---

## 📂 归档清单

### M12 UI 设计讨论（2026-02-20）

| 文件 | 大小 | AI 来源 | 整合到 |
|------|------|---------|--------|
| `AI 对话界面 UI 设计.md` | 7.6K | KIMI | `M12_RESEARCH/06_UI_DESIGN_FINAL.md` |
| `AI 对话界面设计.md` | 20K | ChatGPT | `M12_RESEARCH/06_UI_DESIGN_FINAL.md` |
| `AI对话UI设计.docx` | 33K | 原始文档 | `M12_RESEARCH/06_UI_DESIGN_FINAL.md` |

**最终选型**: QTextBrowser + markdown2 + Pygments + 30ms 批量更新（ChatGPT 轻量级方案）

---

### DeepSeek API 调研（2026-02-20）

| 文件 | 大小 | 来源 | 整合到 |
|------|------|------|--------|
| `DeepSeek API 调研.md` | 4.9K | ChatGPT | `M12_RESEARCH/01_DEEPSEEK_API_RESEARCH.md` |
| `DeepSeek API 核心特性与使用指南.md` | 3.7K | NotebookLM | `M12_RESEARCH/01_DEEPSEEK_API_RESEARCH.md` |

**核心发现**:
- OpenAI SDK 100% 兼容
- 128K 上下文窗口
- 上下文缓存优化（1/10 价格）

---

### Phase 1 存储与检索方案讨论（2026-02-02）

| 文件 | 大小 | 对比方案 | 最终决策 |
|------|------|----------|----------|
| `存储与检索方案讨论.md` | 17K | 初步讨论 | SQLite + hnswlib |
| `存储与检索方案讨论2-LanceDB-vs-SQLite.md` | 11K | LanceDB vs SQLite | ✅ SQLite FTS5 |
| `存储与检索方案讨论3-KV数据库-vs-SQLite.md` | 11K | LevelDB/RocksDB vs SQLite | ✅ SQLite |

**最终架构**:
- 主存储: Markdown + YAML Front Matter
- 辅助索引: SQLite FTS5
- 向量检索: hnswlib

---

## ⏱️ 归档时间

**归档日期**: 2026-02-21
**归档原因**: Day 2 技术预研完成，进入 Day 3 原型实现阶段

---

**注意**: 此目录中的文档内容可能已过时，请参考正式文档的最新版本。
