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
    VerificationStatus,
)
from .base import ValidationIssue, ValidationReport, _issue

# Used to determine whether a timing exception has been formally verified.
# When no formal backend is attached, verify_exceptions uses the
# ConservativeFormalBackend, which returns UNRESOLVED for every query.
from ..exceptions.verifier import verify_exceptions
from ..utils.logging import get_logger

log = get_logger("validation.exceptions")


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

    # --- Step 13 §8: exception safety / formal verification state ---
    # Never conclude an exception is safe merely because it may improve
    # timing.  If formal verification is unavailable (the default
    # ConservativeFormalBackend), mark it UNRESOLVED / UNVERIFIED.
    _record_verification_state(design, tg, cset, report)

    report.exception_summary = {
        "exception_count": sum(1 for c in cset if c.type in exc_types and not c.disabled),
    }


def _record_verification_state(design: Design | None, tg: TimingGraph | None,
                               cset: ConstraintSet,
                               report: ValidationReport) -> None:
    """Surface the formal-verification state of timing exceptions.

    Uses the existing ``verify_exceptions`` harness (Step 8) which feeds a
    ``FormalBackend``.  With no formal backend attached it returns
    UNRESOLVED for every query, so we record that the exception is
    *unverified* — never that it is safe.  We do not block on unverified
    exceptions (that is a policy decision left to emission), but every
    non-verified exception is surfaced and labeled UNRESOLVED.
    """
    exc_ids = {c.id for c in cset if c.type in (
        ConstraintType.SET_FALSE_PATH, ConstraintType.SET_MULTICYCLE_PATH,
        ConstraintType.SET_MIN_DELAY, ConstraintType.SET_MAX_DELAY)}
    if not exc_ids:
        return
    try:
        vrep = verify_exceptions(cset, design=design, tg=tg)
    except Exception as exc:
        log.warning("exception verification skipped: %s", exc)
        return
    for r in vrep:
        if r.constraint_id not in exc_ids:
            continue
        vs = r.verification_status
        if vs == VerificationStatus.UNRESOLVED:
            _issue(report, Severity.INFO, ValidationCategory.EXCEPTION,
                   ErrorCode.EXCEPTION_UNVERIFIED,
                   f"Timing exception {r.constraint_id} "
                   f"({r.constraint_type}) is UNRESOLVED: no formal "
                   f"verification backend produced a proof.",
                   constraint_id=r.constraint_id,
                   suggestion=("Mark the exception safe only after formal "
                               "verification or explicit user confirmation."),
                   blocking=False, resolution_status="UNRESOLVED",
                   evidence=dict(r.evidence or {}),
                   origin="exceptions.verifier")


def validate_scenarios(cset: ConstraintSet, report: ValidationReport,
                       active_scenarios: set[str] | None = None) -> None:
    """Scenario coherence (Step 7 §15, strengthened for Step 13 §9).

    * Empty ``Constraint.scenario_ids`` means the constraint applies to all
      active scenarios.
    * Non-empty ``scenario_ids`` applies only to the listed scenarios that
      are active.
    * A scenario id that is not registered/active in ``cset.scenarios`` is
      reported as ``SCENARIO_UNKNOWN_ID``.
    * Scenario-specific issues are emitted with ``scenario_id`` set so the
      scenario identity is never collapsed.

    ``active_scenarios`` optionally supplies the known-active scenario ids
    from the project config (used when MCMM is enabled and the constraint
    set itself does not carry a scenario registry).  Ids in this set are
    treated as known/active so they are not falsely flagged as unknown.
    """
    report.checks_run.append("scenarios")

    # Registry of known scenarios and active-snapshot.
    known_scenarios: dict[str, bool] = {}
    for sid, sc in cset.scenarios.items():
        active = getattr(sc, "active", True)
        known_scenarios[sid] = bool(active)
    active_ids = {sid for sid, act in known_scenarios.items() if act}
    if active_scenarios:
        for sid in active_scenarios:
            known_scenarios.setdefault(sid, True)
            active_ids.add(sid)

    all_referenced: set[str] = set()
    ref_counts: dict[str, int] = {}
    unknown_ids: dict[str, int] = {}
    for c in cset:
        sids = c.scenario_ids or []
        if not sids:
            all_referenced.update(active_ids)
            continue
        for sid in sids:
            all_referenced.add(sid)
            ref_counts[sid] = ref_counts.get(sid, 0) + 1
            if sid not in known_scenarios:
                unknown_ids[sid] = unknown_ids.get(sid, 0) + 1
            elif not known_scenarios[sid]:
                # Referenced but that scenario is inactive — flag gently.
                unknown_ids[sid] = unknown_ids.get(sid, 0) + 1

    # Nonexistent / inactive scenario ids.
    for sid in sorted(unknown_ids):
        _issue(report, Severity.WARNING, ValidationCategory.SCENARIO,
               ErrorCode.SCENARIO_UNKNOWN_ID,
               f"Scenario '{sid}' is referenced by {unknown_ids[sid]} "
               f"constraint(s) but is not an active/known scenario in the "
               f"set.",
               scenario_id=sid, blocking=False,
               evidence={"referenced": unknown_ids[sid],
                         "known": sid in known_scenarios,
                         "active": known_scenarios.get(sid, False)},
               suggestion="Register the scenario or remove the stale id.")

    # Scenario-only constraints referenced by few constraints (conservative).
    if all_referenced and all(v <= 1 for v in ref_counts.values()):
        for sid, cnt in ref_counts.items():
            if cnt <= 1:
                _issue(report, Severity.LOW, ValidationCategory.SCENARIO,
                       ErrorCode.SCENARIO_MISMATCH,
                       f"Scenario '{sid}' is referenced by only {cnt} "
                       f"constraint(s); ensure it is registered.",
                       scenario_id=sid, blocking=False)

    report.scenario_summary = {
        "scenarios": sorted(all_referenced),
        "scenario_count": len(all_referenced),
        "active_scenarios": sorted(active_ids),
        "unknown_scenario_count": len(unknown_ids),
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
