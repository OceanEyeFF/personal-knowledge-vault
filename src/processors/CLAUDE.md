# Processors 模块

[根目录](../../CLAUDE.md) > [src](..) > **processors**

---

## 模块职责

**内容处理器插件系统**：提供统一的内容抓取与解析接口，支持多种内容源的自动识别和处理。

### 核心理念

- **插件化架构**: 每个处理器独立实现，通过工厂模式注册
- **自动路由**: 根据 URL 特征自动选择合适的处理器
- **统一输出**: 所有处理器返回标准的 `Entry` 数据类
- **优雅降级**: 未匹配的 URL 自动回退到通用处理器
- **安全抓取**: 所有 URL 内容、重定向与页面子资源统一通过 DNS-pinned `SafeFetcher`，禁止网络库直连退路

---

## 入口与启动

### 工厂函数

```python
from src.processors import get_processor

# 自动选择处理器
processor = get_processor("https://mp.weixin.qq.com/xxx", config=runtime_config)
entry = await processor.process(url)
```

Kernel/Application 组合路径必须传入其已验证 config；省略 `config` 仅是旧调用方的
进程全局兼容路径。处理器不得在已显式注入 config 时再读取另一份全局配置。

### 注册机制

处理器按优先级顺序注册在 `__init__.py` 中:

```python
_PROCESSORS = [
    WechatProcessor,       # 微信公众号
    ZhihuProcessor,        # 知乎内容
    ChatProcessor,         # 聊天记录
    AIChatProcessor,       # AI 聊天导出
    TextFallbackProcessor, # 文本回退
    GenericProcessor,      # 通用网页（兜底）
]
```

---

## 对外接口

### BaseProcessor (基类)

所有处理器必须继承此基类:

```python
class BaseProcessor(ABC):
    @classmethod
    @abstractmethod
    def can_handle(cls, url: str) -> bool:
        """判断是否能处理该 URL"""
        pass

    @abstractmethod
    async def process(self, url: str) -> Entry:
        """处理 URL 并返回 Entry 数据类"""
        pass
```

### 辅助方法

- `_extract_metadata(soup: BeautifulSoup) -> Dict[str, str]`
  - 从 HTML 提取元数据 (title, author, published_time)
- `_html_to_markdown(html: str) -> str`
  - HTML 转 Markdown，统一格式化
- `_get_meta_content(soup, **attrs) -> Optional[str]`
  - 提取 meta 标签内容
- `_get_title_text(soup) -> str`
  - 提取标题文本（title 或 h1）

---

## 关键依赖与配置

### 依赖模块

- `src.storage.markdown_store.Entry`: 标准输出数据类
- `src.utils.logger`: 日志记录
- `beautifulsoup4`: HTML 解析
- `html2text`: HTML 转 Markdown
- `urllib3>=2.2,<3`: `Urllib3PinnedTransport` 的显式 direct dependency
- `safe_fetch.py`: URL 解析、全 DNS 答案公网校验、固定 IP 连接、redirect/子资源重校验与响应大小上限

URL processors 不使用 Playwright、requests 或 httpx 作为 runtime 抓取/降级路径。`SafeFetcher` 将安全决策与实际连接绑定：每跳重新解析并验证 DNS，连接验证过的固定 IP，同时保留原始 Host、TLS SNI 与证书 hostname；跨 origin redirect 会剥离 Cookie/Authorization 等敏感 header。

### 配置项

仓库中的 `config/config.yaml` 是默认配置模板。个人运行时唯一可编辑的
配置文件是 `%USERPROFILE%\\.pkv\\config.yaml`；包括
`processors.zhihu.cookie` 在内的 Cookie 和其他凭据只能写入该文件。
`<data-root>/config/local.yaml` 是 PKV 管理的无密钥运行快照，不得存放 Cookie
或其他凭据。

模板中的相关项:

```yaml
processors:
  wechat:
    timeout: 30
    user_agent: "Mozilla/5.0..."
  zhihu:
    timeout: 30
  generic:
    timeout: 30
    readability_min_length: 500
```

---

## 数据模型

### Entry 输出

所有处理器返回此数据类:

```python
@dataclass
class Entry:
    # 必填字段
    title: str
    source_type: str         # wechat/zhihu/chat/ai_chat/generic
    source_url: Optional[str]
    archived_at: Optional[str]

    # 内容分析
    tags: list
    keywords: list
    abstract: str
    summary_one_sentence: str
    summary_100_words: str

    # 检索配置
    search_strategy: str     # keyword/hybrid/vector
    word_count: int

    # 正文
    content: str
```

详细规范: [docs/specs/models/Entry数据模型规范.md](../../docs/specs/models/Entry数据模型规范.md)

---

## 已实现的处理器

### 1. WechatProcessor

**识别规则**: `mp.weixin.qq.com` 域名

**特性**:
- 提取微信公众号文章正文
- 保留图片和格式
- 提取作者和发布时间

**测试**: `tests/unit/test_processors_wechat.py`

---

### 2. ZhihuProcessor

**识别规则**: `zhihu.com` 域名

**特性**:
- 支持问题、回答、专栏
- 提取作者和点赞数
- 保留代码块和引用

**测试**: `tests/unit/test_processors_zhihu.py`

---

### 3. ChatProcessor

**识别规则**: 本地文件路径 + `.md` 后缀

**特性**:
- 解析聊天记录 Markdown 文件
- 提取对话结构
- 生成对话摘要

**测试**: `tests/unit/test_processors_chat.py`

---

### 4. AIChatProcessor

**识别规则**:
- 本地文件路径 + `.md` 后缀
- 包含 AI 聊天特征（### User / ### Assistant）

