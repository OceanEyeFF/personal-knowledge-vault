"""Containment and compensation contracts for the Vault path gateway."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from src.runtime import ErrorCode, PKVRuntimeError
from src.storage import vault_paths as vp
from src.storage.vault_paths import QuarantinedVaultFile, VaultPathGateway


def test_gateway_persists_only_vault_relative_posix_names(tmp_path: Path) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    target = gateway.write_text_atomic("text/entry.md", "hello")

    assert target.read_text(encoding="utf-8") == "hello"
    assert gateway.relative_name(target) == "text/entry.md"


def test_atomic_write_never_replaces_an_existing_vault_file(tmp_path: Path) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    target = gateway.write_text_atomic("text/entry.md", "original")

    with pytest.raises(FileExistsError):
        gateway.write_text_atomic("text/entry.md", "replacement")

    assert gateway.read_text(target) == "original"


@pytest.mark.parametrize(
    "candidate",
    ["../outside.md", "nested/../../outside.md"],
)
def test_gateway_rejects_lexical_escape_before_write(
    tmp_path: Path, candidate: str
) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    outside = tmp_path / "outside.md"

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.write_text_atomic(candidate, "must-not-write")

    assert exc_info.value.code is ErrorCode.PATH_OUTSIDE_VAULT
    assert not outside.exists()


def test_gateway_rejects_absolute_path_outside_vault(tmp_path: Path) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.read_text(tmp_path / "outside.md")

    assert exc_info.value.code is ErrorCode.PATH_OUTSIDE_VAULT


def test_gateway_rejects_hardlink_file(tmp_path: Path) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    original = gateway.write_text_atomic("source.md", "source")
    hardlink = gateway.vault_dir / "hardlink.md"
    try:
        os.link(original, hardlink)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.read_text(hardlink)

    assert exc_info.value.code is ErrorCode.PATH_LINK_UNSAFE


def test_gateway_rejects_link_replacement_in_parent_chain(tmp_path: Path) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_dir = gateway.vault_dir / "linked"
    try:
        linked_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.write_text_atomic("linked/escape.md", "must-not-write")

    assert exc_info.value.code is ErrorCode.PATH_LINK_UNSAFE
    assert not (outside / "escape.md").exists()


def test_gateway_rejects_redirected_parent_before_creating_vault(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-parent"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(PKVRuntimeError) as exc_info:
        VaultPathGateway(linked_parent / "vault")

    assert exc_info.value.code is ErrorCode.PATH_LINK_UNSAFE
    assert not (outside / "vault").exists()


def test_quarantine_can_restore_or_finalize_primary_file(tmp_path: Path) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    original = gateway.write_text_atomic("text/entry.md", "hello")

    first = gateway.quarantine(original)
    assert not original.exists()
    restored = gateway.restore(first)
    assert restored.read_text(encoding="utf-8") == "hello"

    second = gateway.quarantine(restored)
    gateway.finalize_quarantine(second)
    assert not restored.exists()
    assert not second.quarantine_path.exists()


def test_delete_if_identity_rejects_same_inode_content_rewrite(tmp_path: Path) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    published = gateway.write_text_atomic_record("text/entry.md", "original")
    published.path.write_text("user rewrite", encoding="utf-8")
    assert gateway.file_identity(published.path) == published.identity

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.delete_if_identity(
            published.path,
            expected_identity=published.identity,
            expected_sha256=published.sha256,
        )

    assert exc_info.value.code is ErrorCode.PATH_STATE_UNDETERMINED
    assert published.path.read_text(encoding="utf-8") == "user rewrite"


def test_quarantine_restore_rejects_same_inode_content_rewrite(tmp_path: Path) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    original = gateway.write_text_atomic("text/entry.md", "original")
    item = gateway.quarantine(original)
    item.quarantine_path.write_text("user rewrite", encoding="utf-8")
    assert gateway.file_identity(
        item.quarantine_path, allow_internal=True
    ) == item.expected_identity

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.restore(item)

    assert exc_info.value.code is ErrorCode.STORAGE_COMPENSATION_FAILED
    assert not original.exists()
    assert item.quarantine_path.read_text(encoding="utf-8") == "user rewrite"


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive path contract")
def test_reserved_quarantine_name_is_case_insensitive_on_windows(tmp_path: Path) -> None:
    """Case variants must not expose the internal quarantine on NTFS."""

    gateway = VaultPathGateway(tmp_path / "vault")
    original = gateway.write_text_atomic("text/entry.md", "secret")
    quarantined = gateway.quarantine(original, operation_id="a" * 32)
    alias = gateway.vault_dir / ".PKV-QUARANTINE" / quarantined.quarantine_path.name

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.read_text(alias)

    assert exc_info.value.code is ErrorCode.PATH_OUTSIDE_VAULT
    assert quarantined.quarantine_path.read_text(encoding="utf-8") == "secret"


def test_markdown_enumeration_fails_closed_on_link_directory(tmp_path: Path) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    linked_dir = gateway.vault_dir / "linked"
    try:
        linked_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(PKVRuntimeError) as exc_info:
        list(gateway.iter_markdown())

    assert exc_info.value.code is ErrorCode.PATH_LINK_UNSAFE


# ---------------------------------------------------------------------------
# validate-to-open identity race -- read path
# ---------------------------------------------------------------------------


def test_read_text_rejects_handle_identity_mismatch_and_never_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """打开句柄与校验记录不是同一文件: 读取前必须拒绝并关闭句柄。"""
    gateway = VaultPathGateway(tmp_path / "vault")
    gateway.write_text_atomic("text/entry.md", "victim")
    other = gateway.vault_dir / "text" / "other.md"
    other.write_text("attacker", encoding="utf-8")
    real_open = os.open
    real_close = os.close
    opened_fds: list[int] = []
    closed_fds: list[int] = []

    def racing_open(path, flags, *args, **kwargs):
        if Path(path) == gateway.vault_dir / "text" / "entry.md":
            path = other  # 模拟: 校验后、打开时被替换
        fd = real_open(path, flags, *args, **kwargs)
        opened_fds.append(fd)
        return fd

    def never_read(fd, size):
        raise AssertionError("身份核对完成前不得读取内容")

    def tracking_close(fd):
        closed_fds.append(fd)
        return real_close(fd)

    monkeypatch.setattr(vp.os, "open", racing_open)
    monkeypatch.setattr(vp.os, "read", never_read)
    monkeypatch.setattr(vp.os, "close", tracking_close)

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.read_text("text/entry.md")

    assert exc_info.value.code is ErrorCode.PATH_STATE_UNDETERMINED
    assert opened_fds and all(fd in closed_fds for fd in opened_fds)


def test_read_text_rejects_file_swapped_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lstat 记录一个文件, 打开时却是另一个: 必须拒绝且不读取任何内容。"""
    gateway = VaultPathGateway(tmp_path / "vault")
    gateway.write_text_atomic("text/entry.md", "victim")
    decoy = gateway.vault_dir / "text" / "decoy.md"
    decoy.write_text("decoy", encoding="utf-8")
    real_lstat = vp._lstat

    def racing_lstat(path, **kwargs):
        if Path(path) == gateway.vault_dir / "text" / "entry.md":
            return os.stat(decoy)
        return real_lstat(path, **kwargs)

    def never_read(fd, size):
        raise AssertionError("身份核对完成前不得读取内容")

    monkeypatch.setattr(vp, "_lstat", racing_lstat)
    monkeypatch.setattr(vp.os, "read", never_read)

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.read_text("text/entry.md")

    assert exc_info.value.code is ErrorCode.PATH_STATE_UNDETERMINED


