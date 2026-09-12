"""Step-45 canonical offline E2E demo contract."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_governed_workflow_demo_runs_offline_with_honest_boundaries(tmp_path):
    demo = tmp_path / "governed_workflow"
    shutil.copytree(ROOT / "examples" / "governed_workflow", demo)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run([sys.executable, str(demo / "demo.py")], cwd=demo, env=env,
                            capture_output=True, text=True, shell=False, check=False, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"status": "EDA_UNAVAILABLE"' in result.stdout
    assert "Status: MOCK" in result.stdout
    assert '"automatic_replay_supported": false' in result.stdout
    assert '"signoff": "not claimed"' in result.stdout
    assert (demo / "output" / "workflow_report.json").is_file()
    assert (demo / "output" / "replay_evidence.json").is_file()
