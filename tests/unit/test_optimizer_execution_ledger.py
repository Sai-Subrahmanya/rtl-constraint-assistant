"""Execution-ledger observability tests using controlled in-process evaluators."""

from __future__ import annotations

import copy
import json
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from typer.testing import CliRunner

from rca.artifacts import ArtifactManager
from rca.cli.main import _persist_optimizer_execution_artifacts, app
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
from rca.eda import index_deferred_history_evidence, run_flow
from rca.mcmm import MCMMEvaluator, build_scenario_matrix
from rca.optimizer import (
    AdmissionStatus,
    CacheObservation,
    ExecutionStatus,
    FailureClassification,
    OptimizationExecutionStopReason,
    Optimizer,
    SubmissionStatus,
)
from rca.qor.model import QoRResult
from rca.utils.enums import Confidence, OptimizationStatus, PowerStatus, SourceKind, StopReason
from rca.utils.hashing import hash_file


def _cfg(tmp_path: Path, *, workers: int = 2, max_runs: int = 5,
         max_iterations: int = 2, mcmm: bool = False) -> ProjectConfig:
    scenarios = [
        ScenarioSpec(id="SLOW", mode="functional", corner="slow"),
        ScenarioSpec(id="FAST", mode="functional", corner="fast"),
    ] if mcmm else []
    return ProjectConfig(
        project=ProjectInfo(name="ledger", top="top"),
        flow=FlowConfig(output_dir=str(tmp_path / "output")),
        optimization=OptimizationConfig(
            enabled=True,
            workers=workers,
            max_iterations=max_iterations,
            max_eda_runs=max_runs,
            max_runtime_minutes=1,
            convergence_patience=5,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[-0.10, 0.10, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        ),
        scenarios=scenarios,
        mcmm=MCMMConfig(enabled=mcmm),
    )


def _cset(*, mcmm: bool = False) -> ConstraintSet:
    cset = ConstraintSet(name="ledger")
    cset.create_clock(
        name="clk", period_seconds=10e-9, fixed=True,
        source_kind=SourceKind.USER, confidence=Confidence.HIGH,
    )
    uncertainty = cset.create_clock_uncertainty(
        "clk", 0.10e-9, source_kind=SourceKind.INFERENCE,
    )
    uncertainty.opt_status = OptimizationStatus.TUNABLE
    if mcmm:
        cset.scenarios["SLOW"] = Scenario(id="SLOW", mode="functional", corner="slow")
        cset.scenarios["FAST"] = Scenario(id="FAST", mode="functional", corner="fast")
    return cset


def _qor(candidate_id: str, *, scenario: str = "default") -> QoRResult:
    ordinal = int(candidate_id[1:]) if candidate_id.startswith("C") else 0
    return QoRResult(
        setup_wns=(0.80 - ordinal * 0.01) * 1e-9,
        hold_wns=0.20e-9,
        setup_tns=0.0,
        hold_tns=0.0,
        setup_violations=0,
        hold_violations=0,
        area=100.0 + ordinal,
        area_total=100.0 + ordinal,
        power_status=PowerStatus.UNAVAILABLE.value,
        tool="ledger-fake",
        scenario=scenario,
    )


def _run(tmp_path: Path, *, workers: int = 2, max_runs: int = 5,
         max_iterations: int = 2, callback=None, mcmm: bool = False):
    cfg = _cfg(tmp_path, workers=workers, max_runs=max_runs,
               max_iterations=max_iterations, mcmm=mcmm)
    if callback is None:
        callback = lambda candidate, work_dir: {
            "qor": _qor(candidate.id),
            "cache_key": f"input-only-{candidate.constraint_model_hash}",
            "cache_status": "MISS",
            "run_id": f"run-{candidate.id}",
        }
    optimizer = Optimizer(cfg, evaluate_fn=callback, work_dir=tmp_path / "tasks")
    result = optimizer.run(_cset(mcmm=mcmm))
    assert result.execution_ledger is not None
    return cfg, optimizer, result


def _entry(result, candidate_id: str):
    return next(entry for entry in result.execution_ledger.entries
                if entry.candidate_id == candidate_id)


def _normalized_ledger(result) -> dict:
    """Remove intentionally fresh invocation/task locator components."""
    data = copy.deepcopy(result.execution_ledger.to_dict())
    data["invocation_id"] = "<invocation>"
    for entry in data["entries"]:
        entry["task_id"] = "<task>"
        entry["task_work_dir"] = "<task-work-dir>"
        entry["worker_identity"] = "<logical-worker>"
        entry["artifact_locators"] = ["<artifact-locator>" for _ in entry["artifact_locators"]]
    return data


def _write_optimizer_artifacts(tmp_path: Path, cfg: ProjectConfig, result) -> dict[str, str]:
    am = ArtifactManager(cfg.flow.output_dir)
    candidates_path = am.write_text(
        "candidates.jsonl",
        "".join(json.dumps(candidate.to_dict(), default=str) + "\n"
                for candidate in result.all_candidates),
    )
    pareto_path = am.write_json(
        "pareto_frontier.json", [candidate.to_dict() for candidate in result.pareto],
    )
    final_sdc_path = None
    if result.final and result.final.constraint_set:
        final_sdc_path = am.write_text("design.final.sdc", "# controlled test SDC\n")
    return _persist_optimizer_execution_artifacts(
        am, cfg, result, candidates_path=candidates_path,
        pareto_path=pareto_path, final_sdc_path=final_sdc_path,
    )


def test_parallel_ledger_is_deterministic_and_application_order_ignores_completion_order(tmp_path):
    completed: list[str] = []
    lock = threading.Lock()

    def callback(candidate, work_dir):
        time.sleep(0.04 if candidate.id == "C001" else 0.003)
        with lock:
            completed.append(candidate.id)
        return {
            "qor": _qor(candidate.id),
            "cache_key": f"input-only-{candidate.constraint_model_hash}",
            "cache_status": "MISS",
            "run_id": f"run-{candidate.id}",
        }

    _, _, result = _run(tmp_path, callback=callback, max_runs=4)
    ledger = result.execution_ledger
    assert completed != ledger.coordinator_application_order
    assert ledger.coordinator_application_order == ["C000", "C001", "C002", "C003"]
    assert [entry.task_ordinal for entry in ledger.entries] == [0, 1, 2, 3, 4]
    assert ledger.entries[-1].execution_status == ExecutionStatus.SKIPPED
    assert ledger.entries[-1].skip_reason == "eda_run_budget"
    for entry in ledger.entries[1:4]:
        assert entry.status_history == [
            ExecutionStatus.PLANNED, ExecutionStatus.ADMITTED,
            ExecutionStatus.SUBMITTED, ExecutionStatus.RUNNING,
            ExecutionStatus.COMPLETED,
        ]
        assert entry.application_order == entry.task_ordinal + 1
        assert entry.worker_identity.startswith("candidate-task:task_")
        assert entry.cache_observation == CacheObservation.MISS
        assert entry.cache_key == f"input-only-{entry.constraint_set_hash}"
        assert entry.physical_run_ids == [f"run-{entry.candidate_id}"]
    assert ledger.stop_reason == OptimizationExecutionStopReason.EDA_RUN_BUDGET
    assert ledger.final_counts()["planned_total"] == 5
    assert ledger.final_counts()["completed"] == 4
    assert ledger.final_counts()["skipped"] == 1


def test_serial_ledger_uses_direct_task_identity_without_executor(tmp_path, monkeypatch):
    import rca.optimizer.base as optimizer_base

    class UnexpectedExecutor:
        def __init__(self, *args, **kwargs):
            raise AssertionError("serial ledger must not construct an executor")

    monkeypatch.setattr(optimizer_base, "ThreadPoolExecutor", UnexpectedExecutor)
    _, _, result = _run(tmp_path, workers=1)
    ledger = result.execution_ledger
    assert all(entry.worker_identity == "coordinator-direct" for entry in ledger.entries)
    assert all(entry.task_id == "baseline" or entry.task_id.startswith("serial_")
               for entry in ledger.entries)
    assert all(entry.execution_status == ExecutionStatus.COMPLETED for entry in ledger.entries)
    assert ledger.stop_reason == OptimizationExecutionStopReason.EDA_RUN_BUDGET


def test_stop_classification_distinguishes_iteration_no_admission_and_blocked_configuration(tmp_path):
    _, _, iteration_result = _run(
        tmp_path / "iteration", workers=2, max_runs=20, max_iterations=1,
    )
    assert iteration_result.stop_reason == StopReason.MAX_ITERATIONS
    assert iteration_result.execution_stop_reason == OptimizationExecutionStopReason.ITERATION_LIMIT

    cfg = _cfg(tmp_path / "no-admission", workers=2, max_runs=20, max_iterations=2)
    no_candidate_cset = _cset()
    for constraint in no_candidate_cset:
        constraint.opt_status = OptimizationStatus.FIXED
    no_admission = Optimizer(
        cfg, evaluate_fn=lambda candidate, work_dir: _qor(candidate.id),
        work_dir=tmp_path / "no-admission" / "tasks",
    ).run(no_candidate_cset)
    assert no_admission.execution_stop_reason == OptimizationExecutionStopReason.NO_ADMISSIBLE_CANDIDATES
    assert len(no_admission.execution_ledger.entries) == 1

    disabled_cfg = _cfg(tmp_path / "disabled")
    disabled_cfg.optimization.enabled = False
    disabled = Optimizer(disabled_cfg).run(_cset())
    assert disabled.execution_stop_reason == OptimizationExecutionStopReason.BLOCKED_CONFIGURATION
    assert disabled.execution_ledger.entries[0].execution_status == ExecutionStatus.BLOCKED

    explicitly_stopped = Optimizer(
        _cfg(tmp_path / "explicit"), evaluate_fn=lambda candidate, work_dir: _qor(candidate.id),
        work_dir=tmp_path / "explicit" / "tasks",
    )
    explicitly_stopped.request_stop()
    explicit_result = explicitly_stopped.run(_cset())
    assert explicit_result.execution_stop_reason == OptimizationExecutionStopReason.EXPLICIT_OPTIMIZER_STOP


def test_worker_failure_is_recorded_without_changing_other_task_order(tmp_path):
    def callback(candidate, work_dir):
        if candidate.id == "C002":
            raise RuntimeError("controlled worker failure")
        return {"qor": _qor(candidate.id), "cache_status": "MISS", "run_id": candidate.id}

    _, _, result = _run(tmp_path, callback=callback)
    failed = _entry(result, "C002")
    assert failed.execution_status == ExecutionStatus.FAILED
    assert failed.failure_classification == FailureClassification.WORKER_EVALUATION
    assert "controlled worker failure" in failed.failure_detail
    assert _entry(result, "C001").execution_status == ExecutionStatus.COMPLETED
    assert _entry(result, "C003").execution_status == ExecutionStatus.COMPLETED


def test_executor_creation_failure_and_submission_failure_have_distinct_typed_stops(tmp_path, monkeypatch):
    import rca.optimizer.base as optimizer_base

    class BrokenExecutor:
        def __init__(self, *args, **kwargs):
            raise OSError("no threads")

    monkeypatch.setattr(optimizer_base, "ThreadPoolExecutor", BrokenExecutor)
    _, _, creation_result = _run(tmp_path / "creation")
    assert creation_result.stop_reason == StopReason.ERROR
    assert creation_result.execution_stop_reason == OptimizationExecutionStopReason.EXECUTOR_CREATION_FAILURE
    assert all(entry.execution_status == ExecutionStatus.BLOCKED
               for entry in creation_result.execution_ledger.entries[1:])
    assert all(entry.failure_classification == FailureClassification.EXECUTOR_CREATION
               for entry in creation_result.execution_ledger.entries[1:])

    class FailingSubmitExecutor:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def submit(self, function, task):
            type(self).calls += 1
            if type(self).calls == 2:
                raise RuntimeError("controlled submit failure")
            future = Future()
            future.set_result(function(task))
            return future

        def shutdown(self, wait=True):
            pass

    monkeypatch.setattr(optimizer_base, "ThreadPoolExecutor", FailingSubmitExecutor)
    _, _, submission_result = _run(tmp_path / "submission")
    assert submission_result.execution_stop_reason == OptimizationExecutionStopReason.EXECUTOR_SUBMISSION_FAILURE
    failed_submission = _entry(submission_result, "C002")
    cancelled = _entry(submission_result, "C003")
    assert failed_submission.execution_status == ExecutionStatus.BLOCKED
    assert failed_submission.submission_status == SubmissionStatus.FAILED
    assert cancelled.execution_status == ExecutionStatus.CANCELLED
    assert cancelled.failure_classification == FailureClassification.EXECUTOR_SUBMISSION


def test_budget_and_controlled_deadline_skips_are_explicit(tmp_path, monkeypatch):
    _cfg_value, _, budget_result = _run(tmp_path / "budget", workers=2, max_runs=3, mcmm=True)
    budget_skip = _entry(budget_result, "C002")
    assert budget_result.execution_stop_reason == OptimizationExecutionStopReason.EDA_RUN_BUDGET
    assert budget_skip.execution_status == ExecutionStatus.SKIPPED
    assert budget_skip.admission_status == AdmissionStatus.NOT_ADMITTED
    assert budget_skip.skip_reason == "eda_run_budget"
    assert budget_skip.eda_runs_consumed == 0

    import rca.optimizer.base as optimizer_base

    callback = lambda candidate, work_dir: {
        "qor": _qor(candidate.id), "cache_status": "MISS", "run_id": candidate.id,
    }
    deadline_cfg = _cfg(tmp_path / "deadline", workers=2, max_runs=5)
    optimizer = Optimizer(deadline_cfg, evaluate_fn=callback, work_dir=tmp_path / "deadline" / "tasks")
    original_from_config = optimizer_base.OptimizationBudget.from_config

    def deadline_budget(config):
        budget = original_from_config(config)
        budget.start_time = 0.0
        budget.max_runtime_seconds = 1.0
        return budget

    monkeypatch.setattr(optimizer_base.OptimizationBudget, "from_config", deadline_budget)
    ticks = iter([0.0, 0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr(optimizer_base.time, "time", lambda: next(ticks))
    deadline_result = optimizer.run(_cset())
    assert deadline_result.execution_stop_reason == OptimizationExecutionStopReason.ELAPSED_TIME_LIMIT
    assert _entry(deadline_result, "C003").execution_status == ExecutionStatus.SKIPPED
    assert _entry(deadline_result, "C003").admission_status == AdmissionStatus.ADMITTED
    assert _entry(deadline_result, "C003").submission_status == SubmissionStatus.NOT_SUBMITTED
    assert _entry(deadline_result, "C003").skip_reason == "elapsed_time_limit"


def test_mcmm_ledger_accounts_complete_candidate_scenarios_and_cache_observation(tmp_path):
    cfg = _cfg(tmp_path, workers=2, max_runs=6, mcmm=True)
    cset = _cset(mcmm=True)
    matrix = build_scenario_matrix(cfg, cset)

    def evaluate_scenario(scenario, candidate, work_dir):
        qor = _qor(candidate.id, scenario=scenario.id)
        qor.cache_status = "HIT" if candidate.id == "C001" else "MISS"
        return {
            "qor": qor,
            "cache_status": qor.cache_status,
            "cache_key": f"cache-{candidate.id}-{scenario.id}",
            "run_id": f"{candidate.id}-{scenario.id}",
        }

    evaluator = MCMMEvaluator(matrix, evaluate_scenario=evaluate_scenario,
                              base_cset=cset, name="fake")
    optimizer = Optimizer(cfg, evaluate_fn=evaluator, work_dir=tmp_path / "tasks")
    result = optimizer.run(cset)
    entry = _entry(result, "C001")
    assert entry.execution_status == ExecutionStatus.COMPLETED
    assert entry.mcmm_scenario_ids == list(matrix.active_ids)
    assert entry.mcmm_scenario_count == 2
    assert entry.eda_runs_consumed == 2
    assert entry.cache_observation == CacheObservation.HIT
    assert entry.physical_run_ids == ["C001-FAST", "C001-SLOW"]


def test_repeated_runs_have_equivalent_ledgers_after_invocation_locator_normalization(tmp_path):
    cfg = _cfg(tmp_path, workers=3)
    callback = lambda candidate, work_dir: {
        "qor": _qor(candidate.id),
        "cache_key": f"input-only-{candidate.constraint_model_hash}",
        "cache_status": "MISS",
        "run_id": f"run-{candidate.id}",
    }
    optimizer = Optimizer(cfg, evaluate_fn=callback, work_dir=tmp_path / "tasks")
    first = optimizer.run(_cset())
    second = optimizer.run(_cset())
    assert first.execution_ledger.invocation_id != second.execution_ledger.invocation_id
    assert _normalized_ledger(first) == _normalized_ledger(second)


def test_atomic_ledger_artifact_manifest_and_history_json_inspection(tmp_path):
    cfg, _, result = _run(tmp_path, workers=2)
    paths = _write_optimizer_artifacts(tmp_path, cfg, result)
    ledger_path = Path(paths["ledger"])
    manifest_path = Path(paths["manifest"])
    state_path = Path(paths["state"])
    assert ledger_path.is_file() and manifest_path.is_file() and state_path.is_file()
    assert not list(ledger_path.parent.glob(f".{ledger_path.name}.*.tmp"))
    ledger_data = json.loads(ledger_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert ledger_data["entries"] == [entry.to_dict() for entry in result.execution_ledger.entries]
    assert manifest["extra"]["kind"] == "optimizer_execution"
    assert manifest["artifacts"]["execution_ledger"] == "optimizer_execution_ledger.json"
    assert manifest["artifact_hashes"]["execution_ledger"] == hash_file(ledger_path)

    runner = CliRunner()
    json_result = runner.invoke(app, [
        "history", "--optimization-ledger", "--output-dir", cfg.flow.output_dir, "--json",
    ])
    assert json_result.exit_code == 0
    assert json.loads(json_result.output)["counts"]["planned_total"] == 5
    human_result = runner.invoke(app, [
        "history", "--optimization-ledger", "--output-dir", cfg.flow.output_dir,
    ])
    assert human_result.exit_code == 0
    assert "Optimizer execution ledger" in human_result.output
    # Ledger inspection is artifact-only; it does not create or query SQLite.
    assert not (Path(cfg.flow.output_dir) / "qor.sqlite3").exists()


def test_partial_coordinator_failure_remains_persistable_and_sqlite_advice_does_not_change_ledger(tmp_path, monkeypatch):

    original_finalize = Optimizer._finalize_candidate

    def fail_after_first_generated(self, candidate, baseline, result, round_candidates):
        if candidate.id == "C001":
            raise RuntimeError("controlled coordinator failure")
        return original_finalize(self, candidate, baseline, result, round_candidates)

    monkeypatch.setattr(Optimizer, "_finalize_candidate", fail_after_first_generated)
    cfg, _, result = _run(tmp_path, workers=2)
    assert result.stop_reason == StopReason.ERROR
    assert result.execution_stop_reason == OptimizationExecutionStopReason.FATAL_COORDINATOR_ERROR
    assert any("OPTIMIZATION_COORDINATOR_ERROR" in message for message in result.diagnostics)
    assert _entry(result, "C001").execution_status == ExecutionStatus.BLOCKED
    paths = _write_optimizer_artifacts(tmp_path, cfg, result)
    before = hash_file(Path(paths["ledger"]))

    class FailingRepository:
        def record_flow_evaluation(self, *args, **kwargs):
            raise RuntimeError("advisory sidecar failure")

    class Flow:
        output_dir = str(tmp_path / "flow-output")
        backend = "mock"
        stage = "synthesis_sta"

        def liberty_files(self):
            return []

    class Analysis:
        safe_mode = "balanced"

    class Sources:
        def __init__(self):
            self.defines = []
            self.include_dirs = []

    class Config:
        def __init__(self):
            self._config_path = str(tmp_path / "project.yaml")
            self.flow = Flow()
            self.analysis = Analysis()
            self.sources = Sources()
            self.parameters = {}

        def top_module(self):
            return "top"

    flow = run_flow(
        Config(), ConstraintSet(name="top"), "# sdc\n", "COMPLETE", [],
        backend="mock", run_id="deferred", qor_repository=FailingRepository(),
        defer_history_indexing=True,
    )
    warning = index_deferred_history_evidence(flow["_deferred_history_evidence"])
    assert warning and warning.startswith("QOR_DATABASE_PERSISTENCE_WARNING")
    assert hash_file(Path(paths["ledger"])) == before
