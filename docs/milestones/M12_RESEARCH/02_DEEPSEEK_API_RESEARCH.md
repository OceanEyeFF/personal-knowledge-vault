# DeepSeek API 流式调用调研

> **调研日期**: 2026-02-20
> **调研人**: Claude Code + 用户（ChatGPT/NotebookLM 辅助）
> **状态**: 🔲 待开始

---

## 🎯 调研目标

深入理解 DeepSeek API 的流式接口规范，确保能够正确实现：
1. ✅ SSE（Server-Sent Events）格式解析
2. ✅ 错误码处理（4xx/5xx）
3. ✅ 限流策略（429 错误）
4. ✅ Token 计费与配额管理

---

## 📋 调研问题清单

### 问题 1: DeepSeek API 基础信息
- **官方文档**: https://platform.deepseek.com/docs
- **Base URL**: `https://api.deepseek.com/v1`
- **需要验证**:
  - [ ] 流式接口路径：`/chat/completions` ?
  - [ ] 请求头要求：`Authorization: Bearer {API_KEY}` ?
  - [ ] 请求体格式：OpenAI 兼容 JSON ?
  - [ ] 响应格式：SSE 标准 `data: {...}` ?

### 问题 2: 流式响应格式
- **典型 SSE 事件**:
  ```
  data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1234567890,"model":"deepseek-chat","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

  data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1234567890,"model":"deepseek-chat","choices":[{"index":0,"delta":{"content":"你"},"finish_reason":null}]}

  data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1234567890,"model":"deepseek-chat","choices":[{"index":0,"delta":{"content":"好"},"finish_reason":null}]}

  data: [DONE]
  ```
- **需要确认**:
  - [ ] `delta.content` 字段是否总是存在？（首个 chunk 可能只有 `role`）
  - [ ] `finish_reason` 何时出现？（`stop` / `length` / `content_filter`）
  - [ ] `[DONE]` 标记是否总是存在？

### 问题 3: 错误处理
- **HTTP 错误码**:
  - [ ] 401 Unauthorized — API Key 无效
  - [ ] 429 Too Many Requests — 限流
  - [ ] 500 Internal Server Error — 服务端错误
  - [ ] 503 Service Unavailable — 过载
- **错误响应格式**:
  ```json
  {
    "error": {
      "message": "Invalid API key",
      "type": "invalid_request_error",
      "code": "invalid_api_key"
    }
  }
  ```

### 问题 4: 限流策略
- **限流指标**:
  - [ ] RPM (Requests Per Minute) — 每分钟请求数
  - [ ] TPM (Tokens Per Minute) — 每分钟 Token 数
  - [ ] 响应头是否包含剩余配额？（`X-RateLimit-Remaining-Requests` ?）
- **超限处理**:
  - [ ] 429 响应体是否包含 `retry_after` 字段？
  - [ ] 推荐重试策略：指数退避（1s, 2s, 4s, ...）

### 问题 5: Token 计费
- **Token 计算**:
  - [ ] 输入 Token：messages 中所有 content 的总和
  - [ ] 输出 Token：AI 生成的 content
  - [ ] 是否有 Token 预估 API？
- **成本控制**:
  - [ ] `max_tokens` 参数限制输出长度
  - [ ] 对话历史截断策略（超过 N 轮后删除旧消息）

---

## 🧪 验证实验

### 实验 1: 最小流式请求

**测试脚本**: `tests/manual_test_m12/test_deepseek_stream.py`

**测试用例**:
```python
# 最简单的流式请求
messages = [{"role": "user", "content": "你好"}]
# 预期：收到 SSE 事件流，每个 chunk 包含一个字
```

**预期输出**:
```
data: {"choices":[{"delta":{"role":"assistant"}}]}
data: {"choices":[{"delta":{"content":"你"}}]}
data: {"choices":[{"delta":{"content":"好"}}]}
data: {"choices":[{"delta":{"content":"！"}}]}
data: [DONE]
```

**实际结果**:
- [ ] 待测试

### 实验 2: 错误场景测试

**测试用例**:
1. 无效 API Key → 预期 401
2. 空消息列表 → 预期 400
3. 超长输入（> 16k tokens）→ 预期 400

**实际结果**:
- [ ] 待测试

---

## 📊 与 OpenAI API 对比

| 特性 | DeepSeek API | OpenAI API | 兼容性 |
|------|-------------|-----------|--------|
| 请求格式 | OpenAI 兼容 | - | ✅ 完全兼容 |
| 流式接口 | SSE `data: {...}` | 同左 | ✅ 兼容 |
| 错误格式 | 同 OpenAI | - | ✅ 兼容 |
| 限流策略 | 待确认 | 429 + `retry_after` | ⚠️ 待验证 |
| Token 计费 | 待确认 | 按输入/输出分别计费 | ⚠️ 待验证 |

---

## 🛡️ 安全注意事项

1. **API Key 保护**:
   - ❌ 不得硬编码在代码中
   - ✅ 从环境变量或 `.env` 文件读取
   - ✅ 日志中脱敏（`sk-xxxx...` 只显示前 4 位）

2. **SSRF 防护**:
   - ✅ 仅连接官方域名 `api.deepseek.com`
   - ✅ 拒绝自定义 base_url（除非明确配置）

3. **超时控制**:
   - ✅ 连接超时：10s
   - ✅ 读取超时：30s（流式可能较慢）
   - ✅ 总超时：60s

---

## ✅ 调研结论（待补充）

**API 兼容性**: 🔲 待验证（预期与 OpenAI 完全兼容）

**流式接口格式**: 🔲 待确认

**限流策略**: 🔲 待确认

**推荐实现方案**:
- httpx.AsyncClient + 手动解析 SSE（不依赖 `openai` SDK）
- 理由：完全控制流程，透明度高，无额外依赖

---

## 📚 参考资料

- [ ] [DeepSeek 官方文档](https://platform.deepseek.com/docs)
- [ ] [OpenAI API Reference](https://platform.openai.com/docs/api-reference)
- [ ] [SSE 规范 (MDN)](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events)
- [ ] ChatGPT 对话存档 → `references/chatgpt_conversations.md`

---

**文档版本**: v0.1
**最后更新**: 2026-02-20
