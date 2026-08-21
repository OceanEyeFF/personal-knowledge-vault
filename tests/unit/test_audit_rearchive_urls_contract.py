from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
AUDIT_SCRIPT = PROJECT_ROOT / "scripts" / "audit_rearchive_urls.py"


def _load_audit_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "pkv_test_audit_rearchive_urls", AUDIT_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def audit_module() -> ModuleType:
    return _load_audit_module()


def test_audit_requires_an_explicit_vault_argument(
    audit_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        audit_module.parse_args([])

    assert raised.value.code == 2
    assert "--vault" in capsys.readouterr().err
    assert 'default=Path(".data/vault")' not in AUDIT_SCRIPT.read_text(
        encoding="utf-8"
    )


def test_audit_help_declares_its_user_only_network_boundary(
    audit_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        audit_module.parse_args(["--help"])

    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "不属于默认自动化" in help_text
    assert "明确用户授权" in help_text
    assert "外部网络" in help_text


def test_explicit_empty_synthetic_vault_never_requests_network(
    audit_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vault = tmp_path / "synthetic-vault"
    vault.mkdir()
    output = tmp_path / "report"

    def fail_if_network_called(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("URL audit must not request a network resource for an empty vault")

    monkeypatch.setattr(audit_module.requests, "get", fail_if_network_called)

    assert audit_module.main(["--vault", str(vault), "--output", str(output)]) == 0
    assert (output / "summary.json").is_file()
    assert '"unique_urls": 0' in (output / "summary.json").read_text(
        encoding="utf-8"
    )
    assert "URL_AUDIT_OK" in capsys.readouterr().out
