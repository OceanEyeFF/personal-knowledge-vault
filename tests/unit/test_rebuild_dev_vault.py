"""离线测试：scripts/rebuild-dev-vault.py 开发专用轻量重建入口。

覆盖:
- 受控根隔离:重建产物全部落在仓库 .data-test 下的受控临时子目录内
- 生产路径契约:脚本不构造、不访问仓库 .data 相关路径（字符串级 + 监控证明）
- fail-closed:非空但不完整/未知的 root 不带 --force 必须拒绝（exit 1，
  JSON 带 error/phase），不写入、不清理；只有本脚本完整生成的 root 才可
  报告 up_to_date / exit 0
- manifest 契约:版本化 rebuild-manifest.json 识别本脚本产物；缺失/损坏/
  字段不一致/pending migrations/条目数漂移均拒绝
- 幂等、--force 重建、--no-seed 可验证、--check-only 只读且对缺失 DB 失败
- 危险目标拒绝:纯字符串拒绝（不调用 resolve/stat/exists/lstat）；仓库外一律拒绝

安全约定:
- 重建子进程均使用 .data-test 下受控生成的临时子目录（测试后清理），
  外部 tmp_path 一律只用于验证“必须被拒绝”。
- 测试不读取、不 stat、不枚举仓库 .data；对其仅做字符串级比较。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 仅字符串构造，绝不用于任何文件系统访问。
_REPO_DATA_PREFIX = os.path.normcase(str(PROJECT_ROOT / ".data"))


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
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"stdout 不是合法 JSON: {result.stdout[:200]}") from exc


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _assert_not_production_path(path: str) -> None:
    """字符串级断言：路径不落在仓库 .data 目录下（不访问文件系统）。"""
    normalized = os.path.normcase(os.path.normpath(path))
    prefix = os.path.normcase(os.path.normpath(_REPO_DATA_PREFIX))
    if normalized == prefix or normalized.startswith(prefix + os.sep):
        raise AssertionError(f"路径指向生产数据目录: {path}")


@pytest.fixture
def managed_root() -> Iterator[Path]:
    """仓库 .data-test 下的受控临时根；测试后清理（先校验链接）。"""
    root = PROJECT_ROOT / ".data-test" / f"rebuild-test-{uuid.uuid4().hex[:12]}"
    yield root
    if root.exists():
        for child in [root, *root.rglob("*")]:
            try:
                st = os.lstat(child)
            except OSError:
                continue
            is_reparse = bool((st.st_file_attributes or 0) & 0x400)
            if is_reparse or os.path.islink(child):
                raise AssertionError(f"测试根包含链接，拒绝自动清理: {child}")
        try:
            shutil.rmtree(root)
        except OSError as exc:
            raise AssertionError(f"测试根清理失败: {root} - {exc}") from exc


def _read_manifest(root: Path) -> dict:
    manifest_path = root / "rebuild-manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AssertionError(f"manifest 读取失败: {manifest_path}") from exc
    assert isinstance(payload, dict)
    return payload


def _make_sentinel_root(root: Path) -> Path:
    """创建只含 sentinel.txt 的非空 root（无 db、无 manifest）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "sentinel.txt").write_text("junk", encoding="utf-8")
    return root


# ============================================================
# 危险目标拒绝（纯字符串，不访问文件系统）
# ============================================================

