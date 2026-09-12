"""Offline Step-45 governed RCA demonstration; runs no real EDA or formal tool."""

from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from rca.cli.main import app

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "project.yaml"


def invoke(*arguments: str) -> dict:
    """Run an RCA CLI action locally and fail visibly rather than hiding errors."""
    result = CliRunner().invoke(app, list(arguments))
    print(f"$ rca {' '.join(arguments)}")
    print(result.output)
    if result.exit_code:
        raise SystemExit(result.exit_code)
    return json.loads(result.output) if "--json" in arguments else {}


def main() -> None:
    os.chdir(ROOT)
    workflow = invoke("run", str(CONFIG), "--report", "workflow_report.json", "--json")
    assert workflow["summary"]["next_action"]
    assert any(stage["name"] == "eda" and stage["status"] == "EDA_UNAVAILABLE" for stage in workflow["stages"])

    replay = invoke("replay-evidence", str(CONFIG), "--report", "replay_evidence.json", "--json")
    assert replay["automatic_replay_supported"] is False

    # This is intentionally MOCK evidence and must never be confused with a
    # real synthesis/STA result. It is offline-runnable with no EDA install.
    invoke("run-sta", str(CONFIG), "--backend", "mock")
    # Bounded real-tool readiness discovery only. It executes no real EDA.
    invoke("doctor", str(CONFIG), "--backend", "yosys_opensta", "--json")

    print("Demo boundaries: workflow reports no implicit approval/release; mock EDA is explicit; "
          "real EDA/formal are only preflighted and may be unavailable.")


if __name__ == "__main__":
    main()
