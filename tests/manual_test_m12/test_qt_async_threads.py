"""
M12 手动测试：qt-async-threads 集成验证（技术决策 D003）

目标：
1. 验证 @async_slot 装饰器的功能
2. 测试高频 Signal 发射（100 tokens/s）稳定性
3. 验证异常处理和资源清理
4. 集成 OpenAI SDK 流式调用

技术方案：
- 使用 qt-async-threads 库（比手动 QThread 减少 50% 代码）
- @async_slot 自动管理后台线程
- Signal 自动在主线程发射

运行方式：
    python tests/manual_test_m12/test_qt_async_threads.py

环境要求：
    - PySide6 已安装（pip install PySide6）
    - qt-async-threads 已安装（pip install qt-async-threads>=0.6.0）
    - 可选：在 config/local.yaml 配置 LLM 服务（用于真实 API 测试）
"""

# ruff: noqa: E402

__test__ = False  # 手动 GUI/联网脚本，不参与默认 pytest 收集

import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 sys.path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.utils.config import get_config

# 检查依赖
try:
    from PySide6.QtCore import Signal, QObject
    from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QTextEdit, QPushButton, QLabel
except ImportError:
    print("❌ PySide6 未安装，请运行: pip install PySide6")
    sys.exit(1)

try:
    from qt_async_threads import async_slot
except ImportError:
    QT_ASYNC_THREADS_AVAILABLE = False

    def async_slot(function):
        """仅供模块安全导入；手动运行时会在 main() 中报告依赖缺失。"""
        return function
else:
    QT_ASYNC_THREADS_AVAILABLE = True


class ChatViewModel(QObject):
    """使用 qt-async-threads 的 ViewModel（M12 真实架构）"""

    # Signal 定义（自动跨线程）
    token_received = Signal(str)
    progress_updated = Signal(int)
    finished = Signal()
    error_occurred = Signal(str)

    def __init__(self):
        super().__init__()
        self.token_count = 0

    @async_slot
    async def test_basic_stream(self):
        """测试 1: 基本流式输出（10 次，间隔 0.5s）"""
        try:
            for i in range(10):
                await asyncio.sleep(0.5)
                self.token_received.emit(f"Token {i}\n")
                self.progress_updated.emit((i + 1) * 10)

            self.finished.emit()
        except Exception as e:
            self.error_occurred.emit(f"基本流式测试异常: {e}")

    @async_slot
    async def test_high_frequency(self):
        """测试 2: 高频 Signal 发射（100 tokens/s，持续 3s）"""
        try:
            total = 300
            for i in range(total):
                await asyncio.sleep(0.01)  # 每 10ms 发射一次（100 tokens/s）
                self.token_received.emit(f"{i} ")
                if (i + 1) % 50 == 0:
                    self.progress_updated.emit(int((i + 1) / total * 100))

            self.finished.emit()
        except Exception as e:
            self.error_occurred.emit(f"高频测试异常: {e}")

    @async_slot
    async def test_long_running(self):
        """测试 3: 长时间运行（10s，每秒更新）"""
        try:
            for i in range(10):
                await asyncio.sleep(1.0)
                self.token_received.emit(f"[{i+1}s] 心跳\n")
                self.progress_updated.emit((i + 1) * 10)

            self.finished.emit()
        except Exception as e:
            self.error_occurred.emit(f"长时间运行测试异常: {e}")

    @async_slot
    async def test_exception_handling(self):
        """测试 4: 异常处理（模拟网络错误）"""
        try:
            self.token_received.emit("开始模拟网络错误...\n")
            await asyncio.sleep(0.5)

            # 模拟错误
            raise RuntimeError("模拟的网络超时错误")

        except RuntimeError as e:
            # 正确捕获并发射错误 Signal
            self.error_occurred.emit(f"成功捕获异常: {e}")
            self.finished.emit()

    @async_slot
    async def test_openai_sdk_integration(self):
        """测试 5: OpenAI SDK 集成（真实 DeepSeek API 调用）"""
        config = get_config()
        api_key = config.llm_api_key
        if not api_key:
            self.error_occurred.emit(
                "config/local.yaml 未配置 LLM API Key，跳过此测试"
            )
            self.finished.emit()
            return

        try:
            import httpx
            from openai import AsyncOpenAI

            self.token_received.emit("连接 DeepSeek API...\n")

            # 创建 OpenAI 客户端（配置 DeepSeek base_url）
            http_client = httpx.AsyncClient(timeout=30.0)
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=config.llm_base_url,
                http_client=http_client,
            )

            messages = [{"role": "user", "content": "用一句话介绍你自己"}]

            self.token_received.emit("开始流式请求...\n")

            # 流式调用（与 M12 真实架构一致）
            stream = await client.chat.completions.create(
                model=config.llm_model,
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
                max_tokens=100,
            )

            chunk_count = 0
            async for chunk in stream:
                chunk_count += 1

                # 发射 token（M12 真实流程）
                if chunk.choices[0].delta.content:
                    token = chunk.choices[0].delta.content
                    self.token_received.emit(token)

                # 实时 token 统计（M12 真实流程）
                if hasattr(chunk, "usage") and chunk.usage:
                    usage_info = (
                        f"\n\n[Token 统计] "
                        f"输入={chunk.usage.prompt_tokens}, "
                        f"输出={chunk.usage.completion_tokens}, "
                        f"总计={chunk.usage.total_tokens}\n"
                    )
                    self.token_received.emit(usage_info)

                # 完成标志
                if chunk.choices[0].finish_reason:
                    self.token_received.emit(
                        f"\n[完成] finish_reason={chunk.choices[0].finish_reason}\n"
                    )

            self.token_received.emit(f"[统计] 总共接收 {chunk_count} 个 chunk\n")
            self.finished.emit()

        except ImportError:
            self.error_occurred.emit("openai 库未安装: pip install openai>=1.0.0")
            self.finished.emit()
        except Exception as e:
            self.error_occurred.emit(f"API 调用异常: {e}")
            self.finished.emit()


