"""W1 contracts for immutable resources and contained user data."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.utils.config import Config


@pytest.fixture
def resource_root(tmp_path: Path) -> Path:
    root = tmp_path / "resources"
    (root / "config" / "workflows").mkdir(parents=True)
    (root / "scripts" / "migrations").mkdir(parents=True)
    (root / "src" / "ai" / "prompts").mkdir(parents=True)
    (root / "config" / "config.yaml").write_text("{}\n", encoding="utf-8")
    (root / "config" / "custom_dict.txt").write_text("PKV\n", encoding="utf-8")
    (root / "config" / "workflows" / "archive-url.yaml").write_text(
        "name: archive-url\n", encoding="utf-8"
    )
    (root / "config" / "workflows" / "archive-text.yaml").write_text(
        "name: archive-text\n", encoding="utf-8"
    )
    (root / "src" / "ai" / "prompts" / "summarize.txt").write_text(
        "{content}\n", encoding="utf-8"
    )
    (root / "src" / "ai" / "prompts" / "extract_tags.txt").write_text(
        "{content}\n", encoding="utf-8"
    )
    return root


def test_resolve_is_pure_and_derives_every_mutable_path_from_one_root(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "user-data"

    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        user_data_root=data_root,
        environment={},
    )

    assert not data_root.exists()
    assert layout.local_config_path == data_root / "config" / "local.yaml"
    assert layout.db_path == data_root / "db" / "knowledge_vault.db"
    assert layout.vault_dir == data_root / "vault"
    assert layout.vector_index_dir == data_root / "vectors"
    assert layout.log_dir == data_root / "logs"
    assert layout.tmp_dir == data_root / "tmp"
    assert layout.backup_dir == data_root / "backups"


def test_profile_config_and_default_data_root_are_pure_and_injectable(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    fake_user_profile = tmp_path / "fake-user"

    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        environment={"USERPROFILE": str(fake_user_profile)},
    )

    expected_profile = fake_user_profile / ".pkv"
    assert layout.profile_root == expected_profile
    assert layout.user_config_path == expected_profile / "config.yaml"
    assert layout.user_data_root == expected_profile / "data"
    assert layout.runtime_config_path == expected_profile / "data" / "config" / "local.yaml"
    # RuntimeLayout.local_config_path remains the data-root snapshot alias for
    # existing runtime internals; it is never the editable user config path.
    assert layout.local_config_path == layout.runtime_config_path
    assert not expected_profile.exists()


def test_product_environment_ignores_legacy_data_and_child_overrides(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    fake_user_profile = tmp_path / "fake-user"
    legacy_root = tmp_path / "legacy-root"
    legacy_db = tmp_path / "outside" / "legacy.db"

    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        environment={
            "USERPROFILE": str(fake_user_profile),
            "DATA_DIR": str(legacy_root),
            "DB_PATH": str(legacy_db),
            "VAULT_DIR": str(tmp_path / "outside-vault"),
        },
    )

    assert layout.user_data_root == fake_user_profile / ".pkv" / "data"
    assert layout.db_path == layout.user_data_root / "db" / "knowledge_vault.db"
    assert layout.vault_dir == layout.user_data_root / "vault"
    assert not legacy_root.exists()


def test_offline_legacy_environment_remains_contained_test_injection(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "isolated-root"
    custom_db = data_root / "custom" / "knowledge.db"

    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        environment={
            "PKV_TEST_OFFLINE": "1",
            "PKV_DATA_ROOT": str(tmp_path / "formal-root"),
            "DATA_DIR": str(data_root),
            "DB_PATH": str(custom_db),
        },
    )

    assert layout.user_data_root == data_root
    assert layout.db_path == custom_db
    assert layout.vault_dir == data_root / "vault"
    assert not data_root.exists()


def test_explicit_layout_reload_ignores_ambient_root_but_honors_injected_env(
    resource_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_root = tmp_path / "captured-root"
    ambient_root = tmp_path / "ambient-root"
    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        user_data_root=captured_root,
        environment={},
    )
    monkeypatch.setenv("PKV_DATA_ROOT", str(ambient_root))

    ambient_config = Config(layout=layout)
    assert ambient_config.reload_snapshot().data_root == captured_root

    explicit_environment_config = Config(
        layout=layout,
        environment={"PKV_DATA_ROOT": str(ambient_root)},
    )
    with pytest.raises(PKVRuntimeError) as captured:
        explicit_environment_config.reload_snapshot()

    assert captured.value.code is ErrorCode.DATA_ROOT_SWITCH_REQUIRED
    assert not captured_root.exists()
    assert not ambient_root.exists()


def test_offline_resolution_never_reads_host_home_without_injected_root(
    resource_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_home(cls):
        raise AssertionError("offline resolution must not read Path.home()")

    monkeypatch.setattr(Path, "home", classmethod(fail_home))

    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        environment={"PKV_TEST_OFFLINE": "1"},
    )

    assert layout.profile_root == Path.cwd() / ".data-test" / "offline-profile"
    assert layout.user_data_root == layout.profile_root / "data"


def test_ensure_user_directories_creates_only_declared_tree(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "user-data"
    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        user_data_root=data_root,
        environment={},
    )

    layout.ensure_user_directories()

    for directory in (
        layout.local_config_path.parent,
        layout.db_path.parent,
        layout.vault_dir,
        layout.vector_index_dir,
        layout.log_dir,
        layout.tmp_dir,
        layout.backup_dir,
        layout.runtime_state_dir,
    ):
        assert directory.is_dir()
    assert not layout.db_path.exists()
    assert not layout.local_config_path.exists()


def test_child_override_outside_data_root_fails_before_write(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "user-data"
    outside = tmp_path / "outside" / "vault.db"

    with pytest.raises(PKVRuntimeError) as captured:
        RuntimeLayout.resolve(
            resources_root=resource_root,
            user_data_root=data_root,
            environment={
                "PKV_TEST_OFFLINE": "1",
                "DB_PATH": str(outside),
            },
        )

    assert captured.value.code is ErrorCode.DATA_ROOT_UNSAFE
    assert not data_root.exists()
    assert not outside.parent.exists()


@pytest.mark.parametrize(
    "unsafe_root",
    [r"\\server\share\pkv", r"\\?\C:\pkv", "//server/share/pkv"],
)
def test_remote_and_device_data_roots_fail_closed(
    resource_root: Path,
    unsafe_root: str,
) -> None:
    with pytest.raises(PKVRuntimeError) as captured:
        RuntimeLayout.resolve(
            resources_root=resource_root,
            user_data_root=Path(unsafe_root),
            environment={},
        )

    assert captured.value.code is ErrorCode.DATA_ROOT_UNSAFE


def test_bundled_resource_validation_is_allowlisted(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        user_data_root=tmp_path / "data",
        environment={},
    )

    layout.validate_bundled_resources()
    layout.custom_dict_path.unlink()

    with pytest.raises(PKVRuntimeError) as captured:
        layout.validate_bundled_resources()
    assert captured.value.code is ErrorCode.RESOURCE_MISSING


def test_bundled_resource_validation_rejects_wrong_resource_type(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        user_data_root=tmp_path / "data",
        environment={},
    )
    prompt_path = layout.prompts_dir / "summarize.txt"
    prompt_path.unlink()
    prompt_path.mkdir()

    with pytest.raises(PKVRuntimeError) as captured:
        layout.validate_bundled_resources()

    assert captured.value.code is ErrorCode.RESOURCE_NOT_READABLE


def test_workflow_name_cannot_escape_bundled_workflow_directory(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        user_data_root=tmp_path / "data",
        environment={},
    )
    config = Config(layout=layout)

    with pytest.raises(ValueError, match="workflow_name"):
        config.get_workflow_config("../../config")


def test_workflow_config_does_not_fall_back_to_legacy_base_config(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    (resource_root / "config" / "config.yaml").write_text(
        "workflows:\n  search:\n    steps: [legacy]\n",
        encoding="utf-8",
    )
    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        user_data_root=tmp_path / "data",
        environment={},
    )
    config = Config(layout=layout)

    with pytest.raises(FileNotFoundError, match="search"):
        config.get_workflow_config("search")


def test_workflow_config_alias_resolves_real_versioned_yaml(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        user_data_root=tmp_path / "data",
        environment={},
    )
    config = Config(layout=layout)

    loaded = config.get_workflow_config("archive_url")

    assert loaded["name"] == "archive-url"


def test_workflow_yaml_rejects_nested_duplicate_keys(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    (resource_root / "config" / "workflows" / "archive-url.yaml").write_text(
        """
