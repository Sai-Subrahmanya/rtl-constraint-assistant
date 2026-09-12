"""Real RCA CLI integration workflows using parser + explicitly mock EDA."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from rca.cli.main import app

from .conftest import make_project

runner = CliRunner()


def _invoke(*args: str):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result


def test_simple_counter_cli_pipeline_and_artifact_authority(tmp_path):
    """Exercise init through report without replacing any RCA boundary."""
    initialized = tmp_path / "initialized"
    init_result = _invoke("init", str(initialized), "--name", "initialized", "--top", "tiny")
    assert "Initialized RCA project" in init_result.output
    assert (initialized / "rca.project.yaml").is_file()

    config = make_project(tmp_path, workers=1)
    output = config.parent / "output"
    human_analysis = _invoke("analyze", str(config))
    assert "RCA Analysis" in human_analysis.output
    json_analysis = _invoke("analyze", str(config), "--json")
    assert json.loads(json_analysis.output)["design"]["top"] == "counter"
    assert "Inference report" in _invoke("infer", str(config)).output
    infer_json = _invoke("infer", str(config), "--json")
    infer_payload = json.loads(infer_json.output)
    assert infer_payload["constraints_added"] == 0
    assert "structural_facts" in infer_payload and "candidates" in infer_payload
    assert "ambiguous_candidates" in infer_payload
    assert "unsupported_or_rejected_candidates" in infer_payload
    assert all(item["acceptance_state"] == "NOT_ACCEPTED" for item in infer_payload["candidates"])
    assert infer_json.output == _invoke("infer", str(config), "--json").output
    assert "SDC GENERATION" in _invoke("generate", str(config), "--backend", "generic").output
    generated = output / "design.generic.sdc"
    assert generated.is_file()
    assert "Validation Report" in _invoke("validate", str(config)).output
    assert "Coverage Report" in _invoke("coverage", str(config)).output

    compare_human = _invoke("compare", str(config), "--a", str(generated), "--b", str(generated))
    assert "SDC COMPARISON" in compare_human.output
    # JSON comparison behavior is covered in the independent semantic CLI
    # test; this workflow verifies the same production command accepts it.
    _invoke("compare", str(config), "--a", str(generated), "--b", str(generated), "--json")

    sta = _invoke("run-sta", str(config), "--backend", "mock")
    assert "Status: MOCK" in sta.output
    optimized = _invoke("optimize", str(config), "--backend", "mock")
    assert "Execution ledger:" in optimized.output
    for name in (
        "optimizer_state.json", "optimizer_execution_ledger.json",
        "optimizer_execution_manifest.json", "candidates.jsonl", "pareto_frontier.json",
    ):
        assert (output / name).is_file(), name

    ledger_human = _invoke("history", "--optimization-ledger", "--output-dir", str(output))
    assert "Optimizer execution ledger" in ledger_human.output
    ledger_json = _invoke("history", "--optimization-ledger", "--output-dir", str(output), "--json")
    ledger = json.loads(ledger_json.output)
    assert ledger["entries"][0]["candidate_id"] == "C000"
    assert ledger["artifact_path"] == str(output / "optimizer_execution_ledger.json")
    history_json = _invoke("history", "--output-dir", str(output), "--json")
    assert isinstance(json.loads(history_json.output), list)
    legacy_json = _invoke("history", "--import-legacy", "--output-dir", str(output), "--json")
    assert "imported" in json.loads(legacy_json.output)
    report = _invoke("report", str(config))
    assert "RTL Constraint Assistant" in report.output


def test_mcmm_cli_mock_flow_preserves_per_scenario_artifacts_and_history(tmp_path):
    config = make_project(tmp_path, name="mcmm", mcmm=True, workers=2)
    output = config.parent / "output"
    run = _invoke("run-sta", str(config), "--backend", "mock")
    assert "Per-scenario QoR" in run.output
    assert "FAST" in run.output and "SLOW" in run.output
    report = json.loads((output / "mcmm_report.json").read_text(encoding="utf-8"))
    assert list(report["scenario_results"]) == ["FAST", "SLOW"]
    assert all(item["status"] == "feasible" for item in report["scenario_results"].values())
    # Optimizer run is still mock-only and reports a ledger even if this
    # particular real RTL fixture has no eligible generated mutation.
    optimize = _invoke("optimize", str(config), "--backend", "mock")
    assert "Execution ledger:" in optimize.output
    ledger = json.loads((output / "optimizer_execution_ledger.json").read_text(encoding="utf-8"))
    assert ledger["workers"] == 2
    assert ledger["entries"][0]["eda_runs_consumed"] == 2
