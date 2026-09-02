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
import time
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
        }


class Optimizer:
    """Closed-loop multi-objective constraint optimizer (Step 11)."""

    def __init__(self, cfg: ProjectConfig,
                 evaluate_fn: Callable[[Candidate, Path], Any] | None = None,
                 work_dir: Path | None = None) -> None:
        self.cfg = cfg
        self.evaluate_fn = evaluate_fn
        self.work_dir = work_dir or Path(cfg.flow.output_dir) / "optimization"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.budget = OptimizationBudget.from_config(cfg)
        self._priorities = dict(cfg.optimization.priorities)

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------
    def run(self, baseline_cset: ConstraintSet,
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
            self.budget.tick_eda_run()
            result.eda_runs += 1
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

        # ----- main bounded search -----
        explored_hashes: set[str] = {baseline.constraint_model_hash
                                     or stable_hash_cset(baseline.constraint_set)}
        frontier_parents = [baseline]
        iter_num = 0

        while not self.budget.should_stop():
            iter_num += 1
            self.budget.tick_iteration()
            result.iterations = iter_num

            round_candidates: list[Candidate] = []
            for parent in frontier_parents:
                cands = generate_candidates(
                    parent,
                    parent.constraint_set or baseline_cset,
                    self.cfg,
                    max_candidates=4,
                    _id_start=len(result.all_candidates),
                )
                for c in cands:
                    if c.constraint_model_hash in explored_hashes:
                        continue
                    explored_hashes.add(c.constraint_model_hash)
                    self._evaluate(c)
                    self._classify(
                        c,
                        baseline_setup_wns=baseline.qor.setup_wns if baseline.qor else None,
                        baseline_hold_wns=baseline.qor.hold_wns if baseline.qor else None,
                    )
                    self.budget.tick_eda_run()
                    result.eda_runs += 1
                    self._count_cache(c, result)
                    result.all_candidates.append(c)
                    if c.blocked:
                        result.blocked.append(c)
                        c.decision = CandidateDecision.REJECTED_INVALID
                    elif not c.hard_feasible:
                        c.decision = CandidateDecision.REJECTED_INFEASIBLE
                        result.infeasible.append(c)
                    else:
                        round_candidates.append(c)
                    if self.budget.should_stop():
                        break
                if self.budget.should_stop():
                    break

            # Recompute Pareto across ALL feasible candidates so far
            feasible = [c for c in result.all_candidates if c.hard_feasible]
            front = pareto_front(feasible)
            for c in feasible:
                c.pareto_member = c in front
            result.pareto = front

            # Best scalar (reporting only)
            best_cand = select_final(front, baseline, self._priorities)
            best_score = scalar_score(best_cand, baseline, self._priorities) if best_cand else float("-inf")
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

        # ----- final selection -----
        feasible_all = [c for c in result.all_candidates if c.hard_feasible]
        front = pareto_front(feasible_all)
        for c in feasible_all:
            c.pareto_member = c in front
            if c in front:
                c.decision = CandidateDecision.PARETO
            elif c.hard_feasible:
                c.decision = CandidateDecision.DOMINATED
        result.pareto = front

        final = select_final(front, baseline, self._priorities)
        if final is None:
            final = baseline
        final.decision = CandidateDecision.FINAL
        result.final = final

        # Assign ranks to feasible candidates by scalar score (deterministic)
        ranked = sorted(feasible_all,
                        key=lambda c: (-scalar_score(c, baseline, self._priorities), c.id))
        for i, c in enumerate(ranked):
            c.rank = i
            c.priority_score = scalar_score(c, baseline, self._priorities)

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
    # Evaluation & classification
    # ------------------------------------------------------------------
    def _evaluate(self, cand: Candidate) -> None:
        assert self.evaluate_fn is not None
        cand.decision = CandidateDecision.EDA_PENDING
        try:
            qor_out = self.evaluate_fn(cand, self.work_dir)
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
        except Exception as e:
            log.error("Candidate %s evaluation failed: %s", cand.id, e)
            cand.warnings.append(str(e))
            cand.decision = CandidateDecision.REJECTED_INVALID
            cand.validity_status = "ERROR"
            cand.blocked = True
            cand.infeasible_reason = f"evaluation_error:{e}"
            cand.qor = QoRResult(tool="error", notes=[str(e)])

    def _classify(self, cand: Candidate,
                  baseline_setup_wns: float | None = None,
                  baseline_hold_wns: float | None = None) -> None:
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


def _normalize_eval(out: Any) -> tuple[QoRResult | None, str, str, str]:
    """Accept either a QoRResult directly or a dict with provenance."""
    if out is None:
        return None, "", "", ""
    if isinstance(out, QoRResult):
        return out, "", "MISS", ""
    if isinstance(out, dict):
        qor = out.get("qor") if isinstance(out.get("qor"), QoRResult) else None
        return (qor,
                str(out.get("cache_key", "") or ""),
                str(out.get("cache_status", "") or out.get("status", "") or ""),
                str(out.get("run_id", "") or ""))
    return None, "", "", ""


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
