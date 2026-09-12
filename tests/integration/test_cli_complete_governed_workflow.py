"""Step-34/44 user-facing read-only complete workflow command."""

from __future__ import annotations

import importlib
import json

from typer.testing import CliRunner

from rca.web import create_app

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


def test_replay_evidence_cli_is_read_only_and_declares_no_automatic_replay(tmp_path):
    config = make_project(tmp_path, name="replay-evidence")
    result = runner.invoke(cli.app, ["replay-evidence", str(config), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["kind"] == "rca_replay_evidence"
    assert data["automatic_replay_supported"] is False
    assert data["replay_readiness"] == "IDENTITY_COMPLETE_REPLAY_UNPLANNED"
    assert not list(config.parent.glob("*.sqlite"))


def test_projection_report_artifacts_are_opt_in_sandboxed_and_dashboard_readable(tmp_path):
    config = make_project(tmp_path, name="dashboard-projections")
    workflow = runner.invoke(cli.app, ["run", str(config), "--report", "workflow_report.json", "--json"])
    replay = runner.invoke(cli.app, ["replay-evidence", str(config), "--report", "replay_evidence.json", "--json"])
    assert workflow.exit_code == replay.exit_code == 0
    results = config.parent / "output"
    assert (results / "workflow_report.json").is_file()
    assert (results / "replay_evidence.json").is_file()
    dashboard = create_app(results)
    workflow_endpoint = next(route.endpoint for route in dashboard.routes if route.path == "/api/workflow")
    replay_endpoint = next(route.endpoint for route in dashboard.routes if route.path == "/api/replay-evidence")
    page_endpoint = next(route.endpoint for route in dashboard.routes if route.path == "/")
    assert workflow_endpoint()["kind"] == "rca_constraint_workflow"
    assert replay_endpoint()["kind"] == "rca_replay_evidence"
    escaped = page_endpoint()
    assert "const esc = value" in escaped
    bad = runner.invoke(cli.app, ["run", str(config), "--report", "../workflow_report.json", "--json"])
    assert bad.exit_code == 2
    assert not (config.parent / "workflow_report.json").exists()
