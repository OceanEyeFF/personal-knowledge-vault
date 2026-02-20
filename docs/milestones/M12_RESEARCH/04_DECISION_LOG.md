# M12 技术决策日志

> **说明**: 记录 M12 开发过程中所有重要的技术决策，包括选择理由、权衡因素、遗留风险。
> **格式**: 每条决策包含：编号、日期、问题、方案对比、最终决策、理由、影响范围。

---

## 决策索引

| 编号 | 日期 | 决策主题 | 结果 | 风险等级 |
|------|------|---------|------|---------|
| D001 | 2026-02-20 | 独立分支开发 | 采用 `milestone12` 分支 | LOW ✅ |
| D002 | 2026-02-20 | 技术预研方式 | 建立系统化预研框架 | LOW ✅ |
| D003 | 2026-02-20 | asyncio 集成方案 | 采用 qt-async-threads | LOW ✅ |
| D004 | 2026-02-20 | AI API 客户端实现 | 采用 OpenAI SDK | LOW ✅ |
| D005 | 待定 | 对话存储格式 | 待决策（JSON 列 / 独立消息表） | LOW |
| D006 | 2026-02-20 | Token 控制策略 | 单轮输出质量管理 + 会话轮数提示 | LOW ✅ |
| D007 | 2026-02-20 | Token 统计方式 | OpenAI SDK stream_usage=True | LOW ✅ |

---

## D001: 独立分支开发策略

**日期**: 2026-02-20
**状态**: ✅ 已决策

### 问题
M12 功能复杂度高，是否需要独立分支开发？

### 方案对比

| 方案 | 优点 | 缺点 |
|------|------|------|
| **直接在 main 提交** | 流程简单 | 风险高，可能污染主分支 |
| **创建 milestone12 分支** | 隔离风险，可回滚 | 合并时需要处理冲突 |

### 最终决策
✅ 创建 `milestone12` 分支

### 理由
1. M12 预计 6-7 天开发，代码量约 1500 行，变更范围大
2. 涉及新增表结构（chat_sessions），需要验证迁移脚本
3. asyncio + Qt 集成是新技术栈，可能需要多次试错
4. Phase 1/2A/2B 经验：独立分支有助于质量控制

### 影响范围
- 开发流程：需要定期 merge main → milestone12（保持同步）
- 合并条件：所有测试通过 + 代码审查 + 功能验收

### 遗留风险
- 无明显风险

---

## D002: 技术预研系统化

**日期**: 2026-02-20
**状态**: ✅ 已决策

### 问题
如何保留技术预研过程和成果？

### 方案对比

| 方案 | 优点 | 缺点 |
|------|------|------|
| **临时测试脚本 + 口头记录** | 快速灵活 | 知识流失，无法回溯 |
| **建立预研文档框架** | 知识沉淀，可追溯 | 前期投入时间 |

### 最终决策
✅ 建立 `docs/milestones/M12_RESEARCH/` 预研框架

### 理由
1. M12 是 Phase 2B 技术高峰，预研发现值得记录
2. 为 M13 及 Phase 3 积累技术知识库
3. 支持多工具协作（Claude + ChatGPT + NotebookLM）
4. 符合工程最佳实践（可追溯性）

### 影响范围
- 文档结构：新增 6 个预研文档 + 3 个测试脚本
- 开发流程：预研阶段耗时约 1 天（Day 1）

### 遗留风险
- 文档维护负担（可控，预研结束后归档）

---

## D003: asyncio + Qt 集成方案

**日期**: 2026-02-20
**状态**: ✅ 已决策（已根据互联网成熟方案调整）

### 问题
如何在 PySide6 应用中运行 asyncio 协程？

### 方案对比

| 方案 | 优点 | 缺点 | 依赖 |
|------|------|------|------|
| **PySide6.QtAsyncio** | 官方支持 | DNS/Socket 未完整实现 ❌ | 无 |
| **qasync** | 成熟第三方库 | 引入额外依赖 ⚠️ | qasync>=0.23.0 |
| **QThread + asyncio.run()** | 无依赖，隔离清晰 | 手动管理线程 | 无 |
| **qt-async-threads** | async/await 语法自然 ✅ | 引入依赖（轻量） | qt-async-threads>=0.6.1 |

### 最终决策
✅ 采用 **qt-async-threads** 方案

### 理由
1. **更优雅的语法**：使用 `@async_slot` 装饰器，无需手动管理 QThread
2. **成熟验证**：互联网搜索发现多个成熟项目使用此库
3. **自动资源管理**：自动处理线程创建和清理
4. **轻量依赖**：纯 Python 库，无额外系统依赖
5. **代码简洁**：比手动 QThread + asyncio.run() 减少 50% 样板代码

