"""
SDC parser / importer (Manual §35, §77).

A lightweight Tcl/SDC tokenizer and command parser that converts common
SDC commands into the Universal Constraint Model. This is not a full Tcl
interpreter: it handles the SDC command subset listed in §23, skipping
unknown commands and variable/$-substitutions best-effort.
"""

from __future__ import annotations

import re
import io
from pathlib import Path
from typing import Any

from ..constraint_model import ConstraintSet, PathSelector
from ..provenance import ImportMetadata, ProvenanceRecord
from ..utils.enums import (
    Confidence,
    ConstraintStatus,
    ConstraintType,
    OptimizationStatus,
    SourceKind,
)
from ..utils.logging import get_logger
from ..utils.units import parse_time_string

log = get_logger("sdc.parser")


# Command name -> (ConstraintType, kwargs factory)
_CMD_MAP: dict[str, Any] = {}


def _reg(name: str):
    def deco(fn):
        _CMD_MAP[name] = fn
        return fn
    return deco


@_reg("create_clock")
def _create_clock(args: dict, cset: ConstraintSet, prov: ProvenanceRecord) -> None:
    cset.add_constraint_by_type(ConstraintType.CREATE_CLOCK,
                               name=args.get("name", args.get("source", "clk")),
                               period=parse_time_string(args["period"]) if "period" in args else None,
                               source=args.get("source_objects", [""])[0],
                               waveform=[parse_time_string(w) for w in args.get("waveform", [])],
                               source_kind=SourceKind.EXISTING_SDC,
                               confidence=Confidence.HIGH,
                               status=ConstraintStatus.CONFIRMED,
                               provenance=prov, opt_status=OptimizationStatus.FIXED,
                               targets=args.get("source_objects", []))


