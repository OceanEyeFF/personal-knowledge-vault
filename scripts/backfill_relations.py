"""
关系层回填工具。

默认 dry-run，不会写入数据库。
使用 `--apply` 后才会执行实际写入。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.relations.extractors import RelationBackfillService
from src.utils.config import Config


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PKV 关系层回填工具")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行真实写入；默认仅 dry-run",
    )
    parser.add_argument(
        "--knowledge-id",
        action="append",
        type=int,
        dest="knowledge_ids",
        help="仅回填指定 knowledge_id，可重复传入",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="覆盖 SQLite 路径（默认读取配置）",
    )
    parser.add_argument(
        "--vault-dir",
        type=str,
        default=None,
        help="覆盖 vault 路径（默认读取配置）",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    config = Config()
    db_path = Path(args.db_path) if args.db_path else config.db_path
    vault_dir = Path(args.vault_dir) if args.vault_dir else config.vault_dir

    service = RelationBackfillService(db_path=db_path, vault_dir=vault_dir)
    report = service.backfill(
        knowledge_ids=args.knowledge_ids,
        apply=args.apply,
    )

    mode = "APPLY" if args.apply else "DRY-RUN"
    print("=" * 70)
    print(f" PKV 关系回填 ({mode})")
    print("=" * 70)
    print(f"数据库路径: {db_path}")
    print(f"Vault 路径: {vault_dir}")
    print(f"扫描条目: {report.scanned_entries}")
    print(f"处理条目: {report.processed_entries}")
    print(f"提取关系: {report.extracted_relations}")
    print(f"删除旧关系: {report.deleted_relations}")
    print(f"写入关系: {report.applied_relations}")
    print(f"缺失文件: {len(report.missing_files)}")
    print(f"跳过引用: {len(report.skipped_references)}")

    if report.missing_files:
        print("\n缺失文件:")
        for item in report.missing_files[:10]:
            print(f"  - {item}")

    if report.skipped_references:
        print("\n跳过引用:")
        for item in report.skipped_references[:10]:
            print(
                "  - "
                f"source={item['source_knowledge_id']} "
                f"target={item['raw_target']} "
                f"reason={item['reason']}"
            )

    if not args.apply:
        print("\n提示: 默认是 dry-run，确认结果后再加 `--apply` 执行真实写入。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
