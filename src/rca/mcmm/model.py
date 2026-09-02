"""
MCMM (Multi-Mode / Multi-Corner) data model (Step 12).

A per-candidate MCMM evaluation produces one :class:`ScenarioQoR` record per
active scenario and a single :class:`MCMMResult` aggregate.  The aggregate
is *never* a collapse of the individual QoR into one number: every scenario
retains its own QoR, feasibility, cache identity, run id, backend, tool and
diagnostics.  The aggregate carries global feasibility, conservative/binding
global objectives, the limiting scenario(s), and a global margin signal.

Design invariants (Step 12 §3–§8):
- Each candidate is evaluated across ALL active scenarios.
- Scenario identity (mode/corner/libraries/parasitics/environment) is attached
  to every per-scenario value and decision.
- UNKNOWN values stay UNKNOWN; never fabricate missing metrics.
- Area keeps real/proxy source semantics; real-vs-proxy is INCOMPARABLE.
- margin_utilization is a diagnostic, not a Pareto objective.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..qor.model import QoRResult

# Per-scenario feasibility status vocabulary (Step 12 §4).
FEASIBLE = "feasible"
INFEASIBLE = "infeasible"
BLOCKED = "blocked"
INVALID = "invalid"

# Global feasibility status vocabulary (conservative ordering).
GLOBAL_FEASIBLE = "feasible"
GLOBAL_INFEASIBLE = "infeasible"
GLOBAL_BLOCKED = "blocked"
GLOBAL_INVALID = "invalid"

_AREA_SOURCE_UNKNOWN = "unknown"


@dataclass
class ScenarioQoR:
    """Per-scenario evaluation record for one candidate.

    Retains every attribute required for auditability (Step 12 §3): candidate
    identity, scenario identity, mode, corner, per-scenario QoR, per-scenario
    feasibility, cache key, cache status, run id, backend/tool, diagnostics and
    provenance.
    """

    # ---------- identity ----------
    candidate_id: str = ""
    scenario_id: str = ""
    mode: str = "functional"
    corner: str = "slow"
    name: str = ""

    # ---------- per-scenario QoR ----------
    qor: QoRResult | None = None

    # ---------- per-scenario feasibility ----------
    feasible: bool = False
    blocked: bool = False
    invalid: bool = False
    status: str = INFEASIBLE          # feasible | infeasible | blocked | invalid
    infeasible_reason: str = ""

    # ---------- experiment provenance ----------
    cache_key: str = ""
    cache_status: str = "MISS"        # HIT / MISS / N_A / BLOCKED
    run_id: str = ""
    backend: str = ""
    tool: str = ""
    tool_version: str = ""

    # ---------- diagnostics / margin ----------
    diagnostics: list[str] = field(default_factory=list)
    margin_headroom_ns: float | None = None
    margin_utilization: float | None = None
    # Scenario role from global aggregation (set by aggregate.collapse).
    limiting: bool = False
    is_global_binding: bool = False

    # ---------- provenance (Step 12 §15) ----------
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "scenario_id": self.scenario_id,
            "mode": self.mode,
            "corner": self.corner,
            "name": self.name,
            "feasible": self.feasible,
            "blocked": self.blocked,
            "invalid": self.invalid,
            "status": self.status,
            "infeasible_reason": self.infeasible_reason,
            "qor": self.qor.summary() if self.qor else None,
            "cache_key": self.cache_key,
            "cache_status": self.cache_status,
            "run_id": self.run_id,
            "backend": self.backend,
            "tool": self.tool,
            "tool_version": self.tool_version,
            "diagnostics": list(self.diagnostics),
            "margin_headroom_ns": self.margin_headroom_ns,
            "margin_utilization": self.margin_utilization,
            "limiting": self.limiting,
            "is_global_binding": self.is_global_binding,
            "provenance": dict(self.provenance),
        }


@dataclass
class ObjectiveAggregate:
    """Conservative/binding aggregation of one global objective over scenarios.

    - ``value`` is None when UNKNOWN (some scenario lacked the metric).
    - ``unknown`` is True when any active scenario had an UNKNOWN value for
      this objective, in which case NO value is fabricated.
    - ``limiting`` lists the scenario id(s) responsible for the binding
      (worst-per-direction) value, or the unknown scenario(s).
    - ``area_source`` records the aggregate area source ('real'/'proxy'/
      'unknown') and is INCOMPARABLE when scenarios disagree.
    """

    name: str = ""
    value: float | None = None
    unknown: bool = False
    incomparable: bool = False
    limiting: list[str] = field(default_factory=list)
    direction: str = "maximize"       # maximize | minimize
    area_source: str | None = None
    scenarios: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unknown": self.unknown,
            "incomparable": self.incomparable,
            "limiting": list(self.limiting),
            "direction": self.direction,
            "area_source": self.area_source,
            "scenarios": list(self.scenarios),
        }


@dataclass
class MCMMResult:
    """Aggregated per-candidate MCMM evaluation.

    Holds every per-scenario record plus the global feasibility / objective /
    margin summary.  The aggregate never replaces the per-scenario records —
    callers must consult ``scenario_results`` for scenario-level detail.
    """

    candidate_id: str = ""
    scenario_results: dict[str, ScenarioQoR] = field(default_factory=dict)
    active_scenario_ids: list[str] = field(default_factory=list)

    # ---------- global feasibility (Step 12 §4) ----------
    feasible: bool = False
    infeasible: bool = False
    blocked: bool = False
    invalid: bool = False
    global_status: str = GLOBAL_INFEASIBLE
    global_reason: str = ""
    limiting_scenarios: list[str] = field(default_factory=list)

    # ---------- global objectives (Step 12 §5) ----------
    objectives: dict[str, ObjectiveAggregate] = field(default_factory=dict)

    # ---------- global margin (Step 12 §8) ----------
    margin_headroom_ns: float | None = None
    margin_utilization: float | None = None
    margin_limiting_scenarios: list[str] = field(default_factory=list)

    # ---------- experiment provenance ----------
    cache_key: str = ""
    cache_status: str = ""
    run_ids: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    eda_runs: int = 0
    cache_hits: int = 0
    cache_misses: int = 0

    # ---------- provenance (Step 12 §15) ----------
    provenance: dict[str, Any] = field(default_factory=dict)

    def scenario(self, sid: str) -> ScenarioQoR | None:
        return self.scenario_results.get(sid)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "active_scenarios": list(self.active_scenario_ids),
            "feasible": self.feasible,
            "infeasible": self.infeasible,
            "blocked": self.blocked,
            "invalid": self.invalid,
            "global_status": self.global_status,
            "global_reason": self.global_reason,
            "limiting_scenarios": list(self.limiting_scenarios),
            "objectives": {k: v.to_dict() for k, v in self.objectives.items()},
            "margin_headroom_ns": self.margin_headroom_ns,
            "margin_utilization": self.margin_utilization,
            "margin_limiting_scenarios": list(self.margin_limiting_scenarios),
            "cache_key": self.cache_key,
            "cache_status": self.cache_status,
            "run_ids": list(self.run_ids),
            "diagnostics": list(self.diagnostics),
            "eda_runs": self.eda_runs,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "provenance": dict(self.provenance),
            "scenario_results": {
                sid: r.to_dict() for sid, r in sorted(self.scenario_results.items())
            },
        }


__all__ = [
    "FEASIBLE", "INFEASIBLE", "BLOCKED", "INVALID",
    "GLOBAL_FEASIBLE", "GLOBAL_INFEASIBLE", "GLOBAL_BLOCKED", "GLOBAL_INVALID",
    "ScenarioQoR", "ObjectiveAggregate", "MCMMResult",
]
