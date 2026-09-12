"""Step-30 report-only CLI lineage and explicit semantic-diff boundaries."""

from __future__ import annotations

import importlib
import json

from typer.testing import CliRunner

from rca.constraint_model import ConstraintSet
from rca.utils.enums import SourceKind

from .conftest import make_project

cli = importlib.import_module("rca.cli.main")
runner = CliRunner()


def test_lineage_cli_requires_explicit_snapshot_mode_and_never_writes_state(tmp_path):
    config = make_project(tmp_path, name="lineage-cli")
    missing = runner.invoke(cli.app, ["lineage", str(config), "--json"])
    assert missing.exit_code == 2
    assert json.loads(missing.output)["status"] == "INVALID"

    before = ConstraintSet(name="lineage-cli")
    clock = before.create_clock("clk", 10e-9, source="clk", source_kind=SourceKind.USER)
    after = before.clone()
    after.get(clock.id).values["period"] = 8e-9
    before_path = config.parent / "before-ucm.json"
    after_path = config.parent / "after-ucm.json"
    before_path.write_text(before.to_canonical_json(), encoding="utf-8")
    after_path.write_text(after.to_canonical_json(), encoding="utf-8")
    before_text = before_path.read_text(encoding="utf-8")
    after_text = after_path.read_text(encoding="utf-8")
    output_dir = config.parent / "output"
    assert not output_dir.exists()

    current_args = ["lineage", str(config), "--ucm", str(after_path), "--json"]
    mixed = runner.invoke(cli.app, [*current_args, "--before", str(before_path), "--after", str(after_path)])
    incomplete = runner.invoke(cli.app, ["lineage", str(config), "--before", str(before_path), "--json"])
    assert mixed.exit_code == incomplete.exit_code == 2
    assert json.loads(mixed.output)["status"] == json.loads(incomplete.output)["status"] == "INVALID"

    current_first = runner.invoke(cli.app, current_args)
    current_second = runner.invoke(cli.app, current_args)
    assert current_first.exit_code == current_second.exit_code == 0, current_first.output
    assert current_first.output == current_second.output
    current = json.loads(current_first.output)
    assert current["kind"] == "rca_constraint_lineage"
    assert current["change_set"] is None

    diff_args = ["lineage", str(config), "--before", str(before_path), "--after", str(after_path), "--json"]
    diff_first = runner.invoke(cli.app, diff_args)
    diff_second = runner.invoke(cli.app, diff_args)
    assert diff_first.exit_code == diff_second.exit_code == 0, diff_first.output
    assert diff_first.output == diff_second.output
    diff = json.loads(diff_first.output)
    assert any(item["kind"] == "MODIFIED" for item in diff["change_set"]["changes"])

    assert before_path.read_text(encoding="utf-8") == before_text
    assert after_path.read_text(encoding="utf-8") == after_text
    assert not output_dir.exists()
    assert not list(config.parent.glob("*.sdc"))
    assert not list(config.parent.glob("*.sqlite"))
