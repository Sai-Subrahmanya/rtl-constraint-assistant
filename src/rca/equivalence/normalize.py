"""
Constraint normalization for semantic comparison (Step 9 — WP-L).

Normalization converts a UCM :class:`Constraint` into a deterministic,
order-insensitive-where-appropriate semantic identity tuple that:

* stores all timing quantities in canonical SI seconds, so that
  ``10ns``/``10000ps``/``0.00000001s`` compare equal;
* sorts unordered collections (targets, clocks, groups) while preserving
  ordered stage structure (``-through`` stages, generated-clock
  ``edges``, waveform);
* drops presentation-only noise (comment, generated-text caches);
* separates semantic identity from provenance;
* flags unsupported / unresolved options so comparison returns UNKNOWN
  rather than claiming equivalence.

Rules are documented per constraint type in :data:`SEMANTIC_FIELDS`.
"""

from __future__ import annotations

import math
from typing import Any

from ..constraint_model import Constraint, ConstraintSet, PathSelector
from ..utils.enums import (
    CollectionKind,
    ComparisonLevel,
    ConstraintType,
    ImportStatus,
    ResolutionStatus,
)
from ..utils.units import parse_time_string

# Fields that are semantically irrelevant (presentation/provenance) and
# must never enter a semantic identity comparison.
_PRESENTATION_FIELDS = {
    "id", "source_kind", "confidence", "status", "opt_status",
    "generation_confidence", "comment", "provenance", "precedence",
    "generated_text_by_backend", "equivalent_forms",
    "evidence_ids", "assumption_ids", "dependency_ids", "downstream_ids",
    "dependent_analyses", "affected_paths",
}

# Time-valued keys per constraint value dict. Values at these keys are
# converted to seconds if they are numeric or a time-string.
_TIME_VALUE_KEYS = {
    "period", "delay", "uncertainty", "latency", "transition",
    "input_delay", "output_delay", "source_latency", "early_latency",
    "late_latency", "min_delay", "max_delay", "waveform", "edges",
    "edge_shift",
}

# Numeric tolerance (seconds). We use a tight epsilon since values come
# from parse_time_string which preserves float precision.
_EPS = 1e-15


# ---------------------------------------------------------------------------
# Per-type semantic field definitions
# ---------------------------------------------------------------------------
#
# For each constraint type we document which ``values`` keys participate in
# semantic identity, and what collection-order semantics apply.
#
#   "identity"          — primary semantic identifier keys (e.g. clock name)
#   "numeric_time"      — values that must be compared as seconds
#   "ordered"           — keys whose order IS semantically meaningful
#                         (they are NOT sorted across elements)
#   "unordered"         — keys where collection order does not matter
#                         (elements are sorted before comparison)
#   "defaults"          — explicit-default mapping: absence of a key
#                         with this default is semantically equal to
#                         the default being present
#   "unknown_on_extra"  — if True, any values key not in identity/numeric_time
#                         is treated as UNSUPPORTED and forces UNKNOWN
#   "description"       — human-readable rule documentation
#

