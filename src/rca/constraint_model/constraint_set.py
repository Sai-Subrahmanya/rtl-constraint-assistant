"""
ConstraintSet — the UCM container (Manual Invariant 1).

This is the *single source of truth* for constraints. SDC is a derived
representation; downstream tools consume the UCM, never raw SDC.

Two serializations are supported:

* **Presentation summary** (``summary()``/``snapshot()``) — compact view
  intended for CLI output and dashboards; intentionally omits full
  provenance and path-selector detail.
* **Canonical UCM snapshot** (``to_snapshot_dict()``/``from_snapshot_dict()``)
  — lossless, versioned, deterministic representation used for persistence,
  cache keys, reproducibility, candidate reconstruction, and
  auditability.  The schema version is ``UCM_SNAPSHOT_SCHEMA_VERSION``
  and must be incremented on incompatible changes.
"""

from __future__ import annotations

import copy
import json
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from ..provenance import AssumptionLedger, ProvenanceRecord
from ..utils.enums import (
    Confidence,
    ConstraintStatus,
    ConstraintType,
    GenerationConfidence,
    OptimizationStatus,
    SafeMode,
    SourceKind,
)
from ..utils.hashing import stable_hash
from .constraint import UCM_SNAPSHOT_SCHEMA_VERSION, Constraint, _stable_values
from .scenarios import Scenario
from .selectors import PathSelector


class ValidationIssue(BaseModel):
    level: str                           # ERROR | WARNING
    code: str
    message: str
    subject: str | None = None