class SDCParser:
    """Parses SDC files into a ConstraintSet."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def parse_file(self, path: str | Path) -> ConstraintSet:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        return self.parse_text(text, source=str(path))

    def parse_text(self, text: str, source: str = "<sdc>") -> ConstraintSet:
        cset = ConstraintSet(name=Path(source).stem if source else "imported")
        commands = _tokenize_sdc(text)
        for cmd, line_no in commands:
            try:
                self._handle_command(cmd, cset, source, line_no)
            except Exception as e:
                self.warnings.append(f"{source}:{line_no}: failed to parse '{cmd.split()[0]}': {e}")
        log.info("SDC import: %d constraints, %d warnings", len(cset), len(self.warnings))
        return cset

    def _handle_command(self, cmd_line: str, cset: ConstraintSet, source: str, line_no: int) -> None:
        # Strip trailing comments
        line = _strip_comments(cmd_line).strip()
        if not line:
            return
        tokens = _tokenize_line(line)
        if not tokens:
            return
        cmd = tokens[0]
        args = _parse_args(tokens[1:])

        prov = ProvenanceRecord(
            source_kind=SourceKind.EXISTING_SDC,
            confidence="HIGH",
            created_by="sdc_parser",
            explanation=f"Imported from existing SDC ({source}:{line_no})",
        )
        prov.set_import(ImportMetadata(
            source_file=source,
            source_line=line_no,
            original_command=line,
            source_format="sdc",
        ))

        # --- supported commands ---
        if cmd == "create_clock":
            name = args.get("name", args.get("_positional", [""])[0] if args.get("_positional") else "")
            period = parse_time_string(args["period"]) if "period" in args else None
            sources = args.get("_positional", [])
            src = ""
            # Source object may be [get_ports X] or just X — extract leaf
            for tok in reversed(sources):
                if tok.startswith("["):
                    # e.g. "[get_ports clk]" → "clk"
                    parts = tok.strip("[]").split()
                    if len(parts) >= 2:
                        src = parts[-1].rstrip("]")
                        break
                elif tok and not tok.startswith("-"):
                    src = tok
                    break
            if not src:
                src = args.get("name", name)
            waveform = [parse_time_string(w) for w in args.get("waveform", [])]
            if name or period is not None:
                c = cset.create_clock(
                    name=name or src, period_seconds=period, source=src,
                    source_kind=SourceKind.EXISTING_SDC,
                    confidence=Confidence.HIGH,
                    status=ConstraintStatus.CONFIRMED,
                    waveform=waveform or None,
                    fixed=True,
                    comment=f"Imported from {source}:{line_no}",
                )
                c.provenance = prov
        elif cmd == "create_generated_clock":
            name = args.get("name", "")
            master = args.get("master_clock", args.get("source", ""))
            src = args.get("_positional", [""])[-1] if args.get("_positional") else ""
            div = int(args["divide_by"]) if "divide_by" in args else None
            mul = int(args["multiply_by"]) if "multiply_by" in args else None
            if name:
                c = cset.create_generated_clock(
                    name=name, source=src or name, master_clock=master,
                    divide_by=div, multiply_by=mul,
                    source_kind=SourceKind.EXISTING_SDC, confidence=Confidence.HIGH,
                    status=ConstraintStatus.CONFIRMED,
                )
                c.provenance = prov
        elif cmd == "set_input_delay":
            pos = args.get("_positional", [])
            delay = parse_time_string(pos[0]) if pos else None
            clk = args.get("clock")
            # Targets follow delay value; they may be [get_ports X]
            targets = []
            for tok in pos[1:]:
                if tok.startswith("["):
                    parts = tok.strip("[]").split()
                    if len(parts) >= 2:
                        targets.append(parts[-1].rstrip("]"))
                elif tok:
                    targets.append(tok)
            if delay is not None and clk and targets:
                for t in targets:
                    c = cset.create_input_delay(port=t, clock=clk, delay_seconds=delay,
                                                source_kind=SourceKind.EXISTING_SDC,
                                                confidence=Confidence.HIGH,
                                                status=ConstraintStatus.CONFIRMED)
                    c.provenance = prov
        elif cmd == "set_output_delay":
            pos = args.get("_positional", [])
            delay = parse_time_string(pos[0]) if pos else None
            clk = args.get("clock")
            targets = []
            for tok in pos[1:]:
                if tok.startswith("["):
                    parts = tok.strip("[]").split()
                    if len(parts) >= 2:
                        targets.append(parts[-1].rstrip("]"))
                elif tok:
                    targets.append(tok)
            if delay is not None and clk and targets:
                for t in targets:
                    c = cset.create_output_delay(port=t, clock=clk, delay_seconds=delay,
                                                 source_kind=SourceKind.EXISTING_SDC,
                                                 confidence=Confidence.HIGH,
                                                 status=ConstraintStatus.CONFIRMED)
                    c.provenance = prov
        elif cmd == "set_clock_uncertainty":
            val = parse_time_string(args["_positional"][0]) if args.get("_positional") else None
            targets = args.get("_positional", [None])[1:]
            if val is not None:
                for t in targets or args.get("from", []) + args.get("to", []):
                    c = cset.create_clock_uncertainty(clock=t, uncertainty_seconds=val)
                    c.provenance = prov
        elif cmd == "set_false_path":
            c = cset.create_false_path(
                from_set=args.get("from", []), to_set=args.get("to", []),
                through=[args.get("through", [])] if "through" in args else [],
                source_kind=SourceKind.EXISTING_SDC, confidence=Confidence.MEDIUM,
                status=ConstraintStatus.CONFIRMED,
            )
            c.provenance = prov
        elif cmd == "set_multicycle_path":
            cycles = int(args["_positional"][0]) if args.get("_positional") else 1
            c = cset.create_multicycle(
                cycles=cycles, from_set=args.get("from", []), to_set=args.get("to", []),
                source_kind=SourceKind.EXISTING_SDC, confidence=Confidence.MEDIUM,
                status=ConstraintStatus.CONFIRMED,
            )
            c.provenance = prov
        elif cmd == "set_clock_groups":
            groups = []
            for g in args.get("group", []):
                if isinstance(g, str):
                    inner = g.strip().lstrip("{").rstrip("}").strip()
                    members = inner.split()
                    if members:
                        groups.append(members)
                elif isinstance(g, list):
                    groups.append(g)
            if groups:
                rel = "asynchronous"
                for k in ("asynchronous", "physically_exclusive", "logically_exclusive"):
                    if k in args:
                        rel = k
                        break
                c = cset.create_clock_groups(groups=groups, relationship=rel,
                                             source_kind=SourceKind.EXISTING_SDC,
                                             confidence=Confidence.HIGH,
                                             status=ConstraintStatus.CONFIRMED)
                c.provenance = prov
        elif cmd in ("set_load", "set_driving_cell", "set_input_transition",
                     "set_max_transition", "set_max_capacitance", "set_max_fanout",
                     "set_clock_latency", "set_propagated_clock", "set_case_analysis",
                     "set_min_delay", "set_max_delay", "set_disable_timing"):
            # Recognised but minimally handled — store as a passthrough comment
            self.warnings.append(f"{source}:{line_no}: minimal support for '{cmd}' in SDC importer")
        elif cmd.startswith("#") or cmd in ("",):
            pass
        else:
            self.warnings.append(f"{source}:{line_no}: unknown SDC command '{cmd}'")


# ---------------------------------------------------------------------------
# Tokenizer / arg parser
# ---------------------------------------------------------------------------

def _strip_comments(line: str) -> str:
    # Strip # comments outside braces/quotes (conservative)
    out = []
    in_q = False
    in_brace = 0
    for ch in line:
        if ch == '"' and (not out or out[-1] != "\\"):
            in_q = not in_q
        elif not in_q:
            if ch == "{":
                in_brace += 1
            elif ch == "}":
                in_brace = max(0, in_brace - 1)
            elif ch == "#" and in_brace == 0:
                break
        out.append(ch)
    return "".join(out)


def _tokenize_sdc(text: str) -> list[tuple[str, int]]:
    """Split SDC text into individual commands, each with line number."""
    commands: list[tuple[str, int]] = []
    buf: list[str] = []
    line_no = 1
    start_line = 1
    in_q = False
    in_brace = 0
    escaped = False
    # Backslash-newline continuation
    text = re.sub(r"\\\s*\n", " ", text)
    for ch in text:
        if ch == "\n":
            line_no += 1
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\" and not in_q:
            escaped = True
            continue
        if ch == '"':
            in_q = not in_q
            buf.append(ch)
            continue
        if in_q:
            buf.append(ch)
            continue
        if ch == "{":
            in_brace += 1
            buf.append(ch)
            continue
        if ch == "}":
            in_brace = max(0, in_brace - 1)
            buf.append(ch)
            continue
        if ch == ";" and in_brace == 0:
            cmd = "".join(buf).strip()
            if cmd:
                commands.append((cmd, start_line))
            buf = []
            start_line = line_no
            continue
        if ch == "\n" and in_brace == 0:
            cmd = "".join(buf).strip()
            if cmd:
                commands.append((cmd, start_line))
            buf = []
            start_line = line_no
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        commands.append((tail, start_line))
    return commands


def _tokenize_line(line: str) -> list[str]:
    """Tokenize a single SDC command into words, quoted strings, and {}-groups."""
    tokens: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            i += 1
            continue
        if ch == '"':
            # Quoted string
            i += 1
            sbuf = []
            while i < len(line) and line[i] != '"':
                sbuf.append(line[i])
                i += 1
            tokens.append("".join(sbuf))
            i += 1
            continue
        if ch == "{":
            depth = 1
            i += 1
            sbuf = []
            while i < len(line) and depth > 0:
                if line[i] == "{":
                    depth += 1
                elif line[i] == "}":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                sbuf.append(line[i])
                i += 1
            # Preserve brace content as a single comma-separated / space-separated
            # string, but also split so callers can treat it as a list.
            inner = "".join(sbuf).strip()
            # Store as a single token with sentinel prefix so we can re-split on demand
            tokens.append("{" + inner + "}")
            continue
        if ch == "[":
            # Command substitution — capture as one opaque token
            depth = 1
            i += 1
            sbuf = ["["]
            while i < len(line) and depth > 0:
                if line[i] == "[":
                    depth += 1
                elif line[i] == "]":
                    depth -= 1
                sbuf.append(line[i])
                i += 1
            tokens.append("".join(sbuf))
            continue
        buf.append(ch)
        i += 1
    if buf:
        tokens.append("".join(buf))
    return tokens


def _parse_args(tokens: list[str]) -> dict[str, Any]:
    """Parse `-flag value` style arguments into a dict.

    Special handling for:
    - -from/-to/-through/-group collect into lists
    - -waveform {r f} returns a list of strings
    - Positional arguments go into "_positional"
    """
    args: dict[str, Any] = {"_positional": []}
    list_keys = {"from", "to", "through", "group", "clock_from", "clock_to"}
    # Flags that take no value (boolean switches or sub-selectors).
    no_value_flags = {"all", "max", "min", "setup", "hold", "rise", "fall",
                      "asynchronous", "physically_exclusive", "logically_exclusive"}
    # Flags that take exactly one scalar value (string/number).
    single_value_flags = {
        "name", "period", "clock", "divide_by", "multiply_by", "source", "master_clock",
        "uncertainty", "latency", "transition", "fanout", "capacitance",
    }

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-"):
            key = tok.lstrip("-").replace("-", "_")
            if key in no_value_flags:
                args[key] = True
                i += 1
                continue
            vals: list[str] = []
            i += 1
            while i < len(tokens):
                nxt = tokens[i]
                if nxt.startswith("-"):
                    break
                # Stop before a Tcl command substitution ([...]) — that is a positional.
                if nxt.startswith("["):
                    break
                # Flags like -from/-to/-through/-group can absorb multiple objects;
                # single-value flags only consume one.
                vals.append(nxt)
                i += 1
                if key in single_value_flags:
                    break
            if key in list_keys:
                args.setdefault(key, []).extend(vals)
            elif key == "waveform" and len(vals) >= 2:
                args[key] = vals[:2]
            elif len(vals) == 0:
                args[key] = True
            elif len(vals) == 1:
                args[key] = vals[0]
            else:
                args[key] = vals
        else:
            args["_positional"].append(tok)
            i += 1
    return args
