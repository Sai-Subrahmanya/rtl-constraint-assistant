"""Step-32 deterministic CLI release and stateless package-verification contracts."""

from __future__ import annotations

import importlib
import json

from typer.testing import CliRunner

from rca.constraint_model import ConstraintSet
from rca.utils.enums import SourceKind

from .conftest import make_project

cli = importlib.import_module("rca.cli.main")
runner = CliRunner()


def _approved_review(tmp_path):
    config = make_project(tmp_path, name="release-cli")
    cset = ConstraintSet(name="release-cli", created_at="2000-01-01T00:00:00+00:00")
    cset.create_clock("clk", 10e-9, source="clk", source_kind=SourceKind.USER)
    ucm = config.parent / "reviewed-ucm.json"
    ucm.write_text(cset.to_canonical_json(), encoding="utf-8")
    review_policy = config.parent / "review-policy.json"
    review_policy.write_text(json.dumps({
        "require_readiness": False, "require_validation_evidence": False,
        "require_coverage_complete": False, "allow_approval_with_warnings": True,
    }), encoding="utf-8")
    reviewed = runner.invoke(cli.app, [
        "review", str(config), "--ucm", str(ucm), "--policy", str(review_policy),
        "--decision", "APPROVE_WITH_WARNINGS", "--reviewer", "alice", "--comment", "reviewed", "--json",
    ])
    assert reviewed.exit_code == 0, reviewed.output
    review_path = config.parent / "approved-review.json"
    review_path.write_text(reviewed.output, encoding="utf-8")
    release_policy = config.parent / "release-policy.json"
    release_policy.write_text(json.dumps({
        "require_review": True, "allow_review_with_warnings": True,
        "require_readiness": False, "require_validation_evidence": False,
        "require_complete_coverage": False, "require_lineage": False,
        "allow_release_with_warnings": True,
    }), encoding="utf-8")
    return config, ucm, review_path, release_policy


def test_release_cli_assesses_explicit_review_without_mutating_ucm_or_auto_release(tmp_path):
    config, ucm, review, policy = _approved_review(tmp_path)
    before = ucm.read_text(encoding="utf-8")
    args = ["release", str(config), "--ucm", str(ucm), "--review", str(review),
            "--policy", str(policy), "--json"]
    first = runner.invoke(cli.app, args)
    second = runner.invoke(cli.app, args)
    assert first.exit_code == second.exit_code == 0, first.output
    assert first.output == second.output
    payload = json.loads(first.output)
    assert payload["release"]["status"] in {"READY", "CANDIDATE"}
    assert payload["release"]["decision"] is None
    assert payload["release"]["external_eda_signoff"] == "EXTERNAL_EDA_SIGNOFF_UNKNOWN"
    assert ucm.read_text(encoding="utf-8") == before
    assert not list(config.parent.glob("*.sdc"))
    assert not list(config.parent.glob("*.sqlite"))


def test_release_cli_requires_explicit_release_then_builds_and_verifies_package(tmp_path):
    config, ucm, review, policy = _approved_review(tmp_path)
    package = config.parent / "release-package"
    result = runner.invoke(cli.app, [
        "release", str(config), "--ucm", str(ucm), "--review", str(review),
        "--policy", str(policy), "--decision", "RELEASE", "--releaser", "bob",
        "--comment", "explicit release", "--package-dir", str(package), "--json",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["release"]["status"] == "RELEASED_WITH_WARNINGS"
    assert payload["release"]["decision"]["actor"]["identity"] == "bob"
    assert payload["package"]["kind"] == "rca_constraint_release_package"
    verified = runner.invoke(cli.app, ["release-verify", str(package), "--json"])
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.output)["status"] == "VERIFIED"


def test_release_cli_rejects_auto_action_bad_artifacts_and_package_before_release(tmp_path):
    config, ucm, review, policy = _approved_review(tmp_path)
    for extra in (["--decision", "AUTO_RELEASE"], ["--artifact", "no-separator"],
                  ["--package-dir", str(config.parent / "candidate-package")]):
        result = runner.invoke(cli.app, [
            "release", str(config), "--ucm", str(ucm), "--review", str(review),
            "--policy", str(policy), *extra, "--json",
        ])
        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["status"] == "INVALID"


def test_release_verify_cli_fails_closed_for_missing_or_corrupted_package(tmp_path):
    missing = runner.invoke(cli.app, ["release-verify", str(tmp_path / "missing"), "--json"])
    assert missing.exit_code == 2
    assert json.loads(missing.output)["status"] == "INVALID"
