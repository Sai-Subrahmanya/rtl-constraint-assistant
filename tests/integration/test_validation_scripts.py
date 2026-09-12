"""Black-box checks for non-mutating setup, EDA, and regression helpers."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _run(relative: str, *args: str):
    return subprocess.run(
        [sys.executable, str(ROOT / relative), *args], cwd=ROOT, text=True,
        capture_output=True, check=False,
    )


def test_setup_and_eda_diagnostics_classify_optional_tool_absence_without_installing():
    setup = _run("scripts/setup/verify_environment.py", "--json")
    assert setup.returncode == 0, setup.stderr
    setup_data = json.loads(setup.stdout)
    assert setup_data["required_ready"] is True
    assert setup_data["mock_only_supported"] is True
    assert {item["status"] for item in setup_data["optional_tools"]} <= {"AVAILABLE", "MISSING"}

    eda = _run("scripts/eda/diagnose_eda.py", "--json")
    assert eda.returncode == 0, eda.stderr
    eda_data = json.loads(eda.stdout)
    assert eda_data["mock_only_supported"] is True
    assert eda_data["tools"]["yosys"]["probe_only"] is True
    assert eda_data["tools"]["opensta"]["probe_only"] is True
    assert eda_data["real_yosys_opensta_flow_expected_runnable"] is False


def test_regression_runner_lists_real_pytest_gates_in_documented_order():
    result = _run("scripts/regression/run_regression.py", "--list")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].startswith("core:")
    assert lines[1] == "golden: tests/golden"
    assert lines[2] == "integration: tests/integration"
    assert lines[3] == "stress: tests/stress"


def test_regression_runner_preserves_failure_and_parses_pytest_failure_order(tmp_path):
    fake_python = tmp_path / "failing-pytest"
    fake_python.write_text(
        "#!/bin/sh\n"
        "echo 'collected 3 items'\n"
        "echo '========================= 1 failed, 2 passed in 0.12s ========================='\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    result = _run("scripts/regression/run_regression.py", "--python", str(fake_python), "--suite", "golden")
    assert result.returncode == 1
    assert re.search(r"golden\s+3\s+2\s+1\s+0\s+0\s+0\.12\s+1", result.stdout)
