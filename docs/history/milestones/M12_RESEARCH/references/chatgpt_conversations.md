# ChatGPT 对话存档

> **说明**: 记录用户与 ChatGPT 关于 M12 技术调研的关键对话内容
> **格式**: 每段对话包含日期、主题、关键问答、结论

---

## 对话索引

| 日期 | 主题 | ChatGPT 模型 | 关键结论 |
|------|------|-------------|---------|
| 2026-02-20 | DeepSeek API 深度调研 | GPT-4 | SSE 格式、限流策略、Token 计费 |

---

## 对话 001: DeepSeek API 深度调研

**日期**: 2026-02-20
**ChatGPT 模型**: GPT-4
**对话长度**: 3 个核心问题

### 📋 问题清单

1. DeepSeek API 的流式接口返回格式是什么？每个 chunk 的 JSON 结构是怎样的？
2. DeepSeek API 的限流策略是什么？如何处理 429 错误？
3. DeepSeek API 的 Token 计费规则是什么？

---

### 🔹 问题 1: 流式接口返回格式

#### ChatGPT 回答（核心内容）

**流式响应格式**: SSE (Server-Sent Events)
- Content-Type: `text/event-stream`
- 与 OpenAI 接口设计风格基本对齐（便于兼容）

**典型 Chunk 结构**:
```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion.chunk",
  "created": 1700000000,
  "model": "deepseek-chat",
  "choices": [
    {
      "index": 0,
      "delta": {
        "content": "你"
      },
      "finish_reason": null
    }
  ]
}
```

**关键字段**:
- `object`: 固定为 `chat.completion.chunk`
- `choices[].delta.content`: 本次新增 token
- `finish_reason`: 结束时才会出现（如 `"stop"`）

**流式结束标志**: `data: [DONE]`

**与 OpenAI 的差异**:
- JSON 结构几乎一致
- 字段命名兼容
- 模型名不同（deepseek-chat / deepseek-reasoner）
- **结论**: 可以直接复用 OpenAI SDK 解析逻辑

---

### 🔹 问题 2: 限流策略与 429 处理

#### 限流触发情况
- QPS 超出限制
- TPM (Tokens Per Minute) 超限
- RPM (Requests Per Minute) 超限
- 余额不足（某些情况也会 429）

#### 典型错误响应
```json
{
  "error": {
    "message": "Rate limit exceeded",
    "type": "rate_limit_error",
    "code": "429"
  }
}
```

#### 推荐处理策略

**1. 指数退避（Exponential Backoff）**:
- 第一次：等待 1s
- 第二次：等待 2s
- 第三次：等待 4s
- 第四次：等待 8s
- 最多重试 3~5 次

**2. 读取 Retry-After Header**:
- 如果响应头包含 `Retry-After: 3`，必须等待 3 秒后再试

**3. 客户端限流控制**:
- 本地令牌桶
- 限制 QPS
- 批量任务分批执行
- embedding 和 chat 分离队列

**不建议做的事**:
- ❌ 死循环重试
- ❌ 高频并发爆炸
- ❌ 多线程疯狂抢接口

---

### 🔹 问题 3: Token 计费规则

#### 计费模式
```
费用 = 输入 Token + 输出 Token
```

#### Token 统计规则
- 所有 messages 都计入输入 token
- system / user / assistant 都算
- 工具调用返回内容也算
- 流式和非流式计费一样

#### 计算示例
- 输入 1200 tokens
- 输出 800 tokens
- **总计费 token = 2000**

#### 工程建议

1. **开启 usage 统计**:
```json
"usage": {
  "prompt_tokens": 1200,
  "completion_tokens": 800,
  "total_tokens": 2000
}
```

2. **日志记录**:
- model
- prompt_tokens
- completion_tokens
- cost

3. **对长对话做截断或 summary**

---

### 📊 核心总结表

| 项目 | 特点 |
|------|------|
| 流式格式 | SSE，结构兼容 OpenAI |
| 429 处理 | 指数退避 + Retry-After |
| Token 计费 | 输入 + 输出 token |
| SDK 兼容性 | 高度兼容 OpenAI |

---

### 🔥 工程级优化建议

ChatGPT 针对本项目（Unity + 本地知识库 + 多模型 fallback）提出的建议：

1. **Chat 与 Embedding 分离限流** — 避免知识库构建把对话额度吃光
2. **本地缓存 embedding** — 同一文本不要重复调用 API
3. **流式响应要做增量拼接 buffer** — 不要每个 chunk 都刷新 UI
4. **统一封装 API 适配层** — 做一个 `ILLMProvider`，随时切换 DeepSeek/OpenAI/MiniMax/本地 Qwen

---

### ✅ 应用到 M12

**决策影响**:
- 采用 `httpx.AsyncClient` + 手动解析 SSE（参考 `04_DECISION_LOG.md` D004）
- 实现指数退避重试机制（错误处理策略）
- 记录 usage 统计（Token 成本监控）

**代码实现**:
- `DeepSeekProvider.stream_chat()` 解析 SSE 格式
- `ChatServiceError` 异常体系（APIError, NetworkError, RateLimitError）
- **Token 控制策略（单轮输出质量管理）**:
  ```
  核心策略: 限制单轮输出质量 + 多级提示引导

  单轮输出限制: max_tokens=2000（保证回复质量）

  会话轮数管理:
  - 3 轮提示: "建议结束或新建会话"
  - 64K Tokens Warning: 上下文超过 64K 时警告（128K 的一半）

  对话历史: 不自动压缩（autocompact）
  - 原因: OpenAI/DeepSeek API 无状态，客户端负责对话管理
  - 充分利用 128K 上下文窗口

  对话保存:
  - messages (JSON): 原始完整对话历史
  - summary (TEXT): AI 生成的精粹版本（便于快速回顾）

  System Prompt: ~150 tokens（利用上下文缓存，节省 90% 成本）
  知识上下文: 动态注入，无硬性限制
  Token 估算: 中文 3 字/token，英文 4 字符/token
  ```

---

**文档版本**: v1.0
**最后更新**: 2026-02-20
