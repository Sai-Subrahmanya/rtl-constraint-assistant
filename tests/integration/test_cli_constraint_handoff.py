"""Step-33 CLI target handoff boundary contracts."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

from typer.testing import CliRunner

from .test_cli_constraint_release import _approved_review

cli = importlib.import_module("rca.cli.main")
runner = CliRunner()


def _package(tmp_path: Path) -> tuple[Path, Path]:
    config, ucm, review, policy = _approved_review(tmp_path)
    package = tmp_path / "package"
    result = runner.invoke(cli.app, [
        "release", str(config), "--ucm", str(ucm), "--review", str(review), "--policy", str(policy),
        "--decision", "RELEASE", "--package-dir", str(package), "--json",
    ])
    assert result.exit_code == 0, result.output
    return config, package


def test_handoff_cli_assesses_and_prepares_without_claiming_execution(tmp_path: Path):
    config, package = _package(tmp_path)
    inspected = runner.invoke(cli.app, ["handoff", str(config), "--release", str(package), "--target", "GENERIC", "--json"])
    prepared = runner.invoke(cli.app, ["handoff", str(config), "--release", str(package), "--target", "GENERIC",
                                       "--prepare", "--json"])
    assert inspected.exit_code == prepared.exit_code == 0, inspected.output
    assert json.loads(inspected.output)["handoff"]["status"] == "HANDOFF_READY"
    body = json.loads(prepared.output)
    assert body["status"] == "HANDOFF_PREPARED"
    assert body["actual_external_tool_executed"] is False


def test_handoff_cli_vendor_no_sdc_and_execute_boundary_fail_closed(tmp_path: Path):
    config, package = _package(tmp_path)
    vendor = runner.invoke(cli.app, ["handoff", str(config), "--release", str(package), "--target", "SYNOPSYS", "--json"])
    execute = runner.invoke(cli.app, ["handoff", str(config), "--release", str(package), "--target", "GENERIC",
                                      "--prepare", "--execute", "--json"])
    assert vendor.exit_code == execute.exit_code == 2
    assert json.loads(vendor.output)["handoff"]["status"] == "FAILED"
    assert json.loads(execute.output)["status"] == "UNAVAILABLE"
