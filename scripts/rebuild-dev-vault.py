#!/usr/bin/env python3
"""开发专用轻量重建入口（P1）。

在安全隔离根上执行 可受控清理 -> 数据库迁移 -> 确定性最小种子 -> 健康检查
的完整重建流程，并支持幂等重复执行。

安全契约（禁止生产数据）:
- 默认根目录为仓库内 ``.data-test/rebuild-dev``，绝不隐式指向 ``.data``。
- 自定义根目录必须位于仓库 ``.data-test`` 下；``.data``、仓库其他目录、
  文件系统根、用户主目录等危险目标一律拒绝。
- 仓库外根目录默认拒绝；仅显式传入 ``--allow-outside-repo``（CI/测试专用）
  才允许，且仍拒绝 ``.data`` 与危险目标。
- 清理前检查路径上的 junction / 符号链接 / 硬链接，拒绝绕过边界。
- 迁移始终以 ``auto_backup=False`` 执行（自动备份脚本会读取生产 ``.data``）。

用法:
  python scripts/rebuild-dev-vault.py                  # 重建或检查默认根
  python scripts/rebuild-dev-vault.py --root .data-test/rebuild-dev
  python scripts/rebuild-dev-vault.py --force          # 受控清理后完整重建
  python scripts/rebuild-dev-vault.py --check-only     # 仅健康检查，绝不写入
  python scripts/rebuild-dev-vault.py --json           # 机器可读结果契约

退出码:
  0  成功（重建 / 已最新 / 健康检查通过）
  1  流程失败（迁移、种子或健康检查不通过）
  2  参数或根目录校验拒绝（危险目标等）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import shutil
import stat
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import frontmatter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.storage.markdown_store import Entry, MarkdownStore  # noqa: E402
from src.storage.migration_manager import MigrationManager  # noqa: E402
from src.storage.sqlite_store import SQLiteStore  # noqa: E402

DEFAULT_ROOT_REL = ".data-test/rebuild-dev"
DEFAULT_SEED = 20260731
DEFAULT_COUNT = 3
MIGRATIONS_DIR = PROJECT_ROOT / "scripts" / "migrations"

# 生产数据目录名（大小写不敏感比较），无论位于何处都拒绝作为重建根。
_PROHIBITED_ROOT_NAMES = {".data", ".data_backup", ".data-backup", "backups"}
# 测试根本身也不得作为重建根：必须使用其下的专用子目录。
_TEST_ROOT_NAMES = {".data-test", ".data_test"}

# 确定性最小种子内容（不依赖外部 fixture / 网络 / AI 服务）。
_SEED_TOPICS = ["开发环境", "测试契约", "知识工作流", "离线验证"]
_SEED_BODIES = [
    "这是开发重建用的确定性种子条目，仅用于验证本地开发环境。",
    "内容不包含任何用户真实数据，可由固定种子完全复现。",
    "用于验证迁移链、统计与检索契约在隔离根上正常工作。",
]
_SEED_TAGS = ["重建", "种子", "开发", "测试"]


class RebuildError(Exception):
    """重建流程失败（退出码 1）。"""


class RootRejectedError(RebuildError):
    """根目录校验拒绝（退出码 2）。"""


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_unsafe_link(path: Path) -> bool:
    """检测符号链接 / junction / 硬链接，识别前不得执行破坏性操作。"""
    try:
        path_stat = os.lstat(path)
    except FileNotFoundError:
        return False
    file_attributes = getattr(path_stat, "st_file_attributes", 0)
    is_reparse_point = bool(file_attributes & 0x400)
    is_hard_linked_file = stat.S_ISREG(path_stat.st_mode) and path_stat.st_nlink > 1
    return stat.S_ISLNK(path_stat.st_mode) or is_reparse_point or is_hard_linked_file


def _has_production_name(path: Path) -> bool:
    return path.name.casefold() in _PROHIBITED_ROOT_NAMES


def _is_test_root_itself(path: Path) -> bool:
    return path.name.casefold() in _TEST_ROOT_NAMES


def _has_unsafe_link_on_path(root: Path) -> Optional[Path]:
    """检查现有路径链上的 junction / 符号链接 / 硬链接。"""
    parts = list(root.parts)
    for index in range(len(parts) - 1, -1, -1):
        probe = Path(*parts[: index + 1])
        if probe.exists() or probe.is_symlink():
            if _is_unsafe_link(probe):
                return probe
    return None


def _assert_no_unsafe_links_under(root: Path) -> None:
    """清理前递归检查目标目录内不存在链接，避免误删链接指向的真实数据。"""
    if not root.exists():
        return
    if _is_unsafe_link(root):
        raise RootRejectedError(f"拒绝清理链接形式的根目录: {root}")
    for child in root.rglob("*"):
        if _is_unsafe_link(child):
            raise RootRejectedError(f"拒绝清理包含链接的目录: {child}")


def resolve_rebuild_root(
    root_arg: str,
    *,
    project_root: Path = PROJECT_ROOT,
    allow_outside_repo: bool = False,
) -> Tuple[Path, bool]:
    """解析并校验重建根目录，返回 (规范化根, 是否位于仓库内)。"""
    lexical = Path(root_arg).expanduser()
    if not lexical.is_absolute():
        lexical = project_root / lexical
    lexical = Path(os.path.abspath(lexical))

    project_root = project_root.resolve()
    lexical_test_root = (project_root / ".data-test").resolve()

    # 1. 危险目标：文件系统根、项目根本身、用户主目录、生产数据目录名。
    if lexical == lexical.parent:
        raise RootRejectedError(f"拒绝文件系统根目录作为重建根: {lexical}")
    if lexical == project_root:
        raise RootRejectedError("重建根不能是项目根目录")
    try:
        if lexical == Path.home().resolve():
            raise RootRejectedError("重建根不能是用户主目录")
    except (OSError, RuntimeError):
        pass
    if _has_production_name(lexical):
        raise RootRejectedError(
            f"拒绝生产数据目录作为重建根: {lexical}（不得指向 .data 等目录）"
        )
    if _is_test_root_itself(lexical):
        raise RootRejectedError(
            f"重建根必须是 .data-test 下的专用子目录，不能是 .data-test 本身: {lexical}"
        )

    # 2. 仓库内路径：只允许严格位于 .data-test 下的专用子目录。
    if _is_relative_to(lexical, project_root):
        if _is_relative_to(lexical, lexical_test_root):
            if lexical == lexical_test_root:
                raise RootRejectedError(
                    "重建根必须是 .data-test 下的专用子目录，"
                    f"不能是 .data-test 本身: {lexical}"
                )
            link = _has_unsafe_link_on_path(lexical_test_root)
            if link is not None:
                raise RootRejectedError(
                    f"测试数据路径不得经过 junction 或符号链接: {link}"
                )
            link = _has_unsafe_link_on_path(lexical)
            if link is not None:
                raise RootRejectedError(
                    f"测试数据路径不得经过 junction 或符号链接: {link}"
                )
            resolved = lexical.resolve(strict=False)
            if not _is_relative_to(resolved, lexical_test_root):
                raise RootRejectedError(
                    "测试数据路径解析后越过仓库 .data-test 边界"
                )
            return resolved, True
        raise RootRejectedError(
            f"仓库内重建根只能位于 .data-test 下，禁止其他项目路径: {lexical}"
        )

    # 3. 仓库外路径：默认拒绝；CI/测试可显式 --allow-outside-repo。
    if not allow_outside_repo:
        raise RootRejectedError(
            f"仓库外重建根默认拒绝: {lexical}；"
            "CI/测试临时目录需显式传入 --allow-outside-repo"
        )
    for part in lexical.parts[1:]:
        if Path(part).name.casefold() in _PROHIBITED_ROOT_NAMES:
            raise RootRejectedError(
                f"外部重建根路径不得包含生产数据目录名: {part}"
            )
    link = _has_unsafe_link_on_path(lexical)
    if link is not None:
        raise RootRejectedError(f"外部重建根不得经过 junction 或符号链接: {link}")
    resolved = lexical.resolve(strict=False)
    if _is_relative_to(resolved, project_root):
        raise RootRejectedError("外部重建根不得通过链接指向仓库内部")
    return resolved, False


def _cleanup_root(root: Path) -> None:
    """受控清理：删除根目录内容并重建，不删除根目录本身。"""
    if root.exists():
        _assert_no_unsafe_links_under(root)
        for child in root.iterdir():
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except OSError as exc:
                raise RebuildError(f"受控清理失败: {child} - {exc}") from exc
    root.mkdir(parents=True, exist_ok=True)


def _run_migrations(db_path: Path) -> int:
    """执行数据库迁移链（auto_backup=False，禁止备份脚本读取生产 .data）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    applied = manager.apply_all_pending(auto_backup=False)
    return applied