SEMANTIC_FIELDS: dict[ConstraintType, dict[str, Any]] = {
    ConstraintType.CREATE_CLOCK: {
        "identity": ("name", "source"),
        "numeric_time": ("period", "waveform"),
        "ordered": ("waveform",),
        "unordered": ("targets", "source_objects"),
        "defaults": {"add": False},
        "unknown_on_extra": False,
        "description": "Clock identity (name), period and waveform define "
                       "the clock. Target/source ordering is irrelevant. "
                       "Different clock names are never equivalent.",
    },
    ConstraintType.CREATE_GENERATED_CLOCK: {
        "identity": ("name", "source", "master_clock"),
        "numeric_time": ("waveform", "edge_shift"),
        "ordered": ("edges", "edge_shift", "waveform"),
        "unordered": ("targets",),
        "defaults": {"add": False, "combinational": False,
                     "invert": False, "duty_cycle": None,
                     "divide_by": None, "multiply_by": None},
        "unknown_on_extra": False,
        "description": "Generated-clock equivalence requires matching "
                       "name, source pin, master clock, divisor/multiplier "
                       "and edge/waveform. divide_by=2 is NOT equivalent "
                       "to multiply_by=0.5 because 0.5 is not a legal "
                       "integer multiplier in SDC; conservative UNKNOWN.",
    },
    ConstraintType.SET_INPUT_DELAY: {
        "identity": ("clock",),
        "numeric_time": ("delay",),
        "ordered": (),
        "unordered": ("targets",),
        "defaults": {"min_max": "max", "edge": "both",
                     "add_delay": False, "clock_fall": False},
        "unknown_on_extra": False,
        "description": "I/O delays must match min/max, rise/fall, add_delay "
                       "and associated clock. -min/-max differ even if the "
                       "numeric value is identical.",
    },
    ConstraintType.SET_OUTPUT_DELAY: {
        "identity": ("clock",),
        "numeric_time": ("delay",),
        "ordered": (),
        "unordered": ("targets",),
        "defaults": {"min_max": "max", "edge": "both",
                     "add_delay": False, "clock_fall": False},
        "unknown_on_extra": False,
        "description": "Same as set_input_delay.",
    },
    ConstraintType.SET_CLOCK_UNCERTAINTY: {
        "identity": (),
        "numeric_time": ("uncertainty",),
        "ordered": (),
        "unordered": ("targets", "clocks", "from", "to"),
        "defaults": {"setup": True, "hold": True},
        "unknown_on_extra": False,
        "description": "set_clock_uncertainty applies to a (set of) clocks "
                       "and may be setup/hold qualified.",
    },
    ConstraintType.SET_CLOCK_LATENCY: {
        "identity": (),
        "numeric_time": ("latency", "early_latency", "late_latency",
                         "source_latency"),
        "ordered": (),
        "unordered": ("targets", "clocks"),
        "defaults": {"min_max": "max", "source": False,
                     "network": True, "early": False, "late": False},
        "unknown_on_extra": False,
        "description": "Latency compares -source/-network, -early/-late, "
                       "and the associated clocks.",
    },
    ConstraintType.SET_CLOCK_TRANSITION: {
        "identity": (),
        "numeric_time": ("transition",),
        "ordered": (),
        "unordered": ("targets", "clocks"),
        "defaults": {"min_max": "max", "rise": True, "fall": True},
        "unknown_on_extra": False,
        "description": "Clock transition (slew) compares the slew value, "
                       "min/max qualifier, and clock targets.",
    },
    ConstraintType.SET_PROPAGATED_CLOCK: {
        "identity": (),
        "numeric_time": (),
        "ordered": (),
        "unordered": ("targets", "clocks"),
        "defaults": {},
        "unknown_on_extra": False,
        "description": "set_propagated_clock is a flag-like constraint; "
                       "equivalence is defined by the set of clocks it "
                       "applies to.",
    },
    ConstraintType.SET_CLOCK_GROUPS: {
        "identity": ("relationship",),
        "numeric_time": (),
        # Groups form a PARTITION. Group order is non-semantic, order of
        # clocks *within* a group is non-semantic, but collapsing groups
        # changes the partition and is NOT equivalent.
        "ordered": ("groups",),   # groups are compared as a sorted tuple of sorted tuples
        "unordered": (),
        "defaults": {},
        "unknown_on_extra": False,
        "description": "Clock-group partitions are compared as a sorted "
                       "tuple of sorted clock-sets. {A B} {C} ≡ {C} {A B} "
                       "but {A} {B} {C} is NOT equivalent to {A B} {C}.",
    },
    ConstraintType.SET_FALSE_PATH: {
        "identity": (),
        "numeric_time": (),
        "ordered": (),        # order handled by PathSelector.semantic_key
        "unordered": (),
        "defaults": {"min_max": "both", "setup_hold": "both",
                     "edge": None, "add_delay": False, "reset_path": False},
        "path_selector": True,
        "unknown_on_extra": False,
        "description": "False-path equivalence requires matching -from/-to/"
                       "-through selectors with ordered -through stages, "
                       "plus min/max/setup/hold qualifiers.",
    },
    ConstraintType.SET_MULTICYCLE_PATH: {
        "identity": (),
        "numeric_time": (),
        "ordered": (),
        "unordered": (),
        "defaults": {"cycles": 1, "min_max": "max", "setup_hold": "setup",
                     "start": False, "end": True},
        "path_selector": True,
        "unknown_on_extra": False,
        "description": "Multicycle equivalence requires matching cycle count, "
                       "setup/hold qualifier, and start/end selector semantics.",
    },
    ConstraintType.SET_MIN_DELAY: {
        "identity": (),
        "numeric_time": ("delay",),
        "ordered": (),
        "unordered": (),
        "defaults": {},
        "path_selector": True,
        "unknown_on_extra": False,
        "description": "set_min_delay compares the delay value and the full "
                       "path selector.",
    },
    ConstraintType.SET_MAX_DELAY: {
        "identity": (),
        "numeric_time": ("delay",),
        "ordered": (),
        "unordered": (),
        "defaults": {},
        "path_selector": True,
        "unknown_on_extra": False,
        "description": "set_max_delay compares the delay value and the full "
                       "path selector.",
    },
}

