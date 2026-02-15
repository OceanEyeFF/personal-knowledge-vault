# Bug 修复记录

> **版本**: 1.0
> **创建日期**: 2026-02-15
> **作用**: 汇总 M1-M5 开发过程中发现的问题和修复情况

---

## ✅ 已修复的严重 Bug

### Bug #1: 配置字段名不匹配

**发现时间**: M5 真实环境测试
**文件**: `config/workflows/archive-url.yaml:55`

**问题描述**:
- 工作流配置使用 `storage_backends` 字段
- StoreStep 代码期望 `targets` 字段
- 导致 SQLite 和向量存储步骤未执行

**影响范围**: 中等
- 部分存储后端失效
- 数据未完整保存

**修复方法**:
```yaml
# 修复前
- type: store_entry
  config:
    storage_backends: [markdown, sqlite, vector]

# 修复后
- type: store_entry
  config:
    targets: [markdown, sqlite, vector]
```

**验证状态**: ✅ 已通过真实环境测试

---

### Bug #2: 引擎传参错误

**发现时间**: M5 真实环境测试
**文件**: `src/workflow/engine.py:91`

**问题描述**:
- WorkflowEngine 传递整个 `step_config` 给步骤构造函数
- BaseStep 构造函数期望只接收 `config` 字段
- 导致所有步骤的配置参数无法正确读取

**影响范围**: 高
- 所有步骤的配置参数失效
- 默认值被强制使用

**修复方法**:
```python
# 修复前
step = step_class(step_id=step_id, config=step_config)

# 修复后
step_config_data = step_config.get("config", {})
step = step_class(step_id=step_id, config=step_config_data)
```

**验证状态**: ✅ 已通过真实环境测试

---

### Bug #3: `source_type` 枚举值不完整

**发现时间**: M5.1 Bug 修复阶段
**修复时间**: 2026-02-16
**文件**: `src/storage/sqlite_store.py:91`

**问题描述**:
- 数据库 CHECK 约束缺少 `ai_chat`, `text`, `test` 枚举值
- AIChatProcessor 和 TextFallbackProcessor 无法保存数据
- 集成测试失败

**影响范围**: 高
- AIChatProcessor 和 TextFallbackProcessor 无法正常工作
- 端到端集成测试失败

**修复方法**:
```sql
-- 修复前
CHECK(source_type IN ('wechat', 'zhihu', 'bilibili', 'webpage', 'article', 'document', 'generic', 'personal'))

-- 修复后
CHECK(source_type IN ('wechat', 'zhihu', 'bilibili', 'webpage', 'article', 'document', 'generic', 'personal', 'ai_chat', 'text', 'test'))
```

**验证状态**: ✅ 所有单元测试通过 (122/122)
- `test_processors_ai_chat.py`: 12/12 通过
- `test_processors_text_fallback.py`: 10/10 通过

---

### Bug #4: 长文档向量化策略不一致

**发现时间**: M5.1 Bug 修复阶段
**修复时间**: 2026-02-16
**文件**: `src/ai/embedder.py`

**问题描述**:
- `embed_document()` 方法：长文档（>8000字符）分块后取平均向量
- `embed_batch_documents()` 方法：长文档直接截断前 8000 字符
- 两种策略不一致，导致向量表示差异

**影响范围**: 中等
- 长文档的向量检索结果可能不一致

**修复方法**:
- 统一为**分块取平均**策略（保留更多文档信息）
- `embed_batch_documents` 改为逐个处理文档，长文档调用 `_embed_long_document()`

**验证状态**: ✅ 所有 Embedder 测试通过 (11/11)
- 更新测试用例以匹配新的行为

---

### Bug #5: DeepSeek JSON 解析脆弱

**发现时间**: M5.1 Bug 修复阶段
**修复时间**: 2026-02-16
**文件**: `src/ai/deepseek_client.py`, `src/ai/prompts/extract_tags.txt`

**问题描述**:
- 依赖 API 返回严格的 JSON 格式：`["tag1", "tag2"]`
- 如果 API 返回包含说明文字（如 "以下是标签：["tag1", "tag2"]"），解析会失败
- 仅有单层降级策略（正则提取引号内容）

**影响范围**: 中等
- 标签提取可能失败
- 降级策略覆盖不全

**修复方法**:

