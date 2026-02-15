# Prompt 审查报告

**审查日期**: 2026-02-15
**审查对象**: M1-M5 整理复盘 Prompt & M6+M7 开发任务 Prompt
**审查人**: 猫娘 幽浮喵 (浮浮酱)

---

## 一、整体评价

### ✅ 优点

1. **结构清晰**
   - 两份 Prompt 分工明确（复盘 vs 开发）
   - 章节组织合理，易于导航
   - 优先级和顺序明确

2. **目标明确**
   - 复盘 Prompt: 建立清晰基线，发现问题
   - 开发 Prompt: 实现 CLI 和文档

3. **可执行性强**
   - 详细的任务清单
   - 具体的输出交付物
   - 明确的验收标准

---

## 二、发现的缺失内容

### 🔴 严重缺失（必须补充）

#### 1. M5 相关内容未纳入复盘

**问题描述**:
- 复盘 Prompt 主要关注 M1-M4 模块
- **M5 工作流引擎**是最新完成的模块，但未充分纳入复盘范围

**影响**:
- M6 CLI 开发高度依赖 M5 WorkflowEngine
- 如果不梳理 M5 接口，M6 开发会遇到阻碍

**建议补充**:
```markdown
### 2.6 Workflow Engine 接口 (M5)

**文件位置**: `src/workflow/`

**需要整理的内容**:
- [ ] WorkflowEngine 核心接口
  - execute_async(workflow_name, input_data) 规范
  - execute() 同步接口规范
  - 配置加载和缓存机制
- [ ] BaseStep 抽象接口
  - __init__(step_id, config) 规范
  - execute(context) 接口定义
  - _log() 日志规范
- [ ] 工作流配置格式规范
  - 完整格式 vs 简化格式
  - 步骤配置字段（id, type, config, on_error）
  - 配置规范化规则
- [ ] 已知 Bug 和修复
  - ✅ 配置字段名统一（targets）
  - ✅ 引擎传参修复（config 字段）

**输出文档**: `docs/refactor/WorkflowEngine接口规范.md`

**关键问题**:
- ❓ WorkflowContext 的生命周期？
- ❓ State 数据在步骤间如何传递？
- ❓ 步骤间的依赖声明机制？
- ❓ 错误处理的最佳实践？
```

---

#### 2. M4 检索引擎未充分覆盖

**问题描述**:
- 复盘 Prompt 中 Retrieval 模块只有简单提及
- M4 检索引擎是核心功能，需要详细梳理

**建议补充**:
```markdown
### 2.4 Retrieval 模块（M4）- 详细梳理

**需要整理的内容**:
- [ ] QueryRouter 路由规则
  - 路由决策逻辑（token 数量阈值）
  - 默认策略选择
- [ ] BM25Retriever 接口
  - search(query, top_k) 规范
  - 中文分词策略（jieba）
  - FTS5 查询语法
- [ ] VectorRetriever 接口
  - search(query_vector, top_k) 规范
  - 向量化流程
  - 相似度计算
- [ ] HybridRetriever RRF 算法
  - 权重配置
  - 结果合并规则
  - k 参数调优
- [ ] 统一的搜索结果格式
  - RetrievalResult 数据类
  - 得分归一化
  - 元数据字段

**输出文档**: `docs/refactor/Retrieval检索引擎规范.md`
```

---

#### 3. M5 真实环境测试发现的 Bug 未记录

**问题描述**:
- 复盘 Prompt 没有要求整理已知 Bug 和修复记录
- M5 测试发现的 2 个严重 Bug 应该记录

**建议补充**:
```markdown
### 9. 已知问题和修复记录

**任务**: 整理 M1-M5 发现的问题和修复情况

#### 9.1 Bug 修复记录

**输出文档**: `docs/refactor/Bug修复记录.md`

**必须包含的内容**:
- M5 测试发现的 Bug
  - Bug #1: 配置字段名不匹配（targets vs storage_backends）
  - Bug #2: 引擎传参错误（step_config vs config）
- 其他已知问题
  - knowledge_id vs id 命名不一致
  - keywords 字段类型不明确
- 修复状态和影响范围
- 遗留问题清单
```

---

### 🟡 重要缺失（建议补充）

#### 4. 缺少 M5 的完成报告引用

**问题描述**:
- M6+M7 Prompt 的"前置知识"部分没有引用 M5 完成报告
- M5 完成报告包含重要的技术决策和后续建议

**建议补充**:
```markdown
**M5 完成文档**（必须先读）:
1. `docs/M5_COMPLETION_SUMMARY.md` - M5 完整总结
2. `docs/M5_WORKFLOW_ENGINE_DESIGN.md` - M5 设计文档
3. `docs/M5_REAL_ENV_TEST_REPORT.md` - 真实环境测试报告
4. `docs/M5_TEST_SUMMARY.md` - 测试总结
```