class TestRootRejection:
    def test_default_root_is_dedicated_data_test_dir(self) -> None:
        module = _load_script_module()
        root = module.resolve_rebuild_root(module.DEFAULT_ROOT_REL)
        assert root == (PROJECT_ROOT / ".data-test" / "rebuild-dev").resolve()
        # 字符串级：默认根名称不得是 .data
        assert root.name != ".data"

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

    def test_reject_outside_repo_always(self, tmp_path: Path) -> None:
        """仓库外路径一律拒绝（无任何旁路开关）。"""
        module = _load_script_module()
        with pytest.raises(module.RootRejectedError):
            module.resolve_rebuild_root(str(tmp_path))
        with pytest.raises(module.RootRejectedError):
            module.resolve_rebuild_root(str(tmp_path / "sub"))

    def test_reject_nested_test_root_and_data_components(self) -> None:
        module = _load_script_module()
        with pytest.raises(module.RootRejectedError):
            module.resolve_rebuild_root(".data-test/.data-test/x")
        with pytest.raises(module.RootRejectedError):
            module.resolve_rebuild_root(".data-test/x/.data")

    def test_rejection_never_touches_filesystem(self, monkeypatch) -> None:
        """危险路径拒绝必须纯字符串判断，不得调用 resolve/stat/exists/lstat。"""
        module = _load_script_module()

        def boom(*args, **kwargs):
            raise AssertionError("危险路径拒绝不得访问文件系统")

        monkeypatch.setattr(Path, "resolve", boom)
        monkeypatch.setattr(Path, "exists", boom)
        monkeypatch.setattr(Path, "stat", boom)
        monkeypatch.setattr(os, "lstat", boom)
        for bad in (
            ".data",
            ".data-test",
            "src",
            ".",
            Path.cwd().anchor,
            str(Path.home()),
        ):
            with pytest.raises(module.RootRejectedError):
                module.resolve_rebuild_root(bad)

    def test_reject_symlink_under_test_root(self, tmp_path: Path) -> None:
        module = _load_script_module()
        # 链接建在仓库 .data-test 下（Git 已忽略），目标位于系统临时目录。
        link_dir = PROJECT_ROOT / ".data-test" / f"rebuild-link-{uuid.uuid4().hex[:8]}"
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

    def test_reject_hardlinked_file_under_root(self) -> None:
        module = _load_script_module()
        outer = PROJECT_ROOT / ".data-test" / f"rebuild-hardlink-{uuid.uuid4().hex[:8]}"
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
# fail-closed 契约（sentinel-only / 缺失 DB / 无效 manifest）
# ============================================================

def test_sentinel_only_root_fails_closed(managed_root: Path) -> None:
    root = _make_sentinel_root(managed_root)
    result = _run_script(["--root", str(root), "--json"])
    assert result.returncode == 1, result.stdout + result.stderr
    report = _parse_json_output(result)
    assert not report["ok"]
    assert report["phase"] == "invalid"
    assert "manifest" in report["error"] or any(
        "manifest" in issue for issue in report["issues"]
    )
    # 未写入、未清理：sentinel 仍在，db/vault 未创建
    assert (root / "sentinel.txt").is_file()
    assert not (root / "db").exists()
    assert not (root / "vault").exists()
    assert sorted(p.name for p in root.iterdir()) == ["sentinel.txt"]


def test_missing_db_check_only_fails(managed_root: Path) -> None:
    root = _make_sentinel_root(managed_root)
    result = _run_script(["--root", str(root), "--check-only", "--json"])
    assert result.returncode == 1, result.stdout + result.stderr
    report = _parse_json_output(result)
    assert not report["ok"]
    assert report["phase"] == "invalid"
    assert any("数据库" in issue for issue in report["issues"])
    # check-only 绝不创建目录或数据库
    assert not (root / "db").exists()
    assert not (root / "vault").exists()
    assert sorted(p.name for p in root.iterdir()) == ["sentinel.txt"]


def test_check_only_on_missing_root_fails_without_creating(
    managed_root: Path,
) -> None:
    result = _run_script(["--root", str(managed_root), "--check-only", "--json"])
    assert result.returncode == 1, result.stdout + result.stderr
    report = _parse_json_output(result)
    assert not report["ok"]
    assert report["phase"] == "invalid"
    assert not managed_root.exists(), "--check-only 不得创建目标目录"


