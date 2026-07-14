# Processors 接口规范

> **版本**: 1.0
> **创建日期**: 2026-02-15
> **文件位置**: `src/processors/`
> **作用**: 定义内容处理器的统一接口和注册机制

---

## 📋 核心接口定义

### BaseProcessor (抽象基类)

**文件**: `src/processors/base.py`

**作用**: 定义所有处理器的统一接口

```python
class BaseProcessor(ABC):
    """Base class for content processors."""

    @classmethod
    @abstractmethod
    def can_handle(cls, url: str) -> bool:
        """Return True if this processor can handle the given URL."""

    @abstractmethod
    async def process(self, url: str) -> Entry:
        """Process the URL and return an Entry."""
```

---

## 🔍 核心方法规范

### 1. can_handle(url: str) -> bool

**作用**: 判断处理器是否能处理该 URL 或文本

**签名**:
```python
@classmethod
def can_handle(cls, url: str) -> bool:
    """Return True if this processor can handle the given URL."""
```

**输入**:
- `url: str` - 待处理的 URL、文件路径或文本内容

**输出**:
- `bool` - `True` 表示可以处理，`False` 表示不能处理

**约定**:
- ✅ 必须是类方法 (`@classmethod`)
- ✅ 不应抛出异常，失败时返回 `False`
- ✅ 应该快速返回（避免网络请求）
- ✅ 优先级由工厂函数的注册顺序决定

**实现示例**:

```python
# WechatProcessor - 基于域名匹配
@classmethod
def can_handle(cls, url: str) -> bool:
    return "mp.weixin.qq.com" in url

# ZhihuProcessor - 基于域名匹配
@classmethod
def can_handle(cls, url: str) -> bool:
    return "zhihu.com" in url or "zhuanlan.zhihu.com" in url

# AIChatProcessor - 基于内容特征匹配
@classmethod
def can_handle(cls, url_or_text: str) -> bool:
    if not url_or_text or not url_or_text.strip():
        return False

    # 拒绝处理 HTTP URL
    if url_or_text.strip().startswith(("http://", "https://")):
        return False

    # 检查文件或文本内容
    path = Path(url_or_text)
    if path.exists() and path.is_file():
        sample = path.read_text(encoding="utf-8", errors="ignore")
        return cls._looks_like_ai_chat(sample)

    return cls._looks_like_ai_chat(url_or_text)

# GenericProcessor - 兜底处理器（接受所有 URL）
@classmethod
def can_handle(cls, url: str) -> bool:
    return True
```

**注意事项**:
- ⚠️ `can_handle` 的参数名为 `url`，但实际可能接收文本内容（AIChatProcessor, TextFallbackProcessor）
- ⚠️ 不应在 `can_handle` 中执行网络请求或重量级操作

---

### 2. process(url: str) -> Entry

**作用**: 处理 URL 或文本内容并返回结构化的 Entry 对象

**签名**:
```python
async def process(self, url: str) -> Entry:
    """Process the URL and return an Entry."""
```

**输入**:
- `url: str` - 待处理的 URL、文件路径或文本内容

**输出**:
- `Entry` - 结构化的知识条目对象

**异常**:
- `ValueError` - URL 格式错误、内容为空等输入问题
- `httpx.HTTPError` - 网络请求失败
- `Exception` - 其他处理错误

**约定**:
- ✅ 必须是异步方法 (`async def`)
- ✅ 网络错误应考虑重试（由具体实现决定）
- ✅ 失败时抛出明确的异常（包含错误信息）
- ✅ 返回的 Entry 必须包含 `title`, `source_type`, `content` 字段
- ✅ `source_url` 字段应填充原始 URL（如果适用）

**典型实现流程**:

```python
async def process(self, url: str) -> Entry:
    logger.info("XxxProcessor processing url=%s", url)

    # 1. 获取原始内容（HTML、文本等）
    html = await self._fetch_html(url)
    if not html:
        raise ValueError(f"Empty HTML content for url={url}")

    # 2. 解析和提取元数据
    soup = BeautifulSoup(html, "lxml")
    metadata = self._extract_metadata(soup)
    metadata.setdefault("published_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    metadata["source_url"] = url
    metadata["source_type"] = "xxx"

    # 3. 提取正文内容
    content_tag = self._extract_content_tag(soup)
    markdown = self._html_to_markdown(str(content_tag))

    # 4. 构建 Entry 对象
    title = metadata.get("title") or "Untitled"
    abstract = metadata.get("description", "")

    entry = Entry(
        title=title,
        source_type="xxx",
        source_url=url,
        abstract=abstract,
        content=markdown,
    )

    logger.info("XxxProcessor completed url=%s title=%s", url, title)
    return entry
```

