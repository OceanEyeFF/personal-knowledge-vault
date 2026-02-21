"""知识引用工具模块单元测试 (M12)

测试 src.gui.utils.knowledge_ref 中的所有函数：
- estimate_tokens: Token 估算
- smart_truncate: 智能截断
- build_knowledge_reference: 构建引用对象
- format_context_message: 格式化上下文消息
- format_reference_card_html: 格式化 HTML 卡片
- parse_at_references: @ 语法解析
- strip_at_references: 移除 @ 引用
- detect_urls: URL 检测
"""

import pytest

from src.gui.utils.knowledge_ref import (
    AtReference,
    KnowledgeReference,
    build_knowledge_reference,
    detect_urls,
    estimate_tokens,
    format_context_message,
    format_reference_card_html,
    parse_at_references,
    smart_truncate,
    strip_at_references,
)


# ===================================================================
# estimate_tokens
# ===================================================================

class TestEstimateTokens:
    """Token 估算测试"""

    def test_empty_string(self) -> None:
        assert estimate_tokens("") == 0

    def test_whitespace_only(self) -> None:
        assert estimate_tokens("   ") == 0

    def test_chinese_text(self) -> None:
        """中文字符：1 字 ≈ 1.5 tokens"""
        text = "你好世界"  # 4 个中文字符
        tokens = estimate_tokens(text)
        assert tokens == 6  # 4 * 1.5 = 6

    def test_english_text(self) -> None:
        """英文：1 词 ≈ 1 token"""
        text = "hello world"  # 2 个英文单词
        tokens = estimate_tokens(text)
        assert tokens == 2

    def test_mixed_text(self) -> None:
        """中英混合"""
        text = "你好 world"  # 2 个中文 + 1 个英文
        tokens = estimate_tokens(text)
        assert tokens >= 3  # 至少 2*1.5 + 1 = 4

    def test_long_text(self) -> None:
        """长文本应返回较大的 token 数"""
        text = "测试" * 1000  # 2000 个中文字符
        tokens = estimate_tokens(text)
        assert tokens >= 2000  # 至少 2000 * 1.5 = 3000

    def test_single_char(self) -> None:
        """单个字符"""
        assert estimate_tokens("a") >= 1
        assert estimate_tokens("中") >= 1


# ===================================================================
# smart_truncate
# ===================================================================

class TestSmartTruncate:
    """智能截断测试"""

    def test_short_content_no_truncation(self) -> None:
        """短内容不截断"""
        text = "这是一段短文本"
        result, tokens, is_truncated = smart_truncate(text, max_tokens=1000)
        assert result == text
        assert not is_truncated

    def test_empty_content(self) -> None:
        """空内容"""
        result, tokens, is_truncated = smart_truncate("", max_tokens=100)
        assert result == ""
        assert tokens == 0
        assert not is_truncated

    def test_truncation_at_paragraph_boundary(self) -> None:
        """在段落边界截断"""
        paragraphs = ["段落一" * 100, "段落二" * 100, "段落三" * 100]
        text = "\n\n".join(paragraphs)

        result, tokens, is_truncated = smart_truncate(
            text, max_tokens=200
        )
        assert is_truncated
        assert "内容已截断" in result

    def test_truncation_with_summary(self) -> None:
        """截断时追加摘要"""
        text = "长文本内容" * 500
        summary = "这是原文摘要"

        result, tokens, is_truncated = smart_truncate(
            text, max_tokens=100, summary=summary
        )
        assert is_truncated
        assert "原文摘要" in result
        assert summary in result

    def test_truncation_without_summary(self) -> None:
        """截断时不追加摘要（无摘要）"""
        text = "长文本内容" * 500

        result, tokens, is_truncated = smart_truncate(
            text, max_tokens=100, summary=""
        )
        assert is_truncated
        assert "内容已截断" in result
        assert "原文摘要" not in result

    def test_returns_positive_token_count(self) -> None:
        """返回正数 token 计数"""
        text = "一些内容"
        _, tokens, _ = smart_truncate(text, max_tokens=1000)
        assert tokens > 0

    def test_first_paragraph_exceeds_limit_line_truncate(self) -> None:
        """第一个段落就超限，按行截取（保留部分行）"""
        # 构造一个很长的单段落（没有 \n\n 分隔），内部有 \n 换行符
        # 每行约 ~9 tokens（6 中文字 * 1.5 = 9），设置 max_tokens=25
        # 这样前 2 行（~18 tokens）能保留，第 3 行会超限
        lines = [f"短行第{i}号内容" for i in range(10)]  # 每行 ~6 中文字
        text = "\n".join(lines)  # 单段落（无 \n\n）

        result, tokens, is_truncated = smart_truncate(text, max_tokens=25)
        assert is_truncated
        assert "内容已截断" in result
        # 应该只保留了前几行，不是全部
        assert len(result) < len(text)

    def test_multi_paragraph_normal_truncation(self) -> None:
        """多段落正常截断（覆盖 152-153 行：段落未超限时追加）"""
        # 构造多个短段落，确保至少第一个段落能完整加入
        para1 = "第一段内容" * 5   # ~37 tokens
        para2 = "第二段内容" * 5   # ~37 tokens
        para3 = "第三段内容" * 5   # ~37 tokens
        text = "\n\n".join([para1, para2, para3])

        # max_tokens=80：第一段（~37）OK，第二段（37+37=74）OK，第三段（74+37=111）超限
        result, tokens, is_truncated = smart_truncate(text, max_tokens=80)
        assert is_truncated
        assert "第一段内容" in result
        assert "第二段内容" in result
        assert "内容已截断" in result


