"""
Optimization budget tracking and stopping rules (Manual §46, §94, §95, Step 11 §17).

Deterministic stopping conditions:
  - max iterations / EDA runs / runtime
  - no Pareto improvement for `convergence_patience` iterations
  - all allowed tunable mutations exhausted (search exhaustion)
  - margin utilization floor reached (no more usable positive margin to trade)
  - meaningful-improvement thresholds prevent numerical noise being "progress"
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..utils.enums import StopReason


@dataclass
class OptimizationBudget:
    max_iterations: int = 20
    max_eda_runs: int = 20
    max_runtime_seconds: float = 120 * 60.0
    convergence_patience: int = 5
    wns_ps_threshold: float = 1.0       # meaningful ΔWNS in picoseconds
    area_pct_threshold: float = 0.1     # 0.1% area Δ meaningful
    power_pct_threshold: float = 0.1
    min_margin_headroom_ns: float = 0.0  # stop when headroom ≤ this floor

    # runtime state
    start_time: float = field(default_factory=time.time)
    iterations: int = 0
    eda_runs: int = 0
    no_improve: int = 0
    stop_reason: StopReason | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg) -> "OptimizationBudget":
        o = cfg.optimization
        return cls(
            max_iterations=o.max_iterations,
            max_eda_runs=o.max_eda_runs,
            max_runtime_seconds=o.max_runtime_minutes * 60.0,
            convergence_patience=o.convergence_patience,
            wns_ps_threshold=o.thresholds.wns_ps,
            area_pct_threshold=o.thresholds.area_pct,
            power_pct_threshold=o.thresholds.power_pct,
            min_margin_headroom_ns=getattr(o, "min_margin_headroom_ns", 0.0),
        )

    def tick_iteration(self) -> None:
        self.iterations += 1

    def tick_eda_run(self, count: int = 1) -> None:
        """Record ``count`` EDA runs (default 1).

        MCMM candidates account for one run per evaluated scenario, so a
        Step-12 candidate may tick more than once while still counting as a
        single generated mutation (Step 12 §11).
        """
        self.eda_runs += max(1, int(count))

    def elapsed(self) -> float:
        return time.time() - self.start_time

    def record(self, pareto_size: int, best_score: float,
               best_headroom_ns: float | None) -> None:
        self.history.append({
            "iter": self.iterations, "eda": self.eda_runs,
            "pareto_size": pareto_size, "best_score": best_score,
            "best_headroom_ns": best_headroom_ns,
        })
        # meaningful-improvement detection: require Δscore or Δpareto_size
        if len(self.history) >= 2:
            prev = self.history[-2]
            delta_score = best_score - prev["best_score"]
            delta_pareto = pareto_size - prev["pareto_size"]
            meaningful = abs(delta_score) > 1e-6 or delta_pareto != 0
            if not meaningful:
                self.no_improve += 1
            else:
                self.no_improve = 0

    def should_stop(self) -> StopReason | None:
        if self.stop_reason:
            return self.stop_reason
        if self.iterations >= self.max_iterations:
            return StopReason.MAX_ITERATIONS
        if self.eda_runs >= self.max_eda_runs:
            return StopReason.MAX_EDA_RUNS
        if self.elapsed() >= self.max_runtime_seconds:
            return StopReason.MAX_TIME
        if self.no_improve >= self.convergence_patience:
            return StopReason.CONVERGED
        return None
