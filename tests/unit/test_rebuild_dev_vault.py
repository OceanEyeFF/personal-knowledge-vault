"""离线测试：scripts/rebuild-dev-vault.py 开发专用轻量重建入口。

覆盖:
- 临时根隔离:重建产物全部落在隔离根内，绝不触碰仓库 .data
- 幂等:重复执行不破坏已有重建，条目数稳定
- 危险目标拒绝:.data / 仓库外 / 文件系统根 / 主目录 / 链接绕过
- 结果契约:--json 输出结构与退出码

所有子进程重建均使用 pytest tmp_path（--allow-outside-repo 显式开启），
或在仓库 .data-test 下创建临时子目录；不读取、不写入生产 .data。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPO_DATA_DIR = PROJECT_ROOT / ".data"


def _load_script_module():
    """按路径加载重建脚本（不产生副作用，仅定义函数与常量）。"""
    spec = importlib.util.spec_from_file_location(
        "rebuild_dev_vault",
        PROJECT_ROOT / "scripts" / "rebuild-dev-vault.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_script(args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "scripts/rebuild-dev-vault.py", *args]
    return subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _parse_json_output(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stdout.strip(), f"空 stdout: {result.stderr}"
    return json.loads(result.stdout)


# ============================================================
# 危险目标拒绝（进程内快速验证）
# ============================================================

class TestRootRejection:
    def test_default_root_is_dedicated_data_test_dir(self) -> None:
        module = _load_script_module()
        root, in_repo = module.resolve_rebuild_root(module.DEFAULT_ROOT_REL)
        assert in_repo
        assert root == (PROJECT_ROOT / ".data-test" / "rebuild-dev").resolve()
        # 默认根绝不指向 .data
        assert ".data" not in [root.name, root.parent.name]

    def test_reject_production_data_dir(self) -> None:
        module = _load_script_module()
        for bad in (".data", ".data/foo", ".data-backup", "backups"):
            with pytest.raises(module.RootRejectedError):
                module.resolve_rebuild_root(bad)

    def test_reject_test_root_itself(self) -> None:
        module = _load_script_module()
        with pytest.raises(module.RootRejectedError):
            module.resolve_rebuild_root(".data-test")

    def test_reject_repo_paths_outside_test_root(self) -> None:
        module = _load_script_module()
        for bad in ("src", "config", "scripts", "."):
            with pytest.raises(module.RootRejectedError):
                module.resolve_rebuild_root(bad)

    def test_reject_filesystem_root_and_home(self) -> None:
        module = _load_script_module()
        anchor = Path.cwd().anchor
        with pytest.raises(module.RootRejectedError):
            module.resolve_rebuild_root(anchor)
        with pytest.raises(module.RootRejectedError):
            module.resolve_rebuild_root(str(Path.home()))

    def test_reject_outside_repo_without_explicit_opt_in(
        self, tmp_path: Path
    ) -> None:
        module = _load_script_module()
        with pytest.raises(module.RootRejectedError):
            module.resolve_rebuild_root(str(tmp_path))
        # 显式开启后允许（CI/测试临时根），但仍拒绝 .data 名称
        root, in_repo = module.resolve_rebuild_root(
            str(tmp_path), allow_outside_repo=True
        )
        assert not in_repo
        assert root == tmp_path.resolve()
        with pytest.raises(module.RootRejectedError):
            module.resolve_rebuild_root(
                str(tmp_path / ".data"), allow_outside_repo=True
            )

    def test_reject_symlink_under_test_root(self, tmp_path: Path) -> None:
        module = _load_script_module()
        # 目标位于系统临时目录；链接建在仓库 .data-test 下（Git 已忽略）。
        link_dir = PROJECT_ROOT / ".data-test" / f"rebuild-link-{os.getpid()}"
        link = link_dir / "evil"
        link_dir.mkdir(parents=True, exist_ok=True)
        target = tmp_path / "real-target"
        target.mkdir()
        try:
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                # Windows 无管理员权限时改用目录联接（mklink /J 通常无需提权）。
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            with pytest.raises(module.RootRejectedError):
                module.resolve_rebuild_root(str(link))
        finally:
            if link.is_symlink() or link.exists():
                subprocess.run(["cmd", "/c", "rmdir", str(link)], check=False)
            subprocess.run(["cmd", "/c", "rmdir", str(link_dir)], check=False)

    def test_reject_hardlinked_db_file_under_root(self, tmp_path: Path) -> None:
        module = _load_script_module()
        outer = PROJECT_ROOT / ".data-test" / f"rebuild-hardlink-{os.getpid()}"
        outer.mkdir(parents=True, exist_ok=True)
        probe = outer / "probe.txt"
        probe.write_text("x", encoding="utf-8")
        try:
            try:
                os.link(probe, outer / "probe2.txt")
            except OSError:
                pytest.skip("当前文件系统不支持硬链接")
            with pytest.raises(module.RootRejectedError):
                module.resolve_rebuild_root(str(outer / "probe2.txt"))
        finally:
            (outer / "probe2.txt").unlink(missing_ok=True)
            probe.unlink(missing_ok=True)
            outer.rmdir()


# ============================================================
# 端到端流程（子进程，临时根隔离）
# ============================================================

def _snapshot_data_dir() -> tuple[bool, int | None]:
    """记录仓库 .data 目录状态，用于验证重建不触碰生产数据。"""
    if not REPO_DATA_DIR.exists():
        return False, None
    return True, REPO_DATA_DIR.stat().st_mtime_ns


def test_rebuild_creates_isolated_root(tmp_path: Path) -> None:
    root = tmp_path / "vault-rebuild"
    data_before = _snapshot_data_dir()
    result = _run_script(
        [
            "--root",
            str(root),
            "--seed",
            "20260731",
            "--count",
            "3",
            "--allow-outside-repo",
            "--json",
        ]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = _parse_json_output(result)
    assert report["ok"]
    assert report["phase"] == "rebuilt"
    assert report["seeded"] == 3
    assert report["stats"]["total_entries"] == 3
    assert report["schema_version"] == "1.2.3"
    assert report["health"]["healthy"]

    db_path = root / "db" / "knowledge_vault.db"
    assert db_path.exists()
    assert (root / "vault").is_dir()
    markdown_files = list((root / "vault").rglob("*.md"))
    assert len(markdown_files) == 3
    # 仓库 .data 目录状态必须保持不变（不存在，或 mtime 不变）
    assert _snapshot_data_dir() == data_before, "重建不得触碰仓库 .data"


def test_rebuild_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "vault-rebuild"
    first = _run_script(
        [
            "--root",
            str(root),
            "--count",
            "3",
            "--allow-outside-repo",
            "--json",
        ]
    )
    assert first.returncode == 0, first.stdout + first.stderr
    db_path = root / "db" / "knowledge_vault.db"
    before_mtime = db_path.stat().st_mtime_ns

    second = _run_script(
        [
            "--root",
            str(root),
            "--count",
            "3",
            "--allow-outside-repo",
            "--json",
        ]
    )
    assert second.returncode == 0, second.stdout + second.stderr
    second_report = _parse_json_output(second)
    assert second_report["phase"] == "up_to_date"
    assert second_report["ok"]
    assert second_report["stats"]["total_entries"] == 3
    # 幂等路径不做任何写入
    assert db_path.stat().st_mtime_ns == before_mtime


def test_rebuild_force_rebuilds_with_new_count(tmp_path: Path) -> None:
    root = tmp_path / "vault-rebuild"
    first = _run_script(
        [
            "--root",
            str(root),
            "--count",
            "2",
            "--allow-outside-repo",
            "--json",
        ]
    )
    assert first.returncode == 0, first.stdout + first.stderr

    second = _run_script(
        [
            "--root",
            str(root),
            "--count",
            "5",
            "--force",
            "--allow-outside-repo",
            "--json",
        ]
    )
    assert second.returncode == 0, second.stdout + second.stderr
    report = _parse_json_output(second)
    assert report["phase"] == "rebuilt"
    assert report["seeded"] == 5
    assert report["stats"]["total_entries"] == 5
    assert report["migrations_applied"] == 8
    markdown_files = list((root / "vault").rglob("*.md"))
    assert len(markdown_files) == 5


def test_rebuild_seed_is_deterministic(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    args = ["--count", "4", "--seed", "42", "--allow-outside-repo", "--json"]
    result_a = _run_script(["--root", str(root_a), *args])
    result_b = _run_script(["--root", str(root_b), *args])
    assert result_a.returncode == 0, result_a.stdout + result_a.stderr
    assert result_b.returncode == 0, result_b.stdout + result_b.stderr

    titles_a = sorted(
        p.read_text(encoding="utf-8")
        .split("title:", 1)[1]
        .splitlines()[0]
        .strip()
        .strip('"')
        for p in (root_a / "vault").rglob("*.md")
    )
    titles_b = sorted(
        p.read_text(encoding="utf-8")
        .split("title:", 1)[1]
        .splitlines()[0]
        .strip()
        .strip('"')
        for p in (root_b / "vault").rglob("*.md")
    )
    assert titles_a == titles_b
    assert len(titles_a) == 4


def test_check_only_is_read_only(tmp_path: Path) -> None:
    root = tmp_path / "vault-rebuild"
    # 对空根执行 --check-only：绝不创建任何目录
    result = _run_script(
        ["--root", str(root), "--check-only", "--allow-outside-repo", "--json"]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = _parse_json_output(result)
    assert report["phase"] == "checked"
    assert report["ok"]
    assert report["schema_version"] == "0.0.0"
    assert not root.exists(), "--check-only 不得创建任何目录"

    # 重建后 check-only 应报告已迁移状态
    rebuilt = _run_script(
        [
            "--root",
            str(root),
            "--count",
            "2",
            "--allow-outside-repo",
            "--json",
        ]
    )
    assert rebuilt.returncode == 0, rebuilt.stdout + rebuilt.stderr
    checked = _run_script(
        ["--root", str(root), "--check-only", "--allow-outside-repo", "--json"]
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    check_report = _parse_json_output(checked)
    assert check_report["schema_version"] == "1.2.3"
    assert check_report["stats"]["total_entries"] == 2


def test_result_contract_fields(tmp_path: Path) -> None:
    root = tmp_path / "vault-rebuild"
    result = _run_script(
        [
            "--root",
            str(root),
            "--count",
            "1",
            "--allow-outside-repo",
            "--json",
        ]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = _parse_json_output(result)
    for key in ("ok", "phase", "root", "db_path", "schema_version", "health", "stats"):
        assert key in report, f"缺少契约字段: {key}"
    assert report["health"]["healthy"]
    assert report["stats"]["total_entries"] == 1
    # stdout 必须是纯 JSON（无 jieba 等噪音）
    assert result.stdout.strip().startswith("{")


def test_dangerous_root_rejected_in_subprocess() -> None:
    result = _run_script(["--root", ".data", "--json"])
    assert result.returncode == 2
    report = _parse_json_output(result)
    assert not report["ok"]
    assert "生产数据" in report["error"]

    result = _run_script(["--root", "src", "--json"])
    assert result.returncode == 2
    report = _parse_json_output(result)
    assert not report["ok"]

    outside = _run_script(["--root", str(Path.home() / "forbidden-rebuild")])
    assert outside.returncode == 2
    assert "仓库外" in outside.stderr
