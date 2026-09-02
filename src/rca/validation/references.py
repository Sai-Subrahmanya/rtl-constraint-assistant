"""Reference integrity validation (Step 7 §4).

Check that every object referenced by a UCM constraint refers to a
known design object, a defined clock, or is explicitly marked as a
wildcard/collection. Wildcards (patterns containing ``*?[``) are not
flagged as unknown because they are intentionally unresolved;
instead they are classified as "pattern". An empty target list is an
error.
"""

from __future__ import annotations

import fnmatch
from typing import Iterable

from ..constraint_model import Constraint, ConstraintSet, PathSelector
from ..constraint_model.targets import CollectionKind, TargetRef
from ..design_model import Design
from ..timing_model import TimingGraph
from ..utils.enums import (
    ConstraintType, ErrorCode, Severity, ValidationCategory,
)
from .base import ValidationIssue, ValidationReport


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------

def _port_index(design: Design) -> set[str]:
    return {p.local_name for p in design.top_ports()}


def _net_index(design: Design) -> set[str]:
    if not design.top_module:
        return set()
    return {n.local_name for n in design.nets_of(design.top_module)}


def _cell_index(design: Design) -> set[str]:
    if not design.top_module:
        return set()
    return {i.instance_name for i in design.instances_of(design.top_module)}


def _register_index(design: Design) -> set[str]:
    return {r.local_name for r in design.top_registers()}


def _clock_index(tg: TimingGraph, cset: ConstraintSet) -> set[str]:
    names = set(tg.clocks.keys())
    for c in cset.clocks():
        n = c.values.get("name")
        if n:
            names.add(n)
    for c in cset.generated_clocks():
        n = c.values.get("name")
        if n:
            names.add(n)
    return names


def _pin_index(design: Design) -> set[str]:
    """Flat hierarchical pin index we know about (instances may have pins
    named after their ports). Without full elaboration we cannot enumerate
    every pin; we approximate using instance.port combinations that
    appear in instance connections."""
    pins: set[str] = set()
    for inst in design.instances.values():
        for port_name in inst.port_connections.keys():
            pins.add(f"{inst.instance_name}/{port_name}")
    return pins


def _is_wildcard(name: str) -> bool:
    return any(ch in name for ch in "*?[")


def _classify_target(name: str, *, ports: set[str], nets: set[str],
                    cells: set[str], regs: set[str], pins: set[str],
                    clocks: set[str]) -> str:
    """Classify a target name as 'valid', 'wildcard', or 'unknown'.

    We NEVER infer kind from "/" syntax — if the name looks hierarchical
    (contains "/") we check the pin index. We do NOT treat unknown
    hierarchical names as pins merely because they contain "/".
    """
    if not name:
        return "empty"
    if _is_wildcard(name):
        return "wildcard"
    if name in ports or name in nets or name in cells or name in regs \
            or name in pins or name in clocks:
        return "valid"
    # Heuristic fallback for pins (hierarchical <inst>/<port>): accept
    # only if the instance part is known; otherwise unknown.
    if "/" in name:
        inst = name.split("/", 1)[0]
        if inst in cells:
            return "valid"  # pin on known instance
    return "unknown"


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def validate_references(design: Design | None, tg: TimingGraph | None,
                        cset: ConstraintSet, report: ValidationReport) -> None:
    report.checks_run.append("references")
    ports = _port_index(design) if design else set()
    nets = _net_index(design) if design else set()
    cells = _cell_index(design) if design else set()
    regs = _register_index(design) if design else set()
    pins = _pin_index(design) if design else set()
    clocks = _clock_index(tg, cset) if tg is not None else set(
        c.values.get("name") for c in cset.clocks() if c.values.get("name"))

    summary = {"unknown": [], "unresolved_refs": [], "empty_targets": []}

    # --- target/source objects per constraint ---
    for c in cset:
        _check_target_list(c, c.target_objects, c.target_refs,
                           c.type, ports, nets, cells, regs, pins, clocks,
                           report, summary, role="target")
        _check_target_list(c, c.source_objects, c.source_refs,
                           c.type, ports, nets, cells, regs, pins, clocks,
                           report, summary, role="source")
        # Clock refs: must exist in clock index.
        for cr in c.clock_refs:
            if not cr:
                continue
            if _is_wildcard(cr):
                continue
            if cr not in clocks:
                _issue(report, Severity.ERROR, ValidationCategory.REFERENCE,
                       ErrorCode.REF_UNKNOWN,
                       f"Constraint {c.id} ({c.type.value}) references unknown clock '{cr}'.",
                       constraint_id=c.id, object_names=[cr],
                       suggestion="Define the clock with create_clock/generated_clock or correct the name.",
                       evidence={"ref": cr, "role": "clock"})
                summary["unknown"].append({"constraint": c.id, "name": cr, "role": "clock"})
        # Path selector references
        if c.path_selector is not None:
            _check_path_selector(c, c.path_selector, ports, nets, cells, regs,
                                 pins, clocks, report, summary)

    report.reference_summary = {
        "unknown_ref_count": len(summary["unknown"]),
        "empty_target_count": len(summary["empty_targets"]),
        "unknown": summary["unknown"][:50],
    }


