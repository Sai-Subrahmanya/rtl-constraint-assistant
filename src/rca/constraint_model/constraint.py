"""
Universal Constraint Model — core constraint object (Manual §8).

Design principles for Step 3:

* **Status, Confidence, and Mutability are independent axes.**
    - status         : lifecycle state (FIXED / CONFIRMED / PROPOSED / ...)
    - confidence     : evidence strength  (HIGH / MEDIUM / LOW / UNKNOWN)
    - opt_status     : optimizer mutability (FIXED / TUNABLE / DERIVED / UNKNOWN)
* Provenance is structured (ProvenanceRecord), not free text.
* Each Constraint carries explicit dependency ids (upstream constraints
  and assumptions) for invalidation.
* Multi-scenario applicability uses ``scenario_ids[]``, not a single
  scalar ``scenario`` (preserved as a property for backward compat).
* ``target_objects`` / ``source_objects`` / path selectors are kept
  distinct and carry typed semantics.
* Semantic identity is independent of formatting, unit representation
  and insertion order (see :meth:`semantic_key`).
* Cloning is deep for mutable nested state to avoid candidate aliasing.
"""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..provenance import ProvenanceRecord
from ..utils.enums import (
    CollectionKind,
    Confidence,
    ConstraintStatus,
    ConstraintType,
    GenerationConfidence,
    OptimizationStatus,
    SourceKind,
)
from .selectors import PathSelector
from .targets import TargetRef, targets_from_strings

# Statuses under which a constraint must NOT be emitted to downstream
# tools without explicit reactivation.
_NON_EMITTABLE_STATUSES = {
    ConstraintStatus.REJECTED,
    ConstraintStatus.DEPRECATED,
    ConstraintStatus.MISSING,
}


# ---------------------------------------------------------------------------
# Canonical snapshot schema version.  Increment when the snapshot schema
# changes in a backward-incompatible way; provide migration logic in
# ConstraintSet.from_snapshot_dict.
# ---------------------------------------------------------------------------
UCM_SNAPSHOT_SCHEMA_VERSION = 1


