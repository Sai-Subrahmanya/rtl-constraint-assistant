"""Semantic validation: clocks, generated clocks, I/O timing, clock
groups, and path-selector coherence (Step 7 §5–§9).
"""

from __future__ import annotations

import math
from typing import Any

from ..constraint_model import Constraint, ConstraintSet, PathSelector
from ..constraint_model.targets import CollectionKind
from ..design_model import Design
from ..timing_model import TimingGraph
from ..utils.enums import (
    ClockDomainRelationship, ConstraintType, ErrorCode, Severity,
    ValidationCategory,
)
from .base import ValidationIssue, ValidationReport, _issue  # noqa: F401  (re-export for convenience)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def validate_semantic(design: Design | None, tg: TimingGraph | None,
                      cset: ConstraintSet, report: ValidationReport) -> None:
    report.checks_run.append("clocks")
    report.checks_run.append("generated_clocks")
    report.checks_run.append("io_timing")
    report.checks_run.append("clock_groups")
    report.checks_run.append("path_selectors")

    clocks = {c.values.get("name"): c for c in cset.clocks() if c.values.get("name")}
    gen_clocks = {c.values.get("name"): c for c in cset.generated_clocks() if c.values.get("name")}
    all_clock_names = set(clocks) | set(gen_clocks)
    if tg is not None:
        all_clock_names |= set(tg.clocks.keys())

    _validate_clocks(cset, clocks, report)
    _validate_generated_clocks(cset, gen_clocks, all_clock_names, report)
    _validate_io_timing(cset, all_clock_names, design, report)
    _validate_clock_groups(cset, all_clock_names, report)
    _validate_path_selectors(cset, report)
    _validate_value_semantics(cset, all_clock_names, report)


# ---------------------------------------------------------------------------
# Clocks (Step 7 §5)
# ---------------------------------------------------------------------------

def _validate_clocks(cset: ConstraintSet, clocks: dict[str, Constraint],
                     report: ValidationReport) -> None:
    # --- Multiple clocks on same source / duplicate names ---
    sources: dict[str, list[str]] = {}
    for c in cset.clocks():
        name = c.values.get("name") or (c.target_objects[0] if c.target_objects else c.id)
        period = c.values.get("period")
        # Period checks
        if period is None:
            _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                   ErrorCode.CLOCK_PERIOD_MISSING,
                   f"Clock '{name}' (constraint {c.id}) has no period.",
                   constraint_id=c.id, object_names=[name],
                   suggestion="Provide a -period value.")
            continue
        try:
            p = float(period)
        except Exception:
            _issue(report, Severity.CRITICAL, ValidationCategory.CLOCK,
                   ErrorCode.CLOCK_PERIOD_INVALID,
                   f"Clock '{name}' (constraint {c.id}) has unparsable period {period!r}.",
                   constraint_id=c.id, object_names=[name],
                   suggestion="Correct the period value to a positive number with time units.")
            continue
        if not math.isfinite(p) or p <= 0:
            _issue(report, Severity.CRITICAL, ValidationCategory.CLOCK,
                   ErrorCode.CLOCK_PERIOD_INVALID,
                   f"Clock '{name}' (constraint {c.id}) has invalid period {p} (must be > 0 and finite).",
                   constraint_id=c.id, object_names=[name],
                   suggestion="Set period to a positive finite value.")
        # Waveform checks
        wf = c.values.get("waveform")
        if wf is not None:
            if not isinstance(wf, (list, tuple)) or len(wf) < 2:
                _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                       ErrorCode.CLOCK_WAVEFORM_INVALID,
                       f"Clock '{name}' (constraint {c.id}) has malformed -waveform.",
                       constraint_id=c.id, object_names=[name],
                       evidence={"waveform": wf})
            else:
                try:
                    edges = [float(x) for x in wf[:2]]
                except Exception:
                    _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                           ErrorCode.CLOCK_WAVEFORM_INVALID,
                           f"Clock '{name}' (constraint {c.id}) waveform edges must be numeric.",
                           constraint_id=c.id, object_names=[name],
                           evidence={"waveform": list(wf)})
                else:
                    if any(e < 0 or not math.isfinite(e) for e in edges):
                        _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                               ErrorCode.CLOCK_WAVEFORM_INVALID,
                               f"Clock '{name}' waveform contains negative/non-finite edges.",
                               constraint_id=c.id, object_names=[name],
                               evidence={"waveform": edges})
                    elif edges[1] <= edges[0]:
                        _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                               ErrorCode.CLOCK_WAVEFORM_INCOHERENT,
                               f"Clock '{name}' falling edge must come after rising edge.",
                               constraint_id=c.id, object_names=[name],
                               suggestion="Ensure waveform is {<rise> <fall>} with fall > rise.")
                    elif period and edges[1] >= float(period):
                        _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                               ErrorCode.CLOCK_WAVEFORM_INCOHERENT,
                               f"Clock '{name}' falling edge at {edges[1]}s >= period {float(period)}s.",
                               constraint_id=c.id, object_names=[name],
                               suggestion="Ensure the second (falling) edge is strictly less than the period.")
        # Source index (to detect multiple clocks on same source)
        for tgt in c.target_objects or [name]:
            sources.setdefault(tgt, []).append(c.id)
    for src, ids in sources.items():
        # Two clocks on the same source without -add is an error.
        non_add = [cid for cid in ids
                   if not cset.get(cid) or not cset.get(cid).values.get("add")]
        if len(non_add) > 1:
            _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                   ErrorCode.CLOCK_MULTIPLE,
                   f"Multiple create_clock constraints on source '{src}': {non_add}.",
                   constraint_id=non_add[0], related_constraint_ids=non_add[1:],
                   object_names=[src],
                   suggestion="Use -add for additional waveforms on the same source, or remove duplicates.",
                   evidence={"source": src, "conflicting": non_add})