# Constraints not listed in SEMANTIC_FIELDS fall back to a conservative
# "full-values" comparison but are still flagged UNKNOWN if any values
# cannot be normalized; this covers set_load/set_driving_cell/set_input_
# transition/set_max_* etc. without inventing equivalence rules we
# haven't documented.
_FALLBACK_UNKNOWN = {
    ConstraintType.SET_DRIVING_CELL,
    ConstraintType.SET_INPUT_TRANSITION,
    ConstraintType.SET_LOAD,
    ConstraintType.SET_MAX_TRANSITION,
    ConstraintType.SET_MAX_CAPACITANCE,
    ConstraintType.SET_MAX_FANOUT,
    ConstraintType.SET_CASE_ANALYSIS,
    ConstraintType.SET_DISABLE_TIMING,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm_time(v: Any) -> float | str | None:
    """Return v in canonical seconds, or None if it cannot be normalized."""
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return round(float(v), 15)
    if isinstance(v, str):
        try:
            return round(parse_time_string(v), 15)
        except Exception:
            return None
    return None


def _time_eq(a: Any, b: Any) -> bool:
    """True if two timing quantities are semantically equal (seconds)."""
    na = _norm_time(a)
    nb = _norm_time(b)
    if na is None or nb is None:
        return False
    return math.isclose(na, nb, rel_tol=0.0, abs_tol=_EPS)


def _sorted_set(items: Any) -> tuple:
    """Sort a collection into a tuple (unordered normalization)."""
    if items is None:
        return ()
    out = list(items)
    return tuple(sorted(out, key=_sort_key))


def _sort_key(x: Any) -> str:
    return repr(x)


def _frozen(v: Any) -> Any:
    """Recursively convert lists/dicts to deterministic tuples."""
    if isinstance(v, dict):
        return tuple(sorted(((str(k), _frozen(val)) for k, val in v.items()),
                            key=lambda kv: kv[0]))
    if isinstance(v, (list, tuple)):
        return tuple(_frozen(x) for x in v)
    if isinstance(v, float):
        return round(v, 15)
    return v


def _normalize_targets(c: Constraint, unordered_keys: tuple[str, ...]) -> tuple:
    items: list[str] = []
    items.extend(c.target_objects)
    for k in unordered_keys:
        if k == "source_objects":
            items.extend(c.source_objects)
    return _sorted_set(items)


def _clocks(c: Constraint) -> tuple:
    return _sorted_set(c.clock_refs)


def _selector_key(c: Constraint) -> tuple:
    if c.path_selector is None:
        return ()
    # PathSelector.semantic_key() already sorts within unordered sets while
    # preserving ordered through stages.
    return c.path_selector.semantic_key()


def _normalize_waveform(wf: Any) -> tuple:
    """Waveform is an ordered list of times; each edge compared in seconds."""
    if wf is None:
        return ()
    out = []
    try:
        for v in wf:
            t = _norm_time(v)
            if t is None:
                return ("__UNKNOWN__", repr(wf))
            out.append(t)
    except Exception:
        return ("__UNKNOWN__", repr(wf))
    return tuple(out)


def _normalize_groups(groups: Any) -> tuple:
    """Clock-group partition: sort clocks within each group, then sort
    groups so group order is irrelevant."""
    if not groups:
        return ()
    norm = [tuple(sorted(g)) for g in groups]
    return tuple(sorted(norm))


def _normalize_edges(edges: Any) -> tuple:
    """Generated-clock -edges are ordered triples of integers."""
    if not edges:
        return ()
    return tuple(int(x) for x in edges)


def _normalize_edge_shift(es: Any) -> tuple:
    if not es:
        return ()
    out = []
    for v in es:
        t = _norm_time(v)
        if t is None:
            return ("__UNKNOWN__", repr(es))
        out.append(t)
    return tuple(out)


# ---------------------------------------------------------------------------
# Per-constraint normalization
# ---------------------------------------------------------------------------

def normalize_constraint(c: Constraint) -> tuple:
    """Return a deterministic tuple representing c's semantic identity.

    The tuple is suitable for set-membership/equality checks but does
    not carry provenance, comments, or IDs; use
    :func:`semantic_field_diff` for field-level difference explanation.
    """
    t = c.type
    v = dict(c.values or {})
    rules = SEMANTIC_FIELDS.get(t)
    # Head of the tuple is always the type tag.
    head: list[Any] = [t.value]

    # Scenario key: semantic identity is scenario-scoped.
    scenarios = _sorted_set(c.scenario_ids)
    head.append(("scenarios", scenarios))
    head.append(("disabled", bool(c.disabled)))

    if rules is None:
        # Fallback: treat whole-values + targets conservatively and
        # mark with a "__FALLBACK__" sentinel so callers don't mistake
        # this for a documented equivalence.
        if t in _FALLBACK_UNKNOWN:
            head.append(("__UNKNOWN__", "unsupported_type",
                         _frozen(v), _sorted_set(c.target_objects),
                         _clocks(c)))
            return tuple(head)
        head.append(("__FALLBACK__", _frozen(v),
                     _sorted_set(c.target_objects), _clocks(c)))
        return tuple(head)

    # Identity keys
    for k in rules.get("identity", ()):
        head.append((f"id:{k}", v.get(k)))

    # Numeric-time keys
    for k in rules.get("numeric_time", ()):
        if k == "waveform":
            head.append(("time:waveform", _normalize_waveform(v.get("waveform"))))
        elif k in ("edges",):
            head.append(("edges", _normalize_edges(v.get("edges"))))
        elif k in ("edge_shift",):
            head.append(("edge_shift", _normalize_edge_shift(v.get("edge_shift"))))
        else:
            tv = _norm_time(v.get(k))
            head.append((f"time:{k}", tv))

    # Boolean / scalar defaults
    for k, dv in rules.get("defaults", {}).items():
        if k in rules.get("numeric_time", ()):
            continue
        if k in rules.get("ordered", ()) or k in rules.get("unordered", ()):
            continue
        val = v.get(k, dv)
        head.append((f"scalar:{k}", val))

    # Unordered target collections
    if rules.get("path_selector"):
        head.append(("selector", _selector_key(c)))
    else:
        # Targets / clocks are unordered sets
        head.append(("targets", _sorted_set(c.target_objects)))
        head.append(("clocks", _clocks(c)))
        if "source_objects" in rules.get("unordered", ()):
            head.append(("sources", _sorted_set(c.source_objects)))

    # Groups partition
    if "groups" in rules.get("ordered", ()):
        head.append(("groups", _normalize_groups(v.get("groups"))))

    # Divide/multiply — integer equality only, NO cross-conversion
    if t == ConstraintType.CREATE_GENERATED_CLOCK:
        head.append(("divide_by", v.get("divide_by")))
        head.append(("multiply_by", v.get("multiply_by")))
        head.append(("duty_cycle", v.get("duty_cycle")))
        head.append(("invert", bool(v.get("invert", False))))
        head.append(("edges", _normalize_edges(v.get("edges"))))
        head.append(("edge_shift", _normalize_edge_shift(v.get("edge_shift"))))
        head.append(("combinational", bool(v.get("combinational", False))))

    # Cycles for multicycle
    if t == ConstraintType.SET_MULTICYCLE_PATH:
        head.append(("cycles", int(v.get("cycles", 1))))
        head.append(("setup_hold", v.get("setup_hold", "setup")))
        head.append(("start", bool(v.get("start", False))))
        head.append(("end", bool(v.get("end", True))))

    # I/O delay edge/min-max/add_delay
    if t in (ConstraintType.SET_INPUT_DELAY, ConstraintType.SET_OUTPUT_DELAY):
        head.append(("edge", v.get("edge", "both")))
        head.append(("min_max", v.get("min_max", "max")))
        head.append(("add_delay", bool(v.get("add_delay", False))))
        head.append(("clock_fall", bool(v.get("clock_fall", False))))

    # clock uncertainty/latency/transition qualifiers
    if t == ConstraintType.SET_CLOCK_UNCERTAINTY:
        head.append(("setup", bool(v.get("setup", True))))
        head.append(("hold", bool(v.get("hold", True))))
        head.append(("from", _sorted_set(v.get("from"))))
        head.append(("to", _sorted_set(v.get("to"))))
    if t == ConstraintType.SET_CLOCK_LATENCY:
        head.append(("source", bool(v.get("source", False))))
        head.append(("min_max", v.get("min_max", "max")))
        head.append(("early", bool(v.get("early", False))))
        head.append(("late", bool(v.get("late", False))))
    if t == ConstraintType.SET_CLOCK_TRANSITION:
        head.append(("min_max", v.get("min_max", "max")))
        head.append(("rise", bool(v.get("rise", True))))
        head.append(("fall", bool(v.get("fall", True))))

    return tuple(head)


def semantic_match_key(c: Constraint) -> tuple:
    """Deterministic key for pairing constraints across sides.

    Groups by (type, scenario, identity_fields) so that unrelated
    same-type constraints are not accidentally paired.
    """
    t = c.type
    v = dict(c.values or {})
    rules = SEMANTIC_FIELDS.get(t) or {}
    key_parts = [t.value]
    key_parts.append(_sorted_set(c.scenario_ids))
    for k in rules.get("identity", ()):
        key_parts.append((k, v.get(k)))
    # Include primary targets/clocks in the match key to avoid pairing
    # unrelated same-type constraints (e.g. set_input_delay on port A
    # vs port B).
    if not rules.get("path_selector"):
        key_parts.append(_sorted_set(c.target_objects))
        key_parts.append(_clocks(c))
    else:
        # For path-selector constraints, key is (type, scenario, from, to)
        if c.path_selector:
            key_parts.append(("from", _sorted_set(c.path_selector.from_set)))
            key_parts.append(("to", _sorted_set(c.path_selector.to_set)))
        else:
            key_parts.append(()); key_parts.append(())
    return tuple(key_parts)


def constraint_signature_set(cset: ConstraintSet) -> set[tuple]:
    """Return a set of normalized constraint signatures (order-independent)."""
    return {normalize_constraint(c) for c in cset if not c.disabled}


def has_unsupported_options(c: Constraint) -> list[str]:
    """Return list of reasons why c's semantics are UNKNOWN, or []."""
    reasons: list[str] = []
    t = c.type
    if t in _FALLBACK_UNKNOWN:
        reasons.append(f"constraint type {t.value} not modeled for semantic compare")
    # Examine target resolutions — if ANY typed ref is UNRESOLVED we
    # cannot prove equivalence because the collection may refer to a
    # different set of objects.
    for ref_list_name in ("target_refs", "source_refs", "clock_refs_typed"):
        for r in getattr(c, ref_list_name, []) or []:
            if r.collection_kind == CollectionKind.UNRESOLVED \
                    or r.collection_kind == CollectionKind.EXPR:
                reasons.append(f"unresolved {ref_list_name}: {r.pattern if hasattr(r,'pattern') else r}")
    for stage in (c.through_refs or []):
        for r in stage:
            if r.collection_kind in (CollectionKind.UNRESOLVED, CollectionKind.EXPR):
                reasons.append(f"unresolved through ref: {getattr(r,'pattern', r)}")
    # provenance / import status may mark things as PARTIAL
    try:
        prov = c.provenance
        if prov is not None and getattr(prov, "import_status", None) == ImportStatus.PARTIAL:
            reasons.append("import marked PARTIAL: unsupported option present")
        if prov is not None and getattr(prov, "import_status", None) == ImportStatus.UNRESOLVED:
            reasons.append("import marked UNRESOLVED")
    except Exception:
        pass
    return reasons


def field_level_diff(a: Constraint, b: Constraint) -> list[dict[str, Any]]:
    """Produce a list of per-field difference dicts between two constraints
    that share the same semantic_match_key but differ semantically."""
    diffs: list[dict[str, Any]] = []
    va = dict(a.values or {})
    vb = dict(b.values or {})
    rules = SEMANTIC_FIELDS.get(a.type) or {}

    def _record(field: str, va_v: Any, vb_v: Any, explanation: str) -> None:
        diffs.append({
            "field": field,
            "value_a": va_v,
            "value_b": vb_v,
            "explanation": explanation,
        })

    # Time-valued fields
    for k in rules.get("numeric_time", ()):
        if k == "waveform":
            wa = _normalize_waveform(va.get("waveform"))
            wb = _normalize_waveform(vb.get("waveform"))
            if wa != wb:
                _record("waveform", va.get("waveform"), vb.get("waveform"),
                        "waveform edges differ semantically (seconds).")
        else:
            xa = _norm_time(va.get(k))
            xb = _norm_time(vb.get(k))
            if xa != xb:
                _record(k, va.get(k), vb.get(k),
                        f"numeric timing value '{k}' differs after unit normalization.")

    # Scalar fields
    for k in ("name", "source", "master_clock", "clock", "relationship",
              "min_max", "edge", "setup_hold", "add_delay", "clock_fall",
              "divide_by", "multiply_by", "duty_cycle", "invert",
              "combinational", "cycles", "add", "reset_path", "rise", "fall",
              "setup", "hold", "start", "end", "source_latency_flag"):
        if k == "name":
            # Clock name / constraint identity
            if va.get("name") != vb.get("name"):
                _record("name", va.get("name"), vb.get("name"),
                        "clock/constraint identity differs; not equivalent.")
        elif k in va or k in vb:
            if va.get(k) != vb.get(k):
                _record(k, va.get(k), vb.get(k),
                        f"field '{k}' differs.")

    # Generated clock integer divisor/multiplier strict comparison
    if a.type == ConstraintType.CREATE_GENERATED_CLOCK:
        for k in ("divide_by", "multiply_by"):
            if va.get(k) != vb.get(k):
                _record(k, va.get(k), vb.get(k),
                        f"{k} must match exactly; no cross-conversion.")
        if _normalize_edges(va.get("edges")) != _normalize_edges(vb.get("edges")):
            _record("edges", va.get("edges"), vb.get("edges"),
                    "generated-clock -edges are ordered triples; differ.")
        if _normalize_edge_shift(va.get("edge_shift")) != _normalize_edge_shift(vb.get("edge_shift")):
            _record("edge_shift", va.get("edge_shift"), vb.get("edge_shift"),
                    "generated-clock -edge_shift differ.")

    # Clock-group partition diff
    if a.type == ConstraintType.SET_CLOCK_GROUPS:
        ga = _normalize_groups(va.get("groups"))
        gb = _normalize_groups(vb.get("groups"))
        if ga != gb:
            _record("groups", va.get("groups"), vb.get("groups"),
                    "clock-group partition differs (groupings are not the same).")

    # Path selector diff
    if rules.get("path_selector"):
        sa = _selector_key(a)
        sb = _selector_key(b)
        if sa != sb:
            _record("path_selector",
                    a.path_selector.to_dict() if a.path_selector else None,
                    b.path_selector.to_dict() if b.path_selector else None,
                    "from/to/through selectors differ (order preserved for through stages).")

    # Targets/clock sets
    if not rules.get("path_selector"):
        if _sorted_set(a.target_objects) != _sorted_set(b.target_objects):
            _record("targets", sorted(a.target_objects), sorted(b.target_objects),
                    "target object sets differ.")
        if _clocks(a) != _clocks(b):
            _record("clocks", sorted(a.clock_refs), sorted(b.clock_refs),
                    "referenced clock sets differ.")

    # Scenarios
    if _sorted_set(a.scenario_ids) != _sorted_set(b.scenario_ids):
        _record("scenarios", sorted(a.scenario_ids), sorted(b.scenario_ids),
                "scenario applicability differs.")

    return diffs
