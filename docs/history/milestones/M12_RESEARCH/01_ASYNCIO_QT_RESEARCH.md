# asyncio + Qt 事件循环集成调研

> **调研日期**: 2026-02-20
> **调研人**: Claude Code + 用户（ChatGPT/NotebookLM 辅助）
> **状态**: ✅ 已完成（采用 qasync 方案）
> **重要更正**: 2026-02-20 下午 — 纠正了错误的库选择（qt-async-threads → qasync）

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
  - [x] `PySide6.QtAsyncio` 官方方案的限制（文档说 DNS/Socket 未完整实现）✅
  - [x] `qasync` 第三方库的可靠性 ✅ **最终选择**
  - [x] QThread + asyncio.run() 隔离方案的可行性 ✅
  - [x] ~~qt-async-threads~~ ❌ **错误选择（不提供 @asyncSlot）**

### 问题 2: qasync 方案细节（最终选择）
- **方案描述**: 使用 `@asyncSlot()` 装饰器，Qt + asyncio 事件循环融合
- **已验证**:
  - [x] @asyncSlot() 装饰器支持 async/await 语法
  - [x] Signal 自动在主线程发射（无需手动切换）
  - [x] 异常处理自动传递（通过 Signal）
  - [x] 事件循环融合（qasync.QEventLoop）
  - [ ] 高频 Signal 发射（100 tokens/s）稳定性 — **待测试**

### 问题 3: 错误纠正过程
- **原错误**: 误选 qt-async-threads（以为提供 `@async_slot`）
- **发现问题**: 用户运行测试时 `ImportError: cannot import name 'async_slot'`
- **调查结果**: qt-async-threads 提供 `QtAsyncRunner`（线程池模式），不符合需求
- **正确方案**: qasync 提供 `@asyncSlot()`，符合 M12 流式对话架构

---

## 🧪 验证实验

### 实验 1: qasync 集成测试（最终方案）✨

**测试脚本**: `tests/manual_test_m12/test_qasync_integration.py`

**测试场景**:
1. 基本流式输出（10 次，间隔 0.5s）
2. **高频 Signal 发射（100 tokens/s，持续 3s）** — 关键测试
3. 长时间运行（10s 心跳）
4. 异常处理（模拟网络错误）
5. OpenAI SDK 集成（真实 DeepSeek API 调用）

**预期结果**:
- @asyncSlot() 装饰器正常工作
- 所有 Signal 正确传递（无丢失）
- UI 刷新流畅（无卡顿）
- 异常正确捕获并通过 Signal 传递
- OpenAI SDK 流式调用成功（验证 M12 真实架构）

**实际结果**:
- [x] ✅ **测试通过**（2026-02-20 用户手动测试）

**测试结论**:
1. ✅ @asyncSlot() 装饰器工作正常
2. ✅ **高频 Signal（100 tokens/s）流畅无卡顿**（关键验证）
3. ✅ 300 个 token 全部接收，无丢失
4. ✅ 异常处理正确（成功捕获并通过 Signal 传递）
5. ✅ OpenAI SDK + DeepSeek API 集成成功
6. ✅ UI 刷新流畅，无阻塞

**运行方式**:
```bash
python tests/manual_test_m12/test_qasync_integration.py
```

### 实验 2: QThread 手动方案对比（已保留）

**测试脚本**: `tests/manual_test_m12/test_qthread_asyncio.py`

**用途**: 与 qasync 方案对比，验证代码简洁度差异

**实际结果**:
- [ ] 可选测试（用于对比）

### 实验 3: qt-async-threads 测试（已废弃）❌

**测试脚本**: ~~`tests/manual_test_m12/test_qt_async_threads.py`~~ (已废弃)

**废弃原因**: qt-async-threads 不提供 `@async_slot` 装饰器，架构不符合需求

---

## 📊 方案对比

| 方案 | 优点 | 缺点 | 适用性 |
|------|------|------|--------|
| **PySide6.QtAsyncio** | 官方支持，集成度高 | DNS/Socket 未完整实现，调用 httpx 会失败 | ❌ 不适用 |
| **qasync** ✨ | **事件循环融合、@asyncSlot 装饰器、代码优雅** | 引入额外依赖 | ✅ **最终选择** |
| **QThread + asyncio.run()** | 无额外依赖，隔离清晰 | 需要手动管理线程生命周期（50%+ 样板代码） | ⚠️ 可行但繁琐 |
| **qt-async-threads** ❌ | 轻量库（纯 Python） | **不提供 @asyncSlot，架构不符合需求** | ❌ **错误选择** |

### qasync 优势详解

**1. 语法优雅**:
```python
# qasync 方案（简洁）
from PySide6.QtCore import QObject, Signal
from qasync import asyncSlot

class ChatViewModel(QObject):
    token_received = Signal(str)

    @asyncSlot()
    async def send_message(self, user_message: str):
        async for token in stream:
            self.token_received.emit(token)  # 自动在主线程发射
```

**2. 对比手动 QThread 方案（繁琐）**:
```python
# QThread + asyncio.run() 方案（需要 50% 更多代码）
class AsyncWorkerThread(QThread):
    token_received = Signal(str)

    def __init__(self, message):
        super().__init__()
        self.message = message

    def run(self):
        asyncio.run(self._async_work())

    async def _async_work(self):
        async for token in stream:
            self.token_received.emit(token)

class ChatViewModel:
    def send_message(self, user_message: str):
        self.worker = AsyncWorkerThread(user_message)
        self.worker.token_received.connect(...)
        self.worker.finished.connect(self.worker.deleteLater)  # 手动清理
        self.worker.start()
```

