# M12 技术预研索引

> **Milestone**: M12 - AI 对话交互 (v0.8.0)
> **预研开始日期**: 2026-02-20
> **负责人**: Claude Code (幽浮喵) + 用户
> **状态**: 🔬 进行中

---

## 🎯 预研目标

在正式开发 M12 AI 对话功能之前，通过技术预研验证以下关键技术点：

1. ✅ **asyncio + Qt 事件循环集成** — QThread 隔离方案可行性
2. ✅ **DeepSeek API 流式调用** — SSE 格式解析正确性
3. ✅ **线程安全性** — Signal/Slot 跨线程通信稳定性
4. ✅ **错误处理** — 网络异常、API 错误的优雅降级

---

## 📚 预研文档清单

| 序号 | 文档 | 调研主题 | 状态 | 产出 |
|------|------|---------|------|------|
| 01 | [asyncio + Qt 集成调研](./01_ASYNCIO_QT_RESEARCH.md) | QThread + asyncio.run() 方案验证 | 🔲 待开始 | 可行性结论 + 示例代码 |
| 02 | [DeepSeek API 调研](./02_DEEPSEEK_API_RESEARCH.md) | 流式接口格式、错误码、限流策略 | 🔲 待开始 | API 规范文档 + 测试脚本 |
| 03 | [线程安全性调研](./03_THREAD_SAFETY_RESEARCH.md) | Qt Signal 跨线程可靠性验证 | 🔲 待开始 | 安全模式总结 |
| 04 | [技术决策日志](./04_DECISION_LOG.md) | 为什么选 A 而非 B 的记录 | 📝 持续更新 | 决策依据存档 |

---

## 🧪 手动测试脚本清单

| 脚本 | 测试目标 | 运行命令 | 状态 |
|------|---------|---------|------|
| `test_deepseek_stream.py` | DeepSeek 流式 API 可用性 | `python tests/manual_test_m12/test_deepseek_stream.py` | 🔲 待编写 |
| `test_qthread_asyncio.py` | QThread + asyncio 集成 | `python tests/manual_test_m12/test_qthread_asyncio.py` | 🔲 待编写 |
| `test_httpx_async.py` | httpx.AsyncClient 基础验证 | `python tests/manual_test_m12/test_httpx_async.py` | 🔲 待编写 |

---

## 📖 参考资料存档

- [外部资料链接](./references/external_links.md) — 官方文档、技术博客、Stack Overflow
- [ChatGPT 对话存档](./references/chatgpt_conversations.md) — 用户与 ChatGPT 的调研对话记录
- [NotebookLM 笔记](./references/notebooklm_notes.md) — NotebookLM 生成的知识整理

---

## ⚠️ 关键风险点

| 风险 | 影响 | 应对方案 | 状态 |
|------|------|---------|------|
| Qt 事件循环与 asyncio 冲突 | 高 — UI 卡死 | QThread 隔离 + 手动测试验证 | 🔲 待验证 |
| DeepSeek API 限流 | 中 — 用户体验下降 | 指数退避重试 + 友好提示 | 🔲 待设计 |
| Signal/Slot 线程安全 | 中 — 数据竞争 | 遵循 Qt 线程模型最佳实践 | 🔲 待验证 |
| httpx 依赖版本冲突 | 低 — 环境问题 | requirements.txt 固定版本 | 🔲 待确认 |

---

## 📅 预研时间表

| 日期 | 计划任务 | 实际完成 |
|------|---------|---------|
| 2026-02-20 | 创建预研框架 + DeepSeek API 调研 | - |
| 2026-02-21 | QThread + asyncio 集成测试 | - |
| 2026-02-22 | 线程安全性验证 + 总结决策 | - |

---

## ✅ 预研产出（预期）

完成预研后，应获得以下交付物：

1. **技术可行性结论** — 明确"能做"或"需要调整方案"
2. **接口设计草案** — `BaseChatProvider` 抽象接口
3. **错误处理策略** — 异常分类体系 + 重试逻辑
4. **性能基准** — 首 Token 延迟、生成速度等指标
5. **手动测试通过** — 所有 `manual_test_m12/*.py` 脚本可运行

---

**文档版本**: v0.1
**最后更新**: 2026-02-20
**下一步**: 开始 DeepSeek API 调研 → [02_DEEPSEEK_API_RESEARCH.md](./02_DEEPSEEK_API_RESEARCH.md)
