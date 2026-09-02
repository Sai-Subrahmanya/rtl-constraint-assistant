"""
Provenance tracking for constraints and decisions (Manual §9, §118).

Each Constraint carries exactly one ProvenanceRecord.  The record
answers the question "where did this constraint come from and what does
it depend on?":

* ``created_by``     — agent identifier (rca, user, sdc_parser, ...)
* ``created_at``     — ISO-8601 UTC timestamp
* ``source_kind``    — RTL / USER / EXISTING_SDC / LIBRARY / PHYSICAL_DATA / TOOL / INFERENCE / DERIVED
* ``rule_id``        — optional inference-rule id (CLK-001, ...)
* ``evidence[]``     — Evidence records (frozen)
* ``assumption_ids[]`` — Assumption ids this constraint depends on
* ``dependency_ids[]`` — ids of upstream Constraints this depends on
* ``downstream_ids[]`` — reverse edges populated by ConstraintSet
* ``affected_paths[]``  — object groups/path ids affected
* ``scenario_ids[]``    — scenarios this constraint applies to
* ``import_meta``    — ImportMetadata for imported constraints
* ``explanation``    — human-readable rationale
* ``confidence``     — overall provenance confidence (enum-normalized)

ProvenanceRecords are mutable (downstream edges accumulate during pipeline
execution), but Evidence objects are frozen.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..utils.enums import Confidence, SourceKind
from .evidence import Evidence, _normalize_confidence, _sort_dict, copy_literal


class ImportMetadata(BaseModel):
    """Metadata retained when a constraint was imported from an external
    source (SDC file, liberty, UPFF, ...)."""

    model_config = ConfigDict(frozen=True)

    source_file: str
    source_line: int | None = None
    original_command: str | None = None       # raw SDC text
    source_format: str = "sdc"                # sdc / upf / liberty / ...
    import_run_id: str | None = None
    import_timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    extra: dict[str, Any] = Field(default_factory=dict)

    # -- serialization --

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "source_line": self.source_line,
            "original_command": self.original_command,
            "source_format": self.source_format,
            "import_run_id": self.import_run_id,
            "import_timestamp": self.import_timestamp,
            "extra": _sort_dict(self.extra),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ImportMetadata":
        return cls(
            source_file=d["source_file"],
            source_line=d.get("source_line"),
            original_command=d.get("original_command"),
            source_format=d.get("source_format", "sdc"),
            import_run_id=d.get("import_run_id"),
            import_timestamp=d.get("import_timestamp"),
            extra=copy_literal(d.get("extra", {})),
        )

    def summary(self) -> dict[str, Any]:
        """Compact presentation view."""
        return {
            "source_file": self.source_file,
            "source_line": self.source_line,
            "source_format": self.source_format,
            "original_command": self.original_command,
        }


def _normalize_source_kind(v: Any) -> SourceKind:
    if isinstance(v, SourceKind):
        return v
    if v is None:
        return SourceKind.INFERENCE
    s = str(v).upper()
    try:
        return SourceKind(s)
    except ValueError:
        # Compatibility: tolerate "IMPORT" used by some callers.
        if s == "IMPORT":
            return SourceKind.EXISTING_SDC
        return SourceKind.INFERENCE


class ProvenanceRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    created_by: str = "rca"
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    source_kind: SourceKind = SourceKind.INFERENCE
    rule_id: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    assumption_ids: list[str] = Field(default_factory=list)
    dependency_ids: list[str] = Field(default_factory=list)
    downstream_ids: list[str] = Field(default_factory=list)
    affected_paths: list[str] = Field(default_factory=list)
    scenario_ids: list[str] = Field(default_factory=list)
    import_meta: ImportMetadata | None = None
    explanation: str = ""
    confidence: Confidence = Confidence.MEDIUM

    # Allow construction with strings for source_kind / confidence.
    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        d = dict(data)
        d["source_kind"] = _normalize_source_kind(d.get("source_kind", SourceKind.INFERENCE))
        d["confidence"] = _normalize_confidence(d.get("confidence", Confidence.MEDIUM))
        # Evidence items: if they're plain dicts, normalize their confidence.
        evs = d.get("evidence")
        if evs:
            norm_evs = []
            for ev in evs:
                if isinstance(ev, Evidence):
                    norm_evs.append(ev)
                elif isinstance(ev, dict):
                    ev = dict(ev)
                    ev["confidence"] = _normalize_confidence(ev.get("confidence", "MEDIUM"))
                    norm_evs.append(Evidence(**ev))
                else:
                    norm_evs.append(ev)
            d["evidence"] = norm_evs
        # Import meta
        im = d.get("import_meta")
        if im is not None and not isinstance(im, ImportMetadata) and isinstance(im, dict):
            d["import_meta"] = ImportMetadata.from_dict(im)
        return d

    # Append-only semantics --------------------------------------------------

    def add_evidence(self, ev: Evidence) -> None:
        for existing in self.evidence:
            if existing.semantically_equivalent(ev):
                return
        self.evidence.append(ev)

    def add_assumption(self, aid: str) -> None:
        if aid not in self.assumption_ids:
            self.assumption_ids.append(aid)

    def add_dependency(self, cid: str) -> None:
        if cid not in self.dependency_ids:
            self.dependency_ids.append(cid)

    def add_downstream(self, cid: str) -> None:
        if cid not in self.downstream_ids:
            self.downstream_ids.append(cid)

    def add_scenario(self, sid: str) -> None:
        if sid not in self.scenario_ids:
            self.scenario_ids.append(sid)

    def set_import(self, meta: ImportMetadata) -> None:
        self.import_meta = meta
        if meta.original_command:
            self.explanation = (
                f"Imported from {meta.source_file}"
                + (f":{meta.source_line}" if meta.source_line else "")
            )

    # Semantic helpers -------------------------------------------------------

    def evidence_kinds(self) -> list[str]:
        return sorted({e.kind for e in self.evidence})

    # --- CANONICAL serialization (lossless) ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_by": self.created_by,
            "created_at": self.created_at,
            "source_kind": self.source_kind.value,
            "rule_id": self.rule_id,
            "evidence": [e.to_dict() for e in sorted(self.evidence, key=lambda x: x.id)],
            "assumption_ids": sorted(self.assumption_ids),
            "dependency_ids": sorted(self.dependency_ids),
            "downstream_ids": sorted(self.downstream_ids),
            "affected_paths": sorted(self.affected_paths),
            "scenario_ids": sorted(self.scenario_ids),
            "import_meta": self.import_meta.to_dict() if self.import_meta else None,
            "explanation": self.explanation,
            "confidence": self.confidence.value,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProvenanceRecord":
        evs = [Evidence.from_dict(ed) for ed in d.get("evidence", [])]
        im = d.get("import_meta")
        return cls(
            created_by=d.get("created_by", "rca"),
            created_at=d.get("created_at"),
            source_kind=_normalize_source_kind(d.get("source_kind", SourceKind.INFERENCE)),
            rule_id=d.get("rule_id"),
            evidence=evs,
            assumption_ids=list(d.get("assumption_ids", [])),
            dependency_ids=list(d.get("dependency_ids", [])),
            downstream_ids=list(d.get("downstream_ids", [])),
            affected_paths=list(d.get("affected_paths", [])),
            scenario_ids=list(d.get("scenario_ids", [])),
            import_meta=ImportMetadata.from_dict(im) if im else None,
            explanation=d.get("explanation", ""),
            confidence=_normalize_confidence(d.get("confidence", "MEDIUM")),
        )

    def summary(self) -> dict[str, Any]:
        """Compact presentation view (counts, not full evidence)."""
        return {
            "created_by": self.created_by,
            "created_at": self.created_at,
            "source_kind": self.source_kind.value,
            "rule_id": self.rule_id,
            "evidence_count": len(self.evidence),
            "evidence_kinds": self.evidence_kinds(),
            "assumption_ids": list(self.assumption_ids),
            "dependency_ids": list(self.dependency_ids),
            "scenario_ids": list(self.scenario_ids),
            "import_meta": self.import_meta.summary() if self.import_meta else None,
            "explanation": self.explanation,
            "confidence": self.confidence.value,
        }


def make_provenance(
    source_kind: SourceKind | str = SourceKind.INFERENCE,
    *,
    rule_id: str | None = None,
    explanation: str = "",
    confidence: Confidence | str = Confidence.MEDIUM,
    created_by: str = "rca",
) -> ProvenanceRecord:
    return ProvenanceRecord(
        source_kind=_normalize_source_kind(source_kind),
        rule_id=rule_id,
        explanation=explanation,
        confidence=_normalize_confidence(confidence),
        created_by=created_by,
    )