class Constraint(BaseModel):
    """A single normalized constraint, independent of any EDA vendor syntax."""

    model_config = ConfigDict(arbitrary_types_allowed=False, validate_default=False)

    # ---------- identity ----------
    id: str
    type: ConstraintType

    # ---------- target/source semantics (distinct lists) ----------
    target_objects: list[str] = Field(default_factory=list)
    source_objects: list[str] = Field(default_factory=list)
    through_objects: list[str] = Field(default_factory=list)
    clock_refs: list[str] = Field(default_factory=list)
    values: dict[str, Any] = Field(default_factory=dict)

    # ---------- semantic target/clock references (Step 6) ----------
    # These are semantically-typed TargetRef lists. When populated,
    # the SDC generator uses them instead of guessing selector type
    # from string syntax (e.g. "/" -> pin). The plain *_objects lists
    # remain for backward compatibility and presentation.
    target_refs: list[TargetRef] = Field(default_factory=list)
    source_refs: list[TargetRef] = Field(default_factory=list)
    through_refs: list[list[TargetRef]] = Field(default_factory=list)
    clock_refs_typed: list[TargetRef] = Field(default_factory=list)

    # ---------- lifecycle / evidence ----------
    source_kind: SourceKind = SourceKind.INFERENCE
    provenance: ProvenanceRecord = Field(default_factory=ProvenanceRecord)
    confidence: Confidence = Confidence.MEDIUM
    status: ConstraintStatus = ConstraintStatus.PROPOSED
    opt_status: OptimizationStatus = OptimizationStatus.UNKNOWN
    generation_confidence: GenerationConfidence = GenerationConfidence.INFERRED_MEDIUM_CONFIDENCE

    # ---------- dependency graph ----------
    evidence_ids: list[str] = Field(default_factory=list)
    assumption_ids: list[str] = Field(default_factory=list)
    dependency_ids: list[str] = Field(default_factory=list)
    downstream_ids: list[str] = Field(default_factory=list)
    affected_paths: list[str] = Field(default_factory=list)
    dependent_analyses: list[str] = Field(default_factory=list)

    # ---------- scenario applicability ----------
    scenario_ids: list[str] = Field(default_factory=list)

    # ---------- emission ----------
    precedence: int = 0
    disabled: bool = False
    comment: str | None = None

    # ---------- vendor/render cache (TRANSIENT — not in canonical snapshot) ----------
    generated_text_by_backend: dict[str, str] = Field(default_factory=dict, exclude=True)
    equivalent_forms: list[str] = Field(default_factory=list, exclude=True)
    path_selector: PathSelector | None = None

    # ---------- normalization at construction ----------

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        # source_kind
        sk = d.get("source_kind", SourceKind.INFERENCE)
        if not isinstance(sk, SourceKind):
            from ..provenance.provenance import _normalize_source_kind
            d["source_kind"] = _normalize_source_kind(sk)
        # confidence
        cf = d.get("confidence", Confidence.MEDIUM)
        if not isinstance(cf, Confidence):
            from ..provenance.evidence import _normalize_confidence
            d["confidence"] = _normalize_confidence(cf)
        # provenance
        prov = d.get("provenance")
        if prov is not None and not isinstance(prov, ProvenanceRecord):
            if isinstance(prov, dict):
                d["provenance"] = ProvenanceRecord.from_dict(prov)
        elif prov is None:
            d["provenance"] = ProvenanceRecord(source_kind=d.get("source_kind", SourceKind.INFERENCE))
        # path_selector
        ps = d.get("path_selector")
        if ps is not None and not isinstance(ps, PathSelector):
            if isinstance(ps, dict):
                d["path_selector"] = PathSelector.from_dict(ps)
        # Lists: make copies to avoid aliasing defaults.
        for key in ("target_objects", "source_objects", "through_objects",
                    "clock_refs", "evidence_ids", "assumption_ids",
                    "dependency_ids", "downstream_ids", "affected_paths",
                    "dependent_analyses", "scenario_ids"):
            if key in d and d[key] is not None:
                d[key] = list(d[key])
        if "values" in d and d["values"] is not None:
            d["values"] = copy.deepcopy(d["values"])
        # typed refs: convert dicts/strings to TargetRef
        for key in ("target_refs", "source_refs", "clock_refs_typed"):
            if key in d and d[key]:
                normed = []
                for v in d[key]:
                    if isinstance(v, TargetRef):
                        normed.append(v)
                    elif isinstance(v, dict):
                        normed.append(TargetRef.from_dict(v))
                    elif isinstance(v, str):
                        normed.append(TargetRef.literal(v))
                d[key] = normed
        if "through_refs" in d and d["through_refs"]:
            stages = []
            for stage in d["through_refs"]:
                nstage = []
                for v in stage:
                    if isinstance(v, TargetRef):
                        nstage.append(v)
                    elif isinstance(v, dict):
                        nstage.append(TargetRef.from_dict(v))
                    elif isinstance(v, str):
                        nstage.append(TargetRef.literal(v))
                stages.append(nstage)
            d["through_refs"] = stages
        # Synthesize typed refs from plain-string lists when missing,
        # using a safe default kind based on constraint type. This
        # ensures the SDC renderer never has to infer selector kind
        # from name syntax.
        ctype_raw = d.get("type")
        try:
            ctype = ctype_raw if isinstance(ctype_raw, ConstraintType) else (
                ConstraintType(ctype_raw) if ctype_raw else None)
        except Exception:
            ctype = None
        if ctype is not None:
            dk = _default_target_kind(ctype)
            sk_kind = _default_source_kind(ctype)
            if not d.get("target_refs") and d.get("target_objects"):
                d["target_refs"] = targets_from_strings(d["target_objects"], dk)
            if not d.get("source_refs") and d.get("source_objects"):
                d["source_refs"] = targets_from_strings(d["source_objects"], sk_kind)
            if not d.get("clock_refs_typed") and d.get("clock_refs"):
                d["clock_refs_typed"] = targets_from_strings(d["clock_refs"], CollectionKind.CLOCK)
            if not d.get("through_refs") and d.get("through_objects"):
                d["through_refs"] = [targets_from_strings(d["through_objects"], CollectionKind.PIN)]
        return d

    # ---------- backward compatibility shim ----------

    @property
    def scenario(self) -> str | None:
        return self.scenario_ids[0] if self.scenario_ids else None

    @scenario.setter
    def scenario(self, value: str | None) -> None:
        if value is None:
            self.scenario_ids = []
        else:
            self.scenario_ids = [value] + [s for s in self.scenario_ids if s != value]

    # ---------- lifecycle queries ----------

    def is_fixed(self) -> bool:
        return self.status == ConstraintStatus.FIXED or self.opt_status == OptimizationStatus.FIXED

    def is_rejected(self) -> bool:
        return self.status in (ConstraintStatus.REJECTED, ConstraintStatus.DEPRECATED)

    def is_emittable(self, mode: str = "balanced") -> bool:
        return self.is_safe_to_emit(mode)

    def is_safe_to_emit(self, mode: str = "balanced") -> bool:
        if self.disabled:
            return False
        if self.status in _NON_EMITTABLE_STATUSES:
            return False
        if self.is_fixed():
            return True
        if mode == "strict":
            return (self.confidence == Confidence.HIGH
                    and self.status in (ConstraintStatus.FIXED, ConstraintStatus.CONFIRMED))
        if mode == "balanced":
            if self.status == ConstraintStatus.REQUIRES_CONFIRMATION:
                return self.confidence == Confidence.HIGH
            return True
        return True

    # ---------- mutation ----------

    def add_value(self, key: str, val: Any) -> None:
        if self.is_fixed():
            raise ValueError(f"Cannot modify fixed constraint {self.id}")
        self.values[key] = val

    def add_dependency(self, cid: str) -> None:
        if cid == self.id:
            return
        if cid not in self.dependency_ids:
            self.dependency_ids.append(cid)

    def add_downstream(self, cid: str) -> None:
        if cid == self.id:
            return
        if cid not in self.downstream_ids:
            self.downstream_ids.append(cid)

    def add_assumption(self, aid: str) -> None:
        if aid not in self.assumption_ids:
            self.assumption_ids.append(aid)

    def add_scenario(self, sid: str) -> None:
        if sid not in self.scenario_ids:
            self.scenario_ids.append(sid)

    def mark_rejected(self, reason: str = "") -> None:
        if self.is_fixed():
            raise ValueError(f"Cannot reject fixed constraint {self.id}")
        self.status = ConstraintStatus.REJECTED
        if reason:
            self.comment = (self.comment + "; " if self.comment else "") + f"REJECTED: {reason}"

    def confirm(self, confidence: Confidence | None = None) -> None:
        self.status = ConstraintStatus.CONFIRMED
        if confidence is not None:
            self.confidence = confidence

    def fix(self) -> None:
        self.status = ConstraintStatus.FIXED
        self.opt_status = OptimizationStatus.FIXED

    # ---------- semantic identity ----------

    def _canonical_values(self) -> tuple:
        from ..utils.units import parse_time_string
        out: list[tuple[str, Any]] = []
        for k in sorted(self.values.keys()):
            v = self.values[k]
            if isinstance(v, str) and k.endswith(("_seconds", "_delay", "_period", "_latency", "_uncertainty")):
                try:
                    v = parse_time_string(v)
                except Exception:
                    pass
            if isinstance(v, float):
                v = round(v, 15)
            if isinstance(v, list):
                v = tuple(_canon(vx) for vx in v)
            if isinstance(v, dict):
                v = tuple(sorted((str(kk), _canon(vv)) for kk, vv in v.items()))
            out.append((k, v))
        return tuple(out)

    def semantic_key(self) -> tuple:
        return (
            self.type.value,
            tuple(sorted(self.target_objects)),
            tuple(sorted(self.source_objects)),
            tuple(sorted(self.through_objects)),
            tuple(sorted(self.clock_refs)),
            tuple(sorted(self.scenario_ids)),
            self._canonical_values(),
            self.path_selector.semantic_key() if self.path_selector else (),
            self.disabled,
        )

    def semantically_equivalent(self, other: "Constraint") -> bool:
        return self.semantic_key() == other.semantic_key()

    # ---------- cloning / immutability ----------

    def clone(self, *, new_id: str | None = None,
              overrides: dict[str, Any] | None = None) -> "Constraint":
        data = self.to_canonical_dict()
        data = copy.deepcopy(data)
        data["id"] = new_id or f"{self.id}_cand"
        if overrides:
            data.update(overrides)
        return Constraint.from_canonical_dict(data)

    # ---------- invariant validation ----------

    def validate_invariants(self) -> list[str]:
        problems: list[str] = []
        if not self.id:
            problems.append("missing id")
        if self.status not in set(ConstraintStatus):
            problems.append(f"invalid status {self.status!r}")
        if self.confidence not in set(Confidence):
            problems.append(f"invalid confidence {self.confidence!r}")
        if self.opt_status not in set(OptimizationStatus):
            problems.append(f"invalid opt_status {self.opt_status!r}")
        if self.status == ConstraintStatus.FIXED and self.opt_status == OptimizationStatus.TUNABLE:
            problems.append("FIXED constraint cannot be TUNABLE")
        if self.status == ConstraintStatus.REJECTED and self.opt_status == OptimizationStatus.FIXED:
            problems.append("REJECTED constraint cannot be FIXED")
        if self.is_rejected() and self.is_safe_to_emit("exploratory"):
            problems.append("rejected/deprecated constraint must not be emittable")
        if self.provenance is None:
            problems.append("missing provenance")
        else:
            if not isinstance(self.provenance.source_kind, SourceKind):
                problems.append("provenance.source_kind not normalized")
        for s in self.scenario_ids:
            if not isinstance(s, str) or not s:
                problems.append(f"invalid scenario id {s!r}")
        if self.path_selector is not None:
            if self.path_selector.min_max not in ("min", "max", "both"):
                problems.append(f"invalid path_selector.min_max {self.path_selector.min_max!r}")
            if self.path_selector.setup_hold not in ("setup", "hold", "both"):
                problems.append(f"invalid path_selector.setup_hold {self.path_selector.setup_hold!r}")
        return problems

    # ---------- CANONICAL serialization (lossless) ----------

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "target_objects": sorted(self.target_objects),
            "source_objects": sorted(self.source_objects),
            "through_objects": sorted(self.through_objects),
            "clock_refs": sorted(self.clock_refs),
            "target_refs": sorted([t.to_dict() for t in self.target_refs],
                                  key=lambda d: (d["collection_kind"], d.get("pattern") or "")),
            "source_refs": sorted([t.to_dict() for t in self.source_refs],
                                  key=lambda d: (d["collection_kind"], d.get("pattern") or "")),
            "through_refs": [sorted([t.to_dict() for t in stage],
                                    key=lambda d: (d["collection_kind"], d.get("pattern") or ""))
                             for stage in self.through_refs],
            "clock_refs_typed": sorted([t.to_dict() for t in self.clock_refs_typed],
                                       key=lambda d: (d["collection_kind"], d.get("pattern") or "")),
            "values": _stable_values(self.values),
            "source_kind": self.source_kind.value,
            "provenance": self.provenance.to_dict() if self.provenance else None,
            "confidence": self.confidence.value,
            "status": self.status.value,
            "opt_status": self.opt_status.value,
            "generation_confidence": self.generation_confidence.value,
            "evidence_ids": sorted(self.evidence_ids),
            "assumption_ids": sorted(self.assumption_ids),
            "dependency_ids": sorted(self.dependency_ids),
            "downstream_ids": sorted(self.downstream_ids),
            "affected_paths": sorted(self.affected_paths),
            "dependent_analyses": sorted(self.dependent_analyses),
            "scenario_ids": sorted(self.scenario_ids),
            "precedence": self.precedence,
            "disabled": self.disabled,
            "comment": self.comment,
            "path_selector": self.path_selector.to_dict() if self.path_selector else None,
        }

    @classmethod
    def from_canonical_dict(cls, d: dict[str, Any],
                            unknown_field_policy: str = "keep") -> "Constraint":
        """Rebuild from canonical dict.

        unknown_field_policy:
            "keep" — preserve unknown keys in ``values`` extension
                     (transparent pass-through for future versions).
            "error" — raise ValueError on unknown top-level keys.
        """
        known = {
            "id", "type", "target_objects", "source_objects", "through_objects",
            "clock_refs", "target_refs", "source_refs", "through_refs",
            "clock_refs_typed", "values", "source_kind", "provenance", "confidence",
            "status", "opt_status", "generation_confidence", "evidence_ids",
            "assumption_ids", "dependency_ids", "downstream_ids",
            "affected_paths", "dependent_analyses", "scenario_ids",
            "precedence", "disabled", "comment", "path_selector",
            # Unknown-extensions area:
            "_extensions",
        }
        extras = set(d.keys()) - known
        if unknown_field_policy == "error" and extras:
            raise ValueError(f"unknown top-level fields in canonical constraint: {sorted(extras)}")
        extensions = dict(d.get("_extensions", {}))
        for k in extras:
            extensions[k] = copy.deepcopy(d[k])
        return cls(
            id=d["id"],
            type=ConstraintType(d["type"]),
            target_objects=list(d.get("target_objects", [])),
            source_objects=list(d.get("source_objects", [])),
            through_objects=list(d.get("through_objects", [])),
            clock_refs=list(d.get("clock_refs", [])),
            values=copy.deepcopy(d.get("values", {})),
            source_kind=SourceKind(d["source_kind"]),
            provenance=ProvenanceRecord.from_dict(d["provenance"]) if d.get("provenance") else None,
            confidence=Confidence(d.get("confidence", "MEDIUM")),
            status=ConstraintStatus(d.get("status", "PROPOSED")),
            opt_status=OptimizationStatus(d.get("opt_status", "UNKNOWN")),
            generation_confidence=GenerationConfidence(
                d.get("generation_confidence", "INFERRED_MEDIUM_CONFIDENCE")),
            evidence_ids=list(d.get("evidence_ids", [])),
            assumption_ids=list(d.get("assumption_ids", [])),
            dependency_ids=list(d.get("dependency_ids", [])),
            downstream_ids=list(d.get("downstream_ids", [])),
            affected_paths=list(d.get("affected_paths", [])),
            dependent_analyses=list(d.get("dependent_analyses", [])),
            scenario_ids=list(d.get("scenario_ids", [])),
            precedence=int(d.get("precedence", 0)),
            disabled=bool(d.get("disabled", False)),
            comment=d.get("comment"),
            path_selector=PathSelector.from_dict(d["path_selector"]) if d.get("path_selector") else None,
            target_refs=[TargetRef.from_dict(x) for x in d.get("target_refs", [])],
            source_refs=[TargetRef.from_dict(x) for x in d.get("source_refs", [])],
            through_refs=[[TargetRef.from_dict(x) for x in stage] for stage in d.get("through_refs", [])],
            clock_refs_typed=[TargetRef.from_dict(x) for x in d.get("clock_refs_typed", [])],
        )

    # ---------- rendering / PRESENTATION summary ----------

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "targets": sorted(self.target_objects),
            "sources": sorted(self.source_objects),
            "clocks": sorted(self.clock_refs),
            "values": _stable_values(self.values),
            "source": self.source_kind.value,
            "confidence": self.confidence.value,
            "status": self.status.value,
            "opt_status": self.opt_status.value,
            "scenarios": list(self.scenario_ids),
            "assumptions": list(self.assumption_ids),
            "dependencies": list(self.dependency_ids),
            "comment": self.comment,
        }


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


