"""Reference-level golden checks for stable RCA semantic outputs.

The pre-existing SDC corpus covers canonical textual rendering for clocks,
generated clocks, I/O, clock groups, false paths, and multicycle paths. These
checks cover the remaining cross-subsystem reference outputs without comparing
timestamps, temporary paths, or fresh invocation IDs.
"""

from __future__ import annotations

from pathlib import Path

from rca.constraint_model import ConstraintSet
from rca.equivalence import compare_sdc_text
from rca.mcmm import MCMMEvaluator, build_scenario_matrix
from rca.optimizer import Optimizer
from rca.reports.power import PowerParseStatus, parse_openroad_power_report
from rca.validation import validate as run_validation
from tests.support.workflows import (
    fake_qor,
    normalize_ledger,
    optimizer_config,
    tunable_constraint_set,
)

POWER_FIXTURE = Path(__file__).parent / "reports" / "openroad_report_power_representative.rpt"


def test_reference_power_report_is_explicit_tool_evidence_not_an_estimate():
    parsed = parse_openroad_power_report(POWER_FIXTURE, scenario_id="SLOW", mode="functional", corner="slow")
    assert parsed.status == PowerParseStatus.AVAILABLE.value
    assert parsed.total == 1.33e-3
    assert parsed.dynamic == 1.313e-3
    assert parsed.leakage == 1.3e-5
    assert parsed.provenance()["format"] == "openroad_report_power"
    assert parsed.provenance()["scenario_id"] == "SLOW"


def test_reference_semantic_equivalence_difference_and_unsupported_unknown():
    equivalent = compare_sdc_text(
        "create_clock -name clk -period 10 [get_ports clk]\n",
        "create_clock -period 10ns -name clk [get_ports clk]\n",
    )
    different = compare_sdc_text(
        "create_clock -name clk -period 10 [get_ports clk]\n",
        "create_clock -name clk -period 11 [get_ports clk]\n",
    )
    unknown = compare_sdc_text(
        "set_load 0.5 [get_ports q]\n", "set_load 0.5 [get_ports q]\n",
    )
    assert equivalent.overall_status.value.startswith("EQUIVALENT")
    assert different.overall_status.value in {"DIFFERENT", "NON_EQUIVALENT"}
    assert unknown.overall_status.value == "UNKNOWN"


def test_reference_validation_and_coverage_preserve_conflict_classification():
    cset = ConstraintSet(name="reference")
    cset.create_clock(name="clk", period_seconds=10e-9, source="clk")
    cset.create_clock(name="clk", period_seconds=12e-9, source="clk")
    report = run_validation(cset=cset)
    codes = {issue.code.value for issue in report.errors + report.warnings}
    assert report.coverage is not None
    assert {"CLOCK_MULTIPLE", "CONFLICT_CLOCK_PERIOD"}.issubset(codes)
    assert report.status in {"PASS_WITH_WARNINGS", "BLOCKED", "ERROR"}


def test_reference_mcmm_optimizer_ledger_has_ordered_aggregate_evidence(tmp_path):
    cfg = optimizer_config(tmp_path, workers=2, max_runs=6, mcmm=True)
    cset = tunable_constraint_set(mcmm=True)
    matrix = build_scenario_matrix(cfg, cset)

    def scenario_evaluator(scenario, candidate, work_dir):
        return {
            "qor": fake_qor(candidate.id, scenario=scenario.id),
            "cache_key": f"reference-{candidate.constraint_model_hash}-{scenario.id}",
            "cache_status": "MISS",
            "run_id": f"reference-{candidate.id}-{scenario.id}",
        }

    result = Optimizer(
        cfg,
        evaluate_fn=MCMMEvaluator(matrix, evaluate_scenario=scenario_evaluator,
                                  base_cset=cset, name="reference-fake"),
        work_dir=tmp_path / "tasks",
    ).run(cset)
    ledger = normalize_ledger(result)
    assert ledger["entries"][0]["mcmm_scenario_ids"] == ["FAST", "SLOW"]
    assert ledger["entries"][0]["eda_runs_consumed"] == 2
    assert ledger["coordinator_application_order"] == [entry["candidate_id"] for entry in ledger["entries"]
                                                        if entry["application_order"] is not None]
    assert all(entry["execution_status"] in {"completed", "skipped"} for entry in ledger["entries"])
