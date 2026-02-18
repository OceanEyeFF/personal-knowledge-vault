# 未来开发参考文档

> Phase 2 审查中识别的低优先级优化方向，供后续里程碑按需取用。
>
> **创建日期**: 2026-02-18
> **来源**: Phase 2B GUI Prompt 综合审查
> **优先级**: P2（不阻塞 M10~M13 开发，视实际需求逐步引入）
>
> **已纳入 M10 计划的项目**: GUI 状态持久化（QSettings）和崩溃报告机制（sys.excepthook）
> 已在 PHASE2B_GUI_PROMPT.md 的 M10 验收检查点和维护方案中明确要求，不再列入本文档。

---

## 1. QSS 主题变量系统

**现状**: `light.qss` / `dark.qss` 两套静态文件，颜色值硬编码。

**问题**: 新增主题或调整配色时需逐行修改 QSS，容易遗漏。

**建议方案**:

```python
# src/gui/styles/theme_manager.py （路径与 PHASE2B 的 src/gui/styles/ 对齐）
THEME_VARS = {
    "light": {
        "@bg-primary": "#FFFFFF",
        "@bg-secondary": "#F5F5F5",
        "@text-primary": "#333333",
        "@accent": "#1890FF",
    },
    "dark": {
        "@bg-primary": "#1E1E1E",
        "@bg-secondary": "#2D2D2D",
        "@text-primary": "#E0E0E0",
        "@accent": "#58A6FF",
    }
}

def load_theme(theme_name: str) -> str:
    """加载 QSS 模板并替换变量占位符"""
    template = Path(f"src/gui/styles/{theme_name}.qss").read_text()
    for var, value in THEME_VARS[theme_name].items():
        template = template.replace(var, value)
    return template
```

**切换机制**: `QApplication.instance().setStyleSheet(load_theme(name))`，全局一次性刷新。

**引入时机**: M10 开发时如果发现 QSS 维护困难，或需要支持第三个主题时引入。

---

## 2. 国际化 (i18n) 预留

**现状**: UI 文本全中文硬编码。

**问题**: 如果未来有英文/多语言需求，需要大规模改造。

**建议方案**:

```python
# 方案 A: Qt 原生 — QTranslator + .ts 文件
self.tr("搜索知识库")  # 在所有 UI 字符串处使用 self.tr()

# 方案 B: 轻量字典 — 适合小项目
I18N = {
    "zh": {"search": "搜索知识库", "archive": "归档"},
    "en": {"search": "Search Knowledge", "archive": "Archive"},
}
```

**最小预留**: 当前阶段只需确保 UI 字符串不散布在逻辑代码中，集中在 View 层即可。MVVM 架构本身已经做到了这一点。

**引入时机**: 有明确多语言需求时。当前 YAGNI。

---

## 3. 多 AI 提供商扩展

**现状**: M12 已规划 `BaseChatProvider` 抽象接口 + `DeepSeekProvider` 具体实现（见 PHASE2B_GUI_PROMPT.md M12 交付物）。

**扩展方向**: M12 完成后，如需支持 OpenAI / 本地 Ollama / 其他提供商，可基于 `BaseChatProvider` 扩展。

**建议方案**:

```python
# src/gui/services/ai_provider.py
from abc import ABC, abstractmethod

class AIProvider(ABC):
    @abstractmethod
    async def chat_stream(self, messages: list, model: str) -> AsyncIterator[str]:
        """流式对话，yield 文本片段"""
        ...

class DeepSeekProvider(AIProvider):
    async def chat_stream(self, messages, model="deepseek-chat"):
        async with httpx.AsyncClient() as client:
            async with client.stream("POST", ...) as resp:
                async for line in resp.aiter_lines():
                    yield _parse_sse(line)

class OpenAIProvider(AIProvider):
    """复用 src/ai/openai_client.py 的配置"""
    ...

class OllamaProvider(AIProvider):
    """本地推理，base_url = http://localhost:11434"""
    ...
```

**配置扩展**:

```yaml
# config/config.yaml
ai:
  chat_provider: deepseek      # deepseek / openai / ollama
  providers:
    deepseek:
      base_url: https://api.deepseek.com/v1
      model: deepseek-chat
    openai:
      base_url: https://api.openai.com/v1
      model: gpt-4o-mini
    ollama:
      base_url: http://localhost:11434
      model: qwen2.5
```

**引入时机**: M12 已实现 `BaseChatProvider` + `DeepSeekProvider`。新增提供商只需继承 `BaseChatProvider` 并注册到配置中，无需修改现有代码。

---

## 4. 插件化视图注册

**现状**: 主窗口侧栏固定写死"浏览 / 搜索 / 归档 / AI 对话 / 设置"。

**问题**: 未来新增视图（知识图谱、时间轴、统计仪表板等）需要修改主窗口代码。

**建议方案**:

