"""Target collection semantics for SDC imports (Step 5 §4, §5).

A target collection represents something like::

    [get_ports foo]
    [get_pins {U1/A U2/B}]
    [get_clocks -include_generated_clocks clk*]
    [all_inputs]
    foo            (literal name)

We retain:
    collection_kind     PORT/PIN/CELL/NET/CLOCK/REGISTER/ALL_*/LITERAL/...
    expression          the original selector text
    pattern             the pattern argument (if any)
    filters             option key/value pairs (when recognized)
    resolved_objects    list of resolved object names (after design-aware resolution)
    resolution_status   RESOLVED/PATTERN/UNRESOLVED
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Iterable, TYPE_CHECKING

from ..utils.enums import CollectionKind, ResolutionStatus

if TYPE_CHECKING:
    from ..design_model import Design
    from ..timing_model import TimingGraph


# Subset of SDC collection commands we understand how to resolve.
_GET_KIND_MAP = {
    "get_ports": CollectionKind.PORT,
    "get_pins": CollectionKind.PIN,
    "get_cells": CollectionKind.CELL,
    "get_nets": CollectionKind.NET,
    "get_clocks": CollectionKind.CLOCK,
    "all_inputs": CollectionKind.ALL_INPUTS,
    "all_outputs": CollectionKind.ALL_OUTPUTS,
    "all_clocks": CollectionKind.ALL_CLOCKS,
    "all_registers": CollectionKind.ALL_REGISTERS,
    # current_instance is not a direct collection; treated as unresolved.
}


@dataclass
class TargetCollection:
    collection_kind: CollectionKind
    expression: str
    pattern: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    arguments: list[str] = field(default_factory=list)
    resolved_objects: list[str] = field(default_factory=list)
    resolution_status: ResolutionStatus = ResolutionStatus.UNRESOLVED
    unresolved_reason: str | None = None
    # Raw text for provenance.
    raw: str = ""

    # ---------- serialization ----------
    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_kind": self.collection_kind.value,
            "expression": self.expression,
            "pattern": self.pattern,
            "filters": dict(self.filters),
            "arguments": list(self.arguments),
            "resolved_objects": sorted(self.resolved_objects),
            "resolution_status": self.resolution_status.value,
            "unresolved_reason": self.unresolved_reason,
            "raw": self.raw,
        }

    @classmethod
    def literal(cls, name: str) -> "TargetCollection":
        return cls(collection_kind=CollectionKind.LITERAL,
                   expression=name, pattern=name,
                   resolved_objects=[name], resolution_status=ResolutionStatus.RESOLVED,
                   raw=name)

    @classmethod
    def unresolved_expr(cls, expr: str, reason: str = "unsupported Tcl expression") -> "TargetCollection":
        return cls(collection_kind=CollectionKind.EXPR,
                   expression=expr, resolution_status=ResolutionStatus.UNRESOLVED,
                   unresolved_reason=reason, raw=expr)


# ---------------------------------------------------------------------------
# Parsing helpers (convert a raw value from the option/positional list into
# a TargetCollection).
# ---------------------------------------------------------------------------


def parse_target_value(raw: Any) -> TargetCollection:
    """Given a raw value from the SdcParser (string, _CmdSubstValue, or
    _BraceValue), return a :class:`TargetCollection` without any
    design resolution (just lexical/syntactic parsing).
    """
    # Import here to avoid circular imports.
    from .parser import _BraceValue, _CmdSubstValue, _split_braced_list  # noqa: F401

    if isinstance(raw, _CmdSubstValue):
        return _parse_collection_expr(raw.inner.strip(), raw=str(raw))
    if isinstance(raw, _BraceValue):
        items = _split_braced_list(str(raw))
        return _braced_list_collection(items, raw=str(raw))
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("[") and s.endswith("]"):
            return _parse_collection_expr(s[1:-1].strip(), raw=s)
        return TargetCollection.literal(s)
    return TargetCollection.unresolved_expr(str(raw))


def _braced_list_collection(items: list[str], raw: str) -> TargetCollection:
    # A brace list can be a list of literal names or nested [get_*] calls.
    # If any item is a nested collection, we can't reduce to a single
    # kind; fall back to EXPR with the items enumerated.
    if any(it.strip().startswith("[") for it in items):
        return TargetCollection.unresolved_expr(raw,
            reason="brace list contains nested command substitution; resolved separately")
    # Otherwise treat as list of literals.
    return TargetCollection(
        collection_kind=CollectionKind.LITERAL,
        expression=raw,
        pattern=None,
        arguments=list(items),
        resolved_objects=sorted(set(items)),
        resolution_status=ResolutionStatus.RESOLVED,
        raw=raw,
    )


def _parse_collection_expr(inner: str, raw: str) -> TargetCollection:
    """Parse the inside of [ ... ] for a supported collection command.

    Returns EXPR/UNRESOLVED if it contains anything we don't safely support
    (nested command substitutions, variable references, other commands).
    """
    # Import here to avoid a module-init cycle with .parser.
    from .parser import (
        SdcParser as _InnerSdcParser,
        _BraceValue, _stringify_token, _split_braced_list,
    )
    inner = inner.strip()
    if not inner:
        return TargetCollection.unresolved_expr(raw, reason="empty command substitution")
    # Safety: refuse to lex/parse any inner command invocation if it is
    # not a known safe collection command. We do NOT execute inner code.
    first_word = inner.split(None, 1)[0]
    if first_word not in _GET_KIND_MAP and first_word != "list":
        # Security: explicitly refuse exec/source/eval etc. with a clear reason.
        if first_word in ("exec", "source", "eval", "open", "glob", "pid", "file", "unknown",
                           "clock", "after", "catch", "expr", "if", "for", "foreach", "while",
                           "proc", "rename", "uplevel", "namespace"):
            return TargetCollection(
                collection_kind=CollectionKind.EXPR,
                expression=raw,
                resolution_status=ResolutionStatus.UNRESOLVED,
                unresolved_reason=f"disallowed Tcl command '{first_word}' (never executed)",
                raw=raw,
            )
        return TargetCollection.unresolved_expr(raw,
            reason=f"unsupported collection command '{first_word}'")
    # Parse the inner command text as a single SDC command.
    inner_parser = _InnerSdcParser()
    result = inner_parser.parse_text(inner + "\n", source_file="<collection>")
    if not result.parsed.commands:
        return TargetCollection.unresolved_expr(raw, reason="could not parse collection expression")
    if len(result.parsed.commands) > 1:
        return TargetCollection.unresolved_expr(raw, reason="multiple commands in substitution")
    cmd = result.parsed.commands[0]
    if cmd.command == "list":
        # [list a b c] is equivalent to {a b c} — literals only.
        items = [str(x) for x in cmd.positional]
        # Check none are further substitutions
        return TargetCollection(
            collection_kind=CollectionKind.LITERAL,
            expression=raw,
            arguments=items,
            resolved_objects=sorted(set(items)),
            resolution_status=ResolutionStatus.RESOLVED,
            raw=raw,
        )
    kind = _GET_KIND_MAP[cmd.command]
    # Separate pattern/filter options.
    args: list[str] = []
    patterns: list[str] = []
    filters: dict[str, Any] = {}
    # Patterns are positional words (after the command name) that are
    # not the result of another [ ... ] substitution — we accept up to
    # one pattern per get_* (SDC usually takes one, except get_pins/cells
    # -of_objects which is handled separately).
    has_nested_subst = False
    for pv in cmd.positional:
        s = str(pv)
        if s.startswith("["):
            has_nested_subst = True
            continue
        if isinstance(pv, _BraceValue):
            items = _split_braced_list(str(pv))
            patterns.extend(items)
        else:
            patterns.append(s)
    # Capture boolean/value options as filters (e.g. -hierarchical,
    # -include_generated_clocks, -of_objects). We do NOT attempt to
    # interpret -filter Tcl expressions.
    for o in cmd.options:
        if o.is_boolean_switch:
            filters[o.name] = True
        else:
            filters[o.name] = o.values[0] if len(o.values) == 1 else list(o.values)
    if "filter" in filters:
        return TargetCollection(
            collection_kind=kind, expression=raw, pattern=None,
            filters=filters, arguments=patterns,
            resolution_status=ResolutionStatus.UNRESOLVED,
            unresolved_reason="-filter expressions not supported", raw=raw,
        )
    if has_nested_subst:
        return TargetCollection(
            collection_kind=kind, expression=raw, pattern=patterns[0] if patterns else None,
            filters=filters, arguments=patterns,
            resolution_status=ResolutionStatus.UNRESOLVED,
            unresolved_reason="nested command substitution is not executed", raw=raw,
        )
    return TargetCollection(
        collection_kind=kind, expression=raw,
        pattern=patterns[0] if patterns else None,
        filters=filters, arguments=patterns, raw=raw,
        resolution_status=ResolutionStatus.PATTERN if patterns else ResolutionStatus.RESOLVED,
    )


# ---------------------------------------------------------------------------
# Design-aware resolver
# ---------------------------------------------------------------------------


class DesignResolver:
    """Resolve :class:`TargetCollection` against a Design + TimingGraph.

    Populates ``resolved_objects`` in-place.  Safe to use with no design
    (all resolutions return UNRESOLVED with the pattern preserved).
    """

    def __init__(self, design: "Design | None" = None,
                 tg: "TimingGraph | None" = None) -> None:
        self.design = design
        self.tg = tg
        self._port_index: set[str] = set()
        self._net_index: set[str] = set()
        self._cell_index: set[str] = set()
        self._register_index: set[str] = set()
        self._clock_index: set[str] = set()
        if design is not None:
            self._port_index = {p.local_name for p in design.top_ports()}
            for p in design.top_ports():
                self._port_index.add(p.hierarchical_name)
            self._net_index = {n.local_name for n in design.nets_of(design.top_module)}
            self._register_index = {r.local_name for r in design.top_registers()}
            for r in design.top_registers():
                self._register_index.add(r.hierarchical_name)
            self._cell_index = {i.instance_name for i in design.instances_of(design.top_module)}
        if tg is not None:
            self._clock_index = set(tg.clocks.keys())

    # ------------------------------------------------------------------
    def resolve(self, tc: TargetCollection) -> TargetCollection:
        """Return a new TargetCollection with resolved_objects populated
        when possible.  Does not mutate the original."""
        out = TargetCollection(
            collection_kind=tc.collection_kind,
            expression=tc.expression,
            pattern=tc.pattern,
            filters=dict(tc.filters),
            arguments=list(tc.arguments),
            resolved_objects=list(tc.resolved_objects),
            resolution_status=tc.resolution_status,
            unresolved_reason=tc.unresolved_reason,
            raw=tc.raw,
        )
        # Already-resolved literals keep their objects.
        if out.resolution_status == ResolutionStatus.RESOLVED and out.resolved_objects:
            return out
        kind = out.collection_kind
        if kind == CollectionKind.LITERAL and out.pattern:
            return out
        if kind == CollectionKind.EXPR:
            return out
        if self.design is None and kind not in (
            CollectionKind.ALL_CLOCKS,
        ):
            return out
        # universe of candidate names for this kind
        universe = self._universe_for(kind)
        if universe is None:
            out.unresolved_reason = out.unresolved_reason or f"resolution for {kind.value} not available without design"
            out.resolution_status = ResolutionStatus.UNRESOLVED
            return out
        if kind in (CollectionKind.ALL_INPUTS, CollectionKind.ALL_OUTPUTS,
                    CollectionKind.ALL_CLOCKS, CollectionKind.ALL_REGISTERS):
            out.resolved_objects = sorted(universe)
            out.resolution_status = ResolutionStatus.RESOLVED
            return out
        patterns = list(out.arguments) if out.arguments else ([out.pattern] if out.pattern else [])
        if not patterns:
            out.unresolved_reason = "no pattern supplied"
            out.resolution_status = ResolutionStatus.UNRESOLVED
            return out
        found: list[str] = []
        matched_any = False
        for pat in patterns:
            if not pat:
                continue
            if "*" in pat or "?" in pat or "[" in pat:
                matches = sorted(n for n in universe if fnmatch.fnmatchcase(n, pat))
                if matches:
                    matched_any = True
                    found.extend(matches)
            else:
                if pat in universe:
                    matched_any = True
                    found.append(pat)
                # Also accept pin hierarchies like U1/A (fnmatch on *).
                elif "/" in pat and any(
                    n == pat or n.endswith("/" + pat.split("/")[-1]) and fnmatch.fnmatchcase(n, pat)
                    for n in universe
                ):
                    matches = sorted(n for n in universe if n == pat)
                    found.extend(matches)
                    matched_any = True
                else:
                    # Pattern is a literal name not present in design:
                    # preserve as unresolved.
                    found.append(pat)
        if not matched_any:
            out.resolution_status = ResolutionStatus.UNRESOLVED
            out.unresolved_reason = out.unresolved_reason or "no objects matched"
        else:
            out.resolved_objects = sorted(set(found))
            if any("*" in p or "?" in p for p in patterns):
                out.resolution_status = ResolutionStatus.RESOLVED
            else:
                out.resolution_status = ResolutionStatus.RESOLVED if matched_any else ResolutionStatus.UNRESOLVED
        return out

    # ------------------------------------------------------------------
    def _universe_for(self, kind: CollectionKind) -> set[str] | None:
        if kind == CollectionKind.PORT or kind == CollectionKind.ALL_INPUTS:
            if self.design is None:
                return None
            return {p.local_name for p in self.design.top_ports()
                    if p.direction.value in ("input", "inout")} \
                if kind == CollectionKind.ALL_INPUTS else self._port_index
        if kind == CollectionKind.ALL_OUTPUTS:
            if self.design is None:
                return None
            return {p.local_name for p in self.design.top_ports()
                    if p.direction.value in ("output", "inout")}
        if kind == CollectionKind.NET:
            return self._net_index
        if kind == CollectionKind.CELL:
            return self._cell_index
        if kind == CollectionKind.REGISTER or kind == CollectionKind.ALL_REGISTERS:
            return self._register_index
        if kind == CollectionKind.CLOCK or kind == CollectionKind.ALL_CLOCKS:
            return self._clock_index
        if kind == CollectionKind.PIN:
            # We do not maintain a flat pin index in this build; return
            # empty so resolution is preserved as PATTERN/UNRESOLVED.
            return set()
        return None

    # ------------------------------------------------------------------
    def resolve_many(self, tcs: Iterable[TargetCollection]) -> list[TargetCollection]:
        return [self.resolve(tc) for tc in tcs]
