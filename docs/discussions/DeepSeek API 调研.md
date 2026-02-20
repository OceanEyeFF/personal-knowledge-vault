**You:**

  4. 使用 ChatGPT 深化调研（可选）

  主人可以问 ChatGPT：
  - "DeepSeek API 的流式接口返回格式是什么？每个 chunk 的 JSON 结构是怎样的？"      
  - "DeepSeek API 的限流策略是什么？如何处理 429 错误？"
  - "DeepSeek API 的 Token 计费规则是什么？"

  然后把对话存档到 references/chatgpt_conversations.md 喵～

* * *

**ChatGPT:**

很好，这三个问题本身就已经是「API 接入级别」的关键点了。我给你做一个**工程师视角的深度整理版答案**，你可以直接复制到：

代码references/chatgpt_conversations.md

作为调研存档。

---

# DeepSeek API 深度调研记录

## 1️⃣ DeepSeek API 的流式接口返回格式是什么？

DeepSeek 的 Chat Completions API 设计风格基本对齐 OpenAI 接口（便于兼容），流式返回采用：

代码Content-Type: text/event-stream

即 **SSE（Server-Sent Events）格式**

---

### 🔹 返回格式示例

请求参数：

JSON{  
  "model": "deepseek-chat",  
  "messages": [  
    {"role": "user", "content": "你好"}  
  ],  
  "stream": true  
}

---

### 🔹 流式响应结构

服务器会返回多条：

代码data: {json}

每一条是一个 chunk。

示例 chunk：

JSON{  
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

---

### 🔹 关键字段说明

| 字段 | 含义 |
| --- | --- |
| object | 固定为 `chat.completion.chunk` |
| choices[].delta.content | 本次新增 token |
| finish_reason | 结束时才会出现，例如 `"stop"` |

---

### 🔹 流式结束标志

最后会返回：

代码data: [DONE]

你在代码里必须监听这个字符串。

---

### 🔹 与 OpenAI 的差异

* JSON 结构几乎一致
* 字段命名兼容
* 不同模型名字不同（例如 deepseek-chat / deepseek-reasoner）

基本可以直接复用 OpenAI SDK 解析逻辑。

---

## 2️⃣ DeepSeek API 的限流策略是什么？如何处理 429？

### 🔹 限流触发情况

429 通常在以下情况出现：

* QPS 超出限制
* TPM（Tokens Per Minute）超限
* RPM（Requests Per Minute）超限
* 余额不足（某些情况也会 429）

---

### 🔹 典型错误响应

JSON{  
  "error": {  
    "message": "Rate limit exceeded",  
    "type": "rate_limit_error",  
    "code": "429"  
  }  
}

---

### 🔹 正确的处理策略

#### ✅ 1. 指数退避（Exponential Backoff）

推荐策略：

Plain text第一次：等待 1s  
第二次：等待 2s  
第三次：等待 4s  
第四次：等待 8s

最多重试 3~5 次。

---

#### ✅ 2. 读取 Retry-After Header

如果响应头包含：

代码Retry-After: 3

则必须等待 3 秒后再试。

---

#### ✅ 3. 客户端限流控制

工程建议：

* 做一个本地令牌桶
* 限制 QPS
* 批量任务分批执行
* embedding 和 chat 分离队列

---

### 🔹 不建议做的事

❌ 死循环重试  
❌ 高频并发爆炸  
❌ 多线程疯狂抢接口

---

## 3️⃣ DeepSeek API 的 Token 计费规则是什么？

DeepSeek 采用标准 LLM 计费模式：

代码费用 = 输入 Token + 输出 Token

---

### 🔹 Token 统计规则

* 所有 messages 都计入输入 token
* system / user / assistant 都算
* 工具调用返回内容也算
* 流式和非流式计费一样

---

### 🔹 计算示例

如果：

* 输入 1200 tokens
* 输出 800 tokens

总计费 token = 2000

---

### 🔹 工程建议

1. 在代码中开启 usage 统计：

JSON"usage": {  
  "prompt_tokens": 1200,  
  "completion_tokens": 800,  
  "total_tokens": 2000  
}

2. 在日志里记录：

代码model  
prompt_tokens  
completion_tokens  
cost

3. 对长对话做截断或 summary

---

## 🔥 工程级优化建议（给做系统的人）

既然你是做 Unity + 本地知识库 + 多模型 fallback 的，我给你几点进阶建议：

### ✅ 1. Chat 与 Embedding 分离限流

避免知识库构建把对话额度吃光。

### ✅ 2. 本地缓存 embedding

同一文本不要重复调用 API。

### ✅ 3. 流式响应要做增量拼接 buffer

不要每个 chunk 都刷新 UI。

### ✅ 4. 统一封装 API 适配层

做一个：

代码ILLMProvider

这样可以随时：

* DeepSeek
* OpenAI
* MiniMax
* 本地 Qwen

随意切换。

---

# 总结

| 项目 | 特点 |
| --- | --- |
| 流式格式 | SSE，结构兼容 OpenAI |
| 429 处理 | 指数退避 + Retry-After |
| Token 计费 | 输入 + 输出 token |
| SDK 兼容性 | 高度兼容 OpenAI |

---

如果你愿意，我可以下一步帮你写：

* 🔹 DeepSeek C# 封装示例（适配 Unity）
* 🔹 Python httpx 高可靠封装模板
* 🔹 带自动限流和重试的 API Wrapper

你现在这个项目阶段，其实已经可以做一个**多模型统一调度层**了。