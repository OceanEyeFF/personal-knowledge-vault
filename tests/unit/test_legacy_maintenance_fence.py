"""Contracts for retired raw maintenance script entrypoints."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from scripts import backfill_chunks, backfill_relations, init_db, migrate


@pytest.mark.parametrize(
    ("module", "invoke"),
    [
        (backfill_chunks, lambda: backfill_chunks.main(["--apply"])),
        (backfill_relations, lambda: backfill_relations.main(["--apply"])),
        (init_db, init_db.main),
        (migrate, lambda: migrate.main(["--auto", "--no-backup"])),
    ],
)
def test_retired_raw_maintenance_entrypoints_reject_before_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    module: object,
    invoke: Callable[[], int],
) -> None:
    """The raw routes cannot select a configured root or mutate it."""

    if hasattr(module, "Config"):
        monkeypatch.setattr(
            module,
            "Config",
            lambda: pytest.fail("retired script must not construct Config"),
        )

    assert invoke() == 2

    captured = capsys.readouterr()
    assert "已停用" in captured.err
    assert "未读取配置" in captured.err
    assert "未打开数据根" in captured.err


def test_retained_chunk_backfill_helper_requires_isolated_test_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The fixture seam cannot be repurposed against an arbitrary path."""

    monkeypatch.delenv("PKV_TEST_OFFLINE", raising=False)
    db_path = tmp_path / "outside" / "knowledge.db"

    with pytest.raises(RuntimeError, match="隔离合成 fixture"):
        backfill_chunks.run_chunk_backfill(
            db_path=db_path,
            vector_index_dir=tmp_path / "outside" / "vectors",
            apply=False,
        )

    assert not db_path.exists()


@pytest.mark.parametrize(
    ("relative_path", "forbidden_markers", "fence"),
    [
        (
            Path("scripts/legacy/setup.ps1"),
            ("python --version", "pip install", "config\\local.yaml", ".data\\db"),
            "exit 2",
        ),
        (
            Path("scripts/legacy/setup.bat"),
            ("python --version", "pip install", "config\\local.yaml", ".data\\db"),
            "exit /b 2",
        ),
        (
            Path("scripts/legacy/test.ps1"),
            ("Activate.ps1", "python src\\utils\\verify_setup.py"),
            "exit 2",
        ),
    ],
)
def test_legacy_setup_and_test_entrypoints_fence_before_config_network_or_data_root(
    relative_path: Path,
    forbidden_markers: tuple[str, ...],
    fence: str,
) -> None:
    """The historical scripts cannot reach an old layout before failing closed."""

    source = (Path(__file__).resolve().parents[2] / relative_path).read_text(
        encoding="utf-8-sig"
    ).casefold()
    fence_index = source.index(fence)
    for marker in forbidden_markers:
        assert fence_index < source.index(marker.casefold())