# ===================================================================
# build_knowledge_reference
# ===================================================================

class TestBuildKnowledgeReference:
    """构建知识引用测试"""

    def test_with_full_content(self) -> None:
        """带全文内容"""
        entry = {
            "knowledge_id": 42,
            "title": "测试标题",
            "source_type": "wechat",
            "source_url": "https://example.com",
            "summary_one_sentence": "一句话摘要",
        }
        ref = build_knowledge_reference(entry, content="完整内容")
        assert ref.knowledge_id == 42
        assert ref.title == "测试标题"
        assert ref.source_type == "wechat"
        assert ref.source_url == "https://example.com"
        assert ref.summary == "一句话摘要"
        assert ref.token_count > 0

    def test_without_content(self) -> None:
        """无全文内容（使用摘要）"""
        entry = {
            "knowledge_id": 1,
            "title": "标题",
            "summary_one_sentence": "摘要信息",
        }
        ref = build_knowledge_reference(entry, content="")
        assert ref.content_truncated == "摘要信息"
        assert not ref.is_truncated

    def test_without_any_content(self) -> None:
        """无全文也无摘要"""
        entry = {"knowledge_id": 1, "title": "标题"}
        ref = build_knowledge_reference(entry, content="")
        assert ref.content_truncated == "(无内容)"

    def test_long_content_truncated(self) -> None:
        """长内容自动截断"""
        entry = {
            "knowledge_id": 1,
            "title": "标题",
            "summary_one_sentence": "摘要",
        }
        long_content = "这是很长的内容。" * 1000
        ref = build_knowledge_reference(entry, content=long_content)
        assert ref.is_truncated
        assert ref.token_count > 0

    def test_missing_fields(self) -> None:
        """缺少字段时使用默认值"""
        entry = {}
        ref = build_knowledge_reference(entry)
        assert ref.knowledge_id == 0
        assert ref.title == ""
        assert ref.source_type == ""


# ===================================================================
# format_context_message
# ===================================================================

class TestFormatContextMessage:
    """格式化上下文消息测试"""

    def test_empty_refs(self) -> None:
        """空引用列表"""
        assert format_context_message([]) == ""

    def test_single_ref(self) -> None:
        """单个引用"""
        ref = KnowledgeReference(
            knowledge_id=1,
            title="测试文章",
            source_type="wechat",
            source_url="https://example.com",
            summary="摘要",
            content_truncated="内容",
            token_count=100,
        )
        result = format_context_message([ref])
        assert "测试文章" in result
        assert "wechat" in result
        assert "内容" in result
        assert "引用 1" in result

    def test_multiple_refs(self) -> None:
        """多个引用"""
        refs = [
            KnowledgeReference(
                knowledge_id=i,
                title=f"文章{i}",
                content_truncated=f"内容{i}",
            )
            for i in range(3)
        ]
        result = format_context_message(refs)
        assert "引用 1" in result
        assert "引用 2" in result
        assert "引用 3" in result


# ===================================================================
# format_reference_card_html
# ===================================================================

class TestFormatReferenceCardHtml:
    """格式化 HTML 引用卡片测试"""

    def test_basic_card(self) -> None:
        """基本引用卡片"""
        ref = KnowledgeReference(
            knowledge_id=42,
            title="AI 发展趋势",
            source_type="zhihu",
            summary="关于 AI 的讨论",
            token_count=500,
        )
        html = format_reference_card_html(ref)
        assert "AI 发展趋势" in html
        assert "zhihu" in html
        assert "500 tokens" in html
        assert "📎" in html

    def test_truncated_marker(self) -> None:
        """截断标记"""
        ref = KnowledgeReference(
            knowledge_id=1,
            title="标题",
            is_truncated=True,
            token_count=3000,
        )
        html = format_reference_card_html(ref)
        assert "已截断" in html

    def test_no_truncated_marker(self) -> None:
        """未截断时无标记"""
        ref = KnowledgeReference(
            knowledge_id=1,
            title="标题",
            is_truncated=False,
        )
        html = format_reference_card_html(ref)
        assert "已截断" not in html


# ===================================================================
# parse_at_references
# ===================================================================