def _build_seed_entry(
    index: int,
    *,
    rng: random.Random,
    base_time: datetime,
    used_titles: set,
) -> Entry:
    topic = _SEED_TOPICS[index % len(_SEED_TOPICS)]
    title = f"重建种子 {index + 1:03d} {topic}"
    if title in used_titles:
        title = f"{title}-{rng.randint(100, 999)}"
    used_titles.add(title)

    body = "\n\n".join(_SEED_BODIES)
    archived_at = (base_time + timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M:%S")
    tags = [_SEED_TAGS[index % len(_SEED_TAGS)], "重建"]
    keywords = ["rebuild", "seed", f"seed-{index + 1}"]

    return Entry(
        title=title,
        source_type="text",
        source_url=None,
        archived_at=archived_at,
        tags=tags,
        keywords=keywords,
        abstract=f"{topic} 确定性种子条目",
        summary_one_sentence=f"{topic} - 确定性最小种子条目",
        summary_100_words=body,
        search_strategy="keyword",
        word_count=len(body),
        content=f"# {title}\n\n{body}",
    )


def _seed_entries(
    root: Path,
    db_path: Path,
    *,
    seed: int,
    count: int,
) -> int:
    """生成确定性最小种子：Markdown 主存储 + SQLite 索引 + FTS5。"""
    if count <= 0:
        raise RebuildError("count 必须大于 0")
    store = SQLiteStore(db_path)
    md_store = MarkdownStore(vault_dir=root / "vault")
    rng = random.Random(seed)
    used_titles: set = set()
    base_time = datetime(2026, 2, 1, 9, 0, 0)

    file_paths: List[Path] = []
    entry_ids: List[int] = []
    for index in range(count):
        entry = _build_seed_entry(
            index, rng=rng, base_time=base_time, used_titles=used_titles
        )
        file_path = md_store.save(entry)
        knowledge_id = store.insert_entry(entry, str(file_path))
        file_paths.append(file_path)
        entry_ids.append(knowledge_id)

    if len(entry_ids) > 1:
        for index, file_path in enumerate(file_paths):
            candidates = [kid for kid in entry_ids if kid != entry_ids[index]]
            related_count = rng.randint(1, min(2, len(candidates)))
            related_ids = rng.sample(candidates, k=related_count)
            _update_related_docs(file_path, related_ids)

    return len(entry_ids)


def _update_related_docs(file_path: Path, related_ids: List[int]) -> None:
    post = frontmatter.load(file_path)
    post.metadata["related_docs"] = related_ids
    file_path.write_text(frontmatter.dumps(post), encoding="utf-8")


def _current_version_if_exists(db_path: Path) -> str:
    """读取数据库版本；文件不存在时避免创建连接（只读路径）。"""
    if not db_path.exists():
        return "0.0.0"
    return MigrationManager(db_path, MIGRATIONS_DIR).get_current_version()


def _health_report(root: Path, db_path: Path) -> Dict:
    """只读健康检查：迁移链健康 + 数据库统计。"""
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    health = manager.run_health_check()
    stats: Dict = {}
    if db_path.exists():
        store = SQLiteStore(db_path)
        if store.table_exists("knowledge_items"):
            stats = store.get_statistics()
    return {
        "root": str(root),
        "db_path": str(db_path),
        "schema_version": _current_version_if_exists(db_path),
        "health": health,
        "stats": stats,
    }


def _check_only(db_path: Path) -> Dict:
    """仅健康检查（绝不写入）。"""
    manager = MigrationManager(db_path, MIGRATIONS_DIR)
    health = manager.run_health_check()
    stats: Dict = {}
    if db_path.exists():
        store = SQLiteStore(db_path)
        if store.table_exists("knowledge_items"):
            stats = store.get_statistics()
    return {
        "db_path": str(db_path),
        "schema_version": _current_version_if_exists(db_path),
        "health": health,
        "stats": stats,
    }


def _print_summary(report: Dict, *, quiet: bool) -> None:
    if quiet:
        return
    phase = report.get("phase", "checked")
    health = report["health"]
    issues = health.get("issues", [])
    stats = report.get("stats", {})
    print("=" * 60)
    print(" PKV 开发重建结果")
    print("=" * 60)
    print(f"  根目录: {report.get('root', '-')}")
    print(f"  阶段: {phase}")
    print(f"  数据库: {report.get('db_path', '-')}")
    print(f"  迁移版本: {report['schema_version']}")
    if report.get("migrations_applied") is not None:
        print(f"  本次应用迁移: {report['migrations_applied']}")
    if stats:
        print(f"  条目总数: {stats.get('total_entries', 0)}")
    if phase == "up_to_date":
        print("  ✓ 根目录已是最新且健康，未做任何写入")
    if health.get("healthy"):
        print("  ✓ 迁移链健康检查通过")
    else:
        print("  ✗ 迁移链健康检查发现问题:")
        for issue in issues:
            print(f"    - {issue}")
    print("=" * 60)


def _emit_json(payload: Dict, exit_code: int) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(exit_code)


def _prepare_json_mode() -> None:
    """--json 模式下保持 stdout/stderr 契约干净（仅输出 JSON）。"""
    logging.getLogger("jieba").disabled = True


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PKV 开发专用轻量重建（安全隔离根，禁止生产 .data）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--root",
        default=DEFAULT_ROOT_REL,
        help=f"重建根目录（默认: {DEFAULT_ROOT_REL}，必须位于 .data-test 下）",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"种子随机种子（默认: {DEFAULT_SEED}）",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        help=f"种子条目数量（默认: {DEFAULT_COUNT}）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="受控清理现有根目录后完整重建（清理前校验链接）",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="跳过种子生成（仅迁移 + 健康检查）",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="仅健康检查，绝不写入",
    )
    parser.add_argument(
        "--allow-outside-repo",
        action="store_true",
        help="CI/测试专用：允许仓库外根目录（仍拒绝 .data 与危险目标）",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON 结果契约")
    parser.add_argument("--quiet", action="store_true", help="抑制人类可读输出")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    if args.json:
        _prepare_json_mode()

    try:
        root, _in_repo = resolve_rebuild_root(
            args.root,
            allow_outside_repo=args.allow_outside_repo,
        )
        db_path = root / "db" / "knowledge_vault.db"

        if args.check_only:
            report = _check_only(db_path)
            report["phase"] = "checked"
            report["ok"] = bool(report["health"].get("healthy"))
            if args.json:
                _emit_json(report, 0 if report["ok"] else 1)
            _print_summary(report, quiet=args.quiet)
            return 0 if report["ok"] else 1

        root_exists_nonempty = root.exists() and any(root.iterdir())

        if root_exists_nonempty and not args.force:
            # 幂等路径：已有根目录不隐式清理，只做只读健康检查。
            report = _health_report(root, db_path)
            report["phase"] = "up_to_date"
            report["ok"] = bool(report["health"].get("healthy"))
            report["stats"] = report.get("stats") or {}
            if args.json:
                _emit_json(report, 0 if report["ok"] else 1)
            _print_summary(report, quiet=args.quiet)
            return 0 if report["ok"] else 1

        if args.force and root_exists_nonempty:
            _cleanup_root(root)
        else:
            root.mkdir(parents=True, exist_ok=True)

        applied = _run_migrations(db_path)
        seeded = 0
        if not args.no_seed:
            seeded = _seed_entries(root, db_path, seed=args.seed, count=args.count)

        report = _health_report(root, db_path)
        report["phase"] = "rebuilt"
        report["migrations_applied"] = applied
        report["seeded"] = seeded
        report["ok"] = bool(report["health"].get("healthy"))
        if args.json:
            _emit_json(report, 0 if report["ok"] else 1)
        _print_summary(report, quiet=args.quiet)
        return 0 if report["ok"] else 1

    except RootRejectedError as exc:
        if args.json:
            _emit_json({"ok": False, "error": str(exc)}, 2)
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    except RebuildError as exc:
        if args.json:
            _emit_json({"ok": False, "error": str(exc)}, 1)
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - 防御性兜底
        if args.json:
            _emit_json({"ok": False, "error": f"未预期错误: {exc}"}, 1)
        print(f"[error] 未预期错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
