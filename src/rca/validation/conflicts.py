"""Conflict and overlap/shadowing detection (Step 7 §10–§11).

Detects contradictory constraints (same object, incompatible values)
and classifies non-contradictory overlap as DUPLICATE / REDUNDANT /
SHADOWED / OVERLAPPING.  Nothing is deleted — findings are reported.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..constraint_model import Constraint, ConstraintSet
from ..constraint_model.targets import CollectionKind
from ..utils.enums import (
    ConstraintStatus, ConstraintType, ErrorCode, Severity, SourceKind,
    ValidationCategory,
)
from .base import ValidationIssue, ValidationReport


def validate_conflicts(cset: ConstraintSet, report: ValidationReport) -> None:
    report.checks_run.append("conflicts")
    report.checks_run.append("overlaps")

    conflicts: list[ValidationIssue] = []
    overlaps: list[ValidationIssue] = []

    # --- Duplicate/conflicting create_clock on same name ---
    by_clock_name: dict[str, list[Constraint]] = defaultdict(list)
    for c in cset.clocks():
        nm = c.values.get("name") or (c.target_objects[0] if c.target_objects else c.id)
        by_clock_name[nm].append(c)
    for nm, group in by_clock_name.items():
        periods = {float(c.values["period"]) for c in group if c.values.get("period") is not None}
        if len({c.id for c in group}) > 1:
            ids = [c.id for c in group]
            if len(periods) > 1:
                conflicts.append(ValidationIssue(
                    severity=Severity.ERROR, category=ValidationCategory.CONFLICT,
                    code=ErrorCode.CONFLICT_CLOCK_PERIOD,
                    message=f"Conflicting create_clock on '{nm}': different periods {sorted(periods)} (constraints {ids}).",
                    constraint_id=ids[0], related_constraint_ids=ids[1:],
                    object_names=[nm],
                    suggestion="Keep a single clock definition per clock name; use -add only for additional edges on the same clock.",
                    blocking=True))
            else:
                overlaps.append(ValidationIssue(
                    severity=Severity.LOW, category=ValidationCategory.OVERLAP,
                    code=ErrorCode.OVERLAP_DUPLICATE,
                    message=f"Duplicate create_clock on '{nm}' with identical period: {ids}.",
                    constraint_id=ids[0], related_constraint_ids=ids[1:],
                    object_names=[nm],
                    suggestion="Remove the redundant duplicate.",
                    blocking=False))

    # --- Conflicting IO delays ---
    io_keys: dict[tuple, list[Constraint]] = defaultdict(list)
    for c in cset.io_constraints():
        clk = c.values.get("clock") or (c.clock_refs[0] if c.clock_refs else None)
        mm = c.values.get("min_max", "max")
        edge = c.values.get("edge")
        add = bool(c.values.get("add_delay"))
        for tgt in c.target_objects or [None]:
            key = (c.type, tgt, clk, mm, edge, add)
            io_keys[key].append(c)
    for key, group in io_keys.items():
        if len(group) <= 1:
            continue
        delays = {float(c.values.get("delay")) for c in group
                  if c.values.get("delay") is not None}
        ids = [c.id for c in group]
        if len(delays) > 1:
            conflicts.append(ValidationIssue(
                severity=Severity.ERROR, category=ValidationCategory.CONFLICT,
                code=ErrorCode.CONFLICT_IO_DELAY,
                message=f"Conflicting {key[0].value} on '{key[1]}' (clock={key[2]}, min_max={key[3]}, edge={key[4]}): delays {sorted(delays)} (constraints {ids}).",
                constraint_id=ids[0], related_constraint_ids=ids[1:],
                object_names=[key[1]] if key[1] else [],
                suggestion="Align the delay values or disambiguate with -add_delay/-clock_fall.",
                blocking=True))
        else:
            overlaps.append(ValidationIssue(
                severity=Severity.LOW, category=ValidationCategory.OVERLAP,
                code=ErrorCode.OVERLAP_DUPLICATE,
                message=f"Duplicate {key[0].value} on '{key[1]}': {ids}.",
                constraint_id=ids[0], related_constraint_ids=ids[1:],
                object_names=[key[1]] if key[1] else [],
                suggestion="Remove the redundant duplicate.",
                blocking=False))

    # --- Conflicting set_clock_latency / set_clock_uncertainty on same clock ---
    for t, ec in ((ConstraintType.SET_CLOCK_LATENCY, ErrorCode.CONFLICT_LATENCY),
                  (ConstraintType.SET_CLOCK_UNCERTAINTY, ErrorCode.CONFLICT_UNCERTAINTY)):
        lat_keys: dict[tuple, list[Constraint]] = defaultdict(list)
        for c in cset.by_type(t):
            for tgt in c.target_objects or c.clock_refs or [None]:
                flags = tuple(sorted(k for k in ("source", "early", "late", "min",
                                                "max", "rise", "fall", "setup",
                                                "hold") if c.values.get(k) is True))
                key = (tgt, flags)
                lat_keys[key].append(c)
        for key, group in lat_keys.items():
            if len(group) <= 1:
                continue
            vfield = "latency" if t == ConstraintType.SET_CLOCK_LATENCY else "uncertainty"
            vals = {float(c.values.get(vfield)) for c in group
                    if c.values.get(vfield) is not None}
            ids = [c.id for c in group]
            if len(vals) > 1:
                conflicts.append(ValidationIssue(
                    severity=Severity.WARNING, category=ValidationCategory.CONFLICT,
                    code=ec,
                    message=f"Conflicting {t.value} on '{key[0]}' flags={key[1]}: values {sorted(vals)} (constraints {ids}).",
                    constraint_id=ids[0], related_constraint_ids=ids[1:],
                    object_names=[key[0]] if key[0] else [],
                    suggestion="Reconcile the conflicting values; only one applies per analysis corner.",
                    blocking=False))

    # --- Conflicting min/max delay on same endpoints ---
    mm_keys: dict[tuple, list[Constraint]] = defaultdict(list)
    for ct in (ConstraintType.SET_MIN_DELAY, ConstraintType.SET_MAX_DELAY):
        for c in cset.by_type(ct):
            ps = c.path_selector
            fs = tuple(sorted(ps.from_set)) if ps and ps.from_set else ()
            ts = tuple(sorted(ps.to_set)) if ps and ps.to_set else ()
            key = (ct, fs, ts)
            mm_keys[key].append(c)
    for key, group in mm_keys.items():
        if len(group) <= 1:
            continue
        vals = {float(c.values.get("delay")) for c in group
                if c.values.get("delay") is not None}
        ids = [c.id for c in group]
        if len(vals) > 1:
            conflicts.append(ValidationIssue(
                severity=Severity.WARNING, category=ValidationCategory.CONFLICT,
                code=ErrorCode.CONFLICT_MINMAX_DELAY,
                message=f"Conflicting {key[0].value} on from={list(key[1])} to={list(key[2])}: values {sorted(vals)} (constraints {ids}).",
                constraint_id=ids[0], related_constraint_ids=ids[1:],
                suggestion="Reconcile the delay override values.",
                blocking=False))

    # --- Precedence-aware user-vs-inference conflicts (Step 13 §4) ---
    _precedence_conflicts(cset, conflicts)

    # --- Path-exception overlap/shadow classification ---
    _classify_exception_overlaps(cset, overlaps)

    for i in conflicts:
        report.add(i)
    for i in overlaps:
        report.add(i)

    report.conflict_summary = {
        "conflict_count": len(conflicts),
        "overlap_count": len(overlaps),
        "conflict_codes": sorted({i.code.value for i in conflicts}),
        "overlap_codes": sorted({i.code.value for i in overlaps}),
    }


# ---------------------------------------------------------------------------
# Precedence-aware conflict reporting (Step 13 §4)
# ---------------------------------------------------------------------------

# Precedence order: higher rank wins.  Explicit fixed user intent is the
# strongest, weak heuristic the weakest.
_SOURCE_RANK: dict[SourceKind, int] = {
    SourceKind.USER: 6,
    SourceKind.EXISTING_SDC: 5,
    SourceKind.LIBRARY: 4,
    SourceKind.TOOL: 4,
    SourceKind.PHYSICAL_DATA: 4,
    SourceKind.RTL: 3,
    SourceKind.INFERENCE: 2,
    SourceKind.DERIVED: 1,
}


def _precedence_rank(c: Constraint) -> int:
    """Rank a constraint's authority for conflict resolution.

    Fixed/confirmed user intent outranks everything.  Otherwise fall
    back to source-kind precedence; unknown sources rank as weak
    heuristic (rank 1).
    """
    if c.status in (ConstraintStatus.FIXED,):
        return 100
    if c.status == ConstraintStatus.CONFIRMED and c.source_kind == SourceKind.USER:
        return 90
    return _SOURCE_RANK.get(c.source_kind, 1)


def _precedence_conflicts(cset: ConstraintSet,
                          conflicts: list[ValidationIssue]) -> None:
    """Report a CONFLICT_USER_VS_INFERENCE when a low-authority
    (inferred/derived) constraint conflicts with a fixed user constraint
    on the same clock/object.  We never silently drop the weaker one —
    we report it so a human can decide, and we record which wins."""
    # Group create_clock by name for precedence conflicts.
    by_name: dict[str, list[Constraint]] = defaultdict(list)
    for c in cset.clocks():
        nm = c.values.get("name") or (c.target_objects[0] if c.target_objects else c.id)
        by_name[nm].append(c)
    for nm, group in by_name.items():
        if len({c.id for c in group}) < 2:
            continue
        periods = {float(c.values["period"]) for c in group
                   if c.values.get("period") is not None}
        if len(periods) <= 1:
            continue
        ranked = sorted(group, key=_precedence_rank)
        winner, loser = ranked[-1], ranked[0]
        if _precedence_rank(winner) != _precedence_rank(loser):
            conflicts.append(ValidationIssue(
                severity=Severity.WARNING, category=ValidationCategory.CONFLICT,
                code=ErrorCode.CONFLICT_USER_VS_INFERENCE,
                message=(f"Clock '{nm}' has conflicting periods: fixed/user "
                         f"constraint {winner.id} ({winner.values.get('period')}) "
                         f"vs {loser.id} ({loser.values.get('period')}). "
                         f"Precedence favors {winner.id} "
                         f"({winner.source_kind.value})."),
                constraint_id=loser.id, related_constraint_ids=[winner.id],
                object_names=[nm],
                suggestion=("Reconcile the conflicting clock definitions; "
                            "the fixed/user constraint takes precedence and "
                            "the inferred value should be removed or aligned."),
                blocking=False,
                evidence={"winner": winner.id, "loser": loser.id,
                          "winner_rank": _precedence_rank(winner),
                          "loser_rank": _precedence_rank(loser),
                          "winner_source_kind": winner.source_kind.value,
                          "loser_source_kind": loser.source_kind.value}))
            continue

    # Conflicting IO delays across different source kinds.
    io_by_key: dict[tuple, list[Constraint]] = defaultdict(list)
    for c in cset.io_constraints():
        clk = c.values.get("clock") or (c.clock_refs[0] if c.clock_refs else None)
        mm = c.values.get("min_max", "max")
        edge = c.values.get("edge")
        add = bool(c.values.get("add_delay"))
        for tgt in c.target_objects or [None]:
            io_by_key[(c.type, tgt, clk, mm, edge, add)].append(c)
    for key, group in io_by_key.items():
        if len(group) < 2:
            continue
        delays = {float(c.values.get("delay")) for c in group
                  if c.values.get("delay") is not None}
        if len(delays) <= 1:
            continue
        ranked = sorted(group, key=_precedence_rank)
        winner, loser = ranked[-1], ranked[0]
        if _precedence_rank(winner) != _precedence_rank(loser):
            conflicts.append(ValidationIssue(
                severity=Severity.WARNING, category=ValidationCategory.CONFLICT,
                code=ErrorCode.CONFLICT_USER_VS_INFERENCE,
                message=(f"Conflicting {key[0].value} on '{key[1]}' "
                         f"(clock={key[2]}): user/fixed {winner.id} "
                         f"({winner.values.get('delay')}) vs "
                         f"{loser.id} ({loser.values.get('delay')}). "
                         f"Precedence favors {winner.id}."),
                constraint_id=loser.id, related_constraint_ids=[winner.id],
                object_names=[key[1]] if key[1] else [],
                suggestion="Reconcile the conflicting delays; the fixed/user "
                           "value has precedence.",
                blocking=False,
                evidence={"winner": winner.id, "loser": loser.id,
                          "winner_source_kind": winner.source_kind.value,
                          "loser_source_kind": loser.source_kind.value}))

    # Contradictory timing exceptions on the same endpoints.
    _contradictory_exceptions(cset, conflicts)


def _contradictory_exceptions(cset: ConstraintSet,
                              conflicts: list[ValidationIssue]) -> None:
    """Report CONFLICT_EXCEPTION when the same path is both covered by a
    false_path and a multicycle_path (or by min/max delay with an
    incompatible intent across the same selector), because the two can
    contradict one another."""
    exc_types = (ConstraintType.SET_FALSE_PATH, ConstraintType.SET_MULTICYCLE_PATH,
                 ConstraintType.SET_MIN_DELAY, ConstraintType.SET_MAX_DELAY)
    by_selector: dict[tuple, list[Constraint]] = defaultdict(list)
    for c in cset:
        if c.type not in exc_types:
            continue
        ps = c.path_selector
        if ps is None:
            continue
        key = (tuple(sorted(ps.from_set)), tuple(sorted(ps.to_set)),
               tuple(tuple(sorted(s)) for s in ps.through_set))
        by_selector[key].append(c)
    for key, group in by_selector.items():
        types = {c.type for c in group}
        # false_path + multicycle (or false_path + max_delay) on the same
        # selector is inherently contradictory.
        if ConstraintType.SET_FALSE_PATH in types and (
                ConstraintType.SET_MULTICYCLE_PATH in types
                or ConstraintType.SET_MAX_DELAY in types):
            ids = sorted(c.id for c in group)
            conflicts.append(ValidationIssue(
                severity=Severity.HIGH, category=ValidationCategory.CONFLICT,
                code=ErrorCode.CONFLICT_EXCEPTION,
                message=(f"Contradictory exceptions share the same path "
                         f"selector (from={list(key[0])}, to={list(key[1])}): "
                         f"{[c.type.value for c in group]} on constraints "
                         f"{ids}. A false_path disables the path while a "
                         f"multicycle/max_delay applies a timing budget."),
                constraint_id=ids[0], related_constraint_ids=ids[1:],
                suggestion="Remove or reconcile the contradictory exceptions.",
                blocking=True,
                evidence={"from": list(key[0]), "to": list(key[1]),
                          "types": sorted({c.type.value for c in group})}))
            continue
        # min_delay + max_delay with max < min is nonsensical (mutually
        # inconsistent values).
        if ConstraintType.SET_MIN_DELAY in types and ConstraintType.SET_MAX_DELAY in types:
            mins = [float(c.values.get("delay")) for c in group
                    if c.type == ConstraintType.SET_MIN_DELAY
                    and c.values.get("delay") is not None]
            maxs = [float(c.values.get("delay")) for c in group
                    if c.type == ConstraintType.SET_MAX_DELAY
                    and c.values.get("delay") is not None]
            if mins and maxs and min(mins) > min(maxs):
                conflicts.append(ValidationIssue(
                    severity=Severity.WARNING, category=ValidationCategory.CONFLICT,
                    code=ErrorCode.CONFLICT_MINMAX_DELAY,
                    message=(f"min_delay {min(mins)} exceeds max_delay "
                             f"{min(maxs)} on the same path selector; the "
                             f"constraints are mutually inconsistent."),
                    constraint_id=group[0].id,
                    related_constraint_ids=[c.id for c in group[1:]],
                    suggestion="Ensure min_delay <= max_delay for the same path.",
                    blocking=False))


def _classify_exception_overlaps(cset: ConstraintSet,
                                 overlaps: list[ValidationIssue]) -> None:
    """Identify broad exceptions that likely shadow narrower ones.

    This is a conservative structural heuristic — a false path with no
    -from/-to/-through (i.e. -all) is ALWAYS flagged as BROAD.
    A false-path set A⊇B is SHADOWED.
    """
    excs = [c for c in cset if c.type in (ConstraintType.SET_FALSE_PATH,
                                          ConstraintType.SET_MULTICYCLE_PATH,
                                          ConstraintType.SET_MIN_DELAY,
                                          ConstraintType.SET_MAX_DELAY)]
    for c in excs:
        ps = c.path_selector
        if ps is None:
            overlaps.append(ValidationIssue(
                severity=Severity.MEDIUM, category=ValidationCategory.OVERLAP,
                code=ErrorCode.OVERLAP_SHADOWED if any(
                    _selector_subset_of(c, other) for other in excs if other.id != c.id
                ) else ErrorCode.EXCEPTION_BROAD,
                message=f"Exception {c.id} ({c.type.value}) has no -from/-to/-through selectors; it applies to all paths.",
                constraint_id=c.id,
                suggestion="Restrict the scope with -from/-through/-to; broad exceptions can silently mask timing problems.",
                blocking=False))
            continue
        empty_from = not ps.from_set
        empty_to = not ps.to_set
        empty_thru = not ps.through_set
        if empty_from and empty_to and empty_thru:
            overlaps.append(ValidationIssue(
                severity=Severity.MEDIUM, category=ValidationCategory.OVERLAP,
                code=ErrorCode.EXCEPTION_BROAD,
                message=f"Exception {c.id} ({c.type.value}) has empty -from/-to/-through; this covers all paths.",
                constraint_id=c.id,
                suggestion="Restrict the scope of the exception.",
                blocking=False))
        # Shadow detection: if another exception's selector set covers this one
        for other in excs:
            if other.id == c.id:
                continue
            if _selector_subset_of(c, other) and other.type == c.type:
                # `other` covers every path that `c` covers and more, so `c` is redundant
                # unless more specific qualifiers differ.
                if (c.values.get("min_max") == other.values.get("min_max")
                        and c.values.get("setup_hold") == other.values.get("setup_hold")):
                    overlaps.append(ValidationIssue(
                        severity=Severity.LOW, category=ValidationCategory.OVERLAP,
                        code=ErrorCode.OVERLAP_REDUNDANT,
                        message=f"Exception {c.id} is subsumed by broader {other.id} ({other.type.value}).",
                        constraint_id=c.id, related_constraint_ids=[other.id],
                        suggestion="Remove the narrower exception if it is intentionally covered, or refine the broader one.",
                        blocking=False))


def _selector_subset_of(narrow: Constraint, broad: Constraint) -> bool:
    """Return True if `broad`'s path selector covers every path covered by
    `narrow`'s selector (conservative, set-based heuristic)."""
    a = narrow.path_selector
    b = broad.path_selector
    if a is None:
        return False  # narrow covers "all" (already flagged as BROAD)
    if b is None:
        return True   # broad covers "all"
    a_from = set(a.from_set) or None
    b_from = set(b.from_set) or None
    a_to = set(a.to_set) or None
    b_to = set(b.to_set) or None
    a_thru = [set(s) for s in a.through_set]
    b_thru = [set(s) for s in b.through_set]
    # from: b empty => covers all; otherwise b.from ⊇ a.from
    if b_from is not None and a_from is not None and not (a_from <= b_from):
        return False
    if b_from is None and a_from is not None:
        pass  # broad covers all starts
    if a_from is None and b_from is not None:
        return False  # narrow covers all starts, broad is restricted
    if b_to is not None and a_to is not None and not (a_to <= b_to):
        return False
    if a_to is None and b_to is not None:
        return False
    # through: broad stages must form a subsequence of narrow stages (ordered)
    if b_thru and a_thru:
        # Naive: each b_stage must contain at least one a_stage (conservatively
        # we require every b_stage superset of some a_stage in order).
        j = 0
        for astage in a_thru:
            while j < len(b_thru) and not (astage <= b_thru[j]):
                j += 1
            if j >= len(b_thru):
                return False
            j += 1
    elif b_thru and not a_thru:
        return False  # narrow has no through constraints; broad restricts through
    return True
