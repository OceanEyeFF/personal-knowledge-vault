"""
文本处理工具

提供 jieba 分词、文本清洗等功能
"""

import logging
import marshal
import re
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence

import jieba

from src.runtime.errors import ErrorCode, PKVRuntimeError


_MISSING = object()

# Jieba's default logger writes cache filesystem paths directly to process
# stderr while loading its dictionary.  Product CLI/MCP diagnostics must not
# disclose the user's runtime layout; warnings and errors remain available.
jieba.setLogLevel(logging.WARNING)


@dataclass
class _JiebaRuntimeState:
    """A restorable snapshot of the mutable jieba process-global tokenizer.

    ``jieba`` exposes module-level functions bound to one ``dt`` tokenizer.
    A temporary Config root therefore cannot get an independent tokenizer by
    merely changing ``dt.tmp_dir``: initialization and ``load_userdict`` also
    mutate its dictionary.  Short-lived verification/test workspaces use this
    snapshot to leave the surrounding process exactly as they found it.
    """

    tokenizer: Any
    scalar_values: dict[str, Any]
    mapping_values: dict[str, tuple[Any, dict[Any, Any]]]
    force_split_words: tuple[Any, Any, set[Any]] | None

    @classmethod
    def capture(cls, module: Any) -> "_JiebaRuntimeState":
        tokenizer = module.dt
        scalar_values = {
            name: getattr(tokenizer, name, _MISSING)
            for name in ("initialized", "tmp_dir", "cache_file", "dictionary", "total")
        }
        mapping_values: dict[str, tuple[Any, dict[Any, Any]]] = {}
        for name in ("FREQ", "user_word_tag_tab"):
            value = getattr(tokenizer, name, _MISSING)
            if isinstance(value, dict):
                mapping_values[name] = (value, dict(value))
            else:
                scalar_values[name] = value

        finalseg = getattr(module, "finalseg", None)
        force_split = getattr(finalseg, "Force_Split_Words", _MISSING)
        force_split_words = (
            (finalseg, force_split, set(force_split))
            if isinstance(force_split, set)
            else None
        )
        return cls(
            tokenizer=tokenizer,
            scalar_values=scalar_values,
            mapping_values=mapping_values,
            force_split_words=force_split_words,
        )

    @staticmethod
    def _restore_attribute(target: Any, name: str, value: Any) -> None:
        if value is _MISSING:
            try:
                delattr(target, name)
            except AttributeError:
                pass
            return
        setattr(target, name, value)

    def restore(self) -> None:
        # Restore mapping *objects* in place before rebinding the tokenizer.
        # Jieba publishes ``user_word_tag_tab`` as another module-level alias,
        # so assigning a fresh dict would leave callers with stale mutations.
        for name, (original, values) in self.mapping_values.items():
            original.clear()
            original.update(values)
            setattr(self.tokenizer, name, original)
        for name, value in self.scalar_values.items():
            self._restore_attribute(self.tokenizer, name, value)

        if self.force_split_words is not None:
            finalseg, original, values = self.force_split_words
            original.clear()
            original.update(values)
            setattr(finalseg, "Force_Split_Words", original)


@contextmanager
def preserve_jieba_global_state() -> Iterator[None]:
    """Restore jieba after a short-lived, isolated runtime workspace.

    This is deliberately a test/verification lifecycle seam, not a normal
    Application operation.  It neither initializes jieba nor writes a cache;
    callers still must use :class:`TextProcessor` with an explicit Config and
    a writer lease for any cache-producing work.
    """

    state = _JiebaRuntimeState.capture(jieba)
    try:
        yield
    finally:
        state.restore()


