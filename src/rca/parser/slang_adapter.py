"""
pyslang-based SystemVerilog parser adapter (Manual §5.2).

Uses the slang compiler via its Python bindings (pyslang) to parse,
type-check and elaborate SystemVerilog designs, then walks the
elaborated AST to build RCA's normalized Design model.

Connectivity-aware model (Step-1 corrective engineering):
* Continuous assignments produce real ``CombEdge`` records for every RHS
  signal (not just a count).
* Procedural blocks populate ``assigned_signals`` (LHS), ``read_signals``
  (every RHS/condition signal), ``control_signals`` (conditions/selects),
  and ``clock_signals``/``reset_signals`` (posedge/negedge controls) —
  all separated from ordinary data.
* Register ``data_sources`` list every signal in the non-reset
  non-blocking assignment cone; ``q_consumers`` is populated later by
  ``Design.build_connectivity``.
* Instance port connections are extracted via ``InstanceSymbol.portConnections``
  and stored as ``HierPortConn`` records; cross-module CombEdges are
  projected later by the connectivity builder.
* Source locations are propagated where available.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..design_model import (
    CombEdge,
    Design,
    HierPortConn,
    Instance,
    Module,
    Net,
    Port,
    Process,
    Register,
    SensitivityItem,
    SourceLocation,
)
from ..utils.enums import (
    ClockEdge,
    DependencyKind,
    ErrorCode,
    PortDirection,
    ResetPolarity,
    ResetType,
)
from ..utils.logging import get_logger
from .base import ParserAdapter
from .diagnostics import Diagnostic, Severity
from .expr_walker import (
    ExprRef,
    ExprWalkResult,
    ExprWalker,
    source_location,
    walk_assignment,
)

try:
    from pyslang import Bag, SourceManager
    from pyslang.ast import (
        ArgumentDirection,
        Compilation,
        CompilationOptions,
        EdgeKind,
        SignalEventControl,
        SymbolKind,
        VisitAction,
    )
    from pyslang.parsing import ParserOptions, PreprocessorOptions
    from pyslang.syntax import SyntaxTree
    _SLANG_AVAILABLE = True
except ImportError:  # pragma: no cover
    _SLANG_AVAILABLE = False
    Compilation = None  # type: ignore

log = get_logger("parser.slang")


class SlangAdapter(ParserAdapter):
    """Parser adapter backed by slang (pyslang bindings)."""

    name = "slang"

    def __init__(self) -> None:
        super().__init__()
        if not _SLANG_AVAILABLE:
            raise RuntimeError(
                "pyslang is not installed. Install it via: pip install pyslang"
            )
        self._sm = SourceManager()

    def parse(
        self,
        files: list[str | Path],
        include_dirs: list[str | Path] | None = None,
        defines: dict[str, str] | None = None,
        top: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> Design:
        if not files:
            raise ValueError("No source files provided")

        abs_files = [str(Path(f).resolve()) for f in files]
        for f in abs_files:
            if not Path(f).is_file():
                self.diagnostics.add(Diagnostic(
                    code=ErrorCode.PARSER_ERROR,
                    severity=Severity.ERROR,
                    message=f"Source file not found: {f}",
                    file=f,
                ))

        include_dirs = [str(Path(d).resolve()) for d in (include_dirs or [])]
        defines = defines or {}

        log.info("Slang parsing %d files (includes=%d, defines=%d)",
                 len(abs_files), len(include_dirs), len(defines))

        bag = Bag()
        pp_opts = PreprocessorOptions()
        predef_lines: list[str] = []
        for k, v in defines.items():
            predef_lines.append(f"`define {k} {v}" if v else f"`define {k}")
        if predef_lines:
            pp_opts.predefineSource = "\n".join(predef_lines)
        if include_dirs:
            pp_opts.additionalIncludePaths = list(include_dirs)
        bag.preprocessorOptions = pp_opts

        parse_opts = ParserOptions()
        bag.parserOptions = parse_opts

        comp_opts = CompilationOptions()
        comp_opts.errorLimit = 1000
        if top:
            comp_opts.topModules = {top}
        bag.compilationOptions = comp_opts

        try:
            tree = SyntaxTree.fromFiles(abs_files, self._sm, bag)
        except Exception as e:
            self.diagnostics.add(Diagnostic(
                code=ErrorCode.PARSER_ERROR,
                severity=Severity.CRITICAL,
                message=f"SyntaxTree creation failed: {e}",
            ))
            raise

        for diag in tree.diagnostics:
            self._record_diagnostic(diag)

        comp = Compilation(bag)
        comp.addSyntaxTree(tree)

        root = comp.getRoot()
        for diag in comp.getAllDiagnostics():
            self._record_diagnostic(diag)

        top_instances = list(root.topInstances)
        if not top_instances:
            self.diagnostics.add(Diagnostic(
                code=ErrorCode.ELABORATION_ERROR,
                severity=Severity.ERROR,
                message="No top-level modules found after elaboration.",
            ))
            design_name = top or Path(abs_files[0]).stem
            return Design(name=design_name)

        chosen = (next((t for t in top_instances if t.name == top), top_instances[0])
                  if top else top_instances[0])

        design = Design(name=chosen.name, top_module=chosen.name, source_files=abs_files)
        self._walk_instance(chosen, design, parent_path="")

        clk_seed: set[str] = set()
        rst_seed: set[str] = set()
        for r in design.registers.values():
            if r.clock_signal:
                clk_seed.add(r.clock_signal.split(".")[-1])
            if r.reset_signal:
                rst_seed.add(r.reset_signal.split(".")[-1])
        design.clocks_seed = sorted(clk_seed)
        design.resets_seed = sorted(rst_seed)

        design.diagnostics = self.diagnostics.to_list()
        log.info("Slang elaboration complete: %s", design.summary())
        return design

    # ------------------------------------------------------------------
    # Hierarchy walker
    # ------------------------------------------------------------------

    def _walk_instance(self, inst, design: Design, parent_path: str) -> None:
        body = inst.body
        defn = body.definition
        hier_prefix = f"{parent_path}{inst.name}" if parent_path else inst.name
        module_name = defn.name

        if module_name not in design.modules:
            mod = Module(name=module_name, is_top=(hier_prefix == design.top_module))
            design.modules[module_name] = mod
        else:
            mod = design.modules[module_name]
            if hier_prefix == design.top_module:
                mod.is_top = True

        key = parent_path.rstrip(".") if parent_path else "__top__"
        design.hierarchy.setdefault(key, []).append(inst.name)

        self._collect_parameters(body, design, module_name)
        self._collect_ports(body, design, module_name, hier_prefix)
        self._collect_variables(body, design, module_name, hier_prefix)
        self._collect_procedural_blocks(body, design, module_name, hier_prefix)
        self._collect_continuous_assigns(body, design, module_name, hier_prefix)
        self._collect_sub_instances(body, design, module_name, hier_prefix)

        try:
            loc = defn.location
            fname = self._sm.getFileName(loc)
            if fname and fname not in mod.source_files:
                mod.source_files.append(fname)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Collectors
    # ------------------------------------------------------------------

    def _collect_parameters(self, body, design: Design, mod_name: str) -> None:
        mod = design.modules[mod_name]
        for sym in body.parameters:
            try:
                val = sym.value
                if hasattr(val, "value"):
                    val = val.value
                try:
                    val = int(val)
                except Exception:
                    try:
                        val = float(val)
                    except Exception:
                        val = str(val)
                mod.parameters[sym.name] = val
            except Exception:
                mod.parameters[sym.name] = None

    def _collect_ports(self, body, design: Design, mod_name: str, hier: str) -> None:
        mod = design.modules[mod_name]
        for port_sym in body.portList:
            pname = port_sym.name
            hier_name = f"{hier}.{pname}"
            mod.port_names.append(pname)
            try:
                direction = _convert_direction(port_sym.direction)
            except Exception:
                direction = PortDirection.INPUT
            width, width_spec, sig_type, net_kind = _type_info(port_sym)
            port = Port(
                hierarchical_name=hier_name,
                local_name=pname,
                direction=direction,
                width=width,
                width_spec=width_spec,
                datatype=sig_type,
                net_kind=net_kind,
                parent_module=mod_name,
                source_location=source_location(self._sm, port_sym),
            )
            design.ports[hier_name] = port

    def _collect_variables(self, body, design: Design, mod_name: str, hier: str) -> None:
        """Collect Net and Variable symbols (wires, regs, logic, etc.)."""
        def _add(v):
            try:
                vname = v.name
                hier_name = f"{hier}.{vname}"
                if hier_name in design.nets:
                    return
                w, ws, dt, nk = _type_info(v)
                is_in = (hier_name in design.ports
                         and design.ports[hier_name].direction in
                         (PortDirection.INPUT, PortDirection.INOUT))
                is_out = (hier_name in design.ports
                          and design.ports[hier_name].direction in
                          (PortDirection.OUTPUT, PortDirection.INOUT))
                net = Net(
                    hierarchical_name=hier_name,
                    local_name=vname,
                    parent_module=mod_name,
                    width=w,
                    net_kind=nk,
                    datatype=dt,
                    is_input=is_in,
                    is_output=is_out,
                    source_location=source_location(self._sm, v),
                )
                design.nets[hier_name] = net
            except Exception as e:
                log.debug("var collect error: %s", e)

        kinds = {SymbolKind.Net, SymbolKind.Variable}
        def _root(node):
            if hasattr(node, "kind") and node.kind == SymbolKind.Instance:
                return VisitAction.Skip
            if hasattr(node, "kind") and node.kind in kinds:
                _add(node)
            return VisitAction.Advance
        body.visit(f=_root)

    def _collect_procedural_blocks(self, body, design: Design, mod_name: str, hier: str) -> None:
        proc_counter = [0]

        def on_proc(pb):
            proc_counter[0] += 1
            proc_id = f"{hier}.proc{proc_counter[0]}"
            kind_name = str(pb.procedureKind).split(".")[-1].lower().replace("kind", "")
            if kind_name == "alwaysff":
                kind_name = "always_ff"
            elif kind_name == "alwayscomb":
                kind_name = "always_comb"
            elif kind_name == "alwayslatch":
                kind_name = "always_latch"

            proc = Process(
                id=proc_id,
                parent_module=mod_name,
                kind=kind_name,
                source_location=source_location(self._sm, pb),
            )

            # --- Extract sensitivity list (posedge/negedge control) ---
            # We do NOT use a positional heuristic ("first edge = clock,
            # second = reset").  Instead we collect every edge-sensitive
            # signal, then classify using structural evidence:
            #
            #  * A signal used as a posedge condition that is NOT negated
            #    in any if-predicate within the body is a CLOCK.
            #  * A signal that appears as a negated predicate (``!rst_n``
            #    or ``~rst_n``) in the top-level if-condition, paired
            #    with a posedge/negedge sensitivity, is an ASYNC RESET.
            #  * Any remaining edge-sensitive signals are recorded as
            #    clock candidates but we DO NOT silently assign one as
            #    clock vs reset — ambiguity is preserved.
            clk_signal: str | None = None
            clk_edge: ClockEdge = ClockEdge.POSEDGE
            rst_signal: str | None = None
            rst_edge: ClockEdge | None = None
            clk_ctrl_signals: list[str] = []
            rst_ctrl_signals: list[str] = []
            ambiguous_event_signals: list[str] = []
            sensitivity_events: list[tuple[str, ClockEdge]] = []

            # pb.body is a TimedStatement wrapping (event_control, stmt).
            walk_target = pb.body if hasattr(pb, "body") else pb
            wt_nm = type(walk_target).__name__
            if wt_nm == "TimedStatement":
                stmt = getattr(walk_target, "stmt", None)
                if stmt is not None:
                    walk_target = stmt

            # Gather all edge-sensitive signals from the sensitivity list.
            def on_event(node):
                if isinstance(node, SignalEventControl):
                    try:
                        sig_name = _extract_signal_name(node.expr)
                        if sig_name is None:
                            return VisitAction.Advance
                        edge = (ClockEdge.POSEDGE if node.edge == EdgeKind.PosEdge
                                else ClockEdge.NEGEDGE)
                        sensitivity_events.append((sig_name, edge))
                    except Exception:
                        pass
                return VisitAction.Advance

            try:
                pb.body.visit(f=on_event)
            except Exception as e:
                log.debug("event visit error: %s", e)

            # Detect reset signals by scanning top-level conditions in the
            # body for references to an edge-sensitive signal in a reset
            # branch pattern:
            #   - negated predicate + negedge sensitivity → active-low reset
            #   - plain (non-negated) predicate + posedge sensitivity → active-high reset
            # We require the predicate to be a direct NamedValueExpression
            # (no complex boolean), matching the canonical "if (rst)"/"if (!rst_n)".
            negated_predicate_signals = _detect_negated_predicates(walk_target)
            direct_predicate_signals = _detect_direct_predicate_signals(walk_target)

            # Build a set of (signal, edge) for edge-sensitive items.
            edge_sig_set = {s for s, _ in sensitivity_events}
            # Build per-signal edge lookup.
            edge_by_sig = {s: e for s, e in sensitivity_events}
            # Reset candidates:
            #  * negated-in-predicate + posedge/negedge in sensitivity (classic async)
            #  * plain-in-predicate + posedge sensitivity (active-high async)
            reset_candidates: list[str] = []
            for s in edge_sig_set:
                e = edge_by_sig[s]
                if s in negated_predicate_signals:
                    reset_candidates.append(s)
                elif s in direct_predicate_signals and e == ClockEdge.POSEDGE:
                    # Active-high async reset (if (rst) q<=0 with rst on posedge)
                    reset_candidates.append(s)
            clock_candidates = [s for s, _ in sensitivity_events if s not in reset_candidates]

            # Pick reset (must be edge-sensitive AND negated in predicate).
            if len(reset_candidates) == 1:
                rst_signal = reset_candidates[0]
                # Find the edge used in the sensitivity list for this signal
                for s, e in sensitivity_events:
                    if s == rst_signal:
                        rst_edge = e
                        break
                rst_hier = f"{hier}.{rst_signal}"
                rst_ctrl_signals.append(rst_hier)
            elif len(reset_candidates) > 1:
                log.debug("ambiguous async-reset candidates in %s: %s",
                          proc_id, reset_candidates)
                ambiguous_event_signals.extend(
                    f"{hier}.{s}" for s in reset_candidates)

            # Pick clock from remaining candidates.
            posedge_clocks = [s for s, e in sensitivity_events
                              if s not in reset_candidates and e == ClockEdge.POSEDGE]
            if len(posedge_clocks) == 1:
                clk_signal = posedge_clocks[0]
                for s, e in sensitivity_events:
                    if s == clk_signal:
                        clk_edge = e
                        break
                clk_ctrl_signals.append(f"{hier}.{clk_signal}")
                # Any remaining edge-sensitive signals beyond clk+rst are
                # recorded as ambiguous (e.g. second clock, secondary
                # enable) — we do NOT silently relabel them as reset.
                for s, _e in sensitivity_events:
                    if s != clk_signal and s != rst_signal:
                        ambiguous_event_signals.append(f"{hier}.{s}")
            else:
                # No clear single posedge clock.  Apply conservative
                # disambiguation rules in order:
                #  1. If exactly one edge signal total and no reset,
                #     treat it as clock (common single-clock always_ff).
                #  2. If multiple posedge signals and none looks like a
                #     reset (no negated/plain predicate match), pick the
                #     FIRST posedge as the clock and record the rest as
                #     ambiguous event signals (do NOT silently label one
                #     as reset).  This ensures the register is still
                #     created with a known clock; ambiguity is surfaced
                #     via `ambiguous_event_signals` for later review.
                non_reset = [s for s, e in sensitivity_events
                             if s not in reset_candidates]
                if len(non_reset) == 1 and not rst_signal:
                    clk_signal = non_reset[0]
                    for s, e in sensitivity_events:
                        if s == clk_signal:
                            clk_edge = e
                            break
                    clk_ctrl_signals.append(f"{hier}.{clk_signal}")
                elif not rst_signal and len(non_reset) >= 1:
                    # Multiple edge signals, none identified as reset.
                    # Pick the first posedge (if any) as clock; mark rest
                    # ambiguous.  If all are negedge, pick the first edge.
                    posedge_nr = [s for s, e in sensitivity_events
                                  if s in non_reset and e == ClockEdge.POSEDGE]
                    if posedge_nr:
                        clk_signal = posedge_nr[0]
                        for s, e in sensitivity_events:
                            if s == clk_signal:
                                clk_edge = e
                                break
                        clk_ctrl_signals.append(f"{hier}.{clk_signal}")
                        for s in non_reset:
                            if s != clk_signal:
                                ambiguous_event_signals.append(f"{hier}.{s}")
                    else:
                        clk_signal = non_reset[0]
                        for s, e in sensitivity_events:
                            if s == clk_signal:
                                clk_edge = e
                                break
                        clk_ctrl_signals.append(f"{hier}.{clk_signal}")
                        for s in non_reset[1:]:
                            ambiguous_event_signals.append(f"{hier}.{s}")
                else:
                    ambiguous_event_signals.extend(
                        f"{hier}.{s}" for s in non_reset)
                    log.debug("ambiguous clock classification in %s: "
                              "events=%s reset_candidates=%s",
                              proc_id, sensitivity_events, reset_candidates)

            # --- Extract assignments (blocking + nonblocking) ---
            # We use a manual recursive sub-walker (see _subvisit below)
            # that tracks enclosing if/case predicates on a stack.  Each
            # enclosing predicate's refs are attached to every assignment
            # reached inside that branch as CONDITIONAL control deps.
            all_targets: dict[str, ExprWalkResult] = {}
            ordered_assign_lhs: list[str] = []
            control_signal_list: list[str] = []   # insertion-ordered
            _control_seen: set[str] = set()
            read_signal_set: set[str] = set()
            # cond_stack entries are (hier_name, DependencyKind) —
            # predicates are CONDITIONAL, ternary selectors encountered
            # *inside* a predicate are MUX_SELECT.
            cond_stack: list[tuple[str, DependencyKind]] = []

            clk_hier_local = f"{hier}.{clk_signal}" if clk_signal else None
            rst_hier_local = f"{hier}.{rst_signal}" if rst_signal else None

            def _predicate_refs(pred_node) -> list[tuple[str, DependencyKind]]:
                """Walk a procedural predicate expression and return
                ``[(hier_name, kind), ...]`` for all referenced signals.
                Signals in the predicate are CONDITIONAL; selectors of
                ternaries *within* the predicate are MUX_SELECT (per
                ExprWalker rules)."""
                out: list[tuple[str, DependencyKind]] = []
                seen: set[str] = set()
                nodes_to_walk: list[Any] = []
                # ``pred_node`` may be a Condition wrapper; extract its expr.
                if type(pred_node).__name__ == "Condition":
                    ex = getattr(pred_node, "expr", None)
                    if ex is not None:
                        nodes_to_walk.append(ex)
                else:
                    nodes_to_walk.append(pred_node)
                for n in nodes_to_walk:
                    pw = ExprWalker(sm=self._sm)
                    pw.walk(n, kind=DependencyKind.CONDITIONAL)
                    for r in pw.refs:
                        if not r.name:
                            continue
                        hn = f"{hier}.{r.name}"
                        if clk_hier_local and hn == clk_hier_local:
                            continue
                        if rst_hier_local and hn == rst_hier_local:
                            continue
                        if hn in seen:
                            continue
                        seen.add(hn)
                        out.append((hn, r.kind))
                return out

            def _add_control(sig: str) -> None:
                if sig not in _control_seen:
                    _control_seen.add(sig)
                    control_signal_list.append(sig)

            def _process_assignment(node) -> None:
                try:
                    res = walk_assignment(node, self._sm)
                    # Attach enclosing if/case predicates as CONDITIONAL
                    # refs (unless already present).
                    enclosing_names = {n for n, _ in cond_stack}
                    for cs_name, cs_kind in cond_stack:
                        short = cs_name.split(".")[-1]
                        if short in res.names():
                            continue
                        res.refs.append(ExprRef(
                            name=short, kind=cs_kind, op="if"))
                    for tgt in res.targets:
                        hier_tgt = f"{hier}.{tgt}"
                        if hier_tgt not in all_targets:
                            ordered_assign_lhs.append(hier_tgt)
                        if hier_tgt in all_targets:
                            all_targets[hier_tgt].refs.extend(res.refs)
                        else:
                            all_targets[hier_tgt] = res
                    for r in res.refs:
                        if not r.name:
                            continue
                        hn = f"{hier}.{r.name}"
                        read_signal_set.add(hn)
                        if r.kind in (DependencyKind.CONDITIONAL,
                                     DependencyKind.MUX_SELECT):
                            _add_control(hn)
                except Exception as e:
                    log.debug("procedural assign error: %s", e)

            def _children(node) -> list[Any]:
                """Iterate child AST nodes using known attributes."""
                out: list[Any] = []
                nm = type(node).__name__
                # Node-type-specific attributes
                if nm == "BlockStatement":
                    for a in ("body", "stmts", "statements", "items"):
                        v = getattr(node, a, None)
                        if v is None: continue
                        tnm = type(v).__name__
                        if tnm == "StatementList":
                            lst = getattr(v, "list", None)
                            if lst is not None:
                                for x in lst:
                                    out.append(x)
                        elif isinstance(v, (list, tuple)):
                            for x in v: out.append(x)
                        else:
                            out.append(v)
                    return out
                if nm in ("ConditionalStatement",):
                    for a in ("conditions", "ifTrue", "ifFalse"):
                        v = getattr(node, a, None)
                        if v is None: continue
                        if isinstance(v, (list, tuple)):
                            for x in v: out.append(x)
                        else:
                            out.append(v)
                    return out
                if nm in ("CaseStatement",):
                    v = getattr(node, "expr", None)
                    if v is not None: out.append(v)
                    for a in ("items", "defaultCase"):
                        v = getattr(node, a, None)
                        if v is None: continue
                        if isinstance(v, (list, tuple)):
                            for x in v: out.append(x)
                        else:
                            out.append(v)
                    return out
                if nm in ("IfCaseItem", "StandardCaseItem", "PatternCaseItem",
                          "DefaultCaseItem"):
                    for a in ("exprs", "expr", "body", "stmt"):
                        v = getattr(node, a, None)
                        if v is None: continue
                        if isinstance(v, (list, tuple)):
                            for x in v: out.append(x)
                        else:
                            out.append(v)
                    return out
                if nm == "ExpressionStatement":
                    v = getattr(node, "expr", None)
                    if v is not None: out.append(v)
                    return out
                if nm in ("ForLoopStatement", "WhileLoopStatement",
                          "DoWhileLoopStatement", "ForeachLoopStatement",
                          "RepeatLoopStatement", "ForeverLoopStatement",
                          "ReturnStatement"):
                    for a in ("body", "stmt", "init", "stop", "iteration",
                              "loopVars", "expr", "arrayExpr"):
                        v = getattr(node, a, None)
                        if v is None: continue
                        if isinstance(v, (list, tuple)):
                            for x in v: out.append(x)
                        else:
                            out.append(v)
                    return out
                # Generic fallback: try list(), then common attributes.
                try:
                    return list(node)
                except Exception:
                    pass
                for a in ("body", "stmt", "stmts", "statements", "items",
                          "left", "right", "expr", "operand", "operands",
                          "ifTrue", "ifFalse", "conditions", "defaultCase",
                          "value", "target", "source", "concat", "count",
                          "thenExpr", "elseExpr", "predicate"):
                    v = getattr(node, a, None)
                    if v is None: continue
                    if isinstance(v, (list, tuple)):
                        for x in v: out.append(x)
                    else:
                        out.append(v)
                return out

            def _subvisit(node, depth: int = 0) -> None:
                """Manual recursive walker that maintains cond_stack for
                if/else/case predicates, so each nested assignment picks
                up the enclosing conditions as CONDITIONAL refs."""
                if node is None or depth > 64:
                    return
                nm = type(node).__name__
                if nm in ("IntegerLiteral", "IntegerLiteralExpression",
                          "RealLiteral", "TimeLiteral", "ParameterSymbol",
                          "UnbasedUnsizedIntegerLiteral", "NullLiteral",
                          "StringLiteral", "EmptyArgumentExpression",
                          "PortSymbol", "NetSymbol", "VariableSymbol"):
                    return
                if nm == "AssignmentExpression":
                    _process_assignment(node)
                    return
                if nm == "ConditionalStatement":
                    conds = list(getattr(node, "conditions", []) or [])
                    # Slang nests else-if as ConditionalStatement in ifFalse;
                    # we treat conditions[0] as the controlling predicate
                    # for the "if" arm, and recurse on ifFalse WITHOUT
                    # pushing that predicate (else-if chains correctly get
                    # only their own predicate on the stack).
                    pushed: list[tuple[str, DependencyKind]] = []
                    if conds:
                        for ref in _predicate_refs(conds[0]):
                            _add_control(ref[0])
                            read_signal_set.add(ref[0])
                            cond_stack.append(ref)
                            pushed.append(ref)
                    try:
                        _subvisit(getattr(node, "ifTrue", None), depth + 1)
                    finally:
                        for _ in pushed:
                            cond_stack.pop()
                    _subvisit(getattr(node, "ifFalse", None), depth + 1)
                    return
                if nm == "CaseStatement":
                    pushed = []
                    ex = getattr(node, "expr", None)
                    if ex is not None:
                        for ref in _predicate_refs(ex):
                            _add_control(ref[0])
                            read_signal_set.add(ref[0])
                            cond_stack.append(ref)
                            pushed.append(ref)
                    try:
                        for att in ("items", "defaultCase"):
                            _subvisit(getattr(node, att, None), depth + 1)
                    finally:
                        for _ in pushed:
                            cond_stack.pop()
                    return
                if nm in ("IfCaseItem", "PatternCaseItem", "DefaultCaseItem",
                          "StandardCaseItem"):
                    for att in ("expr", "exprs", "body", "stmt"):
                        _subvisit(getattr(node, att, None), depth + 1)
                    return
                if nm == "ExpressionStatement":
                    _subvisit(getattr(node, "expr", None), depth + 1)
                    return
                if nm in ("BlockStatement", "ForLoopStatement",
                          "WhileLoopStatement", "DoWhileLoopStatement",
                          "ForeachLoopStatement", "RepeatLoopStatement",
                          "ForeverLoopStatement"):
                    for ch in _children(node):
                        _subvisit(ch, depth + 1)
                    return
                # Default: walk children
                for ch in _children(node):
                    _subvisit(ch, depth + 1)

            try:
                _subvisit(walk_target, 0)
            except Exception as e:
                log.debug("procedural walk error: %s", e)

            # Build CombEdges.  Edge KIND reflects the source's semantic
            # role for this assignment:
            #   DATA/PART_SELECT/CONCATENATION -> NONBLOCKING_ASSIGN (seq)
            #                                     or BLOCKING_ASSIGN (comb)
            #   MUX_SELECT                     -> MUX_SELECT (mux select
            #                                     arc, part of data path)
            #   CONDITIONAL                    -> CONDITIONAL (procedural
            #                                     guard — if/case predicate)
            for tgt, res in all_targets.items():
                is_nonblocking = bool(res.is_nonblocking)
                added_pairs: set[tuple[str, str, DependencyKind]] = set()
                for r in res.refs:
                    if not r.name:
                        continue
                    hier_src = (f"{hier}.{r.name}" if "." not in r.name
                                else r.name)
                    if hier_src == tgt:
                        continue
                    if r.kind == DependencyKind.CONDITIONAL:
                        kind = DependencyKind.CONDITIONAL
                    elif r.kind == DependencyKind.MUX_SELECT:
                        kind = DependencyKind.MUX_SELECT
                    elif is_nonblocking:
                        kind = DependencyKind.NONBLOCKING_ASSIGN
                    else:
                        kind = DependencyKind.BLOCKING_ASSIGN
                    key = (hier_src, tgt, kind)
                    if key in added_pairs:
                        continue
                    added_pairs.add(key)
                    design.comb_edges.append(CombEdge(
                        src=hier_src,
                        dst=tgt,
                        kind=kind,
                        via=proc_id,
                        source_location=source_location(self._sm, pb),
                        context=r.op,
                    ))

            # --- Populate process fields ---
            proc.inferred_clock = clk_signal
            proc.inferred_reset = rst_signal
            proc.inferred_reset_edge = rst_edge
            proc.has_reset_branch = rst_signal is not None
            proc.clock_signals = sorted(clk_ctrl_signals)
            proc.reset_signals = sorted(rst_ctrl_signals)
            proc.assigned_signals = list(ordered_assign_lhs)
            proc.read_signals = sorted(read_signal_set)
            proc.control_signals = sorted(control_signal_list)
            for s in clk_ctrl_signals:
                proc.sensitivity.append(SensitivityItem(signal=s, edge=clk_edge))
            if rst_signal:
                for s in rst_ctrl_signals:
                    proc.sensitivity.append(SensitivityItem(signal=s, edge=rst_edge))
            # Preserve ambiguity evidence (edge-sensitive signals we
            # couldn't classify as clk vs reset).
            if ambiguous_event_signals:
                proc.control_signals = sorted(
                    set(proc.control_signals) | set(ambiguous_event_signals))

            design.processes[proc_id] = proc
            design.modules[mod_name].process_ids.append(proc_id)

            # --- Build registers for non-blocking assignments in always_ff/always ---
            if kind_name in ("always_ff", "always") and clk_signal:
                self._make_registers_from_targets(
                    all_targets, design, mod_name, hier,
                    clk_signal, clk_edge, rst_signal, rst_edge,
                    proc_id,
                )

            return VisitAction.Skip

        # Use a top-level handler that skips Instance bodies (so we don't
        # descend into sub-instances — those are walked separately in
        # ``_collect_sub_instances`` → ``_walk_instance``).
        def _root(node):
            if hasattr(node, "kind") and node.kind == SymbolKind.Instance:
                return VisitAction.Skip
            if hasattr(node, "kind") and node.kind == SymbolKind.ProceduralBlock:
                return on_proc(node)
            return VisitAction.Advance
        body.visit(f=_root)

    def _make_registers_from_targets(
        self,
        targets: dict[str, ExprWalkResult],
        design: Design,
        mod_name: str,
        hier: str,
        clk_signal: str,
        clk_edge: ClockEdge,
        rst_signal: str | None,
        rst_edge: ClockEdge | None,
        proc_id: str,
    ) -> None:
        reset_type = ResetType.ASYNCHRONOUS if rst_signal else ResetType.UNKNOWN
        polarity = ResetPolarity.UNKNOWN
        if rst_signal and rst_edge is not None:
            polarity = (ResetPolarity.ACTIVE_LOW if rst_edge == ClockEdge.NEGEDGE
                        else ResetPolarity.ACTIVE_HIGH)

        clk_hier = f"{hier}.{clk_signal}"
        rst_hier = f"{hier}.{rst_signal}" if rst_signal else None
        # Names (local) excluded from data/control — the clock and reset
        # signals identified from the sensitivity list / reset-predicate.
        ctrl_local_names = {clk_signal}
        if rst_signal:
            ctrl_local_names.add(rst_signal)
        ctrl_local_hier = {clk_hier}
        if rst_hier:
            ctrl_local_hier.add(rst_hier)

        # Dependency kinds considered DATA for the D-cone: everything
        # that contributes to the *value* being loaded (including
        # comparisons / logical ops on RHS, concat/part members).
        _DATA_REFS = {
            DependencyKind.DATA,
            DependencyKind.CONCATENATION,
            DependencyKind.PART_SELECT,
        }
        # Dependency kinds considered CONTROL (procedural guard or mux
        # select).  These don't drive D but may act as enables/selects.
        _CTRL_REFS = {
            DependencyKind.CONDITIONAL,
            DependencyKind.MUX_SELECT,
        }

        for hier_tgt, res in targets.items():
            tname = hier_tgt.split(".")[-1]
            if tname in ctrl_local_names:
                continue
            width = 1
            net = design.nets.get(hier_tgt)
            if net:
                width = net.width
            data_srcs: list[str] = []
            seen_data: set[str] = set()
            ctrl_srcs: list[str] = []
            seen_ctrl: set[str] = set()

            for r in res.refs:
                if not r.name or r.name in ctrl_local_names:
                    continue
                if r.kind in (DependencyKind.CLOCK, DependencyKind.RESET):
                    continue
                hier_src = f"{hier}.{r.name}"
                if hier_src in ctrl_local_hier:
                    continue
                if r.kind in _CTRL_REFS:
                    if hier_src not in seen_ctrl:
                        seen_ctrl.add(hier_src)
                        ctrl_srcs.append(hier_src)
                    continue
                # r.kind in _DATA_REFS (or unknown — treat as data):
                if hier_src == hier_tgt:
                    # self-feedback (e.g. "else q <= q")
                    if hier_src not in seen_data:
                        seen_data.add(hier_src)
                        data_srcs.append(hier_src)
                    continue
                if hier_src not in seen_data:
                    seen_data.add(hier_src)
                    data_srcs.append(hier_src)

            # enable_signal: the first CONDITIONAL (if/case guard) found
            # in RHS refs; if no CONDITIONAL, fall back to the first
            # MUX_SELECT.  This preserves backward compatibility with the
            # single-signal enable API while keeping the full list in
            # ``control_sources``.
            enable: str | None = None
            _cond_first: str | None = None
            _mux_first: str | None = None
            for r in res.refs:
                if not r.name or r.name in ctrl_local_names:
                    continue
                hn = f"{hier}.{r.name}"
                if hn in ctrl_local_hier:
                    continue
                if r.kind == DependencyKind.CONDITIONAL and _cond_first is None:
                    _cond_first = hn
                if r.kind == DependencyKind.MUX_SELECT and _mux_first is None:
                    _mux_first = hn
            enable = _cond_first or _mux_first

            reg = Register(
                hierarchical_name=hier_tgt,
                local_name=tname,
                parent_module=mod_name,
                width=width,
                clock_signal=clk_hier,
                clock_edge=clk_edge,
                reset_signal=rst_hier,
                reset_type=reset_type,
                reset_edge=rst_edge,
                reset_polarity=polarity,
                enable_signal=enable,
                data_sources=data_srcs,
                control_sources=ctrl_srcs,
                process_id=proc_id,
                inferred_type="dff",
                source_location=None,
            )
            design.registers[hier_tgt] = reg

            clk_port = design.ports.get(clk_hier)
            if clk_port and "clock" not in clk_port.timing_role_candidates:
                clk_port.connected_clock_candidates.append(clk_signal)
                clk_port.timing_role_candidates.append("clock")
            if rst_hier:
                rst_port = design.ports.get(rst_hier)
                if rst_port and "reset" not in rst_port.timing_role_candidates:
                    rst_port.timing_role_candidates.append("reset")

    def _collect_continuous_assigns(self, body, design: Design, mod_name: str, hier: str) -> None:
        counter = [0]

        def on_assign(sym):
            counter[0] += 1
            # Each continuous-assign symbol has one AssignmentExpression child.
            def find_ae(node):
                if type(node).__name__ == "AssignmentExpression":
                    res = walk_assignment(node, self._sm)
                    loc = source_location(self._sm, sym)
                    for tgt in res.targets:
                        hier_tgt = f"{hier}.{tgt}"
                        for r in res.refs:
                            hier_src = f"{hier}.{r.name}"
                            if hier_src == hier_tgt:
                                continue
                            if r.kind == DependencyKind.CONDITIONAL:
                                dk = DependencyKind.CONDITIONAL
                            elif r.kind == DependencyKind.MUX_SELECT:
                                dk = DependencyKind.MUX_SELECT
                            else:
                                dk = DependencyKind.CONTINUOUS_ASSIGN
                            design.comb_edges.append(CombEdge(
                                src=hier_src,
                                dst=hier_tgt,
                                kind=dk,
                                via=f"{hier}.assign{counter[0]}",
                                source_location=loc,
                                context=r.op,
                            ))
                    return VisitAction.Skip
                return VisitAction.Advance
            try:
                sym.visit(f=find_ae)
            except Exception as e:
                log.debug("continuous assign visit error: %s", e)
            return VisitAction.Advance

        def _root(node):
            if hasattr(node, "kind") and node.kind == SymbolKind.Instance:
                return VisitAction.Skip
            if hasattr(node, "kind") and node.kind == SymbolKind.ContinuousAssign:
                return on_assign(node)
            return VisitAction.Advance
        body.visit(f=_root)
        design.modules[mod_name].continuous_assignments += counter[0]

    def _collect_sub_instances(self, body, design: Design, mod_name: str, hier: str) -> None:
        def on_inst(sym):
            try:
                if not sym.name or sym.body is None:
                    return VisitAction.Advance
                inst_hier = f"{hier}.{sym.name}"
                if inst_hier in design.instances:
                    return VisitAction.Skip
                defn = sym.body.definition
                param_overrides: dict[str, Any] = {}
                try:
                    for p in sym.body.parameters:
                        try:
                            v = p.value
                            v = int(v)
                        except Exception:
                            try:
                                v = float(v)
                            except Exception:
                                v = str(v)
                        param_overrides[p.name] = v
                except Exception:
                    pass

                # --- port connections via sym.portConnections ---
                conn: dict[str, str] = {}
                try:
                    pcs = list(sym.portConnections)
                except Exception:
                    pcs = []
                for pc in pcs:
                    try:
                        formal = pc.port.name
                        direction = _convert_direction(pc.port.direction).value
                    except Exception:
                        formal, direction = "?", "input"
                    expr = pc.expression
                    # Resolve the parent-scope signal this connects to.
                    actual = _resolve_connection_actual(expr, self._sm)
                    if actual is not None:
                        actual_hier = f"{hier}.{actual}"
                        conn[formal] = actual
                        design.hier_conns.append(HierPortConn(
                            instance_hier=inst_hier,
                            module_name=defn.name,
                            port_name=formal,
                            direction=direction,
                            actual_signal=actual_hier,
                            source_location=source_location(self._sm, sym),
                        ))
                    else:
                        conn[formal] = formal

                inst = Instance(
                    hierarchical_name=inst_hier,
                    local_name=sym.name,
                    parent_module=mod_name,
                    module_name=defn.name,
                    parameter_overrides=param_overrides,
                    port_connections=conn,
                    source_location=source_location(self._sm, sym),
                )
                design.instances[inst_hier] = inst
                design.modules[mod_name].instance_names.append(sym.name)
                self._walk_instance(sym, design, parent_path=f"{hier}.")
            except Exception as e:
                log.debug("sub-instance collect error: %s", e)
            return VisitAction.Skip

        def _root(node):
            if hasattr(node, "kind") and node.kind == SymbolKind.Instance:
                return on_inst(node)
            return VisitAction.Advance
        body.visit(f=_root)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def _record_diagnostic(self, diag: Any) -> None:
        sev_map = {0: Severity.INFO, 1: Severity.WARNING, 2: Severity.ERROR, 3: Severity.CRITICAL}
        try:
            sev = sev_map.get(int(diag.severity), Severity.WARNING)
        except Exception:
            sev = Severity.WARNING
        fname = line = None
        loc = getattr(diag, "location", None)
        if loc is not None and self._sm is not None:
            try:
                fname = self._sm.getFileName(loc)
                line = self._sm.getLineNumber(loc)
            except Exception:
                pass
        code = (ErrorCode.PARSER_ERROR
                if sev in (Severity.ERROR, Severity.CRITICAL)
                else ErrorCode.INFERENCE_WARNING)
        self.diagnostics.add(Diagnostic(
            code=code, severity=sev, message=str(diag), file=fname, line=line,
        ))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_first_conditional(node: Any, depth: int = 0) -> Any:
    """Locate the first ConditionalStatement under node (up to depth 8)."""
    if node is None or depth > 8:
        return None
    nm = type(node).__name__
    if nm == "ConditionalStatement":
        return node
    if nm == "BlockStatement":
        bd = getattr(node, "body", None)
        if bd is not None:
            tnm = type(bd).__name__
            if tnm == "StatementList":
                lst = getattr(bd, "list", None) or []
                for s in lst:
                    res = _find_first_conditional(s, depth + 1)
                    if res is not None:
                        return res
            else:
                return _find_first_conditional(bd, depth + 1)
    if nm == "StatementList":
        lst = getattr(node, "list", None) or []
        for s in lst:
            res = _find_first_conditional(s, depth + 1)
            if res is not None:
                return res
    return None


def _detect_direct_predicate_signals(stmt: Any) -> set[str]:
    """Return signals that appear as a *direct* NamedValueExpression
    (no negation, no binary operators) in the predicate of the first
    top-level if.  This covers the active-high async-reset pattern::

        always_ff @(posedge clk or posedge rst)
          if (rst) q <= '0; else q <= d;
    """
    found: set[str] = set()
    if stmt is None:
        return found
    cond = _find_first_conditional(stmt)
    if cond is None:
        return found
    conds = list(getattr(cond, "conditions", []) or [])
    for c in conds:
        ex = getattr(c, "expr", c) if type(c).__name__ == "Condition" else c
        # Peel off ConversionExpression/Parenthesized wrappers but stop
        # at anything non-trivial (binary op, negation, etc.).
        while ex is not None:
            nm = type(ex).__name__
            if nm == "NamedValueExpression":
                n = _extract_signal_name(ex)
                if n:
                    found.add(n)
                break
            if nm in ("ConversionExpression", "ParenthesizedExpression"):
                ex = getattr(ex, "operand", None) or getattr(ex, "expr", None)
                continue
            break
    return found


def _detect_negated_predicates(stmt: Any) -> set[str]:
    """Return names of signals that appear as a direct negated reference
    (``!sig`` or ``~sig``) in a top-level ``if`` condition reachable
    from ``stmt``.  This is the canonical async-reset structural cue:

        always_ff @(posedge clk or negedge rst_n)
          if (!rst_n)  q <= '0;
          else         q <= d;

    The function descends through BlockStatement/StatementList wrappers
    to find the first ConditionalStatement (if/else chain), and inspects
    that statement's predicate.  Signals found inside a negation there
    are returned.  Only *direct* negations at the top of the predicate
    are considered (a negation buried inside ``a && !b`` is not treated
    as a reset cue, because that would be speculative).

    Returns a set of *local* (unqualified) signal names.
    """
    found: set[str] = set()
    if stmt is None:
        return found

    def find_first_conditional(node: Any, depth: int = 0) -> Any:
        if node is None or depth > 8:
            return None
        nm = type(node).__name__
        if nm == "ConditionalStatement":
            return node
        if nm == "BlockStatement":
            bd = getattr(node, "body", None)
            if bd is not None:
                tnm = type(bd).__name__
                if tnm == "StatementList":
                    lst = getattr(bd, "list", None) or []
                    for s in lst:
                        res = find_first_conditional(s, depth + 1)
                        if res is not None:
                            return res
                else:
                    return find_first_conditional(bd, depth + 1)
        if nm == "StatementList":
            lst = getattr(node, "list", None) or []
            for s in lst:
                res = find_first_conditional(s, depth + 1)
                if res is not None:
                    return res
        return None

    def collect_direct_negations(expr: Any) -> None:
        """Walk expr looking for UnaryExpression(!/~) whose operand is a
        plain NamedValueExpression; add those signal names to found."""
        if expr is None:
            return
        nm = type(expr).__name__
        if nm == "UnaryExpression":
            op = getattr(expr, "op", None)
            op_str = str(op).split(".")[-1].lower()
            if op_str in ("logicalnot", "bitwisenot", "not", "lnot", "bnot"):
                operand = getattr(expr, "operand", None)
                if operand is not None and type(operand).__name__ == "NamedValueExpression":
                    n = _extract_signal_name(operand)
                    if n:
                        found.add(n)
            # Also descend in case the negation wraps a parenthesised expr
            # (pyslang usually strips parens, but be safe).
            collect_direct_negations(getattr(expr, "operand", None))
            return
        # Descend through parenthesised/conversion wrappers
        for att in ("expr", "operand"):
            ch = getattr(expr, att, None)
            if ch is not None and ch is not expr:
                collect_direct_negations(ch)
        # Descend through Condition wrapper
        if nm == "Condition":
            collect_direct_negations(getattr(expr, "expr", None))

    cond = find_first_conditional(stmt)
    if cond is None:
        return found
    conds = list(getattr(cond, "conditions", []) or [])
    for c in conds:
        ex = getattr(c, "expr", c) if type(c).__name__ == "Condition" else c
        collect_direct_negations(ex)
    return found


def _convert_direction(d) -> PortDirection:
    s = str(d).split(".")[-1].lower()
    if s in ("in", "input", "ref"):
        return PortDirection.INPUT
    if s in ("out", "output"):
        return PortDirection.OUTPUT
    if s == "inout":
        return PortDirection.INOUT
    return PortDirection.INPUT


def _type_info(sym) -> tuple[int, str | None, str, str]:
    width = 1
    width_spec = None
    datatype = "logic"
    net_kind = "wire"
    try:
        t = sym.type
        try:
            bits = t.bitWidth
            width = bits if bits and bits > 0 else 1
        except Exception:
            pass
        ts = str(t)
        if "[" in ts and "]" in ts:
            width_spec = ts[ts.index("["):]
        if "reg" in ts:
            net_kind = "reg"
        elif "wire" in ts:
            net_kind = "wire"
        else:
            net_kind = "logic"
        if "logic" in ts:
            datatype = "logic"
        elif "reg" in ts:
            datatype = "reg"
        elif "wire" in ts:
            datatype = "wire"
    except Exception:
        pass
    return width, width_spec, datatype, net_kind


def _extract_signal_name(expr) -> str | None:
    if expr is None:
        return None
    try:
        if hasattr(expr, "symbol"):
            s = expr.symbol
            if hasattr(s, "name"):
                return s.name
    except Exception:
        pass
    try:
        if hasattr(expr, "name"):
            n = expr.name
            if isinstance(n, str) and n:
                return n
    except Exception:
        pass
    try:
        ts = str(expr)
        if ts and not ts.startswith("Expression("):
            return ts.split("(")[0]
    except Exception:
        pass
    return None


def _resolve_connection_actual(expr, sm) -> str | None:
    """Extract the parent-scope signal name from a port-connection
    expression.

    * NamedValueExpression ``.a(sig)`` / ``.a(sig)`` → ``sig``.
    * ``.a(expr)`` for complex expressions → None (we don't flatten
      combinational expressions through ports; connectivity stops here
      and is marked UNKNOWN per Manual §G).
    * Positional NamedValue → name.
    * AssignmentExpression output form ``.y(actual)`` → the LHS is the
      actual parent signal (output driving back out).
    * EmptyArgumentExpression → None (unconnected port).
    """
    if expr is None:
        return None
    nm = type(expr).__name__
    if nm == "NamedValueExpression":
        try:
            return expr.symbol.name
        except Exception:
            return None
    if nm == "EmptyArgumentExpression":
        return None
    if nm == "AssignmentExpression":
        # Output port connection: .formal(actual) — parent drives formal.
        # The LHS of the assignment expression is the parent signal.
        lhs = getattr(expr, "left", None)
        if lhs is not None:
            return _resolve_connection_actual(lhs, sm)
        return None
    # Complex expression (concat, binary, conditional): do NOT invent.
    return None
