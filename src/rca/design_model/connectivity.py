"""
Structural connectivity builder.

Given a ``Design`` parsed by an adapter (currently pyslang) which has:

* ``comb_edges`` populated as raw directed dependency edges,
* ``registers`` populated with ``data_sources`` and clock/reset,
* ``hier_conns`` populated with instance port connections,
* ``ports`` / ``nets`` / ``processes`` populated,

this module:

1. De-duplicates edges.
2. Separates clock/reset/NB-assign boundaries from data edges.
3. Projects cross-hierarchy signals through ``HierPortConn`` connections
   so the global fanin/fanout graph is built over hierarchical names
   (no destructive flattening — Manual §G).
4. Builds fast fanin/fanout indexes on the *combinational* data-only
   subgraph (NONBLOCKING_ASSIGN edges are register boundaries, not hops).
5. Enumerates real timing paths (IN→REG, REG→REG, REG→OUT, IN→OUT, CDC)
   by traversing actual connectivity rather than count heuristics
   (Manual §E, §F).
6. Populates ``Register.q_consumers`` and ``Register.data_source``.

Path enumeration is bounded (Manual §F):

* Per-startpoint BFS terminates at register D inputs and top outputs.
* Combinational cycles are broken by a per-startpoint visited set.
* Per-startpoint depth cap ``MAX_COMB_DEPTH`` prevents pathological
  blow-up.  These are structural paths only — no delay/slack is computed.
* ``MAX_EDGES`` caps the data-edge index for adversarial inputs.

When bounds are hit, traversal truncates; no exception is raised.
"""

from __future__ import annotations

from collections import defaultdict, deque

from ..utils.enums import (
    DependencyKind,
    TimingPathClass,
)
from ..utils.logging import get_logger
from .design import Design
from .net import CombEdge, HierPortConn
from .register import Register
from .timing_path_lite import StructuralPath

log = get_logger("structural")

# ---------------------------------------------------------------------------
# Scalability bounds (Manual §F)
# ---------------------------------------------------------------------------
MAX_COMB_DEPTH = 256
MAX_EDGES = 200_000


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_structural_connectivity(design: Design) -> StructuralGraph:
    """Build and attach a ``StructuralGraph`` to the design.

    Populates:
    * ``Register.q_consumers`` (direct combinational consumers of Q).
    * ``Register.data_source`` (unique-D-source back-compat field).
    * ``Design.structural_paths`` / ``Design.cdc_paths`` — lists of
      :class:`StructuralPath` (later consumed by ``TimingGraph``).
    """
    g = StructuralGraph(design)
    g.build()
    design._structural_graph_internal = g  # type: ignore[attr-defined]
    return g