@pytest.mark.skipif(os.name == "nt", reason="O_NOFOLLOW is POSIX-only")
def test_read_text_uses_no_follow_and_maps_eloop_to_link_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    gateway.write_text_atomic("text/entry.md", "hello")
    real_open = os.open
    seen_flags: list[int] = []

    def no_follow_open(path, flags, *args, **kwargs):
        seen_flags.append(flags)
        if Path(path) == gateway.vault_dir / "text" / "entry.md":
            raise OSError(errno.ELOOP, "too many levels of symbolic links")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(vp.os, "open", no_follow_open)

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.read_text("text/entry.md")

    assert exc_info.value.code is ErrorCode.PATH_LINK_UNSAFE
    assert seen_flags and seen_flags[0] & os.O_NOFOLLOW


# ---------------------------------------------------------------------------
# record-and-reverify identity before destructive mutations
# ---------------------------------------------------------------------------


def test_reverify_identity_detects_swapped_file(tmp_path: Path) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    original = gateway.write_text_atomic("entry.md", "hello")
    attacker = gateway.write_text_atomic("other.md", "attacker")
    recorded = vp._lstat(original)
    os.replace(attacker, original)  # 记录后、复核前被替换

    with pytest.raises(PKVRuntimeError) as exc_info:
        vp._reverify_identity(original, recorded)

    assert exc_info.value.code is ErrorCode.PATH_STATE_UNDETERMINED


