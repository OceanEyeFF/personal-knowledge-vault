"""
关系层回填工具。

默认 dry-run，不会写入数据库。
使用 `--apply` 后才会执行实际写入。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

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
    parser.add_argument(
        "--report-json",
        type=str,
        default=None,
        help="输出 JSON 质量报告（传入文件路径或 '-' 输出到 stdout）",
    )
    parser.add_argument(
        "--report-yaml",
        type=str,
        default=None,
        help="输出 YAML 质量报告（传入文件路径或 '-' 输出到 stdout）",
    )
    parser.add_argument(
        "--report-md",
        type=str,
        default=None,
        help="输出 Markdown 质量报告（传入文件路径或 '-' 输出到 stdout）",
    )
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=None,
        help="质量门禁：coverage_rate 必须大于等于该阈值",
    )
    parser.add_argument(
        "--max-noise",
        type=float,
        default=None,
        help="质量门禁：noise_rate 必须小于等于该阈值",
    )
    parser.add_argument(
        "--max-conflict",
        type=float,
        default=None,
        help="质量门禁：conflict_rate 必须小于等于该阈值",
    )
    parser.add_argument(
        "--fail-on-gate",
        action="store_true",
        help="若质量门禁失败则返回非零退出码",
    )
    return parser.parse_args()


def _write_report(path: str, content: str) -> None:
    if path == "-":
        print("\n" + content)
        return
    Path(path).write_text(content, encoding="utf-8")


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
    gate_result = report.evaluate_quality_gate(
        min_coverage=args.min_coverage,
        max_noise=args.max_noise,
        max_conflict=args.max_conflict,
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
    print(f"引用总数: {report.total_references}")
    print(f"解析成功: {report.resolved_references}")
    print(f"无效引用: {report.invalid_references}")
    print(f"未命中目标: {report.unresolved_references}")
    print(f"冲突关系: {report.conflicted_relations}")
    print(f"覆盖率: {report.coverage_rate:.4f}")
    print(f"噪声率: {report.noise_rate:.4f}")
    print(f"冲突率: {report.conflict_rate:.4f}")
    print(f"缺失文件: {len(report.missing_files)}")
    print(f"跳过引用: {len(report.skipped_references)}")

    if gate_result.get("configured"):
        print("\n质量门禁:")
        print(f"  - passed: {gate_result['passed']}")
        for item in gate_result["checks"]:
            print(
                "  - "
                f"{item['name']} {item['operator']} {item['threshold']} "
                f"(actual={item['actual']}, passed={item['passed']})"
            )

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

    report_payload = report.to_dict(include_definitions=True)
    if args.report_json:
        _write_report(
            args.report_json,
            json.dumps(
                report_payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
        )
    if args.report_yaml:
        _write_report(
            args.report_yaml,
            yaml.safe_dump(
                report_payload,
                allow_unicode=True,
                sort_keys=False,
            ),
        )
    if args.report_md:
        _write_report(args.report_md, report.to_markdown())

    if args.fail_on_gate and gate_result.get("configured") and not gate_result.get(
        "passed"
    ):
        print("\n质量门禁失败：已按 `--fail-on-gate` 返回退出码 2。")
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
