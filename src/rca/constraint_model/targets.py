"""Shared target/collection representation used by both the SDC importer
(Step 5) and the SDC generator (Step 6).

A :class:`TargetRef` is a semantic reference to zero or more design
objects, carrying just enough information to decide how to render it
to SDC (which ``get_*`` command to use, whether it is a literal, etc.)
and how to resolve it against a Design.

This model intentionally does NOT guess the target kind from string
syntax (e.g. "/" -> pin). The collection kind is authoritative; the
hierarchical-string syntax is only used when *resolving* against a
design, never when *emitting*.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Iterable, TYPE_CHECKING

from ..utils.enums import CollectionKind, ResolutionStatus

if TYPE_CHECKING:
    from ..design_model import Design
    from ..timing_model import TimingGraph


# ---------------------------------------------------------------------------
# Target reference
# ---------------------------------------------------------------------------


@dataclass
class TargetRef:
    """A semantically typed reference to a set of objects.

    Attributes mirror the Step-5 TargetCollection shape closely so the
    importer can construct these directly; the generator consumes
    them to render correct [get_* ...] selectors.
    """
    collection_kind: CollectionKind
    pattern: str | None = None
    # Members when multiple explicit names (e.g. -group {a b c} or
    # -from [get_ports {a b}]). Either pattern OR members may be set,
    # but members wins for explicit multi-object groups.
    members: list[str] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    expression: str | None = None          # original text for provenance
    resolution_status: ResolutionStatus = ResolutionStatus.RESOLVED
    unresolved_reason: str | None = None

    # -- factories ----------------------------------------------------
    @classmethod
    def literal(cls, name: str) -> "TargetRef":
        """Plain unqualified name. The generator treats this as needing
        a default selector per constraint type (e.g. get_ports for
        input_delay targets), but when an explicit target_ref is
        attached we always trust its collection_kind."""
        return cls(collection_kind=CollectionKind.LITERAL, pattern=name,
                   members=[name] if name else [], resolution_status=ResolutionStatus.RESOLVED)

    @classmethod
    def port(cls, name: str) -> "TargetRef":
        return cls(collection_kind=CollectionKind.PORT, pattern=name, members=[name])

    @classmethod
    def pin(cls, name: str) -> "TargetRef":
        return cls(collection_kind=CollectionKind.PIN, pattern=name, members=[name])

    @classmethod
    def net(cls, name: str) -> "TargetRef":
        return cls(collection_kind=CollectionKind.NET, pattern=name, members=[name])

    @classmethod
    def cell(cls, name: str) -> "TargetRef":
        return cls(collection_kind=CollectionKind.CELL, pattern=name, members=[name])

    @classmethod
    def clock(cls, name: str) -> "TargetRef":
        return cls(collection_kind=CollectionKind.CLOCK, pattern=name, members=[name])

    @classmethod
    def register(cls, name: str) -> "TargetRef":
        return cls(collection_kind=CollectionKind.REGISTER, pattern=name, members=[name])

    @classmethod
    def all_inputs(cls) -> "TargetRef":
        return cls(collection_kind=CollectionKind.ALL_INPUTS)

    @classmethod
    def all_outputs(cls) -> "TargetRef":
        return cls(collection_kind=CollectionKind.ALL_OUTPUTS)

    @classmethod
    def all_clocks(cls) -> "TargetRef":
        return cls(collection_kind=CollectionKind.ALL_CLOCKS)

    @classmethod
    def all_registers(cls) -> "TargetRef":
        return cls(collection_kind=CollectionKind.ALL_REGISTERS)

    @classmethod
    def unresolved(cls, expression: str, reason: str) -> "TargetRef":
        return cls(collection_kind=CollectionKind.EXPR, expression=expression,
                   resolution_status=ResolutionStatus.UNRESOLVED, unresolved_reason=reason)

    # -- queries ------------------------------------------------------
    def names(self) -> list[str]:
        """Return explicit member names (for multi-object groups) or a
        single-element list containing the pattern."""
        if self.members:
            return list(self.members)
        if self.pattern:
            return [self.pattern]
        return []

    def is_wildcard(self) -> bool:
        p = self.pattern or ""
        return any(ch in p for ch in "*?[")

    def is_multi_object(self) -> bool:
        return len(self.members) > 1 or self.collection_kind in (
            CollectionKind.ALL_INPUTS, CollectionKind.ALL_OUTPUTS,
            CollectionKind.ALL_CLOCKS, CollectionKind.ALL_REGISTERS,
        )

    # -- serialization -----------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_kind": self.collection_kind.value,
            "pattern": self.pattern,
            "members": list(self.members),
            "filters": dict(self.filters),
            "expression": self.expression,
            "resolution_status": self.resolution_status.value,
            "unresolved_reason": self.unresolved_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TargetRef":
        return cls(
            collection_kind=CollectionKind(d["collection_kind"]),
            pattern=d.get("pattern"),
            members=list(d.get("members", [])),
            filters=dict(d.get("filters", {})),
            expression=d.get("expression"),
            resolution_status=ResolutionStatus(d.get("resolution_status", "RESOLVED")),
            unresolved_reason=d.get("unresolved_reason"),
        )

    def semantic_key(self) -> tuple:
        return (
            self.collection_kind.value,
            self.pattern or "",
            tuple(sorted(self.members)),
            tuple(sorted(self.filters.keys())),
            tuple(sorted((str(k), str(v)) for k, v in self.filters.items())),
            self.resolution_status.value,
            self.unresolved_reason or "",
        )


# ---------------------------------------------------------------------------
# Helpers: build TargetRef lists from plain name lists using a default
# kind. These are used when the UCM contains legacy plain-string target
# lists (e.g. from inference rules that predate target typing). The
# generator can still emit a reasonable selector for these, explicitly
# marking them as "default-kind" (PORT for I/O targets, CLOCK for clock
# lists, PIN for path selectors) at the callsite.
# ---------------------------------------------------------------------------


def targets_from_strings(names: Iterable[str], default_kind: CollectionKind) -> list[TargetRef]:
    out: list[TargetRef] = []
    for n in names:
        if not n:
            continue
        if default_kind == CollectionKind.CLOCK:
            out.append(TargetRef.clock(n))
        elif default_kind == CollectionKind.PORT:
            out.append(TargetRef.port(n))
        elif default_kind == CollectionKind.PIN:
            out.append(TargetRef.pin(n))
        elif default_kind == CollectionKind.NET:
            out.append(TargetRef.net(n))
        elif default_kind == CollectionKind.CELL:
            out.append(TargetRef.cell(n))
        elif default_kind == CollectionKind.REGISTER:
            out.append(TargetRef.register(n))
        else:
            out.append(TargetRef.literal(n))
    return out
