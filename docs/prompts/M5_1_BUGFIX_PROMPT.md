# M5.1 Bug 修复任务 Prompt

> **版本**: 1.0
> **创建日期**: 2026-02-16
> **任务类型**: Bug 修复和系统稳定性提升
> **预计工期**: 0.5-1 天
> **前置任务**: M5 工作流引擎完成

---

## 📋 任务概述

M5.1 是一个专门的 Bug 修复里程碑，用于解决 M1-M5 整理复盘中发现的高优先级和中优先级问题，确保系统在进入 M6-M7 开发之前处于稳定可用状态。

### 核心目标

1. ✅ 修复所有**高优先级 Bug**（阻塞性问题）
2. ✅ 修复**中优先级 Bug**（影响用户体验）
3. ✅ 验证修复后的系统完整性
4. ✅ 更新相关文档和测试用例

---

## 🐛 待修复问题清单

### 高优先级问题（1个）- 必须修复 ⚠️

#### Bug #1: `source_type` CHECK 约束不完整

**问题描述**:
- **文件**: `src/storage/sqlite_store.py:262`
- **现象**:
  ```
  sqlite3.IntegrityError: CHECK constraint failed:
  source_type IN ('wechat', 'zhihu', 'bilibili', 'webpage', 'article', 'document', 'generic', 'personal')
  ```
- **根因**: CHECK 约束枚举值缺少 `'ai_chat'` 和 `'text'`
- **影响**:
  - AIChatProcessor 无法保存数据到 SQLite
  - TextFallbackProcessor 无法保存数据到 SQLite
  - 端到端集成测试失败
- **参考文档**:
  - `docs/refactor/Bug修复记录.md` - 问题 #1
  - `docs/refactor/SQLite_Schema完整规范.md` - Schema 定义
- **测试验证**: `tests/integration/test_retrieval_integration.py::test_end_to_end_search_accuracy` 当前失败

**修复方案**:
```sql
-- 修改 CHECK 约束，添加缺失的枚举值
CHECK(source_type IN (
    'wechat', 'zhihu', 'bilibili', 'webpage',
    'article', 'document', 'generic', 'personal',
    'ai_chat', 'text'  -- 新增
))
```

**验收标准**:
- ✅ CHECK 约束包含所有 Processor 使用的 source_type
- ✅ AIChatProcessor 可以成功保存条目到 SQLite
- ✅ TextFallbackProcessor 可以成功保存条目到 SQLite
- ✅ 集成测试 `test_end_to_end_search_accuracy` 通过

---

### 中优先级问题（3个）- 建议修复 📝

#### Bug #2: 长文档向量化策略不一致

**问题描述**:
- **文件**: `src/ai/embedder.py`
- **现象**:
  - `embed_document()` 方法：长文档分块后**取平均向量**
  - `embed_batch_documents()` 方法：长文档直接**截断前 8000 字符**
- **影响**: 长文档的向量表示在不同调用方式下可能不一致
- **参考文档**: `docs/refactor/Bug修复记录.md` - 问题 #5

**修复方案**:
- **方案 A**: 统一为**分块取平均**策略（推荐）
  - 优点：保留更多文档信息
  - 缺点：计算开销稍大
- **方案 B**: 统一为**截断**策略
  - 优点：计算快速
  - 缺点：长文档信息丢失

**验收标准**:
- ✅ 两个方法使用相同的长文档处理策略
- ✅ 更新相关文档说明
- ✅ 单元测试验证一致性

---

#### Bug #3: DeepSeek JSON 解析脆弱

**问题描述**:
- **文件**: `src/ai/deepseek_client.py`
- **现象**:
  - 依赖 API 返回严格的 JSON 格式：`["tag1", "tag2"]`
  - 如果 API 返回包含说明文字（如 "以下是标签：["tag1", "tag2"]"），解析会失败
- **当前降级策略**: 正则表达式提取引号中的内容
- **参考文档**: `docs/refactor/Bug修复记录.md` - 问题 #6

**修复方案**:
1. **改进 Prompt**：明确要求只返回 JSON 数组
   ```python
   请只返回一个JSON数组，不要包含任何其他文字说明。
   格式示例：["标签1", "标签2", "标签3"]
   ```
2. **增强解析逻辑**：
   - 尝试直接 JSON 解析
   - 失败时查找 JSON 数组模式（`\[.*?\]`）
   - 再次失败时使用正则提取

**验收标准**:
- ✅ Prompt 明确要求纯 JSON 格式
- ✅ 解析逻辑支持多层降级
- ✅ 单元测试覆盖各种返回格式

