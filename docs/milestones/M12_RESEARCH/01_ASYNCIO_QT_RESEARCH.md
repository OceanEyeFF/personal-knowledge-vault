# asyncio + Qt 事件循环集成调研

> **调研日期**: 2026-02-20
> **调研人**: Claude Code + 用户（ChatGPT/NotebookLM 辅助）
> **状态**: 🔲 待开始

---

## 🎯 调研目标

验证在 PySide6 应用中集成 asyncio 的最佳方案，确保：
1. ✅ UI 主线程不阻塞（帧率 > 30fps）
2. ✅ asyncio 协程能正常运行（网络 I/O、流式处理）
3. ✅ 跨线程通信安全可靠（Qt Signal/Slot）

---

## 📋 调研问题清单

### 问题 1: Qt 事件循环与 asyncio 的冲突
- **现象**: Qt 有自己的 `QEventLoop`，asyncio 也有事件循环
- **问题**: 两者能否共存？如何协调？
- **调研方向**:
  - [ ] `PySide6.QtAsyncio` 官方方案的限制（文档说 DNS/Socket 未完整实现）
  - [ ] `qasync` 第三方库的可靠性
  - [ ] QThread + asyncio.run() 隔离方案的可行性

### 问题 2: QThread + asyncio.run() 方案细节
- **方案描述**: 在独立 QThread 中运行 `asyncio.run()`，通过 Signal 传递数据
- **需要验证**:
  - [ ] asyncio 事件循环在 QThread 中能否正常启动？
  - [ ] 多次 start/stop QThread 是否会泄漏资源？
  - [ ] Signal 发射频率过高（如 100 tokens/s）是否会丢失数据？

### 问题 3: 最佳实践模式
- **参考资料**:
  - [ ] Qt 官方文档：Thread-Safety in Qt
  - [ ] PySide6 官方示例
  - [ ] 开源项目案例（Anki、Calibre 等）

---

## 🧪 验证实验

### 实验 1: QThread 中运行 asyncio（最小示例）

**测试脚本**: `tests/manual_test_m12/test_qthread_asyncio.py`

**预期结果**:
- QThread 启动后，asyncio 事件循环正常运行
- 每 0.5s 通过 Signal 发射一个字符串
- 主窗口 QTextEdit 实时显示（无卡顿）

**实际结果**:
- [ ] 待测试

### 实验 2: 高频 Signal 发射压力测试

**测试场景**: 模拟 AI 流式输出（100 tokens/s）

**预期结果**:
- 所有 token 都能正确传递，无丢失
- UI 刷新流畅，无卡顿

**实际结果**:
- [ ] 待测试

---

## 📊 方案对比

| 方案 | 优点 | 缺点 | 适用性 |
|------|------|------|--------|
| **PySide6.QtAsyncio** | 官方支持，集成度高 | DNS/Socket 未完整实现，调用 httpx 会失败 | ❌ 不适用 |
| **qasync** | 成熟第三方库 | 引入额外依赖，违反 Phase 2 约束 | ⚠️ 备选 |
| **QThread + asyncio.run()** | 无额外依赖，隔离清晰 | 需要手动管理线程生命周期 | ✅ 推荐 |

---

## ✅ 调研结论（待补充）

**可行性**: 🔲 待验证

**推荐方案**: 待定

**关键发现**:
- 待补充

**遗留风险**:
- 待补充

---

## 📚 参考资料

- [ ] [Qt Thread Basics](https://doc.qt.io/qt-6/thread-basics.html)
- [ ] [PySide6.QtAsyncio 限制说明](https://doc.qt.io/qtforpython-6/PySide6/QtAsyncio/index.html)
- [ ] [qasync GitHub](https://github.com/CabbageDevelopment/qasync)
- [ ] ChatGPT 对话存档 → `references/chatgpt_conversations.md`

---

**文档版本**: v0.1
**最后更新**: 2026-02-20
