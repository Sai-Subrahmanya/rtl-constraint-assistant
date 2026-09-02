"""Core deterministic SDC renderer (Step 6).

Shared rendering engine used by every backend. Backends subclass
:class:`SdcRenderer` to declare capabilities, tune defaults, and
add vendor-specific headers.
"""

from __future__ import annotations

import io
from typing import Any

from ...constraint_model import Constraint, ConstraintSet, PathSelector
from ...constraint_model.targets import (
    CollectionKind, TargetRef, targets_from_strings,
)
from ...utils.enums import (
    ConstraintStatus, ConstraintType, SafeMode,
)
from ...utils.hashing import stable_hash
from .preflight import preflight_constraint
from .result import GenerationDiagnostic, GenerationStatus, SdcGenerationResult
from .tcl_quote import format_ns, tcl_quote, tcl_quote_list


# Canonical emission order (Step 6 §16).
EMISSION_ORDER: dict[ConstraintType, int] = {
    ConstraintType.CREATE_CLOCK: 1,
    ConstraintType.CREATE_GENERATED_CLOCK: 2,
    ConstraintType.SET_CLOCK_UNCERTAINTY: 3,
    ConstraintType.SET_CLOCK_LATENCY: 4,
    ConstraintType.SET_CLOCK_TRANSITION: 5,
    ConstraintType.SET_PROPAGATED_CLOCK: 6,
    ConstraintType.SET_INPUT_DELAY: 7,
    ConstraintType.SET_OUTPUT_DELAY: 8,
    ConstraintType.SET_DRIVING_CELL: 9,
    ConstraintType.SET_INPUT_TRANSITION: 10,
    ConstraintType.SET_LOAD: 11,
    ConstraintType.SET_MAX_TRANSITION: 12,
    ConstraintType.SET_MAX_CAPACITANCE: 13,
    ConstraintType.SET_MAX_FANOUT: 14,
    ConstraintType.SET_CLOCK_GROUPS: 15,
    ConstraintType.SET_FALSE_PATH: 16,
    ConstraintType.SET_MULTICYCLE_PATH: 17,
    ConstraintType.SET_MIN_DELAY: 18,
    ConstraintType.SET_MAX_DELAY: 19,
}


# ---------------------------------------------------------------------------
# Target rendering
# ---------------------------------------------------------------------------

# Default collection kind for plain-string target/clock/through names when
# the constraint predates typed target_refs. Inference/import should always
# attach typed refs, but we need a safe fallback for legacy tests/cases.
_DEFAULT_TARGET_KIND = {
    ConstraintType.SET_INPUT_DELAY: CollectionKind.PORT,
    ConstraintType.SET_OUTPUT_DELAY: CollectionKind.PORT,
    ConstraintType.CREATE_CLOCK: CollectionKind.PORT,
    ConstraintType.CREATE_GENERATED_CLOCK: CollectionKind.PIN,
    ConstraintType.SET_CLOCK_UNCERTAINTY: CollectionKind.CLOCK,
    ConstraintType.SET_CLOCK_LATENCY: CollectionKind.PIN,
    ConstraintType.SET_CLOCK_TRANSITION: CollectionKind.CLOCK,
    ConstraintType.SET_PROPAGATED_CLOCK: CollectionKind.CLOCK,
    ConstraintType.SET_LOAD: CollectionKind.PORT,
    ConstraintType.SET_INPUT_TRANSITION: CollectionKind.PORT,
    ConstraintType.SET_MAX_TRANSITION: CollectionKind.PORT,
    ConstraintType.SET_MAX_CAPACITANCE: CollectionKind.PORT,
    ConstraintType.SET_MAX_FANOUT: CollectionKind.PORT,
    ConstraintType.SET_DRIVING_CELL: CollectionKind.PORT,
}

_GET_CMD = {
    CollectionKind.PORT: "get_ports",
    CollectionKind.PIN: "get_pins",
    CollectionKind.NET: "get_nets",
    CollectionKind.CELL: "get_cells",
    CollectionKind.CLOCK: "get_clocks",
    CollectionKind.REGISTER: "get_pins",  # registers represented by Q pin
    CollectionKind.LITERAL: None,
    CollectionKind.EXPR: None,
}

