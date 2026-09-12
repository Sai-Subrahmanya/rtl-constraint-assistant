"""Step-34/44 user-facing read-only complete workflow command."""

from __future__ import annotations

import importlib
import json

from typer.testing import CliRunner

from .conftest import make_project

cli = importlib.import_module("rca.cli.main")
runner = CliRunner()


def test_run_projects_existing_stages_and_stops_for_governance(tmp_path):
    config = make_project(tmp_path, name="governed-run")
    first = runner.invoke(cli.app, ["run", str(config), "--json"])
    second = runner.invoke(cli.app, ["run", str(config), "--json"])
    assert first.exit_code == second.exit_code == 0, first.output
    assert first.output == second.output
    data = json.loads(first.output)
    names = {item["name"]: item["status"] for item in data["stages"]}
    assert names["canonical_ucm"] == "COMPLETE"
    assert names["review"] == "REVIEW_REQUIRED"
    assert names["eda"] == "EDA_UNAVAILABLE"
    assert data["summary"]["next_action"]
    assert not list(config.parent.glob("*.sqlite"))
    assert not list(config.parent.glob("*.sdc"))