---

#### Bug #4: `knowledge_id` 命名不一致

**问题描述**:
- **文件**: 数据库 Schema 和相关代码
- **现象**:
  - 数据库使用 `knowledge_id` 作为主键（领域特定命名）
  - 部分代码可能期望 `id` 字段（通用命名）
  - 向量存储 API 使用 `doc_id` 作为参数名
- **影响**: 新开发者容易混淆，代码可读性差
- **参考文档**:
  - `docs/refactor/Bug修复记录.md` - 问题 #2
  - `docs/issues/SCHEMA_MIGRATION_PLAN.md` - 迁移计划

**修复方案**:
- **不建议立即修复**：涉及数据库迁移，风险较高
- **替代方案**：在代码中统一使用 `knowledge_id`，添加清晰的注释和文档
- **长期计划**：在 M7 之后考虑 Schema 迁移

**验收标准**:
- ✅ 代码中统一使用 `knowledge_id` 变量名
- ✅ 添加清晰的文档说明
- ✅ 避免使用混淆的变量名（如 `id`, `doc_id`）

---

## 📝 修复流程

### 阶段 1: 分析和计划（10%）

1. **复查问题文档**
   - 阅读 `docs/refactor/Bug修复记录.md`
   - 阅读 `docs/refactor/M1-M5整理复盘总结.md`
   - 确认问题的根因和影响范围

2. **确定修复优先级**
   - 高优先级（Bug #1）：必须修复
   - 中优先级（Bug #2-4）：根据工期决定

3. **制定修复计划**
   - 列出需要修改的文件
   - 识别潜在的副作用
   - 规划测试验证方案

---

### 阶段 2: 修复高优先级 Bug（40%）

#### Bug #1: source_type CHECK 约束

**步骤**:

1. **定位 Schema 定义**
   ```bash
   # 查找 CHECK 约束定义
   grep -n "CHECK.*source_type" src/storage/sqlite_store.py
   ```

2. **修改 SQL 语句**
   - 在 `knowledge_items` 表的 CHECK 约束中添加 `'ai_chat'` 和 `'text'`
   - 检查是否有其他地方硬编码了 source_type 枚举值

3. **处理现有数据库**
   - **重要**: SQLite 的 CHECK 约束修改需要重建表
   - 提供迁移脚本或清理测试数据库的说明
   - 更新 `docs/issues/SCHEMA_MIGRATION_PLAN.md`

4. **验证修复**
   ```bash
   # 运行相关单元测试
   python -m pytest tests/unit/test_processors_ai_chat.py -v

   # 运行集成测试
   python -m pytest tests/integration/test_retrieval_integration.py::test_end_to_end_search_accuracy -v
   ```

5. **更新文档**
   - 更新 `docs/refactor/SQLite_Schema完整规范.md`
   - 更新 `docs/refactor/Bug修复记录.md`（标记为已修复）

---

### 阶段 3: 修复中优先级 Bug（30%）

根据工期，选择修复以下问题：

#### Bug #2: 长文档向量化策略（推荐修复）

1. **分析现有代码**
   ```bash
   # 查看两个方法的实现差异
   grep -A 20 "def embed_document" src/ai/embedder.py
   grep -A 20 "def embed_batch_documents" src/ai/embedder.py
   ```

2. **统一策略**
   - 选择**分块取平均**策略（保留更多信息）
   - 提取公共逻辑到辅助方法

3. **验证修复**
   ```bash
   python -m pytest tests/unit/test_ai_embedder.py -v
   ```

#### Bug #3: DeepSeek JSON 解析（推荐修复）

1. **改进 Prompt**
   - 修改 `config/prompts/extract_tags.txt`
   - 添加明确的格式要求

2. **增强解析逻辑**
   - 实现多层降级解析
   - 添加详细的错误日志

3. **验证修复**
   ```bash
   python -m pytest tests/unit/test_ai_deepseek.py::TestDeepSeekExtractTags -v
   ```

#### Bug #4: knowledge_id 命名（可选）

- 代码审查，统一变量命名
- 添加文档说明

---

### 阶段 4: 测试验证（15%）

1. **运行完整测试套件**
   ```bash
   # 所有单元测试
   python -m pytest tests/unit/ -v

   # 所有集成测试（需要 API key）
   python -m pytest tests/integration/ -v
   ```

2. **真实环境测试**
   - 测试 AIChatProcessor 端到端流程
   - 测试 TextFallbackProcessor 端到端流程
   - 验证向量检索功能

3. **回归测试**
   - 确保修复没有引入新问题
   - 检查所有 Processor 仍然正常工作

