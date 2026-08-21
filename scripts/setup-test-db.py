#!/usr/bin/env python3
r"""
Generate a reproducible test SQLite database for MCP tests.

Usage examples:
  .\scripts\run-test.ps1 -Direct -DataRoot .data-test\seed -Command `
    @("python", "scripts/setup-test-db.py", "--seed", "42", "--count", "20")
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import stat
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.offline_runtime import require_offline_runtime_ready  # noqa: E402

require_offline_runtime_ready(process_guarded=True)

from bs4 import BeautifulSoup  # noqa: E402
import frontmatter  # noqa: E402

from src.storage.markdown_store import Entry, MarkdownStore  # noqa: E402
from src.storage.sqlite_store import SQLiteStore  # noqa: E402


try:
    from tests.fixtures.sample_data import WECHAT_SAMPLES, ZHIHU_SAMPLES
except Exception as exc:  # pragma: no cover - import failure is fatal
    raise SystemExit(
        f"Failed to import tests.fixtures.sample_data: {exc}\n"
        "Please ensure tests/fixtures/sample_data.py exists."
    ) from exc


TAGS_POOL = {
    "wechat": ["微信", "公众号", "技术", "分享", "样本", "测试"],
    "zhihu": ["知乎", "问答", "讨论", "知识", "样本", "测试"],
    "text": ["笔记", "随笔", "知识库", "测试", "纯文本", "样本"],
}


# This pair is deliberately a fixture-only structural value.  It is never
# persisted to a user profile or the data-runtime snapshot, and the offline
# guard still prevents Provider I/O.  ``--runtime-ready`` uses it solely to
# make the synthetic fixture satisfy the same structural readiness contract as
# the externally launched internal-package smoke process.
_SYNTHETIC_RUNTIME_READY_CONFIG_UPDATES = {
    "ai.llm.api_key": "offline-test-placeholder",
    "ai.embedding.api_key": "offline-test-placeholder",
}

KEYWORDS_POOL = [
    "AI",
    "知识库",
    "检索",
    "工作流",
    "自动化",
    "向量",
    "测试",
    "样本",
    "标签",
]

TEXT_TOPICS = [
    "AI 工作流",
    "知识管理",
    "检索策略",
    "个人笔记",
    "测试数据",
    "工具链",
    "信息整理",
]

TEXT_SENTENCES = [
    "这是一段用于测试的纯文本内容，包含一些关键术语与标签。",
    "系统需要能够稳定地处理不同来源与不同长度的文本条目。",
    "本文强调工作流驱动的知识管理思路，并兼顾检索效率。",
    "为了覆盖更多路径，这里加入一些随机的主题描述与结论。",
    "实际使用中，摘要与关键词应便于快速理解和检索。",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a test SQLite database for MCP tests.")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility.")
    parser.add_argument("--count", type=int, default=20, help="Total entries to generate.")
    parser.add_argument(
        "--output",
        type=str,
        default=os.environ["DB_PATH"],
        help="Output database path.",
    )
    parser.add_argument("--wechat-count", type=int, default=3, help="Wechat entries count.")
    parser.add_argument("--zhihu-count", type=int, default=3, help="Zhihu entries count.")
    parser.add_argument(
        "--embedding-dim",
        type=int,
        default=None,
        help=(
            "Dimension for the optional random vector index. "
            "When omitted, no vector index is created and no Provider/config "
            "is accessed."
        ),
    )
    parser.add_argument(
        "--runtime-ready",
        action="store_true",
        help=(
            "Prepare the selected default test DB for an offline READY fixture. "
            "FT7-only; writes only the secret-free runtime snapshot and tokenizer "
            "cache, and never probes a Provider."
        ),
    )
    return parser.parse_args()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_selected_data_root() -> Path:
    """Return the attested DataRoot after containment and no-link checks."""

    lexical_test_root = PROJECT_ROOT / ".data-test"
    lexical_data_root = Path(
        os.path.abspath(os.path.normpath(os.environ["DATA_DIR"]))
    )
    if not _is_relative_to(lexical_data_root, lexical_test_root):
        raise ValueError("Direct Python DATA_DIR 必须位于仓库 .data-test")

    current = lexical_test_root
    relative = lexical_data_root.relative_to(lexical_test_root)
    for part in (Path(), *relative.parts):
        if part != Path():
            current /= part
        if _is_unsafe_link(current):
            raise ValueError(f"测试数据路径不得经过符号链接或 junction: {current}")

    resolved_test_root = lexical_test_root.resolve(strict=False)
    resolved_data_root = lexical_data_root.resolve(strict=False)
    if not _is_relative_to(resolved_data_root, resolved_test_root):
        raise ValueError("Direct Python DATA_DIR 解析后越过仓库 .data-test")
    return resolved_data_root


def _resolve_output_path(
    path_str: str,
) -> Path:
    """Resolve output and enforce the selected Direct Python DATA_DIR."""
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    lexical_path = Path(os.path.abspath(path))
    lexical_test_root = PROJECT_ROOT / ".data-test"
    lexical_data_root = _resolve_selected_data_root()

    if not _is_relative_to(lexical_path, lexical_data_root):
        raise ValueError("测试数据库必须位于当前 Direct Python DATA_DIR")

    if _is_relative_to(lexical_path, lexical_test_root):
        current = lexical_test_root
        relative_parent = lexical_path.parent.relative_to(lexical_test_root)
        for part in (Path(), *relative_parent.parts):
            if part != Path():
                current = current / part
            if _is_unsafe_link(current):
                raise ValueError(f"测试数据路径不得经过符号链接或 junction: {current}")
        if _is_unsafe_link(lexical_path):
            raise ValueError(f"测试数据库不得为符号链接或硬链接: {lexical_path}")

        resolved = lexical_path.resolve(strict=False)
        test_root = lexical_test_root.resolve(strict=False)
        if not _is_relative_to(resolved, test_root):
            raise ValueError("测试数据路径解析后越过仓库 .data-test 边界")
        data_root = lexical_data_root.resolve(strict=False)
        if not _is_relative_to(resolved, data_root):
            raise ValueError("测试数据库解析后越过当前 Direct Python DATA_DIR")
        return resolved
    raise AssertionError("validated output path did not resolve under .data-test")


def _resolve_managed_directory(path: Path, *, data_root: Path) -> Path:
    """Keep every derived cleanup target inside the selected DataRoot."""

    lexical = Path(os.path.abspath(os.path.normpath(path)))
    if not _is_relative_to(lexical, data_root):
        raise ValueError("派生测试目录必须位于当前 Direct Python DATA_DIR")

    current = data_root
    for part in lexical.relative_to(data_root).parts:
        current /= part
        if _is_unsafe_link(current):
            raise ValueError(f"测试数据路径不得经过符号链接或 junction: {current}")

    resolved = lexical.resolve(strict=False)
    if not _is_relative_to(resolved, data_root):
        raise ValueError("派生测试目录解析后越过当前 Direct Python DATA_DIR")
    return resolved


def _derive_managed_dirs(db_path: Path) -> tuple[Path, Path]:
    """Derive vault/vector roots without escaping a DataRoot named ``db``."""

    data_root = _resolve_selected_data_root()
    if db_path.parent.name.casefold() == "db" and db_path.parent != data_root:
        base_candidate = db_path.parent.parent
    else:
        base_candidate = db_path.parent
    base_dir = _resolve_managed_directory(base_candidate, data_root=data_root)
    vault_dir = _resolve_managed_directory(base_dir / "vault", data_root=data_root)
    vector_dir = _resolve_managed_directory(base_dir / "vectors", data_root=data_root)
    return vault_dir, vector_dir


def _is_unsafe_link(path: Path) -> bool:
    """Detect symlinks/junctions and hard-linked files before destructive work."""
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return False
    file_attributes = getattr(path_stat, "st_file_attributes", 0)
    is_reparse_point = bool(file_attributes & 0x400)
    is_hard_linked_file = stat.S_ISREG(path_stat.st_mode) and path_stat.st_nlink > 1
    return stat.S_ISLNK(path_stat.st_mode) or is_reparse_point or is_hard_linked_file


def _safe_prepare_dirs(
    db_path: Path,
    vault_dir: Path,
    vector_dir: Path,
) -> None:
    for target in (vault_dir, vector_dir):
        if target.exists():
            if _is_unsafe_link(target):
                raise ValueError(f"拒绝清理链接形式的测试目录: {target}")
            for child in target.rglob("*"):
                if _is_unsafe_link(child):
                    raise ValueError(f"拒绝清理包含链接的测试目录: {child}")
    database_artifacts = [
        db_path,
        *(Path(f"{db_path}{suffix}") for suffix in ("-journal", "-wal", "-shm")),
    ]
    existing_artifacts: list[Path] = []
    for artifact in database_artifacts:
        if not os.path.lexists(artifact):
            continue
        artifact_stat = os.lstat(artifact)
        if _is_unsafe_link(artifact) or not stat.S_ISREG(artifact_stat.st_mode):
            raise ValueError(f"拒绝覆盖链接或非普通文件形式的测试数据库: {artifact}")
        existing_artifacts.append(artifact)

    for artifact in existing_artifacts:
        artifact.unlink()

    for target in (vault_dir, vector_dir):
        if target.exists():
            shutil.rmtree(target)
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)


def _extract_text(tag) -> str:
    if tag is None:
        return ""
    return "\n".join([s.strip() for s in tag.stripped_strings if s.strip()])


def _extract_title(soup: BeautifulSoup, source_type: str) -> str:
    if source_type == "wechat":
        title_tag = soup.find("h1", id="activity-name")
        if title_tag and title_tag.get_text(strip=True):
            return title_tag.get_text(strip=True)
        meta_title = soup.find("meta", property="og:title")
        if meta_title and meta_title.get("content"):
            return meta_title["content"].strip()
    if source_type == "zhihu":
        title_tag = soup.find("h1", class_="QuestionHeader-title")
        if title_tag and title_tag.get_text(strip=True):
            return title_tag.get_text(strip=True)
        post_title = soup.find("h1", class_="Post-Title")
        if post_title and post_title.get_text(strip=True):
            return post_title.get_text(strip=True)
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True)
    return "Untitled"


def _select_best_zhihu_answer(soup: BeautifulSoup):
    answers = soup.select(".RichContent-inner")
    if not answers:
        return soup.body or soup

    def _score(tag) -> int:
        parent = tag.find_parent(attrs={"data-score": True})
        if parent and parent.get("data-score"):
            try:
                return int(parent.get("data-score"))
            except ValueError:
                return 0
        return 0

    return max(answers, key=_score)


def _make_abstract(text: str, max_len: int = 120) -> str:
    compact = " ".join(text.split())
    return compact[:max_len]


def _summary_100_words(text: str) -> str:
    words = re.split(r"\s+", text.strip())
    if len(words) <= 100:
        return text.strip()
    return " ".join(words[:100])


def _uniquify_url(url: str, suffix: int) -> str:
    if "?" in url:
        return f"{url}&sample={suffix}"
    return f"{url}?sample={suffix}"


def _pick_tags(source_type: str, rng: random.Random) -> List[str]:
    pool = TAGS_POOL.get(source_type, ["测试"])
    picks = rng.sample(pool, k=min(2, len(pool)))
    tags = [pool[0]] + picks
    seen = set()
    result = []
    for tag in tags:
        if tag and tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result


def _pick_keywords(title: str, rng: random.Random) -> List[str]:
    tokens = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", title)
    pool = KEYWORDS_POOL[:]
    rng.shuffle(pool)
    keywords = tokens[:2] + pool[:3]
    seen = set()
    result = []
    for keyword in keywords:
        if keyword and keyword not in seen:
            seen.add(keyword)
            result.append(keyword)
    return result


def _build_entry_from_html(
    source_type: str,
    url: str,
    html: str,
    rng: random.Random,
    index: int,
    archived_at: str,
    used_titles: set[str],
) -> Entry:
    soup = BeautifulSoup(html, "lxml")
    title = _extract_title(soup, source_type)
    if title in used_titles:
        title = f"{title} {index + 1}"
    used_titles.add(title)

    if source_type == "wechat":
        content_tag = soup.find("div", id="js_content") or soup.body
    else:
        content_tag = _select_best_zhihu_answer(soup)

    content = _extract_text(content_tag)
    if not content:
        content = _extract_text(soup.body) or "Sample content."

    abstract = _make_abstract(content)
    summary_one = f"{title} - {abstract}" if abstract else title
    summary_100 = _summary_100_words(content)

    tags = _pick_tags(source_type, rng)
    keywords = _pick_keywords(title, rng)

    return Entry(
        title=title,
        source_type=source_type,
        source_url=url,
        archived_at=archived_at,
        tags=tags,
        keywords=keywords,
        abstract=abstract,
        summary_one_sentence=summary_one,
        summary_100_words=summary_100,
        search_strategy="keyword",
        content=content,
    )


def _build_text_entry(
    rng: random.Random,
    index: int,
    archived_at: str,
    used_titles: set[str],
) -> Entry:
    topic = rng.choice(TEXT_TOPICS)
    title = f"{topic} 笔记 {index + 1}"
    if title in used_titles:
        title = f"{title}-{rng.randint(100, 999)}"
    used_titles.add(title)

    paragraph_count = rng.randint(2, 4)
    paragraphs = []
    for _ in range(paragraph_count):
        sentence_count = rng.randint(2, 3)
        paragraph = " ".join(rng.sample(TEXT_SENTENCES, k=sentence_count))
        paragraphs.append(paragraph)
    content = "\n\n".join(paragraphs)

    abstract = _make_abstract(content)
    summary_one = f"{title} - {abstract}" if abstract else title
    summary_100 = _summary_100_words(content)

    tags = _pick_tags("text", rng) + [topic]
    keywords = _pick_keywords(title, rng)

    return Entry(
        title=title,
        source_type="text",
        source_url=None,
        archived_at=archived_at,
        tags=tags,
        keywords=keywords,
        abstract=abstract,
        summary_one_sentence=summary_one,
        summary_100_words=summary_100,
        search_strategy="keyword",
        content=content,
    )


def _update_related_docs(file_path: Path, related_ids: List[int]) -> None:
    post = frontmatter.load(file_path)
    post.metadata["related_docs"] = related_ids
    file_path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _populate_vectors(
    vector_dir: Path,
    entry_ids: List[int],
    seed: int | None,
    embedding_dim: int | None,
) -> None:
    if embedding_dim is None:
        print(
            "[info] Random vector index skipped; use --embedding-dim for an "
            "offline test index."
        )
        return

    import hnswlib
    import numpy as np

    np_rng = np.random.default_rng(seed if seed is not None else 0)
    max_elements = max(10_000, len(entry_ids) + 1)

    for name in ("doc_vectors", "chunk_vectors"):
        index = hnswlib.Index(space="cosine", dim=embedding_dim)
        index.init_index(
            max_elements=max_elements,
            ef_construction=200,
            M=16,
        )
        index.set_ef(50)
        if name == "doc_vectors" and entry_ids:
            vectors = np_rng.random((len(entry_ids), embedding_dim)).astype("float32")
            index.add_items(vectors, ids=entry_ids)

        index.save_index(str(vector_dir / f"{name}.idx"))
        metadata = {
            "schema_version": 2,
            "dim": embedding_dim,
            "space": "cosine",
            "M": 16,
            "ef_construction": 200,
            "id_mapping": {},
        }
        (vector_dir / f"{name}_metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def build_database(
    seed: int | None,
    count: int,
    output: Path,
    wechat_count: int,
    zhihu_count: int,
    embedding_dim: int | None = None,
) -> Path:
    if count <= 0:
        raise ValueError("count 必须大于 0")
    if wechat_count < 0 or zhihu_count < 0:
        raise ValueError("wechat_count/zhihu_count 不能为负数")
    if count < wechat_count + zhihu_count:
        raise ValueError("count 必须 >= wechat_count + zhihu_count")
    if embedding_dim is not None and embedding_dim <= 0:
        raise ValueError("embedding_dim 必须大于 0")

    rng = random.Random(seed)

    db_path = _resolve_output_path(str(output))
    vault_dir, vector_dir = _derive_managed_dirs(db_path)

    _safe_prepare_dirs(
        db_path,
        vault_dir,
        vector_dir,
    )

    store = SQLiteStore(db_path)
    store.initialize()
    md_store = MarkdownStore(vault_dir=vault_dir)

    used_titles: set[str] = set()
    entries: List[Entry] = []
    base_time = datetime(2026, 2, 1, 9, 0, 0)

    for idx in range(wechat_count):
        url, html = WECHAT_SAMPLES[idx % len(WECHAT_SAMPLES)]
        url = _uniquify_url(url, idx + 1)
        archived_at = (base_time + timedelta(minutes=len(entries))).strftime("%Y-%m-%d %H:%M:%S")
        entry = _build_entry_from_html("wechat", url, html, rng, idx, archived_at, used_titles)
        entries.append(entry)

    for idx in range(zhihu_count):
        url, html = ZHIHU_SAMPLES[idx % len(ZHIHU_SAMPLES)]
        url = _uniquify_url(url, idx + 1)
        archived_at = (base_time + timedelta(minutes=len(entries))).strftime("%Y-%m-%d %H:%M:%S")
        entry = _build_entry_from_html("zhihu", url, html, rng, idx, archived_at, used_titles)
        entries.append(entry)

    text_count = count - wechat_count - zhihu_count
    for idx in range(text_count):
        archived_at = (base_time + timedelta(minutes=len(entries))).strftime("%Y-%m-%d %H:%M:%S")
        entry = _build_text_entry(rng, idx, archived_at, used_titles)
        entries.append(entry)

    entry_ids: List[int] = []
    file_paths: List[Path] = []
    for entry in entries:
        file_path = md_store.save(entry)
        knowledge_id = store.insert_entry(entry, str(file_path))
        entry_ids.append(knowledge_id)
        file_paths.append(file_path)

    if len(entry_ids) > 1:
        for idx, file_path in enumerate(file_paths):
            candidates = [kid for kid in entry_ids if kid != entry_ids[idx]]
            related_count = rng.randint(1, min(3, len(candidates)))
            related_ids = rng.sample(candidates, k=related_count)
            _update_related_docs(file_path, related_ids)

    _populate_vectors(vector_dir, entry_ids, seed, embedding_dim)

    return db_path


def _prepare_runtime_ready_fixture(db_path: Path) -> None:
    """Publish test-only readiness state for the selected synthetic DB.

    This option intentionally has a narrow meaning: the generated database
    must be the current Direct Python ``DB_PATH``.  A runtime snapshot describes
    the actual application data root, so letting it certify an arbitrary
    side-database would create a misleading fixture.  The existing offline
    entrypoint helper owns the no-Provider snapshot/cache mechanics; this
    script only invokes it *after* its deterministic SQLite fixture exists.
    """

    selected_db_path = _resolve_output_path(os.environ["DB_PATH"])
    if db_path != selected_db_path:
        raise ValueError("--runtime-ready 只能用于当前 Direct Python DB_PATH")

    from src.utils.config import Config
    from tests.offline_entrypoint import _seed_synthetic_ready_runtime_snapshot

    # Explicit constructor arguments retain their normal Config semantics even
    # when offline_entrypoint has installed its process-local Config facade.
    # The two non-empty placeholders are structural test data only; Config's
    # runtime snapshot deliberately excludes credentials and the offline guard
    # continues to prohibit any outbound Provider call.
    config = Config(
        str(PROJECT_ROOT / "config" / "config.yaml"),
        _user_config_updates=_SYNTHETIC_RUNTIME_READY_CONFIG_UPDATES,
    )
    if config.layout.db_path != db_path:
        raise RuntimeError("runtime-ready Config 未绑定当前合成数据库")

    _seed_synthetic_ready_runtime_snapshot(config)

    if config.read_runtime_config_snapshot() is None:
        raise RuntimeError("runtime-ready fixture 未发布运行态配置快照")
    if not (config.layout.tmp_dir / "jieba.cache").is_file():
        raise RuntimeError("runtime-ready fixture 未生成 jieba 运行态缓存")


def main() -> int:
    args = _parse_args()
    try:
        db_path = build_database(
            seed=args.seed,
            count=args.count,
            output=Path(args.output),
            wechat_count=args.wechat_count,
            zhihu_count=args.zhihu_count,
            embedding_dim=args.embedding_dim,
        )
        if args.runtime_ready:
            _prepare_runtime_ready_fixture(db_path)
    except Exception as exc:
        print(f"[error] {exc}")
        return 1

    print(f"Test database created: {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
