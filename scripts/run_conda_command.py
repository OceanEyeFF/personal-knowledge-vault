#!/usr/bin/env python3
"""Execute a JSON-encoded argv vector without Windows PowerShell 5.1 re-quoting."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--args-file", required=True, type=Path)
    return parser.parse_args()


def _load_command(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("命令参数文件必须包含非空 JSON 数组")
    if any(not isinstance(argument, str) for argument in payload):
        raise ValueError("命令参数必须全部为字符串")
    if not payload[0]:
        raise ValueError("命令名不能为空")
    return payload


def main() -> int:
    args = _parse_args()
    try:
        command = _load_command(args.args_file)
    except (OSError, ValueError, json.JSONDecodeError):
        print("错误: 无法读取安全命令参数文件")
        return 2

    try:
        completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    except OSError:
        print("错误: 目标命令启动失败")
        return 1
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