# ---------------------------------------------------------------------------
# Generated clocks (Step 7 §6)
# ---------------------------------------------------------------------------

def _validate_generated_clocks(cset: ConstraintSet, gen_clocks: dict[str, Constraint],
                               all_clock_names: set[str],
                               report: ValidationReport) -> None:
    for c in gen_clocks.values():
        name = c.values.get("name", c.id)
        src = c.values.get("source")
        master = c.values.get("master_clock")
        targets = c.target_objects

        if not src:
            _issue(report, Severity.WARNING, ValidationCategory.CLOCK,
                   ErrorCode.GCLK_SOURCE_MISSING,
                   f"Generated clock '{name}' ({c.id}) has no -source pin.",
                   constraint_id=c.id, object_names=[name],
                   suggestion="Provide -source <pin> for the generated clock.")
        if master and master not in all_clock_names:
            _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                   ErrorCode.GCLK_MASTER_MISSING,
                   f"Generated clock '{name}' ({c.id}) references unknown master_clock '{master}'.",
                   constraint_id=c.id, object_names=[name],
                   related_constraint_ids=[],
                   suggestion="Define the master clock with create_clock before this generated clock.")
        if not targets and not src:
            _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                   ErrorCode.GCLK_TARGET_MISSING,
                   f"Generated clock '{name}' ({c.id}) has no target pin.",
                   constraint_id=c.id, object_names=[name])

        # divide_by / multiply_by
        div = c.values.get("divide_by")
        mul = c.values.get("multiply_by")
        if div is not None:
            try:
                d = int(div)
                if d < 1:
                    raise ValueError
            except Exception:
                _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                       ErrorCode.GCLK_INVALID_DIV,
                       f"Generated clock '{name}' has invalid -divide_by {div!r} (must be integer >= 1).",
                       constraint_id=c.id, object_names=[name])
        if mul is not None:
            try:
                m = int(mul)
                if m < 1:
                    raise ValueError
            except Exception:
                _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                       ErrorCode.GCLK_INVALID_MUL,
                       f"Generated clock '{name}' has invalid -multiply_by {mul!r} (must be integer >= 1).",
                       constraint_id=c.id, object_names=[name])
        if div is not None and mul is not None:
            _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                   ErrorCode.GCLK_CONTRADICTORY_OPTIONS,
                   f"Generated clock '{name}' specifies both -divide_by and -multiply_by; these are mutually exclusive.",
                   constraint_id=c.id, object_names=[name],
                   suggestion="Specify exactly one of -divide_by or -multiply_by.")
        # Edges / edge_shift
        edges = c.values.get("edges")
        es = c.values.get("edge_shift")
        if edges is not None:
            if not isinstance(edges, (list, tuple)) or len(edges) < 3:
                _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                       ErrorCode.GCLK_EDGES_INVALID,
                       f"Generated clock '{name}' -edges requires 3 edge values.",
                       constraint_id=c.id, object_names=[name])
            else:
                try:
                    e = [int(x) for x in edges[:3]]
                    if any(x not in (1, 2) for x in e):
                        raise ValueError
                except Exception:
                    _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                           ErrorCode.GCLK_EDGES_INVALID,
                           f"Generated clock '{name}' -edges values must be a sequence of 1/2 edge references.",
                           constraint_id=c.id, object_names=[name],
                           evidence={"edges": list(edges)})
        if es is not None:
            if not edges:
                _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                       ErrorCode.GCLK_EDGE_SHIFT_WITHOUT_EDGES,
                       f"Generated clock '{name}' specifies -edge_shift without -edges.",
                       constraint_id=c.id, object_names=[name],
                       suggestion="Add -edges {{1 2 3}} when using -edge_shift.")
        if c.values.get("combinational") and (div is not None or mul is not None
                                              or edges is not None):
            _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                   ErrorCode.GCLK_CONTRADICTORY_OPTIONS,
                   f"Generated clock '{name}' combines -combinational with divide/multiply/edges.",
                   constraint_id=c.id, object_names=[name],
                   suggestion="-combinational generated clocks must not specify divide/multiply/edges.")