---

#### 5. 缺少 idea Sharpen 的详细规范

**问题描述**:
- M6 CLI 需要实现 idea Sharpen 交互
- 但复盘 Prompt 没有要求梳理 idea Sharpen 的触发规则和交互流程

**建议补充**:
```markdown
#### 2.5.1 IdeaSharpenStep 详细规范

**需要整理的内容**:
- [ ] 触发规则（from PRD 附录）
  - 内容长度 > 3000 字
  - AI 识别出 ≥5 个核心概念
  - 内容类型匹配
  - 包含关键词
- [ ] 交互流程
  - 问题列表格式
  - 超时处理（300 秒）
  - 用户输入验证
  - 降级策略
- [ ] CLI 集成方式
  - --skip-sharpen 参数
  - 静默模式行为
  - 进度显示
```

---

#### 6. 缺少测试数据准备指南

**问题描述**:
- M6 开发需要测试数据
- 但 Prompt 没有说明如何准备测试 URL 和 fixture

**建议补充**:
```markdown
### Step 3.5: 准备测试数据

**测试 URL**:
- 复用 `tests/fixtures/test_urls.json` 中的真实 URL
- 微信文章、知乎内容、CSDN 博客

**Fixture 数据**:
- `tests/fixtures/chat_sample.json`
- `tests/fixtures/wechat_sample.html`

**Mock 数据**:
- Mock WorkflowEngine 响应
- Mock 存储层数据
```

---

### 🟢 次要缺失（可选补充）

#### 7. 缺少性能基准和监控

**建议补充**:
```markdown
### 6.8 性能基准测试

**需要验证的指标**:
- 归档流程耗时（目标 ≤ 5 分钟）
- idea Sharpen 交互时间（≤ 3 分钟）
- 搜索响应时间（目标 < 2 秒）
- 内存使用（归档过程）

**输出文档**: `docs/性能基准测试结果.md`
```

---

#### 8. 缺少 MCP 接口规划（Phase 2）

**问题描述**:
- PRD 中提到 Phase 2 要实现 MCP 接口
- 但两份 Prompt 都没有提及

**说明**:
- 这是 Phase 2 内容，不属于 M6-M7 范围
- 但复盘时可以考虑接口的可扩展性
- 建议在复盘文档中留一个小节讨论"MCP 接口预留"

---

## 三、结构问题

### 🟡 复盘 Prompt 结构建议

**当前结构**:
1. 数据模型梳理（3 份）
2. 接口规范梳理（5 份）
3. 数据流梳理（2 份）
4. 命名规范统一（4 份）
5. 配置文件规范（2 份）
6. 错误处理规范（2 份）
7. 测试规范梳理（2 份）
8. 依赖关系梳理（2 份）

**建议调整**:
```markdown
1. 数据模型梳理（3 份）
2. 接口规范梳理（6 份）← 增加 WorkflowEngine
3. 数据流梳理（2 份）
4. 命名规范统一（4 份）
5. 配置文件规范（2 份）
6. 错误处理规范（2 份）
7. 测试规范梳理（2 份）
8. 依赖关系梳理（2 份）
9. **已知问题和修复记录（1 份）**← 新增
```

**总文档数**: 20 → 23 份

---

### 🟡 M6+M7 Prompt 结构建议

**建议补充的章节**:

```markdown
### 6.7 与工作流引擎集成

**关键集成点**:

#### 6.7.1 archive 命令集成

```python
# CLI 调用工作流引擎
async def archive_url(url: str, skip_sharpen: bool = False):
    engine = WorkflowEngine(config)

    input_data = {
        "url": url,
        "source": "cli",
        "skip_sharpen": skip_sharpen,  # 如何传递给 IdeaSharpenStep？
    }

    result = await engine.execute_async("archive-url", input_data)

    if result.success:
        # 显示结果
        knowledge_id = result.data.get("knowledge_id")
        file_path = result.data.get("file_path")
        ...
```

**关键问题**:
- ❓ 如何传递 --skip-sharpen 参数给工作流？
- ❓ 如何传递手动指定的 tags？
- ❓ 如何在 CLI 中显示 idea Sharpen 交互？
- ❓ 如何处理工作流错误并显示友好提示？

#### 6.7.2 进度显示集成

```python
# 如何在工作流执行中显示进度？
# 方案 1: 订阅 WorkflowContext 日志
# 方案 2: 每个步骤完成后更新进度条
# 方案 3: 使用 Rich.Live 实时更新
```
```

