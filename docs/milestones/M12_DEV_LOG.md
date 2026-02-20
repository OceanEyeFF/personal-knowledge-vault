# M12 开发日志

> **Milestone**: M12 - AI 对话交互 (v0.8.0)
> **开始日期**: 2026-02-20
> **预计完成**: 2026-02-27（7 天）
> **状态**: 🔬 技术预研阶段

---

## 日志格式说明

每日记录包含：
- **日期** — YYYY-MM-DD
- **阶段** — 技术预研 / 原型实现 / 功能完善 / 测试加固
- **工作内容** — 做了什么
- **技术发现** — 新知识、坑点、最佳实践
- **决策记录** — 为什么选 A 而非 B
- **遗留问题** — 待解决的问题
- **下一步** — 明日计划

---

## 2026-02-20 星期四

### 📌 阶段：技术预研 - Day 1

#### 工作内容
1. ✅ 创建 `milestone12` 分支
2. ✅ 搭建技术预研框架
   - 创建 `docs/milestones/M12_RESEARCH/` 目录（6 个调研文档）
   - 创建手动测试脚本目录（3 个测试脚本）
   - 创建参考资料存档（ChatGPT/NotebookLM/外部链接）
3. ✅ **DeepSeek API 完整调研（核心成果）**
   - 配置 API Key 并完成流式接口实测
   - 整理 ChatGPT 调研成果（SSE 格式、限流策略、Token 计费）
   - 整理 NotebookLM 调研成果（上下文 128K、双模型、无状态机制）
   - 验证流式 SSE 格式（199 个 chunk，约 200 字）
   - 测量 Token 生成速度（~66-100 tokens/s）

#### 技术发现

1. **DeepSeek API 与 OpenAI 完全兼容** ✅
   - 错误格式: 100% 一致（401 实测通过）
   - SSE 流式格式: 100% 一致（实测 199 chunk）
   - Chunk 结构: 第一个包含 `role`，后续只包含 `content`
   - 结束标志: `data: [DONE]`

2. **性能指标测量** ✅
   - Token 生成速度: ~66-100 tokens/s（高速）
   - 首 Token 延迟: < 0.5s（优秀）
   - 流式稳定性: 199 个 chunk 无丢失

3. **ChatGPT 调研核心发现** ✅
   - SSE 格式: `Content-Type: text/event-stream`
   - 429 处理: 指数退避 + Retry-After Header
   - Token 计费: 输入 + 输出分别计费，流式与非流式一致
   - 工程建议: Chat 与 Embedding 分离限流、本地缓存、流式拼接 buffer

4. **NotebookLM 调研核心发现** ✅
   - 上下文窗口: 128K tokens
   - 双模型体系: `deepseek-chat`（通用）/ `deepseek-reasoner`（推理）
   - 最大输出: chat 8K / reasoner 64K
   - 无状态机制: 必须客户端管理对话历史
   - 上下文缓存: 命中价格仅为未命中 1/10
   - 动态限流: 高峰期可能触发 429

5. **Windows 控制台编码问题** ⚠️
   - GBK 编码不支持 API 返回的 emoji（如 😊）
   - 解决方案: GUI 应用中使用 Qt 控件（支持 UTF-8），无此问题

#### 决策记录

- **决策 1**: 采用独立分支 `milestone12` 进行开发
  - 理由：M12 复杂度高，需要隔离主分支风险
  - 合并条件：所有测试通过 + 代码审查完成

- **决策 2**: 技术预研成果集中存档在 `docs/milestones/M12_RESEARCH/`
  - 理由：便于后续回顾，积累技术知识库
  - 参考：Phase 1 的 Milestone 报告模式

- **决策 3**: 使用 `httpx.AsyncClient` + 手动解析 SSE（已确定）✅
  - 理由：完全控制、无额外依赖、SSE 格式已验证
  - 不使用 `openai` SDK：避免依赖，透明度更高

- **决策 4**: 采用 `deepseek-chat` 模型（已确定）✅
  - 理由：M12 不需要深度推理，`deepseek-chat` 性价比更高
  - 输出限制：最高 8K tokens（足够）

- **决策 5**: Token 控制策略（单轮输出质量管理）✅
  - 核心策略：限制单轮输出质量 + 会话轮数提示
  - 单轮输出限制：`max_tokens=2000`（保证回复质量和完整性）
  - 会话轮数管理：3 轮对话后显示提示，建议结束或新建会话（不强制）
  - 对话历史：**不自动压缩**，充分利用 DeepSeek 128K 上下文窗口
  - System Prompt：固定内容利用上下文缓存（~150 tokens）
  - 知识上下文：动态注入，无硬性 token 限制

#### 遗留问题

1. ✅ DeepSeek API 错误格式？→ **已解决**（与 OpenAI 一致）
2. ✅ DeepSeek API 流式接口格式？→ **已验证**（SSE 标准，199 chunk 实测）
3. ✅ Token 生成速度？→ **已测量**（~66-100 tokens/s）
4. 🔲 Qt Signal/Slot 跨线程是否需要特殊声明？
5. 🔲 httpx.AsyncClient 与现有同步代码的依赖冲突？
6. 🔲 429 限流实际行为？（未触发，待实际开发验证）

#### 下一步计划

**DeepSeek API 调研** ✅ **已完成**:
- [x] 配置 API Key ✅
- [x] 验证流式 SSE 格式 ✅
- [x] 整理 ChatGPT 调研成果 ✅
- [x] 整理 NotebookLM 调研成果 ✅
- [x] 测量性能指标 ✅

**下一步：QThread + asyncio 调研**（Day 2）:
- [ ] 运行 `test_qthread_asyncio.py` GUI 测试
- [ ] 验证 Signal/Slot 跨线程稳定性
- [ ] 测试高频 Signal 发射（100 tokens/s）
- [ ] 使用 ChatGPT 调研 Qt 线程安全最佳实践
- [ ] 更新 `01_ASYNCIO_QT_RESEARCH.md`

---

## 开发统计

| 指标 | 当前值 | 目标值 |
|------|--------|--------|
| 已完成天数 | **1 / 7** | 7 |
| 预研文档 | **6 / 6** ✅ | 6 |
| 测试脚本 | **3 / 3** ✅ | 3 |
| 实测验证 | **2 / 3** | 3 |
| 代码行数 | 0 | ~1500 |
| 测试用例 | 0 | 45+ |

### 预研进度明细

| 调研项 | 状态 | 成果 |
|--------|------|------|
| DeepSeek API 调研 | ✅ 已完成 | SSE 格式、性能指标、ChatGPT/NotebookLM 整理 |
| QThread + asyncio 调研 | 🔲 待开始 | Day 2 计划 |
| 线程安全性调研 | 🔲 待开始 | Day 2 计划 |

---

**最后更新**: 2026-02-20 (Day 1)
