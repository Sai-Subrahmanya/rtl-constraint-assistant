"""
Evidence representation for inferences and decisions (Manual §9, §118, §135).

Evidence is an *immutable* observation supporting (or opposing) a
constraint/assumption. It records:

* the kind of observation (structural / user / heuristic / formal / ...)
* a human-readable description
* an optional machine-readable ``detail`` dict for programmatic reasoning
* the design object(s) the evidence was derived from
* an optional source location (file:line)
* the confidence contributed by this piece of evidence
* an optional rule/tool identifier
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..utils.enums import Confidence

# Canonical evidence kinds.  Using a literal set here (instead of an
# Enum) so downstream code can add kinds without breaking imports;
# the constant below documents the expected vocabulary.
EVIDENCE_KINDS = {
    "structural",        # directly observed in RTL structure (edges, regs)
    "user",              # explicit user declaration
    "import",            # imported from an existing SDC / constraint file
    "library",           # derived from library data
    "physical",          # derived from physical data (SPEF / LEF / DEF)
    "formal",            # produced by formal verification
    "simulation",        # produced by simulation
    "heuristic",         # best-effort heuristic (LOW confidence only)
    "naming_hint",       # name-based hint (corroborating, never standalone)
    "rule",              # output of a named inference rule
    "tool",              # tool-generated (e.g. STA output)
    "assumption",        # evidence is an assumption, not a fact
}


def _normalize_confidence(c: Any) -> Confidence:
    if isinstance(c, Confidence):
        return c
    if c is None:
        return Confidence.MEDIUM
    s = str(c).upper()
    try:
        return Confidence(s)
    except ValueError:
        return Confidence.MEDIUM


class Evidence(BaseModel):
    """A single piece of evidence supporting (or opposing) a decision.

    The model is **immutable after creation** (``frozen=True``) so that
    Evidence objects can be safely shared across Constraint copies
    without accidental aliasing mutations.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    kind: str
    description: str
    detail: dict[str, Any] = Field(default_factory=dict)
    source_objects: list[str] = Field(default_factory=list)
    location: str | None = None                 # file:line, e.g. "foo.sv:42"
    confidence: Confidence = Confidence.MEDIUM
    rule_id: str | None = None                  # e.g. "CLK-001"
    created_by: str = "rca"
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    # Allow construction with confidence as a string.
    @classmethod
    def create(cls, **data: Any) -> "Evidence":
        data["confidence"] = _normalize_confidence(data.get("confidence", Confidence.MEDIUM))
        return cls(**data)

    def model_post_init(self, __context: Any) -> None:
        # Normalize confidence if it snuck in as a string via the generic ctor.
        if not isinstance(self.confidence, Confidence):
            # Use object.__setattr__ because the model is frozen; pydantic
            # allows mutations during post_init.
            object.__setattr__(self, "confidence", _normalize_confidence(self.confidence))

    # Semantic identity helpers -------------------------------------------------

    def semantic_key(self) -> tuple:
        """Stable semantic identity independent of id/timestamp formatting."""
        return (
            self.kind,
            self.description.strip(),
            tuple(sorted(self.source_objects)),
            self.rule_id or "",
            tuple(sorted((str(k), _stable_repr(v)) for k, v in self.detail.items())),
            self.confidence.value,
        )

    def semantically_equivalent(self, other: "Evidence") -> bool:
        return self.semantic_key() == other.semantic_key()

    # --- serialization ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "description": self.description,
            "detail": _sort_dict(self.detail),
            "source_objects": sorted(self.source_objects),
            "location": self.location,
            "confidence": self.confidence.value,
            "rule_id": self.rule_id,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Evidence":
        return cls(
            id=d["id"],
            kind=d["kind"],
            description=d["description"],
            detail=copy_literal(d.get("detail", {})),
            source_objects=list(d.get("source_objects", [])),
            location=d.get("location"),
            confidence=_normalize_confidence(d.get("confidence", "MEDIUM")),
            rule_id=d.get("rule_id"),
            created_by=d.get("created_by", "rca"),
            created_at=d.get("created_at"),
        )

    def summary(self) -> dict[str, Any]:
        """Compact presentation view (deliberately omits some detail)."""
        return {
            "id": self.id,
            "kind": self.kind,
            "description": self.description,
            "source_objects": sorted(self.source_objects),
            "location": self.location,
            "confidence": self.confidence.value,
            "rule_id": self.rule_id,
            "created_by": self.created_by,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _stable_repr(v: Any) -> str:
    """Deterministic string form for semantic-key comparisons."""
    if isinstance(v, float):
        return f"{v:.12g}"
    if isinstance(v, bool):  # before int
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (list, tuple, set, frozenset)):
        return "[" + ",".join(_stable_repr(x) for x in sorted(v, key=lambda z: str(z))) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}={_stable_repr(v[k])}" for k in sorted(v)) + "}"
    if v is None:
        return "null"
    return str(v)


def _sort_dict(d: Any) -> Any:
    """Recursively sort dictionary keys and sort homogeneous string lists."""
    if isinstance(d, dict):
        return {k: _sort_dict(d[k]) for k in sorted(d.keys())}
    if isinstance(d, list):
        # Don't sort ordered structures unless we know they're sets; keep
        # order but recursively sort children.
        return [_sort_dict(x) for x in d]
    return d


def copy_literal(v: Any) -> Any:
    """Deep-copy a JSON-literal structure (dict/list/scalar)."""
    if isinstance(v, dict):
        return {k: copy_literal(x) for k, x in v.items()}
    if isinstance(v, list):
        return [copy_literal(x) for x in v]
    return v