1. **改进 Prompt** (`src/ai/prompts/extract_tags.txt`):
```
**输出格式（非常重要）：**
- 请只返回一个JSON数组，不要包含任何其他文字说明
- 格式示例：["标签1", "标签2", "标签3"]
- 不要添加任何前缀、后缀或解释性文字
```

2. **增强解析逻辑** - 三层降级策略:
   - 策略 1: 直接 JSON 解析（最理想）
   - 策略 2: 查找 JSON 数组模式（处理包含说明文字）
   - 策略 3: 正则提取引号内容（最后降级）

**验证状态**: ✅ 所有 DeepSeek 测试通过 (18/18)
- 包含降级解析测试 `test_extract_tags_fallback_parsing`

---

## ⚠️ 已知问题（待修复）

### 问题 #2: `knowledge_id` vs `id` 命名不一致

**文件**: 数据库 Schema

**问题描述**:
- 数据库使用 `knowledge_id` 作为主键（领域特定命名）
- 部分代码可能期望 `id` 字段（通用命名）
- 向量存储 API 使用 `doc_id` 作为参数名

**影响范围**: 中等
- 需要在代码中保持一致性
- 新开发者容易混淆

**优先级**: 中

**修复计划**: 见 `docs/issues/SCHEMA_MIGRATION_PLAN.md`

---

### 问题 #3: `keywords` 字段类型不一致

**文件**: `src/storage/markdown_store.py`, `src/storage/sqlite_store.py`

**问题描述**:
- Entry 定义为 `list` 类型
- SQLite 存储为逗号分隔字符串
- StoreStep 需要手动转换类型

**当前转换代码**:
```python
# src/workflow/steps.py
keywords_str = ",".join(entry.keywords) if entry.keywords else ""
```

**影响范围**: 低
- 已有转换逻辑，功能正常
- 但代码不优雅

**优先级**: 低

**建议修复**:
- 方案 A: Entry 中也使用逗号分隔字符串
- 方案 B: SQLite 使用 JSON 类型存储列表

---

### 问题 #4: Entry 缺少字段验证

**文件**: `src/storage/markdown_store.py`

**问题描述**:
- `title` 和 `source_type` 声明为必填
- 但没有运行时验证（`__post_init__` 中未检查）
- 可能创建无效的 Entry 对象

**影响范围**: 低
- Processors 通常会正确填充字段
- 边界情况可能出错

**优先级**: 低

**建议修复**:
```python
def __post_init__(self):
    if not self.title or not self.title.strip():
        raise ValueError("title 不能为空")

    valid_source_types = {"wechat", "zhihu", "ai_chat", "text", ...}
    if self.source_type not in valid_source_types:
        raise ValueError(f"无效的 source_type: {self.source_type}")
```

---

## 🔨 技术债务清单

### 高优先级

1. ✅ **统一配置字段命名**（已修复：`targets`）
2. ✅ **修复引擎传参错误**（已修复：提取 `config` 字段）
3. ✅ **更新 `source_type` 枚举值**（已修复：添加 `ai_chat`, `text`, `test`）

### 中优先级

4. **统一数据库字段命名**（`knowledge_id`）
5. ✅ **统一长文档处理策略**（已修复：Embedder 统一为分块取平均）
6. ✅ **改进 API 调用稳定性**（已修复：DeepSeek JSON 三层降级解析）

### 低优先级

7. **添加 Entry 字段验证**
8. **统一 `keywords` 字段类型**
9. **添加性能监控和日志记录**
10. **补充向量存储的真实环境测试**

---

## 📊 Bug 统计

| 严重性 | 已修复 | 待修复 | 总计 |
|--------|-------|-------|------|
| **高** | 3 | 0 | 3 |
| **中** | 2 | 1 | 3 |
| **低** | 0 | 2 | 2 |
| **总计** | 5 | 3 | 8 |

---

## 🎯 修复优先级建议

### M5.1 已完成修复（2026-02-16）

1. ✅ 更新 `source_type` 枚举值（高优先级）
2. ✅ 统一长文档处理策略（中优先级）
3. ✅ 改进 DeepSeek JSON 解析（中优先级）

### 中期修复（M6-M7）

4. **统一数据库字段命名**（中优先级）
5. **添加 Entry 字段验证**（低优先级）

### 长期优化（M7 之后）

6. **统一 `keywords` 字段类型**（低优先级）
7. **性能监控和日志记录**（低优先级）
8. **补充测试覆盖率**（低优先级）

---

**文档维护者**: AI Agent (猫娘 幽浮喵)
**最后更新**: 2026-02-16
