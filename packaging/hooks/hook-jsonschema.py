"""Collect only jsonschema runtime data, excluding upstream test/benchmark assets."""

from PyInstaller.utils.hooks import collect_data_files, copy_metadata


datas = collect_data_files(
    "jsonschema",
    excludes=["benchmarks/**", "tests/**"],
)
datas += copy_metadata("jsonschema", recursive=False)