class TestWindow(QWidget):
    """测试主窗口"""

    def __init__(self):
        super().__init__()
        self.view_model = ChatViewModel()
        self.token_count = 0
        self.init_ui()
        self.connect_signals()

    def init_ui(self):
        """初始化 UI"""
        self.setWindowTitle("M12 qt-async-threads 测试（技术决策 D003）")
        self.setGeometry(100, 100, 700, 600)

        layout = QVBoxLayout()

        # 标题
        title_label = QLabel("🔬 qt-async-threads 稳定性测试")
        title_label.setStyleSheet("font-size: 16px; font-weight: bold; padding: 10px;")
        layout.addWidget(title_label)

        # 输出区域
        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        layout.addWidget(self.text_edit)

        # 统计标签
        self.stats_label = QLabel("等待测试...")
        self.stats_label.setStyleSheet("padding: 5px; background-color: #f0f0f0;")
        layout.addWidget(self.stats_label)

        # 测试按钮
        btn1 = QPushButton("测试 1: 基本流式输出（10 次，0.5s 间隔）")
        btn1.clicked.connect(self.start_test_basic)
        layout.addWidget(btn1)

        btn2 = QPushButton("测试 2: 高频 Signal（100 tokens/s，3s）✨ 关键测试")
        btn2.clicked.connect(self.start_test_high_freq)
        btn2.setStyleSheet("background-color: #ffe4b5; font-weight: bold;")
        layout.addWidget(btn2)

        btn3 = QPushButton("测试 3: 长时间运行（10s 心跳）")
        btn3.clicked.connect(self.start_test_long)
        layout.addWidget(btn3)

        btn4 = QPushButton("测试 4: 异常处理（模拟错误）")
        btn4.clicked.connect(self.start_test_exception)
        layout.addWidget(btn4)

        btn5 = QPushButton("测试 5: OpenAI SDK 集成（真实 DeepSeek API）")
        btn5.clicked.connect(self.start_test_openai)
        btn5.setStyleSheet("background-color: #e0ffe0;")
        layout.addWidget(btn5)

        btn_clear = QPushButton("清空输出")
        btn_clear.clicked.connect(self.clear_output)
        layout.addWidget(btn_clear)

        self.setLayout(layout)

    def connect_signals(self):
        """连接 ViewModel 的 Signal"""
        self.view_model.token_received.connect(self.on_token_received)
        self.view_model.progress_updated.connect(self.on_progress_updated)
        self.view_model.finished.connect(self.on_finished)
        self.view_model.error_occurred.connect(self.on_error)

    def start_test_basic(self):
        """启动基本流式测试"""
        self.prepare_test("基本流式输出")
        self.view_model.test_basic_stream()

    def start_test_high_freq(self):
        """启动高频 Signal 测试"""
        self.prepare_test("高频 Signal 发射（关键测试）")
        self.view_model.test_high_frequency()

    def start_test_long(self):
        """启动长时间运行测试"""
        self.prepare_test("长时间运行")
        self.view_model.test_long_running()

    def start_test_exception(self):
        """启动异常处理测试"""
        self.prepare_test("异常处理")
        self.view_model.test_exception_handling()

    def start_test_openai(self):
        """启动 OpenAI SDK 集成测试"""
        self.prepare_test("OpenAI SDK 集成（真实 API）")
        self.view_model.test_openai_sdk_integration()

    def prepare_test(self, test_name: str):
        """准备测试"""
        self.text_edit.clear()
        self.token_count = 0
        self.text_edit.append(f"🔬 开始测试: {test_name}\n")
        self.text_edit.append("=" * 60 + "\n")
        self.stats_label.setText(f"测试进行中: {test_name}")

    def clear_output(self):
        """清空输出"""
        self.text_edit.clear()
        self.token_count = 0
        self.stats_label.setText("等待测试...")

    def on_token_received(self, token: str):
        """接收 token（在主线程中执行）"""
        self.token_count += 1
        self.text_edit.insertPlainText(token)

    def on_progress_updated(self, percent: int):
        """进度更新"""
        self.stats_label.setText(f"进度: {percent}% | 已接收 {self.token_count} 个 token")

    def on_finished(self):
        """测试完成"""
        self.text_edit.append("\n" + "=" * 60)
        self.text_edit.append("✅ 测试完成！")
        self.text_edit.append(f"📊 总共接收 {self.token_count} 个 token")
        self.text_edit.append("=" * 60 + "\n")
        self.stats_label.setText(f"✅ 测试完成 | 总计 {self.token_count} tokens")

    def on_error(self, error_msg: str):
        """错误处理"""
        self.text_edit.append(f"\n⚠️ 错误: {error_msg}\n")
        self.stats_label.setText(f"⚠️ 错误: {error_msg}")


def main():
    """主函数"""
    if not QT_ASYNC_THREADS_AVAILABLE:
        print("[ERROR] qt-async-threads 未安装，请运行: pip install qt-async-threads>=0.6.0")
        return 1

    print("🔬 M12 qt-async-threads 集成测试（技术决策 D003）\n")
    print("=" * 60)
    print("技术方案: qt-async-threads 库（@async_slot 装饰器）")
    print("优势: 比手动 QThread + asyncio.run() 减少 50% 代码")
    print("=" * 60 + "\n")

    app = QApplication(sys.argv)
    window = TestWindow()
    window.show()

    print("📝 测试说明:")
    print("1. 点击任意测试按钮启动测试")
    print("2. 观察输出是否流畅、无卡顿")
    print("3. 验证所有 token 是否正确接收（无丢失）")
    print("4. 重点测试「测试 2: 高频 Signal」（100 tokens/s）")
    print("\n✅ 如果所有测试都通过，说明 qt-async-threads 方案可行！\n")

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
