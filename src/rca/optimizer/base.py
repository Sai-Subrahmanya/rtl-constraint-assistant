"""
Multi-objective optimizer (Step 11, Manual §39–§49, §80–§86, §91–§94).

The optimizer uses a layered selection policy:
    hard feasibility  →  Pareto non-dominance  →  lexicographic priorities
        →  margin utilization  →  deterministic tie-breaker (candidate id).

It never uses a scalar weighted score as the sole decision mechanism.
The baseline candidate is always preserved and may be selected as FINAL if
no dominating improvement exists.

EDA caching is reused (Step 10): evaluate_fn is expected to return either a
QoRResult OR a dict with keys {qor, cache_key, cache_status, run_id}; the
candidate record captures all provenance.
"""

from __future__ import annotations

import copy
import re
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config.model import ProjectConfig
from ..constraint_model import ConstraintSet, stable_hash_cset
from ..eda.base import ToolBackend
from ..qor.model import QoRResult
from ..qor.objectives import (
    FeasibilityResult, classify_feasibility, compare_objectives,
    compute_margin, explanation_for, is_dominating, objective_vector,
    pareto_front, scalar_score, select_final,
)
from ..mcmm.aggregate import (
    mcmm_explanation_for,
    mcmm_is_dominating,
    mcmm_pareto_front,
    mcmm_scalar_score,
    mcmm_select_final,
)
from ..utils.enums import (
    CandidateDecision,
    OptimizationStatus,
    Priority,
    StopReason,
)
from ..utils.logging import get_logger
from .budget import OptimizationBudget
from .candidate import Candidate
from .search import generate_candidates

log = get_logger("optimizer")


