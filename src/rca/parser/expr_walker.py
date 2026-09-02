"""
Expression walker for pyslang.

Walks an AST expression and collects every *named signal* referenced in
it, together with a ``DependencyKind`` that reflects the signal's
*semantic role* as determined by the **caller's context**:

* ``DependencyKind.DATA``        — the reference contributes to a *value*
  (RHS of an assignment, operand of any operator, branch of a ternary,
  concat element, part-select index, …).  Comparison / logical ops on
  the RHS produce DATA refs (they compute the loaded value).
* ``DependencyKind.MUX_SELECT``  — the reference is the *selector* of a
  ternary ``? :`` expression.
* ``DependencyKind.CONDITIONAL`` — the reference appears in a
  *procedural predicate* (``if``/``case``/``while`` condition).
* ``DependencyKind.CONCATENATION`` / ``PART_SELECT`` — structural ops
  (treated as DATA for cone purposes but tagged for debugging).

Key design rule: the operator by itself does NOT force classification.
``a == b`` is DATA on the RHS of ``q <= a == b`` (it computes the 1-bit
value loaded), MUX_SELECT when it is the selector of a ternary, and
CONDITIONAL when it is an ``if`` predicate.  The caller sets the
initial kind; the walker overrides it only at nodes that intrinsically
change role (ternary selector, ternary value branches).

Additional invariants:

* Only names that can be resolved to a declared signal are emitted.
* Integer literals, parameter constants, enum values are skipped.
* Unknown nodes are descended into recursively (inheriting caller's kind).
* Clock/reset are classified by the caller, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..design_model import SourceLocation
from ..utils.enums import DependencyKind


# Expression kinds for part/element select and concatenation:
_SELECT_KINDS = {
    "RangeSelectExpression",
    "ElementSelectExpression",
    "MemberAccessExpression",
}
_CONCAT_KINDS = {
    "ConcatenationExpression",
    "ReplicationExpression",
}


@dataclass
class ExprRef:
    """A reference to a named signal inside an expression."""
    name: str                                # local (unqualified) name
    kind: DependencyKind = DependencyKind.DATA
    op: str | None = None                    # contextual operator for debug


@dataclass
class ExprWalkResult:
    refs: list[ExprRef] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)   # LHS names
    is_nonblocking: bool = False

    def names(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for r in self.refs:
            if r.name and r.name not in seen:
                seen.add(r.name)
                out.append(r.name)
        return out

    def data_names(self) -> list[str]:
        """Names contributing to the *value* (D-cone)."""
        seen: set[str] = set()
        out: list[str] = []
        for r in self.refs:
            if r.kind in _DATA_KINDS and r.name not in seen:
                seen.add(r.name)
                out.append(r.name)
        return out

    def control_names(self) -> list[str]:
        """Names contributing to *control flow* (if/case/ternary-select)."""
        seen: set[str] = set()
        out: list[str] = []
        for r in self.refs:
            if r.kind in _CONTROL_KINDS and r.name not in seen:
                seen.add(r.name)
                out.append(r.name)
        return out


# DependencyKinds treated as data vs control.  Exposed for reuse by
# callers that need the same split.
_DATA_KINDS = frozenset({
    DependencyKind.DATA,
    DependencyKind.CONCATENATION,
    DependencyKind.PART_SELECT,
})
_CONTROL_KINDS = frozenset({
    DependencyKind.CONDITIONAL,
    DependencyKind.MUX_SELECT,
})


class ExprWalker:
    """Collects signal references from a pyslang expression tree."""

    def __init__(self, sm: Any | None = None) -> None:
        self._sm = sm
        self.refs: list[ExprRef] = []

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def walk(self, node: Any, kind: DependencyKind = DependencyKind.DATA,
             op: str | None = None) -> None:
        """Recursively walk ``node`` and append discovered references.

        ``kind`` is the semantic role the caller assigns to this
        subtree.  It propagates unchanged to children everywhere
        except at a ternary (``ConditionalExpression``):

        * the ternary's selector(s) are intrinsically MUX_SELECT;
        * the ternary's branches inherit the caller's ``kind`` — DATA
          when walking an RHS value (``q <= sel ? a : b`` → a/b DATA),
          CONDITIONAL when walking a procedural predicate
          (``if (sel ? a : b)`` → a/b are control that decides
          whether the enclosing assignment fires).

        No other operator forces a classification change: comparisons
        (``==``, ``<``) and logical ops (``&&``, ``||``) are DATA on
        the RHS, CONDITIONAL inside an if/case predicate, and
        MUX_SELECT when they compute the selector of a ternary —
        purely because the *caller* chose that role.
        """
        if node is None:
            return
        nm = type(node).__name__

        # --- Leaf: named reference ---
        if nm == "NamedValueExpression":
            sname = _resolve_name(node)
            if sname:
                self.refs.append(ExprRef(name=sname, kind=kind, op=op))
            return

        # --- Hierarchical / dotted reference (e.g., mod.sig) ---
        if nm == "MemberAccessExpression":
            mem_name = _member_name(node)
            if mem_name:
                self.refs.append(ExprRef(name=mem_name, kind=kind, op="."))
            for child in _children(node, ("value", "expr", "operand")):
                self.walk(child, kind=kind, op=".")
            return

        # --- Part/element select: sig[expr] / sig[hi:lo] ---
        if nm in _SELECT_KINDS:
            value = _getattr_any(node, ("value", "expr", "operand"))
            if value is not None:
                self.walk(value, kind=DependencyKind.PART_SELECT, op="[]")
            # Selector indices are DATA reads (e.g. addr[idx]).
            for child in _children(node, ("selector", "left", "right",
                                          "start", "end", "index")):
                if child is value:
                    continue
                self.walk(child, kind=DependencyKind.DATA, op="[]")
            return

        # --- Concatenation / replication ---
        if nm in _CONCAT_KINDS:
            cop = "{}" if nm == "ConcatenationExpression" else "{{}}"
            for child in _children(node, ("operands", "concatenant", "concat",
                                          "count", "expr", "value")):
                self.walk(child, kind=DependencyKind.CONCATENATION, op=cop)
            return

        # --- Conditional / ternary (pred ? t : f) ---
        # The selector(s) are intrinsically MUX_SELECT (they pick which
        # branch drives the result).  The branches inherit the caller's
        # ``kind`` so that ``q <= sel ? a : b`` classifies a/b as DATA
        # while ``if (sel ? a : b) q <= d`` (called with
        # kind=CONDITIONAL) classifies a/b as CONDITIONAL control.
        if nm == "ConditionalExpression":
            for pred in _getattr_list(node, "conditions"):
                self.walk(pred, kind=DependencyKind.MUX_SELECT, op="?:")
            pred = _getattr_any(node, ("predicate", "condition"))
            if pred is not None:
                self.walk(pred, kind=DependencyKind.MUX_SELECT, op="?:")
            branch_kind = DependencyKind.DATA if kind == DependencyKind.DATA else kind
            self.walk(getattr(node, "left", None), kind=branch_kind, op="?:")
            self.walk(getattr(node, "right", None), kind=branch_kind, op="?:")
            return

        # --- Binary expressions ---
        # Operands inherit the caller's kind.  Comparisons/logical ops
        # do NOT force CONDITIONAL — that depends on context (RHS value
        # vs if-predicate).
        if nm == "BinaryExpression":
            op_str = _binary_op_name(node)
            self.walk(getattr(node, "left", None), kind=kind, op=op_str)
            self.walk(getattr(node, "right", None), kind=kind, op=op_str)
            return

        # --- Unary ---
        if nm == "UnaryExpression":
            op_str = getattr(node, "op", None)
            self.walk(getattr(node, "operand", None), kind=kind,
                      op=str(op_str))
            return

        # --- Conversion/cast ---
        if nm == "ConversionExpression":
            self.walk(getattr(node, "operand", None), kind=kind)
            return

        # --- Inside / set / pattern / streaming ---
        if nm in ("InsideExpression", "PatternExpression",
                  "AssignmentPatternExpression",
                  "StreamingConcatenationExpression"):
            for child in _all_children(node):
                self.walk(child, kind=kind, op=nm)
            return

        # --- Assignment expression (top-level); generic descent ---
        if nm == "AssignmentExpression":
            self.walk(getattr(node, "left", None), kind=kind)
            self.walk(getattr(node, "right", None), kind=kind)
            return

        # --- Literals / placeholders: skip ---
        if nm in ("IntegerLiteral", "IntegerLiteralExpression",
                  "RealLiteral", "TimeLiteral",
                  "UnbasedUnsizedIntegerLiteral", "NullLiteral",
                  "StringLiteral", "EmptyArgumentExpression",
                  "LValueReference"):
            return

        # --- Default: descend into all recognized children, preserving kind ---
        for child in _all_children(node):
            self.walk(child, kind=kind)


def walk_expression(node: Any, sm: Any | None = None) -> ExprWalkResult:
    """Walk a standalone expression (default DATA context) and collect refs."""
    w = ExprWalker(sm=sm)
    w.walk(node)
    return ExprWalkResult(refs=w.refs)


def walk_predicate(node: Any, sm: Any | None = None) -> ExprWalkResult:
    """Walk a procedural predicate (``if``/``case``/``while`` condition).

    All signal refs inside the predicate are classified CONDITIONAL
    *except* selectors of ternaries inside the predicate, which remain
    MUX_SELECT (they are part of the predicate computation).
    """
    w = ExprWalker(sm=sm)
    w.walk(node, kind=DependencyKind.CONDITIONAL)
    return ExprWalkResult(refs=w.refs)


def walk_assignment(node: Any, sm: Any | None = None) -> ExprWalkResult:
    """Walk an ``AssignmentExpression``; split LHS targets from RHS sources.

    The RHS is walked in a DATA context, so RHS comparisons / logical
    ops correctly classify their operands as DATA (they compute the
    value being assigned).
    """
    res = ExprWalkResult()
    if node is None:
        return res
    nm = type(node).__name__
    if nm == "AssignmentExpression":
        lhs_w = ExprWalker(sm=sm)
        lhs_w.walk(getattr(node, "left", None))
        res.targets = _filter_targets(lhs_w.refs)
        # Walk RHS in DATA context (value expression).
        rhs_w = ExprWalker(sm=sm)
        rhs_w.walk(getattr(node, "right", None), kind=DependencyKind.DATA)
        res.refs = rhs_w.refs
        # Part-select indices on LHS (e.g. q[i] <= ...) read ``i`` as DATA.
        for r in lhs_w.refs:
            if r.name not in res.targets:
                res.refs.append(r)
        try:
            res.is_nonblocking = bool(getattr(node, "isNonBlocking", False))
        except Exception:
            res.is_nonblocking = False
    else:
        w = ExprWalker(sm=sm)
        w.walk(node, kind=DependencyKind.DATA)
        res.refs = w.refs
    return res


def source_location(sm: Any | None, sym: Any) -> SourceLocation | None:
    try:
        loc = getattr(sym, "location", None)
        if loc is None or sm is None:
            return None
        fname = sm.getFileName(loc)
        line = sm.getLineNumber(loc)
        col = sm.getColumnNumber(loc)
        if fname:
            return SourceLocation(file=fname, line=line, column=col)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _filter_targets(refs: list[ExprRef]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in refs:
        if r.name and r.name not in seen:
            seen.add(r.name)
            out.append(r.name)
    return out


def _resolve_name(nv: Any) -> str | None:
    """Return the declared signal name for a NamedValueExpression, or None
    for literal-like symbols (parameters, enums)."""
    try:
        sym = nv.symbol
    except Exception:
        return None
    if _is_literal_like(sym):
        return None
    try:
        return getattr(sym, "name", None)
    except Exception:
        return None


def _member_name(node: Any) -> str | None:
    try:
        if hasattr(node, "member"):
            m = node.member
            return getattr(m, "name", None)
    except Exception:
        pass
    return getattr(node, "name", None)


def _is_literal_like(sym: Any) -> bool:
    """Parameter/enum-value/type-parameter symbols carry compile-time
    constants and should NOT produce signal edges."""
    try:
        kn = type(sym).__name__
    except Exception:
        return False
    return kn in ("ParameterSymbol", "EnumValueSymbol",
                  "TypeParameterSymbol", "MethodPrototypeSymbol",
                  "MethodPrototype")


def _getattr_any(obj: Any, names: tuple[str, ...]) -> Any:
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return None


def _getattr_list(obj: Any, name: str) -> list[Any]:
    v = getattr(obj, name, None)
    if v is None:
        return []
    try:
        return [x for x in list(v) if x is not None]
    except Exception:
        return []


def _children(obj: Any, attr_names: tuple[str, ...]) -> list[Any]:
    out: list[Any] = []
    for n in attr_names:
        v = getattr(obj, n, None)
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            for x in v:
                if x is not None:
                    out.append(x)
        else:
            out.append(v)
    return out


def _all_children(obj: Any) -> list[Any]:
    """Generic fallback: pull child-like attributes that aren't simple scalars."""
    SKIP = {"kind", "as", "op", "isNegated", "isUnbounded", "isVirtual",
            "isConstraint", "isTopLevel", "isDefault", "flags", "parent",
            "location", "symbol", "name", "type", "scope", "parentScope",
            "sourceRange", "syntax", "source", "hierarchicalPath",
            "lexicalPath", "designTree"}
    out: list[Any] = []
    for n in dir(obj):
        if n.startswith("_") or n in SKIP:
            continue
        try:
            v = getattr(obj, n)
        except Exception:
            continue
        if callable(v):
            continue
        if isinstance(v, (str, int, float, bool)):
            continue
        try:
            if hasattr(v, "kind"):
                out.append(v)
                continue
        except Exception:
            pass
        if isinstance(v, (list, tuple)):
            for x in v:
                if hasattr(x, "kind"):
                    out.append(x)
    return out


