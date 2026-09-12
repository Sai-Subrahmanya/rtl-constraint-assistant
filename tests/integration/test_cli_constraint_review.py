"""Step-31 CLI review governance boundary and deterministic JSON behavior."""

from __future__ import annotations

import importlib
import json

from typer.testing import CliRunner

from rca.constraint_model import ConstraintSet
from rca.utils.enums import SourceKind

from .conftest import make_project

cli = importlib.import_module("rca.cli.main")
runner = CliRunner()


def test_review_cli_assesses_and_explicitly_decides_without_writing_engineering_state(tmp_path):
    config = make_project(tmp_path, name="review-cli")
    cset = ConstraintSet(name="review-cli", created_at="2000-01-01T00:00:00+00:00")
    clock = cset.create_clock("clk", 10e-9, source="clk", source_kind=SourceKind.USER)
    ucm = config.parent / "reviewed-ucm.json"
    ucm.write_text(cset.to_canonical_json(), encoding="utf-8")
    before = ucm.read_text(encoding="utf-8")
    policy = config.parent / "review-policy.json"
    policy.write_text(json.dumps({
        "require_readiness": False,
        "require_validation_evidence": False,
        "require_coverage_complete": False,
        "allow_approval_with_warnings": True,
    }), encoding="utf-8")
    output_dir = config.parent / "output"
    assert not output_dir.exists()

    inspect_args = ["review", str(config), "--ucm", str(ucm), "--policy", str(policy), "--json"]
    inspected_one = runner.invoke(cli.app, inspect_args)
    inspected_two = runner.invoke(cli.app, inspect_args)
    assert inspected_one.exit_code == inspected_two.exit_code == 0, inspected_one.output
    assert inspected_one.output == inspected_two.output
    inspected = json.loads(inspected_one.output)
    assert inspected["current_status"] == "NEEDS_REVIEW"
    assert inspected["review"]["approval"] is None

    approve_args = [
        "review", str(config), "--ucm", str(ucm), "--policy", str(policy),
        "--decision", "APPROVE_WITH_WARNINGS", "--reviewer", "alice", "--comment", "reviewed", "--json",
    ]
    approved_one = runner.invoke(cli.app, approve_args)
    approved_two = runner.invoke(cli.app, approve_args)
    assert approved_one.exit_code == approved_two.exit_code == 0, approved_one.output
    assert approved_one.output == approved_two.output
    approved = json.loads(approved_one.output)
    assert approved["review"]["status"] == "APPROVED_WITH_WARNINGS"
    assert approved["review"]["approval"]["actor"]["identity"] == "alice"
    assert approved["review"]["external_eda_signoff"] == "EXTERNAL_EDA_SIGNOFF_UNKNOWN"

    invalid = runner.invoke(cli.app, ["review", str(config), "--ucm", str(ucm),
                                      "--decision", "AUTO_APPROVE", "--json"])
    malformed_review = config.parent / "malformed-review.json"
    malformed_review.write_text("{}", encoding="utf-8")
    malformed = runner.invoke(cli.app, ["review", str(config), "--ucm", str(ucm),
                                        "--review", str(malformed_review), "--json"])
    assert invalid.exit_code == malformed.exit_code == 2
    assert json.loads(invalid.output)["status"] == "INVALID"
    assert ucm.read_text(encoding="utf-8") == before
    assert not output_dir.exists()
    assert not list(config.parent.glob("*.sdc"))
    assert not list(config.parent.glob("*.sqlite"))
    assert cset.get(clock.id) is not None