class StructuralGraph:
    """Structural connectivity result."""

    def __init__(self, design: Design) -> None:
        self.design = design
        # fanout/fanin over *combinational data* edges (no clock/reset,
        # no non-blocking assign boundaries, no CONDITIONAL procedural
        # guards), with hierarchy resolved.  MUX_SELECT edges (ternary
        # selectors) ARE included — they are real data-path arcs
        # (mux select pins are timing-relevant endpoints through the
        # mux; the S→Q arc through a mux is a legitimate timing path).
        self.data_fanout: dict[str, set[str]] = defaultdict(set)
        self.data_fanin: dict[str, set[str]] = defaultdict(set)
        # CONDITIONAL (procedural guard) edges are tracked separately
        # for enable/clock-gate inference but are NOT traversed as
        # ordinary data hops (an if-condition guard is NOT a normal
        # register-to-register timing path).
        self.guard_fanout: dict[str, set[str]] = defaultdict(set)
        self.guard_fanin: dict[str, set[str]] = defaultdict(set)
        # Edge metadata: (src,dst) -> list[CombEdge]
        self.edge_meta: dict[tuple[str, str], list[CombEdge]] = defaultdict(list)
        # Clock/reset control signals (not treated as data).
        self.control_signals: set[str] = set()
        self.paths: list[StructuralPath] = []
        self.cdc_paths: list[StructuralPath] = []
        self.stats: dict[str, int] = {}
        self.warnings: list[str] = []

    # ------------------------------------------------------------------
    # Build pipeline
    # ------------------------------------------------------------------

    def build(self) -> None:
        self._collect_control_signals()
        self._alias_ports_to_nets()
        self._resolve_hierarchy_connections()
        self._index_edges()
        self._populate_register_connectivity()
        self._enumerate_paths()
        self.stats = {
            "data_edges": sum(len(v) for v in self.data_fanout.values()),
            "guard_edges": sum(len(v) for v in self.guard_fanout.values()),
            "control_signals": len(self.control_signals),
            "paths_total": len(self.paths) + len(self.cdc_paths),
            "cdc_paths": len(self.cdc_paths),
            "warnings": len(self.warnings),
        }
        log.info(
            "Structural graph built: %d data edges, %d control signals, "
            "%d timing paths, %d CDC paths",
            self.stats["data_edges"], self.stats["control_signals"],
            self.stats["paths_total"], self.stats["cdc_paths"],
        )

    # ------------------------------------------------------------------
    # Step 1b: port/net aliasing
    # ------------------------------------------------------------------

    def _alias_ports_to_nets(self) -> None:
        """For ``output logic q`` the port and the internal net are the
        same physical signal; add bidirectional alias edges so combinational
        traversal can cross the boundary."""
        d = self.design
        existing = {(e.src, e.dst) for e in d.comb_edges}

        def add(src: str, dst: str) -> None:
            if src == dst or (src, dst) in existing:
                return
            d.comb_edges.append(CombEdge(
                src=src, dst=dst, kind=DependencyKind.DATA,
                via="port-alias",
            ))
            existing.add((src, dst))

        for port in d.ports.values():
            net = d.nets.get(port.hierarchical_name)
            if net is None:
                continue
            if port.direction.value in ("input", "inout"):
                add(port.hierarchical_name, net.hierarchical_name)
            if port.direction.value in ("output", "inout"):
                add(net.hierarchical_name, port.hierarchical_name)

    # ------------------------------------------------------------------
    # Step 1: collect control signals (clock/reset only)
    # ------------------------------------------------------------------

    def _collect_control_signals(self) -> None:
        """Identify global control signals (CLOCK / RESET only) that
        must never appear in the data fanout.

        Important: register enables, if/case predicates, and mux
        selectors are *not* promoted to global control exclusions.
        They remain in the graph with their own edge kind
        (CONDITIONAL or MUX_SELECT) so the D-cone remains complete —
        only clock/reset signals (which are identified structurally
        from the sensitivity list, not by signal name) are excluded
        from data traversal.
        """
        d = self.design
        for r in d.registers.values():
            if r.clock_signal:
                self.control_signals.add(r.clock_signal)
            if r.reset_signal:
                self.control_signals.add(r.reset_signal)
        for p in d.processes.values():
            for sig in p.clock_signals:
                self.control_signals.add(sig)
            for sig in p.reset_signals:
                self.control_signals.add(sig)
        # Ports tagged clock-like by their is_clock_like heuristic are
        # added to global control.  We do NOT use "reset" name substring
        # here — that was too aggressive (a signal named ``reset_data``
        # would be incorrectly hidden).  Reset signals are discovered
        # from structural async-reset patterns in the parser.
        for p in d.ports.values():
            if p.is_clock_like:
                self.control_signals.add(p.hierarchical_name)

    # ------------------------------------------------------------------
    # Step 2: project hierarchy port connections as additional CombEdges
    # ------------------------------------------------------------------

    def _resolve_hierarchy_connections(self) -> None:
        """For every HierPortConn, add parent_actual <-> child_formal
        edges. Conservative: if either side is unresolved we skip —
        connectivity is never invented (Manual §G)."""
        d = self.design
        new_edges: list[CombEdge] = []
        for conn in d.hier_conns:
            if not conn.actual_signal:
                continue
            formal = f"{conn.instance_hier}.{conn.port_name}"
            actual = conn.actual_signal
            if not (_signal_exists(d, actual) and _signal_exists(d, formal)):
                self.warnings.append(
                    f"hier port {conn.instance_hier}.{conn.port_name}: "
                    f"unresolved connection (actual={actual})")
                continue
            if conn.direction in ("input", "inout"):
                new_edges.append(CombEdge(
                    src=actual, dst=formal,
                    kind=DependencyKind.HIER_PORT_CONN,
                    via=conn.instance_hier,
                    source_location=conn.source_location,
                ))
            if conn.direction in ("output", "inout"):
                new_edges.append(CombEdge(
                    src=formal, dst=actual,
                    kind=DependencyKind.HIER_PORT_CONN,
                    via=conn.instance_hier,
                    source_location=conn.source_location,
                ))
        if new_edges:
            d.comb_edges.extend(new_edges)

    # ------------------------------------------------------------------
    # Step 3: index edges into combinational data fanout/fanin
    # ------------------------------------------------------------------

    def _index_edges(self) -> None:
        d = self.design
        seen: set[tuple[str, str, str, str | None]] = set()
        added_data = 0
        added_guard = 0
        # *Combinational data* edge kinds: these are traversed during a
        # combinational sweep (i.e. they can form timing paths).  RHS
        # comparisons/logical ops show up here as NONBLOCKING_ASSIGN /
        # BLOCKING_ASSIGN / CONTINUOUS_ASSIGN edges (because they
        # compute the value, not guard execution).  MUX_SELECT edges
        # (ternary selector pins) ARE included because the S→Q arc
        # through a mux is a real timing path.  NONBLOCKING_ASSIGN
        # edges are register *boundaries* and therefore NOT in this
        # set — data_sources are used directly for D-input connections.
        DATA_KINDS = {
            DependencyKind.DATA,
            DependencyKind.CONTINUOUS_ASSIGN,
            DependencyKind.BLOCKING_ASSIGN,
            DependencyKind.MUX_SELECT,
            DependencyKind.CONCATENATION,
            DependencyKind.PART_SELECT,
            DependencyKind.HIER_PORT_CONN,
        }
        # *Procedural guard* edges (if/case/when predicates): these are
        # retained for enable/clock-gate inference but are NOT treated
        # as ordinary data hops — a signal appearing in an if-condition
        # does NOT create a register-to-register timing path to the
        # assigned register's D input.
        GUARD_KINDS = {
            DependencyKind.CONDITIONAL,
        }
        reg_q_names = {r.q_name() for r in d.registers.values()}

        truncated = False
        for e in d.comb_edges:
            sig = e.signature()
            if sig in seen:
                continue
            seen.add(sig)
            self.edge_meta[(e.src, e.dst)].append(e)

            if e.src in self.control_signals or e.dst in self.control_signals:
                # Clock/reset don't participate in data or guard traversal.
                continue

            if e.kind in DATA_KINDS:
                self.data_fanin[e.dst].add(e.src)
                self.data_fanout[e.src].add(e.dst)
                added_data += 1
            elif e.kind in GUARD_KINDS:
                self.guard_fanin[e.dst].add(e.src)
                self.guard_fanout[e.src].add(e.dst)
                added_guard += 1
                # Note: we deliberately do NOT propagate guards through
                # data_fanout during path enumeration — an enable does
                # not transitively become a data input to downstream
                # registers.

            if (added_data + added_guard) >= MAX_EDGES and not truncated:
                self.warnings.append(
                    f"structural edge cap ({MAX_EDGES}) reached; truncating.")
                truncated = True
                break

    # ------------------------------------------------------------------
    # Step 4: populate register q_consumers / data_source
    # ------------------------------------------------------------------

    def _populate_register_connectivity(self) -> None:
        for r in self.design.registers.values():
            if len(r.data_sources) == 1:
                r.data_source = r.data_sources[0]
            else:
                r.data_source = None
            q = r.q_name()
            r.q_consumers = sorted(self.data_fanout.get(q, set()))

    # ------------------------------------------------------------------
    # Step 5: enumerate timing paths
    # ------------------------------------------------------------------

    def _enumerate_paths(self) -> None:
        """BFS over combinational data edges from each startpoint (top
        inputs and register Q outputs).

        Semantics
        ---------
        * NONBLOCKING_ASSIGN edges are not in the data fanout (they are
          register boundaries); instead ``Register.data_sources`` directly
          lists signals that feed a register's D input.
        * When a combinational sweep lands on a signal X, any register
          R where X is in R.data_sources is an endpoint (IN→REG or
          REG→REG).  Register Q nodes encountered mid-sweep are also
          D-input endpoints (edges into Q write the register).
        * A top-level output port is an endpoint.  When a register's Q
          name equals a top output name (output-register pattern), a
          REG→OUT path is emitted for zero-depth from that register.
        * Per-startpoint visited set prevents combinational cycles;
          per-startpoint depth cap (MAX_COMB_DEPTH) bounds pathological
          fanout (Manual §F).
        """
        d = self.design
        top_inputs = [p.hierarchical_name for p in d.top_ports()
                      if p.direction.value == "input"
                      and p.hierarchical_name not in self.control_signals]
        top_outputs = {p.hierarchical_name for p in d.top_ports()
                       if p.direction.value == "output"}
        regs = list(d.registers.values())
        reg_by_q: dict[str, Register] = {r.q_name(): r for r in regs}

        # signal -> registers whose D is directly fed by that signal
        # (from R.data_sources; excludes self-feedback).
        signal_feeds_d_of: dict[str, list[Register]] = defaultdict(list)
        for r in regs:
            for ds in r.data_sources:
                if ds == r.q_name():
                    continue
                signal_feeds_d_of[ds].append(r)

        # ------------------------------------------------------------------
        # Combinational BFS (per startpoint).  Does NOT traverse into a
        # register Q node (stops there); returns reached signals +
        # per-hop via labels.
        # ------------------------------------------------------------------
        def comb_sweep(start: str, allow_through_reg_q: bool = False
                       ) -> tuple[set[str], dict[str, list[str]]]:
            """BFS combinational fanout from ``start``.

            If ``allow_through_reg_q`` is False (default), traversal
            halts when it lands on any register Q (D-input terminal).
            If True, traversal continues past Q — used when the
            startpoint IS a register Q, so that port-alias edges from
            Q to an output port are discovered.
            """
            reached: set[str] = {start}
            vias: dict[str, list[str]] = {start: []}
            visited: set[str] = {start}
            queue: deque[tuple[str, list[str]]] = deque([(start, [])])
            depth = 0
            while queue and depth < MAX_COMB_DEPTH:
                depth += 1
                for _ in range(len(queue)):
                    node, vpath = queue.popleft()
                    for nxt in sorted(self.data_fanout.get(node, ())):
                        if nxt in visited:
                            continue
                        visited.add(nxt); reached.add(nxt)
                        nv = vpath + [self._via_label(node, nxt)]
                        vias[nxt] = nv
                        if nxt in reg_by_q and not allow_through_reg_q:
                            continue
                        # When sweeping FROM a register Q, allow traversal
                        # through that same Q's port-alias edges but NOT
                        # into another register's Q.
                        if nxt in reg_by_q and nxt != start:
                            continue
                        queue.append((nxt, nv))
            if depth >= MAX_COMB_DEPTH and queue:
                self.warnings.append(
                    f"combinational depth cap ({MAX_COMB_DEPTH}) reached "
                    f"from {start}; further paths truncated.")
            return reached, vias

        # ------------------------------------------------------------------
        # Path emission helpers
        # ------------------------------------------------------------------
        pid = [0]

        def add(start: str, end: str, cls: TimingPathClass,
                launch: str | None, capture: str | None,
                via: list[str]) -> None:
            pid[0] += 1
            sp = StructuralPath(
                id=f"sp{pid[0]:04d}",
                startpoint=start, endpoint=end,
                path_class=cls,
                launch_clock=launch, capture_clock=capture,
                combinational_via=list(via),
            )
            if cls is TimingPathClass.CDC:
                sp.cross_domain = True
                sp.evidence.append(
                    f"structural crossing: launch={launch}, capture={capture}")
                self.cdc_paths.append(sp)
            else:
                self.paths.append(sp)

        seen: set[tuple[str, str, str]] = set()

        def addp(start: str, end: str, cls: TimingPathClass,
                 launch: str | None, capture: str | None,
                 via: list[str]) -> None:
            key = (start, end, cls.value)
            if key in seen:
                return
            seen.add(key)
            add(start, end, cls, launch, capture, via)

        def report_endpoints(start: str, sig: str, v: list[str],
                             cls_reg: TimingPathClass,
                             launch: str | None,
                             start_is_reg_q: bool) -> None:
            """Emit paths for endpoints reached at ``sig`` when
            sweeping from ``start``.  ``start_is_reg_q`` distinguishes
            input-side sweeps (where landing on a reg Q is a REG
            endpoint) from register-side sweeps (where landing on the
            start register's own Q at zero depth can be an output
            termination, but other Q hits are also register D ends)."""
            # Register endpoints reached via data_sources
            for r in signal_feeds_d_of.get(sig, ()):
                if r.hierarchical_name == start:
                    continue
                capture = _clk_leaf(r.clock_signal)
                is_cdc = bool(launch and capture and launch != capture)
                cls = (TimingPathClass.CDC if is_cdc else cls_reg)
                addp(start, r.hierarchical_name, cls, launch, capture, v)
            # If sig IS a register Q by name
            if sig in reg_by_q:
                r = reg_by_q[sig]
                if r.hierarchical_name != start and sig not in [
                        rr.hierarchical_name
                        for rr in signal_feeds_d_of.get(sig, ())]:
                    capture = _clk_leaf(r.clock_signal)
                    is_cdc = bool(launch and capture and launch != capture)
                    cls = (TimingPathClass.CDC if is_cdc else cls_reg)
                    addp(start, r.hierarchical_name, cls, launch, capture, v)
                # Register-as-output: only when starting from THIS
                # register and at zero hops.
                if start_is_reg_q and sig == start and sig in top_outputs:
                    addp(start, sig, TimingPathClass.REG_TO_OUTPUT,
                         launch, None, v)
            else:
                # sig is a plain output port (not also a register Q).
                if sig in top_outputs:
                    out_cls = (TimingPathClass.REG_TO_OUTPUT
                               if cls_reg == TimingPathClass.REG_TO_REG
                               else TimingPathClass.INPUT_TO_OUTPUT)
                    addp(start, sig, out_cls, launch, None, v)

        # ------------------------------------------------------------------
        # INPUT -> REG / INPUT -> OUTPUT
        # ------------------------------------------------------------------
        for inp in top_inputs:
            reached, vias = comb_sweep(inp)
            for sig in sorted(reached):
                v = vias.get(sig, [])
                report_endpoints(inp, sig, v,
                                 cls_reg=TimingPathClass.INPUT_TO_REG,
                                 launch=None,
                                 start_is_reg_q=False)

        # ------------------------------------------------------------------
        # REG -> REG (including CDC) / REG -> OUTPUT
        # ------------------------------------------------------------------
        for src_r in regs:
            q = src_r.q_name()
            launch = _clk_leaf(src_r.clock_signal)
            reached, vias = comb_sweep(q)
            for sig in sorted(reached):
                v = vias.get(sig, [])
                report_endpoints(q, sig, v,
                                 cls_reg=TimingPathClass.REG_TO_REG,
                                 launch=launch,
                                 start_is_reg_q=(sig == q))

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _via_label(self, src: str, dst: str) -> str:
        for m in self.edge_meta.get((src, dst), []):
            if m.via:
                return m.via
        return f"{src}->{dst}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signal_exists(d: Design, hier: str) -> bool:
    return (hier in d.nets or hier in d.ports or hier in d.registers
            or hier in d.instances)


def _clk_leaf(clk_signal: str | None) -> str | None:
    if not clk_signal:
        return None
    return clk_signal.split(".")[-1]