**3. 成熟验证**:
- qasync 是 asyncio + Qt 的事实标准库
- 最新版本 0.28.0（2024 年发布）
- 活跃维护，广泛使用

**4. 主程序集成**:
```python
import sys
import asyncio
from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

app = QApplication(sys.argv)
loop = QEventLoop(app)
asyncio.set_event_loop(loop)

window = MainWindow()
window.show()

with loop:
    loop.run_forever()
```

---

## ✅ 调研结论

**可行性**: ✅ 已验证（基于互联网成熟方案）

**推荐方案**: **qasync**（技术决策 D003，已纠正）

### 关键发现

1. **qasync 优势**:
   - 代码简洁：比手动 QThread + asyncio.run() 减少 **50% 代码**
   - 语法自然：`@asyncSlot()` 装饰器支持 async/await
   - 事件循环融合：Qt + asyncio 深度集成
   - 成熟验证：qasync 是 asyncio + Qt 的事实标准库
   - 活跃维护：最新版本 0.28.0（2024 年）

2. **与 OpenAI SDK 完美集成**:
```python
from PySide6.QtCore import QObject, Signal
from qasync import asyncSlot, QEventLoop
from openai import AsyncOpenAI

class ChatViewModel(QObject):
    token_received = Signal(str)
    token_usage_updated = Signal(int, int, int)

    @asyncSlot()
    async def send_message(self, user_message: str):
        stream = await self.client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            max_tokens=2000
        )

        async for chunk in stream:
            # 自动在主线程发射（无需手动切换）
            if chunk.choices[0].delta.content:
                self.token_received.emit(chunk.choices[0].delta.content)

            # 实时 token 统计
            if hasattr(chunk, 'usage') and chunk.usage:
                self.token_usage_updated.emit(
                    chunk.usage.prompt_tokens,
                    chunk.usage.completion_tokens,
                    chunk.usage.total_tokens
                )
```

3. **PySide6.QtAsyncio 不可用原因**:
   - 官方文档明确说明：DNS/Socket 功能未完整实现
   - httpx.AsyncClient 调用会失败（M12 依赖 httpx）
   - 仅支持简单的文件 I/O，不适合网络应用

4. **错误纠正过程**:
   - **原错误**: 误选 qt-async-threads（以为提供 `@async_slot`）
   - **发现问题**: 用户运行测试时 `ImportError: cannot import name 'async_slot'`
   - **调查结果**: qt-async-threads 提供 `QtAsyncRunner`（线程池），不是装饰器模式
   - **正确方案**: qasync 提供 `@asyncSlot()`，符合 M12 流式对话架构

### 遗留风险

- ~~**MEDIUM**: 高频 Signal 发射（100 tokens/s）稳定性~~ → ✅ **已验证通过**（无卡顿、无丢失）
- ~~**MEDIUM**: 事件循环融合可能有潜在冲突~~ → ✅ **已验证通过**（运行正常）
- **LOW**: qasync 依赖（成熟库，风险可控）→ ✅ **可接受**

### 测试验证完成 ✅

**测试日期**: 2026-02-20 (Day 2 下午)
**测试人**: 用户手动测试
**测试结果**: 全部通过（5/5）

**关键验证**:
1. ✅ 高频 Signal（100 tokens/s，300 个 token）— 流畅无卡顿
2. ✅ OpenAI SDK + DeepSeek API — 流式调用成功
3. ✅ 异常处理 — 正确捕获并通过 Signal 传递
4. ✅ UI 刷新 — 无阻塞，用户体验良好

**结论**: qasync 方案完全可行，可以进入下一阶段（UI 设计 + 数据库设计）

### 下一步计划

**技术预研剩余工作**:
1. **P1**: UI 设计草图（chat_view.py 布局）
2. **P1**: 数据库表结构设计（chat_sessions）
3. **P2**: ChatViewModel 架构设计

**预计时间**: Day 3 上午完成全部预研

---

## 📚 参考资料

- [x] [Qt Thread Basics](https://doc.qt.io/qt-6/thread-basics.html) ✅
- [x] [PySide6.QtAsyncio 限制说明](https://doc.qt.io/qtforpython-6/PySide6/QtAsyncio/index.html) ✅
- [x] [qasync GitHub](https://github.com/CabbageDevelopment/qasync) ✅ **最终选择**
- [x] ~~[qt-async-threads GitHub]~~ ❌ 架构不符合需求
- [x] ChatGPT 对话存档 → `references/chatgpt_conversations.md` ✅
- [x] NotebookLM 笔记 → `references/notebooklm_notes.md` ✅

### 关键外部资源

- [qasync 文档](https://github.com/CabbageDevelopment/qasync) — @asyncSlot 使用说明
- [OpenAI SDK 文档](https://github.com/openai/openai-python) — stream_options 参数说明
- [DeepSeek API 文档](https://api-docs.deepseek.com/) — 100% OpenAI 兼容

---

**文档版本**: v3.0（已纠正错误）
**最后更新**: 2026-02-20 (Day 2 下午 - qasync 方案确定)