**特性**:
- 解析 ChatGPT/DeepSeek 导出的 Markdown
- 提取用户-AI 对话结构
- 智能标签提取（讨论主题）

**测试**: `tests/unit/test_processors_ai_chat.py`

**Fixtures**:
- `tests/fixtures/ai_chat/chatgpt_export.md`
- `tests/fixtures/ai_chat/deepseek_export.md`

---

### 5. TextFallbackProcessor

**识别规则**: 本地文件路径 + `.txt` 后缀

**特性**:
- 处理纯文本文件
- 自动生成标题（首行或文件名）
- 无需网络请求

**测试**: `tests/unit/test_processors_text_fallback.py`

---

### 6. GenericProcessor

**识别规则**: 所有未匹配的 URL（兜底处理器）

**特性**:
- 通用网页抓取
- 使用 Readability 算法提取正文
- 自动清理广告和导航栏

**测试**: `tests/unit/test_processors_generic.py`

---

## 测试与质量

### 单元测试

```powershell
# 运行所有处理器测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\processors-unit -Command @("pytest", "tests/unit", "-k", "processors", "-v")

# 运行特定处理器测试
.\scripts\run-test.ps1 -Direct -DataRoot .data-test\processors-ai-chat -Command @("pytest", "tests/unit/test_processors_ai_chat.py", "-v")
```

### 测试覆盖

- ✅ `test_processors_base.py`: 基类和辅助方法
- ✅ `test_processors_wechat.py`: 微信处理器
- ✅ `test_processors_zhihu.py`: 知乎处理器
- ✅ `test_processors_chat.py`: 聊天处理器
- ✅ `test_processors_ai_chat.py`: AI 聊天处理器
- ✅ `test_processors_text_fallback.py`: 文本回退处理器
- ✅ `test_processors_generic.py`: 通用处理器

### Mock 策略

- 向 `SafeFetcher` 注入 fake resolver / pinned transport，覆盖公网、私网、DNS rebinding、redirect、SNI/hostname 与响应上限
- 使用 `tests/fixtures/` 中的真实样本数据
- 避免依赖外部网络

---

## 常见问题 (FAQ)

### Q1: 如何添加新的处理器？

```python
# 1. 创建新文件 src/processors/my_processor.py
from src.processors.base import BaseProcessor
from src.storage.markdown_store import Entry

class MyProcessor(BaseProcessor):
    @classmethod
    def can_handle(cls, url: str) -> bool:
        return "my-site.com" in url

    async def process(self, url: str) -> Entry:
        # 实现抓取逻辑
        ...

# 2. 在 src/processors/__init__.py 中注册
from .my_processor import MyProcessor
_PROCESSORS.insert(0, MyProcessor)  # 插入优先级位置
```

### Q2: 处理器的优先级如何确定？

按 `_PROCESSORS` 列表顺序依次匹配，第一个匹配的处理器被使用。`GenericProcessor` 必须放在最后作为兜底。

### Q3: 如何处理需要登录的网站？

凭据只能来自 Git 忽略的本机配置，并通过同一个 `SafeFetcher` 发送；不得为了登录绕过安全抓取器或恢复 Playwright/requests/httpx 直连。跨 origin redirect 时敏感 header 会自动移除:

```python
from src.processors.safe_fetch import SafeFetcher

fetcher = SafeFetcher(timeout_seconds=30)
response = await fetcher.fetch(
    url,
    headers={"Cookie": self._get_cookie_from_config()},
)
```

### Q4: HTML 转 Markdown 的配置如何调整？

在 `BaseProcessor._html_to_markdown()` 中修改 `html2text.HTML2Text()` 配置:

```python
converter.ignore_links = False    # 保留链接
converter.ignore_images = False   # 保留图片
converter.body_width = 0          # 不限制行宽
converter.unicode_snob = True     # 使用 Unicode 而非 ASCII
```

---

## 相关文件清单

### 核心文件

| 文件 | 说明 |
|------|------|
| `__init__.py` | 处理器注册与工厂函数 |
| `base.py` | 基类与辅助方法 |
| `wechat_processor.py` | 微信公众号处理器 |
| `zhihu_processor.py` | 知乎内容处理器 |
| `chat_processor.py` | 聊天记录处理器 |
| `ai_chat_processor.py` | AI 聊天导出处理器 |
| `text_fallback_processor.py` | 文本回退处理器 |
| `generic_processor.py` | 通用网页处理器 |
| `safe_fetch.py` | DNS-pinned SSRF-safe transport 与 redirect 策略 |

### 测试文件

| 文件 | 说明 |
|------|------|
| `tests/unit/test_processors_*.py` | 单元测试（7 个文件） |
| `tests/unit/test_safe_fetch.py` | SafeFetcher DNS pinning / redirect / TLS 安全合同 |
| `tests/fixtures/ai_chat/` | AI 聊天样本数据 |
| `tests/fixtures/chat_sample.json` | 聊天记录样本 |
| `tests/fixtures/test_urls.json` | 测试 URL 列表 |

---

## 变更记录 (Changelog)

### 2026-02-16
- 生成模块级 CLAUDE.md 文档
- 添加导航面包屑
- 补充 AI 聊天处理器和文本回退处理器说明

### 2026-02-14 (M3.5)
- 新增 `AIChatProcessor` 支持 AI 聊天导出
- 新增 `TextFallbackProcessor` 支持纯文本文件

### 2026-02-10 (M3)
- 完成微信、知乎、聊天、通用处理器
- 完成所有单元测试

---

**模块维护者**: AI Agent
**最后更新**: 2026-02-16 01:53:22

*本文档由 Claude Code 自动生成*
