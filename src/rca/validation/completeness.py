"""Completeness / missing-information validation (Step 13, Req 7).

Surfaces what is *missing* from the timing environment so the user can
supply it, without ever inventing a value.  Each finding is classified as
one of:

* ``REQUIRES_USER_INPUT`` — a concrete value is needed and RCA will not
  guess it (e.g. an unresolved clock relationship, a generated-clock
  divider factor, an input delay for a data port whose capture clock is
  known).
* ``UNKNOWN`` — the state cannot be determined from available info.
* ``UNRESOLVED`` — a reference/relationship could not be resolved yet.

The validator never turns a missing value into a valid or invalid one;
it only records that the constraint/environment is *incomplete* and
points at exactly what is missing.
"""

from __future__ import annotations

from typing import Any

from ..constraint_model import Constraint, ConstraintSet
from ..design_model import Design
from ..timing_model import TimingGraph
from ..utils.enums import (
    ClockDomainRelationship,
    ConstraintType,
    ErrorCode,
    Severity,
    TimingPathClass,
    ValidationCategory,
)
from .base import ValidationIssue, ValidationReport, _issue

_REQUIRES = "REQUIRES_USER_INPUT"


def validate_completeness(design: Design | None, tg: TimingGraph | None,
                          cset: ConstraintSet, report: ValidationReport) -> None:
    report.checks_run.append("completeness")

    # NOTE: a missing clock *period* is already reported by the semantic
    # layer as CLOCK_PERIOD_MISSING. We deliberately do NOT duplicate that
    # here; completeness focuses on graph-derived and generated-clock
    # information that no other layer surfaces.

    # --- 1. Unresolved clock relationships (graph side) ---
    if tg is not None:
        for e in sorted(tg.domain_edges, key=lambda x: (x.clock_a, x.clock_b)):
            if e.relationship == ClockDomainRelationship.UNKNOWN and e.user_confirmation_required:
                _issue(report, Severity.WARNING, ValidationCategory.COMPLETENESS,
                       ErrorCode.COMPLETENESS_CLOCK_RELATIONSHIP,
                       f"Relationship between clocks '{e.clock_a}' and "
                       f"'{e.clock_b}' is unresolved; CDC handling requires "
                       f"confirmation.",
                       object_names=[e.clock_a, e.clock_b],
                       suggestion="Declare set_clock_groups or confirm the "
                                  "relationship for this clock pair.",
                       resolution_status=_REQUIRES, blocking=False,
                       origin="timing_graph")

    # --- 3. Generated-clock missing source / divider (completeness view) ---
    for c in cset.generated_clocks():
        name = c.values.get("name", c.id)
        # A generated clock that specifies neither divide_by nor multiply_by
        # nor edges nor combinational is incomplete: no transformation is
        # defined, so the generated clock's period is unresolvable.
        div = c.values.get("divide_by")
        mul = c.values.get("multiply_by")
        edges = c.values.get("edges")
        comb = c.values.get("combinational")
        if div is None and mul is None and edges is None and not comb:
            _issue(report, Severity.WARNING, ValidationCategory.COMPLETENESS,
                   ErrorCode.COMPLETENESS_GENERATED_CLOCK,
                   f"Generated clock '{name}' ({c.id}) does not define a "
                   f"transform (no -divide_by/-multiply_by/-edges/"
                   f"-combinational); its period cannot be derived.",
                   constraint_id=c.id, object_names=[name],
                   suggestion="Add one of -divide_by, -multiply_by, "
                              "-edges, or -combinational.",
                   resolution_status=_REQUIRES, blocking=False,
                   origin="semantic")
        if c.values.get("source") is None:
            _issue(report, Severity.WARNING, ValidationCategory.COMPLETENESS,
                   ErrorCode.COMPLETENESS_GENERATED_CLOCK,
                   f"Generated clock '{name}' ({c.id}) is missing its "
                   f"-source pin; it cannot be located in the design.",
                   constraint_id=c.id, object_names=[name],
                   resolution_status=_REQUIRES, blocking=False,
                   origin="semantic")

    # --- 4. Missing IO timing (inputs / outputs with no delay) ---
    if tg is not None:
        _missing_io_timing(tg, cset, report)

    # --- 5. Unresolved timing environment ---
    if tg is not None:
        for h in sorted(tg.input_clock_assoc):
            if tg.input_clock_assoc[h] is None:
                leaf = h.split(".")[-1]
                _issue(report, Severity.WARNING, ValidationCategory.COMPLETENESS,
                       ErrorCode.COMPLETENESS_ENVIRONMENT,
                       f"Input port '{leaf}' has no identified clock "
                       f"association; its input timing cannot be resolved.",
                       object_names=[leaf],
                       suggestion="Associate the input with a clock before "
                                  "applying set_input_delay.",
                       resolution_status="UNRESOLVED", blocking=False,
                       origin="timing_graph")
        for h in sorted(tg.output_clock_assoc):
            if tg.output_clock_assoc[h] is None:
                leaf = h.split(".")[-1]
                _issue(report, Severity.WARNING, ValidationCategory.COMPLETENESS,
                       ErrorCode.COMPLETENESS_ENVIRONMENT,
                       f"Output port '{leaf}' has no identified clock "
                       f"association; its output timing cannot be resolved.",
                       object_names=[leaf],
                       suggestion="Associate the output with a clock before "
                                  "applying set_output_delay.",
                       resolution_status="UNRESOLVED", blocking=False,
                       origin="timing_graph")