name: archive-url
version: 1
steps:
  - id: fetch
    type: fetch_content
    on_error: fail
    on_error: continue
""".lstrip(),
        encoding="utf-8",
    )
    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        user_data_root=tmp_path / "data",
        environment={},
    )
    config = Config(layout=layout)

    with pytest.raises(ValueError, match="工作流配置文件 YAML 格式错误"):
        config.get_workflow_config("archive-url")


def test_existing_link_as_data_root_is_rejected(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-data"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        user_data_root=link,
        environment={},
    )
    with pytest.raises(PKVRuntimeError) as captured:
        layout.ensure_user_directories()

    assert captured.value.code is ErrorCode.DATA_ROOT_UNSAFE
    assert os.path.islink(link)


def test_redirected_parent_is_rejected_before_creating_data_root(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-parent"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        user_data_root=linked_parent / "user-data",
        environment={},
    )
    with pytest.raises(PKVRuntimeError) as captured:
        layout.ensure_user_directories()

    assert captured.value.code is ErrorCode.DATA_ROOT_UNSAFE
    assert not (outside / "user-data").exists()


# ============================================================
# 统一可写叶子合同：链接/硬链接叶子、父目录替换、原子发布
# ============================================================


def _leaf_layout(resource_root: Path, tmp_path: Path) -> RuntimeLayout:
    layout = RuntimeLayout.resolve(
        resources_root=resource_root,
        user_data_root=tmp_path / "data",
        environment={},
    )
    layout.ensure_user_directories()
    return layout


def _make_hardlink(source: Path, target: Path) -> None:
    """Windows/POSIX 都可用的硬链接故障注入。"""
    os.link(source, target)


def test_writable_user_path_rejects_hardlinked_leaf(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    """硬链接叶子在任何读取/写入前拒绝（不依赖 symlink 权限，可移植）。"""
    layout = _leaf_layout(resource_root, tmp_path)
    target = layout.tmp_dir / "leaf.bin"
    target.write_bytes(b"original")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"attacker")
    target.unlink()
    _make_hardlink(outside, target)

    try:
        with pytest.raises(PKVRuntimeError) as captured:
            layout.writable_user_path(target, label="测试文件")

        assert captured.value.code is ErrorCode.DATA_ROOT_UNSAFE
        assert outside.read_bytes() == b"attacker"
    finally:
        # Do not leave the deliberately injected hard-link directory entry for
        # the runner cleanup audit to remove recursively.  This only unlinks
        # the test-owned name; ``outside`` remains intact.
        target.unlink(missing_ok=True)


def test_open_user_file_rejects_hardlinked_leaf(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    """open 前的链接检查拒绝硬链接叶子，且外部文件不被打开改写。"""
    layout = _leaf_layout(resource_root, tmp_path)
    target = layout.tmp_dir / "leaf.bin"
    target.write_bytes(b"original")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"attacker")
    target.unlink()
    _make_hardlink(outside, target)

    try:
        with pytest.raises(PKVRuntimeError) as captured:
            with layout.open_user_file(target, "wb", label="测试文件") as f:
                f.write(b"overwrite")

        assert captured.value.code is ErrorCode.DATA_ROOT_UNSAFE
        assert outside.read_bytes() == b"attacker"
    finally:
        target.unlink(missing_ok=True)


def test_open_user_file_write_race_rejects_before_truncating_hardlink(
    resource_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hardlink swapped in at open time must be rejected before O_TRUNC."""

    layout = _leaf_layout(resource_root, tmp_path)
    target = layout.tmp_dir / "leaf.bin"
    target.write_bytes(b"original")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"attacker")
    real_open = os.open
    raced = {"done": False}

    def racing_open(path, flags, mode=0o777):
        if Path(path) == target and not raced["done"]:
            raced["done"] = True
            target.unlink()
            _make_hardlink(outside, target)
        return real_open(path, flags, mode)

    monkeypatch.setattr("src.runtime.layout.os.open", racing_open)

    try:
        with pytest.raises(PKVRuntimeError) as captured:
            with layout.open_user_file(target, "wb", label="测试文件") as stream:
                stream.write(b"overwrite")

        assert captured.value.code is ErrorCode.DATA_ROOT_UNSAFE
        assert outside.read_bytes() == b"attacker"
    finally:
        target.unlink(missing_ok=True)