def _check_target_list(c: Constraint, names: list[str], refs: list[TargetRef],
                       ctype: ConstraintType, ports, nets, cells, regs, pins,
                       clocks, report: ValidationReport, summary: dict,
                       role: str) -> None:
    # Typed refs are authoritative; string names are fallbacks.
    if refs:
        for r in refs:
            if r.collection_kind == CollectionKind.EXPR and \
                    r.resolution_status.value == "UNRESOLVED":
                _issue(report, Severity.WARNING, ValidationCategory.REFERENCE,
                       ErrorCode.REF_UNSUPPORTED_SELECTOR,
                       f"Constraint {c.id} has unsupported/unresolved selector: {r.expression or r.unresolved_reason}.",
                       constraint_id=c.id,
                       suggestion="Rewrite the target using a supported get_* collection or a literal name.",
                       evidence={"expression": r.expression,
                                 "reason": r.unresolved_reason})
                summary["unresolved_refs"].append(c.id)
                continue
            for n in r.names():
                if not n:
                    continue
                # For typed refs we trust the kind: only check existence
                # for PORT/PIN/CELL/NET/CLOCK. ALL_* are always valid.
                if r.collection_kind in (CollectionKind.ALL_INPUTS,
                                         CollectionKind.ALL_OUTPUTS,
                                         CollectionKind.ALL_CLOCKS,
                                         CollectionKind.ALL_REGISTERS):
                    continue
                status = _classify_target(n, ports=ports, nets=nets, cells=cells,
                                          regs=regs, pins=pins, clocks=clocks)
                if status == "unknown":
                    _emit_unknown(c, n, role, r.collection_kind.value, report, summary)
        return

    # Fallback to plain-string names.
    if not names:
        # Some constraint types legitimately have no targets (e.g.
        # set_propagated_clock with no args → [all_clocks], which the
        # renderer handles).  Only flag if there are genuinely no
        # targets AND no typed refs for constraints that require them.
        if ctype in (ConstraintType.SET_INPUT_DELAY, ConstraintType.SET_OUTPUT_DELAY,
                     ConstraintType.SET_LOAD, ConstraintType.SET_INPUT_TRANSITION,
                     ConstraintType.SET_MAX_TRANSITION, ConstraintType.SET_MAX_CAPACITANCE,
                     ConstraintType.SET_MAX_FANOUT, ConstraintType.SET_DRIVING_CELL):
            _issue(report, Severity.ERROR, ValidationCategory.REFERENCE,
                   ErrorCode.REF_UNKNOWN,
                   f"Constraint {c.id} ({ctype.value}) has empty {role} list.",
                   constraint_id=c.id,
                   suggestion="Add a target object (port/pin/net/cell).")
            summary["empty_targets"].append(c.id)
        return

    for n in names:
        if not n:
            continue
        status = _classify_target(n, ports=ports, nets=nets, cells=cells,
                                  regs=regs, pins=pins, clocks=clocks)
        if status == "unknown":
            _emit_unknown(c, n, role, "default", report, summary)


def _emit_unknown(c: Constraint, name: str, role: str, kind: str,
                  report: ValidationReport, summary: dict) -> None:
    _issue(report, Severity.WARNING, ValidationCategory.REFERENCE,
           ErrorCode.REF_UNKNOWN,
           f"Constraint {c.id} ({c.type.value}) references unknown {role} object '{name}' (kind={kind}).",
           constraint_id=c.id, object_names=[name],
           suggestion=f"Verify that '{name}' exists in the design or is a valid clock.",
           evidence={"role": role, "kind": kind})
    summary["unknown"].append({"constraint": c.id, "name": name, "role": role})


def _check_path_selector(c: Constraint, ps: PathSelector, ports, nets, cells,
                         regs, pins, clocks, report: ValidationReport,
                         summary: dict) -> None:
    groups = [("from", ps.from_set), ("to", ps.to_set)]
    for label, names in groups:
        for n in names:
            if not n:
                continue
            if _is_wildcard(n):
                continue
            # -from/-to default to clocks; tolerate ports/cells.
            if n in clocks or n in ports or n in cells or n in pins:
                continue
            status = _classify_target(n, ports=ports, nets=nets, cells=cells,
                                      regs=regs, pins=pins, clocks=clocks)
            if status == "unknown":
                _issue(report, Severity.WARNING, ValidationCategory.REFERENCE,
                       ErrorCode.REF_UNKNOWN,
                       f"Constraint {c.id} path selector -{label} references unknown object '{n}'.",
                       constraint_id=c.id, object_names=[n],
                       evidence={"selector": label})
    for i, stage in enumerate(ps.through_set):
        for n in stage:
            if not n or _is_wildcard(n):
                continue
            status = _classify_target(n, ports=ports, nets=nets, cells=cells,
                                      regs=regs, pins=pins, clocks=clocks)
            if status == "unknown":
                _issue(report, Severity.WARNING, ValidationCategory.REFERENCE,
                       ErrorCode.REF_UNKNOWN,
                       f"Constraint {c.id} path selector -through stage {i} references unknown object '{n}'.",
                       constraint_id=c.id, object_names=[n],
                       evidence={"selector": "through", "stage": i})


# ---------------------------------------------------------------------------
# Issue helper
# ---------------------------------------------------------------------------

def _issue(report: ValidationReport, severity: Severity,
           category: ValidationCategory, code: ErrorCode, message: str, *,
           constraint_id: str | None = None,
           related_constraint_ids: list[str] | None = None,
           object_names: list[str] | None = None,
           scenario_id: str | None = None,
           evidence: dict | None = None,
           suggestion: str | None = None,
           blocking: bool | None = None,
           source_location: dict | None = None) -> ValidationIssue:
    # Default blocking: CRITICAL/HIGH/ERROR block by default; WARNING/
    # MEDIUM/LOW/INFO do not.
    if blocking is None:
        blocking = severity in (Severity.CRITICAL, Severity.HIGH, Severity.ERROR)
    iss = ValidationIssue(
        severity=severity, category=category, code=code, message=message,
        constraint_id=constraint_id,
        related_constraint_ids=related_constraint_ids or [],
        object_names=object_names or [],
        scenario_id=scenario_id,
        evidence=evidence or {},
        suggestion=suggestion,
        blocking=blocking,
        source_location=source_location,
    )
    report.add(iss)
    return iss