@dataclass
class OptimizationResult:
    baseline: Candidate | None = None
    final: Candidate | None = None
    pareto: list[Candidate] = field(default_factory=list)
    all_candidates: list[Candidate] = field(default_factory=list)
    infeasible: list[Candidate] = field(default_factory=list)
    blocked: list[Candidate] = field(default_factory=list)
    iterations: int = 0
    eda_runs: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    stop_reason: StopReason = StopReason.USER_STOP
    elapsed_seconds: float = 0.0
    explanation: dict[str, Any] = field(default_factory=dict)
    # Coordinator-only execution diagnostics.  They never influence QoR,
    # feasibility, cache identity, or candidate/Pareto selection.
    diagnostics: list[str] = field(default_factory=list)

    # Convenience counts --------------------------------------------------
    @property
    def total_candidates(self) -> int:
        return len(self.all_candidates)

    @property
    def feasible_count(self) -> int:
        return sum(1 for c in self.all_candidates if c.hard_feasible)

    @property
    def infeasible_count(self) -> int:
        return len(self.infeasible)

    @property
    def pareto_size(self) -> int:
        return len(self.pareto)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the optimization result (alias of :meth:`summary`)."""
        return self.summary()

    def summary(self) -> dict[str, Any]:
        return {
            "baseline_id": self.baseline.id if self.baseline else None,
            "final_id": self.final.id if self.final else None,
            "total_candidates": self.total_candidates,
            "feasible": self.feasible_count,
            "infeasible": self.infeasible_count,
            "blocked": len(self.blocked),
            "pareto_size": self.pareto_size,
            "pareto_ids": [c.id for c in self.pareto],
            "iterations": self.iterations,
            "eda_runs": self.eda_runs,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "elapsed_s": self.elapsed_seconds,
            "candidates": [c.to_dict() for c in self.all_candidates],
            "explanation": self.explanation,
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True)
class _EvaluationTask:
    """One coordinator-owned, preplanned candidate evaluation.

    The candidate ID, lineage, and mutation have already been assigned before
    this task reaches a worker.  The task directory is context for a callback;
    real flow output remains the separately named physical run directory.
    """

    ordinal: int
    # Coordinator-owned record that receives outcome classification.
    candidate: Candidate
    # Isolated callback input; a worker never receives the coordinator's
    # mutable Candidate object or its ConstraintSet/lists.
    worker_candidate: Candidate
    work_dir: Path


@dataclass(frozen=True)
class _EvaluationOutcome:
    """Raw worker result returned to the coordinator in planned-task order."""

    task: _EvaluationTask
    value: Any = None
    error: Exception | None = None


class Optimizer:
    """Closed-loop multi-objective constraint optimizer (Step 11)."""

    def __init__(self, cfg: ProjectConfig,
                 evaluate_fn: Callable[[Candidate, Path], Any] | None = None,
                 work_dir: Path | None = None) -> None:
        self.cfg = cfg
        self.evaluate_fn = evaluate_fn
        self.work_dir = Path(work_dir) if work_dir is not None else (
            Path(cfg.flow.output_dir) / "optimization")
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.budget = OptimizationBudget.from_config(cfg)
        self._priorities = dict(cfg.optimization.priorities)
        self.workers = int(cfg.optimization.workers)
        # Allocated only when the parallel planner first creates a task. It is
        # deliberately not a candidate, QoR, cache, or constraint identity;
        # it solely separates task-local working directories and flow run IDs.
        self._invocation_token: str | None = None
        self._next_task_ordinal = 0
        self._executor: ThreadPoolExecutor | None = None
        self._deferred_history_evidence: list[dict[str, Any]] = []

    @property
    def deferred_history_evidence(self) -> tuple[dict[str, Any], ...]:
        """Completed physical-flow evidence for coordinator-only indexing.

        Parallel CLI callbacks use this transient channel after they have
        written authoritative artifacts/manifests.  It intentionally is not
        part of ``OptimizationResult`` serialization or the QoR model.
        """
        return tuple(self._deferred_history_evidence)

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    def run(self, baseline_cset: ConstraintSet,
            baseline_qor: QoRResult | None = None,
            baseline_sdc: str = "") -> OptimizationResult:
        """Run optimization and always release a parallel executor if one exists."""
        try:
            return self._run_impl(baseline_cset, baseline_qor, baseline_sdc)
        finally:
            # Covers coordinator exceptions as well as normal and early exits.
            # The serial path never constructs an executor.
            self._shutdown_executor()

    def _run_impl(self, baseline_cset: ConstraintSet,
                  baseline_qor: QoRResult | None = None,
                  baseline_sdc: str = "") -> OptimizationResult:
        result = OptimizationResult()
        t0 = time.time()
        opt = self.cfg.optimization

        # ----- baseline candidate -----
        baseline = Candidate(
            id="C000",
            constraint_set=_clone_cset(baseline_cset),
            sdc_text=baseline_sdc,
            generation=0,
            generated_changes=["__baseline__"],
            decision_reason="baseline",
            scenario=getattr(opt, "scenario", "default"),
            corner=getattr(opt, "corner", "default"),
            mode=getattr(opt, "mode", "default"),
        )
        result.baseline = baseline

        if not opt.enabled or self.evaluate_fn is None:
            baseline.qor = baseline_qor
            self._classify(baseline)
            baseline.decision = CandidateDecision.FINAL
            result.final = baseline
            result.all_candidates.append(baseline)
            result.stop_reason = StopReason.USER_STOP
            result.elapsed_seconds = time.time() - t0
            result.explanation = explanation_for(baseline, baseline,
                                                 [baseline], [baseline],
                                                 self._priorities)
            return result

        # Evaluate baseline if needed
        if baseline.qor is None or baseline.run_id == "":
            self._evaluate(baseline)
            self._classify(baseline)
            baseline_eda_runs = _eda_run_count(baseline)
            self.budget.tick_eda_run(baseline_eda_runs)
            result.eda_runs += baseline_eda_runs
            self._count_cache(baseline, result)
        else:
            self._classify(baseline)

        result.all_candidates.append(baseline)
        if baseline.blocked:
            # Can't proceed if baseline itself could not run.
            baseline.decision = CandidateDecision.FINAL
            result.final = baseline
            result.blocked.append(baseline)
            result.stop_reason = StopReason.ERROR
            result.elapsed_seconds = time.time() - t0
            return result
        if not baseline.hard_feasible:
            baseline.decision = CandidateDecision.REJECTED_INFEASIBLE
            result.infeasible.append(baseline)

        mcmm = self._mcmm_enabled()

        # ----- main bounded search -----
        explored_hashes: set[str] = {baseline.constraint_model_hash
                                     or stable_hash_cset(baseline.constraint_set)}
        frontier_parents = [baseline]
        iter_num = 0

        while not self.budget.should_stop():
            iter_num += 1
            self.budget.tick_iteration()
            result.iterations = iter_num

            concurrency_error: str | None = None
            deadline_reached = False
            if self.workers == 1:
                # Deliberate direct serial path.  Keep the established
                # candidate-by-candidate budget checks exactly intact.
                round_candidates = self._evaluate_iteration_serial(
                    frontier_parents, baseline_cset, baseline, result, explored_hashes
                )
            else:
                round_candidates, concurrency_error, deadline_reached = (
                    self._evaluate_iteration_parallel(
                        frontier_parents, baseline_cset, baseline, result, explored_hashes, mcmm
                    )
                )

            # Recompute Pareto across ALL feasible candidates so far
            feasible = [c for c in result.all_candidates if c.hard_feasible]
            use_mcmm = mcmm or self._any_mcmm(feasible)
            if use_mcmm:
                front = mcmm_pareto_front(feasible)
                best_cand = mcmm_select_final(front, baseline, self._priorities)
                best_score = (mcmm_scalar_score(best_cand, baseline, self._priorities)
                              if best_cand else float("-inf"))
            else:
                front = pareto_front(feasible)
                best_cand = select_final(front, baseline, self._priorities)
                best_score = (scalar_score(best_cand, baseline, self._priorities)
                              if best_cand else float("-inf"))
            for c in feasible:
                c.pareto_member = c in front
            result.pareto = front

            # Best scalar (reporting only)
            best_headroom = best_cand.margin_headroom_ns if best_cand else None
            self.budget.record(len(front), best_score, best_headroom)

            # Check margin floor
            if best_cand and best_cand.margin_headroom_ns is not None \
                    and best_cand.margin_headroom_ns <= self.budget.min_margin_headroom_ns + 1e-9:
                # No more slack to spend
                self.budget.no_improve = self.budget.convergence_patience

            # Next frontier = Pareto candidates (search locally from them)
            frontier_parents = list(front) if round_candidates else []
            if not round_candidates and iter_num > 1:
                self.budget.no_improve += 1
            if concurrency_error:
                result.diagnostics.append(concurrency_error)
                self.budget.stop_reason = StopReason.ERROR
                break
            if deadline_reached:
                # Do not admit another parallel wave after elapsed runtime
                # reached its existing wall-clock boundary. In-flight work was
                # collected safely and remains ordered by task plan.
                self.budget.stop_reason = StopReason.MAX_TIME
                break

        # ----- final selection -----
        feasible_all = [c for c in result.all_candidates if c.hard_feasible]
        use_mcmm = mcmm or self._any_mcmm(feasible_all)
        if use_mcmm:
            front = mcmm_pareto_front(feasible_all)
            final = mcmm_select_final(front, baseline, self._priorities)
        else:
            front = pareto_front(feasible_all)
            final = select_final(front, baseline, self._priorities)
        for c in feasible_all:
            c.pareto_member = c in front
            if c in front:
                c.decision = CandidateDecision.PARETO
            elif c.hard_feasible:
                c.decision = CandidateDecision.DOMINATED
        result.pareto = front

        if final is None:
            final = baseline
        final.decision = CandidateDecision.FINAL
        result.final = final

        # Assign ranks to feasible candidates by scalar score (deterministic)
        if use_mcmm:
            ranked = sorted(feasible_all,
                            key=lambda c: (-mcmm_scalar_score(c, baseline, self._priorities), c.id))
            for i, c in enumerate(ranked):
                c.rank = i
                c.priority_score = mcmm_scalar_score(c, baseline, self._priorities)
        else:
            ranked = sorted(feasible_all,
                            key=lambda c: (-scalar_score(c, baseline, self._priorities), c.id))
            for i, c in enumerate(ranked):
                c.rank = i
                c.priority_score = scalar_score(c, baseline, self._priorities)

        if use_mcmm:
            result.explanation = mcmm_explanation_for(final, baseline, front,
                                                      result.all_candidates,
                                                      self._priorities)
        else:
            result.explanation = explanation_for(final, baseline, front,
                                                 result.all_candidates,
                                                 self._priorities)
        final.explanation = result.explanation

        stop = self.budget.should_stop()
        result.stop_reason = stop if stop else StopReason.ALL_GOALS_SATISFIED
        result.elapsed_seconds = time.time() - t0
        log.info(
            "Optimizer finished: %s after %d iters (%d EDA runs, %d cache hits, %.1fs); "
            "final=%s pareto=%d feasible=%d infeasible=%d blocked=%d",
            result.stop_reason.value, result.iterations, result.eda_runs,
            result.cache_hits, result.elapsed_seconds,
            final.id, len(front), result.feasible_count,
            result.infeasible_count, len(result.blocked),
        )
        return result

    # ------------------------------------------------------------------
    # Deterministic candidate-task scheduling
    # ------------------------------------------------------------------
    def _evaluate_iteration_serial(
        self, frontier_parents: list[Candidate], baseline_cset: ConstraintSet,
        baseline: Candidate, result: OptimizationResult, explored_hashes: set[str],
    ) -> list[Candidate]:
        """Established direct candidate loop used when ``workers == 1``."""
        round_candidates: list[Candidate] = []
        for parent in frontier_parents:
            cands = generate_candidates(
                parent,
                parent.constraint_set or baseline_cset,
                self.cfg,
                max_candidates=4,
                _id_start=len(result.all_candidates),
            )
            for candidate in cands:
                if candidate.constraint_model_hash in explored_hashes:
                    continue
                explored_hashes.add(candidate.constraint_model_hash)
                self._evaluate(candidate)
                self._finalize_candidate(candidate, baseline, result, round_candidates)
                if self.budget.should_stop():
                    break
            if self.budget.should_stop():
                break
        return round_candidates

    def _evaluate_iteration_parallel(
        self, frontier_parents: list[Candidate], baseline_cset: ConstraintSet,
        baseline: Candidate, result: OptimizationResult, explored_hashes: set[str],
        mcmm_enabled: bool,
    ) -> tuple[list[Candidate], str | None, bool]:
        """Evaluate one deterministic iteration with bounded candidate workers.

        Candidate generation/deduplication/IDs happen before task submission.
        Worker completion order is intentionally ignored; outcomes are applied
        in the planned task order by this coordinator thread.
        """
        candidates = self._plan_parallel_candidates(
            frontier_parents, baseline_cset, result, explored_hashes, mcmm_enabled
        )
        tasks = [self._make_task(candidate) for candidate in candidates]
        outcomes, concurrency_error, deadline_reached = self._run_parallel_tasks(tasks)
        round_candidates: list[Candidate] = []
        for outcome in outcomes:
            candidate = outcome.task.candidate
            self._apply_evaluation_outcome(candidate, outcome)
            self._finalize_candidate(candidate, baseline, result, round_candidates)
        return round_candidates, concurrency_error, deadline_reached

    def _plan_parallel_candidates(
        self, frontier_parents: list[Candidate], baseline_cset: ConstraintSet,
        result: OptimizationResult, explored_hashes: set[str], mcmm_enabled: bool,
    ) -> list[Candidate]:
        """Return the serial-order candidate prefix permitted by EDA budget.

        The existing serial loop accounts each complete MCMM candidate as one
        attempt per active scenario.  That count is known from the immutable
        active matrix before dispatch, so a parallel batch does not oversubscribe
        ``max_eda_runs`` merely because several tasks are in flight.
        """
        planned: list[Candidate] = []
        next_id_start = len(result.all_candidates)
        projected_eda_runs = self.budget.eda_runs
        task_cost = self._planned_eda_run_cost(mcmm_enabled)
        # The established serial loop consults ``should_stop`` after each
        # candidate. On its final permitted iteration that means only the
        # first candidate is admitted before MAX_ITERATIONS is observed. Keep
        # that legacy admission boundary while still planning before dispatch.
        last_permitted_iteration = self.budget.iterations >= self.budget.max_iterations
        for parent in frontier_parents:
            candidates = generate_candidates(
                parent,
                parent.constraint_set or baseline_cset,
                self.cfg,
                max_candidates=4,
                _id_start=next_id_start,
            )
            for candidate in candidates:
                if candidate.constraint_model_hash in explored_hashes:
                    continue
                # Do not dispatch a candidate whose complete MCMM task would
                # exceed the configured physical EDA-run budget. This check is
                # performed before submission, not when worker completions
                # arrive, so parallelism cannot oversubscribe the budget.
                if projected_eda_runs + task_cost > self.budget.max_eda_runs:
                    self.budget.stop_reason = StopReason.MAX_EDA_RUNS
                    return planned
                explored_hashes.add(candidate.constraint_model_hash)
                planned.append(candidate)
                next_id_start += 1
                projected_eda_runs += task_cost
                if projected_eda_runs == self.budget.max_eda_runs:
                    self.budget.stop_reason = StopReason.MAX_EDA_RUNS
                    return planned
                if last_permitted_iteration:
                    return planned
        return planned

    def _planned_eda_run_cost(self, mcmm_enabled: bool) -> int:
        if not mcmm_enabled:
            return 1
        try:
            from ..mcmm import build_scenario_matrix
            return max(1, len(build_scenario_matrix(self.cfg).active_ids))
        except Exception:
            # Matrix construction is already guarded by _mcmm_enabled; retain
            # safe single-task accounting if an external config-like caller
            # cannot expose the matrix.
            return 1

    def _make_task(self, candidate: Candidate) -> _EvaluationTask:
        if self._invocation_token is None:
            self._invocation_token = uuid.uuid4().hex[:16]
        self._next_task_ordinal += 1
        candidate_component = (
            re.sub(r"[^A-Za-z0-9_.-]+", "-", candidate.id).strip(".-") or "candidate"
        )
        name = f"task_{self._invocation_token}_{self._next_task_ordinal:04d}_{candidate_component}"
        return _EvaluationTask(
            ordinal=self._next_task_ordinal,
            candidate=candidate,
            worker_candidate=_worker_candidate_snapshot(candidate),
            work_dir=self.work_dir / "tasks" / name,
        )

    def _run_parallel_tasks(
        self, tasks: list[_EvaluationTask],
    ) -> tuple[list[_EvaluationOutcome], str | None, bool]:
        """Run bounded waves and return outcomes in planned order.

        No ``as_completed`` ordering is used.  A construction/submission error
        is a deterministic concurrency error, not a serial fallback.  Already
        submitted independent tasks are allowed to finish before the coordinator
        reports that error.
        """
        if not tasks:
            return [], None, False
        try:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.workers,
                    thread_name_prefix="rca-candidate",
                )
        except Exception as exc:
            return (
                [self._concurrency_error_outcome(task, "executor", exc) for task in tasks],
                "OPTIMIZATION_CONCURRENCY_ERROR: executor construction failed "
                f"({type(exc).__name__})",
                False,
            )

        outcomes: list[_EvaluationOutcome] = []
        deadline_reached = False
        deadline = self.budget.start_time + self.budget.max_runtime_seconds
        for wave_start in range(0, len(tasks), self.workers):
            if time.time() >= deadline:
                deadline_reached = True
                break
            wave = tasks[wave_start:wave_start + self.workers]
            futures: list[tuple[_EvaluationTask, Future[_EvaluationOutcome]]] = []
            submission_error: Exception | None = None
            for task in wave:
                try:
                    futures.append((task, self._executor.submit(self._worker_evaluate, task)))
                except Exception as exc:
                    submission_error = exc
                    break
            # Preserve planned semantic order even where a task unexpectedly
            # fails outside the worker wrapper.
            for task, future in futures:
                try:
                    outcomes.append(future.result())
                except Exception as exc:
                    outcomes.append(self._concurrency_error_outcome(task, "future", exc))
            if submission_error is not None:
                submitted = {task.ordinal for task, _ in futures}
                for task in tasks[wave_start:]:
                    if task.ordinal not in submitted:
                        outcomes.append(
                            self._concurrency_error_outcome(
                                task, "submission", submission_error
                            )
                        )
                return (
                    outcomes,
                    "OPTIMIZATION_CONCURRENCY_ERROR: task submission failed "
                    f"({type(submission_error).__name__})",
                    False,
                )
        return outcomes, None, deadline_reached

    def _worker_evaluate(self, task: _EvaluationTask) -> _EvaluationOutcome:
        """Execute only task-local callback work; never mutate optimizer state."""
        try:
            task.work_dir.mkdir(parents=True, exist_ok=False)
            assert self.evaluate_fn is not None
            return _EvaluationOutcome(
                task=task,
                value=self.evaluate_fn(task.worker_candidate, task.work_dir),
            )
        except Exception as exc:
            return _EvaluationOutcome(task=task, error=exc)

    @staticmethod
    def _concurrency_error_outcome(
        task: _EvaluationTask, phase: str, error: Exception,
    ) -> _EvaluationOutcome:
        return _EvaluationOutcome(
            task=task,
            error=RuntimeError(f"concurrency_{phase}_error:{type(error).__name__}:{error}"),
        )

    def _finalize_candidate(
        self, candidate: Candidate, baseline: Candidate, result: OptimizationResult,
        round_candidates: list[Candidate],
    ) -> None:
        self._classify(
            candidate,
            baseline_setup_wns=baseline.qor.setup_wns if baseline.qor else None,
            baseline_hold_wns=baseline.qor.hold_wns if baseline.qor else None,
        )
        candidate_eda_runs = _eda_run_count(candidate)
        self.budget.tick_eda_run(candidate_eda_runs)
        result.eda_runs += candidate_eda_runs
        self._count_cache(candidate, result)
        result.all_candidates.append(candidate)
        if candidate.blocked:
            result.blocked.append(candidate)
            candidate.decision = CandidateDecision.REJECTED_INVALID
        elif not candidate.hard_feasible:
            candidate.decision = CandidateDecision.REJECTED_INFEASIBLE
            result.infeasible.append(candidate)
        else:
            round_candidates.append(candidate)

    def _shutdown_executor(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    # ------------------------------------------------------------------
    # Evaluation & classification
    # ------------------------------------------------------------------
    def _mcmm_enabled(self) -> bool:
        """Whether MCMM is active for this optimizer run.

        MCMM is active only when explicitly enabled AND more than one scenario is
        active.  When disabled or a single active scenario exists the optimizer
        uses the legacy Step-11 path (backward compatibility, Step 12 §22).
        """
        mcmm = getattr(self.cfg, "mcmm", None)
        if mcmm is None or not bool(getattr(mcmm, "enabled", False)):
            return False
        try:
            from ..mcmm import build_scenario_matrix
            mat = build_scenario_matrix(self.cfg)
            return bool(mat.is_enabled and mat.scenario_count > 1)
        except Exception:
            return False

    @staticmethod
    def _any_mcmm(cands) -> bool:
        """True when any candidate carries an MCMM aggregate result."""
        return any(getattr(c, "mcmm", None) is not None for c in cands)

    def _evaluate(self, cand: Candidate) -> None:
        """Evaluate directly in deliberate serial mode (no executor)."""
        assert self.evaluate_fn is not None
        cand.decision = CandidateDecision.EDA_PENDING
        try:
            self._apply_evaluation_value(cand, self.evaluate_fn(cand, self.work_dir))
        except Exception as exc:
            self._apply_evaluation_error(cand, exc)

    def _apply_evaluation_outcome(self, cand: Candidate, outcome: _EvaluationOutcome) -> None:
        """Apply one worker result on the coordinator in planned-task order."""
        cand.decision = CandidateDecision.EDA_PENDING
        if outcome.error is not None:
            self._apply_evaluation_error(cand, outcome.error)
            return
        try:
            self._apply_evaluation_value(cand, outcome.value)
        except Exception as exc:
            self._apply_evaluation_error(cand, exc)

    def _apply_evaluation_value(self, cand: Candidate, qor_out: Any) -> None:
        self._capture_deferred_history(qor_out)
        if _is_mcmm_result(qor_out):
            self._apply_mcmm(cand, qor_out)
            return
        qor, cache_key, cache_status, run_id = _normalize_eval(qor_out)
        cand.qor = qor
        cand.cache_key = cache_key or ""
        cand.cache_status = cache_status or "MISS"
        cand.run_id = run_id or ""
        if qor is not None:
            qor.candidate_id = cand.id
            qor.cache_key = cand.cache_key
            qor.cache_status = cand.cache_status
            qor.run_id = cand.run_id or qor.run_id
        cand.validity_status = "VALIDATED"
        cand.decision = CandidateDecision.EVALUATED

    def _capture_deferred_history(self, value: Any) -> None:
        """Collect transient flow evidence only after a task has completed."""
        evidence: Any = None
        if _is_mcmm_result(value):
            evidence = getattr(value, "_deferred_history_evidence", None)
        elif isinstance(value, dict):
            evidence = value.get("_deferred_history_evidence")
        if isinstance(evidence, dict):
            self._deferred_history_evidence.append(evidence)
        elif isinstance(evidence, (list, tuple)):
            self._deferred_history_evidence.extend(
                item for item in evidence if isinstance(item, dict)
            )

    @staticmethod
    def _apply_evaluation_error(cand: Candidate, exc: Exception) -> None:
        log.error("Candidate %s evaluation failed: %s", cand.id, exc)
        cand.warnings.append(str(exc))
        cand.decision = CandidateDecision.REJECTED_INVALID
        cand.validity_status = "ERROR"
        cand.blocked = True
        cand.infeasible_reason = f"evaluation_error:{exc}"
        cand.qor = QoRResult(tool="error", notes=[str(exc)])

    def _apply_mcmm(self, cand: Candidate, mcmm_result: Any) -> None:
        """Attach an MCMMResult to a candidate and derive global verdict.

        The per-scenario records are retained on ``cand.mcmm``; ``cand.qor`` is
        left as None because MCMM results are never collapsed into a single
        QoR value.
        """
        cand.mcmm = mcmm_result
        cand.qor = None
        cand.cache_key = getattr(mcmm_result, "cache_key", "") or ""
        cand.cache_status = _mcmm_cache_status(mcmm_result)
        cand.run_id = ";".join(getattr(mcmm_result, "run_ids", []) or [])
        cand.global_status = getattr(mcmm_result, "global_status", "") or ""
        cand.limiting_scenarios = list(getattr(mcmm_result, "limiting_scenarios", []) or [])
        cand.margin_limiting_scenarios = list(
            getattr(mcmm_result, "margin_limiting_scenarios", []) or [])
        cand.hard_feasible = bool(getattr(mcmm_result, "feasible", False))
        cand.blocked = bool(getattr(mcmm_result, "blocked", False))
        cand.infeasible_reason = getattr(mcmm_result, "global_reason", "") or ""
        cand.margin_headroom_ns = getattr(mcmm_result, "margin_headroom_ns", None)
        cand.margin_utilization = getattr(mcmm_result, "margin_utilization", None)
        cand.diagnostics = list(getattr(mcmm_result, "diagnostics", []) or [])
        cand.validity_status = "VALIDATED"
        cand.decision = CandidateDecision.EVALUATED

    def _classify(self, cand: Candidate,
                  baseline_setup_wns: float | None = None,
                  baseline_hold_wns: float | None = None) -> None:
        # MCMM candidates are classified globally during _apply_mcmm.
        if getattr(cand, "mcmm", None) is not None:
            return
        qor = cand.qor
        if cand.blocked or qor is None:
            cand.hard_feasible = False
            cand.blocked = True
            cand.infeasible_reason = cand.infeasible_reason or "blocked"
            return
        req_s = self.cfg.optimization.required_setup_margin_ns
        req_h = self.cfg.optimization.required_hold_margin_ns
        # Safe policy by default: unsafe_exceptions INFEASIBLE. Exploratory
        # opt-in must be performed by an explicit caller, not by default search.
        allow_unsafe = bool(getattr(self.cfg.optimization,
                                    "allow_unsafe_exceptions", False))
        fr = classify_feasibility(qor,
                                  required_setup_ns=req_s,
                                  required_hold_ns=req_h,
                                  allow_unsafe_exceptions=allow_unsafe)
        cand.hard_feasible = fr.feasible
        cand.blocked = fr.blocked
        cand.infeasible_reason = fr.infeasible_reason
        cand.diagnostics = list(fr.diagnostics)
        # Do NOT fabricate constraint_quality=1.0 when quality is unmeasured:
        # QoRResult.constraint_quality defaults to None (UNKNOWN), which
        # objectives.py treats conservatively.
        # populate margin metrics
        if fr.feasible:
            m = compute_margin(qor, required_setup_ns=req_s, required_hold_ns=req_h,
                               baseline_setup_wns=baseline_setup_wns,
                               baseline_hold_wns=baseline_hold_wns)
            cand.margin_headroom_ns = m["margin_headroom_ns"]
            cand.margin_utilization = m["margin_utilization"]
            qor.margin_headroom_ns = m["margin_headroom_ns"]
            qor.margin_utilization = m["margin_utilization"]

    def _count_cache(self, cand: Candidate, result: OptimizationResult) -> None:
        if cand.cache_status == "HIT" or cand.cache_status == "CACHE_HIT":
            result.cache_hits += 1
        elif cand.cache_status and cand.cache_status != "N_A":
            result.cache_misses += 1


def _is_mcmm_result(out: Any) -> bool:
    """Return True when an evaluation output is an MCMM aggregate result."""
    return (out is not None
            and hasattr(out, "scenario_results")
            and hasattr(out, "active_scenario_ids")
            and hasattr(out, "global_status"))


def _eda_run_count(cand: Candidate | None) -> int:
    """Number of individual EDA runs represented by a candidate.

    For MCMM candidates this is the per-scenario run count; otherwise it is 1
    (one STA/synthesis run per candidate).
    """
    if cand is None:
        return 0
    mcmm = getattr(cand, "mcmm", None)
    if mcmm is not None and getattr(mcmm, "eda_runs", 0):
        return int(mcmm.eda_runs)
    return 1


def _mcmm_cache_status(mcmm_result: Any) -> str:
    """Derive a candidate-level cache status from the MCMM aggregate."""
    cache_hits = int(getattr(mcmm_result, "cache_hits", 0) or 0)
    cache_misses = int(getattr(mcmm_result, "cache_misses", 0) or 0)
    if cache_misses > 0:
        return "MISS"
    if cache_hits > 0:
        return "HIT"
    return "N_A"


def _normalize_eval(out: Any) -> tuple[QoRResult | None, str, str, str]:
    """Accept either a QoRResult directly or a dict with provenance."""
    if out is None:
        return None, "", "", ""
    if isinstance(out, QoRResult):
        # Preserve a physical flow's run identity when a callback returns the
        # canonical QoR object directly.  This is provenance propagation, not
        # a new optimizer/cache identity.
        return out, "", "MISS", str(getattr(out, "run_id", "") or "")
    if isinstance(out, dict):
        qor = out.get("qor") if isinstance(out.get("qor"), QoRResult) else None
        return (qor,
                str(out.get("cache_key", "") or ""),
                str(out.get("cache_status", "") or out.get("status", "") or ""),
                str(out.get("run_id", "") or ""))
    return None, "", "", ""


def _worker_candidate_snapshot(candidate: Candidate) -> Candidate:
    """Copy the callback input so worker-side mutation cannot race coordinator state.

    The callback receives all immutable planning identity and constraint inputs
    it needs, but none of the coordinator-owned evaluation, selection, rank,
    warning, or list state. ConstraintSet cloning follows the existing
    fallback-safe clone helper used by baseline construction.
    """
    return Candidate(
        id=candidate.id,
        parent_id=candidate.parent_id,
        generation=candidate.generation,
        constraint_model_hash=candidate.constraint_model_hash,
        sdc_hash=candidate.sdc_hash,
        constraint_set=(
            _clone_cset(candidate.constraint_set)
            if candidate.constraint_set is not None else None
        ),
        sdc_text=candidate.sdc_text,
        generated_changes=list(candidate.generated_changes),
        mutated_constraint_ids=list(candidate.mutated_constraint_ids),
        decision_reason=candidate.decision_reason,
        scenario=candidate.scenario,
        corner=candidate.corner,
        mode=candidate.mode,
    )


def _clone_cset(cset: ConstraintSet) -> ConstraintSet:
    """Deep-copy a ConstraintSet; rebuild by re-adding copied constraints
    when pydantic deepcopy fails due to transient locks."""
    try:
        return cset.model_copy(deep=True)
    except Exception:
        new_cs = ConstraintSet(name=getattr(cset, "name", ""))
        for c in cset:
            try:
                new_cs.add(copy.deepcopy(c))
            except Exception:
                new_cs.add(c)
        return new_cs
