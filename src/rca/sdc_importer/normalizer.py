"""Stage C: Semantic normalization into the UCM.

This layer takes :class:`ParsedSdc` output and produces an
:class:`SdcImportResult` that contains:

* A fully-populated :class:`ConstraintSet` (UCM) with EXISTING_SDC
  source_kind.
* Per-command import metadata (import status, recognized options,
  unsupported options, original source spans).
* Structured diagnostics for unknown/unsupported commands,
  malformed commands, resolution failures, and disallowed Tcl.

It never executes Tcl — nested substitutions beyond the supported
subset become UNRESOLVED with their raw text preserved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..config.model import ProjectConfig
from ..constraint_model import ConstraintSet, PathSelector
from ..constraint_model.constraint import Constraint
from ..design_model import Design
from ..provenance import Evidence, ImportMetadata, ProvenanceRecord
from ..timing_model import TimingGraph
from ..utils.enums import (
    ClockGroupsRelationship,
    CollectionKind,
    Confidence,
    ConstraintStatus,
    ConstraintType,
    DiagnosticSeverity,
    ImportStatus,
    OptimizationStatus,
    ResolutionStatus,
    SourceKind,
)
from ..utils.hashing import stable_hash
from ..utils.logging import get_logger
from ..utils.units import parse_time_string
from .collections import DesignResolver, TargetCollection, parse_target_value
from .parser import (
    ParseDiagnostic, ParsedSdc, SdcCommand, SdcParseResult, SdcParser,
    _CmdSubstValue,
)

log = get_logger("sdc_importer")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ImportedConstraint:
    """Record associating an imported command to zero or more UCM
    constraints plus import metadata."""
    command_name: str
    source_file: str
    source_line_start: int
    source_line_end: int
    original_command: str
    import_status: ImportStatus
    constraint_ids: list[str] = field(default_factory=list)
    recognized_options: list[str] = field(default_factory=list)
    unsupported_options: list[dict[str, Any]] = field(default_factory=list)
    targets: list[TargetCollection] = field(default_factory=list)
    diagnostics: list[ParseDiagnostic] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class SdcImportResult:
    constraint_set: ConstraintSet
    imports: list[ImportedConstraint] = field(default_factory=list)
    diagnostics: list[ParseDiagnostic] = field(default_factory=list)
    source_file: str = "<sdc>"
    source_text: str = ""

    # Convenience summaries --------------------------------------------
    def counts(self) -> dict[str, int]:
        c = {"total": len(self.imports), "complete": 0, "partial": 0,
             "unresolved": 0, "error": 0, "constraints": len(self.constraint_set)}
        for i in self.imports:
            if i.import_status == ImportStatus.COMPLETE:
                c["complete"] += 1
            elif i.import_status == ImportStatus.PARTIAL:
                c["partial"] += 1
            elif i.import_status == ImportStatus.UNRESOLVED:
                c["unresolved"] += 1
            elif i.import_status == ImportStatus.ERROR:
                c["error"] += 1
        return c


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------


# Known commands handled semantically. Commands not in this set produce
# a recorded "unsupported" entry rather than a constraint.
_SUPPORTED_COMMANDS = {
    "create_clock", "create_generated_clock",
    "set_input_delay", "set_output_delay",
    "set_clock_uncertainty", "set_clock_latency",
    "set_clock_transition",
    "set_clock_groups",
    "set_false_path", "set_multicycle_path",
    "set_min_delay", "set_max_delay",
    "set_propagated_clock",
}

# Commands we recognize but treat as no-op / passthrough with a warning.
_RECOGNIZED_UNSUPPORTED = {
    "set_load", "set_driving_cell", "set_input_transition",
    "set_max_transition", "set_max_capacitance", "set_max_fanout",
    "set_case_analysis", "set_disable_timing", "set_operating_conditions",
    "set_wire_load_model", "set_wire_load_mode",
    "set_clock_gating_check", "set_data_check", "set_ideal_network",
    "set_ideal_latency", "set_ideal_transition", "set_resistance",
    "set_timing_derate", "group_path", "set_sense",
}

# Disallowed / dangerous Tcl commands — NEVER executed; recorded as SECURITY diag.
_FORBIDDEN_COMMANDS = {
    "exec", "source", "eval", "open", "close", "read", "write", "puts",
    "file", "glob", "pid", "socket", "fconfigure", "load", "package",
    "unknown", "after", "catch", "proc", "rename", "uplevel", "upvar",
    "namespace", "apply", "coroutine", "yield", "interp",
}


class SdcImporter:
    """Import parsed SDC into a UCM ConstraintSet."""

    def __init__(self, design: Design | None = None,
                 tg: TimingGraph | None = None,
                 *, run_ts: str | None = None,
                 run_id: str | None = None,
                 source_file: str = "<sdc>") -> None:
        self.design = design
        self.tg = tg
        self.resolver = DesignResolver(design, tg)
        self.run_ts = run_ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.run_id = run_id
        self.source_file = source_file
        self.parser = SdcParser()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def from_text(self, text: str, source_file: str | None = None,
                  cset: ConstraintSet | None = None) -> SdcImportResult:
        sf = source_file or self.source_file
        parse = self.parser.parse_text(text, source_file=sf)
        return self._import_parsed(parse, cset=cset)

    def from_file(self, path: str | Path,
                  cset: ConstraintSet | None = None) -> SdcImportResult:
        p = Path(path)
        text = p.read_text(encoding="utf-8", errors="replace")
        return self.from_text(text, source_file=str(p), cset=cset)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _import_parsed(self, pr: SdcParseResult,
                       cset: ConstraintSet | None = None) -> SdcImportResult:
        cset = cset or ConstraintSet(name=Path(pr.source_file).stem)
        cset.created_at = self.run_ts
        cset.run_id = self.run_id or cset.run_id
        result = SdcImportResult(
            constraint_set=cset,
            source_file=pr.source_file,
            source_text=pr.original_text,
        )
        # Carry parser diagnostics forward.
        result.diagnostics.extend(pr.parsed.diagnostics)
        for d in pr.parsed.diagnostics:
            if d.severity == DiagnosticSeverity.ERROR:
                pass  # already counted below

        for cmd in pr.parsed.commands:
            ic = self._import_command(cmd, cset)
            result.imports.append(ic)
            result.diagnostics.extend(ic.diagnostics)
        return result

    # ------------------------------------------------------------------
    def _import_command(self, cmd: SdcCommand, cset: ConstraintSet) -> ImportedConstraint:
        ic = ImportedConstraint(
            command_name=cmd.command,
            source_file=cmd.source_file,
            source_line_start=cmd.source_line_start,
            source_line_end=cmd.source_line_end,
            original_command=cmd.original_text,
            import_status=ImportStatus.COMPLETE,
        )
        # Detect disallowed commands anywhere in token stream (security)
        for tok in cmd.tokens:
            if tok.kind == "CMD_SUBST":
                inner_word = tok.inner.split(None, 1)[0] if tok.inner.strip() else ""
                if inner_word in _FORBIDDEN_COMMANDS:
                    ic.diagnostics.append(ParseDiagnostic(
                        severity=DiagnosticSeverity.SECURITY,
                        message=f"Disallowed Tcl command '{inner_word}' not executed (security policy).",
                        source_file=cmd.source_file, line=cmd.source_line_start,
                        command=cmd.command, original_text=cmd.original_text,
                        code="FORBIDDEN_TCL",
                    ))
                    ic.import_status = ImportStatus.ERROR
                    return ic

        # Forbid top-level forbidden commands as well (shouldn't normally
        # appear as the SDC command itself, but be defensive).
        if cmd.command in _FORBIDDEN_COMMANDS:
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.SECURITY,
                message=f"Disallowed Tcl command '{cmd.command}' not executed.",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="FORBIDDEN_TCL",
            ))
            ic.import_status = ImportStatus.ERROR
            return ic

        try:
            if cmd.command in _SUPPORTED_COMMANDS:
                self._dispatch(cmd, cset, ic)
            elif cmd.command in _RECOGNIZED_UNSUPPORTED:
                ic.import_status = ImportStatus.PARTIAL
                ic.diagnostics.append(ParseDiagnostic(
                    severity=DiagnosticSeverity.WARNING,
                    message=f"Command '{cmd.command}' is recognized but not semantically modeled; preserved in metadata.",
                    source_file=cmd.source_file, line=cmd.source_line_start,
                    command=cmd.command, original_text=cmd.original_text,
                    code="UNSUPPORTED_COMMAND",
                ))
                # Attach as an opaque passthrough constraint so provenance
                # is not lost.
                opaque_id = self._opaque_passthrough(cmd, cset)
                ic.constraint_ids.append(opaque_id)
            else:
                ic.import_status = ImportStatus.PARTIAL
                ic.diagnostics.append(ParseDiagnostic(
                    severity=DiagnosticSeverity.WARNING,
                    message=f"Unknown SDC command '{cmd.command}'; preserved as opaque metadata.",
                    source_file=cmd.source_file, line=cmd.source_line_start,
                    command=cmd.command, original_text=cmd.original_text,
                    code="UNKNOWN_COMMAND",
                ))
                opaque_id = self._opaque_passthrough(cmd, cset)
                ic.constraint_ids.append(opaque_id)
        except Exception as e:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message=f"Failed to normalize '{cmd.command}': {e}",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="NORMALIZE_ERROR",
            ))
        return ic

    # ------------------------------------------------------------------
    def _provenance(self, cmd: SdcCommand) -> ProvenanceRecord:
        im = ImportMetadata(
            source_file=cmd.source_file,
            source_line=cmd.source_line_start,
            original_command=cmd.original_text,
            source_format="sdc",
            import_timestamp=self.run_ts,
            extra={
                "source_line_end": cmd.source_line_end,
                "normalized_command_name": cmd.command,
            },
        )
        p = ProvenanceRecord(
            created_by="sdc_importer",
            created_at=self.run_ts,
            source_kind=SourceKind.EXISTING_SDC,
            confidence=Confidence.HIGH,
            explanation=f"Imported from SDC: {cmd.command}",
        )
        # Use object.__setattr__ because ImportMetadata is frozen; pydantic
        # allows assignment but frozen=True prevents direct setattr.
        object.__setattr__(p, "import_meta", im)
        p.explanation = (f"Imported from {cmd.source_file}"
                         f":{cmd.source_line_start} ({cmd.command})")
        return p

    def _opaque_passthrough(self, cmd: SdcCommand, cset: ConstraintSet) -> str:
        """Record an unsupported/unknown command as an opaque provenance
        entry attached to a comment-only record, so source text is not lost."""
        # Reuse SET_CASE_ANALYSIS as an "opaque" slot is inappropriate —
        # instead create a comment-only record via MISSING? No: attach to
        # a Constraint with a special value entry so it round-trips.
        # Since UCM doesn't have a "generic SDC" type, we use a comment
        # field on a SET_CASE_ANALYSIS with value None and flag unsupported.
        cid = cset._next_id("IMP")
        c = Constraint(
            id=cid,
            type=ConstraintType.SET_CASE_ANALYSIS,
            values={"_passthrough_command": cmd.command,
                    "_original": cmd.original_text,
                    "_passthrough": True},
            source_kind=SourceKind.EXISTING_SDC,
            confidence=Confidence.LOW,
            status=ConstraintStatus.PROPOSED,
            provenance=self._provenance(cmd),
            comment=f"UNSUPPORTED SDC: {cmd.command}",
        )
        cset.add(c)
        return cid

    # ------------------------------------------------------------------
    def _targets(self, raw_list: list[Any]) -> list[TargetCollection]:
        return [self.resolver.resolve(parse_target_value(v)) for v in raw_list]

    def _scalar_object(self, cmd: SdcCommand, name: str) -> tuple[str | None, TargetCollection | None]:
        """Return (resolved_name, target_collection) for a scalar flag that
        names an object (e.g. -source, -master_clock, -clock). The value may
        be a plain string or a ``[get_* ...]`` command substitution."""
        from .parser import _CmdSubstValue
        o = cmd.opt(name)
        if o is None or not o.values:
            return None, None
        v = o.values[0]
        if isinstance(v, _CmdSubstValue):
            tc = self.resolver.resolve(parse_target_value(v))
            return (tc.resolved_objects[0] if tc.resolved_objects else (tc.pattern or tc.expression)), tc
        return str(v), None

    def _target_names(self, tcs: list[TargetCollection]) -> list[str]:
        out: list[str] = []
        for t in tcs:
            if t.resolution_status == ResolutionStatus.RESOLVED and t.resolved_objects:
                out.extend(t.resolved_objects)
            elif t.pattern:
                out.append(t.pattern)
            elif t.expression:
                out.append(t.expression)
        # deterministic order
        return sorted(set(out))

    def _edge_min_max_setup_hold(self, cmd: SdcCommand, *,
                                 default_min_max: str = "both") -> dict[str, str]:
        """Return edge/min_max/setup_hold qualifiers per SDC semantics.

        default_min_max controls behavior when neither -min nor -max is
        specified:
          - I/O delays (set_input_delay / set_output_delay /
            set_clock_latency_source) default to ``"max"`` per SDC
            convention: a value given without a qualifier applies to
            setup/max only.
          - Clock uncertainty / false_path / exceptions default to
            ``"both"``.
        """
        opts = {o.name for o in cmd.options}
        edge = "rise" if "rise" in opts else ("fall" if "fall" in opts else None)
        has_min = "min" in opts
        has_max = "max" in opts
        mm = "min" if has_min and not has_max else (
             "max" if has_max and not has_min else (
             "both" if has_min and has_max else default_min_max))
        has_setup = "setup" in opts
        has_hold = "hold" in opts
        sh = "setup" if has_setup and not has_hold else (
             "hold" if has_hold and not has_setup else "both")
        return {"edge": edge, "min_max": mm, "setup_hold": sh}

    def _collect_path_selector(self, cmd: SdcCommand, ic: ImportedConstraint) -> PathSelector:
        from_groups = []
        to_groups = []
        through_groups: list[list[str]] = []
        for o in cmd.options:
            if o.name == "from":
                vals = self._flatten_option_values(o.values)
                tcs = self.resolver.resolve_many([parse_target_value(v) for v in vals])
                from_groups = self._target_names(tcs)
                ic.targets.extend(tcs)
            elif o.name == "to":
                vals = self._flatten_option_values(o.values)
                tcs = self.resolver.resolve_many([parse_target_value(v) for v in vals])
                to_groups = self._target_names(tcs)
                ic.targets.extend(tcs)
            elif o.name == "through":
                vals = self._flatten_option_values(o.values)
                tcs = self.resolver.resolve_many([parse_target_value(v) for v in vals])
                stage = self._target_names(tcs)
                if stage:
                    through_groups.append(stage)
                ic.targets.extend(tcs)
        qual = self._edge_min_max_setup_hold(cmd)
        return PathSelector(
            from_set=from_groups, to_set=to_groups, through_set=through_groups,
            edge=qual["edge"], min_max=qual["min_max"], setup_hold=qual["setup_hold"],
        )

    def _flatten_option_values(self, values: list[Any]) -> list[Any]:
        """Flatten a brace-list value into multiple raw entries; others
        pass through as-is."""
        from .parser import _BraceValue, _split_braced_list
        out = []
        for v in values:
            if isinstance(v, _BraceValue):
                out.extend(_split_braced_list(str(v)))
            else:
                out.append(v)
        return out

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------
    def _dispatch(self, cmd: SdcCommand, cset: ConstraintSet, ic: ImportedConstraint) -> None:
        name = cmd.command
        if name == "create_clock":
            self._cmd_create_clock(cmd, cset, ic)
        elif name == "create_generated_clock":
            self._cmd_create_generated_clock(cmd, cset, ic)
        elif name == "set_input_delay":
            self._cmd_io_delay(cmd, cset, ic, ConstraintType.SET_INPUT_DELAY)
        elif name == "set_output_delay":
            self._cmd_io_delay(cmd, cset, ic, ConstraintType.SET_OUTPUT_DELAY)
        elif name == "set_clock_uncertainty":
            self._cmd_clock_uncertainty(cmd, cset, ic)
        elif name == "set_clock_latency":
            self._cmd_clock_latency_or_transition(cmd, cset, ic, ConstraintType.SET_CLOCK_LATENCY)
        elif name == "set_clock_transition":
            self._cmd_clock_latency_or_transition(cmd, cset, ic, ConstraintType.SET_CLOCK_TRANSITION)
        elif name == "set_clock_groups":
            self._cmd_clock_groups(cmd, cset, ic)
        elif name == "set_false_path":
            self._cmd_false_path(cmd, cset, ic)
        elif name == "set_multicycle_path":
            self._cmd_multicycle(cmd, cset, ic)
        elif name == "set_min_delay":
            self._cmd_minmax_delay(cmd, cset, ic, ConstraintType.SET_MIN_DELAY)
        elif name == "set_max_delay":
            self._cmd_minmax_delay(cmd, cset, ic, ConstraintType.SET_MAX_DELAY)
        elif name == "set_propagated_clock":
            self._cmd_propagated_clock(cmd, cset, ic)
        ic.recognized_options = sorted({o.name for o in cmd.options})

    # ----- create_clock ----------------------------------------------
    def _cmd_create_clock(self, cmd, cset, ic):
        name = cmd.scalar("name")
        period_raw = cmd.scalar("period")
        if period_raw is None:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message="create_clock missing -period",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="MISSING_PERIOD",
            ))
            return
        try:
            period = float(parse_time_string(str(period_raw)))
        except Exception as e:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message=f"create_clock invalid -period: {period_raw!r} ({e})",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="BAD_PERIOD",
            ))
            return
        # Targets are positional (port/pin/net after options).
        raw_targets = list(cmd.positional)
        # Waveform option
        waveform = None
        wf = cmd.opt("waveform")
        if wf:
            from .parser import _BraceValue, _split_braced_list
            waves: list[float] = []
            for v in wf.values:
                items = _split_braced_list(str(v)) if isinstance(v, _BraceValue) else [str(v)]
                for it in items:
                    try:
                        waves.append(float(parse_time_string(it)))
                    except Exception:
                        waves.append(float(it))
            waveform = waves if waves else None

        comment = cmd.scalar("comment")
        add = cmd.has_flag("add")

        tcs = self.resolver.resolve_many([parse_target_value(v) for v in raw_targets])
        ic.targets.extend(tcs)
        targets = self._target_names(tcs)

        # If no explicit -name, derive from first target.
        if not name and targets:
            name = targets[0]
        if not name:
            name = "clk"
            ic.notes.append("Clock name inferred as 'clk' (no -name or target)")

        unsupported = {o.name for o in cmd.options} - {"name", "period", "waveform", "add", "comment"}
        for o in unsupported:
            ic.unsupported_options.append({"name": o, "reason": "option not semantically modeled"})
        if unsupported:
            ic.import_status = ImportStatus.PARTIAL
        if any(t.resolution_status == ResolutionStatus.UNRESOLVED for t in tcs):
            ic.import_status = _worsen(ic.import_status, ImportStatus.PARTIAL)

        cid = cset._next_id("CLK")
        values: dict[str, Any] = {"name": name, "period": period}
        if waveform:
            values["waveform"] = waveform
        if add:
            values["add"] = True
        if comment:
            values["comment"] = comment
        c = Constraint(
            id=cid, type=ConstraintType.CREATE_CLOCK,
            target_objects=targets or [name],
            clock_refs=[name],
            values=values,
            source_kind=SourceKind.EXISTING_SDC,
            confidence=Confidence.HIGH,
            status=ConstraintStatus.FIXED,
            opt_status=OptimizationStatus.FIXED,
            provenance=self._provenance(cmd),
        )
        cset.add(c)
        ic.constraint_ids.append(cid)

    # ----- create_generated_clock ------------------------------------
    def _cmd_create_generated_clock(self, cmd, cset, ic):
        name = cmd.scalar("name")
        source, src_tc = self._scalar_object(cmd, "source")
        if src_tc is not None:
            ic.targets.append(src_tc)
        master, m_tc = self._scalar_object(cmd, "master_clock")
        if m_tc is not None:
            ic.targets.append(m_tc)
        div = cmd.scalar("divide_by")
        mul = cmd.scalar("multiply_by")
        duty = cmd.scalar("duty_cycle")
        invert = cmd.has_flag("invert")
        combinational = cmd.has_flag("combinational")
        add = cmd.has_flag("add")
        edges = None
        edge_shift = None
        for o in cmd.options:
            if o.name == "edges":
                from .parser import _BraceValue, _split_braced_list
                vals: list[int] = []
                for v in o.values:
                    items = _split_braced_list(str(v)) if isinstance(v, _BraceValue) else [str(v)]
                    for it in items:
                        try:
                            vals.append(int(it))
                        except Exception:
                            pass
                edges = vals or None
            if o.name == "edge_shift":
                from .parser import _BraceValue, _split_braced_list
                es: list[float] = []
                for v in o.values:
                    items = _split_braced_list(str(v)) if isinstance(v, _BraceValue) else [str(v)]
                    for it in items:
                        try:
                            es.append(float(parse_time_string(it)))
                        except Exception:
                            es.append(float(it))
                edge_shift = es or None

        raw_targets = list(cmd.positional)
        tcs = self.resolver.resolve_many([parse_target_value(v) for v in raw_targets])
        ic.targets.extend(tcs)
        targets = self._target_names(tcs)

        if not name and targets:
            name = targets[0]
        if not name:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message="create_generated_clock missing -name",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="MISSING_GCLK_NAME",
            ))
            return
        # Source/pin is the last positional if not given.
        if not source and targets:
            source = targets[-1]
        values: dict[str, Any] = {
            "name": name,
            "source": source or "",
            "master_clock": master or "",
        }
        if div is not None:
            try:
                values["divide_by"] = int(div)
            except Exception:
                values["divide_by"] = div
        if mul is not None:
            try:
                values["multiply_by"] = int(mul)
            except Exception:
                values["multiply_by"] = mul
        if duty is not None:
            try:
                values["duty_cycle"] = float(duty)
            except Exception:
                values["duty_cycle"] = duty
        if edges:
            values["edges"] = edges
        if edge_shift:
            values["edge_shift"] = edge_shift
        if invert:
            values["invert"] = True
        if combinational:
            values["combinational"] = True
        if add:
            values["add"] = True

        recognized = {"name", "source", "master_clock", "divide_by", "multiply_by",
                      "duty_cycle", "invert", "edges", "edge_shift", "combinational", "add"}
        unsupported = {o.name for o in cmd.options} - recognized
        for u in unsupported:
            ic.unsupported_options.append({"name": u, "reason": "option not semantically modeled"})
        if unsupported:
            ic.import_status = ImportStatus.PARTIAL
        if any(t.resolution_status == ResolutionStatus.UNRESOLVED for t in tcs):
            ic.import_status = _worsen(ic.import_status, ImportStatus.PARTIAL)

        cid = cset._next_id("GCLK")
        c = Constraint(
            id=cid, type=ConstraintType.CREATE_GENERATED_CLOCK,
            target_objects=targets or [source or name],
            clock_refs=sorted({n for n in (name, master) if n}),
            values=values,
            source_kind=SourceKind.EXISTING_SDC,
            confidence=Confidence.HIGH,
            status=ConstraintStatus.FIXED,
            opt_status=OptimizationStatus.FIXED,
            provenance=self._provenance(cmd),
        )
        cset.add(c)
        ic.constraint_ids.append(cid)

    # ----- set_input_delay / set_output_delay ------------------------
    def _cmd_io_delay(self, cmd, cset, ic, ctype):
        if not cmd.positional:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message=f"{cmd.command} missing delay value",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="MISSING_DELAY_VALUE",
            ))
            return
        try:
            delay = float(parse_time_string(str(cmd.positional[0])))
        except Exception as e:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message=f"{cmd.command} invalid delay value {cmd.positional[0]!r} ({e})",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="BAD_DELAY",
            ))
            return
        clk, clk_tc = self._scalar_object(cmd, "clock")
        if clk_tc is not None:
            ic.targets.append(clk_tc)
        add_delay = cmd.has_flag("add_delay")
        clock_fall = cmd.has_flag("clock_fall")
        qual = self._edge_min_max_setup_hold(cmd, default_min_max="max")
        edge = qual["edge"]
        mm = qual["min_max"]

        raw_targets = list(cmd.positional[1:])
        tcs = self.resolver.resolve_many([parse_target_value(v) for v in raw_targets])
        ic.targets.extend(tcs)
        targets = self._target_names(tcs)
        if not targets:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message=f"{cmd.command} has no target collection",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="NO_TARGETS",
            ))
            return
        if not clk:
            ic.import_status = ImportStatus.PARTIAL
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.WARNING,
                message=f"{cmd.command} missing -clock; recorded without clock reference",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="MISSING_CLOCK",
            ))

        # Emit separate constraints per (min/max, rise/fall) combination
        # that is active. SDC says: with neither -min nor -max the value
        # applies to both. With neither -rise nor -fall it applies to
        # both edges. When -add_delay is present, multiple constraints
        # on the same port/clock coexist.
        min_maxes = ["min", "max"] if mm == "both" else [mm]
        edges = ["rise", "fall"] if edge is None else [edge]
        recognized = {"clock", "clock_fall", "rise", "fall", "min", "max",
                      "add_delay", "reference_pin",
                      "source_latency_included", "network_latency_included"}
        unsupported = {o.name for o in cmd.options} - recognized
        for u in unsupported:
            ic.unsupported_options.append({"name": u, "reason": "option not semantically modeled"})
        if unsupported:
            ic.import_status = _worsen(ic.import_status, ImportStatus.PARTIAL)
        if any(t.resolution_status == ResolutionStatus.UNRESOLVED for t in tcs):
            ic.import_status = _worsen(ic.import_status, ImportStatus.PARTIAL)

        for port in targets:
            for mv in min_maxes:
                for ev in edges:
                    values = {
                        "clock": clk or "",
                        "delay": delay,
                        "min_max": mv,
                        "edge": ev,
                    }
                    if add_delay:
                        values["add_delay"] = True
                    if clock_fall:
                        values["clock_fall"] = True
                    prefix = "INP" if ctype == ConstraintType.SET_INPUT_DELAY else "OUT"
                    cid = cset._next_id(prefix)
                    c = Constraint(
                        id=cid, type=ctype,
                        target_objects=[port],
                        clock_refs=[clk] if clk else [],
                        values=values,
                        source_kind=SourceKind.EXISTING_SDC,
                        confidence=Confidence.HIGH,
                        status=ConstraintStatus.FIXED,
                        opt_status=OptimizationStatus.FIXED,
                        provenance=self._provenance(cmd),
                    )
                    cset.add(c)
                    ic.constraint_ids.append(cid)

    # ----- set_clock_uncertainty -------------------------------------
    def _cmd_clock_uncertainty(self, cmd, cset, ic):
        if not cmd.positional:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message="set_clock_uncertainty missing value",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="MISSING_VALUE",
            ))
            return
        try:
            val = float(parse_time_string(str(cmd.positional[0])))
        except Exception as e:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message=f"set_clock_uncertainty invalid value {cmd.positional[0]!r} ({e})",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="BAD_VALUE",
            ))
            return
        qual = self._edge_min_max_setup_hold(cmd)
        # Object selectors: positional (after value) OR -from/-to pairs.
        from_list_o = cmd.opt("from")
        to_list_o = cmd.opt("to")
        objects: list[TargetCollection] = []
        if from_list_o and to_list_o:
            from .parser import _BraceValue, _split_braced_list
            fvals = []
            for v in from_list_o.values:
                fvals.extend(_split_braced_list(str(v)) if isinstance(v, _BraceValue) else [v])
            svals = []
            for v in to_list_o.values:
                svals.extend(_split_braced_list(str(v)) if isinstance(v, _BraceValue) else [v])
            objects = self.resolver.resolve_many([parse_target_value(v) for v in fvals + svals])
        else:
            raw = list(cmd.positional[1:])
            objects = self.resolver.resolve_many([parse_target_value(v) for v in raw])
        ic.targets.extend(objects)
        names = self._target_names(objects)
        if not names:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message="set_clock_uncertainty has no targets",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="NO_TARGETS",
            ))
            return
        for n in names:
            cid = cset._next_id("UNC")
            c = Constraint(
                id=cid, type=ConstraintType.SET_CLOCK_UNCERTAINTY,
                target_objects=[n],
                clock_refs=[n],
                values={
                    "uncertainty": val,
                    "min_max": qual["min_max"],
                    "setup_hold": qual["setup_hold"],
                    "edge": qual["edge"],
                },
                source_kind=SourceKind.EXISTING_SDC,
                confidence=Confidence.HIGH,
                status=ConstraintStatus.FIXED,
                opt_status=OptimizationStatus.FIXED,
                provenance=self._provenance(cmd),
            )
            cset.add(c)
            ic.constraint_ids.append(cid)

    # ----- set_clock_latency / set_clock_transition ------------------
    def _cmd_clock_latency_or_transition(self, cmd, cset, ic, ctype):
        if not cmd.positional:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message=f"{cmd.command} missing value",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="MISSING_VALUE",
            ))
            return
        try:
            val = float(parse_time_string(str(cmd.positional[0])))
        except Exception as e:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message=f"{cmd.command} invalid value {cmd.positional[0]!r} ({e})",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="BAD_VALUE",
            ))
            return
        # Latency defaults to max (late) per SDC when no -min/-max given.
        qual = self._edge_min_max_setup_hold(cmd, default_min_max="max")
        is_source = cmd.has_flag("source")
        early = cmd.has_flag("early")
        late = cmd.has_flag("late")
        clk, clk_tc = self._scalar_object(cmd, "clock")
        if clk_tc is not None:
            ic.targets.append(clk_tc)
        raw = list(cmd.positional[1:])
        tcs = self.resolver.resolve_many([parse_target_value(v) for v in raw])
        ic.targets.extend(tcs)
        names = self._target_names(tcs)
        if clk and clk not in names:
            names.append(clk)
        if not names:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message=f"{cmd.command} has no clock targets",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="NO_TARGETS",
            ))
            return
        vkey = "latency" if ctype == ConstraintType.SET_CLOCK_LATENCY else "transition"
        for n in names:
            cid = cset._next_id("LAT" if ctype == ConstraintType.SET_CLOCK_LATENCY else "TRN")
            values = {vkey: val, "min_max": qual["min_max"],
                      "edge": qual["edge"], "setup_hold": qual["setup_hold"]}
            if is_source:
                values["source"] = True
            if early:
                values["early"] = True
            if late:
                values["late"] = True
            c = Constraint(
                id=cid, type=ctype,
                target_objects=[n],
                clock_refs=[n],
                values=values,
                source_kind=SourceKind.EXISTING_SDC,
                confidence=Confidence.HIGH,
                status=ConstraintStatus.FIXED,
                opt_status=OptimizationStatus.FIXED,
                provenance=self._provenance(cmd),
            )
            cset.add(c)
            ic.constraint_ids.append(cid)

    # ----- set_clock_groups -----------------------------------------
    def _cmd_clock_groups(self, cmd, cset, ic):
        rel = None
        for r in (ClockGroupsRelationship.ASYNCHRONOUS,
                  ClockGroupsRelationship.LOGICALLY_EXCLUSIVE,
                  ClockGroupsRelationship.PHYSICALLY_EXCLUSIVE):
            if cmd.has_flag(r.value):
                rel = r.value
                break
        if rel is None:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message="set_clock_groups requires one of -asynchronous/-logically_exclusive/-physically_exclusive",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="MISSING_RELATIONSHIP",
            ))
            return
        groups: list[list[str]] = []
        all_tcs: list[TargetCollection] = []
        from .parser import _BraceValue, _split_braced_list
        for o in cmd.options:
            if o.name != "group":
                continue
            members: list[str] = []
            for v in o.values:
                if isinstance(v, _BraceValue):
                    items = _split_braced_list(str(v))
                elif isinstance(v, _CmdSubstValue):
                    # e.g. -group [get_clocks {a b}]
                    items = [v]
                else:
                    items = [v]
                tcs = self.resolver.resolve_many([parse_target_value(it) for it in items])
                all_tcs.extend(tcs)
                members.extend(self._target_names(tcs))
            if members:
                groups.append(sorted(set(members)))
        ic.targets.extend(all_tcs)
        if len(groups) < 2:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message="set_clock_groups requires at least two -group arguments",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="GROUPS_TOO_FEW",
            ))
            return
        cid = cset._next_id("CG")
        c = Constraint(
            id=cid, type=ConstraintType.SET_CLOCK_GROUPS,
            values={"groups": groups, "relationship": rel},
            clock_refs=sorted({n for g in groups for n in g}),
            source_kind=SourceKind.EXISTING_SDC,
            confidence=Confidence.HIGH,
            status=ConstraintStatus.FIXED,
            opt_status=OptimizationStatus.FIXED,
            provenance=self._provenance(cmd),
        )
        cset.add(c)
        ic.constraint_ids.append(cid)

    # ----- path-exception helpers ------------------------------------
    def _exception_values(self, cmd, ctype):
        qual = self._edge_min_max_setup_hold(cmd)
        return {
            "min_max": qual["min_max"],
            "setup_hold": qual["setup_hold"],
            "edge": qual["edge"],
        }

    # ----- set_false_path -------------------------------------------
    def _cmd_false_path(self, cmd, cset, ic):
        sel = self._collect_path_selector(cmd, ic)
        cid = cset._next_id("FP")
        c = Constraint(
            id=cid, type=ConstraintType.SET_FALSE_PATH,
            path_selector=sel,
            values=self._exception_values(cmd, cmd.command),
            source_kind=SourceKind.EXISTING_SDC,
            confidence=Confidence.HIGH,
            status=ConstraintStatus.FIXED,
            opt_status=OptimizationStatus.FIXED,
            provenance=self._provenance(cmd),
        )
        cset.add(c)
        ic.constraint_ids.append(cid)
        if any(t.resolution_status == ResolutionStatus.UNRESOLVED for t in ic.targets):
            ic.import_status = ImportStatus.PARTIAL

    # ----- set_multicycle_path --------------------------------------
    def _cmd_multicycle(self, cmd, cset, ic):
        if not cmd.positional:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message="set_multicycle_path missing cycle count",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="MISSING_CYCLES",
            ))
            return
        try:
            cycles = int(cmd.positional[0])
        except Exception:
            try:
                cycles = int(float(cmd.positional[0]))
            except Exception as e:
                ic.import_status = ImportStatus.ERROR
                ic.diagnostics.append(ParseDiagnostic(
                    severity=DiagnosticSeverity.ERROR,
                    message=f"set_multicycle_path invalid cycle count {cmd.positional[0]!r} ({e})",
                    source_file=cmd.source_file, line=cmd.source_line_start,
                    command=cmd.command, original_text=cmd.original_text,
                    code="BAD_CYCLES",
                ))
                return
        sel = self._collect_path_selector(cmd, ic)
        start = cmd.has_flag("start")
        end = cmd.has_flag("end")
        vals = self._exception_values(cmd, cmd.command)
        vals["cycles"] = cycles
        if start:
            vals["start"] = True
        if end:
            vals["end"] = True
        cid = cset._next_id("MC")
        c = Constraint(
            id=cid, type=ConstraintType.SET_MULTICYCLE_PATH,
            path_selector=sel,
            values=vals,
            source_kind=SourceKind.EXISTING_SDC,
            confidence=Confidence.HIGH,
            status=ConstraintStatus.FIXED,
            opt_status=OptimizationStatus.FIXED,
            provenance=self._provenance(cmd),
        )
        cset.add(c)
        ic.constraint_ids.append(cid)

    # ----- set_min_delay / set_max_delay ----------------------------
    def _cmd_minmax_delay(self, cmd, cset, ic, ctype):
        if not cmd.positional:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message=f"{cmd.command} missing delay value",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="MISSING_VALUE",
            ))
            return
        try:
            val = float(parse_time_string(str(cmd.positional[0])))
        except Exception as e:
            ic.import_status = ImportStatus.ERROR
            ic.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message=f"{cmd.command} invalid delay {cmd.positional[0]!r} ({e})",
                source_file=cmd.source_file, line=cmd.source_line_start,
                command=cmd.command, original_text=cmd.original_text,
                code="BAD_VALUE",
            ))
            return
        sel = self._collect_path_selector(cmd, ic)
        vals = self._exception_values(cmd, cmd.command)
        vals["delay"] = val
        cid = cset._next_id("MIND" if ctype == ConstraintType.SET_MIN_DELAY else "MAXD")
        c = Constraint(
            id=cid, type=ctype,
            path_selector=sel,
            values=vals,
            source_kind=SourceKind.EXISTING_SDC,
            confidence=Confidence.HIGH,
            status=ConstraintStatus.FIXED,
            opt_status=OptimizationStatus.FIXED,
            provenance=self._provenance(cmd),
        )
        cset.add(c)
        ic.constraint_ids.append(cid)

    # ----- set_propagated_clock -------------------------------------
    def _cmd_propagated_clock(self, cmd, cset, ic):
        raw = list(cmd.positional)
        tcs = self.resolver.resolve_many([parse_target_value(v) for v in raw])
        ic.targets.extend(tcs)
        names = self._target_names(tcs)
        if not names:
            names = ["*"]
            ic.import_status = ImportStatus.PARTIAL
            ic.notes.append("set_propagated_clock has no explicit targets; applied to all clocks")
        for n in names:
            cid = cset._next_id("PROP")
            c = Constraint(
                id=cid, type=ConstraintType.SET_PROPAGATED_CLOCK,
                target_objects=[n],
                clock_refs=[n] if n != "*" else [],
                values={"propagated": True},
                source_kind=SourceKind.EXISTING_SDC,
                confidence=Confidence.HIGH,
                status=ConstraintStatus.FIXED,
                opt_status=OptimizationStatus.FIXED,
                provenance=self._provenance(cmd),
            )
            cset.add(c)
            ic.constraint_ids.append(cid)


def _worsen(current: ImportStatus, target: ImportStatus) -> ImportStatus:
    order = {ImportStatus.COMPLETE: 0, ImportStatus.PARTIAL: 1,
             ImportStatus.UNRESOLVED: 2, ImportStatus.ERROR: 3}
    return current if order[current] >= order[target] else target
