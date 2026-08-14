"""Static build-graph contracts that do not require PyInstaller at test time."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = PROJECT_ROOT / "packaging" / "pkv.spec"
MANIFEST_PATH = PROJECT_ROOT / "packaging" / "runtime-resources.json"
JIEBA_HOOK = PROJECT_ROOT / "packaging" / "hooks" / "hook-jieba.py"
JSONSCHEMA_HOOK = PROJECT_ROOT / "packaging" / "hooks" / "hook-jsonschema.py"
pytestmark = pytest.mark.packaging_contract

BUILD_ONLY_MODULES = frozenset(
    {
        "_distutils_hack",
        "mypy",
        "mypy_extensions",
        "packaging",
        "pydantic.mypy",
        "pydantic.v1.mypy",
        "setuptools",
        "wheel",
    }
)


def _assigned_call(tree: ast.AST, name: str) -> ast.Call:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            if isinstance(node.value, ast.Call):
                return node.value
    raise AssertionError(f"missing assigned call: {name}")


def _keyword_constant(call: ast.Call, name: str):
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    raise AssertionError(f"missing constant keyword: {name}")


def _analysis_member_names(toc: object) -> set[str]:
    """Return every destination/module name recorded in an Analysis TOC."""

    names: set[str] = set()
    pending = [toc]
    while pending:
        value = pending.pop()
        if isinstance(value, (list, tuple)):
            if (
                len(value) == 3
                and isinstance(value[0], str)
                and isinstance(value[1], str)
                and value[2] in {"BINARY", "DATA", "EXTENSION", "PYMODULE", "PYSOURCE"}
            ):
                names.add(value[0].replace("\\", "/"))
            pending.extend(value)
        elif isinstance(value, dict):
            pending.extend(value.items())
    return names


def _has_build_only_module_prefix(name: str) -> bool:
    normalized = name.replace("\\", "/").casefold()
    dotted = normalized.replace("/", ".")
    for module_name in BUILD_ONLY_MODULES:
        prefix = module_name.casefold()
        if dotted == prefix or dotted.startswith(f"{prefix}."):
            return True
    return False


def _is_build_only_payload_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    contents_prefix = "_internal/"
    if normalized.casefold().startswith(contents_prefix):
        normalized = normalized[len(contents_prefix) :]
    return _has_build_only_module_prefix(normalized)


def test_spec_has_one_analysis_two_headless_exes_and_one_shared_collect() -> None:
    tree = ast.parse(SPEC_PATH.read_text(encoding="utf-8"), filename=str(SPEC_PATH))

    analysis_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Analysis"
    ]
    assert len(analysis_calls) == 1

    expectations = {
        "pkv_cli": ("pkv", True),
        "pkv_mcp": ("pkv-mcp", True),
    }
    for variable, (name, console) in expectations.items():
        call = _assigned_call(tree, variable)
        assert isinstance(call.func, ast.Name) and call.func.id == "EXE"
        assert _keyword_constant(call, "name") == name
        assert _keyword_constant(call, "console") is console
        assert isinstance(call.args[0], ast.Name) and call.args[0].id == "pyz"
        assert (
            isinstance(call.args[2], ast.Name) and call.args[2].id == "python_options"
        )

    collect = _assigned_call(tree, "coll")
    assert isinstance(collect.func, ast.Name) and collect.func.id == "COLLECT"
    assert _keyword_constant(collect, "name") == "pkv"
    assert [
        argument.id for argument in collect.args[:2] if isinstance(argument, ast.Name)
    ] == [
        "pkv_cli",
        "pkv_mcp",
    ]


def test_manifest_declares_headless_dynamic_imports_and_metadata() -> (
    None
):
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    settings = manifest["pyinstaller"]

    assert manifest["schema_version"] == 2
    assert {
        "src.main",
        "src.cli.commands",
        "src.mcp.server",
        "anyio._backends._asyncio",
        "bs4.builder._lxml",
        "hnswlib",
        "lxml.etree",
        "openai.resources.chat.completions",
    }.issubset(settings["hiddenimports"])
    assert settings["collect_submodules"] == []
    assert settings["copy_metadata"] == ["mcp"]
    assert settings["collect_data_files"] == [
        {"package": "certifi", "includes": ["cacert.pem"]}
    ]
    assert {
        "jsonschema.benchmarks",
        "jsonschema.tests",
        "tests",
        "pytest",
        *BUILD_ONLY_MODULES,
    }.issubset(settings["excludes"])
    assert "requests" not in settings["excludes"]


def test_spec_uses_narrow_collection_helpers_and_versioned_local_hook() -> None:
    spec_source = SPEC_PATH.read_text(encoding="utf-8")
    hook_source = JIEBA_HOOK.read_text(encoding="utf-8")
    jsonschema_hook_source = JSONSCHEMA_HOOK.read_text(encoding="utf-8")

    assert "collect_all" not in spec_source
    assert "collect_all" not in hook_source
    assert "collect_all" not in jsonschema_hook_source
    assert 'collect_data_files("jieba", includes=["dict.txt"])' in hook_source
    assert '"jsonschema"' in jsonschema_hook_source
    assert 'excludes=["benchmarks/**", "tests/**"]' in jsonschema_hook_source
    assert 'copy_metadata("jsonschema", recursive=False)' in jsonschema_hook_source
    assert "runtime-resources.json" in spec_source
    assert 'name="pkv"' in spec_source
    assert '("X utf8", None, "OPTION")' in spec_source
    assert "BUILD_ONLY_MODULE_EXCLUDES" in spec_source
    for module_name in BUILD_ONLY_MODULES:
        assert f'"{module_name}"' in spec_source


def test_real_analysis_and_payload_have_no_build_only_module_members() -> None:
    """Opt-in closure assertion used by the real PyInstaller inventory smoke."""

    analysis_value = os.environ.get("PKV_TEST_PYINSTALLER_ANALYSIS")
    payload_value = os.environ.get("PKV_TEST_PYINSTALLER_PAYLOAD")
    if not analysis_value or not payload_value:
        pytest.skip("real PyInstaller closure paths were not supplied")

    analysis_path = Path(analysis_value)
    payload_root = Path(payload_value)
    assert analysis_path.is_file(), analysis_path
    assert payload_root.is_dir(), payload_root

    toc = ast.literal_eval(analysis_path.read_text(encoding="utf-8"))
    analysis_leaks = sorted(
        name
        for name in _analysis_member_names(toc)
        if _has_build_only_module_prefix(name)
    )
    payload_leaks = sorted(
        path.relative_to(payload_root).as_posix()
        for path in payload_root.rglob("*")
        if path.is_file()
        and _is_build_only_payload_member(path.relative_to(payload_root).as_posix())
    )

    assert analysis_leaks == []
    assert payload_leaks == []
