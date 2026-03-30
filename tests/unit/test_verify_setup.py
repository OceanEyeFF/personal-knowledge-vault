"""
verify_setup 隔离性测试
"""

import sys
from pathlib import Path
from types import SimpleNamespace

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.utils import verify_setup


def test_verify_setup_main_uses_isolated_workspace(tmp_path: Path, monkeypatch):
    """验证脚本不应污染当前配置指向的真实目录。"""
    real_root = tmp_path / "real"
    sandbox_root = tmp_path / "sandbox"

    config = SimpleNamespace(
        vault_dir=real_root / "vault",
        db_path=real_root / "data" / "knowledge.db",
        vector_index_dir=real_root / "vectors",
        log_dir=real_root / "logs",
        tmp_dir=real_root / "tmp",
        log_level="INFO",
        embedding_dim=8,
    )

    class _TempWorkspace:
        def __enter__(self):
            sandbox_root.mkdir(parents=True, exist_ok=True)
            return str(sandbox_root)

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(verify_setup, "get_config", lambda: config)
    monkeypatch.setattr(
        verify_setup.tempfile,
        "TemporaryDirectory",
        lambda prefix=None: _TempWorkspace(),
    )

    verify_setup.main()

    assert not config.vault_dir.exists()
    assert not config.db_path.exists()
    assert not config.vector_index_dir.exists()
    assert not config.log_dir.exists()
    assert (sandbox_root / "vault").exists()
    assert (sandbox_root / "data" / "verify.db").exists()
    assert (sandbox_root / "vectors").exists()
    assert (sandbox_root / "logs" / "verify.log").exists()