_ALL_CMD = {
    CollectionKind.ALL_INPUTS: "all_inputs",
    CollectionKind.ALL_OUTPUTS: "all_outputs",
    CollectionKind.ALL_CLOCKS: "all_clocks",
    CollectionKind.ALL_REGISTERS: "all_registers",
}


def render_target(ref: TargetRef) -> str | None:
    """Render a :class:`TargetRef` as an SDC selector expression."""
    k = ref.collection_kind
    if k in _ALL_CMD:
        return f"[{_ALL_CMD[k]}]"
    if k == CollectionKind.LITERAL:
        names = ref.names()
        if not names:
            return None
        return tcl_quote(names[0]) if len(names) == 1 else tcl_quote_list(names)
    get = _GET_CMD.get(k)
    if get is None:
        return None
    names = ref.names()
    if not names:
        return None
    if ref.is_multi_object():
        return f"[{get} {tcl_quote_list(names)}]"
    return f"[{get} {tcl_quote(names[0])}]"


def render_target_list(refs: list[TargetRef], fallback_names: list[str],
                       default_kind: CollectionKind) -> list[str]:
    out: list[str] = []
    if refs:
        for r in refs:
            sel = render_target(r)
            if sel is not None:
                out.append(sel)
    elif fallback_names:
        refs2 = _infer_refs(fallback_names, default_kind)
        for r in refs2:
            sel = render_target(r)
            if sel is not None:
                out.append(sel)
    return out


def _infer_refs(names: list[str], default_kind: CollectionKind) -> list[TargetRef]:
    """Fallback: build typed refs for plain names using a default kind.

    This is used ONLY for legacy string-only ConstraintSets (pre-dating
    typed refs). The default kind is determined by the constraint type,
    never by name syntax.
    """
    refs: list[TargetRef] = []
    for n in names:
        if not n:
            continue
        if default_kind == CollectionKind.CLOCK:
            refs.append(TargetRef.clock(n))
        elif default_kind == CollectionKind.PORT:
            refs.append(TargetRef.port(n))
        elif default_kind == CollectionKind.PIN:
            refs.append(TargetRef.pin(n))
        elif default_kind == CollectionKind.NET:
            refs.append(TargetRef.net(n))
        elif default_kind == CollectionKind.CELL:
            refs.append(TargetRef.cell(n))
        elif default_kind == CollectionKind.REGISTER:
            refs.append(TargetRef.register(n))
        else:
            refs.append(TargetRef.literal(n))
    return refs


def render_clock_name(refs: list[TargetRef], fallback_names: list[str]) -> str | None:
    """Render a clock *name* for ``-clock <name>`` (bare, not [get_clocks])."""
    if refs:
        for r in refs:
            names = r.names()
            if names:
                return tcl_quote(names[0])
    if fallback_names:
        return tcl_quote(fallback_names[0])
    return None


