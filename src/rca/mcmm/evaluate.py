"""
Per-candidate MCMM evaluation orchestration (Step 12 §3, §16).

An :class:`MCMMEvaluator` evaluates a single candidate across ALL active
scenarios.  It is callable with the optimizer's ``evaluate_fn`` signature
``(candidate, work_dir) -> MCMMResult`` and never collapses per-scenario QoR
into one value.

Evaluation of an individual scenario is delegated to a user-supplied callable
that returns either a :class:`QoRResult` or a dict with ``{qor, cache_key,
cache_status, run_id}`` provenance.  This keeps vendor-specific behaviour out
of the core (Step 12 §16); the mock backend is available for tests and offline
development and is clearly labelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..constraint_model import ConstraintSet, Scenario
from ..qor.model import QoRResult
from ..utils.hashing import stable_hash
from .aggregate import (
    aggregate_objectives,
    finalize_limiting,
    global_feasibility,
    global_margin,
)
from .cache import mcmm_run_cache_key, scenario_cache_key
from .matrix import ScenarioMatrix
from .model import (
    BLOCKED,
    FEASIBLE,
    MCMMResult,
    ScenarioQoR,
)


@dataclass
class MCMMEvaluator:
    """Evaluate one candidate across all active scenarios."""

    matrix: ScenarioMatrix
    evaluate_scenario: Callable[[Scenario, Any, Path], Any]
    base_cset: ConstraintSet | None = None
    name: str = "mcmm"
    tool_path: str = ""
    tool_version: str = ""

    @property
    def enabled(self) -> bool:
        return self.matrix.is_enabled

    def __call__(self, cand: Any, work_dir: Path) -> MCMMResult:
        # If MCMM disabled or a single active scenario, still evaluate every
        # active scenario (which is just the one) so the result stays uniform;
        # legacy single-scenario consumers read ``cand.qor`` separately.
        cset = getattr(cand, "constraint_set", None) or self.base_cset
        result = MCMMResult(candidate_id=getattr(cand, "id", ""))
        result.active_scenario_ids = list(self.matrix.active_ids)

        qor_by_scenario: dict[str, Any] = {}
        for scenario in self.matrix.active_scenarios():
            sqor = self._evaluate_scenario(cand, cset, scenario, work_dir)
            qor_by_scenario[scenario.id] = sqor.qor
            result.scenario_results[scenario.id] = sqor
            if sqor.run_id:
                result.run_ids.append(sqor.run_id)
        result.eda_runs = len(self.matrix.active_scenarios())

        # Global aggregation.
        global_feasibility(result)
        aggregate_objectives(result)
        global_margin(result)
        finalize_limiting(result)

        # Experiment cache identity for the whole MCMM run.
        result.cache_key = mcmm_run_cache_key(
            cset or ConstraintSet(name="empty"),
            self.matrix.summary(),
            backend=self.name,
            tool_version=self.tool_version,
            tool_path=self.tool_path,
        )
        result.cache_hits = sum(
            1 for s in result.scenario_results.values() if s.cache_status in ("HIT", "CACHE_HIT"))
        result.cache_misses = sum(
            1 for s in result.scenario_results.values()
            if s.cache_status and s.cache_status not in ("HIT", "CACHE_HIT", "N_A"))
        # Aggregate cache status for the whole MCMM run: HIT only when every
        # scenario was served from cache; otherwise MISS (or N_A when unknown).
        if result.scenario_results:
            statuses = {s.cache_status for s in result.scenario_results.values()}
            if statuses == {"HIT"}:
                result.cache_status = "HIT"
            elif "BLOCKED" in statuses:
                result.cache_status = "BLOCKED"
            else:
                result.cache_status = "MISS"
        result.diagnostics = self._collect_diagnostics(result)

        # Attach provenance (scenario identity bound to every derived decision).
        result.provenance = self._provenance(cand, cset, result)
        return result

    # ------------------------------------------------------------------
    def _evaluate_scenario(self, cand: Any, cset: ConstraintSet | None,
                           scenario: Scenario, work_dir: Path) -> ScenarioQoR:
        sqor = ScenarioQoR(
            candidate_id=getattr(cand, "id", ""),
            scenario_id=scenario.id,
            mode=scenario.mode,
            corner=scenario.corner,
            name=scenario.name,
            backend=self.name,
            tool=self.name,
            tool_version=self.tool_version,
        )
        out = self.evaluate_scenario(scenario, cand, work_dir)
        qor, cache_key, cache_status, run_id = _normalize_scenario_eval(out)
        sqor.qor = qor
        sqor.cache_key = cache_key or scenario_cache_key(
            scenario, cset, backend=self.name,
            tool_version=self.tool_version, tool_path=self.tool_path,
        )
        sqor.cache_status = cache_status or ("HIT" if cache_key else "MISS")
        sqor.run_id = run_id or ""
        if qor is not None:
            qor.candidate_id = getattr(cand, "id", "")
            qor.scenario = scenario.id
            qor.mode = scenario.mode
            qor.corner = scenario.corner
            qor.backend = self.name
            qor.tool = qor.tool or self.name
            qor.run_id = run_id or qor.run_id or ""
            qor.cache_key = sqor.cache_key
            qor.cache_status = sqor.cache_status
            qor.notes = list(getattr(qor, "notes", []))
            qor.notes.append(f"[mcmm] scenario={scenario.id} mode={scenario.mode} "
                             f"corner={scenario.corner}")
        sqor.diagnostics = self._scenario_diagnostics(sqor)
        sqor.provenance = self._scenario_provenance(scenario, sqor)
        return sqor

    # ------------------------------------------------------------------
    def _scenario_diagnostics(self, sqor: ScenarioQoR) -> list[str]:
        diag: list[str] = [f"scenario={sqor.scenario_id} mode={sqor.mode} "
                           f"corner={sqor.corner}"]
        if sqor.qor is not None:
            diag.extend(list(getattr(sqor.qor, "diagnostics", None) or []))
            diag.extend(list(getattr(sqor.qor, "notes", None) or []))
        return diag

    def _scenario_provenance(self, scenario: Scenario, sqor: ScenarioQoR) -> dict[str, Any]:
        return {
            "scenario_id": scenario.id,
            "mode": scenario.mode,
            "corner": scenario.corner,
            "libraries": list(scenario.libraries),
            "parasitics": scenario.parasitics,
            "environment": dict(scenario.environment),
            "cache_key": sqor.cache_key,
            "cache_status": sqor.cache_status,
            "run_id": sqor.run_id,
            "backend": sqor.backend,
            "tool": sqor.tool,
            "tool_version": sqor.tool_version,
        }

    def _collect_diagnostics(self, result: MCMMResult) -> list[str]:
        diag: list[str] = [f"global_status={result.global_status}"]
        if result.global_reason:
            diag.append(f"global_reason={result.global_reason}")
        diag.append(f"scenarios_evaluated={len(result.scenario_results)}")
        diag.append(f"cache_hits={result.cache_hits} cache_misses={result.cache_misses}")
        diag.append(f"eda_runs={result.eda_runs}")
        return diag

    def _provenance(self, cand: Any, cset: ConstraintSet | None,
                    result: MCMMResult) -> dict[str, Any]:
        return {
            "mcmm_enabled": self.matrix.is_enabled,
            "candidate_id": getattr(cand, "id", ""),
            "cset_hash": (cset.semantic_hash() if cset is not None else ""),
            "active_scenarios": list(self.matrix.active_ids),
            "backend": self.name,
            "tool_path": self.tool_path,
            "tool_version": self.tool_version,
        }


def _normalize_scenario_eval(out: Any) -> tuple[QoRResult | None, str, str, str]:
    """Accept a QoRResult or a dict with {qor, cache_key, cache_status, run_id}."""
    if out is None:
        return None, "", "BLOCKED", ""
    if isinstance(out, QoRResult):
        return out, "", "MISS", getattr(out, "run_id", "") or ""
    if isinstance(out, dict):
        qor = out.get("qor") if isinstance(out.get("qor"), QoRResult) else None
        return (
            qor,
            str(out.get("cache_key", "") or ""),
            str(out.get("cache_status", "") or out.get("status", "") or ""),
            str(out.get("run_id", "") or ""),
        )


# ---------------------------------------------------------------------------
# Mock MCMM evaluator factory (Step 12 §16): clearly labelled, deterministic.
# ---------------------------------------------------------------------------

def mock_mcmm_evaluator(matrix: ScenarioMatrix,
                        *,
                        seed: int = 42,
                        base_area: float = 100.0,
                        base_power: float | None = None,
                        scenario_offsets: dict[str, dict[str, float]] | None = None,
                        required_setup_ns: float = 0.0,
                        required_hold_ns: float = 0.0,
                        baseline_by_scenario: dict[str, tuple[float | None, float | None]]
                        | None = None,
                        baseline_qor_by_scenario: dict[str, QoRResult] | None = None,
                        base_cset: ConstraintSet | None = None,
                        ) -> MCMMEvaluator:
    """Build an :class:`MCMMEvaluator` backed by the deterministic mock EDA.

    Every scenario is evaluated with a seed derived from (candidate_id,
    scenario_id, seed) so the result is reproducible and scenario-distinct.
    ``scenario_offsets`` optionally injects per-scenario WNS / area offsets to
    make tests / examples meaningful (e.g. fast corner is tighter on setup).
    """
    baseline_by_scenario = baseline_by_scenario or {}
    baseline_qor_by_scenario = baseline_qor_by_scenario or {}

    def evaluate_scenario(scenario: Scenario, cand: Any, work_dir: Path) -> QoRResult:
        from ..eda import MockEDA
        import random
        from ..qor.model import QoRResult, PowerStatus

        rng = random.Random(f"{getattr(cand, 'id', '')}-{scenario.id}-{seed}")
        offsets = (scenario_offsets or {}).get(scenario.id, {})
        effective_cset = getattr(cand, 'constraint_set', None) or base_cset
        n = len(effective_cset) if effective_cset is not None else 0
        setup_wns = (0.50 - 0.05 * n) + rng.uniform(-0.05, 0.05) + offsets.get("setup_wns", 0.0)
        hold_wns = 0.20 + rng.uniform(-0.05, 0.05) + offsets.get("hold_wns", 0.0)
        if getattr(cand, "generated_changes", None) and any(
                "false_path" in c for c in cand.generated_changes):
            setup_wns += 0.80
        area = base_area + rng.uniform(-2, 5) + 0.3 * n + offsets.get("area", 0.0)
        qor = QoRResult(
            backend="mock", is_mock=True, tool="mock", tool_version="0.1",
            flow_stage="synthesis_sta",
            scenario=scenario.id, mode=scenario.mode, corner=scenario.corner,
            setup_wns=setup_wns * 1e-9,
            setup_tns=min(0.0, setup_wns) * 1e-9,
            setup_violations=0 if setup_wns >= 0 else 1,
            hold_wns=hold_wns * 1e-9,
            hold_tns=min(0.0, hold_wns) * 1e-9,
            hold_violations=0 if hold_wns >= 0 else 1,
            area=area if offsets.get("area_is_proxy", False) is False else None,
            area_proxy=area,
            cell_count=50 + n, ff_count=25,
            power=None, power_status=PowerStatus.UNAVAILABLE.value,
            notes=["MOCK result — not from real EDA tools."],
        )
        # Allow tests to inject real area explicitly.
        if offsets.get("area_is_proxy") is False:
            qor.area = area
            qor.area_proxy = None
        qor.cache_key = scenario_cache_key(
            scenario, effective_cset, backend="mock", tool_version="0.1")
        qor.cache_status = "MISS"
        return qor

    evaluator = MCMMEvaluator(
        matrix=matrix,
        evaluate_scenario=evaluate_scenario,
        base_cset=base_cset,
        name="mock",
        tool_version="0.1",
        tool_path="mock",
    )
    return evaluator


__all__ = ["MCMMEvaluator", "mock_mcmm_evaluator"]