# ---------------------------------------------------------------------------
# I/O timing (Step 7 §7)
# ---------------------------------------------------------------------------

def _validate_io_timing(cset: ConstraintSet, all_clock_names: set[str],
                        design: Design | None,
                        report: ValidationReport) -> None:
    port_dir = {}
    if design is not None:
        for p in design.top_ports():
            port_dir[p.local_name] = p.direction.value

    seen_keys: dict[tuple, list[str]] = {}
    for c in cset.io_constraints():
        clk = (c.values.get("clock")
               or (c.clock_refs[0] if c.clock_refs else None))
        delay = c.values.get("delay")
        mm = c.values.get("min_max", "max")
        edge = c.values.get("edge")
        add_delay = bool(c.values.get("add_delay"))

        if delay is None:
            _issue(report, Severity.ERROR, ValidationCategory.TIMING,
                   ErrorCode.IO_DELAY_INVALID,
                   f"{c.type.value} {c.id} is missing a delay value.",
                   constraint_id=c.id,
                   suggestion="Provide a delay value.")
            continue
        try:
            d = float(delay)
            if not math.isfinite(d) or d < 0:
                raise ValueError
        except Exception:
            _issue(report, Severity.ERROR, ValidationCategory.TIMING,
                   ErrorCode.IO_DELAY_INVALID,
                   f"{c.type.value} {c.id} has invalid delay {delay!r}.",
                   constraint_id=c.id,
                   evidence={"delay": delay})
            continue
        if mm not in ("min", "max", "both"):
            _issue(report, Severity.ERROR, ValidationCategory.TIMING,
                   ErrorCode.IO_MIN_MAX_INCOHERENT,
                   f"{c.type.value} {c.id} has invalid min_max {mm!r}.",
                   constraint_id=c.id)
        if edge not in (None, "rise", "fall"):
            _issue(report, Severity.ERROR, ValidationCategory.TIMING,
                   ErrorCode.IO_MIN_MAX_INCOHERENT,
                   f"{c.type.value} {c.id} has invalid edge {edge!r}.",
                   constraint_id=c.id)
        if clk and clk not in all_clock_names:
            _issue(report, Severity.ERROR, ValidationCategory.TIMING,
                   ErrorCode.IO_CLOCK_UNKNOWN,
                   f"{c.type.value} {c.id} references unknown clock '{clk}'.",
                   constraint_id=c.id, object_names=[clk],
                   suggestion=f"Define clock '{clk}' with create_clock.")
        # Direction check
        for tgt in c.target_objects:
            direction = port_dir.get(tgt)
            if direction:
                if c.type == ConstraintType.SET_INPUT_DELAY and direction == "output":
                    _issue(report, Severity.ERROR, ValidationCategory.TIMING,
                           ErrorCode.IO_WRONG_DIRECTION,
                           f"set_input_delay {c.id} applied to output port '{tgt}'.",
                           constraint_id=c.id, object_names=[tgt],
                           suggestion="Apply set_input_delay only to input/inout ports; use set_output_delay for outputs.")
                if c.type == ConstraintType.SET_OUTPUT_DELAY and direction == "input":
                    _issue(report, Severity.ERROR, ValidationCategory.TIMING,
                           ErrorCode.IO_WRONG_DIRECTION,
                           f"set_output_delay {c.id} applied to input port '{tgt}'.",
                           constraint_id=c.id, object_names=[tgt],
                           suggestion="Apply set_output_delay only to output/inout ports.")
            # Duplicate detection: same (cmd, target, clock, min_max, edge, add_delay)
            key = (c.type.value, tgt, clk, mm, edge, add_delay)
            seen_keys.setdefault(key, []).append(c.id)

    for key, ids in seen_keys.items():
        if len(ids) > 1:
            _issue(report, Severity.WARNING, ValidationCategory.TIMING,
                   ErrorCode.IO_DUPLICATE,
                   f"Duplicate {key[0]} on '{key[1]}' (clock={key[2]}, min_max={key[3]}, edge={key[4]}, add_delay={key[5]}): constraints {ids}.",
                   constraint_id=ids[0], related_constraint_ids=ids[1:],
                   object_names=[key[1]],
                   suggestion="Remove duplicate constraints or consolidate them.")


