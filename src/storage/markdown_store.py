"""
Markdown 存储层

负责 Markdown 文件的读写，以及 YAML Front Matter 的解析和生成
"""

import frontmatter
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime
from dataclasses import dataclass, field, asdict

from src.utils.logger import get_logger
from src.utils.text_utils import TextProcessor

logger = get_logger(__name__)


@dataclass
class Entry:
    """知识条目数据类"""

    # 基础元数据 (必填)
    title: str
    source_type: str  # wechat/zhihu/bilibili/pdf/personal
    source_url: Optional[str] = None
    archived_at: Optional[str] = None

    # 内容分析 (必填)
    tags: list = field(default_factory=list)
    keywords: list = field(default_factory=list)
    abstract: str = ""

    # 多层次摘要 (必填)
    summary_one_sentence: str = ""
    summary_100_words: str = ""

    # 检索配置 (必填)
    search_strategy: str = "keyword"  # keyword/hybrid/vector/structured
    word_count: int = 0

    # 关联信息 (可选)
    related_docs: list = field(default_factory=list)

    # 个人标注 (可选)
    reading_status: str = ""
    rating: int = 0
    notes: str = ""

    # 正文内容
    content: str = ""

    def __post_init__(self):
        """初始化后处理"""
        if self.archived_at is None:
            self.archived_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 自动计算字数
        if self.word_count == 0 and self.content:
            self.word_count = TextProcessor.calculate_word_count(self.content)

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典 (不包含 content)

        Returns:
            元数据字典
        """
        data = asdict(self)
        data.pop("content", None)
        return data


class MarkdownStore:
    """Markdown 文件存储管理器"""

    def __init__(self, vault_dir: Path):
        """
        初始化 Markdown 存储

        Args:
            vault_dir: Markdown Vault 目录
        """
        self.vault_dir = Path(vault_dir)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Markdown 存储初始化完成: {self.vault_dir}")

    def save(self, entry: Entry, subdir: Optional[str] = None) -> Path:
        """
        保存知识条目为 Markdown 文件

        Args:
            entry: 知识条目
            subdir: 子目录 (如 "wechat", "zhihu")

        Returns:
            保存的文件路径

        Example:
            >>> store = MarkdownStore(vault_dir=".data/vault")
            >>> entry = Entry(title="测试", content="# 内容")
            >>> path = store.save(entry, subdir="wechat")
        """
        # 确定子目录
        if subdir is None:
            subdir = entry.source_type

        target_dir = self.vault_dir / subdir
        target_dir.mkdir(parents=True, exist_ok=True)

        # 生成文件名
        safe_title = TextProcessor.sanitize_filename(entry.title)
        filename = f"{safe_title}.md"
        file_path = target_dir / filename

        # 如果文件已存在，添加时间戳后缀
        if file_path.exists():
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = f"{safe_title}-{timestamp}.md"
            file_path = target_dir / filename

        # 构建 Front Matter
        metadata = entry.to_dict()

        # 创建 frontmatter 对象
        post = frontmatter.Post(entry.content, **metadata)

        # 写入文件
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

        logger.info(f"保存 Markdown 文件: {file_path}")
        return file_path

    def load(self, file_path: Path) -> Entry:
        """
        加载 Markdown 文件

        Args:
            file_path: 文件路径

        Returns:
            Entry 对象

        Example:
            >>> store = MarkdownStore(vault_dir=".data/vault")
            >>> entry = store.load(Path(".data/vault/wechat/测试.md"))
        """
        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        # 解析 frontmatter
        with open(file_path, "r", encoding="utf-8") as f:
            post = frontmatter.load(f)

        # 提取元数据和内容
        metadata = post.metadata
        content = post.content

        # 构建 Entry 对象
        entry = Entry(
            title=metadata.get("title", ""),
            source_type=metadata.get("source_type", "personal"),
            source_url=metadata.get("source_url"),
            archived_at=metadata.get("archived_at"),
            tags=metadata.get("tags", []),
            keywords=metadata.get("keywords", []),
            abstract=metadata.get("abstract", ""),
            summary_one_sentence=metadata.get("summary_one_sentence", ""),
            summary_100_words=metadata.get("summary_100_words", ""),
            search_strategy=metadata.get("search_strategy", "keyword"),
            word_count=metadata.get("word_count", 0),
            related_docs=metadata.get("related_docs", []),
            reading_status=metadata.get("reading_status", ""),
            rating=metadata.get("rating", 0),
            notes=metadata.get("notes", ""),
            content=content,
        )

        logger.info(f"加载 Markdown 文件: {file_path}")
        return entry

    def list_all(self, subdir: Optional[str] = None) -> list[Path]:
        """
        列出所有 Markdown 文件

        Args:
            subdir: 子目录 (如 "wechat")

        Returns:
            文件路径列表
        """
        if subdir:
            search_dir = self.vault_dir / subdir
        else:
            search_dir = self.vault_dir

        if not search_dir.exists():
            return []

        # 递归查找所有 .md 文件
        md_files = list(search_dir.rglob("*.md"))
        logger.info(f"找到 {len(md_files)} 个 Markdown 文件")
        return md_files

    def delete(self, file_path: Path):
        """
        删除 Markdown 文件

        Args:
            file_path: 文件路径
        """
        if file_path.exists():
            file_path.unlink()
            logger.info(f"删除 Markdown 文件: {file_path}")
        else:
            logger.warning(f"文件不存在，无法删除: {file_path}")