def test_reverify_identity_detects_vanished_file(tmp_path: Path) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    original = gateway.write_text_atomic("entry.md", "hello")
    recorded = vp._lstat(original)
    original.unlink()

    with pytest.raises(PKVRuntimeError) as exc_info:
        vp._reverify_identity(original, recorded)

    assert exc_info.value.code is ErrorCode.PATH_STATE_UNDETERMINED


def test_delete_aborts_when_reverify_fails_and_leaves_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    original = gateway.write_text_atomic("entry.md", "hello")

    def injected_failure(path, expected):
        raise PKVRuntimeError(ErrorCode.PATH_STATE_UNDETERMINED, "injected race")

    monkeypatch.setattr(vp, "_reverify_identity", injected_failure)

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.delete("entry.md")

    assert exc_info.value.code is ErrorCode.PATH_STATE_UNDETERMINED
    assert original.exists()


def test_delete_detects_path_recreated_after_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    gateway.write_text_atomic("entry.md", "hello")
    real_unlink = os.unlink

    def recreating_unlink(path):
        real_unlink(path)
        Path(path).write_text("recreated", encoding="utf-8")

    monkeypatch.setattr(vp.os, "unlink", recreating_unlink)

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.delete("entry.md")

    assert exc_info.value.code is ErrorCode.PATH_STATE_UNDETERMINED
    assert (gateway.vault_dir / "entry.md").read_text(encoding="utf-8") == "recreated"


def test_quarantine_detects_swapped_file_before_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    original = gateway.write_text_atomic("entry.md", "hello")
    attacker = gateway.write_text_atomic("other.md", "attacker")
    real_reverify = vp._reverify_identity

    def swapping_reverify(path, expected):
        if path == original:
            os.replace(attacker, original)  # 复核前注入替换
        return real_reverify(path, expected)

    monkeypatch.setattr(vp, "_reverify_identity", swapping_reverify)

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.quarantine(original)

    assert exc_info.value.code is ErrorCode.PATH_STATE_UNDETERMINED
    assert original.exists()  # 未被移动
    assert not list((gateway.vault_dir / ".pkv-quarantine").glob("*"))  # 无残留


def test_finalize_quarantine_aborts_when_reverify_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    original = gateway.write_text_atomic("entry.md", "hello")
    item = gateway.quarantine(original)

    def injected_failure(path, expected):
        raise PKVRuntimeError(ErrorCode.PATH_STATE_UNDETERMINED, "injected race")

    monkeypatch.setattr(vp, "_reverify_identity", injected_failure)

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.finalize_quarantine(item)

    assert exc_info.value.code is ErrorCode.PATH_STATE_UNDETERMINED
    assert item.quarantine_path.exists()


