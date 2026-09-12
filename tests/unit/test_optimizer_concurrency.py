"""Candidate-level ThreadPoolExecutor scheduling regression tests.

These tests use controlled in-process evaluators only.  They deliberately do
not spawn Python child processes or invoke EDA tools.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from rca.cli.main import _parallel_task_flow_id
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
from rca.config.schema import PROJECT_SCHEMA
from rca.constraint_model import ConstraintSet, Scenario
from rca.eda import index_deferred_history_evidence, run_flow
from rca.mcmm import MCMMEvaluator, build_scenario_matrix
from rca.optimizer import Optimizer
from rca.qor.model import QoRResult
from rca.utils.enums import Confidence, OptimizationStatus, PowerStatus, SourceKind, StopReason


def _cfg(tmp_path: Path, *, workers: int, max_runs: int = 5,
         mcmm: bool = False) -> ProjectConfig:
    scenarios = (
        [
            ScenarioSpec(id="SLOW", mode="functional", corner="slow"),
            ScenarioSpec(id="FAST", mode="functional", corner="fast"),
        ]
        if mcmm else []
    )
    return ProjectConfig(
        project=ProjectInfo(name="concurrency", top="top"),
        flow=FlowConfig(output_dir=str(tmp_path / "output")),
        optimization=OptimizationConfig(
            enabled=True,
            workers=workers,
            max_iterations=2,
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
    cset = ConstraintSet(name="concurrency")
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
    # Derive stable test data from the preplanned model rather than completion
    # order or thread identity.
    value = int(candidate_id[1:]) if candidate_id.startswith("C") else 0
    return QoRResult(
        setup_wns=(0.80 - value * 0.01) * 1e-9,
        hold_wns=0.20e-9,
        setup_tns=0.0,
        hold_tns=0.0,
        setup_violations=0,
        hold_violations=0,
        area=100.0 + value,
        area_total=100.0 + value,
        power=None,
        power_status=PowerStatus.UNAVAILABLE.value,
        tool="fake",
        scenario=scenario,
    )


def _candidate_projection(result) -> list[tuple]:
    return [
        (
            candidate.id,
            candidate.parent_id,
            tuple(candidate.generated_changes),
            candidate.hard_feasible,
            candidate.decision.value,
            candidate.qor.setup_wns if candidate.qor else None,
            candidate.qor.area if candidate.qor else None,
            candidate.run_id,
        )
        for candidate in result.all_candidates
    ]


def test_workers_config_is_strict_and_bounded_and_schema_matches():
    assert OptimizationConfig().workers == 1
    assert OptimizationConfig(workers=8).workers == 8
    for invalid in (0, 9, True, 1.0, "2"):
        with pytest.raises(ValidationError):
            OptimizationConfig(workers=invalid)
    workers_schema = PROJECT_SCHEMA["properties"]["optimization"]["properties"]["workers"]
    assert workers_schema == {
        "type": "integer", "minimum": 1, "maximum": 8, "default": 1,
        "description": "Bounded concurrent complete-candidate evaluations; 1 is serial.",
    }


def test_workers_one_never_constructs_executor_and_preserves_direct_workdir(tmp_path, monkeypatch):
    import rca.optimizer.base as optimizer_base

    class UnexpectedExecutor:
        def __init__(self, *args, **kwargs):
            raise AssertionError("workers=1 must not construct an executor")

    monkeypatch.setattr(optimizer_base, "ThreadPoolExecutor", UnexpectedExecutor)
    cfg = _cfg(tmp_path, workers=1)
    work_dirs: list[Path] = []

    def evaluate(candidate, work_dir):
        work_dirs.append(work_dir)
        return {"qor": _qor(candidate.id), "run_id": f"serial-{candidate.id}"}

    result = Optimizer(cfg, evaluate_fn=evaluate, work_dir=tmp_path / "serial-work").run(_cset())
    assert result.stop_reason == StopReason.MAX_EDA_RUNS
    assert all(path == tmp_path / "serial-work" for path in work_dirs)
    assert not (tmp_path / "serial-work" / "tasks").exists()


def test_parallel_is_bounded_preplanned_and_applies_results_in_task_order(tmp_path):
    cfg = _cfg(tmp_path, workers=2)
    lock = threading.Lock()
    active = 0
    maximum_active = 0
    callback_order: list[str] = []
    work_by_candidate: dict[str, Path] = {}

    def evaluate(candidate, work_dir):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        # C001 intentionally finishes after later tasks. Coordinator order
        # must nevertheless remain candidate/task order.
        time.sleep(0.06 if candidate.id == "C001" else 0.01)
        with lock:
            callback_order.append(candidate.id)
            work_by_candidate[candidate.id] = work_dir
            active -= 1
        return {
            "qor": _qor(candidate.id),
            "cache_key": f"input-only-cache-{candidate.id}",
            "cache_status": "MISS",
            "run_id": f"physical-{candidate.id}",
            "_deferred_history_evidence": (
                {"candidate_id": candidate.id}
                if Path(work_dir).name.startswith("task_") else None
            ),
        }

    optimizer = Optimizer(cfg, evaluate_fn=evaluate, work_dir=tmp_path / "parallel-work")
    result = optimizer.run(_cset())
    assert maximum_active == 2
    assert callback_order != [candidate.id for candidate in result.all_candidates]
    assert [candidate.id for candidate in result.all_candidates] == [
        "C000", "C001", "C002", "C003", "C004",
    ]
    assert all(candidate.parent_id == "C000" for candidate in result.all_candidates[1:])
    task_paths = [work_by_candidate[candidate.id] for candidate in result.all_candidates[1:]]
    assert len(set(task_paths)) == 4
    assert all(path.parent.name == "tasks" and path.name.startswith("task_") for path in task_paths)
    assert result.eda_runs == cfg.optimization.max_eda_runs
    assert result.stop_reason == StopReason.MAX_EDA_RUNS
    assert [item["candidate_id"] for item in optimizer.deferred_history_evidence] == [
        "C001", "C002", "C003", "C004",
    ]


def test_parallel_and_serial_have_identical_candidate_semantics(tmp_path):
    def evaluate(candidate, work_dir):
        # Force a completion order different from candidate order only when a
        # worker pool is used; returned QoR is independent of that ordering.
        if Path(work_dir).name.startswith("task_"):
            time.sleep(0.04 if candidate.id == "C001" else 0.005)
        return {
            "qor": _qor(candidate.id),
            "cache_key": f"cache-{candidate.constraint_model_hash}",
            "cache_status": "MISS",
            "run_id": f"run-{candidate.id}",
        }

    serial = Optimizer(
        _cfg(tmp_path / "serial", workers=1), evaluate_fn=evaluate,
        work_dir=tmp_path / "serial" / "work",
    ).run(_cset())
    parallel = Optimizer(
        _cfg(tmp_path / "parallel", workers=3), evaluate_fn=evaluate,
        work_dir=tmp_path / "parallel" / "work",
    ).run(_cset())
    assert _candidate_projection(parallel) == _candidate_projection(serial)
    assert parallel.eda_runs == serial.eda_runs
    assert parallel.stop_reason == serial.stop_reason
    assert [candidate.id for candidate in parallel.pareto] == [
        candidate.id for candidate in serial.pareto
    ]
    assert parallel.final.id == serial.final.id


def test_worker_receives_an_isolated_candidate_snapshot(tmp_path):
    cfg = _cfg(tmp_path, workers=2)

    def evaluate(candidate, work_dir):
        original_id = candidate.id
        if original_id != "C000":
            # A misbehaving callback cannot alter coordinator candidate
            # identity, mutation provenance, or the shared constraint model.
            candidate.id = "MUTATED-WORKER-INPUT"
            candidate.generated_changes.append("worker-side mutation")
            assert candidate.constraint_set is not None
            candidate.constraint_set.name = "mutated-worker-cset"
        return {"qor": _qor(original_id), "run_id": f"run-{original_id}"}

    result = Optimizer(cfg, evaluate_fn=evaluate, work_dir=tmp_path / "work").run(_cset())
    assert [candidate.id for candidate in result.all_candidates] == [
        "C000", "C001", "C002", "C003", "C004",
    ]
    assert all("worker-side mutation" not in candidate.generated_changes
               for candidate in result.all_candidates)
    assert all(
        candidate.constraint_set.name == "concurrency"
        for candidate in result.all_candidates
    )


def test_individual_worker_failure_is_isolated_to_that_candidate(tmp_path):
    cfg = _cfg(tmp_path, workers=3)

    def evaluate(candidate, work_dir):
        if candidate.id == "C002":
            raise RuntimeError("intentional fake-evaluator failure")
        return {"qor": _qor(candidate.id), "run_id": candidate.id}

    result = Optimizer(cfg, evaluate_fn=evaluate, work_dir=tmp_path / "work").run(_cset())
    failed = next(candidate for candidate in result.all_candidates if candidate.id == "C002")
    assert failed.blocked and failed.validity_status == "ERROR"
    assert failed in result.blocked
    assert all(
        candidate.validity_status == "VALIDATED"
        for candidate in result.all_candidates if candidate.id not in {"C002"}
    )


def test_executor_construction_failure_is_a_blocked_concurrency_error_not_serial_fallback(
    tmp_path, monkeypatch,
):
    import rca.optimizer.base as optimizer_base

    class BrokenExecutor:
        def __init__(self, *args, **kwargs):
            raise OSError("thread resources unavailable")

    monkeypatch.setattr(optimizer_base, "ThreadPoolExecutor", BrokenExecutor)
    cfg = _cfg(tmp_path, workers=2)
    evaluated: list[str] = []

    def evaluate(candidate, work_dir):
        evaluated.append(candidate.id)
        return {"qor": _qor(candidate.id), "run_id": candidate.id}

    result = Optimizer(cfg, evaluate_fn=evaluate, work_dir=tmp_path / "work").run(_cset())
    # Baseline is deliberately direct. Generated candidates must not be retried
    # serially after executor creation fails.
    assert evaluated == ["C000"]
    assert result.stop_reason == StopReason.ERROR
    assert any("OPTIMIZATION_CONCURRENCY_ERROR" in message for message in result.diagnostics)
    assert all(candidate.blocked for candidate in result.all_candidates[1:])


def test_task_submission_failure_is_a_concurrency_error_without_serial_fallback(
    tmp_path, monkeypatch,
):
    import rca.optimizer.base as optimizer_base

    class FailingSubmitExecutor:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def submit(self, function, task):
            type(self).calls += 1
            if type(self).calls == 2:
                raise RuntimeError("synthetic submit failure")
            future = Future()
            future.set_result(function(task))
            return future

        def shutdown(self, wait=True):
            pass

    monkeypatch.setattr(optimizer_base, "ThreadPoolExecutor", FailingSubmitExecutor)
    cfg = _cfg(tmp_path, workers=2)
    evaluated: list[str] = []

    def evaluate(candidate, work_dir):
        evaluated.append(candidate.id)
        return {"qor": _qor(candidate.id), "run_id": candidate.id}

    result = Optimizer(cfg, evaluate_fn=evaluate, work_dir=tmp_path / "work").run(_cset())
    assert evaluated == ["C000", "C001"]
    assert result.stop_reason == StopReason.ERROR
    assert any("task submission failed" in message for message in result.diagnostics)
    assert all(candidate.blocked for candidate in result.all_candidates[2:])


def test_parallel_mcmm_keeps_each_complete_candidate_serial_inside_one_worker(tmp_path):
    cfg = _cfg(tmp_path, workers=2, max_runs=6, mcmm=True)
    cset = _cset(mcmm=True)
    matrix = build_scenario_matrix(cfg, cset)
    seen: dict[str, list[tuple[str, int]]] = {}
    lock = threading.Lock()

    def evaluate_scenario(scenario, candidate, work_dir):
        with lock:
            seen.setdefault(candidate.id, []).append((scenario.id, threading.get_ident()))
        time.sleep(0.02)
        return _qor(candidate.id, scenario=scenario.id)

    evaluator = MCMMEvaluator(
        matrix, evaluate_scenario=evaluate_scenario, base_cset=cset, name="fake",
    )
    result = Optimizer(cfg, evaluate_fn=evaluator, work_dir=tmp_path / "work").run(cset)
    # Baseline consumes two scenario evaluations. At most two further complete
    # MCMM candidates fit; no candidate x scenario task flattening occurs.
    assert result.eda_runs == 6
    assert result.stop_reason == StopReason.MAX_EDA_RUNS
    assert [candidate.id for candidate in result.all_candidates] == ["C000", "C001", "C002"]
    expected_scenario_order = list(matrix.active_ids)
    for candidate_id, calls in seen.items():
        assert [scenario for scenario, _ in calls] == expected_scenario_order
        assert len({thread_id for _, thread_id in calls}) == 1, candidate_id


def test_deferred_history_is_not_written_in_a_worker_and_is_indexed_by_coordinator(tmp_path):
    class RecordingRepository:
        def __init__(self):
            self.thread_ids: list[int] = []

        def record_flow_evaluation(self, *args, **kwargs):
            self.thread_ids.append(threading.get_ident())

    class Flow:
        output_dir = str(tmp_path / "output")
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

    repo = RecordingRepository()
    main_thread = threading.get_ident()
    with ThreadPoolExecutor(max_workers=1) as executor:
        flow_result = executor.submit(
            run_flow, Config(), ConstraintSet(name="top"), "# sdc\n", "COMPLETE", [],
            backend="mock", run_id="task-flow", qor_repository=repo,
            defer_history_indexing=True,
        ).result()
    assert repo.thread_ids == []
    evidence = flow_result["_deferred_history_evidence"]
    assert evidence is not None and flow_result["persistence_warning"] is None
    assert index_deferred_history_evidence(evidence) is None
    assert repo.thread_ids == [main_thread]


def test_task_run_ids_include_preassigned_task_candidate_and_scenario_without_cache_identity():
    task = Path("output/runs/opt/tasks/task_0123456789abcdef_0007_C002")
    run_id = _parallel_task_flow_id(task, "C002", "FUNC SLOW/0.90")
    assert run_id == "run_task_0123456789abcdef_0007_C002_C002_FUNC-SLOW-0.90"
    assert _parallel_task_flow_id(Path("output/runs/opt"), "C000", "default") is None