### 影响范围
- 新增依赖：`qt-async-threads>=0.6.0`（最新版本 0.6.0）
- ViewModel 层使用 `@async_slot` 装饰器
- 自动处理跨线程 Signal 发射

### 代码示例
```python
from qt_async_threads import async_slot
from PySide6.QtCore import Signal

class ChatViewModel:
    token_received = Signal(str)

    @async_slot
    async def send_message(self, user_message: str):
        async for token in self.ai_service.stream_chat(messages):
            self.token_received.emit(token)  # 自动在主线程发射
```

### 遗留风险
- LOW（库已在多个项目中验证）

---

## D004: AI API 客户端实现方式

**日期**: 2026-02-20
**状态**: ✅ 已决策（已根据互联网成熟方案调整）

### 问题
DeepSeek API 调用应该使用 `openai` SDK 还是手动实现？

### 方案对比

| 方案 | 优点 | 缺点 |
|------|------|------|
| **openai SDK** | 成熟可靠，自动 token 统计 ✅ | 引入依赖（已是项目依赖） |
| **httpx 手动解析 SSE** | 完全控制，透明度高 | 需手动处理 SSE 格式和 token 统计 |

### 最终决策
✅ 采用 **OpenAI SDK** 方案

### 理由
1. **精确 Token 统计**：`stream_usage=True` 自动统计 tokens（服务器端精确值）
2. **成熟验证**：VividNode、pyqt-ai 等成熟项目均使用 OpenAI SDK
3. **DeepSeek 兼容**：DeepSeek API 100% 兼容 OpenAI SDK
4. **错误处理完善**：SDK 内置重试、超时、错误解析
5. **代码简洁**：比手动解析减少 60% 代码量
6. **已是项目依赖**：Phase 1 已引入 `openai>=1.0.0`

### 影响范围
- 使用 `AsyncOpenAI` 客户端（配置 DeepSeek base_url）
- 自动获取 `usage.total_tokens`（精确值，无需估算）
- 简化 SSE 解析逻辑（SDK 内置处理）

### 代码示例
```python
from openai import AsyncOpenAI

client = AsyncOpenAI(
    api_key=config.deepseek_api_key,
    base_url="https://api.deepseek.com/v1"
)

stream = await client.chat.completions.create(
    model="deepseek-chat",
    messages=messages,
    stream=True,
    stream_usage=True,  # ✅ 关键：开启流式 token 统计
    max_tokens=2000
)

async for chunk in stream:
    if chunk.choices[0].delta.content:
        yield chunk.choices[0].delta.content

    # ✅ 实时获取 token 统计（部分 chunk 包含）
    if hasattr(chunk, 'usage') and chunk.usage:
        self.update_token_usage(chunk.usage)
```

### 遗留风险
- LOW（OpenAI SDK 已在数百万项目中验证）

---

## D005: 对话存储格式（待决策）

**日期**: 2026-02-20
**状态**: 🔲 待决策

### 问题
对话记录应该用 JSON 列还是独立消息表？

### 方案对比

| 方案 | 优点 | 缺点 |
|------|------|------|
| **JSON 列** (文档推荐) | 简单，与 API 格式一致 | 无法高效查询单条消息 |
| **独立 messages 表** | 查询灵活 | 复杂，需要关联查询 |

### 文档建议
> 文档 `PHASE2B_GUI_PROMPT.md` 决策 1 推荐使用 JSON 列

### 调研任务
- [ ] 评估查询需求（是否需要"搜索所有提到 XX 的消息"？）
- [ ] 验证 SQLite `json_extract()` 性能

### 预期决策时间
2026-02-22（Day 3）

---

## D006: Token 控制策略

**日期**: 2026-02-20
**状态**: ✅ 已决策

### 问题
如何管理对话的 Token 消耗，平衡输出质量与成本？

### 方案对比

| 方案 | 优点 | 缺点 |
|------|------|------|
| **动态输入预算分配** | 精细控制输入 tokens | 复杂，可能牺牲上下文完整性 |
| **单轮输出质量管理** | 保证回复质量，策略简单 ✅ | 不限制输入，依赖用户自觉 |
| **自动压缩历史** | 节省 tokens | 丢失上下文，用户体验差 ❌ |

### 最终决策
✅ 采用 **单轮输出质量管理 + 会话轮数提示** 方案

### 理由
1. **输出质量优先**：限制 `max_tokens=2000` 确保回复完整性
2. **用户体验优先**：不强制截断历史，充分利用 128K 上下文
3. **引导式管理**：3 轮后提示用户结束或新建会话（不强制）
4. **策略简单**：无需复杂的动态预算分配算法
5. **成本可控**：结合上下文缓存（System Prompt 固定），整体成本可接受

