"""Step-26 CLI integration: advisory knowledge never changes source intent."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from rca.cli.main import app

from .conftest import make_project

runner = CliRunner()


def test_knowledge_cli_list_search_show_and_advisory_suggest(tmp_path):
    config = make_project(tmp_path, name="knowledge-cli")
    output = config.parent / "output"
    listed = runner.invoke(app, ["knowledge", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    assert listed.output.lstrip().startswith("{")
    list_data = json.loads(listed.output)
    assert list_data["kind"] == "rca_knowledge_list"
    assert any(item["id"] == "K-BUILTIN-CLOCK-DEFINITION" for item in list_data["items"])

    searched = runner.invoke(app, ["knowledge", "search", "false path", "--json"])
    assert searched.exit_code == 0, searched.output
    search_data = json.loads(searched.output)
    assert search_data["suggestions"][0]["knowledge_item_id"] == "K-BUILTIN-FALSE-PATH"
    assert search_data["suggestions"][0]["acceptance_state"] == "NOT_ACCEPTED"

    shown = runner.invoke(app, ["knowledge", "show", "K-BUILTIN-CLOCK-DEFINITION", "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["item"]["constraint_template"] is None

    suggested = runner.invoke(app, ["knowledge", "suggest", str(config), "--json"])
    assert suggested.exit_code == 0, suggested.output
    suggestion_data = json.loads(suggested.output)
    assert suggestion_data["advisory"] is True
    assert suggestion_data["acceptance"] == "No suggestion was accepted or written to the project."
    artifact = output / "knowledge_suggestions.json"
    assert artifact.is_file()
    assert json.loads(artifact.read_text(encoding="utf-8"))["advisory"] is True