# ---------------------------------------------------------------------------
# Clock groups (Step 7 §8)
# ---------------------------------------------------------------------------

def _validate_clock_groups(cset: ConstraintSet, all_clock_names: set[str],
                           report: ValidationReport) -> None:
    # Index relationships per (clock_a, clock_b) unordered pair.
    pair_rels: dict[tuple[str, str], list[tuple[str, str]]] = {}

    for c in cset.by_type(ConstraintType.SET_CLOCK_GROUPS):
        groups = c.values.get("groups", [])
        rel = c.values.get("relationship", "asynchronous")
        if rel not in ("asynchronous", "logically_exclusive", "physically_exclusive"):
            _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                   ErrorCode.GROUPS_EMPTY,
                   f"set_clock_groups {c.id} has invalid relationship {rel!r}.",
                   constraint_id=c.id)
            continue
        if len(groups) < 2:
            _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                   ErrorCode.GROUPS_EMPTY,
                   f"set_clock_groups {c.id} must specify at least two groups.",
                   constraint_id=c.id,
                   suggestion="Add at least two -group arguments.")
            continue
        for gi, g in enumerate(groups):
            members = list(g)
            if not members:
                _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                       ErrorCode.GROUPS_EMPTY,
                       f"set_clock_groups {c.id} group {gi} is empty.",
                       constraint_id=c.id)
            # Duplicate within a group
            if len(set(members)) != len(members):
                dupes = [m for m in members if members.count(m) > 1]
                _issue(report, Severity.WARNING, ValidationCategory.CLOCK,
                       ErrorCode.GROUPS_DUPLICATE_MEMBER,
                       f"set_clock_groups {c.id} group {gi} has duplicate members: {sorted(set(dupes))}.",
                       constraint_id=c.id,
                       suggestion="Remove duplicate entries from the group.")
            for m in members:
                if _is_wildcard(m):
                    continue
                if m not in all_clock_names:
                    _issue(report, Severity.ERROR, ValidationCategory.CLOCK,
                           ErrorCode.GROUPS_CLOCK_UNKNOWN,
                           f"set_clock_groups {c.id} references unknown clock '{m}'.",
                           constraint_id=c.id, object_names=[m])
            # Record inter-group relationships
            for og_i, og in enumerate(groups):
                if og_i <= gi:
                    continue
                for a in members:
                    for b in og:
                        if a == b:
                            _issue(report, Severity.WARNING, ValidationCategory.CLOCK,
                                   ErrorCode.GROUPS_DUPLICATE_MEMBER,
                                   f"set_clock_groups {c.id} has clock '{a}' in multiple groups (self-grouping).",
                                   constraint_id=c.id, object_names=[a])
                            continue
                        pair = tuple(sorted((a, b)))
                        pair_rels.setdefault(pair, []).append((rel, c.id))

    # Detect conflicting relationships between the same clock pair.
    for pair, rels in pair_rels.items():
        rel_kinds = {r for r, _ in rels}
        if len(rel_kinds) > 1:
            cids = sorted({cid for _, cid in rels})
            _issue(report, Severity.ERROR, ValidationCategory.CONFLICT,
                   ErrorCode.GROUPS_CONTRADICTORY_RELATIONSHIP,
                   f"Clocks '{pair[0]}' and '{pair[1]}' have conflicting group relationships: {sorted(rel_kinds)} (constraints {cids}).",
                   constraint_id=cids[0], related_constraint_ids=cids[1:],
                   object_names=list(pair),
                   suggestion="Pick a single group relationship; remove or reconcile conflicting declarations.")


