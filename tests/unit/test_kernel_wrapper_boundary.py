"""Architecture contracts for the headless Kernel public surface."""

from __future__ import annotations

import ast
from contextlib import nullcontext
from pathlib import Path
import tomllib
from types import SimpleNamespace

import pytest
import pkv_kernel
from src.kernel import KnowledgeKernel


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_FIND = tomllib.loads(
    (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
)["tool"]["setuptools"]["packages"]["find"]
_WHEEL_CORE_INCLUDES = tuple(_PACKAGE_FIND["include"])
_WHEEL_CORE_EXCLUDES = tuple(_PACKAGE_FIND["exclude"])
_EXPECTED_WHEEL_CORE_INCLUDES = frozenset(
    {
        "pkv_kernel*",
        "src",
        "src.ai*",
        "src.application*",
        "src.kernel*",
        "src.processors*",
        "src.relations*",
        "src.retrieval*",
        "src.runtime*",
        "src.storage*",
        "src.utils*",
        "src.workflow*",
    }
)
_REQUIRED_WHEEL_CORE_EXCLUDES = frozenset(
    {"src.cli*", "src.mcp*", "src.gui*", "tests*"}
)
_FORBIDDEN_IMPORT_PREFIXES = (
    "src.cli",
    "src.mcp",
    "src.gui",
    "mcp",
    "pkv_gui",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "qasync",
)
_PUBLIC_API_V1 = frozenset(
    {
        "__version__",
        "ChatProvider",
        "ChatProviderSettings",
        "ChatStream",
        "ChatStreamEvent",
        "Config",
        "ErrorCode",
        "KernelChatSessions",
        "KernelCapabilities",
        "KernelCompatibilityError",
        "KERNEL_API_VERSION",
        "KERNEL_CAPABILITIES",
        "KnowledgeKernel",
        "OperationStatus",
        "PKVRuntimeError",
        "PreviewIssue",
        "PreviewOutcome",
        "RetrievalIssue",
        "SearchResponse",
        "SearchResult",
        "StorageOperationResult",
        "StorageStage",
        "SUPPORTED_PLATFORMS",
        "SUPPORTED_PYTHON",
        "WorkflowResult",
        "bootstrap_kernel",
        "configure_kernel",
        "contracts",
        "describe_url_target",
        "get_config",
        "get_kernel",
        "get_kernel_capabilities",
        "is_strict_chat_stream_event",
        "is_strict_preview_outcome",
        "is_strict_search_response",
        "is_supported_chat_finish_reason",
        "load_preview_with_store",
        "lifecycle",
        "project_bootstrap_error",
        "redact_url_credentials",
        "reload_kernel",
        "require_kernel_compatibility",
        "reset_kernel",
        "runtime_is_supported",
        "sanitize_public_source_url",
        "url_contains_credentials",
        "validate_provider_base_url",
        "validate_text_length",
        "validate_url_security_result",
    }
)
_PUBLIC_CONTRACTS_API_V1 = frozenset(
    {
        "KERNEL_API_VERSION",
        "KERNEL_CAPABILITIES",
        "SUPPORTED_PLATFORMS",
        "SUPPORTED_PYTHON",
        "KernelCapabilities",
        "KernelCompatibilityError",
        "get_kernel_capabilities",
        "require_kernel_compatibility",
        "runtime_is_supported",
    }
)
_PUBLIC_LIFECYCLE_API_V1 = frozenset(
    {
        "RuntimeConfirmation",
        "RuntimeExecution",
        "RuntimeInspection",
        "RuntimePlan",
        "confirm_runtime_plan",
        "execute_runtime_plan",
        "inspect_runtime",
        "open_kernel_from_execution",
        "plan_runtime",
    }
)
_KERNEL_CAPABILITIES_V1 = frozenset(
    {
        "kernel.lifecycle.v1",
        "kernel.runtime-lifecycle.v1",
        "kernel.archive.v1",
        "kernel.retrieval.v1",
        "kernel.entries.v1",
        "kernel.chat-sessions.v1",
        "kernel.configuration-snapshot-reload.v1",
    }
)


def _source_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
            if node.module == "src":
                imports.extend(f"src.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    return imports


def _wheel_core_source_files() -> tuple[Path, ...]:
    """Expand exactly the packages shipped by the Core wheel allowlist."""

    files: set[Path] = set()
    for pattern in _WHEEL_CORE_INCLUDES:
        package_name = pattern.removesuffix("*").rstrip(".")
        package_root = PROJECT_ROOT.joinpath(*package_name.split("."))
        assert package_root.is_dir(), f"wheel package root is missing: {pattern}"
        if pattern.endswith("*"):
            files.update(package_root.rglob("*.py"))
        else:
            package_init = package_root / "__init__.py"
            assert package_init.is_file(), f"wheel package init is missing: {pattern}"
            files.add(package_init)
    return tuple(sorted(files))


def _is_forbidden_import(module: str) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.")
        for prefix in _FORBIDDEN_IMPORT_PREFIXES
    )


def test_wheel_core_allowlist_excludes_adapter_packages() -> None:
    """The distribution manifest is part of the no-adapter public boundary."""

    assert frozenset(_WHEEL_CORE_INCLUDES) == _EXPECTED_WHEEL_CORE_INCLUDES
    assert _REQUIRED_WHEEL_CORE_EXCLUDES <= frozenset(_WHEEL_CORE_EXCLUDES)


def test_wheel_core_packages_have_no_adapter_or_framework_dependency() -> None:
    violations = [
        f"{path.relative_to(PROJECT_ROOT)} -> {module}"
        for path in _wheel_core_source_files()
        for module in _source_imports(path)
        if _is_forbidden_import(module)
    ]

    assert violations == []


def test_pkv_kernel_v1_public_api_is_exactly_frozen() -> None:
    """Freeze both declared SDK public namespaces without ``src.*`` imports."""

    assert frozenset(pkv_kernel.__all__) == _PUBLIC_API_V1
    assert all(hasattr(pkv_kernel, name) for name in _PUBLIC_API_V1)
    assert frozenset(pkv_kernel.contracts.__all__) == _PUBLIC_CONTRACTS_API_V1
    assert all(
        hasattr(pkv_kernel.contracts, name) for name in _PUBLIC_CONTRACTS_API_V1
    )
    assert frozenset(pkv_kernel.lifecycle.__all__) == _PUBLIC_LIFECYCLE_API_V1
    assert all(
        hasattr(pkv_kernel.lifecycle, name) for name in _PUBLIC_LIFECYCLE_API_V1
    )


def test_pkv_kernel_capability_handshake_enforces_wrapper_requirements() -> None:
    capabilities = pkv_kernel.get_kernel_capabilities()

    assert capabilities.sdk_version == pkv_kernel.__version__
    assert capabilities.api_version == "1.0.0"
    assert capabilities.capabilities == _KERNEL_CAPABILITIES_V1
    assert pkv_kernel.KERNEL_CAPABILITIES == _KERNEL_CAPABILITIES_V1
    assert pkv_kernel.contracts.KERNEL_CAPABILITIES == _KERNEL_CAPABILITIES_V1
    assert (
        pkv_kernel.contracts.get_kernel_capabilities(pkv_kernel.__version__)
        == capabilities
    )
    assert capabilities.python_requires == ">=3.11,<3.13"
    assert capabilities.supported_platforms == ("Windows",)
    assert (
        pkv_kernel.require_kernel_compatibility(
            minimum_sdk_version="0.8.0",
            maximum_sdk_version="0.8.1",
            required_capabilities=(
                "kernel.archive.v1",
                "kernel.configuration-snapshot-reload.v1",
            ),
        )
        == capabilities
    )

    with pytest.raises(pkv_kernel.KernelCompatibilityError, match="below"):
        pkv_kernel.require_kernel_compatibility(minimum_sdk_version="0.8.2")
    with pytest.raises(pkv_kernel.KernelCompatibilityError, match="missing"):
        pkv_kernel.require_kernel_compatibility(
            required_capabilities=("kernel.unknown.v1",)
        )


def test_runtime_support_requires_windows_and_supported_python(monkeypatch) -> None:
    import pkv_kernel.contracts as contracts

    monkeypatch.setattr(contracts.platform, "system", lambda: "Windows")
    monkeypatch.setattr(contracts.sys, "version_info", (3, 11, 0, "final", 0))
    assert pkv_kernel.runtime_is_supported() is True

    monkeypatch.setattr(contracts.platform, "system", lambda: "Linux")
    assert pkv_kernel.runtime_is_supported() is False

    monkeypatch.setattr(contracts.platform, "system", lambda: "Windows")
    monkeypatch.setattr(contracts.sys, "version_info", (3, 13, 0, "final", 0))
    assert pkv_kernel.runtime_is_supported() is False


def test_kernel_owns_cross_store_delete_vector_callback() -> None:
    calls: list[tuple[str, int]] = []
    vector_store = SimpleNamespace(
        delete_vectors_for_entry=lambda knowledge_id: calls.append(
            ("vector", knowledge_id)
        )
    )

    def delete(knowledge_id, *, vector_operation):
        calls.append(("coordinator", knowledge_id))
        vector_operation(knowledge_id)
        return "deleted"

    application = SimpleNamespace(
        vector_store=vector_store,
        storage_coordinator=SimpleNamespace(delete=delete),
        _write_lease_scope=lambda: nullcontext(),
        _audit_mutation=lambda *_args, **_kwargs: nullcontext(),
        _finish_mutation_audit=lambda *_args, **_kwargs: None,
    )
    kernel = KnowledgeKernel._from_application(application)

    assert kernel.delete_entry(41) == "deleted"
    assert calls == [("coordinator", 41), ("vector", 41)]


def test_kernel_reprobes_absent_vector_index_then_uses_new_index_for_delete() -> None:
    """Absence is not cached across archive-created index state transitions."""

    calls: list[tuple[str, int]] = []
    created_index = SimpleNamespace(
        delete_vectors_for_entry=lambda knowledge_id: calls.append(("vector", knowledge_id))
    )
    readonly_candidates = iter((None, created_index))
    # ``SimpleNamespace`` does not invoke descriptor properties, so use a
    # minimal application object whose property mirrors KnowledgeApplication.
    class Application:
        _write_lease_scope = staticmethod(nullcontext)
        _audit_mutation = staticmethod(lambda *_args, **_kwargs: nullcontext())
        _finish_mutation_audit = staticmethod(lambda *_args, **_kwargs: None)

        @property
        def readonly_vector_store(self):
            return next(readonly_candidates)

        @property
        def vector_store(self):
            return created_index

    kernel = KnowledgeKernel._from_application(Application())

    assert kernel.has_vector_index() is False
    kernel.delete_vectors_for_entry(73)
    assert calls == [("vector", 73)]


def test_kernel_has_vector_index_never_opens_writer_port() -> None:
    """Read predicates must not create vector writer locks or recovery state."""

    class Application:
        @property
        def readonly_vector_store(self):
            return object()

        @property
        def vector_store(self):
            raise AssertionError("has_vector_index must use strict readonly access")

    assert KnowledgeKernel._from_application(Application()).has_vector_index() is True


def test_knowledge_kernel_rejects_direct_public_construction() -> None:
    """The public class is a return/type port, never an application constructor."""

    with pytest.raises(TypeError, match="factory-only"):
        KnowledgeKernel(object())
