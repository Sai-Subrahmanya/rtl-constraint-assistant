"""Short deterministic stress checks for the accepted bounded scheduler."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future

import pytest

from rca.mcmm import MCMMEvaluator, build_scenario_matrix
from rca.optimizer import (
    ExecutionStatus,
    FailureClassification,
    OptimizationExecutionStopReason,
    Optimizer,
)
from rca.utils.enums import StopReason
from tests.support.workflows import (
    fake_evaluator,
    normalize_ledger,
    optimizer_config,
    tunable_constraint_set,
)


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_repeated_candidate_batches_are_bounded_and_semantically_repeatable(tmp_path, workers):
    """Thirty controlled candidate attempts exercise serial and bounded paths."""
    observed_max = 0
    active = 0
    lock = threading.Lock()

    def callback(candidate, work_dir):
        nonlocal active, observed_max
        with lock:
            active += 1
            observed_max = max(observed_max, active)
        try:
            # Deterministic stagger makes completion order differ for workers>1.
            time.sleep(0.002 * (int(candidate.id[1:]) % 3))
            return fake_evaluator(cache_status="HIT")(candidate, work_dir)
        finally:
            with lock:
                active -= 1

    normalized = []
    evaluated = 0
    for run in range(6):
        cfg = optimizer_config(tmp_path / f"run-{run}", workers=workers, max_runs=5, max_iterations=2)
        result = Optimizer(cfg, evaluate_fn=callback, work_dir=tmp_path / f"tasks-{run}").run(tunable_constraint_set())
        evaluated += result.total_candidates
        normalized.append(normalize_ledger(result))
        assert result.execution_ledger.stop_reason == OptimizationExecutionStopReason.EDA_RUN_BUDGET
        assert result.cache_hits == result.total_candidates
    assert evaluated == 30
    assert observed_max <= workers
    assert all(item == normalized[0] for item in normalized[1:])


def test_parallel_stress_failure_budget_and_deadline_states_remain_auditable(tmp_path, monkeypatch):
    # Worker failure remains local and does not scramble ordered application.
    failure_cfg = optimizer_config(tmp_path / "failure", workers=4, max_runs=5, max_iterations=2)
    failure_result = Optimizer(
        failure_cfg, evaluate_fn=fake_evaluator(fail={"C002"}), work_dir=tmp_path / "failure-tasks",
    ).run(tunable_constraint_set())
    failed = next(entry for entry in failure_result.execution_ledger.entries if entry.candidate_id == "C002")
    assert failed.execution_status == ExecutionStatus.FAILED
    assert failed.failure_classification == FailureClassification.WORKER_EVALUATION
    assert failure_result.execution_ledger.coordinator_application_order == ["C000", "C001", "C002", "C003", "C004"]

    # Conservative admission records a not-dispatched budget task.
    budget_cfg = optimizer_config(tmp_path / "budget", workers=2, max_runs=3, max_iterations=2)
    budget_result = Optimizer(
        budget_cfg, evaluate_fn=fake_evaluator(), work_dir=tmp_path / "budget-tasks",
    ).run(tunable_constraint_set())
    assert budget_result.stop_reason == StopReason.MAX_EDA_RUNS
    assert any(entry.execution_status == ExecutionStatus.SKIPPED
               for entry in budget_result.execution_ledger.entries)

    # A binding deadline records admitted-but-not-submitted work; it does not
    # mark it completed merely because a previous wave returned.
    import rca.optimizer.base as optimizer_base

    original_from_config = optimizer_base.OptimizationBudget.from_config

    def deadline_budget(config):
        budget = original_from_config(config)
        budget.start_time = 0.0
        budget.max_runtime_seconds = 1.0
        return budget

    monkeypatch.setattr(optimizer_base.OptimizationBudget, "from_config", deadline_budget)
    ticks = iter([0.0, 0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr(optimizer_base.time, "time", lambda: next(ticks))
    deadline_cfg = optimizer_config(tmp_path / "deadline", workers=2, max_runs=5, max_iterations=2)
    deadline_result = Optimizer(
        deadline_cfg, evaluate_fn=fake_evaluator(), work_dir=tmp_path / "deadline-tasks",
    ).run(tunable_constraint_set())
    assert deadline_result.execution_stop_reason == OptimizationExecutionStopReason.ELAPSED_TIME_LIMIT
    assert any(entry.execution_status == ExecutionStatus.SKIPPED and entry.skip_reason == "elapsed_time_limit"
               for entry in deadline_result.execution_ledger.entries)


def test_mcmm_stress_keeps_each_candidate_scenarios_serial_and_repeatable(tmp_path):
    observed: dict[str, list[str]] = {}

    def scenario_evaluator(scenario, candidate, work_dir):
        observed.setdefault(candidate.id, []).append(scenario.id)
        return {
            "qor": fake_evaluator()(candidate, work_dir)["qor"],
            "cache_key": f"mcmm-input-{candidate.constraint_model_hash}-{scenario.id}",
            "cache_status": "HIT",
            "run_id": f"mcmm-{candidate.id}-{scenario.id}",
        }

    cfg = optimizer_config(tmp_path, workers=4, max_runs=10, max_iterations=2, mcmm=True)
    cset = tunable_constraint_set(mcmm=True)
    matrix = build_scenario_matrix(cfg, cset)
    result = Optimizer(
        cfg,
        evaluate_fn=MCMMEvaluator(matrix, evaluate_scenario=scenario_evaluator,
                                  base_cset=cset, name="stress-fake"),
        work_dir=tmp_path / "mcmm-tasks",
    ).run(cset)
    assert observed and all(order == ["FAST", "SLOW"] for order in observed.values())
    assert result.execution_ledger.total_eda_runs == result.eda_runs
    assert all(entry.mcmm_scenario_ids == ["FAST", "SLOW"]
               for entry in result.execution_ledger.entries if entry.execution_status == ExecutionStatus.COMPLETED)


def test_executor_creation_and_submission_fail_closed_without_serial_fallback(tmp_path, monkeypatch):
    import rca.optimizer.base as optimizer_base

    class BrokenExecutor:
        def __init__(self, *args, **kwargs):
            raise OSError("controlled executor creation failure")

    monkeypatch.setattr(optimizer_base, "ThreadPoolExecutor", BrokenExecutor)
    cfg = optimizer_config(tmp_path / "creation", workers=2, max_runs=5, max_iterations=2)
    creation = Optimizer(cfg, evaluate_fn=fake_evaluator(), work_dir=tmp_path / "creation-tasks").run(tunable_constraint_set())
    assert creation.execution_stop_reason == OptimizationExecutionStopReason.EXECUTOR_CREATION_FAILURE
    assert all(entry.execution_status == ExecutionStatus.BLOCKED
               for entry in creation.execution_ledger.entries[1:])

    class SubmissionFails:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def submit(self, fn, task):
            type(self).calls += 1
            if type(self).calls == 2:
                raise RuntimeError("controlled submit failure")
            future = Future()
            future.set_result(fn(task))
            return future

        def shutdown(self, wait=True):
            pass

    monkeypatch.setattr(optimizer_base, "ThreadPoolExecutor", SubmissionFails)
    cfg = optimizer_config(tmp_path / "submission", workers=2, max_runs=5, max_iterations=2)
    submission = Optimizer(cfg, evaluate_fn=fake_evaluator(), work_dir=tmp_path / "submission-tasks").run(tunable_constraint_set())
    assert submission.execution_stop_reason == OptimizationExecutionStopReason.EXECUTOR_SUBMISSION_FAILURE
    assert any(entry.execution_status == ExecutionStatus.CANCELLED
               for entry in submission.execution_ledger.entries)