# ---------------------------------------------------------------------------
# Path-selector sanity (Step 7 §9)
# ---------------------------------------------------------------------------

def _validate_path_selectors(cset: ConstraintSet, report: ValidationReport) -> None:
    for c in cset:
        ps = c.path_selector
        if ps is None:
            continue
        # Detect duplicates
        seen_stages = set()
        for i, stage in enumerate(ps.through_set):
            key = tuple(sorted(stage))
            if key in seen_stages:
                _issue(report, Severity.WARNING, ValidationCategory.EXCEPTION,
                       ErrorCode.PATH_SELECTOR_DUPLICATE_STAGE,
                       f"Constraint {c.id} has duplicate -through stage {stage}.",
                       constraint_id=c.id,
                       evidence={"stage_index": i, "stage": list(stage)})
            seen_stages.add(key)
        # Coherence for multicycle
        if c.type == ConstraintType.SET_MULTICYCLE_PATH:
            cyc = c.values.get("cycles")
            try:
                if cyc is None or int(cyc) < 1:
                    raise ValueError
            except Exception:
                _issue(report, Severity.ERROR, ValidationCategory.EXCEPTION,
                       ErrorCode.EXCEPTION_BAD_CYCLES,
                       f"set_multicycle_path {c.id} has invalid cycle count {cyc!r} (must be >= 1).",
                       constraint_id=c.id,
                       suggestion="Set -cycle to an integer >= 1.")
            sh = ps.setup_hold
            if c.values.get("start") and c.values.get("end"):
                _issue(report, Severity.WARNING, ValidationCategory.EXCEPTION,
                       ErrorCode.EXCEPTION_SETUP_HOLD_INCOHERENT,
                       f"set_multicycle_path {c.id} specifies both -start and -end.",
                       constraint_id=c.id)


# ---------------------------------------------------------------------------
# Value/unit/range semantics for the remaining constraint types (Step 13 §3)
# ---------------------------------------------------------------------------

