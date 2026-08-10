"""W2 MCP server provider and publication-boundary contracts."""

import logging
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.mcp import server
from src.runtime.errors import ErrorCode, PKVRuntimeError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TRANSPORT_SECRET = "TRANSPORT-SECRET-CANARY"
MALICIOUS_TRANSPORT = (
    f"streamable-http\r\nFORGED api_key={TRANSPORT_SECRET} C:\\private"
)
MALICIOUS_LOG_LEVEL = "INFO\r\nFORGED api_key=MCP-CLI-LOG-SECRET C:\\private"


class _TruthinessBomb:
    def __bool__(self):
        raise AssertionError("malformed config value must not be coerced")


@pytest.fixture
def preserve_root_logger():
    root_logger = logging.getLogger()
    previous_handlers = list(root_logger.handlers)
    previous_level = root_logger.level
    try:
        yield
    finally:
        for handler in root_logger.handlers:
            if handler not in previous_handlers:
                handler.close()
        root_logger.handlers[:] = previous_handlers
        root_logger.setLevel(previous_level)


def test_exception_type_for_log_does_not_trust_dynamic_class_name():
    class CustomFailure(Exception):
        pass

    CustomFailure.__name__ = "MCP_LOG_SECRET\r\nFORGED"

    assert server._exception_type_for_log(CustomFailure("private")) == "Exception"
    assert server._exception_type_for_log(PermissionError("private")) == "OSError"
    assert server._exception_type_for_log(ValueError("private")) == "ValueError"
    assert server._exception_type_for_log(TypeError("private")) == "TypeError"


@pytest.mark.parametrize(
    "configured_level",
    [
        "INFO\r\nFORGED api_key=MCP-LOG-SECRET C:\\Users\\private",
        {"api_key": "MCP-LOG-SECRET"},
    ],
)
def test_main_canonicalizes_untrusted_config_log_level(
    configured_level,
    preserve_root_logger,
    capsys,
):
    config = SimpleNamespace(log_level=configured_level)
    config.get = lambda key, default=None: (
        False if key == "logging.file.enabled" else default
    )

    with (
        patch.object(sys, "argv", ["pkv-mcp"]),
        patch.object(server, "get_config", return_value=config),
        patch.object(server, "bootstrap_runtime") as bootstrap,
        patch.object(server.mcp, "run") as run_server,
    ):
        server.main()

    bootstrap.assert_called_once_with(config)
    run_server.assert_called_once_with(transport="stdio")
    stderr = capsys.readouterr().err
    assert "log_level=INFO" in stderr
    assert "MCP-LOG-SECRET" not in stderr
    assert "FORGED" not in stderr
    assert "Users" not in stderr


@pytest.mark.parametrize(
    "configured_enabled",
    ["true", "false", 1, 0, {"enabled": True}, _TruthinessBomb()],
)
def test_main_does_not_coerce_malformed_file_logging_flag(
    configured_enabled,
    preserve_root_logger,
):
    config = SimpleNamespace(
        log_level="INFO",
        log_dir=Path("isolated") / "logs",
        layout=SimpleNamespace(writable_user_path=object()),
    )
    config.get = lambda key, default=None: (
        configured_enabled if key == "logging.file.enabled" else default
    )

    with (
        patch.object(sys, "argv", ["pkv-mcp"]),
        patch.object(server, "get_config", return_value=config),
        patch.object(server, "bootstrap_runtime"),
        patch.object(server.LoggerSetup, "add_file_handler") as add_file_handler,
        patch.object(server.mcp, "run"),
    ):
        server.main()

    add_file_handler.assert_not_called()


