"""Helper: attach typed :class:`TargetRef` to UCM constraints built
from parsed SDC, so the Step 6 renderer never has to infer collection
kind from name syntax.
"""

from __future__ import annotations

from typing import Iterable

from ..constraint_model import Constraint, PathSelector
from ..constraint_model.targets import (
    TargetRef, CollectionKind, port, pin, net, cell, clock, register,
    literal, all_inputs, all_outputs, all_clocks, all_registers,
    unresolved,
)
from .collections import TargetCollection


def tc_to_ref(tc: TargetCollection) -> TargetRef:
    """Convert a parsed :class:`TargetCollection` to a semantic
    :class:`TargetRef`.  Never guesses: the collection_kind from the
    parsed ``[get_*]``/``[all_*]``/literal form is authoritative.
    """
    k = tc.collection_kind
    names: list[str] = []
    if tc.resolved_objects:
        names = list(tc.resolved_objects)
    elif tc.arguments:
        names = list(tc.arguments)
    elif tc.pattern:
        names = [tc.pattern]

    if k == CollectionKind.ALL_INPUTS:
        return all_inputs()
    if k == CollectionKind.ALL_OUTPUTS:
        return all_outputs()
    if k == CollectionKind.ALL_CLOCKS:
        return all_clocks()
    if k == CollectionKind.ALL_REGISTERS:
        return all_registers()
    if k == CollectionKind.EXPR:
        return unresolved(tc.expression,
                          reason=tc.unresolved_reason or "unsupported expression")
    if not names:
        return unresolved(tc.expression or tc.raw or "",
                          reason="no names in collection")
    if k == CollectionKind.PORT:
        return TargetRef(collection_kind=k, pattern=tc.pattern, members=names)
    if k == CollectionKind.PIN:
        return TargetRef(collection_kind=k, pattern=tc.pattern, members=names)
    if k == CollectionKind.NET:
        return net(names[0]) if len(names) == 1 else TargetRef(
            collection_kind=k, pattern=tc.pattern, members=names)
    if k == CollectionKind.CELL:
        return cell(names[0]) if len(names) == 1 else TargetRef(
            collection_kind=k, pattern=tc.pattern, members=names)
    if k == CollectionKind.CLOCK:
        return clock(names[0]) if len(names) == 1 else TargetRef(
            collection_kind=k, pattern=tc.pattern, members=names)
    if k == CollectionKind.REGISTER:
        return register(names[0]) if len(names) == 1 else TargetRef(
            collection_kind=k, pattern=tc.pattern, members=names)
    return TargetRef(collection_kind=CollectionKind.LITERAL,
                     pattern=tc.pattern, members=names)


def tcs_to_refs(tcs: Iterable[TargetCollection]) -> list[TargetRef]:
    return [tc_to_ref(tc) for tc in tcs]


def attach_typed_refs(constraint: Constraint,
                      target_tcs: list[TargetCollection] | None = None,
                      source_tcs: list[TargetCollection] | None = None,
                      clock_tcs: list[TargetCollection] | None = None,
                      through_tcs: list[list[TargetCollection]] | None = None,
                      ps_from_tcs: list[TargetCollection] | None = None,
                      ps_to_tcs: list[TargetCollection] | None = None,
                      ps_through_tcs: list[list[TargetCollection]] | None = None,
                      ps_from_clock_tcs: list[TargetCollection] | None = None,
                      ps_to_clock_tcs: list[TargetCollection] | None = None,
                      ) -> Constraint:
    """Attach typed target/source/clock/through refs + path-selector refs
    to a constraint when those collections were captured during import.
    Always appends; never overwrites existing entries set by the caller.
    """
    if target_tcs:
        for r in tcs_to_refs(target_tcs):
            if not _ref_in(r, constraint.target_refs):
                constraint.target_refs.append(r)
    if source_tcs:
        for r in tcs_to_refs(source_tcs):
            if not _ref_in(r, constraint.source_refs):
                constraint.source_refs.append(r)
    if clock_tcs:
        for r in tcs_to_refs(clock_tcs):
            if not _ref_in(r, constraint.clock_refs_typed):
                constraint.clock_refs_typed.append(r)
    if through_tcs:
        for stage in through_tcs:
            constraint.through_refs.append(tcs_to_refs(stage))
    if constraint.path_selector is not None:
        ps = constraint.path_selector
        if ps_from_tcs:
            for r in tcs_to_refs(ps_from_tcs):
                if not _ref_in(r, ps.from_refs):
                    ps.from_refs.append(r)
        if ps_to_tcs:
            for r in tcs_to_refs(ps_to_tcs):
                if not _ref_in(r, ps.to_refs):
                    ps.to_refs.append(r)
        if ps_through_tcs:
            for stage in ps_through_tcs:
                ps.through_refs.append(tcs_to_refs(stage))
    return constraint


def _ref_in(r: TargetRef, rs: list[TargetRef]) -> bool:
    return any(r.semantic_key() == x.semantic_key() for x in rs)