def test_write_text_atomic_rejects_temp_file_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    decoy = gateway.vault_dir / "decoy.md"
    decoy.write_text("decoy", encoding="utf-8")
    real_lstat = vp._lstat

    def racing_lstat(path, **kwargs):
        if path.name.startswith(".entry.md.") and path.name.endswith(".tmp"):
            return os.stat(decoy)  # 不同 inode: 临时文件被替换
        return real_lstat(path, **kwargs)

    monkeypatch.setattr(vp, "_lstat", racing_lstat)

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.write_text_atomic("text/entry.md", "hello")

    assert exc_info.value.code is ErrorCode.PATH_STATE_UNDETERMINED
    assert not (gateway.vault_dir / "text" / "entry.md").exists()


# ---------------------------------------------------------------------------
# restore -- containment, internal quarantine dir, never overwrite
# ---------------------------------------------------------------------------


def test_restore_rejects_quarantine_path_outside_internal_dir(tmp_path: Path) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    original = gateway.write_text_atomic("entry.md", "hello")
    item = gateway.quarantine(original)
    stray = gateway.write_text_atomic("stray.md", "stray")
    bad = QuarantinedVaultFile(original, stray)

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.restore(bad)

    assert exc_info.value.code is ErrorCode.PATH_OUTSIDE_VAULT
    assert stray.exists()
    assert item.quarantine_path.exists()


def test_restore_rejects_original_path_outside_vault(tmp_path: Path) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    original = gateway.write_text_atomic("entry.md", "hello")
    item = gateway.quarantine(original)
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    bad = QuarantinedVaultFile(outside, item.quarantine_path)

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.restore(bad)

    assert exc_info.value.code is ErrorCode.PATH_OUTSIDE_VAULT
    assert item.quarantine_path.exists()


def test_restore_never_overwrites_concurrent_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    original = gateway.write_text_atomic("entry.md", "hello")
    item = gateway.quarantine(original)
    original_path = gateway.vault_dir / "entry.md"
    if os.name == "nt":
        real_move = os.rename

        def racing_move(src, dst):
            if Path(dst) == original_path:
                Path(dst).write_text("concurrent", encoding="utf-8")
            return real_move(src, dst)

        monkeypatch.setattr(vp.os, "rename", racing_move)
    else:
        real_link = os.link

        def racing_link(src, dst, **kwargs):
            if Path(dst) == original_path:
                Path(dst).write_text("concurrent", encoding="utf-8")
            return real_link(src, dst, **kwargs)

        monkeypatch.setattr(vp.os, "link", racing_link)

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.restore(item)

    assert exc_info.value.code is ErrorCode.STORAGE_COMPENSATION_FAILED
    assert (gateway.vault_dir / "entry.md").read_text(encoding="utf-8") == "concurrent"
    assert item.quarantine_path.read_text(encoding="utf-8") == "hello"


def test_restore_raises_compensation_failure_when_post_move_identity_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = VaultPathGateway(tmp_path / "vault")
    original = gateway.write_text_atomic("entry.md", "hello")
    item = gateway.quarantine(original)
    decoy = gateway.vault_dir / "decoy.md"
    decoy.write_text("decoy", encoding="utf-8")
    real_lstat = vp._lstat

    def racing_lstat(path, **kwargs):
        if Path(path) == gateway.vault_dir / "entry.md":
            return os.stat(decoy)
        return real_lstat(path, **kwargs)

    monkeypatch.setattr(vp, "_lstat", racing_lstat)

    with pytest.raises(PKVRuntimeError) as exc_info:
        gateway.restore(item)

    assert exc_info.value.code is ErrorCode.STORAGE_COMPENSATION_FAILED
