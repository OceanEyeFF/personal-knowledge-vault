"""
M12 手动测试：QThread + asyncio 集成验证

目标：
1. 验证 QThread 中运行 asyncio.run() 的可行性
2. 测试 Signal/Slot 跨线程通信稳定性
3. 验证高频 Signal 发射不丢失数据

运行方式：
    python tests/manual_test_m12/test_qthread_asyncio.py

环境要求：
    - PySide6 已安装（pip install PySide6）
"""

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

try:
    from PySide6.QtCore import QThread, Signal, QObject
    from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QTextEdit, QPushButton
except ImportError:
    print("❌ PySide6 未安装，请运行: pip install PySide6")
    sys.exit(1)


class AsyncWorkerThread(QThread):
    """在独立线程中运行 asyncio 事件循环"""

    # Signal 定义（线程安全，自动排队到主线程）
    token_received = Signal(str)
    progress_updated = Signal(int)
    finished_signal = Signal()
    error_occurred = Signal(str)

    def __init__(self, test_mode: str, parent=None):
        super().__init__(parent)
        self.test_mode = test_mode

    def run(self):
        """QThread 的入口函数（在工作线程中执行）"""
        try:
            # 在工作线程中启动独立的 asyncio 事件循环
            asyncio.run(self._async_work())
        except Exception as e:
            self.error_occurred.emit(f"线程异常: {e}")

    async def _async_work(self):
        """异步工作函数"""
        if self.test_mode == "basic":
            await self._test_basic_stream()
        elif self.test_mode == "high_freq":
            await self._test_high_frequency()
        elif self.test_mode == "long":
            await self._test_long_running()

    async def _test_basic_stream(self):
        """测试 1: 基本流式输出（10 次，间隔 0.5s）"""
        for i in range(10):
            await asyncio.sleep(0.5)
            self.token_received.emit(f"Token {i}\n")
            self.progress_updated.emit((i + 1) * 10)

        self.finished_signal.emit()

    async def _test_high_frequency(self):
        """测试 2: 高频 Signal 发射（100 tokens/s，持续 3s）"""
        total = 300
        for i in range(total):
            await asyncio.sleep(0.01)  # 每 10ms 发射一次（100 tokens/s）
            self.token_received.emit(f"{i} ")
            if (i + 1) % 50 == 0:
                self.progress_updated.emit(int((i + 1) / total * 100))

        self.finished_signal.emit()

    async def _test_long_running(self):
        """测试 3: 长时间运行（10s，每秒更新）"""
        for i in range(10):
            await asyncio.sleep(1.0)
            self.token_received.emit(f"[{i+1}s] 心跳\n")
            self.progress_updated.emit((i + 1) * 10)

        self.finished_signal.emit()


class TestWindow(QWidget):
    """测试主窗口"""

    def __init__(self):
        super().__init__()
        self.worker_thread = None
        self.token_count = 0
        self.init_ui()

    def init_ui(self):
        """初始化 UI"""
        self.setWindowTitle("M12 QThread + asyncio 测试")
        self.setGeometry(100, 100, 600, 500)

        layout = QVBoxLayout()

        # 输出区域
        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        layout.addWidget(self.text_edit)

        # 测试按钮
        btn1 = QPushButton("测试 1: 基本流式输出（10 次，0.5s 间隔）")
        btn1.clicked.connect(lambda: self.start_test("basic"))
        layout.addWidget(btn1)

        btn2 = QPushButton("测试 2: 高频 Signal（100 tokens/s，3s）")
        btn2.clicked.connect(lambda: self.start_test("high_freq"))
        layout.addWidget(btn2)

        btn3 = QPushButton("测试 3: 长时间运行（10s 心跳）")
        btn3.clicked.connect(lambda: self.start_test("long"))
        layout.addWidget(btn3)

        btn_clear = QPushButton("清空输出")
        btn_clear.clicked.connect(self.text_edit.clear)
        layout.addWidget(btn_clear)

        self.setLayout(layout)

    def start_test(self, mode: str):
        """启动测试"""
        if self.worker_thread and self.worker_thread.isRunning():
            self.text_edit.append("⚠️ 测试正在运行中，请等待完成\n")
            return

        self.text_edit.clear()
        self.token_count = 0

        mode_names = {
            "basic": "基本流式输出",
            "high_freq": "高频 Signal 发射",
            "long": "长时间运行",
        }
        self.text_edit.append(f"🔬 开始测试: {mode_names.get(mode, mode)}\n")
        self.text_edit.append("=" * 50 + "\n")

        # 创建并启动工作线程
        self.worker_thread = AsyncWorkerThread(mode)
        self.worker_thread.token_received.connect(self.on_token_received)
        self.worker_thread.progress_updated.connect(self.on_progress_updated)
        self.worker_thread.finished_signal.connect(self.on_finished)
        self.worker_thread.error_occurred.connect(self.on_error)
        self.worker_thread.start()

    def on_token_received(self, token: str):
        """接收 token（在主线程中执行）"""
        self.token_count += 1
        self.text_edit.insertPlainText(token)

    def on_progress_updated(self, percent: int):
        """进度更新"""
        # 可以在这里更新进度条（如果有的话）
        pass

    def on_finished(self):
        """测试完成"""
        self.text_edit.append("\n" + "=" * 50)
        self.text_edit.append(f"✅ 测试完成！")
        self.text_edit.append(f"📊 总共接收 {self.token_count} 个 token")
        self.text_edit.append("=" * 50 + "\n")

    def on_error(self, error_msg: str):
        """错误处理"""
        self.text_edit.append(f"\n❌ 错误: {error_msg}\n")


def main():
    """主函数"""
    print("🔬 M12 QThread + asyncio 集成测试\n")
    print("启动 GUI 测试窗口...")

    app = QApplication(sys.argv)
    window = TestWindow()
    window.show()

    print("\n📝 测试说明:")
    print("1. 点击任意测试按钮启动测试")
    print("2. 观察输出是否流畅、无卡顿")
    print("3. 验证所有 token 是否正确接收（无丢失）")
    print("\n✅ 如果所有测试都通过，说明 QThread + asyncio 方案可行！\n")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
