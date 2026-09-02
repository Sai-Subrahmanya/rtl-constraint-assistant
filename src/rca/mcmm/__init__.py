"""
MCMM — Multi-Mode / Multi-Corner support (Step 12).

Public API:

- :func:`build_scenario_matrix`: build the active scenario matrix from config.
- :class:`ScenarioMatrix`: active scenario selection + scenario-aware
  constraint applicability.
- :class:`MCMMEvaluator`: per-candidate evaluation across all active scenarios.
- :func:`mock_mcmm_evaluator`: deterministic mock-backed evaluator for tests.
- :class:`MCMMResult` / :class:`ScenarioQoR` / :class:`ObjectiveAggregate`:
  per-candidate & per-scenario data model.
- aggregate helpers: ``global_feasibility``, ``aggregate_objectives``,
  ``global_margin``, ``mcmm_is_dominating``, ``mcmm_pareto_front``,
  ``mcmm_scalar_score``, ``mcmm_select_final``.

The module integrates with the existing architecture (ConstraintSet, Scenario,
QoR objectives, ToolBackend, stable_hash_cset, provenance ledger) and does NOT
introduce a second scenario model.
"""

from .aggregate import (
    aggregate_objectives,
    finalize_limiting,
    global_feasibility,
    global_margin,
    mcmm_explanation_for,
    mcmm_is_dominating,
    mcmm_pareto_front,
    mcmm_scalar_score,
    mcmm_select_final,
    scenario_feasibility,
    scenario_margin,
)
from .cache import mcmm_run_cache_key, scenario_cache_key, scenario_semantic_key
from .evaluate import MCMMEvaluator, mock_mcmm_evaluator
from .matrix import ScenarioMatrix, build_scenario_matrix
from .model import (
    BLOCKED,
    FEASIBLE,
    GLOBAL_BLOCKED,
    GLOBAL_FEASIBLE,
    GLOBAL_INFEASIBLE,
    GLOBAL_INVALID,
    INFEASIBLE,
    INVALID,
    MCMMResult,
    ObjectiveAggregate,
    ScenarioQoR,
)

__all__ = [
    "ScenarioMatrix",
    "build_scenario_matrix",
    "MCMMEvaluator",
    "mock_mcmm_evaluator",
    "MCMMResult",
    "ScenarioQoR",
    "ObjectiveAggregate",
    "scenario_feasibility",
    "scenario_margin",
    "global_feasibility",
    "aggregate_objectives",
    "global_margin",
    "finalize_limiting",
    "mcmm_is_dominating",
    "mcmm_pareto_front",
    "mcmm_scalar_score",
    "mcmm_select_final",
    "mcmm_explanation_for",
    "scenario_semantic_key",
    "scenario_cache_key",
    "mcmm_run_cache_key",
    "FEASIBLE", "INFEASIBLE", "BLOCKED", "INVALID",
    "GLOBAL_FEASIBLE", "GLOBAL_INFEASIBLE", "GLOBAL_BLOCKED", "GLOBAL_INVALID",
]
