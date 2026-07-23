from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = PROJECT_ROOT / "scripts" / "run_conda_command.py"


def test_bridge_preserves_quotes_empty_arguments_and_boundaries(tmp_path: Path) -> None:
    assertion = "import sys; assert sys.argv[1:] == ['x\"y', '', 'tail value']"
    args_file = tmp_path / "command.json"
    args_file.write_text(
        json.dumps([sys.executable, "-c", assertion, 'x"y', "", "tail value"]),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(BRIDGE), "--args-file", str(args_file)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_bridge_rejects_non_string_argv(tmp_path: Path) -> None:
    args_file = tmp_path / "invalid.json"
    args_file.write_text(json.dumps(["python", 123]), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(BRIDGE), "--args-file", str(args_file)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "123" not in result.stdout + result.stderr