---

## 🏭 工厂函数：get_processor

**文件**: `src/processors/__init__.py`

**作用**: 根据 URL 自动选择合适的处理器

**签名**:
```python
def get_processor(url: str) -> BaseProcessor:
    """
    Get a processor instance for the given URL.

    Args:
        url: Target URL or path.

    Returns:
        A processor instance capable of handling the URL.
    """
```

**输入**:
- `url: str` - 待处理的 URL、文件路径或文本内容

**输出**:
- `BaseProcessor` - 处理器实例

**异常**:
- `ValueError` - URL 为空或无效

**处理逻辑**:

```python
def get_processor(url: str) -> BaseProcessor:
    # 1. 验证输入
    if not url or not url.strip():
        raise ValueError("URL cannot be empty")

    # 2. 懒加载处理器列表
    global _PROCESSORS
    if _PROCESSORS is None:
        _PROCESSORS = _load_processors()

    # 3. 按优先级顺序匹配
    for processor_class in _PROCESSORS:
        if processor_class.can_handle(url):
            logger.info("Selected processor: %s for url=%s", processor_class.__name__, url)
            return processor_class()

    # 4. 兜底：返回通用处理器
    logger.warning("No specific processor matched. Falling back to GenericProcessor for url=%s", url)
    return GenericProcessor()
```

**优先级顺序**:

```python
def _load_processors() -> List[Type[BaseProcessor]]:
    processors = [
        WechatProcessor,       # 1. 微信公众号
        ZhihuProcessor,        # 2. 知乎
        ChatProcessor,         # 3. 人机对话记录
        AIChatProcessor,       # 4. AI 聊天记录
        TextFallbackProcessor, # 5. 纯文本（可选）
        GenericProcessor,      # 6. 通用网页（兜底）
    ]
    return processors
```

**注意事项**:
- ⚠️ `GenericProcessor` 必须在最后（因为 `can_handle` 总是返回 `True`）
- ⚠️ 处理器顺序很重要：越具体的处理器应该越靠前
- ⚠️ `_PROCESSORS` 全局变量会被缓存，避免重复加载

---

## 🛠️ 辅助方法 (BaseProcessor 提供)

### 1. _extract_metadata(soup: BeautifulSoup) -> Dict[str, str]

**作用**: 从 HTML 提取通用元数据

**签名**:
```python
def _extract_metadata(self, soup: BeautifulSoup) -> Dict[str, str]:
    """Extract common metadata from HTML."""
```

**提取字段**:
- `title` - 从 `og:title` 或 `<title>` 标签提取
- `author` - 从 `<meta name="author">` 提取
- `published_time` - 从 `<meta property="article:published_time">` 提取
- `description` - 从 `<meta name="description">` 或 `og:description` 提取

**返回值**:
- 只包含非空字段的字典

**子类可以覆盖**以提供更丰富的元数据提取

---

### 2. _html_to_markdown(html: str) -> str

**作用**: 将 HTML 转换为 Markdown（统一配置）

**签名**:
```python
def _html_to_markdown(self, html: str) -> str:
    """Convert HTML to Markdown using a consistent configuration."""
```

**配置**:
```python
converter = html2text.HTML2Text()
converter.ignore_links = False      # 保留链接
converter.ignore_images = False     # 保留图片
converter.body_width = 0            # 不限制行宽
converter.unicode_snob = True       # Unicode 处理
converter.protect_links = True      # 保护链接格式
```

**后处理**:
- 统一换行符为 `\n`
- 移除 3 个以上连续换行（最多保留 2 个）
- 去除首尾空白

---

### 3. _get_meta_content(soup, **attrs) -> Optional[str]

**作用**: 从 `<meta>` 标签提取 `content` 属性

**签名**:
```python
def _get_meta_content(self, soup: BeautifulSoup, **attrs: str) -> Optional[str]:
    """Get content attribute from a meta tag."""
```

**用法**:
```python
# 按 name 查找
author = self._get_meta_content(soup, name="author")

# 按 property 查找
og_title = self._get_meta_content(soup, property="og:title")
```

---

### 4. _get_title_text(soup: BeautifulSoup) -> str

**作用**: 提取页面标题（优先 `<title>`，其次 `<h1>`）

