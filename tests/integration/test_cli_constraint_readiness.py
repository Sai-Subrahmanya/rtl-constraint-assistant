"""Step-29 CLI boundary: supplied UCM, deterministic report, no mutations."""

from __future__ import annotations

import importlib
import json

from typer.testing import CliRunner

from rca.constraint_model import ConstraintSet

from .conftest import make_project

cli = importlib.import_module("rca.cli.main")
runner = CliRunner()


def test_readiness_cli_requires_canonical_ucm_and_is_deterministic_report_only(tmp_path):
    config = make_project(tmp_path, name="readiness-cli")
    missing = runner.invoke(cli.app, ["readiness", str(config), "--json"])
    assert missing.exit_code == 2
    assert "--ucm" in missing.output

    snapshot = config.parent / "canonical-ucm.json"
    cset = ConstraintSet(name="readiness-cli")
    snapshot.write_text(cset.to_canonical_json(), encoding="utf-8")
    before_snapshot = snapshot.read_text(encoding="utf-8")
    output_dir = config.parent / "output"
    assert not output_dir.exists()

    args = ["readiness", str(config), "--ucm", str(snapshot), "--json"]
    first = runner.invoke(cli.app, args)
    second = runner.invoke(cli.app, args)
    assert first.exit_code == second.exit_code == 0, first.output
    assert first.output == second.output
    payload = json.loads(first.output)

    assert payload["kind"] == "rca_constraint_readiness"
    assert payload["status"] in {"BLOCKED", "INCOMPLETE", "UNKNOWN", "READY_WITH_WARNINGS", "READY"}
    assert snapshot.read_text(encoding="utf-8") == before_snapshot
    assert not output_dir.exists()
    assert not list(config.parent.glob("*.sdc"))
    assert not list(config.parent.glob("*.sqlite"))