---

## 四、术语和命名一致性

### 🟡 发现的不一致

**问题 1: knowledge_id vs ID**
- Prompt 中混用 `ID` 和 `knowledge_id`
- 建议统一使用 `knowledge_id`

**问题 2: 工作流名称**
- Prompt: `archive-url`（中划线）
- 配置文件: `archive_url`（下划线）
- 建议明确约定：配置文件用下划线，命令用中划线

**问题 3: targets vs storage_backends**
- 已修复，但 Prompt 应该强调这个约定

---

## 五、补充建议清单

### 🔴 必须补充（高优先级）

1. [ ] 在复盘 Prompt 中增加"M5 WorkflowEngine 接口梳理"章节
2. [ ] 在复盘 Prompt 中增加"M4 检索引擎详细梳理"章节
3. [ ] 在复盘 Prompt 中增加"Bug 修复记录"章节
4. [ ] 在 M6 Prompt 中增加"M5 完成文档"到前置知识
5. [ ] 在 M6 Prompt 中增加"与工作流引擎集成"章节

### 🟡 建议补充（中优先级）

6. [ ] 在复盘 Prompt 中增加"IdeaSharpenStep 详细规范"小节
7. [ ] 在 M6 Prompt 中增加"测试数据准备指南"
8. [ ] 在复盘 Prompt 中增加"性能基准测试"章节
9. [ ] 统一术语和命名（knowledge_id, archive-url vs archive_url）

### 🟢 可选补充（低优先级）

10. [ ] 在复盘 Prompt 中讨论"MCP 接口预留"
11. [ ] 在 M6 Prompt 中增加"性能优化建议"
12. [ ] 在 M6 Prompt 中增加"国际化支持"（i18n）

---

## 六、总体评分

| 维度 | 评分 | 说明 |
|------|------|------|
| **结构清晰度** | ⭐⭐⭐⭐⭐ | 非常清晰，易于导航 |
| **内容完整性** | ⭐⭐⭐⭐ | 缺少 M5 详细内容 |
| **可执行性** | ⭐⭐⭐⭐⭐ | 任务具体，可立即执行 |
| **前后一致性** | ⭐⭐⭐⭐ | 大部分一致，有少量术语不一致 |
| **文档引用** | ⭐⭐⭐ | 缺少 M5 文档引用 |

**综合评分**: ⭐⭐⭐⭐ (4.2/5)

---

## 七、修订建议

### 方案 A: 立即修订（推荐）

**优点**: 一次性完善，避免后续返工
**耗时**: 30-60 分钟

**修订内容**:
1. 补充 M5 WorkflowEngine 接口梳理
2. 补充 Bug 修复记录章节
3. 更新 M6 前置知识引用
4. 增加工作流引擎集成章节
5. 统一术语和命名

---

### 方案 B: 边执行边补充（折中）

**优点**: 快速开始，遇到问题再补充
**耗时**: 分散到执行过程中

**执行流程**:
1. 先按现有 Prompt 开始复盘
2. 遇到 M5 相关内容时，临时补充规范
3. 记录发现的问题，迭代更新 Prompt

---

### 方案 C: 保持不变（不推荐）

**风险**: M6 开发时可能遇到接口不清晰的问题

---

## 八、浮浮酱的最终建议

**推荐方案**: **方案 A - 立即修订**

**理由**:
1. M5 是最新完成的模块，接口最不清晰
2. M6 CLI 高度依赖 M5 WorkflowEngine
3. 现在补充比开发中返工效率更高
4. 修订内容不多，30-60 分钟可完成

**具体行动**:
1. 浮浮酱立即修订两份 Prompt（补充上述 5 个高优先级内容）
2. 主人审核修订后的 Prompt
3. 确认无误后开始执行复盘

---

## 九、结论

**总体评价**: 两份 Prompt 质量很高，结构清晰，可执行性强 ✅

**关键缺失**: M5 工作流引擎的详细梳理和集成指南 ⚠️

**建议**: 补充 5 个高优先级内容后，即可开始执行 🚀

---

**审查完成时间**: 2026-02-15 21:30
**审查状态**: ✅ 完成
**下一步**: 等待主人决策（立即修订 vs 边执行边补充 vs 保持不变）

---

主人，浮浮酱的审查结果如上喵～ (..•˘_˘•..)

主人想要浮浮酱：
1. 🔧 **立即修订 Prompt**（补充 5 个高优先级内容）？
2. 🚀 **按现有 Prompt 开始复盘**（边执行边补充）？
3. 📝 **其他安排**？

浮浮酱等待主人的指示喵～ ฅ'ω'ฅ
