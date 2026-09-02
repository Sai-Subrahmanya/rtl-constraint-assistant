"""Stage B: SDC command / option parsing.

Given lexed tokens from :mod:`lexer`, build a list of structured
:class:`SdcCommand` objects with:

* command name
* ordered options (``-flag value...``)
* ordered positional arguments
* source file + start/end line
* original source text
* any parse diagnostics

This layer knows about SDC option grammar (which flags are booleans,
which take values, which are list-collecting) but does NOT resolve
target collections against a Design; that's Stage C.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..utils.enums import DiagnosticSeverity
from .lexer import (
    BWORD, CMD_SUBST, COMMENT, LexError, LexToken, NEWLINE, QWORD, SEMI, WORD,
    TclLexer, _fold_line_continuations,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SdcOption:
    name: str                        # without leading dash
    values: list[Any] = field(default_factory=list)
    is_boolean_switch: bool = False  # flag without value (e.g. -add)
    # Source-span: which token indices in parent command this option spanned.
    token_start: int = 0
    token_end: int = 0


@dataclass
class SdcCommand:
    """A single parsed SDC command."""
    command: str
    options: list[SdcOption] = field(default_factory=list)
    positional: list[Any] = field(default_factory=list)
    source_file: str = "<sdc>"
    source_line_start: int = 0
    source_line_end: int = 0
    original_text: str = ""
    tokens: list[LexToken] = field(default_factory=list)

    # Convenience -------------------------------------------------------
    def opt(self, name: str) -> SdcOption | None:
        for o in self.options:
            if o.name == name:
                return o
        return None

    def has_flag(self, name: str) -> bool:
        o = self.opt(name)
        return o is not None and o.is_boolean_switch

    def scalar(self, name: str) -> Any:
        o = self.opt(name)
        if not o or not o.values:
            return None
        return o.values[0]

    def list_of(self, name: str) -> list[Any]:
        """Return all values for repeated flags (e.g. multiple -group)."""
        out: list[Any] = []
        for o in self.options:
            if o.name == name:
                out.extend(o.values)
        return out


@dataclass
class ParseDiagnostic:
    severity: DiagnosticSeverity
    message: str
    source_file: str
    line: int
    command: str | None = None
    original_text: str | None = None
    code: str = ""


@dataclass
class ParsedSdc:
    commands: list[SdcCommand] = field(default_factory=list)
    diagnostics: list[ParseDiagnostic] = field(default_factory=list)


@dataclass
class SdcParseResult:
    parsed: ParsedSdc
    original_text: str
    source_file: str


# ---------------------------------------------------------------------------
# Per-command option grammar
# ---------------------------------------------------------------------------
#
# The grammar tells the parser, for each known SDC command:
#   boolean_flags  -> flags that take no value  (e.g. -add, -rise)
#   list_flags     -> flags that may repeat and collect multiple values
#                     (each occurrence collects words until next flag or
#                     brace/command-substitution boundary).
#   scalar_flags   -> flags that take exactly one value.
#   remainder_positional_after_first_scalar -> False (default)
#
# Unknown flags are still captured (as generic string options) rather
# than rejected, so we can preserve them in import metadata.
#

_BOOLEAN_FLAGS: dict[str, set[str]] = {
    "create_clock": {"add"},
    "create_generated_clock": {
        "add", "invert", "combinational",
    },
    "set_input_delay": {
        "clock_fall", "rise", "fall", "min", "max", "add_delay",
        "source_latency_included", "network_latency_included",
    },
    "set_output_delay": {
        "clock_fall", "rise", "fall", "min", "max", "add_delay",
        "source_latency_included", "network_latency_included",
    },
    "set_clock_uncertainty": {
        "rise", "fall", "min", "max", "setup", "hold",
    },
    "set_clock_latency": {
        "rise", "fall", "min", "max", "source", "early", "late",
    },
    "set_clock_transition": {
        "rise", "fall", "min", "max", "setup", "hold",
    },
    "set_input_transition": {
        "rise", "fall", "min", "max",
    },
    "set_clock_groups": {
        "asynchronous", "logically_exclusive", "physically_exclusive",
        "allow_paths",
    },
    "set_false_path": {
        "rise", "fall", "setup", "hold", "reset_path",
    },
    "set_multicycle_path": {
        "setup", "hold", "rise", "fall", "start", "end",
    },
    "set_min_delay": {
        "rise", "fall", "setup", "hold",
    },
    "set_max_delay": {
        "rise", "fall", "setup", "hold",
    },
    "set_propagated_clock": set(),
    "set_driving_cell": set(),
    "set_load": {"min", "max"},
    "set_max_transition": {"min", "max"},
    "set_max_capacitance": {"min", "max"},
    "set_max_fanout": set(),
    "set_case_analysis": set(),
    "set_disable_timing": set(),
}

_LIST_FLAGS: dict[str, set[str]] = {
    "create_clock": {},
    "create_generated_clock": {"edges", "edge_shift"},
    "set_input_delay": {},
    "set_output_delay": {},
    "set_clock_uncertainty": {"from", "to"},
    "set_clock_latency": {},
    "set_clock_transition": {},
    "set_input_transition": {},
    "set_clock_groups": {"group"},
    "set_false_path": {"from", "to", "through"},
    "set_multicycle_path": {"from", "to", "through"},
    "set_min_delay": {"from", "to", "through"},
    "set_max_delay": {"from", "to", "through"},
}

_SCALAR_FLAGS: dict[str, set[str]] = {
    "create_clock": {"name", "period", "waveform", "comment", "domain"},
    "create_generated_clock": {
        "name", "source", "master_clock", "divide_by", "multiply_by",
        "duty_cycle", "edges", "edge_shift",
    },
    "set_input_delay": {"clock", "reference_pin"},
    "set_output_delay": {"clock", "reference_pin"},
    "set_clock_uncertainty": {"rise", "fall"},
    "set_clock_latency": {"clock"},
    "set_clock_transition": {},
    "set_input_transition": {"clock"},
    "set_false_path": {},
    "set_multicycle_path": {},
    "set_min_delay": {},
    "set_max_delay": {},
    "set_propagated_clock": set(),
    "set_driving_cell": {"lib_cell", "pin"},
    "set_load": set(),
    "set_max_transition": set(),
    "set_max_capacitance": set(),
    "set_max_fanout": set(),
    "set_case_analysis": set(),
    "set_disable_timing": {"from", "to"},
}


def _split_braced_list(value: str) -> list[str]:
    """Split a brace group's inner text into whitespace-separated items
    (supporting nested brace-quoted items)."""
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        while i < n and value[i].isspace():
            i += 1
        if i >= n:
            break
        start = i
        depth = 0
        in_q = False
        while i < n:
            ch = value[i]
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"' and depth == 0:
                in_q = not in_q
            elif not in_q:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth = max(0, depth - 1)
                elif ch.isspace() and depth == 0:
                    break
            i += 1
        item = value[start:i]
        # Strip wrapping braces from a single braced element.
        item = item.strip()
        if item.startswith("{") and item.endswith("}"):
            # Count leading / trailing to avoid stripping nested ones
            item = _strip_wrapper_braces(item)
        if item or True:
            out.append(item)
    return [x for x in out if x != ""]


def _strip_wrapper_braces(s: str) -> str:
    if not (s.startswith("{") and s.endswith("}")):
        return s
    depth = 0
    for idx, ch in enumerate(s):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and idx == len(s) - 1:
                return s[1:-1]
    return s


def _stringify_token(tok: LexToken) -> Any:
    """Return the 'interpreted' value of a lexed token suitable for
    storage in an option value or positional argument.

    * BWORD -> returns a :class:`BraceList` wrapper that can be split later.
    * CMD_SUBST -> returns the raw text (brackets included) for later
      collection parsing.
    * QWORD/WORD -> plain string.
    """
    if tok.kind == BWORD:
        return _BraceValue(tok.text)
    if tok.kind == CMD_SUBST:
        return _CmdSubstValue(tok.text, tok.inner)
    return tok.text


class _BraceValue(str):
    """Marker subclass: the value came from a { ... } group.

    Behaves like the raw inner string but callers can test
    ``isinstance(v, _BraceValue)`` to know it was braced and needs
    splitting for list-context flags.
    """


class _CmdSubstValue(str):
    """Marker: the value is ``[subcommand ...]`` raw text.

    ``inner`` is stored in a per-instance ``__dict__`` entry to avoid
    sharing across instances (a naive ``cls.inner = ...`` on a str
    subclass sets a *class* attribute, so every instance sees the last
    value assigned).
    """
    def __new__(cls, raw, inner):
        s = super().__new__(cls, raw)
        s.inner = inner
        return s


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


# Line-based original-text reconstruction is tricky after continuation
# folding; we instead reconstruct from tokens + raw text line ranges.
# We capture original text per command from the original source using
# (start_line, end_line).
#

class SdcParser:
    """Parse SDC text into :class:`ParsedSdc`.

    The parser never executes Tcl. Command substitutions are preserved
    as raw text; unsupported commands become diagnostics with severity
    WARNING but are still recorded so downstream code can inspect them.
    """

    def __init__(self) -> None:
        self.lexer = TclLexer()

    # -- public --------------------------------------------------------

    def parse_text(self, text: str, source_file: str = "<sdc>") -> SdcParseResult:
        # Preserve original lines for source-text reconstruction.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        parsed = ParsedSdc()
        for cmd_tokens in self.lexer.tokenize_commands(text, source_file=source_file):
            if not cmd_tokens:
                continue
            # Skip comment-only commands.
            if len(cmd_tokens) == 1 and cmd_tokens[0].kind == COMMENT:
                continue
            # Drop any comment tokens inside a command (they shouldn't
            # appear mid-command but be defensive).
            cmd_tokens = [t for t in cmd_tokens if t.kind != COMMENT]
            if not cmd_tokens:
                continue

            start_line = min(t.line for t in cmd_tokens)
            end_line = max(t.line for t in cmd_tokens)
            orig = self._extract_original(lines, start_line, end_line)
            cmd_name_tok = cmd_tokens[0]
            cmd_name = cmd_name_tok.text
            # Validate the command name is a plain word (not [ ... ] or { ... })
            if cmd_name_tok.kind not in (WORD, QWORD):
                parsed.diagnostics.append(ParseDiagnostic(
                    severity=DiagnosticSeverity.ERROR,
                    message=f"Command name must be a plain word, got {cmd_name_tok.kind}",
                    source_file=source_file, line=start_line,
                    original_text=orig, code="INVALID_COMMAND_NAME",
                ))
                continue

            try:
                cmd = self._build_command(cmd_tokens, source_file, start_line, end_line, orig)
                parsed.commands.append(cmd)
            except Exception as e:
                parsed.diagnostics.append(ParseDiagnostic(
                    severity=DiagnosticSeverity.ERROR,
                    message=f"Internal error parsing '{cmd_name}': {e}",
                    source_file=source_file, line=start_line,
                    command=cmd_name, original_text=orig, code="INTERNAL_PARSE_ERROR",
                ))

        # Surface lexer errors as diagnostics too.
        for e in self.lexer.errors:
            parsed.diagnostics.append(ParseDiagnostic(
                severity=DiagnosticSeverity.ERROR,
                message=str(e), source_file=source_file, line=e.line,
                code="LEX_ERROR",
            ))

        return SdcParseResult(parsed=parsed, original_text=text, source_file=source_file)

    def parse_file(self, path: str) -> SdcParseResult:
        from pathlib import Path
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        return self.parse_text(text, source_file=str(path))

    # -- internals -----------------------------------------------------

    def _extract_original(self, lines: list[str], start: int, end: int) -> str:
        # Lines are 1-indexed.
        s = max(start - 1, 0)
        e = min(end, len(lines))
        return "\n".join(lines[s:e])

    def _build_command(self, tokens: list[LexToken], source_file: str,
                       start_line: int, end_line: int, original_text: str) -> SdcCommand:
        assert tokens
        cmd_name = tokens[0].text
        opts: list[SdcOption] = []
        positional: list[Any] = []
        i = 1
        n = len(tokens)
        known_bools = _BOOLEAN_FLAGS.get(cmd_name, set())
        known_lists = _LIST_FLAGS.get(cmd_name, set())
        # Any flag token that looks like -foo is treated as a flag even
        # if unknown; unknown flags are preserved with generic semantics.

        while i < n:
            tok = tokens[i]
            sval = _stringify_token(tok)
            if isinstance(sval, str) and sval.startswith("-") and tok.kind in (WORD, QWORD):
                flag = sval.lstrip("-")
                # normalize repeated dashes (edge case)
                while flag.startswith("-"):
                    flag = flag[1:]
                flag = flag.replace("-", "_")
                opt = SdcOption(name=flag, token_start=i, token_end=i)
                # Boolean?
                if flag in known_bools or self._is_boolean_default(flag, cmd_name):
                    opt.is_boolean_switch = True
                    opts.append(opt)
                    i += 1
                    continue
                # Otherwise consume values until next flag.
                i += 1
                opt.token_start = i
                consumed_one_value = False
                consumed_brace = False
                stop_due_to_cmd_subst = False
                while i < n:
                    nxt = tokens[i]
                    nv = _stringify_token(nxt)
                    is_flag = (isinstance(nv, str) and nv.startswith("-")
                               and nxt.kind in (WORD, QWORD)
                               and not isinstance(nv, _CmdSubstValue))
                    # Stop if next is a flag and we've already consumed at
                    # least one value (including a brace group).
                    if is_flag and consumed_one_value:
                        break
                    # Braced value: split into a list if this is a list
                    # flag, otherwise keep as a string.
                    if isinstance(nv, _BraceValue):
                        if flag in ("waveform", "edges", "edge_shift", "group",
                                     "from", "to", "through", "rise_from",
                                     "rise_to", "fall_from", "fall_to"):
                            parts = _split_braced_list(str(nv))
                            opt.values.extend(parts)
                            consumed_one_value = True
                            consumed_brace = True
                        else:
                            opt.values.append(str(nv))
                            consumed_one_value = True
                            consumed_brace = True
                        i += 1
                        opt.token_end = i
                    elif isinstance(nv, _CmdSubstValue):
                        # Command substitutions are target collections.
                        # List/path/group flags and explicit scalar flags
                        # that semantically take an object (like -source,
                        # -master_clock, -clock) absorb one substitution.
                        list_like = ("from", "to", "through", "group",
                                     "rise_from", "rise_to", "fall_from", "fall_to")
                        scalar_obj_flags = {
                            # Per-command: which scalar flags take an object
                            # expressed as [get_* ...]?
                            "create_clock": {"name"},
                            "create_generated_clock": {"source", "master_clock", "name"},
                            "set_input_delay": {"clock", "reference_pin"},
                            "set_output_delay": {"clock", "reference_pin"},
                            "set_clock_latency": {"clock"},
                            "set_clock_transition": {"clock"},
                            "set_input_transition": {"clock"},
                        }
                        obj_flags_for_cmd = scalar_obj_flags.get(cmd_name, set())
                        if flag in list_like:
                            opt.values.append(nv)
                            consumed_one_value = True
                            i += 1
                            opt.token_end = i
                        elif flag in obj_flags_for_cmd:
                            opt.values.append(nv)
                            consumed_one_value = True
                            i += 1
                            opt.token_end = i
                        else:
                            # Scalar numeric/string flags (e.g. -period)
                            # never take a command substitution as a value;
                            # leave it for positional processing.
                            stop_due_to_cmd_subst = True
                            break
                    else:
                        opt.values.append(nv)
                        consumed_one_value = True
                        i += 1
                        opt.token_end = i
                    # Scalar flags (non-list, non-waveform/edges) stop
                    # after a single value or after one brace group.
                    if flag not in known_lists and flag not in {"edges", "edge_shift"}:
                        if consumed_brace or (consumed_one_value and flag != "waveform"):
                            break
                        if flag == "waveform" and consumed_brace:
                            break
                opts.append(opt)
                if stop_due_to_cmd_subst:
                    # Do NOT advance i — outer loop will process the
                    # CmdSubst as positional on the next iteration.
                    continue
            else:
                positional.append(sval)
                i += 1

        return SdcCommand(
            command=cmd_name,
            options=opts,
            positional=positional,
            source_file=source_file,
            source_line_start=start_line,
            source_line_end=end_line,
            original_text=original_text,
            tokens=list(tokens),
        )

    _GLOBAL_BOOLEAN_FLAGS = {
        "add_delay", "clock_fall", "rise", "fall", "min", "max",
        "setup", "hold", "start", "end", "invert", "combinational",
        "add", "asynchronous", "physically_exclusive", "logically_exclusive",
        "reset_path",
    }

    # Per-command overrides: flags that are boolean for the listed commands
    # only (avoids treating -source as boolean for create_generated_clock,
    # where it takes a pin name).
    _PER_CMD_BOOLEAN_FLAGS: dict[str, set[str]] = {
        "set_clock_latency": {"source", "early", "late"},
        "set_clock_uncertainty": {"rise", "fall", "min", "max", "setup", "hold"},
        "set_input_transition": {"rise", "fall", "min", "max"},
    }

    def _is_boolean_default(self, flag: str, cmd_name: str) -> bool:
        if flag in self._GLOBAL_BOOLEAN_FLAGS:
            return True
        return flag in self._PER_CMD_BOOLEAN_FLAGS.get(cmd_name, set())
