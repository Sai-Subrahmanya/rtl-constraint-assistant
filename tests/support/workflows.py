"""Small deterministic RCA workflow fixtures; never emulate a real EDA tool."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rca.config.model import (
    FlowConfig,
    MCMMConfig,
    OptimizationConfig,
    OptimizationPerturbation,
    OptimizationThresholds,
    ProjectConfig,
    ProjectInfo,
    ScenarioSpec,
)
from rca.constraint_model import ConstraintSet, Scenario
from rca.qor.model import QoRResult
from rca.utils.enums import Confidence, OptimizationStatus, PowerStatus, SourceKind


def optimizer_config(root: Path, *, workers: int = 2, max_runs: int = 8,
                     max_iterations: int = 3, mcmm: bool = False) -> ProjectConfig:
    scenarios = [
        ScenarioSpec(id="FAST", mode="functional", corner="fast"),
        ScenarioSpec(id="SLOW", mode="functional", corner="slow"),
    ] if mcmm else []
    return ProjectConfig(
        project=ProjectInfo(name="validation-fixture", top="top"),
        flow=FlowConfig(output_dir=str(root / "output")),
        optimization=OptimizationConfig(
            enabled=True,
            workers=workers,
            max_iterations=max_iterations,
            max_eda_runs=max_runs,
            max_runtime_minutes=1,
            convergence_patience=5,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[-0.20, 0.20, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        ),
        scenarios=scenarios,
        mcmm=MCMMConfig(enabled=mcmm),
    )


def tunable_constraint_set(*, mcmm: bool = False) -> ConstraintSet:
    cset = ConstraintSet(name="validation-fixture")
    cset.create_clock(
        name="clk", period_seconds=10e-9, fixed=True,
        source_kind=SourceKind.USER, confidence=Confidence.HIGH,
    )
    uncertainty = cset.create_clock_uncertainty(
        "clk", 0.10e-9, source_kind=SourceKind.INFERENCE,
    )
    uncertainty.opt_status = OptimizationStatus.TUNABLE
    if mcmm:
        cset.scenarios["FAST"] = Scenario(id="FAST", mode="functional", corner="fast")
        cset.scenarios["SLOW"] = Scenario(id="SLOW", mode="functional", corner="slow")
    return cset


def fake_qor(candidate_id: str, *, scenario: str = "default") -> QoRResult:
    ordinal = int(candidate_id[1:]) if candidate_id.startswith("C") else 0
    return QoRResult(
        setup_wns=(0.8 - ordinal * 0.01) * 1e-9,
        hold_wns=0.2e-9,
        setup_tns=0.0,
        hold_tns=0.0,
        setup_violations=0,
        hold_violations=0,
        area=100.0 + ordinal,
        area_total=100.0 + ordinal,
        power_status=PowerStatus.UNAVAILABLE.value,
        tool="controlled-fake",
        scenario=scenario,
    )


def fake_evaluator(*, cache_status: str = "MISS", fail: set[str] | None = None,
                   on_call: Callable[[str], None] | None = None):
    """Return deterministic synthetic callback evidence, explicitly non-EDA."""
    failures = fail or set()

    def evaluate(candidate, work_dir):
        if on_call:
            on_call(candidate.id)
        if candidate.id in failures:
            raise RuntimeError(f"controlled failure for {candidate.id}")
        return {
            "qor": fake_qor(candidate.id),
            "cache_key": f"controlled-input-{candidate.constraint_model_hash}",
            "cache_status": cache_status,
            "run_id": f"controlled-{candidate.id}",
        }

    return evaluate


def normalize_ledger(result) -> dict:
    """Normalize only documented invocation-specific ledger locator fields."""
    data = result.execution_ledger.to_dict()
    data["invocation_id"] = "<invocation>"
    for entry in data["entries"]:
        entry["task_id"] = "<task>"
        entry["task_work_dir"] = "<task-work-dir>"
        entry["worker_identity"] = "<logical-worker>"
        entry["artifact_locators"] = ["<locator>" for _ in entry["artifact_locators"]]
    return data
