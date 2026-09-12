"""Step-28 CLI contract: one explicit candidate decision, never apply-all."""

from __future__ import annotations

import importlib
import json

import yaml
from typer.testing import CliRunner

from rca.constraint_model import ConstraintSet

from .conftest import make_project

cli = importlib.import_module("rca.cli.main")

runner = CliRunner()


def _project_without_clock_intent(tmp_path):
    config_path = make_project(tmp_path, name="controlled-cli")
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["constraints"]["user"]["clocks"] = []
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return config_path


def test_apply_cli_requires_candidate_and_explicit_decision_and_is_safe_for_invalid_ids(tmp_path):
    config = _project_without_clock_intent(tmp_path)
    missing_candidate = runner.invoke(cli.app, ["apply", str(config), "--decision", "DEFER"])
    assert missing_candidate.exit_code == 2
    assert "--candidate" in missing_candidate.output

    unknown = runner.invoke(
        cli.app,
        ["apply", str(config), "--candidate", "IC-NOT-REAL", "--decision", "ACCEPT", "--json"],
    )
    assert unknown.exit_code == 2
    payload = json.loads(unknown.output)
    assert payload == {
        "blocking_reasons": ["Candidate ID was not found in fresh deterministic inference output."],
        "candidate_id": "IC-NOT-REAL",
        "status": "INVALID",
        "ucm_mutated": False,
    }
    assert not (config.parent / "output" / "applied_constraint_model.json").exists()

    malformed_snapshot = config.parent / "malformed-ucm.json"
    malformed_snapshot.write_text("[]", encoding="utf-8")
    malformed = runner.invoke(cli.app, ["apply", str(config), "--candidate", "IC-NOT-REAL",
                                        "--decision", "ACCEPT", "--ucm", str(malformed_snapshot)])
    assert malformed.exit_code == 2
    assert "Cannot load canonical UCM snapshot" in malformed.output


def test_apply_cli_dry_run_and_accept_are_deterministic_and_only_accept_writes_ucm(tmp_path, monkeypatch):
    config = _project_without_clock_intent(tmp_path)
    original_timing = cli._do_timing

    def controlled_tool_timing(cfg, design):
        timing = original_timing(cfg, design)
        timing.clocks["clk"].period_seconds = 10e-9
        timing.clocks["clk"].source_of_value = "TOOL"
        return timing

    monkeypatch.setattr(cli, "_do_timing", controlled_tool_timing)
    advisory = runner.invoke(cli.app, ["infer", str(config), "--json"])
    assert advisory.exit_code == 0, advisory.output
    report = json.loads(advisory.output)
    candidate = next(item for item in report["candidates"] if item["decision"] == "ACCEPTABLE")
    candidate_id = candidate["id"]

    args = ["apply", str(config), "--candidate", candidate_id, "--decision", "ACCEPT", "--dry-run", "--json"]
    first_preview = runner.invoke(cli.app, args)
    second_preview = runner.invoke(cli.app, args)
    assert first_preview.exit_code == second_preview.exit_code == 0
    assert first_preview.output == second_preview.output
    preview = json.loads(first_preview.output)
    assert preview["status"] == "NOT_APPLIED"
    assert preview["ucm_mutated"] is False
    assert not (config.parent / "output" / "applied_constraint_model.json").exists()
    assert not list((config.parent / "output").glob("*.sdc"))

    output = config.parent / "reviewed-ucm.json"
    accepted = runner.invoke(
        cli.app,
        ["apply", str(config), "--candidate", candidate_id, "--decision", "ACCEPT", "--output", str(output), "--json"],
    )
    assert accepted.exit_code == 0, accepted.output
    receipt = json.loads(accepted.output)
    assert receipt["status"] == "APPLIED"
    assert receipt["ucm_mutated"] is True
    assert receipt["canonical_ucm_output"] == str(output)
    saved = ConstraintSet.from_snapshot_dict(json.loads(output.read_text(encoding="utf-8")), unknown_field_policy="error")
    assert len(saved) == 1

    fresh_advisory = runner.invoke(cli.app, ["infer", str(config), "--ucm", str(output), "--json"])
    assert fresh_advisory.exit_code == 0, fresh_advisory.output
    fresh_id = json.loads(fresh_advisory.output)["candidates"][0]["id"]
    deferred = runner.invoke(
        cli.app,
        ["apply", str(config), "--candidate", fresh_id, "--decision", "DEFER", "--ucm", str(output), "--json"],
    )
    assert deferred.exit_code == 0, deferred.output
    assert json.loads(deferred.output)["status"] == "DEFERRED"
    assert json.loads(output.read_text(encoding="utf-8")) == saved.to_snapshot_dict()