def test_main_accepts_exact_true_file_logging_flag(preserve_root_logger):
    path_validator = object()
    config = SimpleNamespace(
        log_level="INFO",
        log_dir=Path("isolated") / "logs",
        layout=SimpleNamespace(writable_user_path=path_validator),
    )
    config.get = lambda key, default=None: (
        True if key == "logging.file.enabled" else default
    )

    with (
        patch.object(sys, "argv", ["pkv-mcp"]),
        patch.object(server, "get_config", return_value=config),
        patch.object(server, "bootstrap_runtime"),
        patch.object(server.LoggerSetup, "add_file_handler") as add_file_handler,
        patch.object(server.mcp, "run"),
    ):
        server.main()

    add_file_handler.assert_called_once_with(
        config.log_dir / "pkv.log",
        path_validator=path_validator,
        level=logging.INFO,
        log_format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


def test_transport_contract_only_publishes_stdio():
    server.ensure_supported_transport("stdio")

    with pytest.raises(PKVRuntimeError) as exc_info:
        server.ensure_supported_transport(MALICIOUS_TRANSPORT)

    assert exc_info.value.code is ErrorCode.TRANSPORT_UNSUPPORTED
    assert exc_info.value.stage == "mcp_transport_selection"
    assert str(exc_info.value) == "M13 仅发布 stdio transport"
    assert TRANSPORT_SECRET not in str(exc_info.value)
    assert "api_key" not in str(exc_info.value)
    assert "FORGED" not in str(exc_info.value)


def test_transport_rejection_subprocess_never_echoes_raw_argument():
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "tests" / "offline_entrypoint.py"),
            "mcp",
            "--transport",
            MALICIOUS_TRANSPORT,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 2
    assert ErrorCode.TRANSPORT_UNSUPPORTED.value in output
    assert "M13 仅发布 stdio transport" in output
    assert TRANSPORT_SECRET not in output
    assert "api_key" not in output
    assert "FORGED" not in output
    assert r"C:\private" not in output


def test_legacy_port_argument_cannot_bypass_stable_transport_rejection():
    port_secret = "PORT-SECRET\r\nFORGED api_key=PRIVATE C:\\private"
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "tests" / "offline_entrypoint.py"),
            "mcp",
            "--transport",
            "streamable-http",
            "--port",
            port_secret,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 2
    assert ErrorCode.TRANSPORT_UNSUPPORTED.value in output
    assert "PORT-SECRET" not in output
    assert "FORGED" not in output
    assert "api_key" not in output
    assert r"C:\private" not in output


def test_log_level_rejection_subprocess_never_echoes_raw_argument():
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "tests" / "offline_entrypoint.py"),
            "mcp",
            "--log-level",
            MALICIOUS_LOG_LEVEL,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 2
    assert "日志级别无效" in output
    assert "MCP-CLI-LOG-SECRET" not in output
    assert "FORGED" not in output
    assert "api_key" not in output
    assert r"C:\private" not in output


def test_help_does_not_publish_http_or_port(capsys):
    with (
        patch.object(sys, "argv", ["pkv-mcp", "--help"]),
        patch.object(server, "get_config") as get_config,
        pytest.raises(SystemExit) as exc_info,
    ):
        server.main()

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "stdio" in help_text
    assert "streamable-http" not in help_text
    assert "--port" not in help_text
    get_config.assert_not_called()


def test_query_router_receives_lazy_provider_factory():
    previous = server._query_router
    server._query_router = None
    config = SimpleNamespace(
        db_path=Path("isolated") / "knowledge.db",
        vector_index_dir=Path("isolated") / "vectors",
    )
    router = object()
    embedder = object()
    try:
        with (
            patch.object(server, "get_config", return_value=config),
            patch(
                "src.ai.provider_factory.create_embedder",
                return_value=embedder,
            ) as create_embedder,
            patch(
                "src.retrieval.query_router.QueryRouter",
                return_value=router,
            ) as router_type,
        ):
            assert server.get_query_router() is router
            assert server.get_query_router() is router

            create_embedder.assert_not_called()
            kwargs = router_type.call_args.kwargs
            assert kwargs["db_path"] == config.db_path
            assert kwargs["vector_index_dir"] == config.vector_index_dir
            assert "embedder" not in kwargs
            assert kwargs["embedder_factory"]() is embedder

        create_embedder.assert_called_once_with(config)
        router_type.assert_called_once()
    finally:
        server._query_router = previous