def test_force_rebuilds_incomplete_root(managed_root: Path) -> None:
    root = _make_sentinel_root(managed_root)
    result = _run_script(
        ["--root", str(root), "--force", "--count", "2", "--json"]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = _parse_json_output(result)
    assert report["ok"]
    assert report["phase"] == "rebuilt"
    assert report["seeded"] == 2
    assert report["stats"]["total_entries"] == 2
    assert not (root / "sentinel.txt").exists()
    manifest = _read_manifest(root)
    assert manifest["seeded"]
    assert manifest["seed_count"] == 2
    assert manifest["schema_version"] == "1.2.3"


def test_invalid_manifest_fails_closed(managed_root: Path) -> None:
    root = managed_root
    first = _run_script(["--root", str(root), "--count", "2", "--json"])
    assert first.returncode == 0, first.stdout + first.stderr

    # 损坏的 manifest（非法 JSON）→ 拒绝
    (root / "rebuild-manifest.json").write_text("not json", encoding="utf-8")
    result = _run_script(["--root", str(root), "--json"])
    assert result.returncode == 1
    report = _parse_json_output(result)
    assert not report["ok"]
    assert report["phase"] == "invalid"
    assert any("manifest" in issue for issue in report["issues"])

    # 删除 manifest → 拒绝
    (root / "rebuild-manifest.json").unlink()
    result = _run_script(["--root", str(root), "--json"])
    assert result.returncode == 1
    report = _parse_json_output(result)
    assert report["phase"] == "invalid"


def test_seed_count_drift_fails_closed(managed_root: Path) -> None:
    root = managed_root
    first = _run_script(["--root", str(root), "--count", "2", "--json"])
    assert first.returncode == 0, first.stdout + first.stderr

    # 篡改 manifest 期望条目数 → 结构校验失败
    manifest_path = root / "rebuild-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssertionError(f"manifest 读取失败: {manifest_path}") from exc
    manifest["seed_count"] = 99
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    result = _run_script(["--root", str(root), "--json"])
    assert result.returncode == 1
    report = _parse_json_output(result)
    assert report["phase"] == "invalid"
    assert any("条目数" in issue for issue in report["issues"])


def test_pending_migrations_not_up_to_date(managed_root: Path, monkeypatch) -> None:
    root = managed_root
    first = _run_script(["--root", str(root), "--count", "1", "--json"])
    assert first.returncode == 0, first.stdout + first.stderr

    module = _load_script_module()
    monkeypatch.setattr(
        module.MigrationManager,
        "get_pending_migrations",
        lambda self: [("9.9.9", Path("unused.sql"))],
    )
    validation = module._validate_rebuilt_root(root, root / "db" / "knowledge_vault.db")
    assert not validation["ok"]
    assert any("待执行迁移" in issue for issue in validation["issues"])


def test_no_seed_root_is_verifiable(managed_root: Path) -> None:
    root = managed_root
    first = _run_script(["--root", str(root), "--no-seed", "--json"])
    assert first.returncode == 0, first.stdout + first.stderr
    report = _parse_json_output(first)
    assert report["ok"]
    assert report["phase"] == "rebuilt"
    assert report["seeded"] == 0
    assert report["stats"]["total_entries"] == 0

    manifest = _read_manifest(root)
    assert not manifest["seeded"]
    assert manifest["seed_count"] == 0

    second = _run_script(["--root", str(root), "--json"])
    assert second.returncode == 0, second.stdout + second.stderr
    report = _parse_json_output(second)
    assert report["phase"] == "up_to_date"
    assert report["ok"]


# ============================================================
# 端到端流程（子进程，受控根位于仓库 .data-test 下）
# ============================================================

def test_rebuild_creates_isolated_root(managed_root: Path) -> None:
    result = _run_script(
        ["--root", str(managed_root), "--seed", "20260731", "--count", "3", "--json"]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = _parse_json_output(result)
    assert report["ok"]
    assert report["phase"] == "rebuilt"
    assert report["seeded"] == 3
    assert report["stats"]["total_entries"] == 3
    assert report["schema_version"] == "1.2.3"
    assert report["health"]["healthy"]

    db_path = managed_root / "db" / "knowledge_vault.db"
    assert db_path.exists()
    assert len(list((managed_root / "vault").rglob("*.md"))) == 3
    # 契约：报告中的实际路径位于受控根内，且字符串级不指向生产 .data
    assert _is_under(Path(report["root"]), managed_root.resolve())
    assert _is_under(Path(report["db_path"]), managed_root.resolve())
    _assert_not_production_path(report["root"])
    _assert_not_production_path(report["db_path"])
    # manifest 契约
    manifest = _read_manifest(managed_root)
    assert manifest["manifest_version"] == 1
    assert manifest["tool"] == "rebuild-dev-vault"
    assert manifest["seeded"]
    assert manifest["seed_count"] == 3
    assert manifest["schema_version"] == "1.2.3"


def test_rebuild_is_idempotent(managed_root: Path) -> None:
    first = _run_script(["--root", str(managed_root), "--count", "3", "--json"])
    assert first.returncode == 0, first.stdout + first.stderr
    db_path = managed_root / "db" / "knowledge_vault.db"
    before_mtime = db_path.stat().st_mtime_ns

    second = _run_script(["--root", str(managed_root), "--count", "3", "--json"])
    assert second.returncode == 0, second.stdout + second.stderr
    report = _parse_json_output(second)
    assert report["phase"] == "up_to_date"
    assert report["ok"]
    assert report["stats"]["total_entries"] == 3
    # 幂等路径不做任何写入
    assert db_path.stat().st_mtime_ns == before_mtime


def test_rebuild_force_rebuilds_with_new_count(managed_root: Path) -> None:
    first = _run_script(["--root", str(managed_root), "--count", "2", "--json"])
    assert first.returncode == 0, first.stdout + first.stderr

    second = _run_script(
        ["--root", str(managed_root), "--count", "5", "--force", "--json"]
    )
    assert second.returncode == 0, second.stdout + second.stderr
    report = _parse_json_output(second)
    assert report["phase"] == "rebuilt"
    assert report["seeded"] == 5
    assert report["stats"]["total_entries"] == 5
    assert report["migrations_applied"] == 8
    assert len(list((managed_root / "vault").rglob("*.md"))) == 5
    manifest = _read_manifest(managed_root)
    assert manifest["seed_count"] == 5


def test_rebuild_seed_is_deterministic(managed_root: Path) -> None:
    root_a = managed_root / "a"
    root_b = managed_root / "b"
    args = ["--count", "4", "--seed", "42", "--json"]
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


def test_check_only_reports_valid_root(managed_root: Path) -> None:
    rebuilt = _run_script(["--root", str(managed_root), "--count", "2", "--json"])
    assert rebuilt.returncode == 0, rebuilt.stdout + rebuilt.stderr
    checked = _run_script(
        ["--root", str(managed_root), "--check-only", "--json"]
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    report = _parse_json_output(checked)
    assert report["phase"] == "checked"
    assert report["ok"]
    assert report["schema_version"] == "1.2.3"
    assert report["stats"]["total_entries"] == 2


def test_result_contract_fields(managed_root: Path) -> None:
    result = _run_script(["--root", str(managed_root), "--count", "1", "--json"])
    assert result.returncode == 0, result.stdout + result.stderr
    report = _parse_json_output(result)
    for key in ("ok", "phase", "root", "db_path", "schema_version", "health", "stats"):
        assert key in report, f"缺少契约字段: {key}"
    assert report["health"]["healthy"]
    assert report["stats"]["total_entries"] == 1
    # stdout 必须是纯 JSON（无 jieba 等噪音）
    assert result.stdout.strip().startswith("{")


def test_resolution_never_touches_production_data_path(
    managed_root: Path, monkeypatch
) -> None:
    """监控证明：解析候选根时访问过的路径均不以仓库 .data 为前缀。"""
    module = _load_script_module()
    touched: list[str] = []
    real_resolve = Path.resolve
    real_exists = Path.exists
    real_stat = Path.stat
    real_lstat = os.lstat

    def spy_resolve(self, *args, **kwargs):
        touched.append(str(self))
        return real_resolve(self, *args, **kwargs)

    def spy_exists(self, *args, **kwargs):
        touched.append(str(self))
        return real_exists(self, *args, **kwargs)

    def spy_stat(self, *args, **kwargs):
        touched.append(str(self))
        return real_stat(self, *args, **kwargs)

    def spy_lstat(path):
        touched.append(str(path))
        return real_lstat(path)

    monkeypatch.setattr(Path, "resolve", spy_resolve)
    monkeypatch.setattr(Path, "exists", spy_exists)
    monkeypatch.setattr(Path, "stat", spy_stat)
    monkeypatch.setattr(os, "lstat", spy_lstat)

    root = module.resolve_rebuild_root(str(managed_root))
    assert root == managed_root.resolve()
    assert touched, "候选根解析应发生文件系统访问"
    for touched_path in touched:
        _assert_not_production_path(touched_path)


def test_dangerous_root_rejected_in_subprocess(tmp_path: Path) -> None:
    # .data → exit 2（纯字符串拒绝）
    result = _run_script(["--root", ".data", "--json"])
    assert result.returncode == 2
    report = _parse_json_output(result)
    assert not report["ok"]
    assert "生产数据" in report["error"]

    # 仓库内非 .data-test 路径 → exit 2
    result = _run_script(["--root", "src", "--json"])
    assert result.returncode == 2

    # 仓库外（外部 tmp_path）→ exit 2
    result = _run_script(["--root", str(tmp_path)])
    assert result.returncode == 2
    assert "仓库外" in result.stderr

    # 已移除的旁路开关必须被 CLI 拒绝（未知参数 → exit 2）
    result = _run_script(["--root", str(tmp_path), "--allow-outside-repo"])
    assert result.returncode == 2
