"""
Assumption ledger (Manual §9.2).

Every important value that isn't directly proven by RTL gets tracked
as an Assumption with a stable ID, so downstream constraints and
analyses can be invalidated when the user changes their intent.

The ledger supports reverse lookups: given an assumption id, you can
ask which constraints depend on it and which analyses use those
constraints.
"""

from __future__ import annotations

import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..utils.enums import Confidence
from .evidence import Evidence, _normalize_confidence, _sort_dict


class Assumption(BaseModel):
    """A single engineering assumption.

    An Assumption represents a design decision that was not derived
    from RTL but is required to emit constraints or run analysis.
    Examples: a clock period, an async relationship between two
    clocks, an IO delay reference.
    """

    model_config = ConfigDict(arbitrary_types_allowed=False)

    id: str
    statement: str
    origin: str = "USER"                        # USER | INFERENCE | TOOL | LIBRARY
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: Confidence = Confidence.MEDIUM
    severity: str = "RECOMMENDED"               # REQUIRED / RECOMMENDED / INFO
    user_confirmed: bool = False
    fixed: bool = False
    default_value: Any = None
    current_value: Any = None
    dependent_constraints: list[str] = Field(default_factory=list)
    dependent_analyses: list[str] = Field(default_factory=list)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    notes: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        d["confidence"] = _normalize_confidence(d.get("confidence", Confidence.MEDIUM))
        evs = d.get("evidence")
        if evs:
            norm = []
            for ev in evs:
                if isinstance(ev, Evidence):
                    norm.append(ev)
                elif isinstance(ev, dict):
                    ev = dict(ev)
                    ev["confidence"] = _normalize_confidence(ev.get("confidence", "MEDIUM"))
                    norm.append(Evidence(**ev))
                else:
                    norm.append(ev)
            d["evidence"] = norm
        return d

    # --------------------------------------------------------------
    # Mutation helpers
    # --------------------------------------------------------------

    def add_evidence(self, ev: Evidence) -> None:
        for existing in self.evidence:
            if existing.semantically_equivalent(ev):
                return
        self.evidence.append(ev)

    def mark_dependent_constraint(self, cid: str) -> None:
        if cid not in self.dependent_constraints:
            self.dependent_constraints.append(cid)

    def mark_dependent_analysis(self, aid: str) -> None:
        if aid not in self.dependent_analyses:
            self.dependent_analyses.append(aid)

    def confirm(self, value: Any = None) -> None:
        self.user_confirmed = True
        if value is not None:
            self.current_value = value

    # -- canonical serialization (lossless) --

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "origin": self.origin,
            "evidence": [e.to_dict() for e in sorted(self.evidence, key=lambda x: x.id)],
            "confidence": self.confidence.value,
            "severity": self.severity,
            "user_confirmed": self.user_confirmed,
            "fixed": self.fixed,
            "default_value": _jsonable(self.default_value),
            "current_value": _jsonable(self.current_value),
            "dependent_constraints": sorted(self.dependent_constraints),
            "dependent_analyses": sorted(self.dependent_analyses),
            "created_at": self.created_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Assumption":
        return cls(
            id=d["id"],
            statement=d["statement"],
            origin=d.get("origin", "USER"),
            evidence=[Evidence.from_dict(e) for e in d.get("evidence", [])],
            confidence=_normalize_confidence(d.get("confidence", "MEDIUM")),
            severity=d.get("severity", "RECOMMENDED"),
            user_confirmed=bool(d.get("user_confirmed", False)),
            fixed=bool(d.get("fixed", False)),
            default_value=_deep_copy_jsonable(d.get("default_value")),
            current_value=_deep_copy_jsonable(d.get("current_value")),
            dependent_constraints=list(d.get("dependent_constraints", [])),
            dependent_analyses=list(d.get("dependent_analyses", [])),
            created_at=d.get("created_at"),
            notes=d.get("notes", ""),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "origin": self.origin,
            "confidence": self.confidence.value,
            "severity": self.severity,
            "confirmed": self.user_confirmed,
            "fixed": self.fixed,
            "default_value": self.default_value,
            "current_value": self.current_value,
            "dependent_constraints": list(self.dependent_constraints),
            "dependent_analyses": list(self.dependent_analyses),
            "evidence_count": len(self.evidence),
        }


