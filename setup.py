"""Build hooks for the local-only ``pkv-kernel`` wheel.

K1b intentionally packages the headless Kernel, not a release Artifact.  The
runtime needs a small immutable resource tree, but those files live at the
repository root during development.  Copying an explicit allowlist into the
wheel keeps installed execution independent of a neighbouring source checkout
without ever including ``config/local.yaml`` or user data.
"""

from __future__ import annotations

from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py as _BuildPy


# This is deliberately a file-by-file manifest, rather than a glob.  Adding a
# resource to the installed Kernel is an API/payload decision and must be
# reviewed together with its wheel contract test.
KERNEL_RESOURCE_MANIFEST: tuple[tuple[str, str], ...] = (
    ("config/config.yaml", "config/config.yaml"),
    ("config/custom_dict.txt", "config/custom_dict.txt"),
    ("config/workflows/archive-text.yaml", "config/workflows/archive-text.yaml"),
    ("config/workflows/archive-url.yaml", "config/workflows/archive-url.yaml"),
    ("scripts/migrations/001_initial_schema.sql", "scripts/migrations/001_initial_schema.sql"),
    ("scripts/migrations/002_add_cli_tables.sql", "scripts/migrations/002_add_cli_tables.sql"),
    ("scripts/migrations/004_add_chat_sessions.sql", "scripts/migrations/004_add_chat_sessions.sql"),
    ("scripts/migrations/005_add_review_system.sql", "scripts/migrations/005_add_review_system.sql"),
    ("scripts/migrations/006_add_relations_foundation.sql", "scripts/migrations/006_add_relations_foundation.sql"),
    ("scripts/migrations/007_add_timeline_time_fields.sql", "scripts/migrations/007_add_timeline_time_fields.sql"),
    ("scripts/migrations/008_align_fts_contract.sql", "scripts/migrations/008_align_fts_contract.sql"),
    ("scripts/migrations/009_repair_fts_storage_contract.sql", "scripts/migrations/009_repair_fts_storage_contract.sql"),
    ("scripts/migrations/010_add_storage_operation_commits.sql", "scripts/migrations/010_add_storage_operation_commits.sql"),
    ("scripts/migrations/011_add_ai_automation_ledger.sql", "scripts/migrations/011_add_ai_automation_ledger.sql"),
    ("src/ai/prompts/extract_tags.txt", "src/ai/prompts/extract_tags.txt"),
    ("src/ai/prompts/summarize.txt", "src/ai/prompts/summarize.txt"),
)


class BuildKernelResources(_BuildPy):
    """Build only Kernel modules plus the runtime resources they require."""

    # ``src`` remains a namespace anchor for the headless Core packages, but
    # its top-level ``main.py`` is the repository CLI entrypoint.  Package
    # discovery treats it as a module of the ``src`` package unless it is
    # explicitly removed here; shipping it would let a Kernel wheel dynamically
    # import ``src.cli`` despite the K1a/K1b public-boundary contract.
    _EXCLUDED_PACKAGE_MODULES = frozenset({("src", "main")})

    def find_package_modules(self, package: str, package_dir: str):
        modules = super().find_package_modules(package, package_dir)
        return [
            module
            for module in modules
            if (module[0], module[1]) not in self._EXCLUDED_PACKAGE_MODULES
        ]

    def run(self) -> None:
        super().run()

        # ``build_py`` deliberately reuses its build directory.  A checkout
        # which produced an earlier wheel can therefore retain a previously
        # copied ``src/main.py`` even after module discovery stops selecting it.
        # Remove only this exact forbidden output before bdist_wheel collects
        # files; source files and all supported Core package modules remain
        # untouched.
        for package, module in self._EXCLUDED_PACKAGE_MODULES:
            stale_output = Path(self.build_lib).joinpath(
                *package.split("."), f"{module}.py"
            )
            if stale_output.is_file():
                stale_output.unlink()

        project_root = Path(__file__).resolve().parent
        resources_root = Path(self.build_lib) / "pkv_kernel" / "_resources"
        for source, relative_destination in self._resource_files(project_root):
            destination = resources_root / relative_destination
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    @staticmethod
    def _resource_files(project_root: Path) -> list[tuple[Path, Path]]:
        required_files = [
            (project_root / source, Path(relative_destination))
            for source, relative_destination in KERNEL_RESOURCE_MANIFEST
        ]
        missing = [str(path) for path, _ in required_files if not path.is_file()]
        if missing:
            raise RuntimeError("missing Kernel wheel resources: " + ", ".join(missing))
        return required_files


setup(cmdclass={"build_py": BuildKernelResources})