def _validate_value_semantics(cset: ConstraintSet, all_clock_names: set[str],
                              report: ValidationReport) -> None:
    """Validate required values / units / ranges for clock uncertainty,
    clock latency, clock transition, input transition, load, driving cell,
    design rules, and min/max delay constraints.

    This is strictly observational: it never rewrites a value. If a value
    is merely absent but the constraint may still be meaningful (e.g. a
    set_driving_cell without an explicit -max/-min), we only flag genuine
    contradictions and out-of-range / non-numeric values.
    """
    for c in cset:
        t = c.type
        if t == ConstraintType.SET_CLOCK_UNCERTAINTY:
            _check_positive_value(c, "uncertainty", report, allow_zero=True)
            # uncertainty without a target clock is meaningless
            if not (c.clock_refs or c.target_objects):
                _issue(report, Severity.WARNING, ValidationCategory.CLOCK,
                       ErrorCode.CLOCK_UNCERTAINTY_INVALID,
                       f"set_clock_uncertainty {c.id} has no target clock.",
                       constraint_id=c.id,
                       suggestion="Specify the clock(s) the uncertainty applies to.")
        elif t == ConstraintType.SET_CLOCK_LATENCY:
            # latency may legitimately be negative (early/late); only check
            # that a value is present and finite.
            _check_finite_value(c, "latency", report)
            if not (c.clock_refs or c.target_objects):
                _issue(report, Severity.WARNING, ValidationCategory.CLOCK,
                       ErrorCode.CLOCK_LATENCY_INVALID,
                       f"set_clock_latency {c.id} has no target clock.",
                       constraint_id=c.id,
                       suggestion="Specify the clock(s) the latency applies to.")
        elif t == ConstraintType.SET_CLOCK_TRANSITION:
            _check_positive_value(c, "transition", report)
        elif t == ConstraintType.SET_INPUT_TRANSITION:
            _check_positive_value(c, "transition", report)
            # input transition requires a target port
            if not c.target_objects:
                _issue(report, Severity.ERROR, ValidationCategory.TIMING,
                       ErrorCode.IO_TRANSITION_INVALID,
                       f"set_input_transition {c.id} has no target port.",
                       constraint_id=c.id,
                       suggestion="Specify the input port(s).")
        elif t == ConstraintType.SET_LOAD:
            _check_positive_value(c, "load", report, allow_zero=True)
            if not c.target_objects:
                _issue(report, Severity.ERROR, ValidationCategory.TIMING,
                       ErrorCode.LOAD_INVALID,
                       f"set_load {c.id} has no target port/net.",
                       constraint_id=c.id,
                       suggestion="Specify the port or net to load.")
        elif t == ConstraintType.SET_DRIVING_CELL:
            if not c.target_objects:
                _issue(report, Severity.ERROR, ValidationCategory.TIMING,
                       ErrorCode.DRIVING_CELL_INVALID,
                       f"set_driving_cell {c.id} has no target port.",
                       constraint_id=c.id,
                       suggestion="Specify the input port(s).")
            cell = c.values.get("cell") or c.values.get("lib_cell") or c.values.get("cell_name")
            if cell is None:
                _issue(report, Severity.WARNING, ValidationCategory.TIMING,
                       ErrorCode.DRIVING_CELL_INVALID,
                       f"set_driving_cell {c.id} does not name a library cell.",
                       constraint_id=c.id,
                       suggestion="Provide -lib_cell or -cell to name the driving cell.")
        elif t == ConstraintType.SET_MAX_TRANSITION:
            _check_positive_value(c, "max_transition", report)
        elif t == ConstraintType.SET_MAX_CAPACITANCE:
            _check_positive_value(c, "max_capacitance", report)
        elif t == ConstraintType.SET_MAX_FANOUT:
            _check_positive_value(c, "max_fanout", report, integer=True)
        elif t in (ConstraintType.SET_MIN_DELAY, ConstraintType.SET_MAX_DELAY):
            _check_positive_value(c, "delay", report, allow_zero=True)
            if c.path_selector is not None:
                ps = c.path_selector
                if not ps.from_set and not ps.to_set and not ps.through_set:
                    _issue(report, Severity.WARNING, ValidationCategory.EXCEPTION,
                           ErrorCode.MINMAX_DELAY_INVALID,
                           f"{t.value} {c.id} has no path selector (-from/-to/-through); it applies to all paths.",
                           constraint_id=c.id,
                           suggestion="Restrict the delay constraint with -from/-through/-to.")