def _missing_io_timing(tg: TimingGraph, cset: ConstraintSet,
                       report: ValidationReport) -> None:
    """Flag input/output ports that have a structural path but no
    corresponding set_input_delay / set_output_delay, using the same
    association data the coverage layer uses.  This is a *completeness*
    signal, not a duplicate of the coverage metric: it reports the
    specific missing constraint without computing percentages."""
    input_ports: set[str] = set()
    output_ports: set[str] = set()
    for p in tg.paths:
        pt = getattr(p, "path_type", None)
        if pt in (TimingPathClass.INPUT_TO_REG, TimingPathClass.INPUT_TO_OUTPUT):
            input_ports.add(p.startpoint)
        if pt in (TimingPathClass.REG_TO_OUTPUT, TimingPathClass.INPUT_TO_OUTPUT):
            output_ports.add(p.endpoint)

    def leaf(name: str) -> str:
        return name.split(".")[-1]

    has_input_delay = {leaf(t) for c in cset.by_type(ConstraintType.SET_INPUT_DELAY)
                       for t in (c.target_objects or [])}
    has_output_delay = {leaf(t) for c in cset.by_type(ConstraintType.SET_OUTPUT_DELAY)
                        for t in (c.target_objects or [])}
    # Compare on leaf names so hierarchical (counter.q) vs local (q) naming
    # does not create a false "missing" finding.
    missing_inputs = sorted({p for p in input_ports
                             if leaf(p) not in has_input_delay})
    missing_outputs = sorted({p for p in output_ports
                              if leaf(p) not in has_output_delay})

    for port in missing_inputs:
        _issue(report, Severity.WARNING, ValidationCategory.COMPLETENESS,
               ErrorCode.COMPLETENESS_IO_TIMING,
               f"Input port '{port}' has a structural path but no "
               f"set_input_delay.",
               object_names=[port],
               suggestion="Add set_input_delay on '{port}' referencing its "
                          "capture clock.",
               resolution_status=_REQUIRES, blocking=False,
               origin="timing_graph")
    for port in missing_outputs:
        _issue(report, Severity.WARNING, ValidationCategory.COMPLETENESS,
               ErrorCode.COMPLETENESS_IO_TIMING,
               f"Output port '{port}' has a structural path but no "
               f"set_output_delay.",
               object_names=[port],
               suggestion="Add set_output_delay on '{port}' referencing its "
                          "launch clock.",
               resolution_status=_REQUIRES, blocking=False,
               origin="timing_graph")
