"""
文本处理工具

提供 jieba 分词、文本清洗等功能
"""

import re
import jieba
from typing import List, Optional, Sequence
from pathlib import Path


class TextProcessor:
    """文本处理器"""

    def __init__(self, custom_dict_path: Optional[str] = None):
        """
        初始化文本处理器

        Args:
            custom_dict_path: jieba 自定义词典路径
        """
        # 产品默认资源必须通过 RuntimeLayout 解析；显式路径仅保留为测试/
        # 运维注入 seam，不能影响产品的 bundled-resource 根。
        if custom_dict_path is None:
            from src.utils.config import get_config

            layout = get_config().layout
            dict_path = layout.validate_bundled_path(
                layout.custom_dict_path,
                label="jieba 自定义词典",
            )
        else:
            dict_path = Path(custom_dict_path)
        if dict_path.exists():
            jieba.load_userdict(str(dict_path))

    @staticmethod
    def tokenize_chinese(text: str) -> str:
        """
        使用 jieba 对中文文本进行分词，返回空格分隔的字符串

        Args:
            text: 原始文本

        Returns:
            空格分隔的分词结果

        Example:
            >>> processor = TextProcessor()
            >>> processor.tokenize_chinese("人工智能的未来")
            "人工智能 的 未来"
        """
        if not text:
            return ""

        # 使用 jieba 分词
        words = jieba.cut(text)

        # 转换为空格分隔字符串
        return " ".join(words)

    @staticmethod
    def prepare_fts5_data(
        title: str,
        summary: str,
        keywords: str | Sequence[str] | None,
        tags: str | Sequence[str] | None,
    ) -> dict:
        """
        准备 FTS5 虚拟表的数据（预分词）

        Args:
            title: 标题
            summary: 摘要
            keywords: 关键词列表或逗号分隔字符串
            tags: 标签列表或逗号分隔字符串

        Returns:
            包含预分词后字段的字典
        """
        return {
            "title": TextProcessor.tokenize_chinese(title),
            "summary_100_words": TextProcessor.tokenize_chinese(summary),
            "keywords": TextProcessor.tokenize_chinese(TextProcessor._normalize_terms(keywords)),
            "tags": TextProcessor.tokenize_chinese(TextProcessor._normalize_terms(tags)),
        }

    @staticmethod
    def _normalize_terms(terms: str | Sequence[str] | None) -> str:
        """将标签/关键词统一成适合 FTS 分词的文本。"""
        if not terms:
            return ""
        if isinstance(terms, str):
            return re.sub(r"[,，;；|]+", " ", terms).strip()
        return " ".join(str(term).strip() for term in terms if str(term).strip())

    @staticmethod
    def sanitize_filename(title: str, max_length: int = 100) -> str:
        """
        清理文件名，移除非法字符

        Args:
            title: 原始标题
            max_length: 最大长度

        Returns:
            清理后的文件名

        Example:
            >>> TextProcessor.sanitize_filename("AI驱动的知识管理?")
            "AI驱动的知识管理？"
        """
        # 替换规则
        replacements = {
            "/": "-",
            "?": "？",
            ":": "：",
            "*": "×",
            '"': "'",
            "<": "",
            ">": "",
            "|": "",
        }

        for old, new in replacements.items():
            title = title.replace(old, new)

        # 移除非法字符
        title = re.sub(r'[<>|]', '', title)

        # 限制长度
        return title[:max_length]

    @staticmethod
    def calculate_word_count(text: str) -> int:
        """
        计算文本字数 (中英文混合)

        Args:
            text: 文本内容

        Returns:
            字数

        Example:
            >>> TextProcessor.calculate_word_count("Hello 世界")
            3
        """
        # 移除空白字符
        text = re.sub(r'\s+', '', text)

        # 计算中文字符数
        chinese_chars = re.findall(r'[\u4e00-\u9fff]', text)
        chinese_count = len(chinese_chars)

        # 计算英文单词数
        english_words = re.findall(r'[a-zA-Z]+', text)
        english_count = len(english_words)

        return chinese_count + english_count


def split_text_into_chunks(
    text: str,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> List[str]:
    """
    将文本分割成多个分块（用于向量化）

    Args:
        text: 原始文本
        chunk_size: 每个分块的字符数
        chunk_overlap: 分块之间的重叠字符数

    Returns:
        分块列表

    Example:
        >>> chunks = split_text_into_chunks("长文本...", chunk_size=500, chunk_overlap=50)
        >>> assert all(len(chunk) <= 550 for chunk in chunks)  # chunk_size + chunk_overlap
    """
    if not text or not text.strip():
        return []

    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")

    if chunk_overlap < 0:
        raise ValueError("chunk_overlap 不能为负数")

    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap 必须小于 chunk_size")

    chunks = []
    start = 0

    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        # 下一个分块的起始位置（带重叠）
        start = end - chunk_overlap

        # 如果剩余文本不足一个分块，直接添加
        if start + chunk_size >= len(text) and end < len(text):
            chunk = text[start:].strip()
            if chunk and chunk not in chunks:
                chunks.append(chunk)
            break

    return chunks


# 全局文本处理器实例
_text_processor_instance = None


def get_text_processor() -> TextProcessor:
    """
    获取全局文本处理器实例 (单例)

    Returns:
        TextProcessor 实例
    """
    global _text_processor_instance
    if _text_processor_instance is None:
        _text_processor_instance = TextProcessor()
    return _text_processor_instance
