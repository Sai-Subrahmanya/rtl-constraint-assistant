#!/usr/bin/env python3
"""Focused, fake-evaluator benchmark for bounded candidate scheduling.

This is an observational harness, not a performance promise.  It exercises
only the standard-library ThreadPoolExecutor candidate scheduler; no EDA tool
or Python child process is launched.  Use it to compare elapsed time and
observed in-flight candidate count on the host that will run RCA.

Example:
    python scripts/benchmark_candidate_concurrency.py --workers 1,2,4 --delay-ms 80
"""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
from pathlib import Path

from rca.config.model import (
    FlowConfig,
    OptimizationConfig,
    OptimizationPerturbation,
    OptimizationThresholds,
    ProjectConfig,
    ProjectInfo,
)
from rca.constraint_model import ConstraintSet
from rca.optimizer import Optimizer
from rca.qor.model import QoRResult
from rca.utils.enums import Confidence, OptimizationStatus, PowerStatus, SourceKind


def _config(output_dir: Path, workers: int) -> ProjectConfig:
    return ProjectConfig(
        project=ProjectInfo(name="candidate-scheduler-benchmark", top="top"),
        flow=FlowConfig(output_dir=str(output_dir)),
        optimization=OptimizationConfig(
            enabled=True,
            workers=workers,
            # Two iterations allow the first serial-equivalent admission wave
            # to contain the four deterministic generated candidates.
            max_iterations=2,
            max_eda_runs=5,
            max_runtime_minutes=1,
            convergence_patience=5,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[-0.10, 0.10, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        ),
    )


def _constraint_set() -> ConstraintSet:
    cset = ConstraintSet(name="candidate-scheduler-benchmark")
    cset.create_clock(
        name="clk", period_seconds=10e-9, fixed=True,
        source_kind=SourceKind.USER, confidence=Confidence.HIGH,
    )
    uncertainty = cset.create_clock_uncertainty(
        "clk", 0.10e-9, source_kind=SourceKind.INFERENCE,
    )
    uncertainty.opt_status = OptimizationStatus.TUNABLE
    return cset


def _qor(candidate_id: str) -> QoRResult:
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
        tool="benchmark-fake",
    )


def run_once(root: Path, workers: int, delay_seconds: float) -> dict[str, object]:
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def evaluate(candidate, work_dir):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(delay_seconds)
            return {
                "qor": _qor(candidate.id),
                "cache_key": f"fake-input-only-{candidate.constraint_model_hash}",
                "cache_status": "MISS",
                "run_id": f"fake-{candidate.id}",
            }
        finally:
            with lock:
                active -= 1

    cfg = _config(root / f"workers-{workers}", workers)
    started = time.perf_counter()
    result = Optimizer(cfg, evaluate_fn=evaluate, work_dir=root / "tasks").run(_constraint_set())
    elapsed = time.perf_counter() - started
    return {
        "workers": workers,
        "delay_ms": round(delay_seconds * 1000, 3),
        "elapsed_seconds": round(elapsed, 6),
        "observed_max_in_flight_candidates": maximum_active,
        "candidate_count": result.total_candidates,
        "eda_runs": result.eda_runs,
        "stop_reason": result.stop_reason.value if result.stop_reason else None,
        "final_candidate": result.final.id if result.final else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers", default="1,2,4", help="comma-separated worker counts in [1, 8]",
    )
    parser.add_argument("--delay-ms", type=float, default=80.0,
                        help="synthetic complete-candidate evaluation delay (default: 80)")
    parser.add_argument("--repeats", type=int, default=1, help="measurements per worker count")
    args = parser.parse_args()
    worker_counts = [int(value.strip()) for value in args.workers.split(",") if value.strip()]
    if not worker_counts or any(value < 1 or value > 8 for value in worker_counts):
        parser.error("--workers must contain one or more integers from 1 through 8")
    if args.delay_ms < 0 or args.repeats < 1:
        parser.error("--delay-ms must be non-negative and --repeats must be positive")

    measurements: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="rca-candidate-benchmark-") as tmp:
        root = Path(tmp)
        for workers in worker_counts:
            for repeat in range(args.repeats):
                row = run_once(root / f"repeat-{repeat}", workers, args.delay_ms / 1000.0)
                row["repeat"] = repeat + 1
                measurements.append(row)
    payload = {
        "benchmark": "candidate-level-thread-scheduler",
        "measurements": measurements,
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