def render_collection_arg(names: list[str], kind: CollectionKind) -> str | None:
    """Render a list of names as ``[get_* { ... }]``/``[all_*]``/``{...}``.

    Single names become ``[get_* name]``; multiple names become
    ``[get_* { a b }]``; empty -> None.
    """
    if not names:
        return None
    if kind in _ALL_CMD:
        return f"[{_ALL_CMD[kind]}]"
    if kind == CollectionKind.LITERAL:
        return tcl_quote_list(names)
    get = _GET_CMD.get(kind)
    if get is None:
        return None
    if len(names) == 1:
        return f"[{get} {tcl_quote(names[0])}]"
    return f"[{get} {tcl_quote_list(names)}]"


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class SdcRenderer:
    """Deterministic SDC renderer."""

    name: str = "generic"
    file_extension: str = ".sdc"

    def capabilities(self) -> dict[str, bool]:
        return {
            "create_clock": True,
            "create_generated_clock": True,
            "set_input_delay": True,
            "set_output_delay": True,
            "set_clock_uncertainty": True,
            "set_clock_latency": True,
            "set_clock_transition": True,
            "set_propagated_clock": True,
            "set_clock_groups": True,
            "set_false_path": True,
            "set_multicycle_path": True,
            "set_min_delay": True,
            "set_max_delay": True,
            "set_load": True,
            "set_input_transition": True,
            "set_max_transition": True,
            "set_max_capacitance": True,
            "set_max_fanout": True,
            "set_driving_cell": True,
            "design_rules": True,
            "mcmm": False,
            "waveform": True,
            "edge_qualifiers": True,
            "through": True,
            "add_delay": True,
            "generated_clock_options": True,
            "generated_clock_edge_shift": True,
        }

    def header_lines(self, design_name: str, mode: SafeMode) -> list[str]:
        return [
            f"# SDC generated by RTL Constraint Assistant ({self.name} backend)",
            f"# Design: {design_name}",
            f"# Safe mode: {mode.value}",
            "# All time values in nanoseconds",
            "",
        ]

    def footer_lines(self) -> list[str]:
        return []

    def render(self, cset: ConstraintSet, design_name: str = "top",
               mode: SafeMode | str = SafeMode.BALANCED,
               with_provenance: bool = True) -> SdcGenerationResult:
        if isinstance(mode, str):
            mode = SafeMode(mode)
        result = SdcGenerationResult(
            backend=self.name, safe_mode=mode.value, design_name=design_name,
            capabilities=self.capabilities(),
        )
        caps = result.capabilities
        buf = io.StringIO()
        for line in self.header_lines(design_name, mode):
            buf.write(line + "\n")

        eligible = list(cset.emittable(mode))
        ordered = sorted(eligible,
                         key=lambda c: (EMISSION_ORDER.get(c.type, 99), c.id))
        emitted_any = False
        blocked_count = 0

        for c in ordered:
            issues = preflight_constraint(c, caps)
            fatal = [i for i in issues if i.fatal]
            if fatal:
                for i in issues:
                    result.add(GenerationDiagnostic(
                        severity=i.severity, code=i.code, message=i.message,
                        constraint_id=c.id, constraint_type=c.type.value))
                result.skipped_constraint_ids.append(c.id)
                blocked_count += 1
                continue
            for i in issues:
                result.add(GenerationDiagnostic(
                    severity=i.severity, code=i.code, message=i.message,
                    constraint_id=c.id, constraint_type=c.type.value))
            if with_provenance:
                for cl in self._provenance_comment(c):
                    buf.write(cl + "\n")
            try:
                line = self._render_constraint(c, caps, result)
            except Exception as e:  # pragma: no cover
                result.add(GenerationDiagnostic(
                    severity="ERROR", code="RENDER_EXCEPTION",
                    message=f"{type(e).__name__}: {e}",
                    constraint_id=c.id, constraint_type=c.type.value))
                result.skipped_constraint_ids.append(c.id)
                blocked_count += 1
                continue
            if line is None:
                result.skipped_constraint_ids.append(c.id)
                blocked_count += 1
                result.add(GenerationDiagnostic(
                    severity="WARNING", code="NOT_EMITTED",
                    message=f"constraint {c.type.value} could not be rendered",
                    constraint_id=c.id, constraint_type=c.type.value))
                continue
            buf.write(line + "\n\n")
            result.emitted_constraint_ids.append(c.id)
            emitted_any = True

        for line in self.footer_lines():
            buf.write(line + "\n")

        result.text = buf.getvalue()
        if blocked_count and emitted_any:
            result.status = GenerationStatus.PARTIAL
        elif blocked_count and not emitted_any:
            result.status = GenerationStatus.BLOCKED
        elif result.errors():
            result.status = GenerationStatus.ERROR
        else:
            result.status = GenerationStatus.COMPLETE
        result.stats = {
            "total": len(cset),
            "eligible": len(eligible),
            "emitted": len(result.emitted_constraint_ids),
            "skipped": len(result.skipped_constraint_ids),
        }
        result.semantic_hash = self._semantic_hash(cset)
        return result

    # ---- helpers --------------------------------------------------
    def _semantic_hash(self, cset: ConstraintSet) -> str:
        return stable_hash([c.semantic_key() for c in
                            sorted(cset, key=lambda c: c.id)])[:12]

    def _provenance_comment(self, c: Constraint) -> list[str]:
        lines = [f"# [{c.id}] {c.type.value}  source={c.source_kind.value}  "
                 f"conf={c.confidence.value}  status={c.status.value}"]
        rule_id = getattr(c, "rule_id", None)
        if rule_id:
            lines.append(f"#   rule={rule_id}")
        if c.comment:
            for piece in str(c.comment).splitlines():
                lines.append(f"#   {piece}")
        return lines

    # ---- per-constraint dispatch ---------------------------------
    def _render_constraint(self, c: Constraint, caps: dict[str, bool],
                           res: SdcGenerationResult) -> str | None:
        t = c.type
        v = dict(c.values)
        if t == ConstraintType.CREATE_CLOCK:
            return self._create_clock(c, v)
        if t == ConstraintType.CREATE_GENERATED_CLOCK:
            return self._generated_clock(c, v, caps, res)
        if t == ConstraintType.SET_INPUT_DELAY:
            return self._io_delay("set_input_delay", c, v,
                                  default_target_kind=CollectionKind.PORT)
        if t == ConstraintType.SET_OUTPUT_DELAY:
            return self._io_delay("set_output_delay", c, v,
                                  default_target_kind=CollectionKind.PORT)
        if t == ConstraintType.SET_CLOCK_UNCERTAINTY:
            return self._clock_uncertainty(c, v)
        if t == ConstraintType.SET_CLOCK_LATENCY:
            return self._clock_latency(c, v)
        if t == ConstraintType.SET_CLOCK_TRANSITION:
            return self._clock_transition(c, v)
        if t == ConstraintType.SET_PROPAGATED_CLOCK:
            return self._propagated_clock(c, v)
        if t == ConstraintType.SET_CLOCK_GROUPS:
            return self._clock_groups(c, v)
        if t == ConstraintType.SET_FALSE_PATH:
            return self._path_exception("set_false_path", c, v, has_value=False,
                                        from_kind=CollectionKind.CLOCK,
                                        to_kind=CollectionKind.CLOCK,
                                        thr_kind=CollectionKind.PIN)
        if t == ConstraintType.SET_MULTICYCLE_PATH:
            return self._multicycle(c, v)
        if t == ConstraintType.SET_MIN_DELAY:
            return self._path_exception("set_min_delay", c, v, has_value=True,
                                        value_key="delay",
                                        from_kind=CollectionKind.CLOCK,
                                        to_kind=CollectionKind.PORT,
                                        thr_kind=CollectionKind.PIN)
        if t == ConstraintType.SET_MAX_DELAY:
            return self._path_exception("set_max_delay", c, v, has_value=True,
                                        value_key="delay",
                                        from_kind=CollectionKind.CLOCK,
                                        to_kind=CollectionKind.PORT,
                                        thr_kind=CollectionKind.PIN)
        if t == ConstraintType.SET_LOAD:
            return self._drc("set_load", c, v, val_key="value",
                             default_kind=CollectionKind.PORT,
                             value_is_time=False)
        if t == ConstraintType.SET_INPUT_TRANSITION:
            vk = "transition" if "transition" in v else "value"
            return self._drc("set_input_transition", c, v, val_key=vk,
                             default_kind=CollectionKind.PORT,
                             value_is_time=True)
        if t == ConstraintType.SET_MAX_TRANSITION:
            vk = "transition" if "transition" in v else "value"
            return self._drc("set_max_transition", c, v, val_key=vk,
                             default_kind=CollectionKind.PORT,
                             value_is_time=True)
        if t == ConstraintType.SET_MAX_CAPACITANCE:
            return self._drc("set_max_capacitance", c, v, val_key="value",
                             default_kind=CollectionKind.PORT)
        if t == ConstraintType.SET_MAX_FANOUT:
            return self._drc("set_max_fanout", c, v, val_key="value",
                             default_kind=CollectionKind.PORT)
        if t == ConstraintType.SET_DRIVING_CELL:
            lib = v.get("lib_cell")
            if not lib:
                res.add(GenerationDiagnostic(
                    severity="WARNING", code="MISSING_FIELD",
                    message="set_driving_cell missing lib_cell",
                    constraint_id=c.id, constraint_type=t.value))
                return None
            targets = render_target_list(
                c.target_refs, c.target_objects, CollectionKind.PORT)
            if not targets:
                return None
            return f"set_driving_cell -lib_cell {tcl_quote(lib)} {' '.join(targets)}"
        if v.get("_passthrough"):
            return v.get("_original", "")
        res.add(GenerationDiagnostic(
            severity="WARNING", code="UNSUPPORTED_TYPE",
            message=f"no renderer for {t.value}",
            constraint_id=c.id, constraint_type=t.value))
        return None

    # ---------- create_clock ---------------------------------------
    def _create_clock(self, c: Constraint, v: dict) -> str | None:
        period = v.get("period")
        if period is None:
            return None
        name = v.get("name") or (c.target_objects[0] if c.target_objects else c.id)
        parts = [f"create_clock -name {tcl_quote(name)}",
                 f"-period {format_ns(period)}"]
        wf = v.get("waveform")
        if wf and len(wf) >= 2 and v.get("_suppress_waveform") is not True:
            parts.append(f"-waveform {{ {format_ns(wf[0])} {format_ns(wf[1])} }}")
        if v.get("add"):
            parts.append("-add")
        targets = render_target_list(c.target_refs, c.target_objects,
                                     CollectionKind.PORT)
        if not targets:
            targets = [f"[get_ports {tcl_quote(name)}]"]
        parts.append(" ".join(targets))
        return " ".join(parts)

    # ---------- create_generated_clock -----------------------------
    def _generated_clock(self, c: Constraint, v: dict, caps,
                         res: SdcGenerationResult) -> str | None:
        if not caps.get("create_generated_clock", False):
            res.add(GenerationDiagnostic(
                severity="ERROR", code="UNSUPPORTED_CAPABILITY",
                message=f"backend '{self.name}' does not support create_generated_clock",
                constraint_id=c.id,
                constraint_type=ConstraintType.CREATE_GENERATED_CLOCK.value))
            return None
        name = v.get("name")
        if not name:
            res.add(GenerationDiagnostic(
                severity="ERROR", code="NO_NAME",
                message="create_generated_clock has no -name",
                constraint_id=c.id,
                constraint_type=ConstraintType.CREATE_GENERATED_CLOCK.value))
            return None
        parts = [f"create_generated_clock -name {tcl_quote(name)}"]
        # -source is a pin; -master_clock is a clock name
        source = v.get("source")
        if c.source_refs:
            source_sel = " ".join(s for s in (render_target(r) for r in c.source_refs) if s)
        elif source:
            source_sel = f"[get_pins {tcl_quote(source)}]"
        else:
            source_sel = None
        if source_sel:
            parts.append(f"-source {source_sel}")
        master = v.get("master_clock")
        if master:
            parts.append(f"-master_clock {tcl_quote(master)}")
        # Step 6 corrective pass: divide_by and multiply_by are emitted
        # independently (they are mutually exclusive semantically in SDC,
        # but the UCM may carry one or the other; we never silently drop
        # a set value. Co-occurrence is flagged in preflight).
        if v.get("divide_by") is not None:
            parts.append(f"-divide_by {int(v['divide_by'])}")
        if v.get("multiply_by") is not None:
            parts.append(f"-multiply_by {int(v['multiply_by'])}")
        if v.get("duty_cycle") is not None:
            parts.append(f"-duty_cycle {float(v['duty_cycle'])}")
        if v.get("invert"):
            parts.append("-invert")
        if v.get("combinational"):
            parts.append("-combinational")
        edges = v.get("edges")
        edge_shift_emitted = False
        if edges and len(edges) >= 3:
            parts.append("-edges { " + " ".join(str(int(e)) for e in edges[:3]) + " }")
            es = v.get("edge_shift")
            if es and len(es) >= 3:
                if not caps.get("generated_clock_edge_shift", True):
                    # Step 6 corrective pass: do NOT emit -edge_shift on a
                    # backend that declares it unsupported; record ERROR
                    # diagnostic and skip the constraint (blocked) because
                    # omitting edge_shift materially changes the generated
                    # clock edges' timing.
                    res.add(GenerationDiagnostic(
                        severity="ERROR",
                        code="UNSUPPORTED_OPTION",
                        message=(f"create_generated_clock requires -edge_shift which "
                                 f"backend '{self.name}' does not support; "
                                 f"constraint cannot be safely approximated."),
                        constraint_id=c.id,
                        constraint_type=ConstraintType.CREATE_GENERATED_CLOCK.value))
                    return None
                parts.append("-edge_shift { " + " ".join(
                    format_ns(x) for x in es[:3]) + " }")
                edge_shift_emitted = True
        if v.get("add"):
            parts.append("-add")
        targets = render_target_list(c.target_refs, c.target_objects,
                                     CollectionKind.PIN)
        if not targets and not source_sel:
            return None
        if targets:
            parts.append(" ".join(targets))
        return " ".join(parts)

    # ---------- set_input_delay / set_output_delay ----------------
    def _io_delay(self, cmd: str, c: Constraint, v: dict,
                  default_target_kind: CollectionKind) -> str | None:
        """Render set_input_delay / set_output_delay.

        Policy for min_max / edge (Step 6 corrective pass):

        Per the SDC standard (Synopsys SDC 2.1+, Cadence/OpenSTA), when
        neither ``-min`` nor ``-max`` is specified the value applies to
        both minimum and maximum delay analyses; likewise when neither
        ``-rise`` nor ``-fall`` is specified the value applies to both
        edges.  This is the canonical single-command representation of
        ``min_max="both"`` / ``edge=None`` in the UCM and is PROVABLY
        equivalent to emitting two separate ``-min`` and ``-max`` (or
        ``-rise``/``-fall``) commands per the SDC specification, which
        we do not do to avoid redundant output.
        """
        delay = v.get("delay")
        if delay is None:
            return None
        parts: list[str] = [cmd]
        mm = v.get("min_max", "both")
        if mm in ("min", "max"):
            parts.append(f"-{mm}")
        edge = v.get("edge")
        if edge in ("rise", "fall"):
            parts.append(f"-{edge}")
        if v.get("clock_fall"):
            parts.append("-clock_fall")
        if v.get("add_delay"):
            parts.append("-add_delay")
        clk = render_clock_name(c.clock_refs_typed, c.clock_refs)
        if clk:
            parts.append(f"-clock {clk}")
        parts.append(format_ns(delay))
        targets = render_target_list(c.target_refs, c.target_objects,
                                     default_target_kind)
        if not targets:
            return None
        parts.extend(targets)
        return " ".join(parts)

    # ---------- flag helpers -------------------------------------
    def _edge_mm_sh_flags(self, v: dict) -> list[str]:
        flags: list[str] = []
        edge = v.get("edge")
        if edge in ("rise", "fall"):
            flags.append(f"-{edge}")
        mm = v.get("min_max")
        if mm in ("min", "max"):
            flags.append(f"-{mm}")
        sh = v.get("setup_hold")
        if sh in ("setup", "hold"):
            flags.append(f"-{sh}")
        return flags

    def _bool_flags(self, v: dict, names: list[str]) -> list[str]:
        return [f"-{n}" for n in names if v.get(n) is True]

    # ---------- clock uncertainty --------------------------------
    def _clock_uncertainty(self, c: Constraint, v: dict) -> str | None:
        unc = v.get("uncertainty")
        if unc is None:
            return None
        parts = ["set_clock_uncertainty"]
        parts.extend(self._edge_mm_sh_flags(v))
        ps = c.path_selector
        has_from_to = False
        if ps is not None:
            # -from clock (ps.from_refs typed first, else ps.from_clock strings)
            if ps.from_refs:
                parts.append("-from")
                parts.append(self._render_ref_group(ps.from_refs, CollectionKind.CLOCK))
                has_from_to = True
            elif ps.from_clock:
                parts.append("-from")
                parts.append(render_collection_arg(ps.from_clock, CollectionKind.CLOCK)
                             or tcl_quote_list(ps.from_clock))
                has_from_to = True
            if ps.to_refs:
                parts.append("-to")
                parts.append(self._render_ref_group(ps.to_refs, CollectionKind.CLOCK))
                has_from_to = True
            elif ps.to_clock:
                parts.append("-to")
                parts.append(render_collection_arg(ps.to_clock, CollectionKind.CLOCK)
                             or tcl_quote_list(ps.to_clock))
                has_from_to = True
            # -through: prefer typed through_refs
            if ps.through_refs:
                for stage in ps.through_refs:
                    parts.append("-through")
                    parts.append(self._render_ref_group(stage, CollectionKind.CLOCK))
                    has_from_to = True
            else:
                for stage in ps.through_clock or []:
                    parts.append("-through")
                    st = stage if isinstance(stage, list) else [stage]
                    parts.append(render_collection_arg(st, CollectionKind.CLOCK))
                    has_from_to = True
        parts.append(format_ns(unc))
        if not has_from_to:
            targets = render_target_list(
                c.target_refs, c.target_objects or c.clock_refs,
                CollectionKind.CLOCK)
            parts.extend(targets)
        return " ".join(parts)

    # ---------- clock latency ------------------------------------
    def _clock_latency(self, c: Constraint, v: dict) -> str | None:
        lat = v.get("latency")
        if lat is None:
            return None
        parts = ["set_clock_latency"]
        if v.get("source") is True:
            parts.append("-source")
        if v.get("early") is True:
            parts.append("-early")
        if v.get("late") is True:
            parts.append("-late")
        parts.extend(self._edge_mm_sh_flags(v))
        parts.append(format_ns(lat))
        # Targets: source latency -> CLOCK; network latency -> PIN.
        if v.get("source") is True:
            targets = render_target_list(
                c.target_refs, c.target_objects or c.clock_refs,
                CollectionKind.CLOCK)
        else:
            targets = render_target_list(
                c.target_refs, c.target_objects,
                CollectionKind.PIN)
        if not targets:
            return None
        parts.extend(targets)
        return " ".join(parts)

    # ---------- clock transition ---------------------------------
    def _clock_transition(self, c: Constraint, v: dict) -> str | None:
        val = v.get("transition")
        if val is None:
            return None
        parts = ["set_clock_transition"]
        parts.extend(self._edge_mm_sh_flags(v))
        parts.append(format_ns(val))
        targets = render_target_list(
            c.target_refs, c.target_objects or c.clock_refs,
            CollectionKind.CLOCK)
        if not targets:
            return None
        parts.extend(targets)
        return " ".join(parts)

    # ---------- propagated clock ---------------------------------
    def _propagated_clock(self, c: Constraint, v: dict) -> str | None:
        if c.target_refs:
            targets = [s for s in (render_target(r) for r in c.target_refs) if s]
        elif c.target_objects or c.clock_refs:
            targets = [f"[get_clocks {tcl_quote(n)}]" for n in
                       (c.target_objects or c.clock_refs)]
        else:
            targets = ["[all_clocks]"]
        if not targets:
            return None
        return "set_propagated_clock " + " ".join(targets)

    # ---------- clock groups -------------------------------------
    def _clock_groups(self, c: Constraint, v: dict) -> str | None:
        groups = v.get("groups", [])
        rel = v.get("relationship", "asynchronous")
        if len(groups) < 2 or rel not in ("asynchronous", "logically_exclusive",
                                          "physically_exclusive"):
            return None
        parts = ["set_clock_groups", f"-{rel}"]
        # Groups are lists of clock names; wrap with [get_clocks ...]
        # when more than one; single names use [get_clocks n].
        for g in groups:
            parts.append("-group")
            members = list(g)
            parts.append(render_collection_arg(members, CollectionKind.CLOCK)
                         or tcl_quote_list(members))
        return " ".join(parts)

    # ---------- path selectors ----------------------------------
    def _selector_parts(self, ps: PathSelector | None,
                        default_from_kind: CollectionKind = CollectionKind.CLOCK,
                        default_to_kind: CollectionKind = CollectionKind.CLOCK,
                        default_thr_kind: CollectionKind = CollectionKind.PIN
                        ) -> list[str]:
        """Render a PathSelector as a list of SDC flag+arg tokens."""
        if ps is None:
            return []
        parts: list[str] = []
        if ps.edge in ("rise", "fall"):
            parts.append(f"-{ps.edge}")
        if ps.min_max in ("min", "max"):
            parts.append(f"-{ps.min_max}")
        if ps.setup_hold in ("setup", "hold"):
            parts.append(f"-{ps.setup_hold}")

        # -from: typed refs win; fall back to string names with default kind.
        if ps.from_refs:
            parts.append("-from")
            parts.append(self._render_ref_group(ps.from_refs, default_from_kind))
        elif ps.from_set:
            parts.append("-from")
            parts.append(render_collection_arg(list(ps.from_set), default_from_kind))

        # ordered multi-stage -through
        if ps.through_refs:
            for stage in ps.through_refs:
                parts.append("-through")
                parts.append(self._render_ref_group(stage, default_thr_kind))
        else:
            for stage in ps.through_set:
                parts.append("-through")
                parts.append(render_collection_arg(list(stage), default_thr_kind))

        # -to
        if ps.to_refs:
            parts.append("-to")
            parts.append(self._render_ref_group(ps.to_refs, default_to_kind))
        elif ps.to_set:
            parts.append("-to")
            parts.append(render_collection_arg(list(ps.to_set), default_to_kind))

        if ps.add_delay:
            parts.append("-add_delay")
        if ps.reset_path:
            parts.append("-reset_path")
        return [p for p in parts if p is not None]

    def _render_ref_group(self, refs: list[TargetRef],
                          default_kind: CollectionKind) -> str | None:
        """Render a group of TargetRefs as a single SDC arg.

        * Single ref: render as ``[get_* name]``/``[all_*]``.
        * Multiple refs of same kind: render as ``[get_* {a b}]``.
        * Mixed kinds: fall back to a Tcl list literal { ... }
          (semantically suboptimal but valid SDC).
        """
        if not refs:
            return None
        kinds = {r.collection_kind for r in refs}
        if len(kinds) == 1:
            k = next(iter(kinds))
            names: list[str] = []
            for r in refs:
                names.extend(r.names())
            return render_collection_arg(names, k)
        # Mixed kinds — concat rendered selectors with spaces.
        return " ".join(s for s in (render_target(r) for r in refs) if s)

    # ---------- path exceptions ---------------------------------
    def _path_exception(self, cmd: str, c: Constraint, v: dict,
                        has_value: bool, value_key: str | None = None,
                        from_kind: CollectionKind = CollectionKind.CLOCK,
                        to_kind: CollectionKind = CollectionKind.CLOCK,
                        thr_kind: CollectionKind = CollectionKind.PIN
                        ) -> str | None:
        parts = [cmd]
        if has_value and value_key:
            val = v.get(value_key)
            if val is None:
                return None
            parts.append(format_ns(val))
        # reset_path already in _selector_parts; avoid duplication
        parts.extend(self._selector_parts(c.path_selector,
                                          default_from_kind=from_kind,
                                          default_to_kind=to_kind,
                                          default_thr_kind=thr_kind))
        return " ".join(parts)

    def _multicycle(self, c: Constraint, v: dict) -> str | None:
        cyc = v.get("cycles")
        if cyc is None:
            return None
        parts = [f"set_multicycle_path {int(cyc)}"]
        if v.get("start"):
            parts.append("-start")
        if v.get("end"):
            parts.append("-end")
        # _selector_parts will add -setup/-hold from ps.setup_hold or
        # from v["setup_hold"] if ps is None. We don't add it here to
        # avoid duplication.
        parts.extend(self._selector_parts(c.path_selector))
        return " ".join(parts)

    # ---------- design-rule constraints --------------------------
    def _drc(self, cmd: str, c: Constraint, v: dict, val_key: str,
             default_kind: CollectionKind,
             value_is_time: bool = False) -> str | None:
        val = v.get(val_key)
        if val is None:
            return None
        parts = [cmd]
        parts.extend(self._edge_mm_sh_flags(v))
        parts.append(format_ns(val) if value_is_time else _format_drc_value(val))
        targets = render_target_list(c.target_refs, c.target_objects,
                                     default_kind)
        if not targets:
            return None
        parts.extend(targets)
        return " ".join(parts)


def _format_drc_value(v: Any) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        f = float(v)
        if f == int(f):
            return str(int(f))
        return f"{f:.6g}"
    return tcl_quote(str(v))
