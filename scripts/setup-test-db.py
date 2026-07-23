#!/usr/bin/env python3
"""
Generate a reproducible test SQLite database for MCP tests.

Usage examples:
  python scripts/setup-test-db.py --count 20
  python scripts/setup-test-db.py --seed 42 --count 50 --output /tmp/test.db --allow-outside-test-root
  python scripts/setup-test-db.py --count 30 --wechat-count 5 --zhihu-count 10
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

from bs4 import BeautifulSoup
import frontmatter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
        default=".data-test/db/knowledge_vault.db",
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
        "--allow-outside-test-root",
        action="store_true",
        help=(
            "Allow an output outside the repository .data-test directory. "
            "Repository paths outside .data-test remain forbidden."
        ),
    )
    return parser.parse_args()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_output_path(
    path_str: str,
    *,
    allow_outside_test_root: bool,
) -> tuple[Path, bool]:
    """Resolve output and enforce the non-production test-data boundary."""
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    lexical_path = Path(os.path.abspath(path))
    project_root = PROJECT_ROOT.resolve()
    lexical_test_root = PROJECT_ROOT / ".data-test"

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
        return resolved, True
    if _is_relative_to(lexical_path, project_root):
        raise ValueError(
            "仓库内测试数据库只能写入 .data-test；禁止覆盖 .data 或其他项目文件"
        )
    resolved = lexical_path.resolve(strict=False)
    if _is_relative_to(resolved, project_root):
        raise ValueError("外部输出路径不得通过链接指向仓库内部")
    if not allow_outside_test_root:
        raise ValueError(
            "输出路径必须位于仓库 .data-test；外部临时目录需显式传入 "
            "--allow-outside-test-root"
        )
    return resolved, False


def _derive_base_dir(db_path: Path) -> Path:
    if db_path.parent.name == "db":
        return db_path.parent.parent
    return db_path.parent


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
    *,
    managed_test_root: bool,
) -> None:
    for target in (vault_dir, vector_dir):
        if target.exists():
            if _is_unsafe_link(target):
                raise ValueError(f"拒绝清理链接形式的测试目录: {target}")
            for child in target.rglob("*"):
                if _is_unsafe_link(child):
                    raise ValueError(f"拒绝清理包含链接的测试目录: {child}")
            if managed_test_root:
                continue
            elif any(target.iterdir()):
                raise ValueError(
                    "外部测试目录已存在且非空，拒绝递归清理；"
                    "请改用新的空目录或仓库 .data-test"
                )

    if db_path.exists():
        if _is_unsafe_link(db_path):
            raise ValueError(f"拒绝覆盖链接形式的测试数据库: {db_path}")
        db_path.unlink()

    for target in (vault_dir, vector_dir):
        if target.exists() and managed_test_root:
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
    allow_outside_test_root: bool = False,
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

    db_path, managed_test_root = _resolve_output_path(
        str(output),
        allow_outside_test_root=allow_outside_test_root,
    )
    base_dir = _derive_base_dir(db_path)
    vault_dir = base_dir / "vault"
    vector_dir = base_dir / "vectors"

    _safe_prepare_dirs(
        db_path,
        vault_dir,
        vector_dir,
        managed_test_root=managed_test_root,
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
            allow_outside_test_root=args.allow_outside_test_root,
        )
    except Exception as exc:
        print(f"[error] {exc}")
        return 1

    print(f"Test database created: {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
