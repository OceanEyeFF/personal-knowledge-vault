"""Packaging-contract tests for the frozen three-entrypoint surface."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src import __version__
from src.runtime.bootstrap import bootstrap_runtime, project_bootstrap_error
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.utils.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT_PATH = PROJECT_ROOT / "packaging" / "pkv_entrypoint.py"
pytestmark = pytest.mark.packaging_contract


def _load_dispatcher():
    spec = importlib.util.spec_from_file_location(
        "pkv_frozen_dispatcher", ENTRYPOINT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("executable", "module_name", "result", "expected"),
    [
        ("C:/PKV/pkv.exe", "src.main", None, 0),
        ("C:/PKV/PKV-GUI.EXE", "src.gui.app", 7, 7),
        ("C:/PKV/pkv-mcp.exe", "src.mcp.server", None, 0),
    ],
)
def test_dispatcher_selects_one_published_role_by_executable_name(
    executable: str,
    module_name: str,
    result: int | None,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher = _load_dispatcher()
    imported: list[str] = []

    def fake_import(name: str):
        imported.append(name)
        return SimpleNamespace(main=lambda: result)

    monkeypatch.setattr(dispatcher.importlib, "import_module", fake_import)

    assert dispatcher.dispatch(executable) == expected
    assert imported == [module_name]


def test_gui_dispatch_pins_qt_api_before_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher = _load_dispatcher()
    monkeypatch.setenv("QT_API", "pyqt5")

    def fake_import(name: str):
        assert name == "src.gui.app"
        assert dispatcher.os.environ["QT_API"] == "pyside6"
        return SimpleNamespace(main=lambda: 0)

    monkeypatch.setattr(dispatcher.importlib, "import_module", fake_import)

    assert dispatcher.dispatch("C:/PKV/pkv-gui.exe") == 0


def test_unknown_frozen_name_fails_with_stable_stderr_only(capsys) -> None:
    dispatcher = _load_dispatcher()

    assert dispatcher.dispatch("C:/private/renamed-secret.exe") == 64

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "code": "entrypoint_unknown",
        "recoverable": False,
        "stage": "entrypoint_dispatch",
        "status": "error",
    }
    assert "private" not in captured.err
    assert "secret" not in captured.err


def test_product_version_has_one_python_source_of_truth() -> None:
    assignments: list[tuple[str, str]] = []
    for path in sorted((PROJECT_ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in targets
            ):
                continue
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                assignments.append(
                    (path.relative_to(PROJECT_ROOT).as_posix(), value.value)
                )

    assert __version__ == "0.8.1"
    assert assignments == [("src/__init__.py", "0.8.1")]


def test_bootstrap_error_projection_is_stable_and_does_not_publish_message() -> None:
    secret = "C:/private/Vault api_key=DO-NOT-PUBLISH"
    error = PKVRuntimeError(
        ErrorCode.DATABASE_UPGRADE_REQUIRED,
        secret,
        stage="database_preflight",
        recoverable=False,
    )

    assert project_bootstrap_error(error, adapter="cli") == {
        "adapter": "cli",
        "code": "database_upgrade_required",
        "recoverable": False,
        "stage": "database_preflight",
        "status": "error",
    }
    assert secret not in repr(project_bootstrap_error(error, adapter="cli"))


def test_bootstrap_error_projection_bounds_untrusted_stage() -> None:
    error = PKVRuntimeError(
        ErrorCode.RESOURCE_MISSING,
        "private",
        stage="bad\r\napi_key=PRIVATE",
        recoverable=True,
    )

    payload = project_bootstrap_error(error, adapter="mcp")

    assert payload["stage"] == "runtime_bootstrap"
    assert payload["recoverable"] is True
    assert "api_key" not in repr(payload)


def test_unexpected_startup_projection_is_fail_closed_and_message_free() -> None:
    secret = "C:/private/local.yaml api_key=DO-NOT-PUBLISH"

    payload = project_bootstrap_error(
        ValueError(secret),
        adapter="cli",
        stage="runtime_configuration",
    )

    assert payload == {
        "adapter": "cli",
        "code": "runtime_startup_failed",
        "recoverable": False,
        "stage": "runtime_configuration",
        "status": "error",
    }
    assert secret not in repr(payload)


def test_cli_bootstrap_failure_uses_stderr_machine_projection_only(capsys) -> None:
    from src import main as cli_main

    failure = PKVRuntimeError(
        ErrorCode.DATABASE_FUTURE_VERSION,
        "C:/private/future.db",
        recoverable=False,
    )
    with (
        patch.object(cli_main, "cli", side_effect=failure),
        pytest.raises(SystemExit) as raised,
    ):
        cli_main.main()

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "adapter": "cli",
        "code": "database_future_version",
        "recoverable": False,
        "stage": "runtime_bootstrap",
        "status": "error",
    }
    assert "private" not in captured.err


def test_cli_malformed_configuration_has_stable_cold_entry_failure(capsys) -> None:
    from src import main as cli_main

    secret = "C:/private/local.yaml api_key=CLI-STARTUP-SECRET"
    with (
        patch.object(sys, "argv", ["pkv", "stats"]),
        patch.object(cli_main, "get_config", side_effect=ValueError(secret)),
        pytest.raises(SystemExit) as raised,
    ):
        cli_main.main()

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "adapter": "cli",
        "code": "runtime_startup_failed",
        "recoverable": False,
        "stage": "runtime_configuration",
        "status": "error",
    }
    assert secret not in captured.err
    assert "Traceback" not in captured.err


def test_cli_routes_console_logging_to_stderr() -> None:
    from src import main as cli_main

    config = SimpleNamespace(
        log_dir=Path("isolated") / "logs",
        layout=SimpleNamespace(writable_user_path=object()),
    )
    with (
        patch.object(cli_main, "get_config", return_value=config),
        patch.object(cli_main, "bootstrap_runtime"),
        patch.object(cli_main.LoggerSetup, "setup") as setup,
    ):
        cli_main._configure_logging("WARNING")

    assert setup.call_args.kwargs["console_stream"] is sys.stderr


def test_mcp_bootstrap_failure_uses_stderr_machine_projection_only(capsys) -> None:
    from src.mcp import server

    failure = PKVRuntimeError(
        ErrorCode.DATABASE_SCHEMA_DRIFT,
        "C:/private/drift.db",
        stage="database_preflight",
        recoverable=False,
    )
    with (
        patch.object(sys, "argv", ["pkv-mcp"]),
        patch.object(server, "get_config", return_value=object()),
        patch.object(server, "bootstrap_runtime", side_effect=failure),
        patch.object(server.mcp, "run") as run_server,
        pytest.raises(SystemExit) as raised,
    ):
        server.main()

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "adapter": "mcp",
        "code": "database_schema_drift",
        "recoverable": False,
        "stage": "database_preflight",
        "status": "error",
    }
    assert "private" not in captured.err
    run_server.assert_not_called()


def test_mcp_malformed_configuration_has_stable_cold_entry_failure(capsys) -> None:
    from src.mcp import server

    secret = "C:/private/local.yaml api_key=MCP-STARTUP-SECRET"
    with (
        patch.object(sys, "argv", ["pkv-mcp"]),
        patch.object(server, "get_config", side_effect=ValueError(secret)),
        patch.object(server.mcp, "run") as run_server,
        pytest.raises(SystemExit) as raised,
    ):
        server.main()

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "adapter": "mcp",
        "code": "runtime_startup_failed",
        "recoverable": False,
        "stage": "runtime_configuration",
        "status": "error",
    }
    assert secret not in captured.err
    assert "Traceback" not in captured.err
    run_server.assert_not_called()


def test_gui_bootstrap_failure_preserves_stable_projection(caplog) -> None:
    from src.gui import app as gui_app

    failure = PKVRuntimeError(
        ErrorCode.RESOURCE_MISSING,
        "C:/private/missing-resource",
        recoverable=False,
    )
    with (
        patch.object(gui_app, "Config", return_value=object()),
        patch.object(gui_app, "bootstrap_runtime", side_effect=failure),
        caplog.at_level("ERROR", logger="pkv.gui.app"),
    ):
        assert gui_app.ensure_database_initialized() is False

    assert gui_app.get_bootstrap_failure_projection() == {
        "adapter": "gui",
        "code": "resource_missing",
        "recoverable": False,
        "stage": "runtime_bootstrap",
        "status": "error",
    }
    assert "private" not in caplog.text


def test_gui_malformed_configuration_has_stable_cold_entry_failure(caplog) -> None:
    from src.gui import app as gui_app

    secret = "C:/private/local.yaml api_key=GUI-STARTUP-SECRET"
    with (
        patch.object(gui_app, "Config", side_effect=ValueError(secret)),
        caplog.at_level("ERROR", logger="pkv.gui.app"),
    ):
        assert gui_app.ensure_database_initialized() is False

    assert gui_app.get_bootstrap_failure_projection() == {
        "adapter": "gui",
        "code": "runtime_startup_failed",
        "recoverable": False,
        "stage": "runtime_configuration",
        "status": "error",
    }
    assert secret not in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_adapter_startup_boundaries_do_not_swallow_base_exceptions(
    exception_type: type[BaseException],
) -> None:
    from src import main as cli_main
    from src.gui import app as gui_app
    from src.mcp import server

    with patch.object(cli_main, "get_config", side_effect=exception_type()):
        with pytest.raises(exception_type):
            cli_main._configure_logging("WARNING")

    with (
        patch.object(sys, "argv", ["pkv-mcp"]),
        patch.object(server, "get_config", side_effect=exception_type()),
    ):
        with pytest.raises(exception_type):
            server.main()

    with patch.object(gui_app, "Config", side_effect=exception_type()):
        with pytest.raises(exception_type):
            gui_app.ensure_database_initialized()


def test_bootstrap_routes_jieba_cache_to_runtime_tmp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jieba

    layout = RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT,
        user_data_root=tmp_path / "user-data",
        environment={},
    )
    monkeypatch.setattr(jieba.dt, "tmp_dir", "C:/outside-jieba-cache")

    bootstrap_runtime(Config(layout=layout))

    assert jieba.dt.tmp_dir == str(layout.tmp_dir)
    assert layout.tmp_dir.is_dir()


def test_gui_source_pins_qt_api_before_qt_or_main_window_import() -> None:
    source = (PROJECT_ROOT / "src" / "gui" / "app.py").read_text(encoding="utf-8")

    qt_api = source.index('os.environ["QT_API"] = "pyside6"')
    pyside = source.index("from PySide6.QtWidgets")
    main_window = source.index("from src.gui.main_window")

    assert qt_api < pyside < main_window


def test_gui_module_cold_import_has_no_logger_runtime_cycle(tmp_path: Path) -> None:
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(PROJECT_ROOT)!r});"
        "import src.gui.app;"
        "print('gui-import-ok')"
    )
    environment = dict(os.environ)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["QT_API"] = "pyqt5"
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "gui-import-ok"