def test_open_user_file_rejects_symlink_leaf(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    """symlink 叶子在 O_NOFOLLOW/身份核验之前即被拒绝。"""
    layout = _leaf_layout(resource_root, tmp_path)
    target = layout.tmp_dir / "leaf.bin"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"attacker")
    try:
        target.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(PKVRuntimeError) as captured:
        with layout.open_user_file(target, "rb", label="测试文件") as f:
            f.read()

    assert captured.value.code is ErrorCode.DATA_ROOT_UNSAFE
    assert outside.read_bytes() == b"attacker"


def test_open_user_file_identity_check_detects_replaced_leaf(
    resource_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """open 后路径被替换为另一文件时，fstat/lstat 身份核验必须拒绝。"""
    layout = _leaf_layout(resource_root, tmp_path)
    target = layout.tmp_dir / "leaf.bin"
    target.write_bytes(b"original")
    real_lstat = os.lstat
    swapped = tmp_path / "swapped.bin"

    def swapped_lstat(path):
        info = real_lstat(path)
        if os.path.abspath(os.fspath(path)) == os.path.abspath(os.fspath(target)):
            swapped.write_bytes(b"other")
            return real_lstat(swapped)
        return info

    monkeypatch.setattr("src.runtime.layout.os.lstat", swapped_lstat)

    with pytest.raises(PKVRuntimeError) as captured:
        with layout.open_user_file(target, "rb", label="测试文件") as f:
            f.read()

    assert captured.value.code is ErrorCode.DATA_ROOT_UNSAFE


def test_leaf_parent_replaced_by_link_is_rejected(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    """叶子的父目录被替换为链接时，写入前必须拒绝且根外无产物。"""
    layout = _leaf_layout(resource_root, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.rmtree(layout.log_dir)
    try:
        layout.log_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    with pytest.raises(PKVRuntimeError) as captured:
        layout.writable_user_path(
            layout.log_dir / "pkv.log",
            label="日志文件",
        )

    assert captured.value.code is ErrorCode.DATA_ROOT_UNSAFE
    assert not (outside / "pkv.log").exists()


def test_atomic_publish_publishes_complete_temp(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    """data 与 writer 两种方式都先写完整临时文件再原子发布。"""
    layout = _leaf_layout(resource_root, tmp_path)
    target = layout.tmp_dir / "leaf.bin"

    layout.atomic_publish_user_file(
        target,
        label="测试文件",
        data=b"payload-data",
    )
    assert target.read_bytes() == b"payload-data"
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []

    target.unlink()

    def writer(temp_path: Path) -> None:
        temp_path.write_bytes(b"payload-writer")

    layout.atomic_publish_user_file(
        target,
        label="测试文件",
        writer=writer,
    )
    assert target.read_bytes() == b"payload-writer"
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_atomic_publish_rejects_hardlinked_target_before_replace(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    """writer 把目标换成硬链接时，发布前检查拒绝，根外文件不被覆盖。"""
    layout = _leaf_layout(resource_root, tmp_path)
    target = layout.tmp_dir / "leaf.bin"
    target.write_bytes(b"original")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"attacker")

    def evil_writer(temp_path: Path) -> None:
        temp_path.write_bytes(b"complete-temp-payload")
        target.unlink()
        _make_hardlink(outside, target)

    try:
        with pytest.raises(PKVRuntimeError) as captured:
            layout.atomic_publish_user_file(
                target,
                label="测试文件",
                writer=evil_writer,
            )

        assert captured.value.code is ErrorCode.DATA_ROOT_UNSAFE
        assert outside.read_bytes() == b"attacker"
        assert target.read_bytes() == b"attacker"
        assert list(target.parent.glob(f".{target.name}.*.tmp")) == []
    finally:
        target.unlink(missing_ok=True)


def test_atomic_publish_post_replace_swap_detected(
    resource_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """replace 后目标被换成硬链接时，发布后核验必须失败。"""
    layout = _leaf_layout(resource_root, tmp_path)
    target = layout.tmp_dir / "leaf.bin"
    target.write_bytes(b"original")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"attacker")
    real_replace = os.replace

    def swap_after_replace(source, destination):
        result = real_replace(source, destination)
        target.unlink()
        _make_hardlink(outside, target)
        return result

    monkeypatch.setattr(
        "src.runtime.layout.os.replace",
        swap_after_replace,
    )

    try:
        with pytest.raises(PKVRuntimeError) as captured:
            layout.atomic_publish_user_file(
                target,
                label="测试文件",
                data=b"new",
            )

        assert captured.value.code is ErrorCode.DATA_ROOT_UNSAFE
        assert outside.read_bytes() == b"attacker"
        assert list(target.parent.glob(f".{target.name}.*.tmp")) == []
    finally:
        # This test deliberately leaves ``target`` as a hard link to prove the
        # post-replace check is fail-closed.  Remove only that test-created
        # directory entry so the Windows P0 runner can retain its equally
        # fail-closed "never recursively delete a hard link" cleanup contract.
        # Unlinking a hard-link name cannot delete ``outside``.
        target.unlink(missing_ok=True)


def test_atomic_publish_failure_preserves_target_and_cleans_temp(
    resource_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """replace 失败时原目标字节不变且临时文件被清理。"""
    layout = _leaf_layout(resource_root, tmp_path)
    target = layout.tmp_dir / "leaf.bin"
    target.write_bytes(b"original")

    def fail_replace(source, destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr("src.runtime.layout.os.replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        layout.atomic_publish_user_file(
            target,
            label="测试文件",
            data=b"new",
        )

    assert target.read_bytes() == b"original"
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_atomic_publish_detects_parent_dir_swap(
    resource_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """父目录在写入期间被换成链接时，目录身份检查拒绝并清理临时文件。"""
    layout = _leaf_layout(resource_root, tmp_path)
    target = layout.tmp_dir / "leaf.bin"
    target.write_bytes(b"original")
    outside = tmp_path / "outside"
    outside.mkdir()
    real_mkstemp = __import__("tempfile").mkstemp
    swapped = False

    def swap_parent_then_mkstemp(**kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            real_dir = layout.tmp_dir
            real_dir.rename(real_dir.with_name(real_dir.name + "-real"))
            try:
                real_dir.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"directory symlink unavailable: {exc}")
        return real_mkstemp(**kwargs)

    monkeypatch.setattr(
        "src.runtime.layout.tempfile.mkstemp",
        swap_parent_then_mkstemp,
    )

    with pytest.raises(PKVRuntimeError) as captured:
        layout.atomic_publish_user_file(
            target,
            label="测试文件",
            data=b"new",
        )

    assert captured.value.code is ErrorCode.DATA_ROOT_UNSAFE
    assert list(outside.iterdir()) == []
