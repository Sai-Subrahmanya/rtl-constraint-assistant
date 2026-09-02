"""Exception sanity checks (Step 7 §14) and scenario validation (§15).

These are conservative structural checks. Full formal verification is
a later step; this module flags exceptions that are structurally
suspicious (broad scope, no effect, bad cycles, setup/hold
inconsistencies)."""

from __future__ import annotations

from ..constraint_model import Constraint, ConstraintSet
from ..design_model import Design
from ..timing_model import TimingGraph
from ..utils.enums import (
    ClockDomainRelationship,
    ConstraintType,
    ErrorCode,
    Severity,
    ValidationCategory,
)
from .base import ValidationIssue, ValidationReport, _issue


def validate_exceptions(design: Design | None, tg: TimingGraph | None,
                        cset: ConstraintSet, report: ValidationReport) -> None:
    report.checks_run.append("exceptions")
    exc_types = (ConstraintType.SET_FALSE_PATH, ConstraintType.SET_MULTICYCLE_PATH,
                 ConstraintType.SET_MIN_DELAY, ConstraintType.SET_MAX_DELAY)
    for c in cset:
        if c.type not in exc_types:
            continue
        ps = c.path_selector
        # Multicycle-specific
        if c.type == ConstraintType.SET_MULTICYCLE_PATH:
            cyc = c.values.get("cycles")
            try:
                if cyc is None or int(cyc) < 0:
                    raise ValueError
            except Exception:
                _issue(report, Severity.ERROR, ValidationCategory.EXCEPTION,
                       ErrorCode.EXCEPTION_BAD_CYCLES,
                       f"set_multicycle_path {c.id} has invalid cycle count {cyc!r}.",
                       constraint_id=c.id,
                       suggestion="Set -cycles to a non-negative integer.")
            if bool(c.values.get("start")) and bool(c.values.get("end")):
                _issue(report, Severity.WARNING, ValidationCategory.EXCEPTION,
                       ErrorCode.EXCEPTION_SETUP_HOLD_INCOHERENT,
                       f"set_multicycle_path {c.id} specifies both -start and -end (likely unintended).",
                       constraint_id=c.id,
                       suggestion="Specify either -start or -end, not both.")
        # Empty selector → applies to all paths
        if ps is None or not (ps.from_set or ps.to_set or ps.through_set):
            if c.type == ConstraintType.SET_FALSE_PATH:
                _issue(report, Severity.HIGH, ValidationCategory.EXCEPTION,
                       ErrorCode.EXCEPTION_BROAD,
                       f"set_false_path {c.id} has no -from/-to/-through; this disables ALL timing checks.",
                       constraint_id=c.id,
                       suggestion="Restrict with -from/-to/-through; false_path with no selectors is almost never intended.",
                       blocking=False)
            else:
                _issue(report, Severity.MEDIUM, ValidationCategory.EXCEPTION,
                       ErrorCode.EXCEPTION_BROAD,
                       f"Exception {c.id} ({c.type.value}) has no -from/-to/-through (very broad scope).",
                       constraint_id=c.id,
                       suggestion="Restrict the exception scope.")
            continue
        # Structurally no-effect: if -from clocks are in domains that have
        # no possible structural path to -to clocks, flag NO_EFFECT.
        if tg is not None and ps.from_set and ps.to_set:
            structurally_possible = _any_path_exists(tg, ps.from_set, ps.to_set)
            if structurally_possible is False:
                _issue(report, Severity.LOW, ValidationCategory.EXCEPTION,
                       ErrorCode.EXCEPTION_NO_EFFECT,
                       f"Exception {c.id} affects no structurally known path between {list(ps.from_set)} and {list(ps.to_set)}.",
                       constraint_id=c.id,
                       object_names=list(ps.from_set) + list(ps.to_set),
                       suggestion="The exception may be unnecessary or misspelled.")
        # Cross-domain without a relationship declaration.
        if tg is not None and ps.from_set and ps.to_set:
            known_clocks = set(tg.clocks.keys())
            from_clocks = [x for x in ps.from_set if x in known_clocks]
            to_clocks = [x for x in ps.to_set if x in known_clocks]
            for a in from_clocks:
                for b in to_clocks:
                    if a == b:
                        continue
                    edge = _find_edge(tg, a, b)
                    if edge is not None and edge.relationship == ClockDomainRelationship.UNKNOWN:
                        _issue(report, Severity.MEDIUM, ValidationCategory.EXCEPTION,
                               ErrorCode.EXCEPTION_SUSPICIOUS,
                               f"Exception {c.id} crosses unrelated clocks '{a}' -> '{b}' without an explicit CDC relationship.",
                               constraint_id=c.id,
                               object_names=[a, b],
                               suggestion="Declare the clock relationship (set_clock_groups) before relying on this exception.")

    report.exception_summary = {
        "exception_count": sum(1 for c in cset if c.type in exc_types and not c.disabled),
    }


def validate_scenarios(cset: ConstraintSet, report: ValidationReport) -> None:
    """Step 7 §15: scenario coherence. This implementation is conservative:
    if any constraint lists scenario_ids that no other constraint or
    project info references, flag for review."""
    report.checks_run.append("scenarios")
    all_scenarios: set[str] = set()
    ref_counts: dict[str, int] = {}
    for c in cset:
        for sid in c.scenario_ids or []:
            all_scenarios.add(sid)
            ref_counts[sid] = ref_counts.get(sid, 0) + 1
    # With no project scenario registry available at this layer, we only
    # flag the special case where a constraint has scenario_ids but the
    # same constraint is ALSO in the global set (i.e., no scenario-only
    # constraints are present and scenario_ids may be stale). We do NOT
    # invent a registry here.
    if all_scenarios and all(v <= 1 for v in ref_counts.values()):
        for sid, cnt in ref_counts.items():
            if cnt <= 1:
                _issue(report, Severity.LOW, ValidationCategory.SCENARIO,
                       ErrorCode.SCENARIO_MISMATCH,
                       f"Scenario '{sid}' is referenced by only {cnt} constraint(s); ensure it is registered.",
                       scenario_id=sid, blocking=False)
    report.scenario_summary = {
        "scenarios": sorted(all_scenarios),
        "scenario_count": len(all_scenarios),
    }


def _any_path_exists(tg: TimingGraph, from_set: list[str], to_set: list[str]) -> bool | None:
    """Return True/False if any structural path exists between the given
    clock/object sets. Returns None when graph is insufficient."""
    # Conservative: if we can't reason, return None (unknown).
    if not tg.paths:
        return None
    # Look for at least one TimingPath whose start is in from_set
    # (matching clock name or any object in from_set) and end in to_set.
    found = False
    for p in tg.paths:
        s = getattr(p, "startpoint", None)
        e = getattr(p, "endpoint", None)
        sc = getattr(p, "launch_clock", None)
        ec = getattr(p, "capture_clock", None)
        if (s in from_set or sc in from_set) and (e in to_set or ec in to_set):
            found = True
            break
    return found


def _find_edge(tg: TimingGraph, a: str, b: str):
    for e in tg.domain_edges:
        if {e.clock_a, e.clock_b} == {a, b}:
            return e
    return None