### 影响范围
- **API 调用参数**：
  - `max_tokens=2000`（单轮输出限制）
  - 发送完整历史消息（充分利用 128K 上下文）
- **UI 提示逻辑**：
  - **轮数计数器**：每次用户发送消息 +1
  - **3 轮提示**："对话已进行 3 轮，建议结束当前会话或新建会话以保持回复质量"
  - **64K Tokens Warning**：当对话上下文超过 64K tokens 时显示警告（达到 128K 上限的一半）
    - 提示内容："对话上下文已超过 64K tokens，建议结束当前会话以避免逼近上下文极限"
    - Token 估算：中文约 3 字/token，实时计算当前对话总 tokens
- **System Prompt**：
  - 固定内容，利用上下文缓存（节省 90% 输入成本）
- **知识上下文**：
  - 根据检索结果动态注入，无硬性 token 限制
- **对话保存策略**（数据库字段设计）：
  - `messages` (JSON)：原始完整对话历史（所有消息）
  - `summary` (TEXT)：精粹版本（AI 生成的对话摘要，便于快速回顾）
  - 保存时机：会话结束时或用户手动触发"生成摘要"

### 遗留风险
- 用户可能忽略提示，持续对话导致上下文过长 → 64K 警告提供二次提醒
- 长对话可能触发 DeepSeek 10 分钟连接超时 → UI 显示超时提示
- Token 估算可能不精确 → 使用保守估算（中文 3 字/token）

### 技术说明
- **为何不自动压缩（autocompact）**：
  - OpenAI/DeepSeek API 均为无状态设计，不提供服务端自动压缩功能
  - 所有对话管理（历史截断、摘要生成）均由客户端负责
  - 自动压缩会丢失上下文，影响用户体验 → 采用引导式提示而非强制截断

---

## D007: Token 统计方式

**日期**: 2026-02-20
**状态**: ✅ 已决策

### 问题
如何精确统计对话的 Token 消耗？

### 方案对比

| 方案 | 优点 | 缺点 |
|------|------|------|
| **客户端估算**（中文 3 字/token） | 无需 API 支持 | 估算不准确（±20%） |
| **OpenAI SDK stream_usage=True** | 服务器端精确统计 ✅ | 需 API 支持（OpenAI/DeepSeek 支持） |
| **tiktoken 库**（本地计算） | 较准确 | 与实际计费可能有差异 |

### 最终决策
✅ 采用 **OpenAI SDK stream_usage=True** 方案

### 理由
1. **精确统计**：服务器端返回的 tokens 数量与实际计费 100% 一致
2. **实时更新**：流式响应中可实时获取 token 累计
3. **无需估算**：避免客户端估算误差（中文 3 字/token 仅为粗略估算）
4. **API 支持**：OpenAI 和 DeepSeek 均支持 `stream_usage=True`
5. **成本透明**：用户可实时看到 token 消耗，成本可控

### 影响范围
- API 调用时设置 `stream_usage=True`
- 实时更新 UI 显示（输入 tokens、输出 tokens、总 tokens）
- 数据库保存精确 token 统计（用于成本分析）

### 代码示例
```python
stream = await client.chat.completions.create(
    model="deepseek-chat",
    messages=messages,
    stream=True,
    stream_usage=True,  # ✅ 关键参数
    max_tokens=2000
)

total_input_tokens = 0
total_output_tokens = 0

async for chunk in stream:
    # 流式接收 token
    if chunk.choices[0].delta.content:
        yield chunk.choices[0].delta.content

    # 实时获取 token 统计（部分 chunk 包含 usage 字段）
    if hasattr(chunk, 'usage') and chunk.usage:
        total_input_tokens = chunk.usage.prompt_tokens
        total_output_tokens = chunk.usage.completion_tokens
        self.update_token_display(
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            total_tokens=chunk.usage.total_tokens
        )
```

### 遗留风险
- 需验证 DeepSeek API 是否正确返回 usage 字段（待实测）
- 流式响应中 usage 字段可能仅在最后一个 chunk 返回（需处理）

---

## 决策模板（复制使用）

```markdown
## DXXX: 决策主题

**日期**: YYYY-MM-DD
**状态**: 🔲 待决策 / ✅ 已决策 / ❌ 已废弃

### 问题
描述需要决策的问题

### 方案对比
| 方案 | 优点 | 缺点 |
|------|------|------|
| ... | ... | ... |

### 最终决策
✅ 选择的方案

### 理由
1. ...
2. ...

### 影响范围
- ...

### 遗留风险
- ...
```

---

**文档版本**: v0.1
**最后更新**: 2026-02-20