def _check_positive_value(c: Constraint, field: str, report: ValidationReport,
                          allow_zero: bool = False, integer: bool = False) -> None:
    v = c.values.get(field)
    code = _value_code_for(c.type)
    if v is None:
        _issue(report, Severity.ERROR, ValidationCategory.TIMING,
               code,
               f"{c.type.value} {c.id} is missing its {field} value.",
               constraint_id=c.id,
               suggestion="Provide a non-negative numeric value.")
        return
    try:
        if integer:
            f = float(v)
            if not math.isfinite(f) or f != int(f):
                raise ValueError
        n = float(v)
    except Exception:
        _issue(report, Severity.ERROR, ValidationCategory.TIMING,
               code,
               f"{c.type.value} {c.id} has non-numeric {field} value {v!r}.",
               constraint_id=c.id, evidence={field: v})
        return
    if not math.isfinite(n) or n < 0 or (n == 0 and not allow_zero):
        _issue(report, Severity.ERROR, ValidationCategory.TIMING,
               code,
               f"{c.type.value} {c.id} has invalid {field} value {n} "
               f"(must be {'>= 0' if allow_zero else '> 0'}).",
               constraint_id=c.id, evidence={field: n})


def _check_finite_value(c: Constraint, field: str, report: ValidationReport) -> None:
    v = c.values.get(field)
    code = _value_code_for(c.type)
    if v is None:
        return  # latency may default; not a hard missing-value error
    try:
        n = float(v)
    except Exception:
        _issue(report, Severity.ERROR, ValidationCategory.CLOCK, code,
               f"{c.type.value} {c.id} has non-numeric {field} value {v!r}.",
               constraint_id=c.id, evidence={field: v})
        return
    if not math.isfinite(n):
        _issue(report, Severity.ERROR, ValidationCategory.CLOCK, code,
               f"{c.type.value} {c.id} has non-finite {field} value {n!r}.",
               constraint_id=c.id, evidence={field: n})


def _value_code_for(t: ConstraintType) -> ErrorCode:
    return {
        ConstraintType.SET_CLOCK_UNCERTAINTY: ErrorCode.CLOCK_UNCERTAINTY_INVALID,
        ConstraintType.SET_CLOCK_LATENCY: ErrorCode.CLOCK_LATENCY_INVALID,
        ConstraintType.SET_CLOCK_TRANSITION: ErrorCode.CLOCK_TRANSITION_INVALID,
        ConstraintType.SET_INPUT_TRANSITION: ErrorCode.IO_TRANSITION_INVALID,
        ConstraintType.SET_LOAD: ErrorCode.LOAD_INVALID,
        ConstraintType.SET_DRIVING_CELL: ErrorCode.DRIVING_CELL_INVALID,
        ConstraintType.SET_MAX_TRANSITION: ErrorCode.DESIGN_RULE_INVALID,
        ConstraintType.SET_MAX_CAPACITANCE: ErrorCode.DESIGN_RULE_INVALID,
        ConstraintType.SET_MAX_FANOUT: ErrorCode.DESIGN_RULE_INVALID,
        ConstraintType.SET_MIN_DELAY: ErrorCode.MINMAX_DELAY_INVALID,
        ConstraintType.SET_MAX_DELAY: ErrorCode.MINMAX_DELAY_INVALID,
    }.get(t, ErrorCode.VALIDATION_ERROR)


def _is_wildcard(name: str) -> bool:
    return any(ch in name for ch in "*?[")
