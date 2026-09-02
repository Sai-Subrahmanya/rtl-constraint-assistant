"""
Candidate constraint set for optimization (Manual §40, §44, §80, §86).

Step 11 extensions (§15): every candidate retains full evaluation provenance —
parent, changed constraints, UCM/SDC identity, experiment cache key, EDA run id,
QoR, feasibility, Pareto status, priority/ranking, margin utilization,
structured explanation, and a rejection reason when rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from ..constraint_model import ConstraintSet
from ..qor.model import QoRResult
from ..utils.enums import CandidateDecision


@dataclass
class Candidate:
    # ---------- identity ----------
    id: str = field(default_factory=lambda: f"C{uuid4().hex[:6].upper()}")
    parent_id: str | None = None
    generation: int = 0
    constraint_model_hash: str = ""
    sdc_hash: str = ""

    # ---------- constraint delta ----------
    constraint_set: ConstraintSet | None = None
    sdc_text: str = ""
    generated_changes: list[str] = field(default_factory=list)
    mutated_constraint_ids: list[str] = field(default_factory=list)

    # ---------- validity / lifecycle ----------
    validity_status: str = "PROPOSED"
    warnings: list[str] = field(default_factory=list)
    decision: CandidateDecision = CandidateDecision.PROPOSED
    decision_reason: str = ""

    # ---------- Step 11: evaluation provenance ----------
    qor: QoRResult | None = None
    coverage: dict[str, Any] | None = None
    # Scenario basis (Step 11 §22)
    scenario: str = "default"
    corner: str = "default"
    mode: str = "default"
    # Experiment identity (Step 10 cache integration)
    cache_key: str = ""
    cache_status: str = ""    # HIT / MISS / N_A / BLOCKED
    run_id: str = ""
    # Feasibility classification
    hard_feasible: bool = False
    blocked: bool = False
    infeasible_reason: str = ""
    diagnostics: list[str] = field(default_factory=list)
    # Pareto / ranking
    pareto_member: bool = False
    rank: int = -1
    priority_score: float = 0.0
    # Margin utilization (computed from QoR against baseline)
    margin_headroom_ns: float | None = None
    margin_utilization: float | None = None
    # Structured explanation (set after final selection)
    explanation: dict[str, Any] = field(default_factory=dict)

    # ---------- helpers ----------

    @property
    def feasible(self) -> bool:
        """Compatibility alias: returns the authoritative optimizer feasibility
        state (`hard_feasible`). It NEVER falls back to re-deriving feasibility
        from raw WNS values — a candidate classified INFEASIBLE due to
        validation errors, unsafe exceptions, missing timing, or hard margin
        floors must stay infeasible even if raw WNS happens to look positive.
        """
        return bool(self.hard_feasible)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent": self.parent_id,
            "generation": self.generation,
            "decision": self.decision.value,
            "validity": self.validity_status,
            "hard_feasible": self.hard_feasible,
            "blocked": self.blocked,
            "infeasible_reason": self.infeasible_reason,
            "feasible": self.feasible,
            "qor": self.qor.summary() if self.qor else None,
            "changes": self.generated_changes,
            "mutated_constraints": self.mutated_constraint_ids,
            "warnings": self.warnings,
            "reason": self.decision_reason,
            "scenario": self.scenario, "corner": self.corner, "mode": self.mode,
            "cache_key": self.cache_key, "cache_status": self.cache_status,
            "run_id": self.run_id,
            "pareto": self.pareto_member,
            "rank": self.rank,
            "margin_headroom_ns": self.margin_headroom_ns,
            "margin_utilization": self.margin_utilization,
            "explanation": self.explanation,
        }
