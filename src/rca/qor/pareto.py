"""
Pareto-dominance and candidate comparison (Manual §40, §42, §81, §82, §83).

Backward-compatible wrappers around :mod:`rca.qor.objectives` (Step 11).
New code should use `rca.qor.objectives` directly.
"""

from __future__ import annotations

from . import objectives as _obj
from .objectives import (
    AREA_REAL, AREA_PROXY, AREA_UNKNOWN,
    AreaValue, CompareResult, Direction, FeasibilityResult, ObjectiveSpec,
    OBJECTIVE_SPECS,
)

__all__ = [
    "is_dominating", "pareto_filter", "score_candidate",
    "AREA_REAL", "AREA_PROXY", "AREA_UNKNOWN",
    "AreaValue", "CompareResult", "Direction", "FeasibilityResult",
    "ObjectiveSpec", "OBJECTIVE_SPECS",
]


def is_dominating(a, b) -> bool:
    return _obj.is_dominating(a, b)


def pareto_filter(candidates):
    return _obj.pareto_front(list(candidates))


def score_candidate(c, baseline, priorities):
    return _obj.scalar_score(c, baseline, priorities)