```python
# src/gui/views/registry.py
from dataclasses import dataclass
from typing import Type
from PySide6.QtWidgets import QWidget

@dataclass
class ViewEntry:
    name: str
    icon: str
    view_class: Type[QWidget]
    order: int = 100

VIEW_REGISTRY: list[ViewEntry] = []

def register_view(name: str, icon: str, order: int = 100):
    """装饰器：注册视图到侧栏"""
    def decorator(cls):
        VIEW_REGISTRY.append(ViewEntry(name, icon, cls, order))
        return cls
    return decorator

# 使用
@register_view("知识图谱", "mdi.graph-outline", order=50)
class KnowledgeGraphView(QWidget):
    ...
```

**主窗口改造**: `MainWindow.__init__()` 中遍历 `VIEW_REGISTRY` 动态创建侧栏按钮。

**引入时机**: 当视图数量超过 5 个时。当前 M10~M12 的 4-5 个视图直接硬编码更简单。

---

## 5. 快捷键可配置

**现状**: PHASE2B 规划了 `Ctrl+K` 全局搜索等快捷键，初期以硬编码方式实现。

**问题**: 如果用户习惯不同或与系统快捷键冲突，需要可配置化支持。

**建议方案**:

```python
# src/gui/shortcuts.py
DEFAULT_SHORTCUTS = {
    "global_search": "Ctrl+K",
    "new_archive": "Ctrl+N",
    "toggle_sidebar": "Ctrl+B",
    "quit": "Ctrl+Q",
}

class ShortcutManager:
    def __init__(self, settings: QSettings):
        self._shortcuts = {}
        for action, default_key in DEFAULT_SHORTCUTS.items():
            key = settings.value(f"shortcuts/{action}", default_key)
            self._shortcuts[action] = QKeySequence(key)

    def get(self, action: str) -> QKeySequence:
        return self._shortcuts[action]
```

**引入时机**: 有用户反馈快捷键冲突时。当前硬编码即可。

---

## 6. `chat_sessions.messages` 长对话优化

**现状**: M12 设计中 `messages TEXT NOT NULL` 存储整个对话 JSON 数组。

**问题**: 超长对话（100+ 轮）时：
- 单行 JSON 可达 100KB+，每次更新需重写整个字段
- 无法对单条消息做 SQL 查询或索引

**当前评估**: M12 初期完全够用（KISS 原则），绝大多数对话在 20 轮以内。

**未来优化方案（当对话平均超过 50 轮时考虑）**:

```sql
-- 拆分为独立消息表
CREATE TABLE chat_messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,          -- user / assistant / system
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    token_count INTEGER DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES chat_sessions(session_id)
);

CREATE INDEX idx_chat_messages_session ON chat_messages(session_id, created_at);
```

**迁移路径**: `004_split_chat_messages.sql`，从 JSON 列解析并插入新表。

**引入时机**: 性能数据表明 JSON 更新成为瓶颈时（预计 Phase 3）。

---

## 7. 性能基准与监控

**GUI 性能指标（未来可加入 CI）**:

| 指标 | 目标值 | 测量方式 |
|-----|--------|---------|
| 冷启动时间 | < 3s | 从 `app.exec()` 到主窗口 `showEvent` |
| 搜索响应 | < 500ms | 从按键到结果渲染完毕 |
| 内存占用（空闲） | < 200MB | 主窗口加载后无操作 |
| 内存占用（峰值） | < 500MB | 长对话 + 搜索并发 |

**监控方案**:

```python
# src/gui/utils/perf.py
import time, psutil

class PerfMonitor:
    @staticmethod
    def measure(label: str):
        """上下文管理器，打印耗时"""
        class _Timer:
            def __enter__(self):
                self.start = time.perf_counter()
                return self
            def __exit__(self, *args):
                elapsed = time.perf_counter() - self.start
                logger.debug(f"[PERF] {label}: {elapsed:.3f}s")
        return _Timer()
```

**引入时机**: M13 打包验收时加入基准测试。

---

## 引入优先级速查表

| 编号 | 优化项 | 建议引入时机 | 代码量 |
|-----|-------|------------|-------|
| 1 | QSS 主题变量 | 需要第 3 套主题时 | ~50 行 |
| 2 | i18n 预留 | 有多语言需求时 | ~100 行框架 |
| 3 | 多 AI 提供商扩展 | M12 后有切换需求时 | ~150 行/提供商 |
| 4 | 插件化视图 | 视图超过 5 个时 | ~60 行 |
| 5 | 快捷键可配置 | 有用户反馈快捷键冲突时 | ~40 行 |
| 6 | 长对话优化 | Phase 3 / 对话超 50 轮 | ~50 行 + 迁移 |
| 7 | 性能监控 | M13 打包验收 | ~30 行 |

---

**文档版本**: v1.1（复查修正：删除已纳入 M10 的冗余项，对齐路径与描述）
**维护者**: 幽浮喵 (猫娘工程师)
