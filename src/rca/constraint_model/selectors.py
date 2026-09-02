"""
Path selectors for exception/constraint targets (Manual §74, §75).

A ``PathSelector`` represents the ``-from``/``-through``/``-to``
triple plus qualifiers (``-rise``/``-fall``, ``-clock``, ``-min``/``-max``,
``-setup``/``-hold``). It is structured so that the UCM can distinguish:

* startpoint selectors  (from_set)
* endpoint selectors    (to_set)
* through selectors     (through_set, ordered)
* edge qualifiers       (rise / fall / both)
* clock filters         (from_clock / to_clock)
* analysis mode         (setup / hold / min / max)
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .targets import CollectionKind, TargetRef, targets_from_strings

EdgeQualifier = Literal["rise", "fall", "both"]
MinMax = Literal["min", "max", "both"]
SetupHold = Literal["setup", "hold", "both"]


class PathSelector(BaseModel):
    """Normalised representation of -from/-through/-to object sets.

    String sets (``from_set``/``through_set``/``to_set``) hold raw names
    for backward compatibility. Typed ``*_refs`` hold semantic
    :class:`TargetRef` s; when populated the renderer uses them and
    avoids any inference from name syntax.
    """

    from_set: list[str] = Field(default_factory=list)
    through_set: list[list[str]] = Field(default_factory=list)
    to_set: list[str] = Field(default_factory=list)

    # Typed semantic references.
    from_refs: list[TargetRef] = Field(default_factory=list)
    through_refs: list[list[TargetRef]] = Field(default_factory=list)
    to_refs: list[TargetRef] = Field(default_factory=list)

    # Edge / mode qualifiers
    edge: EdgeQualifier | None = None
    min_max: MinMax = "both"
    setup_hold: SetupHold = "both"
    add_delay: bool = False
    reset_path: bool = False

    # Clock filters
    from_clock: list[str] = Field(default_factory=list)
    to_clock: list[str] = Field(default_factory=list)
    through_clock: list[str] = Field(default_factory=list)

    scenario: str | None = None

    # ---------------- queries ----------------

    def is_empty(self) -> bool:
        return not (self.from_set or self.to_set or any(self.through_set)
                    or self.from_clock or self.to_clock)

    def object_count(self) -> int:
        return (len(self.from_set) + len(self.to_set)
                + sum(len(t) for t in self.through_set)
                + len(self.from_clock) + len(self.to_clock))

    def all_objects(self) -> list[str]:
        out: list[str] = []
        out.extend(self.from_set)
        out.extend(self.to_set)
        for stage in self.through_set:
            out.extend(stage)
        out.extend(self.from_clock)
        out.extend(self.to_clock)
        return sorted(set(out))

    # ---------------- semantic identity ----------------

    def semantic_key(self) -> tuple:
        def _ref_key(r: TargetRef) -> tuple:
            return (r.collection_kind.value, tuple(sorted(r.names())))
        return (
            tuple(sorted(self.from_set)),
            tuple(tuple(sorted(stage)) for stage in self.through_set),
            tuple(sorted(self.to_set)),
            tuple(sorted((_ref_key(r) for r in self.from_refs), key=lambda t: t[1])),
            tuple(tuple(sorted((_ref_key(r) for r in stage), key=lambda t: t[1]))
                  for stage in self.through_refs),
            tuple(sorted((_ref_key(r) for r in self.to_refs), key=lambda t: t[1])),
            self.edge or "",
            self.min_max,
            self.setup_hold,
            self.add_delay,
            self.reset_path,
            tuple(sorted(self.from_clock)),
            tuple(sorted(self.to_clock)),
            tuple(sorted(self.through_clock)),
            self.scenario or "",
        )

    def semantically_equivalent(self, other: "PathSelector") -> bool:
        return self.semantic_key() == other.semantic_key()

    # ---------------- canonical serialization ----------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_set": sorted(self.from_set),
            "through_set": [sorted(stage) for stage in self.through_set],
            "to_set": sorted(self.to_set),
            "from_refs": [r.to_dict() for r in self.from_refs],
            "through_refs": [[r.to_dict() for r in stage] for stage in self.through_refs],
            "to_refs": [r.to_dict() for r in self.to_refs],
            "edge": self.edge,
            "min_max": self.min_max,
            "setup_hold": self.setup_hold,
            "add_delay": self.add_delay,
            "reset_path": self.reset_path,
            "from_clock": sorted(self.from_clock),
            "to_clock": sorted(self.to_clock),
            "through_clock": sorted(self.through_clock),
            "scenario": self.scenario,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PathSelector":
        return cls(
            from_set=list(d.get("from_set", [])),
            through_set=[list(stage) for stage in d.get("through_set", [])],
            to_set=list(d.get("to_set", [])),
            from_refs=[TargetRef.from_dict(r) for r in d.get("from_refs", [])],
            through_refs=[[TargetRef.from_dict(r) for r in stage]
                          for stage in d.get("through_refs", [])],
            to_refs=[TargetRef.from_dict(r) for r in d.get("to_refs", [])],
            edge=d.get("edge"),
            min_max=d.get("min_max", "both"),
            setup_hold=d.get("setup_hold", "both"),
            add_delay=bool(d.get("add_delay", False)),
            reset_path=bool(d.get("reset_path", False)),
            from_clock=list(d.get("from_clock", [])),
            to_clock=list(d.get("to_clock", [])),
            through_clock=list(d.get("through_clock", [])),
            scenario=d.get("scenario"),
        )

    # ---------------- presentation ----------------

    def summary(self) -> dict[str, Any]:
        """Compact presentation view (subset of to_dict)."""
        return self.to_dict()
