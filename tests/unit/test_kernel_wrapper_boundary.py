"""Architecture contracts for the headless Kernel public surface."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from src.kernel import KnowledgeKernel


PROJECT_ROOT = Path(__file__).resolve().parents[2]
HEADLESS_ROOTS = (PROJECT_ROOT / "src" / "kernel", PROJECT_ROOT / "pkv_kernel")
_FORBIDDEN_IMPORT_PREFIXES = ("src.gui", "pkv_gui", "PySide6", "qasync")


def _source_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    return imports


def test_kernel_public_surface_has_no_framework_or_wrapper_dependency() -> None:
    violations = [
        f"{path.relative_to(PROJECT_ROOT)} -> {module}"
        for root in HEADLESS_ROOTS
        for path in root.rglob("*.py")
        for module in _source_imports(path)
        if module.startswith(_FORBIDDEN_IMPORT_PREFIXES)
    ]

    assert violations == []


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
    )
    kernel = KnowledgeKernel(application)

    assert kernel.delete_entry(41) == "deleted"
    assert calls == [("coordinator", 41), ("vector", 41)]