class AssumptionLedger:
    """Collects, indexes, and queries assumptions for a project.

    Thread-safe counter so that assumption ids are stable across
    imports/parses within a single run.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._assumptions: dict[str, Assumption] = {}
        self._next_id = 0

    # --------------------------------------------------------------
    # Construction
    # --------------------------------------------------------------

    def _next_aid(self) -> str:
        with self._lock:
            self._next_id += 1
            return f"A{self._next_id:04d}"

    def reset_id_counter(self, start: int = 0) -> None:
        """Reset counter (test-only, not for production pipelines)."""
        with self._lock:
            self._next_id = start

    def add(self, assumption: Assumption) -> Assumption:
        """Register an assumption; ensures id is unique."""
        if not assumption.id:
            # assign a new id; we have to work around frozen? Assumption is not frozen.
            object.__setattr__(assumption, "id", self._next_aid())
        if assumption.id.startswith("A") and assumption.id[1:].isdigit():
            with self._lock:
                self._next_id = max(self._next_id, int(assumption.id[1:]))
        self._assumptions[assumption.id] = assumption
        return assumption

    def make(self, statement: str, *,
             origin: str = "USER",
             confidence: Confidence | str = Confidence.HIGH,
             severity: str = "RECOMMENDED",
             fixed: bool = True,
             user_confirmed: bool = True,
             default_value: Any = None,
             current_value: Any = None,
             evidence: list[Evidence] | None = None,
             notes: str = "") -> Assumption:
        a = Assumption(
            id=self._next_aid(),
            statement=statement,
            origin=origin,
            confidence=_normalize_confidence(confidence),
            severity=severity,
            fixed=fixed,
            user_confirmed=user_confirmed,
            default_value=default_value,
            current_value=current_value if current_value is not None else default_value,
            evidence=list(evidence or []),
            notes=notes,
        )
        return self.add(a)

    # --------------------------------------------------------------
    # Queries
    # --------------------------------------------------------------

    def get(self, aid: str) -> Assumption | None:
        return self._assumptions.get(aid)

    def all(self) -> list[Assumption]:
        return sorted(self._assumptions.values(), key=lambda a: a.id)

    def __iter__(self) -> Iterator[Assumption]:
        return iter(self.all())

    def __len__(self) -> int:
        return len(self._assumptions)

    def __contains__(self, aid: Any) -> bool:
        if isinstance(aid, Assumption):
            return aid.id in self._assumptions
        return aid in self._assumptions

    # --------------------------------------------------------------
    # Reverse dependency lookup
    # --------------------------------------------------------------

    def constraints_for(self, aid: str) -> list[str]:
        a = self._assumptions.get(aid)
        return list(a.dependent_constraints) if a else []

    def analyses_for(self, aid: str) -> list[str]:
        a = self._assumptions.get(aid)
        return list(a.dependent_analyses) if a else []

    def bind_constraint(self, aid: str, cid: str) -> None:
        a = self._assumptions.get(aid)
        if a is not None:
            a.mark_dependent_constraint(cid)

    def bind_analysis(self, aid: str, analysis_id: str) -> None:
        a = self._assumptions.get(aid)
        if a is not None:
            a.mark_dependent_analysis(analysis_id)

    # --------------------------------------------------------------
    # Staleness / invalidation
    # --------------------------------------------------------------

    def stale_consumers(self, changed_aids: set[str]) -> dict[str, Any]:
        stale_c: set[str] = set()
        stale_a: set[str] = set()
        for aid in changed_aids:
            a = self._assumptions.get(aid)
            if a is None:
                continue
            stale_c.update(a.dependent_constraints)
            stale_a.update(a.dependent_analyses)
        return {
            "changed_assumptions": sorted(changed_aids),
            "stale_constraints": sorted(stale_c),
            "stale_analyses": sorted(stale_a),
        }

    # --------------------------------------------------------------
    # Canonical serialization (lossless)
    # --------------------------------------------------------------

    def to_list(self) -> list[dict[str, Any]]:
        """Lossy presentation view (summary only)."""
        return [a.summary() for a in self.all()]

    def to_dict(self) -> dict[str, Any]:
        """Canonical snapshot of the full ledger."""
        return {
            "assumptions": [a.to_dict() for a in self.all()],
            "next_id": self._next_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AssumptionLedger":
        ledger = cls()
        for ad in d.get("assumptions", []):
            a = Assumption.from_dict(ad)
            ledger._assumptions[a.id] = a
        ledger._next_id = int(d.get("next_id", len(ledger._assumptions)))
        return ledger


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _jsonable(v: Any) -> Any:
    """Make a value safe for JSON serialization (deterministic)."""
    import math
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    if isinstance(v, dict):
        return _sort_dict({str(k): _jsonable(x) for k, x in v.items()})
    if isinstance(v, (list, tuple, set, frozenset)):
        items = [_jsonable(x) for x in v]
        try:
            return sorted(items, key=lambda z: str(z))
        except Exception:
            return items
    # Fallback: stringify
    return str(v)


def _deep_copy_jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return {k: _deep_copy_jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_deep_copy_jsonable(x) for x in v]
    return v