**签名**:
```python
def _get_title_text(self, soup: BeautifulSoup) -> str:
    """Extract title from <title> or <h1> tag."""
```

**逻辑**:
1. 尝试从 `<title>` 标签提取
2. 如果失败，尝试从 `<h1>` 标签提取
3. 如果都失败，返回空字符串

---

## 📦 已实现的处理器

### 1. WechatProcessor

**文件**: `src/processors/wechat_processor.py`

**匹配规则**: `"mp.weixin.qq.com" in url`

**特性**:
- 使用 Playwright 渲染（带 requests 降级）
- 下载并本地化图片
- 提取微信特定元数据

**source_type**: `"wechat"`

---

### 2. ZhihuProcessor

**文件**: `src/processors/zhihu_processor.py`

**匹配规则**: `"zhihu.com" in url or "zhuanlan.zhihu.com" in url`

**特性**:
- 处理知乎文章和回答
- 提取作者、点赞数等元数据

**source_type**: `"zhihu"`

---

### 3. ChatProcessor

**文件**: `src/processors/chat_processor.py`

**匹配规则**: 检测对话格式（人机交互）

**特性**:
- 解析结构化对话
- 生成对话摘要

**source_type**: `"chat"`

---

### 4. AIChatProcessor

**文件**: `src/processors/ai_chat_processor.py`

**匹配规则**:
- 拒绝 HTTP URL
- 检测 ChatGPT/DeepSeek 导出格式

**特性**:
- 支持 HTML 和 Markdown 格式
- 解析对话角色（user/assistant）
- 调用 OpenAI-compatible LLM API 生成摘要

**source_type**: `"ai_chat"`

---

### 5. TextFallbackProcessor

**文件**: `src/processors/text_fallback_processor.py`

**匹配规则**: 检测纯文本输入

**特性**:
- 处理纯文本内容
- 生成简单的 Entry 对象

**source_type**: `"text"`

---

### 6. GenericProcessor (兜底)

**文件**: `src/processors/generic_processor.py`

**匹配规则**: `return True` (接受所有 URL)

**特性**:
- 通用网页抓取
- 自动提取主内容区域
- 适用于新闻、博客等通用网页

**source_type**: `"generic"`

---

## 📊 数据结构：ExtractedMetadata

**文件**: `src/processors/base.py`

**作用**: 结构化的元数据提取结果

```python
@dataclass
class ExtractedMetadata:
    """Structured metadata extracted from HTML."""

    title: str = ""
    author: str = ""
    published_time: str = ""
    description: str = ""

    def to_dict(self) -> Dict[str, str]:
        """Convert to dict, omitting empty values."""
```

**用途**:
- 统一元数据提取格式
- 自动过滤空值

---

## 🔗 与 Entry 的映射关系

### 必填字段映射

| Entry 字段 | 来源 | 说明 |
|-----------|------|------|
| `title` | `metadata["title"]` 或 `"Untitled"` | 标题 |
| `source_type` | 处理器硬编码 | `"wechat"`, `"zhihu"` 等 |
| `source_url` | 输入的 `url` 参数 | 原始 URL |
| `content` | HTML → Markdown 转换 | 正文内容 |

### 可选字段映射

| Entry 字段 | 来源 | 说明 |
|-----------|------|------|
| `abstract` | `metadata["description"]` | 摘要 |
| `archived_at` | Entry 自动生成 | 归档时间 |
| `tags` | 未填充（由 AI 步骤处理） | 标签 |
| `keywords` | 未填充（由 AI 步骤处理） | 关键词 |
| `summary_100_words` | 未填充（由 AI 步骤处理） | AI 摘要 |

**注意**:
- ⚠️ Processors 只负责内容提取，不负责 AI 分析
- ⚠️ AI 相关字段由 `AnalyzeStep` 工作流步骤填充

---

## ⚠️ 已知问题和改进建议

### 问题 1: `can_handle` 参数名误导

**问题描述**:
- 参数名为 `url: str`，但实际可能接收文本内容
- AIChatProcessor 和 TextFallbackProcessor 处理文本而非 URL

**示例**:
```python
# 实际调用
processor = get_processor("这是一段纯文本...")  # 不是 URL！
```

**影响范围**: 低 - 功能正常，但代码可读性差

**优先级**: 低

**建议修复**: 重命名参数为 `input: str` 或 `url_or_text: str`

---

### 问题 2: 异常处理不统一

**问题描述**:
- 不同处理器抛出不同类型的异常
- 缺少统一的异常类型