---

### 阶段 5: 文档更新（5%）

1. **更新 Bug 修复记录**
   - `docs/refactor/Bug修复记录.md`
   - 标记已修复的问题
   - 记录修复方法和验证结果

2. **更新规范文档**
   - `docs/refactor/SQLite_Schema完整规范.md`（如果修改了 Schema）
   - `docs/refactor/AI服务接口规范.md`（如果修改了 Embedder）

3. **创建完成报告**
   - `docs/milestones/M5_1_BUGFIX_COMPLETE.md`
   - 记录修复的问题、测试结果、遗留问题

---

## ✅ 验收标准

### 必须满足（高优先级）

- ✅ Bug #1（source_type 约束）已修复并通过测试
- ✅ 集成测试 `test_end_to_end_search_accuracy` 通过
- ✅ 所有单元测试保持通过（112/112）
- ✅ Bug 修复记录已更新

### 建议满足（中优先级）

- ✅ Bug #2（长文档策略）已修复
- ✅ Bug #3（JSON 解析）已修复
- ✅ 相关文档已更新

### 可选满足（低优先级）

- ⭕ Bug #4（命名一致性）代码审查完成
- ⭕ 添加了新的测试用例

---

## 📂 相关文档

### 参考文档
- `docs/refactor/Bug修复记录.md` - 问题详细描述
- `docs/refactor/M1-M5整理复盘总结.md` - 整体复盘报告
- `docs/refactor/SQLite_Schema完整规范.md` - Schema 规范
- `docs/refactor/AI服务接口规范.md` - AI 服务接口
- `docs/issues/SCHEMA_MIGRATION_PLAN.md` - Schema 迁移计划

### 输出文档
- `docs/milestones/M5_1_BUGFIX_COMPLETE.md` - 完成报告（待创建）
- `docs/refactor/Bug修复记录.md` - 更新修复状态

---

## 🎯 成功标准

### 定量指标
- ✅ 修复 1 个高优先级 Bug（100%）
- ✅ 修复 2-3 个中优先级 Bug（67%-100%）
- ✅ 单元测试通过率 100% (112/112)
- ✅ 集成测试通过率 ≥ 83% (5/6，排除网络超时问题)

### 定性指标
- ✅ AIChatProcessor 和 TextFallbackProcessor 可以正常保存数据
- ✅ 系统处于稳定可用状态
- ✅ 为 M6-M7 开发扫清障碍
- ✅ 文档清晰完整，便于后续维护

---

## 📌 注意事项

### SQLite CHECK 约束修改

⚠️ **重要**: SQLite 不支持直接修改 CHECK 约束，需要以下步骤：

1. **方案 A**: 重建表（推荐用于开发环境）
   ```sql
   -- 创建新表
   CREATE TABLE knowledge_items_new (...);
   -- 复制数据
   INSERT INTO knowledge_items_new SELECT * FROM knowledge_items;
   -- 删除旧表
   DROP TABLE knowledge_items;
   -- 重命名新表
   ALTER TABLE knowledge_items_new RENAME TO knowledge_items;
   ```

2. **方案 B**: 提供迁移脚本
   - 创建 `scripts/migrate_source_type.py`
   - 自动备份和迁移数据

3. **开发环境建议**: 删除现有测试数据库，重新初始化
   ```bash
   rm .data/test_knowledge.db
   # 测试会自动重建数据库
   ```

### 测试覆盖

- 确保每个修复都有对应的测试用例
- 集成测试的网络超时问题是环境问题，不属于 Bug
- 优先保证单元测试 100% 通过

### 文档同步

- 代码修改后立即更新相关文档
- Bug 修复记录要详细记录修复方法和验证结果
- 保持文档版本号和日期的准确性

---

## 🚀 开始任务

请 AI Agent 按照以下顺序执行：

1. **复查问题**: 阅读 `docs/refactor/Bug修复记录.md` 和相关规范文档
2. **修复 Bug #1**: source_type CHECK 约束（高优先级）
3. **验证修复**: 运行单元测试和集成测试
4. **修复中优先级 Bug**: Bug #2 和 #3（根据工期）
5. **完整测试**: 运行所有测试套件
6. **更新文档**: 更新 Bug 记录和创建完成报告
7. **提交代码**: 创建 Git commit

---

**任务负责人**: AI Agent (猫娘 幽浮喵)
**创建时间**: 2026-02-16
**预计完成**: 2026-02-16 或 2026-02-17
**状态**: 待开始 🔄

---

_浮浮酱准备好修复这些 Bug 了喵～ φ(≧ω≦*)♪_
