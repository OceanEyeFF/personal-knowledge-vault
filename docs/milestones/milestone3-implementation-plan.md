# Milestone 3: 内容处理器实现方案

> **最小变更设计 - 遵循 Phase 1 既定模式**
>
> **设计日期**: 2026-02-14
> **设计原则**: KISS、DRY、SOLID

---

## 📋 目录

1. [文件清单](#文件清单)
2. [核心设计决策](#核心设计决策)
3. [构建顺序](#构建顺序)
4. [测试计划](#测试计划)
5. [风险和缓解措施](#风险和缓解措施)

---

## 文件清单

### 需要创建的文件（7 个核心文件 + 测试）

```
src/processors/
├── __init__.py              # 【修改】处理器注册和工厂函数
├── base.py                  # 【新建】处理器基类
├── wechat_processor.py      # 【新建】微信文章处理器
├── zhihu_processor.py       # 【新建】知乎内容处理器
├── generic_processor.py     # 【新建】通用网页处理器
└── chat_processor.py        # 【新建】聊天记录处理器

tests/unit/
├── test_processors_base.py         # 【新建】基类测试
├── test_processors_wechat.py       # 【新建】微信处理器测试
├── test_processors_zhihu.py        # 【新建】知乎处理器测试
├── test_processors_generic.py      # 【新建】通用处理器测试
└── test_processors_chat.py         # 【新建】聊天记录处理器测试

tests/fixtures/
├── wechat_sample.html       # 【新建】微信文章样本
├── zhihu_sample.html        # 【新建】知乎内容样本
├── generic_sample.html      # 【新建】通用网页样本
└── chat_sample.txt          # 【新建】聊天记录样本
```

**文件总数**: 15 个文件（5 个核心处理器 + 1 个基类 + 1 个注册文件 + 5 个测试 + 4 个样本）

---

## 核心设计决策

### 1. 复用现有抽象（不重复造轮子）

#### 复用 `Entry` 数据类
- **位置**: `src/storage/markdown_store.py:Entry`
- **用途**: 统一的数据模型，处理器直接返回 `Entry` 对象
- **优势**: 避免定义新的数据结构，保持一致性

#### 复用日志和配置系统
- **日志**: `src/utils/logger.py:get_logger()`
- **配置**: `src/utils/config.py:get_config()`
- **优势**: 保持统一的日志格式和配置管理

#### 复用 AI 服务
- **DeepSeek**: `src/ai/deepseek_client.py:DeepSeekClient`（摘要和标签）
- **OpenAI**: `src/ai/embedder.py:Embedder`（向量化，暂不使用）
- **优势**: 处理器专注于内容提取，AI 能力按需调用

### 2. 技术栈选择

| 功能 | 库 | 理由 |
|------|-----|------|
| **网页抓取** | `playwright` | 已在 requirements.txt，支持 JS 渲染，异步 |
| **HTML 解析** | `beautifulsoup4` | 已在 requirements.txt，简单易用 |
| **HTML → Markdown** | `html2text` | 已在 requirements.txt，开箱即用 |
| **文本清理** | `TextProcessor` | 复用 `src/utils/text_utils.py` |

**关键决策**: 不引入新依赖，全部使用已有的库。

### 3. 处理器架构设计

#### 基类设计（`base.py`）

```python
from abc import ABC, abstractmethod
from typing import Optional
from src.storage.markdown_store import Entry

class BaseProcessor(ABC):
    """内容处理器基类"""

    @classmethod
    @abstractmethod
    def can_handle(cls, url: str) -> bool:
        """判断是否可以处理该 URL"""
        pass

    @abstractmethod
    async def process(self, url: str) -> Entry:
        """处理 URL，返回 Entry 对象"""
        pass

    def _extract_metadata(self, soup) -> dict:
        """提取元数据（子类可重写）"""
        pass

    def _html_to_markdown(self, html: str) -> str:
        """HTML 转 Markdown（统一实现）"""
        pass
```

**设计要点**:
- 遵循 SOLID 的**开放-封闭原则**（OCP）：基类定义接口，子类扩展功能
- 提供默认实现（如 `_html_to_markdown`），子类按需重写
- 使用类方法 `can_handle()` 实现工厂模式路由

#### 处理器注册机制（`__init__.py`）

```python
from typing import Optional
from src.processors.base import BaseProcessor
from src.processors.wechat_processor import WechatProcessor
from src.processors.zhihu_processor import ZhihuProcessor
from src.processors.generic_processor import GenericProcessor

# 处理器注册表（优先级顺序）
_PROCESSORS = [
    WechatProcessor,
    ZhihuProcessor,
    # GenericProcessor 必须放最后（兜底）
]

def get_processor(url: str) -> BaseProcessor:
    """根据 URL 获取对应的处理器"""
    for processor_class in _PROCESSORS:
        if processor_class.can_handle(url):
            return processor_class()

    # 兜底：通用处理器
    return GenericProcessor()
```

**设计要点**:
- 简单的列表注册，不使用复杂的插件发现机制（KISS 原则）
- 按优先级顺序检查（专用处理器优先，通用处理器兜底）
- 遵循**工厂模式**，调用者无需关心具体实现

### 4. 各处理器设计要点

#### 微信文章处理器（`wechat_processor.py`）

**识别规则**:
```python
@classmethod
def can_handle(cls, url: str) -> bool:
    return "mp.weixin.qq.com" in url
```

**提取策略**:
- **标题**: `<meta property="og:title">` 或 `<h1 id="activity-name">`
- **作者**: `<meta name="author">` 或 `<span class="rich_media_meta_text">`
- **发布时间**: `<meta property="article:published_time">` 或 `<em id="publish_time">`
- **正文**: `<div id="js_content">` 或 class 包含 `rich_media_content`
- **图片处理**: 下载保存到 `.data/tmp/`，Markdown 中使用相对路径

**特殊处理**:
- 清理微信特有的样式标签（`<section>`、`<span>` 嵌套）
- 保留代码块格式
- 处理外链转换（`weixin.qq.com/s/xxx` → 真实链接）

#### 知乎内容处理器（`zhihu_processor.py`）

**识别规则**:
```python
@classmethod
def can_handle(cls, url: str) -> bool:
    return "zhihu.com" in url
```

**提取策略**:
- **问题/文章标题**: `<h1 class="QuestionHeader-title">` 或 `<h1 class="Post-Title">`
- **作者**: `<meta name="author">` 或 `.AuthorInfo-name`
- **发布时间**: `<meta property="article:published_time">`
- **正文**: `.RichContent-inner` 或 `.Post-RichTextContainer`
- **高赞回答**: 如果是问题页，提取票数最高的回答

**特殊处理**:
- 知乎的动态加载：使用 Playwright 等待内容渲染
- 公式渲染：保留 LaTeX 公式（`$$...$$`）
- 折叠内容：展开所有折叠区域

#### 通用网页处理器（`generic_processor.py`）

**识别规则**:
```python
@classmethod
def can_handle(cls, url: str) -> bool:
    return True  # 兜底处理器，处理所有 URL
```

**提取策略（优先级顺序）**:
1. **优先使用 `<article>` 标签**（HTML5 语义化）
2. **次选 `<main>` 标签**
3. **最后使用启发式算法**:
   - 移除 `<header>`、`<footer>`、`<nav>`、`<aside>`
   - 寻找最长的 `<div>` 或 `<section>`
   - 计算文本密度，选择密度最高的区域

**元数据提取（Open Graph 协议）**:
```python
title = soup.find("meta", property="og:title")["content"]
author = soup.find("meta", name="author")["content"]
published_time = soup.find("meta", property="article:published_time")["content"]
```

**降级策略**:
- 如果 Open Graph 不存在，使用 `<title>` 和 `<h1>`
- 如果时间不存在，使用当前时间

#### 聊天记录处理器（`chat_processor.py`）

**识别规则**:
```python
@classmethod
def can_handle(cls, url: str) -> bool:
    # 文件路径，非 URL
    return url.endswith(".txt") or url.endswith(".json")
```

**输入格式（支持两种）**:

**格式 1: 纯文本**
```
2026-01-01 10:00 张三
这是一条消息

2026-01-01 10:01 李四
这是回复
```

**格式 2: JSON**
```json
[
  {
    "timestamp": "2026-01-01 10:00",
    "sender": "张三",
    "message": "这是一条消息"
  }
]
```

**处理逻辑**:
1. 解析聊天记录
2. 生成对话摘要（调用 DeepSeek）
3. 提取关键话题（作为标签）
4. 转换为 Markdown 格式

**Markdown 输出格式**:
```markdown
# 聊天记录 - [主题]

## 对话摘要
[DeepSeek 生成的摘要]

## 对话内容

**张三** (2026-01-01 10:00):
> 这是一条消息

**李四** (2026-01-01 10:01):
> 这是回复
```

---

## 构建顺序

### Phase 1: 基础设施（Day 1）

**任务**:
1. 实现 `base.py` 基类
2. 实现 `__init__.py` 注册机制
3. 编写基类单元测试

**验收标准**:
```python
from src.processors import get_processor
from src.processors.base import BaseProcessor

# 测试注册机制
processor = get_processor("https://mp.weixin.qq.com/s/xxx")
assert isinstance(processor, BaseProcessor)
```

**时间估算**: 2-3 小时

---

### Phase 2: 通用网页处理器（Day 1-2）

**任务**:
1. 实现 `generic_processor.py`
2. 准备 `generic_sample.html` 测试样本
3. 编写单元测试

**验收标准**:
```python
processor = get_processor("https://example.com/article")
entry = await processor.process("https://example.com/article")

assert entry.title
assert entry.content
assert entry.source_url == "https://example.com/article"
assert entry.source_type == "generic"
```

**时间估算**: 3-4 小时

---

### Phase 3: 微信和知乎处理器（Day 2-3）

**任务**:
1. 实现 `wechat_processor.py`
2. 实现 `zhihu_processor.py`
3. 准备 HTML 样本
4. 编写单元测试

**验收标准**:
```python
# 微信文章
entry = await wechat_processor.process("https://mp.weixin.qq.com/s/xxx")
assert entry.source_type == "wechat"
assert entry.metadata.get("author")
assert entry.metadata.get("published_time")

# 知乎内容
entry = await zhihu_processor.process("https://zhuanlan.zhihu.com/p/123456")
assert entry.source_type == "zhihu"
assert entry.content
```

**时间估算**: 4-6 小时

---

### Phase 4: 聊天记录处理器（Day 3）

**任务**:
1. 实现 `chat_processor.py`
2. 准备 `chat_sample.txt` 和 JSON 样本
3. 编写单元测试

**验收标准**:
```python
entry = await chat_processor.process("chat_sample.txt")
assert entry.source_type == "chat"
assert entry.summary_100_words  # AI 生成的摘要
assert len(entry.tags) >= 3     # 提取的话题
```

**时间估算**: 2-3 小时

---

### Phase 5: 集成测试和优化（Day 3-4）

**任务**:
1. 端到端测试（抓取 → 解析 → 存储）
2. 错误处理优化
3. 性能测试
4. 日志完善

**验收标准**:
```python
# 集成测试
from src.processors import get_processor
from src.storage.markdown_store import MarkdownStore

processor = get_processor("https://mp.weixin.qq.com/s/xxx")
entry = await processor.process("https://mp.weixin.qq.com/s/xxx")

store = MarkdownStore(vault_dir=".data/vault")
file_path = store.save(entry, subdir="wechat")

# 验证文件存在且格式正确
assert file_path.exists()
loaded_entry = store.load(file_path)
assert loaded_entry.title == entry.title
```

**时间估算**: 3-4 小时

---

## 测试计划

### 单元测试覆盖目标

| 模块 | 覆盖率目标 | 关键测试点 |
|------|-----------|-----------|
| `base.py` | 90%+ | 抽象方法、默认实现、错误处理 |
| `wechat_processor.py` | 85%+ | URL 识别、元数据提取、正文解析 |
| `zhihu_processor.py` | 85%+ | URL 识别、动态加载、公式保留 |
| `generic_processor.py` | 80%+ | 启发式算法、降级策略 |
| `chat_processor.py` | 85%+ | 格式解析、AI 摘要、Markdown 生成 |

### 测试策略

#### 1. 使用 HTML 样本文件（离线测试）

**优势**:
- 不依赖网络，测试速度快
- 避免反爬虫干扰
- 可复现的测试环境

**实现**:
```python
# tests/unit/test_processors_wechat.py
import pytest
from pathlib import Path

@pytest.fixture
def wechat_html():
    fixture_path = Path(__file__).parent.parent / "fixtures" / "wechat_sample.html"
    with open(fixture_path, "r", encoding="utf-8") as f:
        return f.read()

@pytest.mark.asyncio
async def test_wechat_parse_html(wechat_html):
    processor = WechatProcessor()

    # Mock playwright 返回
    with patch.object(processor, '_fetch_html') as mock_fetch:
        mock_fetch.return_value = wechat_html

        entry = await processor.process("https://mp.weixin.qq.com/s/xxx")

        assert entry.title == "预期的标题"
        assert "关键内容" in entry.content
```

#### 2. Mock Playwright（避免浏览器依赖）

```python
@pytest.fixture
def mock_playwright():
    with patch('playwright.async_api.async_playwright') as mock:
        # 配置 mock 行为
        page = Mock()
        page.goto = AsyncMock()
        page.content = AsyncMock(return_value="<html>...</html>")

        browser = Mock()
        browser.new_page = AsyncMock(return_value=page)

        playwright = Mock()
        playwright.chromium.launch = AsyncMock(return_value=browser)

        mock.return_value.__aenter__.return_value = playwright
        yield mock
```

#### 3. AI 服务 Mock（避免 API 调用）

```python
@pytest.fixture
def mock_deepseek():
    with patch('src.processors.chat_processor.DeepSeekClient') as mock:
        client = Mock()
        client.summarize.return_value = "这是摘要"
        client.extract_tags.return_value = ["标签1", "标签2", "标签3"]
        mock.return_value = client
        yield mock
```

### 集成测试

#### 端到端测试（E2E）

```python
# tests/integration/test_processors_e2e.py
import pytest
from pathlib import Path
from src.processors import get_processor
from src.storage.markdown_store import MarkdownStore

@pytest.mark.asyncio
async def test_archive_wechat_article_e2e(tmp_path):
    """端到端测试：归档微信文章"""

    # 1. 获取处理器
    processor = get_processor("https://mp.weixin.qq.com/s/test")

    # 2. 处理 URL
    entry = await processor.process("https://mp.weixin.qq.com/s/test")

    # 3. 存储
    store = MarkdownStore(vault_dir=tmp_path)
    file_path = store.save(entry, subdir="wechat")

    # 4. 验证
    assert file_path.exists()

    # 5. 重新加载验证
    loaded_entry = store.load(file_path)
    assert loaded_entry.title == entry.title
    assert loaded_entry.content == entry.content
```

### 白盒测试检查点（按 STARTER_PROMPT 要求）

#### Milestone 3 检查点

1. **每个处理器的 `can_handle()` 逻辑正确**
   - 测试正确的 URL 返回 `True`
   - 测试不相关的 URL 返回 `False`
   - 测试边界情况（子域名、查询参数等）

2. **HTML 转 Markdown 保留关键格式**
   - 标题层级正确（`# ## ###`）
   - 列表格式正确（有序/无序）
   - 代码块保留语法高亮标记
   - 链接和图片格式正确

3. **元数据提取完整**
   - 作者、发布时间、来源 URL 全部提取
   - 缺失字段有默认值或合理降级

4. **异常处理**
   - 网络错误：重试 3 次，超时处理
   - 解析失败：降级到通用处理器
   - 反爬虫：使用 Playwright 的 stealth 模式

---

## 风险和缓解措施

### 风险 1: 反爬虫限制

**风险描述**:
- 微信、知乎等网站有反爬虫机制
- 可能导致抓取失败或被封禁

**缓解措施**:
1. **使用 Playwright 的 Stealth 模式**
   ```python
   browser = await playwright.chromium.launch(
       headless=True,
       args=['--disable-blink-features=AutomationControlled']
   )
   ```

2. **添加随机延迟**
   ```python
   import random
   await asyncio.sleep(random.uniform(1.0, 3.0))
   ```

3. **设置真实的 User-Agent**
   ```python
   await page.set_extra_http_headers({
       'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...'
   })
   ```

4. **降级策略**
   - 如果 Playwright 失败，尝试使用 `requests` + `beautifulsoup`
   - 如果仍然失败，返回错误提示，建议用户手动复制内容

**优先级**: 高

---

### 风险 2: HTML 结构变化

**风险描述**:
- 网站改版导致 CSS 选择器失效
- 元数据提取失败

**缓解措施**:
1. **多种选择器兜底**
   ```python
   title = (
       soup.find("meta", property="og:title") or
       soup.find("h1", id="activity-name") or
       soup.find("title")
   )
   ```

2. **日志记录选择器失败**
   ```python
   if not title:
       logger.warning(f"未找到标题，URL: {url}")
   ```

3. **降级到通用处理器**
   - 如果专用处理器提取失败，尝试通用处理器

4. **版本化配置（未来扩展）**
   - 将选择器提取到配置文件，易于更新

**优先级**: 中

---

### 风险 3: Playwright 性能开销

**风险描述**:
- Playwright 启动浏览器耗时（2-5 秒）
- 内存占用高（200-500 MB）

**缓解措施**:
1. **复用浏览器实例**
   ```python
   # 全局单例浏览器
   class BrowserPool:
       _browser = None

       @classmethod
       async def get_browser(cls):
           if cls._browser is None:
               cls._browser = await playwright.chromium.launch()
           return cls._browser
   ```

2. **批处理优化（未来扩展）**
   - 批量归档时复用同一浏览器

3. **可选的 headless-only 模式**
   - 配置文件中允许用户选择是否使用 Playwright

**优先级**: 中

---

### 风险 4: 依赖库兼容性

**风险描述**:
- `html2text` 的 Markdown 输出格式可能不符合预期
- `beautifulsoup4` 解析复杂 HTML 可能出错

**缓解措施**:
1. **使用固定版本**
   - `requirements.txt` 中已锁定版本

2. **输出格式后处理**
   ```python
   def _cleanup_markdown(self, md: str) -> str:
       # 移除多余空行
       md = re.sub(r'\n{3,}', '\n\n', md)
       # 修复列表格式
       md = re.sub(r'\n\*\s+\n', '\n* ', md)
       return md
   ```

3. **单元测试覆盖边界情况**
   - 测试嵌套列表、代码块、表格等复杂格式

**优先级**: 低

---

### 风险 5: AI 服务依赖

**风险描述**:
- 聊天记录处理器依赖 DeepSeek API
- API 调用失败或限流

**缓解措施**:
1. **优雅降级**
   ```python
   try:
       summary = deepseek_client.summarize(content)
   except Exception as e:
       logger.warning(f"AI 摘要生成失败: {e}")
       summary = content[:300] + "..."  # 简单截断
   ```

2. **重试机制（已在 DeepSeekClient 实现）**
   - 最多重试 3 次
   - 指数退避

3. **成本控制**
   - 限制单次请求的 token 数量
   - 日志记录 API 调用成本

**优先级**: 中

---

## 总结

### 关键设计亮点

1. **零新依赖**: 全部使用 `requirements.txt` 中已有的库
2. **复用现有抽象**: `Entry`、`MarkdownStore`、`DeepSeekClient`
3. **遵循既定模式**: 日志、配置、测试、类型注解、docstring
4. **简单胜于复杂**: 列表注册而非复杂插件系统
5. **灵活深度**: 处理器按需调用 AI 服务，不强制分层

### 交付标准

- ✅ 5 个处理器（基类 + 4 个实现）
- ✅ 注册机制（`__init__.py`）
- ✅ 单元测试覆盖率 ≥ 85%
- ✅ 4 个 HTML/文本样本
- ✅ 集成测试通过
- ✅ 白盒测试检查点全部验证

### 预计工作量

**总计**: 3-4 天（按 STARTER_PROMPT 估算）

| Phase | 任务 | 时间 |
|-------|------|------|
| Phase 1 | 基础设施 | 2-3 小时 |
| Phase 2 | 通用处理器 | 3-4 小时 |
| Phase 3 | 微信/知乎处理器 | 4-6 小时 |
| Phase 4 | 聊天记录处理器 | 2-3 小时 |
| Phase 5 | 集成测试和优化 | 3-4 小时 |
| **总计** | | **14-20 小时** |

---

**设计完成，准备开始实施！** 🚀
