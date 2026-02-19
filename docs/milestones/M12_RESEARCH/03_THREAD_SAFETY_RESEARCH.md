# Qt 线程安全性调研

> **调研日期**: 2026-02-20
> **调研人**: Claude Code + 用户（ChatGPT/NotebookLM 辅助）
> **状态**: 🔲 待开始

---

## 🎯 调研目标

确保 AI 对话功能的多线程实现符合 Qt 线程安全规范，避免：
1. ❌ UI 对象在非主线程访问导致崩溃
2. ❌ 数据竞争（Race Condition）
3. ❌ 死锁（Deadlock）
4. ❌ Signal/Slot 跨线程数据丢失

---

## 📋 Qt 线程模型核心规则

### 规则 1: UI 对象只能在主线程访问
- **禁止**:
  ```python
  # ❌ 错误！在 QThread 中直接修改 UI
  class MyThread(QThread):
      def run(self):
          self.text_edit.setText("Hello")  # 崩溃！
  ```
- **正确**:
  ```python
  # ✅ 使用 Signal 传递数据到主线程
  class MyThread(QThread):
      text_updated = Signal(str)

      def run(self):
          self.text_updated.emit("Hello")  # 安全

  # 主线程连接 Signal
  thread.text_updated.connect(text_edit.setText)
  ```

### 规则 2: Signal/Slot 跨线程自动排队
- **机制**: Qt 会自动检测 Signal 发射线程与 Slot 执行线程
  - 同线程 → 直接调用（Qt.DirectConnection）
  - 跨线程 → 排队到目标线程事件循环（Qt.QueuedConnection）
- **数据安全**: Signal 参数会被**复制**（值传递），避免竞争

### 规则 3: 共享数据需要加锁
- **场景**: 多个线程读写同一个变量
- **工具**: `QMutex` / `QReadWriteLock`
- **示例**:
  ```python
  from PySide6.QtCore import QMutex

  class SharedData:
      def __init__(self):
          self.mutex = QMutex()
          self.value = 0

      def increment(self):
          self.mutex.lock()
          try:
              self.value += 1
          finally:
              self.mutex.unlock()
  ```

---

## 🧪 验证实验

### 实验 1: Signal/Slot 跨线程可靠性

**测试脚本**: `tests/manual_test_m12/test_thread_signal.py`

**测试场景**:
- QThread 中循环发射 1000 次 Signal，每次间隔 1ms
- 主线程 Slot 累加计数器
- 预期：计数器最终为 1000（无丢失）

**实际结果**:
- [ ] 待测试

### 实验 2: 高频 Signal 发射（AI 流式模拟）

**测试场景**:
- 模拟 100 tokens/s 的流式输出（每 10ms 发射一次）
- 持续 10 秒（共 1000 次）
- 主线程追加到 QTextEdit

**预期结果**:
- 所有 token 正确显示
- UI 刷新流畅（无卡顿）

**实际结果**:
- [ ] 待测试

### 实验 3: 线程生命周期管理

**测试场景**:
- 启动线程 → 等待 1s → 停止线程
- 重复 10 次
- 检查内存占用变化

**预期结果**:
- 无内存泄漏
- QThread 正常销毁

**实际结果**:
- [ ] 待测试

---

## 📊 Signal/Slot 连接类型对比

| 连接类型 | 使用场景 | 线程安全 | 数据传递 |
|---------|---------|---------|---------|
| **Qt.AutoConnection** (默认) | 自动选择 | ✅ | 跨线程时复制 |
| **Qt.DirectConnection** | 同线程直接调用 | ⚠️ 需手动保证 | 引用传递 |
| **Qt.QueuedConnection** | 强制排队 | ✅ | 复制 |
| **Qt.BlockingQueuedConnection** | 跨线程同步调用 | ⚠️ 易死锁 | 复制 |

**M12 推荐**: 使用默认 `Qt.AutoConnection`，Qt 会自动处理。

---

## ⚠️ 常见陷阱与避坑指南

### 陷阱 1: 在线程中创建 UI 对象
- **错误**:
  ```python
  class MyThread(QThread):
      def run(self):
          widget = QWidget()  # ❌ 崩溃！QWidget 只能在主线程创建
  ```
- **正解**: 所有 UI 对象在主线程创建，线程只负责数据处理

### 陷阱 2: 忘记启动事件循环
- **问题**: QThread 的 Slot 不响应 Signal
- **原因**: 未调用 `exec()` 启动线程事件循环
- **解决**:
  ```python
  class MyThread(QThread):
      def run(self):
          self.exec()  # 启动事件循环，允许接收 Signal
  ```

### 陷阱 3: 父对象在错误线程
- **问题**: QObject 的父子关系要求在同一线程
- **解决**: 工作线程对象不设置 parent（或 parent 是 QThread 本身）

---

## ✅ M12 线程安全设计

### 设计方案: QThread + Signal 单向数据流

```
[UI 主线程]                    [AIChatThread 工作线程]
    |                                   |
    | 1. 调用 thread.start()            |
    |---------------------------------->|
    |                                   | 2. 运行 asyncio.run()
    |                                   | 3. 流式接收 token
    |                                   |
    | 4. token_received Signal          |
    |<----------------------------------|
    | 5. 追加到 QTextEdit (主线程)      |
    |                                   |
    | 6. finished Signal                |
    |<----------------------------------|
    | 7. 更新状态 (主线程)              |
```

**关键点**:
- ✅ 数据流单向：工作线程 → 主线程
- ✅ 无共享可变状态（工作线程独立运行）
- ✅ Signal 参数简单（str/int），无需深拷贝担忧

---

## 📚 参考资料

- [ ] [Qt Thread Basics](https://doc.qt.io/qt-6/thread-basics.html)
- [ ] [Threads and QObjects](https://doc.qt.io/qt-6/threads-qobject.html)
- [ ] [Signals & Slots Across Threads](https://doc.qt.io/qt-6/threads-qobject.html#signals-and-slots-across-threads)
- [ ] ChatGPT 对话存档 → `references/chatgpt_conversations.md`

---

## ✅ 调研结论（待补充）

**线程安全性**: 🔲 待验证

**推荐模式**: QThread + Signal 单向流（已在设计中采用）

**关键发现**:
- 待补充

**遗留风险**:
- 待补充

---

**文档版本**: v0.1
**最后更新**: 2026-02-20