class SnapshotFormatError(ValueError):
    """Raised when a canonical snapshot cannot be restored safely.

    Carries a ``details`` list describing each integrity problem so
    callers can present, log, or record them without mutating the
    original snapshot.
    """

    def __init__(self, message: str, *, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.details: list[dict[str, Any]] = list(details or [])


class SnapshotRepairRecord(BaseModel):
    """Record of a single repair performed when restoring with repair_reverse_edges=True."""

    code: str                              # e.g. "REVERSE_EDGE_MISMATCH"
    subject: str | None = None
    message: str
    before: Any = None
    after: Any = None


class ConstraintSet(BaseModel):
    """A set of constraints forming a coherent timing model."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = "default"
    constraints: dict[str, Constraint] = Field(default_factory=dict)
    scenarios: dict[str, Scenario] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None
    created_at: str | None = None

    # Assumption ledger is not a pydantic field because it contains
    # thread-safe state; it is serialized explicitly in the canonical
    # snapshot.
    _ledger: AssumptionLedger | None = None
    _counter: int = 0
    _snapshot_repairs: list[SnapshotRepairRecord] = []

    def __init__(self, **data: Any) -> None:
        ledger = data.pop("assumption_ledger", None)
        super().__init__(**data)
        self._ledger = ledger if isinstance(ledger, AssumptionLedger) else AssumptionLedger()
        self._snapshot_repairs = []
        max_n = 0
        for cid in self.constraints:
            try:
                n = int("".join(ch for ch in cid if ch.isdigit()) or 0)
                max_n = max(max_n, n)
            except Exception:
                pass
        self._counter = max_n

    @property
    def snapshot_repairs(self) -> list[SnapshotRepairRecord]:
        """Repair records populated by from_snapshot_dict(repair_reverse_edges=True).

        Non-empty only when the caller explicitly opted into repair
        AND the persisted snapshot was inconsistent.  Normal restore
        leaves this list empty.
        """
        return list(self._snapshot_repairs)

    @property
    def ledger(self) -> AssumptionLedger:
        if self._ledger is None:
            self._ledger = AssumptionLedger()
        return self._ledger

    @ledger.setter
    def ledger(self, value: AssumptionLedger) -> None:
        self._ledger = value

    def _next_id(self, prefix: str = "C") -> str:
        self._counter += 1
        return f"{prefix}{self._counter:04d}"

    # ------------------------------------------------------------------
    # Mutation helpers that maintain reverse edges
    # ------------------------------------------------------------------

    def add_dependency_edge(self, upstream_id: str, downstream_id: str) -> None:
        """Add a forward+reverse dependency edge between two constraints."""
        up = self.constraints.get(upstream_id)
        dn = self.constraints.get(downstream_id)
        if up is None or dn is None or upstream_id == downstream_id:
            return
        if downstream_id not in up.downstream_ids:
            up.downstream_ids.append(downstream_id)
        if upstream_id not in dn.dependency_ids:
            dn.dependency_ids.append(upstream_id)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add(self, c: Constraint) -> Constraint:
        if not c.id or c.id in self.constraints:
            c.id = self._next_id()
        for dep in c.dependency_ids:
            up = self.constraints.get(dep)
            if up is not None and c.id not in up.downstream_ids:
                up.downstream_ids.append(c.id)
        if c.provenance is None:
            c.provenance = ProvenanceRecord(source_kind=c.source_kind)
        self.constraints[c.id] = c
        # Sync assumption <-> constraint links.
        for aid in c.assumption_ids:
            self.ledger.bind_constraint(aid, c.id)
        for aid in c.provenance.assumption_ids:
            self.ledger.bind_constraint(aid, c.id)
        return c

    def remove(self, cid: str) -> None:
        c = self.constraints.pop(cid, None)
        if c is not None:
            for dep in c.dependency_ids:
                up = self.constraints.get(dep)
                if up is not None and cid in up.downstream_ids:
                    up.downstream_ids.remove(cid)

    # ---------------- factory methods ----------------

    def create_clock(
        self, name: str, period_seconds: float | None, source: str = "",
        source_kind: SourceKind = SourceKind.INFERENCE,
        confidence: Confidence = Confidence.MEDIUM,
        status: ConstraintStatus = ConstraintStatus.PROPOSED,
        waveform: list[float] | None = None,
        uncertainty_seconds: float | None = None,
        comment: str | None = None,
        fixed: bool = False,
        scenario_ids: list[str] | None = None,
        assumption_ids: list[str] | None = None,
        provenance: ProvenanceRecord | None = None,
    ) -> Constraint:
        cid = self._next_id("CLK")
        if source_kind == SourceKind.USER and status == ConstraintStatus.PROPOSED:
            status = ConstraintStatus.FIXED if fixed else ConstraintStatus.CONFIRMED
        if fixed:
            opt = OptimizationStatus.FIXED
            status = ConstraintStatus.FIXED
        else:
            opt = OptimizationStatus.TUNABLE
        values: dict[str, Any] = {"name": name, "period": period_seconds}
        if waveform is not None:
            values["waveform"] = waveform
        if uncertainty_seconds is not None:
            values["uncertainty"] = uncertainty_seconds
        c = Constraint(
            id=cid,
            type=ConstraintType.CREATE_CLOCK,
            target_objects=[source or name],
            clock_refs=[name],
            values=values,
            source_kind=source_kind,
            confidence=confidence,
            status=status,
            opt_status=opt,
            generation_confidence=(GenerationConfidence.USER_SPECIFIED
                                   if source_kind == SourceKind.USER
                                   else GenerationConfidence.INFERRED_HIGH_CONFIDENCE),
            comment=comment,
            scenario_ids=list(scenario_ids or []),
            assumption_ids=list(assumption_ids or []),
            provenance=provenance or ProvenanceRecord(source_kind=source_kind),
        )
        return self.add(c)

    def create_generated_clock(
        self, name: str, source: str, master_clock: str,
        divide_by: int | None = None, multiply_by: int | None = None,
        **kw: Any,
    ) -> Constraint:
        cid = self._next_id("GCLK")
        c = Constraint(
            id=cid,
            type=ConstraintType.CREATE_GENERATED_CLOCK,
            target_objects=[source],
            clock_refs=[name, master_clock],
            values={
                "name": name,
                "source": source,
                "master_clock": master_clock,
                "divide_by": divide_by,
                "multiply_by": multiply_by,
            },
            source_kind=kw.get("source_kind", SourceKind.INFERENCE),
            confidence=kw.get("confidence", Confidence.MEDIUM),
            status=kw.get("status", ConstraintStatus.PROPOSED),
            scenario_ids=kw.get("scenario_ids", []),
            assumption_ids=kw.get("assumption_ids", []),
        )
        master_id = kw.get("master_constraint_id")
        if master_id:
            c.dependency_ids.append(master_id)
        return self.add(c)

    def create_input_delay(
        self, port: str, clock: str, delay_seconds: float,
        source_kind: SourceKind = SourceKind.USER, **kw: Any,
    ) -> Constraint:
        cid = self._next_id("INP")
        c = Constraint(
            id=cid,
            type=ConstraintType.SET_INPUT_DELAY,
            target_objects=[port],
            clock_refs=[clock],
            values={"clock": clock, "delay": delay_seconds,
                    "min_max": kw.get("min_max", "max")},
            source_kind=source_kind,
            confidence=kw.get("confidence", Confidence.HIGH if source_kind == SourceKind.USER else Confidence.MEDIUM),
            status=kw.get("status", ConstraintStatus.CONFIRMED if source_kind == SourceKind.USER else ConstraintStatus.PROPOSED),
            scenario_ids=kw.get("scenario_ids", []),
            assumption_ids=kw.get("assumption_ids", []),
        )
        return self.add(c)

    def create_output_delay(
        self, port: str, clock: str, delay_seconds: float,
        source_kind: SourceKind = SourceKind.USER, **kw: Any,
    ) -> Constraint:
        cid = self._next_id("OUT")
        c = Constraint(
            id=cid,
            type=ConstraintType.SET_OUTPUT_DELAY,
            target_objects=[port],
            clock_refs=[clock],
            values={"clock": clock, "delay": delay_seconds,
                    "min_max": kw.get("min_max", "max")},
            source_kind=source_kind,
            confidence=kw.get("confidence", Confidence.HIGH if source_kind == SourceKind.USER else Confidence.MEDIUM),
            status=kw.get("status", ConstraintStatus.CONFIRMED if source_kind == SourceKind.USER else ConstraintStatus.PROPOSED),
            scenario_ids=kw.get("scenario_ids", []),
            assumption_ids=kw.get("assumption_ids", []),
        )
        return self.add(c)

    def create_false_path(
        self, from_set: list[str] | None = None, to_set: list[str] | None = None,
        through: list[list[str]] | None = None, **kw: Any,
    ) -> Constraint:
        cid = self._next_id("FP")
        sel = PathSelector(
            from_set=from_set or [], to_set=to_set or [], through_set=through or [],
            min_max=kw.get("min_max", "both"),
            setup_hold=kw.get("setup_hold", "both"),
        )
        c = Constraint(
            id=cid,
            type=ConstraintType.SET_FALSE_PATH,
            path_selector=sel,
            source_kind=kw.get("source_kind", SourceKind.INFERENCE),
            confidence=kw.get("confidence", Confidence.LOW),
            status=kw.get("status", ConstraintStatus.REQUIRES_CONFIRMATION),
            values=kw.get("values", {}),
            scenario_ids=kw.get("scenario_ids", []),
            assumption_ids=kw.get("assumption_ids", []),
        )
        return self.add(c)

    def create_multicycle(
        self, cycles: int, from_set: list[str] | None = None,
        to_set: list[str] | None = None, **kw: Any,
    ) -> Constraint:
        cid = self._next_id("MC")
        c = Constraint(
            id=cid,
            type=ConstraintType.SET_MULTICYCLE_PATH,
            path_selector=PathSelector(from_set=from_set or [], to_set=to_set or []),
            values={"cycles": cycles, "min_max": kw.get("min_max", "max")},
            source_kind=kw.get("source_kind", SourceKind.INFERENCE),
            confidence=kw.get("confidence", Confidence.LOW),
            status=kw.get("status", ConstraintStatus.REQUIRES_CONFIRMATION),
            scenario_ids=kw.get("scenario_ids", []),
            assumption_ids=kw.get("assumption_ids", []),
        )
        return self.add(c)

    def create_clock_groups(
        self, groups: list[list[str]], relationship: str = "asynchronous", **kw: Any,
    ) -> Constraint:
        cid = self._next_id("CG")
        c = Constraint(
            id=cid,
            type=ConstraintType.SET_CLOCK_GROUPS,
            values={"groups": groups, "relationship": relationship},
            clock_refs=sorted({c for g in groups for c in g}),
            source_kind=kw.get("source_kind", SourceKind.INFERENCE),
            confidence=kw.get("confidence", Confidence.MEDIUM),
            status=kw.get("status", ConstraintStatus.PROPOSED),
            scenario_ids=kw.get("scenario_ids", []),
            assumption_ids=kw.get("assumption_ids", []),
        )
        return self.add(c)

    def create_clock_uncertainty(self, clock: str, uncertainty_seconds: float, **kw: Any) -> Constraint:
        cid = self._next_id("UNC")
        c = Constraint(
            id=cid,
            type=ConstraintType.SET_CLOCK_UNCERTAINTY,
            target_objects=[clock],
            clock_refs=[clock],
            values={"uncertainty": uncertainty_seconds},
            source_kind=kw.get("source_kind", SourceKind.USER),
            confidence=Confidence.HIGH,
            status=ConstraintStatus.CONFIRMED,
            scenario_ids=kw.get("scenario_ids", []),
        )
        return self.add(c)

    def add_constraint_by_type(self, t: ConstraintType, name: str = "", period: float | None = None,
                               source: str = "", waveform: list[float] | None = None,
                               targets: list[str] | None = None,
                               source_kind: SourceKind = SourceKind.INFERENCE,
                               confidence: Confidence = Confidence.MEDIUM,
                               status: ConstraintStatus = ConstraintStatus.PROPOSED,
                               provenance: ProvenanceRecord | None = None,
                               opt_status: OptimizationStatus = OptimizationStatus.TUNABLE,
                               comment: str | None = None,
                               scenario_ids: list[str] | None = None,
                               assumption_ids: list[str] | None = None,
                               **values: Any) -> Constraint:
        if t == ConstraintType.CREATE_CLOCK:
            c = self.create_clock(name=name or (targets[0] if targets else "clk"),
                                  period_seconds=period,
                                  source=source or name, source_kind=source_kind,
                                  confidence=confidence, status=status,
                                  waveform=waveform, comment=comment,
                                  fixed=(opt_status == OptimizationStatus.FIXED),
                                  scenario_ids=scenario_ids,
                                  assumption_ids=assumption_ids,
                                  provenance=provenance)
        else:
            cid = self._next_id("IMP")
            c = Constraint(id=cid, type=t, target_objects=targets or [],
                           source_kind=source_kind, confidence=confidence,
                           status=status, opt_status=opt_status,
                           values={"name": name, **values}, comment=comment,
                           scenario_ids=list(scenario_ids or []),
                           assumption_ids=list(assumption_ids or []),
                           provenance=provenance or ProvenanceRecord(source_kind=source_kind))
            self.add(c)
        return c

    def create_design_rule(self, dr_type: str, value: Any, targets: list[str] | None = None, **kw: Any) -> Constraint:
        type_map = {
            "max_transition": ConstraintType.SET_MAX_TRANSITION,
            "max_capacitance": ConstraintType.SET_MAX_CAPACITANCE,
            "max_fanout": ConstraintType.SET_MAX_FANOUT,
        }
        cid = self._next_id("DR")
        c = Constraint(
            id=cid,
            type=type_map.get(dr_type, ConstraintType.SET_MAX_TRANSITION),
            target_objects=targets or [],
            values={"value": value},
            source_kind=kw.get("source_kind", SourceKind.USER),
            confidence=Confidence.MEDIUM,
            status=ConstraintStatus.CONFIRMED,
        )
        return self.add(c)

    # ------------------------------------------------------------------
    # Scenarios
    # ------------------------------------------------------------------

    def add_scenario(self, s: Scenario) -> Scenario:
        self.scenarios[s.id] = s
        return s

    def get_scenario(self, sid: str) -> Scenario | None:
        return self.scenarios.get(sid)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def by_type(self, t: ConstraintType) -> list[Constraint]:
        return [c for c in self.constraints.values() if c.type == t and not c.disabled]

    def clocks(self) -> list[Constraint]:
        return self.by_type(ConstraintType.CREATE_CLOCK)

    def generated_clocks(self) -> list[Constraint]:
        return self.by_type(ConstraintType.CREATE_GENERATED_CLOCK)

    def exceptions(self) -> list[Constraint]:
        return [c for c in self.constraints.values()
                if c.type in (ConstraintType.SET_FALSE_PATH, ConstraintType.SET_MULTICYCLE_PATH,
                              ConstraintType.SET_MIN_DELAY, ConstraintType.SET_MAX_DELAY)
                and not c.disabled]

    def io_constraints(self) -> list[Constraint]:
        return [c for c in self.constraints.values()
                if c.type in (ConstraintType.SET_INPUT_DELAY, ConstraintType.SET_OUTPUT_DELAY)
                and not c.disabled]

    def __iter__(self) -> Iterator[Constraint]:
        return iter(sorted(self.constraints.values(), key=lambda c: c.id))

    def __len__(self) -> int:
        return len(self.constraints)

    def get(self, cid: str) -> Constraint | None:
        return self.constraints.get(cid)

    # ------------------------------------------------------------------
    # Emittable filtering
    # ------------------------------------------------------------------

    def emittable(self, mode: SafeMode = SafeMode.BALANCED) -> list[Constraint]:
        order = {
            ConstraintType.CREATE_CLOCK: 1,
            ConstraintType.CREATE_GENERATED_CLOCK: 2,
            ConstraintType.SET_CLOCK_UNCERTAINTY: 3,
            ConstraintType.SET_CLOCK_LATENCY: 4,
            ConstraintType.SET_PROPAGATED_CLOCK: 5,
            ConstraintType.SET_INPUT_DELAY: 6,
            ConstraintType.SET_OUTPUT_DELAY: 7,
            ConstraintType.SET_DRIVING_CELL: 8,
            ConstraintType.SET_INPUT_TRANSITION: 9,
            ConstraintType.SET_LOAD: 10,
            ConstraintType.SET_MAX_TRANSITION: 11,
            ConstraintType.SET_MAX_CAPACITANCE: 12,
            ConstraintType.SET_MAX_FANOUT: 13,
            ConstraintType.SET_CLOCK_GROUPS: 14,
            ConstraintType.SET_FALSE_PATH: 15,
            ConstraintType.SET_MULTICYCLE_PATH: 16,
            ConstraintType.SET_MIN_DELAY: 17,
            ConstraintType.SET_MAX_DELAY: 18,
        }
        mode_str = mode.value if isinstance(mode, SafeMode) else str(mode)
        eligible = [c for c in self.constraints.values() if c.is_safe_to_emit(mode_str)]
        return sorted(eligible, key=lambda c: (order.get(c.type, 99), c.id))

    # ------------------------------------------------------------------
    # Validation / invariants
    # ------------------------------------------------------------------

    def validate(self, ledger: AssumptionLedger | None = None) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        ledger = ledger or self.ledger
        seen_ids: set[str] = set()
        for cid in self.constraints:
            if cid in seen_ids:
                issues.append(ValidationIssue(level="ERROR", code="DUP_ID",
                                              message=f"duplicate constraint id {cid}", subject=cid))
            seen_ids.add(cid)

        for c in self.constraints.values():
            for p in c.validate_invariants():
                issues.append(ValidationIssue(level="ERROR", code="INVARIANT",
                                              message=f"{c.id}: {p}", subject=c.id))
            for aid in c.assumption_ids:
                if aid not in ledger:
                    issues.append(ValidationIssue(level="ERROR", code="BAD_ASSUMPTION",
                                                  message=f"{c.id} references missing assumption {aid}",
                                                  subject=c.id))
            for aid in c.provenance.assumption_ids:
                if aid not in ledger:
                    issues.append(ValidationIssue(level="ERROR", code="BAD_PROV_ASSUMPTION",
                                                  message=f"{c.id} provenance references missing assumption {aid}",
                                                  subject=c.id))
            for did in c.dependency_ids:
                if did not in self.constraints:
                    issues.append(ValidationIssue(level="ERROR", code="BAD_DEPENDENCY",
                                                  message=f"{c.id} depends on missing constraint {did}",
                                                  subject=c.id))
            for did in c.downstream_ids:
                if did not in self.constraints:
                    issues.append(ValidationIssue(level="ERROR", code="BAD_DOWNSTREAM",
                                                  message=f"{c.id} lists missing downstream {did}",
                                                  subject=c.id))
            # Self dependencies
            if c.id in c.dependency_ids:
                issues.append(ValidationIssue(level="ERROR", code="SELF_DEPENDENCY",
                                              message=f"{c.id} lists itself in dependency_ids",
                                              subject=c.id))
            if c.id in c.downstream_ids:
                issues.append(ValidationIssue(level="ERROR", code="SELF_DEPENDENCY",
                                              message=f"{c.id} lists itself in downstream_ids",
                                              subject=c.id))
            # Reverse-edge consistency: if B depends on A, A.downstream must contain B
            for up_id in c.dependency_ids:
                up = self.constraints.get(up_id)
                if up is not None and c.id not in up.downstream_ids:
                    issues.append(ValidationIssue(level="ERROR", code="REVERSE_EDGE_MISMATCH",
                                                  message=f"{up_id} -> {c.id} forward edge exists but reverse is missing",
                                                  subject=c.id))
            for dn_id in c.downstream_ids:
                dn = self.constraints.get(dn_id)
                if dn is not None and c.id not in dn.dependency_ids:
                    issues.append(ValidationIssue(level="ERROR", code="REVERSE_EDGE_MISMATCH",
                                                  message=f"{c.id} -> {dn_id} reverse edge exists but forward is missing",
                                                  subject=c.id))
            for sid in c.scenario_ids:
                if sid not in self.scenarios:
                    issues.append(ValidationIssue(level="WARNING", code="BAD_SCENARIO",
                                                  message=f"{c.id} references unknown scenario {sid}",
                                                  subject=c.id))
            # Provenance normalization
            if c.provenance is not None:
                if not isinstance(c.provenance.source_kind, SourceKind):
                    issues.append(ValidationIssue(level="ERROR", code="BAD_PROV_SOURCE_KIND",
                                                  message=f"{c.id} provenance.source_kind not normalized",
                                                  subject=c.id))
                for ev in c.provenance.evidence:
                    if ev.id is None or not ev.kind:
                        issues.append(ValidationIssue(level="ERROR", code="BAD_EVIDENCE",
                                                      message=f"{c.id} has invalid evidence record",
                                                      subject=c.id))

        for sid, s in self.scenarios.items():
            if s.parent_scenario_id and s.parent_scenario_id not in self.scenarios:
                issues.append(ValidationIssue(level="WARNING", code="BAD_PARENT_SCENARIO",
                                              message=f"scenario {sid} references missing parent {s.parent_scenario_id}",
                                              subject=sid))

        # Cycles are warned at WARNING level; they do not block
        # emission because the stale-set walker tolerates them.  Callers
        # may pass allow_cycles=False to from_snapshot_dict to reject
        # them at restore time.
        for cyc in _find_dependency_cycles(self):
            issues.append(ValidationIssue(level="WARNING", code="DEPENDENCY_CYCLE",
                                          message=f"dependency cycle detected: {' -> '.join(cyc)}",
                                          subject=" -> ".join(cyc)))

        for c in self.constraints.values():
            if c.is_rejected() and c.is_safe_to_emit("exploratory"):
                issues.append(ValidationIssue(level="ERROR", code="REJECTED_EMITTABLE",
                                              message=f"{c.id} is {c.status.value} but would be emitted",
                                              subject=c.id))
        return issues

    def find_semantic_duplicates(self) -> list[list[str]]:
        groups: dict[tuple, list[str]] = {}
        for c in self.constraints.values():
            groups.setdefault(c.semantic_key(), []).append(c.id)
        return [sorted(ids) for ids in groups.values() if len(ids) > 1]

    # ------------------------------------------------------------------
    # Dependency graph / invalidation
    # ------------------------------------------------------------------

    def downstream_closure(self, cid: str) -> set[str]:
        seen: set[str] = set()
        q: deque[str] = deque([cid])
        while q:
            cur = q.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            c = self.constraints.get(cur)
            if c is None:
                continue
            for d in c.downstream_ids:
                if d not in seen:
                    q.append(d)
        return seen

    def stale_set(self, changed_constraint_ids: set[str] | None = None,
                  changed_assumption_ids: set[str] | None = None,
                  ledger: AssumptionLedger | None = None) -> dict[str, Any]:
        ledger = ledger or self.ledger
        stale_c: set[str] = set(changed_constraint_ids or set())
        if changed_assumption_ids:
            for c in self.constraints.values():
                aids = set(c.assumption_ids) | set(c.provenance.assumption_ids)
                if any(a in changed_assumption_ids for a in aids):
                    stale_c.add(c.id)
            info = ledger.stale_consumers(set(changed_assumption_ids))
            stale_c.update(info["stale_constraints"])
        closed: set[str] = set()
        for cid in list(stale_c):
            closed.update(self.downstream_closure(cid))
        stale_a: set[str] = set()
        for cid in closed:
            c = self.constraints.get(cid)
            if c is not None:
                stale_a.update(c.dependent_analyses)
        if changed_assumption_ids:
            stale_a.update(ledger.stale_consumers(set(changed_assumption_ids))["stale_analyses"])
        return {
            "changed_constraints": sorted(changed_constraint_ids or set()),
            "changed_assumptions": sorted(changed_assumption_ids or set()),
            "stale_constraints": sorted(closed),
            "stale_analyses": sorted(stale_a),
        }

    # ------------------------------------------------------------------
    # Clone / candidate isolation
    # ------------------------------------------------------------------

    def clone(self, *, name: str | None = None,
              clone_assumptions: bool = True) -> "ConstraintSet":
        new = ConstraintSet(
            name=name or self.name,
            metadata=copy.deepcopy(self.metadata),
            run_id=self.run_id,
            created_at=self.created_at,
        )
        for sid, s in self.scenarios.items():
            new.scenarios[sid] = s.model_copy(deep=True)
        for cid, c in self.constraints.items():
            new.constraints[cid] = c.clone(new_id=cid)
        new._counter = self._counter
        if clone_assumptions:
            # Restore ledger from canonical snapshot (deep copy).
            new._ledger = AssumptionLedger.from_dict(self.ledger.to_dict())
        else:
            new._ledger = self.ledger  # shared
        return new

    # ------------------------------------------------------------------
    # CANONICAL SEMANTIC IDENTITY (deterministic hash for dedup / cache)
    # ------------------------------------------------------------------

    def semantic_hash(self) -> str:
        """Return a deterministic SHA-256 hash of the *semantic* identity of
        this ConstraintSet. Used for candidate dedup, EDA cache keys, and
        reproducibility checks. Delegates to the module-level canonical
        `stable_hash_cset` so there is exactly one identity implementation.
        """
        return stable_hash_cset(self)

    # ------------------------------------------------------------------
    # PRESENTATION summary (compact, lossy by design)
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return self.summary_dict()

    def summary_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "constraints": {cid: self.constraints[cid].summary()
                            for cid in sorted(self.constraints.keys())},
            "scenarios": {sid: s.summary() for sid, s in sorted(self.scenarios.items())},
            "metadata": _stable_values(self.metadata),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.summary_dict(), indent=indent, sort_keys=True, default=str)

    # ------------------------------------------------------------------
    # CANONICAL snapshot (lossless, versioned)
    # ------------------------------------------------------------------

    def to_snapshot_dict(self, *, include_assumptions: bool = True) -> dict[str, Any]:
        return {
            "schema_version": UCM_SNAPSHOT_SCHEMA_VERSION,
            "name": self.name,
            "run_id": self.run_id,
            "created_at": self.created_at
                or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metadata": _stable_values(copy.deepcopy(self.metadata)),
            "scenarios": {sid: self._scenario_dict(s)
                          for sid, s in sorted(self.scenarios.items())},
            "constraints": {cid: self.constraints[cid].to_canonical_dict()
                            for cid in sorted(self.constraints.keys())},
            "assumptions": self.ledger.to_dict() if include_assumptions else None,
        }

    def to_canonical_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_snapshot_dict(), indent=indent,
                          sort_keys=False, default=str, ensure_ascii=False)

    @classmethod
    def from_snapshot_dict(cls, snap: dict[str, Any],
                           *, unknown_field_policy: str = "keep",
                           schema_version: int | None = None,
                           repair_reverse_edges: bool = False,
                           allow_cycles: bool = True) -> "ConstraintSet":
        """Restore a ConstraintSet from a canonical snapshot dict.

        Parameters
        ----------
        repair_reverse_edges : bool, default False
            If False (the default) and the snapshot's dependency_ids /
            downstream_ids disagree, a :class:`SnapshotFormatError` is
            raised listing every mismatch; the caller's data is NOT
            mutated and no ConstraintSet is returned.
            If True, reverse edges are rebuilt from forward edges, and
            each repair is recorded on the returned ConstraintSet in
            ``.snapshot_repairs`` so the inconsistency is auditable.
            Under no circumstances is the original ``snap`` dict
            mutated.
        allow_cycles : bool, default True
            If False, dependency cycles raise SnapshotFormatError.
            Default is True because the UCM treats cycles as a
            permitted-but-reported topology (the stale-set walker
            handles them gracefully); they are still warned on by
            :meth:`validate`.

        Raises
        ------
        SnapshotFormatError
            On any structural integrity violation (missing
            schema_version, unsupported schema_version, missing
            dependency/downstream references, self-dependencies,
            reverse-edge mismatches when repair_reverse_edges is
            False, or cycles when allow_cycles is False).
        """
        # ---- schema version gating ----
        ver = snap.get("schema_version")
        expected = schema_version if schema_version is not None else UCM_SNAPSHOT_SCHEMA_VERSION
        if ver is None:
            raise SnapshotFormatError(
                "snapshot missing schema_version; refusing to restore unversioned data",
                details=[{"code": "MISSING_SCHEMA_VERSION", "message": "schema_version field absent"}],
            )
        if ver > expected:
            raise SnapshotFormatError(
                f"snapshot schema_version {ver} is newer than supported {expected}; "
                "refusing to restore to avoid silent data loss",
                details=[{"code": "FUTURE_SCHEMA", "subject": str(ver),
                          "message": f"supported version is {expected}"}],
            )
        if ver != 1:
            raise SnapshotFormatError(f"no migration available from schema_version {ver}",
                                      details=[{"code": "NO_MIGRATION", "subject": str(ver),
                                                "message": "only v1 is supported"}])

        cs = cls(
            name=snap.get("name", "default"),
            metadata=copy.deepcopy(snap.get("metadata", {})),
            run_id=snap.get("run_id"),
            created_at=snap.get("created_at"),
        )

        # ---- restore scenarios ----
        for sid, sd in snap.get("scenarios", {}).items():
            cs.scenarios[sid] = Scenario(
                id=sid,
                mode=sd.get("mode", "functional"),
                corner=sd.get("corner", "slow"),
                libraries=list(sd.get("libraries", [])),
                parasitics=sd.get("parasitics"),
                sdc_set_id=sd.get("sdc_set_id"),
                environment=copy.deepcopy(sd.get("environment", {})),
                active=sd.get("active", True),
                analysis_count=int(sd.get("analysis_count", 0)),
                parent_scenario_id=sd.get("parent"),
            )

        # ---- restore constraints (deep copy; the caller's dict is untouched) ----
        for cid, cd in snap.get("constraints", {}).items():
            c = Constraint.from_canonical_dict(copy.deepcopy(cd),
                                               unknown_field_policy=unknown_field_policy)
            cs.constraints[cid] = c
        cs._counter = _id_numeric_max(cs.constraints.keys())

        # ---- restore ledger ----
        a = snap.get("assumptions")
        if a is not None:
            cs._ledger = AssumptionLedger.from_dict(a)

        # ---- integrity checks (NEVER mutate snapshot, NEVER silently drop refs) ----
        issues: list[dict[str, Any]] = []
        # 1. missing dependency / downstream references & self dependencies
        for cid, c in cs.constraints.items():
            if cid in c.dependency_ids:
                issues.append({"code": "SELF_DEPENDENCY", "subject": cid,
                               "message": f"{cid} lists itself in dependency_ids",
                               "edge": (cid, cid)})
            if cid in c.downstream_ids:
                issues.append({"code": "SELF_DEPENDENCY", "subject": cid,
                               "message": f"{cid} lists itself in downstream_ids",
                               "edge": (cid, cid)})
            for did in c.dependency_ids:
                if did not in cs.constraints:
                    issues.append({"code": "MISSING_DEPENDENCY", "subject": cid,
                                   "message": f"{cid} depends on missing constraint {did}",
                                   "missing": did})
            for did in c.downstream_ids:
                if did not in cs.constraints:
                    issues.append({"code": "MISSING_DOWNSTREAM", "subject": cid,
                                   "message": f"{cid} lists missing downstream {did}",
                                   "missing": did})

        # 2. reverse-edge consistency — collect every disagreement
        reverse_mismatches: list[dict[str, Any]] = []
        for cid, c in cs.constraints.items():
            for up_id in c.dependency_ids:
                up = cs.constraints.get(up_id)
                if up is not None and cid not in up.downstream_ids:
                    reverse_mismatches.append({
                        "code": "REVERSE_EDGE_MISMATCH",
                        "subject": cid,
                        "message": f"forward edge {up_id} -> {cid} exists but reverse edge is missing from {up_id}.downstream_ids",
                        "up": up_id, "down": cid,
                        "upstream_downstream_ids": list(up.downstream_ids),
                    })
            for dn_id in c.downstream_ids:
                dn = cs.constraints.get(dn_id)
                if dn is not None and cid not in dn.dependency_ids:
                    reverse_mismatches.append({
                        "code": "REVERSE_EDGE_MISMATCH",
                        "subject": cid,
                        "message": f"reverse edge {cid} -> {dn_id} exists but forward edge is missing from {dn_id}.dependency_ids",
                        "up": cid, "down": dn_id,
                        "downstream_dependency_ids": list(dn.dependency_ids),
                    })

        # 3. cycles
        cycles = _find_dependency_cycles(cs)
        if cycles and not allow_cycles:
            for cyc in cycles:
                issues.append({"code": "DEPENDENCY_CYCLE", "subject": " -> ".join(cyc),
                               "message": f"dependency cycle detected: {' -> '.join(cyc)}",
                               "cycle": cyc})

        # Any structural (non-reverse-edge) issue is always fatal — do not return
        # a broken UCM even under repair mode, because we cannot safely guess
        # what to delete.
        fatal = [i for i in issues]
        if fatal:
            raise SnapshotFormatError(
                f"canonical snapshot failed integrity checks ({len(fatal)} issue(s)); "
                "the original snapshot was not mutated",
                details=fatal,
            )

        # Reverse-edge mismatches: policy applies.
        if reverse_mismatches and not repair_reverse_edges:
            raise SnapshotFormatError(
                f"canonical snapshot has inconsistent dependency/downstream edges "
                f"({len(reverse_mismatches)} mismatch(es)); refusing to restore. "
                "Pass repair_reverse_edges=True to explicitly rebuild reverse edges "
                "(repairs will be recorded on the returned ConstraintSet).",
                details=reverse_mismatches,
            )

        if reverse_mismatches and repair_reverse_edges:
            # Rebuild downstream_ids from forward edges and record each repair.
            expected_down: dict[str, set[str]] = {cid: set() for cid in cs.constraints}
            for cid, c in cs.constraints.items():
                for dep in c.dependency_ids:
                    if dep in expected_down:
                        expected_down[dep].add(cid)
            for cid, c in cs.constraints.items():
                actual = set(c.downstream_ids)
                want = expected_down.get(cid, set())
                if actual != want:
                    before = sorted(actual)
                    after = sorted(want)
                    cs._snapshot_repairs.append(SnapshotRepairRecord(
                        code="REVERSE_EDGE_REPAIRED",
                        subject=cid,
                        message=f"{cid}.downstream_ids repaired to match dependency graph",
                        before=before, after=after,
                    ))
                    c.downstream_ids = after
            # After repair there should be zero mismatches; re-validate to be sure.
            remaining = []
            for cid, c in cs.constraints.items():
                for up_id in c.dependency_ids:
                    up = cs.constraints.get(up_id)
                    if up is not None and cid not in up.downstream_ids:
                        remaining.append((up_id, cid))
                for dn_id in c.downstream_ids:
                    dn = cs.constraints.get(dn_id)
                    if dn is not None and cid not in dn.dependency_ids:
                        remaining.append((cid, dn_id))
            if remaining:
                raise SnapshotFormatError(
                    f"reverse-edge repair failed; {len(remaining)} mismatches remain",
                    details=[{"code": "REPAIR_FAILED", "edges": remaining}],
                )

        # Cycle warnings are recorded even when allowed.
        for cyc in cycles:
            cs._snapshot_repairs.append(SnapshotRepairRecord(
                code="DEPENDENCY_CYCLE_DETECTED",
                subject=" -> ".join(cyc),
                message=f"dependency cycle present in snapshot (allowed by policy): {' -> '.join(cyc)}",
                before=list(cyc), after=list(cyc),
            ))

        return cs

    @classmethod
    def from_canonical_json(cls, text: str, **kw: Any) -> "ConstraintSet":
        return cls.from_snapshot_dict(json.loads(text), **kw)

    # ---- helpers ----

    @staticmethod
    def _scenario_dict(s: Scenario) -> dict[str, Any]:
        return {
            "id": s.id,
            "mode": s.mode,
            "corner": s.corner,
            "libraries": list(s.libraries),
            "parasitics": s.parasitics,
            "sdc_set_id": s.sdc_set_id,
            "environment": _stable_values(copy.deepcopy(s.environment)),
            "active": s.active,
            "analysis_count": s.analysis_count,
            "parent": s.parent_scenario_id,
        }

    @classmethod
    def from_snapshot(cls, snap: dict[str, Any], **kw: Any) -> "ConstraintSet":
        """Backward-compat alias: previously summary-level restore.

        NOTE: for new code, prefer ``from_snapshot_dict`` which performs
        a lossless restore.  This method now delegates to the lossless
        restore because callers expected a usable ConstraintSet; the
        previous summary-level behavior is preserved by
        ``summary_dict()``.  Pre-v1 (no ``schema_version``) snapshots
        go through best-effort legacy rehydration.
        """
        if "schema_version" not in snap:
            return _from_legacy_summary(snap)
        return cls.from_snapshot_dict(snap, **kw)


def _find_dependency_cycles(cs: "ConstraintSet") -> list[list[str]]:
    """Return a list of dependency cycles (each cycle a list of ids).

    Traverses only forward ``dependency_ids`` edges; downstream_ids
    are the inverse.  Uses Tarjan's SCC algorithm and reports any
    SCC of size > 1, plus any node whose dependency_ids contain
    itself (self-loop reported separately as a SELF_DEPENDENCY
    issue by the caller).
    """
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(v: str) -> None:
        index[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in cs.constraints.get(v).dependency_ids:
            if w not in cs.constraints:
                continue
            if w == v:
                continue  # self-loop handled elsewhere
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])
        if lowlink[v] == index[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                sccs.append(sorted(comp))

    for v in list(cs.constraints.keys()):
        if v not in index:
            strongconnect(v)
    return sccs


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _id_numeric_max(ids) -> int:
    m = 0
    for cid in ids:
        try:
            n = int("".join(ch for ch in cid if ch.isdigit()) or 0)
            m = max(m, n)
        except Exception:
            continue
    return m


def _from_legacy_summary(snap: dict[str, Any]) -> ConstraintSet:
    """Best-effort rehydration from the old lossy presentation snapshot."""
    cs = ConstraintSet(name=snap.get("name", "default"),
                       metadata=copy.deepcopy(snap.get("metadata", {})),
                       run_id=snap.get("run_id"),
                       created_at=snap.get("created_at"))
    for sid, sd in snap.get("scenarios", {}).items():
        cs.add_scenario(Scenario(id=sid, mode=sd.get("mode", "functional"),
                                 corner=sd.get("corner", "slow"),
                                 libraries=sd.get("libraries", []),
                                 parasitics=sd.get("parasitics"),
                                 active=sd.get("active", True),
                                 parent_scenario_id=sd.get("parent")))
    type_map = {t.value: t for t in ConstraintType}
    for cid, cd in snap.get("constraints", {}).items():
        t = type_map.get(cd.get("type", ""))
        if t is None:
            continue
        c = Constraint(
            id=cid,
            type=t,
            target_objects=list(cd.get("targets", [])),
            source_objects=list(cd.get("sources", [])),
            clock_refs=list(cd.get("clocks", [])),
            values=copy.deepcopy(cd.get("values", {})),
            source_kind=SourceKind(cd.get("source", "INFERENCE")),
            confidence=Confidence(cd.get("confidence", "MEDIUM")),
            status=ConstraintStatus(cd.get("status", "PROPOSED")),
            opt_status=OptimizationStatus(cd.get("opt_status", "UNKNOWN")),
            scenario_ids=list(cd.get("scenarios", [])),
            assumption_ids=list(cd.get("assumptions", [])),
            dependency_ids=list(cd.get("dependencies", [])),
            comment=cd.get("comment"),
        )
        cs.constraints[cid] = c
    cs._counter = _id_numeric_max(cs.constraints.keys())
    # Best-effort: rebuild downstream_ids from forward edges. This path is
    # only used for pre-v1 lossy snapshots, where reverse edges were never
    # persisted in the first place.
    expected_down: dict[str, set[str]] = {cid: set() for cid in cs.constraints}
    for cid, c in cs.constraints.items():
        for dep in c.dependency_ids:
            if dep in expected_down:
                expected_down[dep].add(cid)
    for cid, c in cs.constraints.items():
        c.downstream_ids = sorted(expected_down.get(cid, set()))
    return cs


# ----------------------------------------------------------------------
# CANONICAL SEMANTIC IDENTITY — single implementation used by optimizer,
# cache, and dedup. Lives at module level (outside ConstraintSet) so it
# can be imported without instantiating anything and so both base.py and
# search.py resolve to the same function object.
# ----------------------------------------------------------------------


def _hashable_val(v: Any) -> Any:
    if isinstance(v, dict):
        return tuple(sorted((str(k), _hashable_val(vv)) for k, vv in v.items()))
    if isinstance(v, list):
        return tuple(_hashable_val(x) for x in v)
    if isinstance(v, set):
        return tuple(sorted(_hashable_val(x) for x in v))
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, str)) or v is None:
        return v
    return str(v)


def _constraint_semantic_key(c: "Constraint") -> tuple:
    """Semantic key for a single Constraint.

    Includes every field that materially affects generated SDC or EDA
    behavior: id, type, values (sorted kv), target/source/clock refs
    (sorted), through_objects (semantically unordered -> sorted), path
    selector semantic key (preserves through-stage order), scenario
    applicability (sorted), opt_status, status, disabled flag, precedence,
    comment.

    Excludes transient/presentation fields: generated_text_by_backend,
    equivalent_forms, provenance runtime ids / timestamps, logs, memory
    addresses, and analysis bookkeeping (evidence/assumption/dependency/
    downstream link lists that are derived not semantic).
    """
    def _sorted_strs(x):
        return tuple(sorted(str(v) for v in (x or [])))

    def _ps_key(ps):
        if ps is None:
            return None
        try:
            return ps.semantic_key()
        except Exception:
            # Fallback preserves stage order for through_set (outer NOT sorted)
            return (
                tuple(sorted(ps.from_set or [])),
                tuple(tuple(sorted(s)) for s in (ps.through_set or [])),
                tuple(sorted(ps.to_set or [])),
            )

    return (
        c.id,
        c.type.value,
        tuple(sorted((str(k), _hashable_val(v)) for k, v in (c.values or {}).items())),
        _sorted_strs(c.target_objects),
        _sorted_strs(c.source_objects),
        _sorted_strs(c.through_objects),
        _sorted_strs(c.clock_refs),
        _ps_key(getattr(c, "path_selector", None)),
        _sorted_strs(getattr(c, "scenario_ids", None) or []),
        (c.opt_status.value if hasattr(c.opt_status, "value") else str(c.opt_status)),
        (c.status.value if hasattr(c.status, "value") else str(c.status)),
        bool(c.disabled),
        int(c.precedence) if c.precedence is not None else 0,
        c.comment,
    )


def stable_hash_cset(cset: "ConstraintSet | None") -> str:
    """Canonical deterministic semantic hash for a ConstraintSet.

    Insertion-order independent at the constraint level (constraints sorted
    by their semantic key before hashing); uses utils.hashing.stable_hash,
    never Python's builtin hash(). Single source of truth for optimizer
    dedup and EDA cache identity.
    """
    if cset is None:
        return ""
    try:
        keys = sorted(_constraint_semantic_key(c) for c in cset)
        return stable_hash(tuple(keys))
    except Exception:
        return stable_hash(str(cset))