def _binary_op_name(node: Any) -> str:
    """Map pyslang BinaryOperator enum value to a short symbolic string."""
    try:
        o = node.op
        # pyslang enum __str__ looks like "BinaryOperator.Add"; we strip
        # both "BinaryOperator." prefix and "Binary" prefix word.
        s = (str(o).split(".")[-1]
             .lower()
             .replace("binary", "")
             .replace("operator", ""))
        mapping = {
            # Arithmetic
            "add": "+", "subtract": "-", "sub": "-",
            "multiply": "*", "mul": "*",
            "divide": "/", "div": "/",
            "mod": "%", "modulus": "%",
            "power": "**",
            # Bitwise (after strip("binary"): "and"/"or"/"xor"/"nand"/"nor"/"xnor")
            "and": "&", "or": "|", "xor": "^",
            "nand": "~&", "nor": "~|", "xnor": "^~",
            # Logical
            "logicaland": "&&", "logicalor": "||",
            "logicalimplication": "->", "logicalequivalence": "<->",
            "implies": "->", "equiv": "<->",
            # Equality.  NB: pyslang uses "Equality" for == and
            # "CaseEquality" for === (after lower+strip: "equality" /
            # "caseequality").  Older pyslang versions used "eq"/"ceq".
            "equality": "==", "inequality": "!=",
            "caseequality": "===", "caseinequality": "!==",
            "wildcardequality": "==?", "wildcardinequality": "!=?",
            "eq": "==", "neq": "!=", "ceq": "===", "cneq": "!==",
            "wildcardeq": "==?", "wildcardneq": "!=?",
            # Relational
            "lessthan": "<", "greaterthan": ">",
            "lessthanequal": "<=", "greaterthanequal": ">=",
            "lt": "<", "gt": ">", "le": "<=", "ge": ">=",
            # Shift
            "logicalshiftleft": "<<", "logicalshiftright": ">>",
            "arithmeticshiftleft": "<<<", "arithmeticshiftright": ">>>",
            "lshift": "<<", "rshift": ">>",
            "ashiftl": "<<<", "ashiftr": ">>>",
            # Unary (handled in UnaryExpression; listed for safety)
            "logicalnot": "!", "bitwisenot": "~", "not": "~",
            "bnot": "~", "negate": "-",
            "andreduction": "&", "orreduction": "|", "xorreduction": "^",
            "nandreduction": "~&", "norreduction": "~|",
            "xnorreduction": "^~",
        }
        return mapping.get(s, s)
    except Exception:
        return "?"
