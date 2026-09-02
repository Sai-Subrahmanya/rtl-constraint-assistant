"""Preflight validation: backend-independent checks that must pass before
a constraint is emittable.

Returns :class:`PreflightIssue` objects rather than raising so the
renderer can accumulate diagnostics and mark PARTIAL/BLOCKED status.

Capability negotiation policy (Step 6 corrective pass):

* When a backend capability flag is False for an option/command the
  constraint requires, we emit a FATAL error and skip emission.
* WARNING issues do not block emission but are recorded.
* The renderer must NOT emit an option when the backend declares the
  corresponding capability False; if the semantic field materially
  changes timing the constraint must be blocked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...constraint_model import Constraint, PathSelector
from ...utils.enums import CollectionKind, ConstraintType


@dataclass
class PreflightIssue:
    code: str
    message: str
    severity: str = "ERROR"   # ERROR blocks emission, WARNING allows partial
    fatal: bool = False      # True means constraint cannot be emitted at all


def _cap(capabilities: dict[str, bool], key: str) -> bool:
    """True means the capability is supported (default True for safety)."""
    return capabilities.get(key, True)


def preflight_constraint(c: Constraint, capabilities: dict[str, bool]) -> list[PreflightIssue]:
    issues: list[PreflightIssue] = []
    t = c.type
    v = c.values

    def err(code: str, msg: str) -> None:
        issues.append(PreflightIssue(code, msg, "ERROR", fatal=True))

    def warn(code: str, msg: str) -> None:
        issues.append(PreflightIssue(code, msg, "WARNING", fatal=False))

    # Common: disabled/rejected should not reach here, but be defensive.
    if c.disabled:
        err("DISABLED", "constraint is disabled")
        return issues
    if c.status.value in ("REJECTED", "DEPRECATED", "MISSING"):
        err("NOT_ELIGIBLE", f"constraint has status {c.status.value}")
        return issues

    # --------------------------------------------------------------
    # create_clock
    # --------------------------------------------------------------
    if t == ConstraintType.CREATE_CLOCK:
        if "period" not in v or v.get("period") is None:
            err("NO_PERIOD", "create_clock has no period; cannot emit")
        if not _cap(capabilities, "create_clock"):
            err("CAP_CREATE_CLOCK", "backend lacks create_clock capability")
        if not (c.target_objects or c.target_refs or v.get("name")):
            err("NO_TARGET", "create_clock has no target or name")
        if v.get("waveform") and not _cap(capabilities, "waveform"):
            err("CAP_WAVEFORM", "backend does not support -waveform; cannot safely approximate")
        if c.clock_refs_typed and any(
                tr.collection_kind == CollectionKind.EXPR and
                tr.resolution_status.value == "UNRESOLVED"
                for tr in c.clock_refs_typed):
            warn("UNRESOLVED_CLOCK", "some clock references are unresolved")

    # --------------------------------------------------------------
    # create_generated_clock
    # --------------------------------------------------------------
    elif t == ConstraintType.CREATE_GENERATED_CLOCK:
        if not _cap(capabilities, "create_generated_clock"):
            err("CAP_GCLK", "backend lacks create_generated_clock capability")
        if not v.get("name"):
            err("NO_NAME", "create_generated_clock has no -name")
        if not (v.get("master_clock") or v.get("source")):
            warn("NO_MASTER", "create_generated_clock has no master_clock/source")
        if v.get("edges") and len(v.get("edges", [])) < 3:
            warn("EDGES_SHORT", "create_generated_clock -edges expects 3 edges")
        # divide_by and multiply_by are mutually exclusive in SDC.
        if v.get("divide_by") is not None and v.get("multiply_by") is not None:
            err("DIV_MUL_CONFLICT",
                "create_generated_clock cannot have both -divide_by and -multiply_by")
        if v.get("edge_shift") is not None:
            if not v.get("edges"):
                err("EDGE_SHIFT_WITHOUT_EDGES",
                    "-edge_shift requires -edges; cannot emit safely")
            if not _cap(capabilities, "generated_clock_edge_shift"):
                err("CAP_EDGE_SHIFT",
                    "backend does not support -edge_shift; constraint cannot be safely approximated")
        # Generated clock options (duty_cycle, invert, edges, combinations)
        if (v.get("duty_cycle") or v.get("invert") or v.get("edges") or
                v.get("combinational")) and not _cap(capabilities, "generated_clock_options"):
            err("CAP_GCLK_OPT",
                "backend does not support extended create_generated_clock options")

    # --------------------------------------------------------------
    # set_input_delay / set_output_delay
    # --------------------------------------------------------------
    elif t in (ConstraintType.SET_INPUT_DELAY, ConstraintType.SET_OUTPUT_DELAY):
        if v.get("delay") is None:
            err("NO_DELAY", f"{t.value} missing delay value")
        if not v.get("clock") and not c.clock_refs:
            warn("NO_CLOCK", f"{t.value} has no -clock")
        if not c.target_objects and not c.target_refs:
            err("NO_TARGET", f"{t.value} has no target")
        mm = v.get("min_max", "both")
        if mm not in ("min", "max", "both"):
            err("BAD_MIN_MAX", f"{t.value} invalid min_max {mm!r}")
        edge = v.get("edge")
        if edge not in (None, "rise", "fall"):
            err("BAD_EDGE", f"{t.value} invalid edge {edge!r}")
        if (mm != "both" or edge is not None) and not _cap(capabilities, "edge_qualifiers"):
            err("CAP_EDGE_QUAL",
                "backend lacks edge qualifier (-min/-max/-rise/-fall) support")
        if v.get("add_delay") and not _cap(capabilities, "add_delay"):
            warn("CAP_ADD_DELAY", "backend may not support -add_delay")

    # --------------------------------------------------------------
    # set_clock_uncertainty
    # --------------------------------------------------------------
    elif t == ConstraintType.SET_CLOCK_UNCERTAINTY:
        if not _cap(capabilities, "set_clock_uncertainty"):
            err("CAP_UNC", "backend lacks set_clock_uncertainty capability")
        if v.get("uncertainty") is None:
            err("NO_UNC", "set_clock_uncertainty missing value")
        sh = v.get("setup_hold", "both")
        mm = v.get("min_max", "both")
        edge = v.get("edge")
        if (sh != "both" or mm != "both" or edge is not None) and not _cap(capabilities, "edge_qualifiers"):
            err("CAP_EDGE_QUAL",
                "backend lacks edge qualifier support for clock uncertainty")
        ps = c.path_selector
        if ps and (ps.through_set or ps.through_refs) and not _cap(capabilities, "through"):
            err("CAP_THROUGH",
                "backend does not support -through on set_clock_uncertainty")

    # --------------------------------------------------------------
    # set_clock_latency
    # --------------------------------------------------------------
    elif t == ConstraintType.SET_CLOCK_LATENCY:
        if not _cap(capabilities, "set_clock_latency"):
            err("CAP_LAT", "backend lacks set_clock_latency capability")
        if v.get("latency") is None:
            err("NO_LAT", "set_clock_latency missing value")
        sh = v.get("setup_hold", "both")
        mm = v.get("min_max", "both")
        edge = v.get("edge")
        if (sh != "both" or mm != "both" or edge is not None) and not _cap(capabilities, "edge_qualifiers"):
            err("CAP_EDGE_QUAL",
                "backend lacks edge qualifier support for clock latency")

    # --------------------------------------------------------------
    # set_clock_transition
    # --------------------------------------------------------------
    elif t == ConstraintType.SET_CLOCK_TRANSITION:
        if not _cap(capabilities, "set_clock_transition"):
            err("CAP_TRN", "backend lacks set_clock_transition capability")
        if v.get("transition") is None:
            err("NO_TRN", "set_clock_transition missing value")
        sh = v.get("setup_hold", "both")
        mm = v.get("min_max", "both")
        edge = v.get("edge")
        if (sh != "both" or mm != "both" or edge is not None) and not _cap(capabilities, "edge_qualifiers"):
            err("CAP_EDGE_QUAL",
                "backend lacks edge qualifier support for clock transition")

    # --------------------------------------------------------------
    # set_propagated_clock
    # --------------------------------------------------------------
    elif t == ConstraintType.SET_PROPAGATED_CLOCK:
        if not _cap(capabilities, "set_propagated_clock"):
            err("CAP_PROP", "backend lacks set_propagated_clock capability")

    # --------------------------------------------------------------
    # set_clock_groups
    # --------------------------------------------------------------
    elif t == ConstraintType.SET_CLOCK_GROUPS:
        if not _cap(capabilities, "set_clock_groups"):
            err("CAP_GROUPS", "backend lacks set_clock_groups capability")
        groups = v.get("groups", [])
        if len(groups) < 2:
            err("GROUPS_TOO_FEW", "set_clock_groups needs >=2 groups")
        rel = v.get("relationship", "asynchronous")
        if rel not in ("asynchronous", "logically_exclusive", "physically_exclusive"):
            err("BAD_REL", f"set_clock_groups invalid relationship {rel!r}")

    # --------------------------------------------------------------
    # Path exceptions: false_path / multicycle / min_delay / max_delay
    # --------------------------------------------------------------
    elif t in (ConstraintType.SET_FALSE_PATH, ConstraintType.SET_MULTICYCLE_PATH,
               ConstraintType.SET_MIN_DELAY, ConstraintType.SET_MAX_DELAY):
        ps = c.path_selector
        if ps is not None:
            for stage in ps.through_set:
                if not isinstance(stage, list):
                    err("BAD_THROUGH", "through_set stage must be a list")
            if (ps.through_set or ps.through_refs) and not _cap(capabilities, "through"):
                err("CAP_THROUGH", "backend does not support -through")
            sh = ps.setup_hold if ps.setup_hold else "both"
            mm = ps.min_max if ps.min_max else "both"
            if (sh != "both" or mm != "both" or ps.edge is not None) and not _cap(capabilities, "edge_qualifiers"):
                err("CAP_EDGE_QUAL",
                    "backend lacks edge qualifier support for path exceptions")
        if t == ConstraintType.SET_MULTICYCLE_PATH:
            cyc = v.get("cycles")
            if cyc is None or int(cyc) < 1:
                err("BAD_CYCLES", "set_multicycle_path cycles must be >= 1")
        if t in (ConstraintType.SET_MIN_DELAY, ConstraintType.SET_MAX_DELAY):
            cap_key = "set_min_delay" if t == ConstraintType.SET_MIN_DELAY else "set_max_delay"
            if not _cap(capabilities, cap_key):
                err(f"CAP_{cap_key}", f"backend lacks {t.value} capability")
            if v.get("delay") is None:
                err("NO_DELAY", f"{t.value} missing delay value")

    # --------------------------------------------------------------
    # Design-rule constraints
    # --------------------------------------------------------------
    elif t in (ConstraintType.SET_LOAD, ConstraintType.SET_INPUT_TRANSITION,
               ConstraintType.SET_MAX_TRANSITION, ConstraintType.SET_MAX_CAPACITANCE,
               ConstraintType.SET_MAX_FANOUT, ConstraintType.SET_DRIVING_CELL):
        if not _cap(capabilities, "design_rules"):
            err("CAP_DRC",
                f"backend does not support design-rule constraint {t.value}")
        # Accept either "value", a type-specific key (e.g. "transition"),
        # or "lib_cell" for set_driving_cell.
        val = (v.get("value") if v.get("value") is not None else
               v.get("transition") if v.get("transition") is not None else
               v.get("lib_cell") if v.get("lib_cell") is not None else
               v.get("capacitance") if v.get("capacitance") is not None else
               v.get("fanout") if v.get("fanout") is not None else
               None)
        if val is None:
            err("NO_VALUE", f"{t.value} missing value")
        if t == ConstraintType.SET_DRIVING_CELL and not v.get("lib_cell"):
            err("NO_LIB_CELL", "set_driving_cell missing -lib_cell")

    # Validate typed target refs are emittable (no unresolved EXPR).
    for refs, label in ((c.target_refs, "target"), (c.source_refs, "source"),
                        (c.clock_refs_typed, "clock")):
        for tr in refs:
            if tr.resolution_status.value == "UNRESOLVED":
                warn("UNRESOLVED_REF",
                     f"{label} reference {tr.expression or tr.pattern!r} unresolved: {tr.unresolved_reason}")

    return issues