class TextProcessor:
    """文本处理器"""

    def __init__(
        self,
        custom_dict_path: Optional[str] = None,
        *,
        runtime_config: Any | None = None,
        initialize_cache: bool = False,
    ):
        """
        初始化文本处理器

        Args:
            custom_dict_path: jieba 自定义词典路径
            runtime_config: Application 捕获的 Config snapshot。产品路径必须
                显式传入，避免 Config B 回退到全局 Config A。
            initialize_cache: 仅 runtime bootstrap 在持有数据根 writer lease
                时传入；普通读取不得借分词静默创建 jieba cache。
        """
        # 产品默认资源必须通过 RuntimeLayout 解析；显式路径仅保留为测试/
        # 运维注入 seam，不能影响产品的 bundled-resource 根。
        self._runtime_config = runtime_config
        if custom_dict_path is None:
            legacy_global_config = runtime_config is None
            if runtime_config is None:
                # Historical direct utility/test compatibility.  Product
                # Application composition always injects its captured Config.
                from src.utils.config import get_config

                runtime_config = get_config()
            self._runtime_config = runtime_config
            layout = runtime_config.layout
            self._bind_runtime_cache(
                layout,
                initialize_cache=initialize_cache or legacy_global_config,
                require_writer_lease=not legacy_global_config,
            )
            dict_path = layout.validate_bundled_path(
                layout.custom_dict_path,
                label="jieba 自定义词典",
            )
        else:
            dict_path = Path(custom_dict_path)
        if dict_path.exists():
            jieba.load_userdict(str(dict_path))

    @staticmethod
    def _bind_runtime_cache(
        layout: Any,
        *,
        initialize_cache: bool,
        require_writer_lease: bool = False,
    ) -> None:
        """Bind jieba to one Config snapshot without allowing read-time writes.

        Jieba owns one process-global tokenizer.  Bootstrap initializes that
        tokenizer while holding the data-root lease.  Later Application readers
        must verify *their own* snapshot cache even if another Config has
        already initialized jieba in this process; they fail closed rather than
        recreating it from a read/search path.

        ``runtime_config=None`` remains the historical utility compatibility
        path.  Only an explicitly injected product Config requires a live
        writer lease when initialization/publishing is requested.
        """

        cache_path = layout.validate_user_file(
            Path(layout.tmp_dir) / "jieba.cache",
            label="jieba 运行态缓存",
            allow_missing=True,
        )
        cache_exists = cache_path.is_file()
        if jieba.dt.initialized:
            if cache_exists:
                # ``initialize`` is a no-op after the first process-wide
                # tokenizer bootstrap, but ``tmp_dir`` remains a mutable
                # third-party global.  Rebind it to the explicit snapshot only
                # after that snapshot's durable cache has been verified.  This
                # is metadata-only (no initialization or write) and prevents a
                # Config B reader from inheriting Config A's cache location.
                jieba.dt.tmp_dir = str(layout.tmp_dir)
                return
            if not initialize_cache:
                TextProcessor._raise_missing_runtime_cache()
            TextProcessor._require_cache_writer_lease(
                layout,
                required=require_writer_lease,
            )
            # A confirmed B bootstrap may materialize B's cache from the
            # already-loaded global dictionary.  Keep the global cache target
            # aligned with the snapshot that owns that writer operation.
            jieba.dt.tmp_dir = str(layout.tmp_dir)
            if require_writer_lease:
                TextProcessor._publish_initialized_runtime_cache(layout, cache_path)
            return
        if not initialize_cache:
            if not cache_exists:
                TextProcessor._raise_missing_runtime_cache()
            # ``jieba.cut`` will lazily load this existing cache.  Do not call
            # initialize here: a read path must not be the code path that could
            # regenerate a missing/corrupt cache.
            jieba.dt.tmp_dir = str(layout.tmp_dir)
            return
        TextProcessor._require_cache_writer_lease(
            layout,
            required=require_writer_lease,
        )
        jieba.dt.tmp_dir = str(layout.tmp_dir)
        jieba.initialize()
        # Jieba intentionally suppresses some cache-write failures.  PKV's
        # bootstrap contract cannot: a later fresh reader needs a durable cache
        # and must never repair it from a read operation.
        validated_cache = layout.validate_user_file(
            cache_path,
            label="jieba 运行态缓存",
            allow_missing=True,
        )
        if require_writer_lease and not validated_cache.is_file():
            TextProcessor._raise_missing_runtime_cache()

    @staticmethod
    def _raise_missing_runtime_cache() -> None:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "jieba 运行态缓存缺失；请先通过确认的运行时初始化或修复流程恢复。",
            stage="tokenizer_cache",
            recoverable=True,
        )

    @staticmethod
    def _require_cache_writer_lease(layout: Any, *, required: bool) -> None:
        if not required:
            return
        from src.runtime.write_lease import has_active_write_lease

        if not has_active_write_lease(layout):
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "jieba 运行态缓存只能由已确认的运行时初始化或修复写入。",
                stage="tokenizer_cache",
                recoverable=True,
            )

    @staticmethod
    def _publish_initialized_runtime_cache(layout: Any, cache_path: Path) -> None:
        """Materialize this snapshot's cache from an already-global tokenizer.

        The third-party tokenizer is process-global, so a second Config cannot
        ask ``jieba.initialize()`` to create another file after the first
        Config has initialized it.  Persisting the already-loaded dictionary
        through RuntimeLayout keeps the per-data-root bootstrap contract without
        retargeting or reinitializing the live tokenizer.
        """

        try:
            payload = marshal.dumps((jieba.dt.FREQ, jieba.dt.total))
        except (AttributeError, TypeError, ValueError) as error:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "jieba 运行态缓存无法从当前分词器安全建立。",
                stage="tokenizer_cache",
                recoverable=True,
            ) from error
        layout.atomic_publish_user_file(
            cache_path,
            label="jieba 运行态缓存",
            data=payload,
        )

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
            "keywords": TextProcessor.tokenize_chinese(
                TextProcessor._normalize_terms(keywords)
            ),
            "tags": TextProcessor.tokenize_chinese(
                TextProcessor._normalize_terms(tags)
            ),
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
        清理文件名，生成可安全传给 Vault 路径网关的 stem。

        Args:
            title: 原始标题
            max_length: 最大长度

        Returns:
            清理后的文件名

        Example:
            >>> TextProcessor.sanitize_filename("AI驱动的知识管理?")
            "AI驱动的知识管理？"
        """
        # Both separators are normalized regardless of the host platform: a
        # title may be created on one platform and later archived on another.
        # Cc characters are never valid in a Vault filename (and include NUL,
        # tabs and newlines), so remove them before applying display-friendly
        # punctuation replacements.
        title = "".join(
            character for character in title if unicodedata.category(character) != "Cc"
        )

        # 替换规则
        replacements = {
            "/": "-",
            "\\": "-",
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
        title = re.sub(r"[<>|]", "", title)

        # 限制长度。单独的 ``.``/``..`` 不是文件名 stem；归一为稳定的
        # 回退名，保证 MarkdownStore 不会把它交给路径层。
        title = title[:max_length]
        if title in {"", ".", ".."}:
            return "untitled"[:max_length]
        return title

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
        text = re.sub(r"\s+", "", text)

        # 计算中文字符数
        chinese_chars = re.findall(r"[\u4e00-\u9fff]", text)
        chinese_count = len(chinese_chars)

        # 计算英文单词数
        english_words = re.findall(r"[a-zA-Z]+", text)
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