def _canon(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 15)
    if isinstance(v, (list, tuple)):
        return tuple(_canon(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((str(k), _canon(vv)) for k, vv in v.items()))
    return v


def _default_target_kind(ctype: ConstraintType) -> CollectionKind:
    return {
        ConstraintType.SET_INPUT_DELAY: CollectionKind.PORT,
        ConstraintType.SET_OUTPUT_DELAY: CollectionKind.PORT,
        ConstraintType.SET_INPUT_TRANSITION: CollectionKind.PORT,
        ConstraintType.SET_LOAD: CollectionKind.PORT,
        ConstraintType.SET_MAX_TRANSITION: CollectionKind.PORT,
        ConstraintType.SET_MAX_CAPACITANCE: CollectionKind.PORT,
        ConstraintType.SET_MAX_FANOUT: CollectionKind.PORT,
        ConstraintType.SET_DRIVING_CELL: CollectionKind.PORT,
        ConstraintType.CREATE_CLOCK: CollectionKind.PORT,
        ConstraintType.CREATE_GENERATED_CLOCK: CollectionKind.PIN,
        ConstraintType.SET_CLOCK_UNCERTAINTY: CollectionKind.CLOCK,
        ConstraintType.SET_CLOCK_LATENCY: CollectionKind.CLOCK,
        ConstraintType.SET_CLOCK_TRANSITION: CollectionKind.CLOCK,
        ConstraintType.SET_PROPAGATED_CLOCK: CollectionKind.CLOCK,
        ConstraintType.SET_CLOCK_GROUPS: CollectionKind.CLOCK,
        ConstraintType.SET_FALSE_PATH: CollectionKind.CLOCK,
        ConstraintType.SET_MULTICYCLE_PATH: CollectionKind.CLOCK,
        ConstraintType.SET_MIN_DELAY: CollectionKind.PORT,
        ConstraintType.SET_MAX_DELAY: CollectionKind.PORT,
    }.get(ctype, CollectionKind.LITERAL)


def _default_source_kind(ctype: ConstraintType) -> CollectionKind:
    # -source on create_generated_clock is a pin; other "source" lists
    # default to CLOCK (e.g. set_clock_uncertainty -from <clock>).
    if ctype == ConstraintType.CREATE_GENERATED_CLOCK:
        return CollectionKind.PIN
    return CollectionKind.CLOCK


def _stable_values(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in sorted(d.keys()):
        v = d[k]
        if isinstance(v, dict):
            out[k] = _stable_values(v)
        elif isinstance(v, list):
            if all(isinstance(x, str) for x in v):
                out[k] = sorted(v, key=str)
            else:
                out[k] = list(v)
        else:
            out[k] = v
    return out
