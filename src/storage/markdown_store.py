"""
Markdown 存储层

负责 Markdown 文件的读写，以及 YAML Front Matter 的解析和生成
"""

import frontmatter
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import date, datetime
from dataclasses import dataclass, field, asdict

from src.utils.logger import get_logger
from src.utils.text_utils import TextProcessor
from src.storage.vault_paths import (
    PublishedVaultFile,
    QuarantinedVaultFile,
    VaultPathGateway,
)

logger = get_logger(__name__)


def _normalize_time_field(value: Any) -> Optional[str]:
    """归一化时间字段，统一为单值字符串。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            normalized = _normalize_time_field(item)
            if normalized:
                return normalized
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")

    text = str(value).strip()
    return text or None


@dataclass
class Entry:
    """知识条目数据类"""

    # 基础元数据 (必填)
    title: str
    source_type: str  # wechat/zhihu/bilibili/pdf/personal
    source_url: Optional[str] = None
    event_time: Optional[str] = None
    published_at: Optional[str] = None
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
        self.event_time = _normalize_time_field(self.event_time)
        self.published_at = _normalize_time_field(self.published_at)
        self.archived_at = _normalize_time_field(self.archived_at)

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


@dataclass(frozen=True)
class PlannedVaultWrite:
    """Deterministic no-clobber Markdown target selected before any write."""

    absolute_path: Path
    relative_path: str


class MarkdownStore:
    """Markdown 文件存储管理器"""

    def __init__(self, vault_dir: Path, *, create: bool = True):
        """
        初始化 Markdown 存储

        Args:
            vault_dir: Markdown Vault 目录
            create: 仅受已取得 writer lease 的 bootstrap / mutation 路径可为
                ``True``。公开读取应传 ``False``，缺失 Vault 必须显式失败，
                不能把一次 read 伪装成 fresh runtime 初始化。
        """
        self.gateway = VaultPathGateway(vault_dir, create=create)
        self.vault_dir = self.gateway.vault_dir
        logger.info("Markdown 存储初始化完成")

    def plan_save(self, entry: Entry, subdir: Optional[str] = None) -> PlannedVaultWrite:
        """Plan the exact no-clobber Markdown target without writing any file.

        Deterministic for a given Vault state; keeps the human-friendly
        ``{title}.md`` name while uncontended.  The coordinator journals this
        plan before any archive file write.
        """
        if subdir is None:
            subdir = entry.source_type
        safe_title = TextProcessor.sanitize_filename(entry.title)
        file_path = self.gateway.unique_markdown_path(subdir, safe_title)
        return PlannedVaultWrite(file_path, self.gateway.relative_name(file_path))

    def save_planned(self, plan: PlannedVaultWrite, entry: Entry) -> Path:
        """Publish exactly at the planned target; never retry, never overwrite.

        If the planned target raced into existence, ``FileExistsError``
        propagates and the temporary file is cleaned: no overwrite and no
        orphan is left behind.
        """
        return self.save_planned_record(plan, entry).path

    def save_planned_record(
        self, plan: PlannedVaultWrite, entry: Entry
    ) -> PublishedVaultFile:
        """Publish a planned Markdown file and retain its exact identity."""

        metadata = entry.to_dict()
        post = frontmatter.Post(entry.content, **metadata)
        serialized = frontmatter.dumps(post)
        return self.gateway.write_text_atomic_record(plan.absolute_path, serialized)

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
        # 兼容入口：先规划精确目标，再严格写入该目标（并发同名不重试、不覆盖）。
        plan = self.plan_save(entry, subdir)
        return self.save_planned(plan, entry)

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
        safe_path = self.gateway.resolve(file_path, must_exist=True, require_file=True)

        # 解析 frontmatter
        post = frontmatter.loads(self.gateway.read_text(safe_path))

        # 提取元数据和内容
        metadata = post.metadata
        content = post.content

        # 构建 Entry 对象
        entry = Entry(
            title=metadata.get("title", ""),
            source_type=metadata.get("source_type", "personal"),
            source_url=metadata.get("source_url"),
            event_time=metadata.get("event_time"),
            published_at=metadata.get("published_at"),
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

        logger.info("加载 Markdown 文件完成")
        return entry

    def list_all(self, subdir: Optional[str] = None) -> list[Path]:
        """
        列出所有 Markdown 文件

        Args:
            subdir: 子目录 (如 "wechat")

        Returns:
            文件路径列表
        """
        md_files = list(self.gateway.iter_markdown(subdir))
        logger.info(f"找到 {len(md_files)} 个 Markdown 文件")
        return md_files

    def delete(self, file_path: Path):
        """
        删除 Markdown 文件

        Args:
            file_path: 文件路径
        """
        if self.gateway.delete(file_path):
            logger.info("删除 Markdown 文件完成")
        else:
            logger.warning("Markdown 文件不存在，无法删除")

    def relative_path(self, file_path: Path | str) -> str:
        """返回数据库唯一允许持久化的 Vault-relative POSIX 路径。"""
        return self.gateway.relative_name(file_path)

    def quarantine(
        self,
        file_path: Path | str,
        operation_id: Optional[str] = None,
        expected_identity: tuple[int, int] | None = None,
        expected_sha256: str | None = None,
    ) -> QuarantinedVaultFile:
        """为跨存储删除准备可恢复的 Markdown 隔离（支持确定性 operation 派生路径）。"""
        return self.gateway.quarantine(
            file_path,
            operation_id=operation_id,
            expected_identity=expected_identity,
            expected_sha256=expected_sha256,
        )

    def plan_quarantine(self, file_path: Path | str, operation_id: str) -> Path:
        """返回由 operation_id 派生的确定性隔离目标（不移动文件）。"""
        return self.gateway.plan_quarantine_path(file_path, operation_id=operation_id)

    def restore(self, item: QuarantinedVaultFile) -> Path:
        """回滚尚未提交的跨存储删除。"""
        return self.gateway.restore(item)

    def finalize_quarantine(self, item: QuarantinedVaultFile) -> None:
        """在 SQLite 删除提交后清理隔离文件。"""
        self.gateway.finalize_quarantine(item)