**示例**:
```python
# WechatProcessor
raise ValueError(f"Empty HTML content for url={url}")

# GenericProcessor
raise ValueError(f"Empty HTML content for url={url}")

# AIChatProcessor
# 可能抛出多种异常类型
```

**影响范围**: 中 - 上层调用难以捕获特定错误

**优先级**: 中

**建议修复**: 定义自定义异常类
```python
class ProcessorError(Exception):
    """Base exception for processor errors."""

class ContentFetchError(ProcessorError):
    """Failed to fetch content."""

class ContentParseError(ProcessorError):
    """Failed to parse content."""
```

---

### 问题 3: 网络重试策略不一致

**问题描述**:
- 各处理器的重试策略不同
- 没有统一的重试配置

**影响范围**: 中 - 不同处理器的可靠性不一致

**优先级**: 中

**建议修复**: 提供统一的重试装饰器
```python
@retry(max_attempts=3, backoff=2.0)
async def _fetch_html(self, url: str) -> str:
    ...
```

---

### 问题 4: `metadata` 字段未定义在 Entry 中

**问题描述**:
- 处理器设置 `entry.metadata = metadata`
- 但 Entry 类没有 `metadata` 字段
- 这是动态添加的属性

**示例**:
```python
# WechatProcessor
entry = Entry(...)
entry.metadata = metadata  # 动态添加！
```

**影响范围**: 低 - 功能正常，但违反类型约定

**优先级**: 低

**建议修复**: 在 Entry 中添加 `metadata` 字段
```python
@dataclass
class Entry:
    ...
    metadata: Dict[str, Any] = field(default_factory=dict)
```

---

### 问题 5: 图片下载逻辑分散

**问题描述**:
- WechatProcessor 实现了图片下载和本地化
- 其他处理器没有此功能
- 应该抽象为通用能力

**影响范围**: 低 - 功能缺失但不影响核心流程

**优先级**: 低

**建议修复**: 在 BaseProcessor 中提供 `_download_images` 方法

---

### 问题 6: `GenericProcessor` 优先级问题

**问题描述**:
- `GenericProcessor` 的 `can_handle` 总是返回 `True`
- 如果注册顺序错误，会导致其他处理器失效

**影响范围**: 高 - 破坏性配置错误

**优先级**: 高

**建议修复**: 添加运行时检查
```python
def _load_processors():
    processors = [...]

    # 验证 GenericProcessor 在最后
    if processors[-1] != GenericProcessor:
        raise ValueError("GenericProcessor must be the last processor")

    return processors
```

---

## 🎯 总结

### 设计优点

✅ 清晰的抽象接口（`can_handle` + `process`）
✅ 工厂模式自动选择处理器
✅ 优先级机制（注册顺序）
✅ 兜底处理器（GenericProcessor）
✅ 丰富的辅助方法（元数据提取、HTML 转 Markdown）

### 需要改进

⚠️ `can_handle` 参数名误导（接收文本而非 URL）
⚠️ 异常处理不统一
⚠️ 网络重试策略不一致
⚠️ `entry.metadata` 动态添加属性
⚠️ GenericProcessor 优先级配置风险
⚠️ 图片下载能力未抽象

---

## 📝 使用示例

### 基本用法

```python
from src.processors import get_processor

# 1. 获取处理器
url = "https://mp.weixin.qq.com/s/xxx"
processor = get_processor(url)  # 返回 WechatProcessor

# 2. 处理内容
entry = await processor.process(url)

# 3. 访问结果
print(entry.title)
print(entry.content)
```

### 扩展新处理器

```python
from src.processors.base import BaseProcessor
from src.storage.markdown_store import Entry

class MyCustomProcessor(BaseProcessor):
    @classmethod
    def can_handle(cls, url: str) -> bool:
        return "example.com" in url

    async def process(self, url: str) -> Entry:
        # 1. 获取内容
        html = await self._fetch_html(url)

        # 2. 解析
        soup = BeautifulSoup(html, "lxml")
        metadata = self._extract_metadata(soup)

        # 3. 转换
        content = self._html_to_markdown(str(soup.body))

        # 4. 构建 Entry
        return Entry(
            title=metadata.get("title", "Untitled"),
            source_type="custom",
            source_url=url,
            content=content
        )

# 注册到工厂函数
# 修改 src/processors/__init__.py 的 _load_processors()
```

---

**文档维护者**: AI Agent
**最后更新**: 2026-02-15