class TestParseAtReferences:
    """@ 语法解析测试"""

    def test_no_references(self) -> None:
        """无引用"""
        refs = parse_at_references("普通消息文本")
        assert refs == []

    def test_knowledge_reference(self) -> None:
        """@知识库/ID 引用"""
        refs = parse_at_references("请看 @知识库/42 的内容")
        assert len(refs) == 1
        assert refs[0].ref_type == "knowledge"
        assert refs[0].value == "42"

    def test_search_reference(self) -> None:
        """@搜索/关键词 引用"""
        refs = parse_at_references("关于 @搜索/人工智能 的问题")
        assert len(refs) == 1
        assert refs[0].ref_type == "search"
        assert refs[0].value == "人工智能"

    def test_multiple_references(self) -> None:
        """多个引用"""
        text = "对比 @知识库/10 和 @知识库/20 的观点"
        refs = parse_at_references(text)
        assert len(refs) == 2
        assert refs[0].value == "10"
        assert refs[1].value == "20"

    def test_mixed_references(self) -> None:
        """混合引用"""
        text = "参考 @知识库/5 以及 @搜索/深度学习 相关内容"
        refs = parse_at_references(text)
        assert len(refs) == 2
        # 按位置排序
        assert refs[0].ref_type == "knowledge"
        assert refs[1].ref_type == "search"

    def test_reference_positions(self) -> None:
        """引用位置正确"""
        text = "看看 @知识库/1 吧"
        refs = parse_at_references(text)
        assert len(refs) == 1
        assert refs[0].start >= 0
        assert refs[0].end > refs[0].start
        assert refs[0].original_text == "@知识库/1"


# ===================================================================
# strip_at_references
# ===================================================================

class TestStripAtReferences:
    """移除 @ 引用测试"""

    def test_no_references(self) -> None:
        """无引用保持不变"""
        text = "普通消息"
        assert strip_at_references(text) == text

    def test_strip_knowledge_ref(self) -> None:
        """移除 @知识库 引用"""
        text = "讨论 @知识库/42 的内容"
        result = strip_at_references(text)
        assert "@知识库/42" not in result
        assert "讨论" in result

    def test_strip_search_ref(self) -> None:
        """移除 @搜索 引用"""
        text = "关于 @搜索/AI 的问题"
        result = strip_at_references(text)
        assert "@搜索/AI" not in result

    def test_strip_all_refs(self) -> None:
        """移除所有引用"""
        text = "@知识库/1 @搜索/测试 讨论内容"
        result = strip_at_references(text)
        assert "@知识库" not in result
        assert "@搜索" not in result
        assert "讨论内容" in result


# ===================================================================
# detect_urls
# ===================================================================

class TestDetectUrls:
    """URL 检测测试"""

    def test_no_urls(self) -> None:
        """无 URL"""
        assert detect_urls("普通文本") == []

    def test_http_url(self) -> None:
        """HTTP URL"""
        urls = detect_urls("看看 http://example.com 这个")
        assert len(urls) == 1
        assert urls[0] == "http://example.com"

    def test_https_url(self) -> None:
        """HTTPS URL"""
        urls = detect_urls("看看 https://example.com/path 这个")
        assert len(urls) == 1
        assert "https://example.com/path" in urls[0]

    def test_multiple_urls(self) -> None:
        """多个 URL"""
        text = "对比 https://a.com 和 https://b.com"
        urls = detect_urls(text)
        assert len(urls) == 2

    def test_complex_url(self) -> None:
        """复杂 URL（含参数）"""
        url = "https://example.com/path?key=value&foo=bar"
        urls = detect_urls(f"链接: {url}")
        assert len(urls) == 1
        assert "key=value" in urls[0]

    def test_wechat_url(self) -> None:
        """微信文章 URL"""
        url = "https://mp.weixin.qq.com/s/abcdef123456"
        urls = detect_urls(f"文章链接 {url}")
        assert len(urls) == 1


# ===================================================================
# KnowledgeReference 数据类
# ===================================================================

class TestKnowledgeReference:
    """KnowledgeReference 数据类测试"""

    def test_default_values(self) -> None:
        """默认值"""
        ref = KnowledgeReference(knowledge_id=1, title="标题")
        assert ref.source_type == ""
        assert ref.source_url == ""
        assert ref.summary == ""
        assert ref.content_truncated == ""
        assert ref.token_count == 0
        assert ref.is_truncated is False


# ===================================================================
# AtReference 数据类
# ===================================================================

class TestAtReference:
    """AtReference 数据类测试"""

    def test_fields(self) -> None:
        """字段完整性"""
        ref = AtReference(
            ref_type="knowledge",
            value="42",
            original_text="@知识库/42",
            start=5,
            end=12,
        )
        assert ref.ref_type == "knowledge"
        assert ref.value == "42"
        assert ref.original_text == "@知识库/42"
        assert ref.start == 5
        assert ref.end == 12
