"""
TimingGraph orchestration: builds clocks, resets, domains, and path
classifications from a structurally-analyzed Design model (WP-D).

Design principles:

* Clock/reset discovery is **evidence-driven**. Structural facts
  (edge-sensitive use, registers clocked, reset branches) produce HIGH
  confidence; name heuristics produce LOW confidence and can never
  alone set a clock or reset.
* The default relationship between any two clocks is UNKNOWN. It is
  only refined when structural or user evidence exists.
* CDC detection is derived from the Step-1 structural graph: a real
  path that crosses from a register in one clock domain to a register
  in another is marked CDC; CDC observation is evidence (not proof)
  that clocks may be asynchronous. We do NOT emit ``set_false_path``
  or ``set_clock_groups -asynchronous`` automatically.
* Generated-clock, clock-gating, and clock-mux detection produces
  **candidate** records (POSSIBLE_GENERATED_CLOCK) with attached
  evidence; confirmation is required before ``create_generated_clock``
  is emitted downstream.
* Input/output clock association is recorded explicitly; when unknown
  it is left as ``None`` and surfaced via ``missing_information()``.
* Ordering is deterministic: all iterations sort signal/register/path
  names so two runs on the same input produce identical results.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..design_model import Design
from ..utils.enums import (
    ClockDomainRelationship,
    ClockEdge,
    DependencyKind,
    ResetPolarity,
    ResetType,
    TimingPathClass,
)
from ..utils.logging import get_logger
from .clock import Clock, ClockEvidence, ClockEvidenceKind
from .clock_domain import ClockDomain, ClockDomainEdge
from .reset import Reset, ResetEvidence, ResetEvidenceKind
from .timing_path import TimingPath

log = get_logger("timing")

# Names that suggest a clock for *weak* naming-hint evidence only.
_CLOCK_NAME_HINTS = (
    "clk", "clock", "gclk", "sclk", "mclk", "pclk", "aclk",
    "sys_clk", "clk_",
)
_RESET_NAME_HINTS = (
    "rst", "reset", "rst_n", "reset_n", "arst", "srst",
)


class TimingGraph(BaseModel):
    """Container for all timing-related information extracted from a Design."""
    clocks: dict[str, Clock] = Field(default_factory=dict)
    resets: dict[str, Reset] = Field(default_factory=dict)
    domains: dict[str, ClockDomain] = Field(default_factory=dict)
    domain_edges: list[ClockDomainEdge] = Field(default_factory=list)
    paths: list[TimingPath] = Field(default_factory=list)
    # Conservative candidates (all require confirmation):
    generated_clock_candidates: list[dict[str, Any]] = Field(default_factory=list)
    clock_mux_candidates: list[dict[str, Any]] = Field(default_factory=list)
    clock_gating_candidates: list[dict[str, Any]] = Field(default_factory=list)
    # Input/output port -> associated clock (None when unknown).
    input_clock_assoc: dict[str, str | None] = Field(default_factory=dict)
    output_clock_assoc: dict[str, str | None] = Field(default_factory=dict)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # ------------------------------------------------------------------
    # Build from Design
    # ------------------------------------------------------------------

    @classmethod
    def build(cls, design: Design, user_clocks: list[dict[str, Any]] | None = None,
              user_relationships: list[dict[str, Any]] | None = None) -> "TimingGraph":
        """Build a TimingGraph from a structurally-analyzed Design."""
        tg = cls()
        # Ensure connectivity is built (idempotent).
        try:
            design.build_connectivity()
        except Exception as e:  # pragma: no cover - defensive
            log.warning("Structural connectivity build failed: %s", e)

        tg._discover_clocks(design, user_clocks or [])
        tg._detect_clock_gating_and_muxes(design)
        tg._detect_generated_clock_candidates(design)
        tg._discover_resets(design)
        tg._build_domains()
        tg._associate_port_clocks(design)
        tg._apply_user_relationships(user_relationships or [])
        tg._classify_paths_structural(design)
        tg._finalize()

        log.info(
            "Timing graph: %d clocks, %d resets, %d domains, %d edges, %d paths, "
            "%d gen-candidates, %d mux-candidates, %d gate-candidates",
            len(tg.clocks), len(tg.resets), len(tg.domains),
            len(tg.domain_edges), len(tg.paths),
            len(tg.generated_clock_candidates),
            len(tg.clock_mux_candidates),
            len(tg.clock_gating_candidates),
        )
        return tg

    # ------------------------------------------------------------------
    # Clock discovery (Manual §13)
    # ------------------------------------------------------------------

    def _discover_clocks(self, design: Design, user_clocks: list[dict[str, Any]]) -> None:
        """Discover clock candidates using structural evidence, then
        merge user-specified clocks. Evidence categories:
        * EDGE_SENSITIVE / DRIVES_REGISTER / SEQUENTIAL_PROCESS → HIGH
        * TOP_LEVEL_PORT → corroborating
        * NAMING_HINT → LOW only (never sets HIGH alone)."""
        # Gather evidence per clock leaf name.
        ev: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "edge": ClockEdge.POSEDGE,
            "edge_seen": False,
            "regs": set(),
            "procs": set(),
            "ports": set(),
            "hints": set(),
        })

        # From registers.
        for r in design.registers.values():
            if not r.clock_signal:
                continue
            leaf = r.clock_signal.split(".")[-1]
            ev[leaf]["regs"].add(r.hierarchical_name)
            ev[leaf]["edge"] = r.clock_edge

        # From processes.
        for p in design.processes.values():
            if p.inferred_clock:
                leaf = p.inferred_clock
                ev[leaf]["procs"].add(p.id)
                ev[leaf]["edge_seen"] = True
            for s in p.clock_signals:
                leaf = s.split(".")[-1]
                ev[leaf]["procs"].add(p.id)
                ev[leaf]["edge_seen"] = True
                if hasattr(p, "sensitivity"):
                    for si in p.sensitivity:
                        if si.signal.split(".")[-1] == leaf and si.edge is not None:
                            ev[leaf]["edge"] = si.edge

        # From ports: a port is clock evidence only when it has been
        # *structurally* connected to a register clock (via
        # ``connected_clock_candidates``, populated by the parser).
        # Pure naming hints are recorded separately as NAMING_HINT and
        # never, by themselves, create a clock candidate.
        for port in design.ports.values():
            if not _is_top_port(design, port):
                continue
            leaf = port.local_name
            if port.connected_clock_candidates:
                ev[leaf]["ports"].add(port.hierarchical_name)
            else:
                lname = leaf.lower()
                if any(lname == h.rstrip("_") or lname.startswith(h)
                       for h in _CLOCK_NAME_HINTS):
                    ev[leaf]["hints"].add("name matches clock convention (weak)")

        # Also consider non-top signals driven by flops (for internal dividers).
        # They get flagged as generated-clock candidates later; here we only
        # mark them if a process uses them as an edge-sensitive clock.

        for name in sorted(ev.keys()):
            e = ev[name]
            # Naming hints alone NEVER create a clock.  Require at least
            # one piece of structural evidence (edge-sensitive use,
            # register driven, sequential process, or top-level port
            # with connected_clock_candidates i.e. structurally linked
            # to a register clock input).
            has_structural = bool(e["edge_seen"] or e["regs"] or e["procs"]
                                  or e["ports"])
            if not has_structural:
                continue
            clock_id = f"clk_{name}"
            src = next(iter(e["ports"])) if e["ports"] else name
            c = Clock(
                id=clock_id,
                name=name,
                source_object=src,
                source_port_or_pin=src,
                edge=e["edge"],
                registers_driven=sorted(e["regs"]),
                processes=sorted(e["procs"]),
                is_top_level_port=bool(e["ports"]),
                status="PROPOSED",
                source_of_value="INFERENCE",
            )
            # Attach evidence.
            if e["edge_seen"]:
                c.add_evidence(ClockEvidenceKind.EDGE_SENSITIVE,
                               f"used on {e['edge'].value} sensitivity in "
                               f"{len(e['procs'])} process(es)",
                               source=None)
            if e["regs"]:
                c.add_evidence(ClockEvidenceKind.DRIVES_REGISTER,
                               f"drives {len(e['regs'])} register(s)",
                               source=None)
                # If a register is clocked but edge_seen is False (shouldn't
                # happen) it's still strong evidence via reg.
                if not e["edge_seen"]:
                    c.add_evidence(ClockEvidenceKind.SEQUENTIAL_PROCESS,
                                   "register clocked without direct edge event recorded",
                                   source=None)
            if e["procs"] and not e["edge_seen"]:
                c.add_evidence(ClockEvidenceKind.SEQUENTIAL_PROCESS,
                               f"inferred clock in {len(e['procs'])} process(es)",
                               source=None)
            if e["ports"]:
                c.add_evidence(ClockEvidenceKind.TOP_LEVEL_PORT,
                               f"top-level port: {next(iter(e['ports']))}",
                               source=None)
            for hint in sorted(e["hints"]):
                c.add_evidence(ClockEvidenceKind.NAMING_HINT, hint, source=None)
            self.clocks[name] = c

        # Merge user-specified clocks (highest confidence).
        for uc in user_clocks:
            name = uc.get("name")
            if not name:
                continue
            if name in self.clocks:
                c = self.clocks[name]
                c.source_of_value = "USER"
                c.status = "FIXED" if uc.get("fixed", True) else "TUNABLE"
                if uc.get("port"):
                    c.source_object = uc["port"]
                    c.source_port_or_pin = uc["port"]
                c.add_evidence(ClockEvidenceKind.USER_DECLARED,
                               "user-specified clock", source="user")
            else:
                port = uc.get("port", name)
                c = Clock(
                    id=f"clk_{name}",
                    name=name,
                    source_object=port,
                    source_port_or_pin=port,
                    source_of_value="USER",
                    status="FIXED" if uc.get("fixed", True) else "TUNABLE",
                    is_top_level_port=True,
                )
                c.add_evidence(ClockEvidenceKind.USER_DECLARED,
                               "user-specified clock", source="user")
                self.clocks[name] = c
            if uc.get("period_seconds") is not None:
                c.period_seconds = uc["period_seconds"]
            if uc.get("waveform"):
                c.waveform = uc["waveform"]
            if uc.get("uncertainty_seconds") is not None:
                c.uncertainty_seconds = uc["uncertainty_seconds"]
            if uc.get("edge"):
                try:
                    c.edge = ClockEdge(uc["edge"])
                except Exception:
                    pass
            c._recompute_confidence()

    # ------------------------------------------------------------------
    # Reset discovery (Manual §14)
    # ------------------------------------------------------------------

    def _discover_resets(self, design: Design) -> None:
        """Classify each reset candidate using structural evidence.

        * ASYNC reset: signal appears on an edge-sensitive sensitivity
          AND is observed in a negated/positive predicate at a top-level
          if that loads a constant (the reset-value branch).
        * SYNC reset: signal controls a constant-load branch in a
          sequential process but is NOT on the sensitivity list.
        """
        ev: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "regs": set(),
            "procs": set(),
            "edge": None,
            "polarity_from_reg": ResetPolarity.UNKNOWN,
            "type_from_reg": ResetType.UNKNOWN,
            "has_reset_branch": False,
            "in_sensitivity": False,
            "sync_control": False,
            "ports": set(),
            "hints": set(),
        })
        for r in design.registers.values():
            if not r.reset_signal:
                continue
            leaf = r.reset_signal.split(".")[-1]
            ev[leaf]["regs"].add(r.hierarchical_name)
            ev[leaf]["type_from_reg"] = r.reset_type
            ev[leaf]["polarity_from_reg"] = r.reset_polarity
            ev[leaf]["edge"] = r.reset_edge
            if r.reset_type == ResetType.ASYNCHRONOUS:
                ev[leaf]["in_sensitivity"] = True
                ev[leaf]["has_reset_branch"] = True
        for p in design.processes.values():
            for s in p.reset_signals:
                leaf = s.split(".")[-1]
                ev[leaf]["procs"].add(p.id)
                ev[leaf]["in_sensitivity"] = True
                if p.has_reset_branch:
                    ev[leaf]["has_reset_branch"] = True
            # Detect synchronous resets: signals named in control_signals
            # that assign constants (the ``if (rst) q <= '0`` pattern with
            # rst NOT in the sensitivity list).  We only flag signals
            # whose control action loads a constant zero/one into a
            # register AND whose name is not already identified as async
            # reset or clock.
        # Scan registers that have a control_source loading a constant.
        # The parser already classified async resets; anything in
        # control_sources that conditionally loads a constant (and is
        # not clk/async-reset) is a SYNC-reset *candidate*.
        clk_names = set(self.clocks.keys())
        async_rst_names = {n for n, e in ev.items() if e["in_sensitivity"]}
        for r in design.registers.values():
            # We detect sync reset by seeing if any control source
            # feeds a branch where RHS is a constant.  This uses the
            # edge metadata built by connectivity.
            for cs in r.control_sources:
                leaf = cs.split(".")[-1]
                if leaf in clk_names or leaf in async_rst_names:
                    continue
                # Heuristic: a control signal that is the sole
                # predicate on a constant-assign looks like a reset.
                # We only record it when the RHS constant loads 0
                # (the common reset-to-zero pattern).  We approximate
                # this by checking that the edge from ``cs`` to
                # ``r.hierarchical_name`` is CONDITIONAL and that r's
                # data_sources do not include other data besides the
                # self-feedback/constant.
                if _looks_like_sync_reset(design, r, cs):
                    ev[leaf]["regs"].add(r.hierarchical_name)
                    ev[leaf]["sync_control"] = True
                    ev[leaf]["procs"].add(r.process_id or "")
        for port in design.ports.values():
            if not _is_top_port(design, port):
                continue
            leaf = port.local_name
            lname = leaf.lower()
            if any(lname.startswith(h) or lname == h
                   for h in _RESET_NAME_HINTS):
                ev[leaf]["hints"].add("name matches reset convention")
                ev[leaf]["ports"].add(port.hierarchical_name)

        for name in sorted(ev.keys()):
            e = ev[name]
            if not e["regs"] and not e["sync_control"] and not e["in_sensitivity"]:
                # Only naming hints without structural evidence: skip
                # (do not create a Reset object for pure name matches).
                continue
            rst_id = f"rst_{name}"
            src = next(iter(e["ports"])) if e["ports"] else name
            # Determine reset type/polarity from evidence.
            rtype = ResetType.UNKNOWN
            pol = e["polarity_from_reg"]
            edge = e["edge"]
            if e["in_sensitivity"] and e["has_reset_branch"]:
                rtype = ResetType.ASYNCHRONOUS
            elif e["sync_control"]:
                rtype = ResetType.SYNCHRONOUS
                # If not set by parser, infer polarity: look for negation.
                if pol == ResetPolarity.UNKNOWN:
                    pol = _infer_sync_reset_polarity(design, name)
            # Assemble reset.
            r = Reset(
                id=rst_id,
                name=name,
                source_object=src,
                reset_type=rtype,
                edge=edge,
                polarity=pol,
                registers_driven=sorted(e["regs"]),
                processes=sorted(x for x in e["procs"] if x),
                is_top_level_port=bool(e["ports"]),
                source_of_value="INFERENCE",
                associated_clock=_associated_clock_for(design, name, clk_names),
            )
            if e["in_sensitivity"]:
                r.add_evidence(ResetEvidenceKind.EDGE_SENSITIVE,
                               "on sensitivity list of sequential process",
                               source=None)
            if e["has_reset_branch"]:
                r.add_evidence(ResetEvidenceKind.RESET_BRANCH,
                               "loads constant in dedicated reset branch",
                               source=None)
            if e["sync_control"]:
                r.add_evidence(ResetEvidenceKind.SYNC_CONTROL,
                               "guards constant load inside clocked process",
                               source=None)
            if e["regs"]:
                r.add_evidence(ResetEvidenceKind.REGISTER_ASSIGN,
                               f"controls reset of {len(e['regs'])} register(s)",
                               source=None)
            for hint in sorted(e["hints"]):
                r.add_evidence(ResetEvidenceKind.NAMING_HINT, hint, source=None)
            self.resets[name] = r

    # ------------------------------------------------------------------
    # Clock gating / mux detection
    # ------------------------------------------------------------------

    def _detect_clock_gating_and_muxes(self, design: Design) -> None:
        """Identify clock-gating and clock-mux candidates from continuous
        assignments (``assign gated_clk = clk & en`` / ``assign clk_out =
        sel ? clk_a : clk_b``). These are CANDIDATES only — confirmation
        is required before treating them as real clock structures. We do
        NOT treat every ``clk & x`` expression as a clock gate.

        Detection is structural: we look at the RHS of continuous
        assignments and check whether the destination is used as an
        edge-sensitive clock (either already in ``self.clocks`` from
        register/process evidence, or observed on a sensitivity list in
        any process).  Source-side "clock-like" signals include:
          * already-discovered clocks in ``self.clocks``
          * top-level input ports connected to register clocks
          * top-level input ports with clock-naming hints
        """
        clk_names = set(self.clocks.keys())

        # Signals used as edge-sensitive clocks anywhere in the design.
        edge_sensitive_leaves: set[str] = set()
        for p in design.processes.values():
            for s in p.clock_signals:
                edge_sensitive_leaves.add(s.split(".")[-1])

        clock_like_inputs: set[str] = set()
        for port in design.ports.values():
            if not _is_top_port(design, port):
                continue
            if port.direction.value != "input":
                continue
            leaf = port.local_name
            if leaf in clk_names or port.connected_clock_candidates:
                clock_like_inputs.add(leaf)
                continue
            lname = leaf.lower()
            if any(lname == h.rstrip("_") or lname.startswith(h)
                   for h in _CLOCK_NAME_HINTS):
                clock_like_inputs.add(leaf)

        def _is_clock_like(leaf: str) -> bool:
            return leaf in clk_names or leaf in clock_like_inputs

        # Collect continuous-assign comb edges keyed by dst.
        assign_by_dst: dict[str, list[Any]] = defaultdict(list)
        for e in design.comb_edges:
            if e.via and ".assign" in e.via:
                assign_by_dst[e.dst].append(e)

        # Reset candidates to avoid double-listing on re-entry.
        self.clock_mux_candidates.clear()
        self.clock_gating_candidates.clear()

        for dst, edges in assign_by_dst.items():
            dst_leaf = dst.split(".")[-1]
            # Only flag gate/mux when the destination is itself used as
            # a clock (edge-sensitive), or when we already know it as a
            # clock.  This avoids flagging ``clk & x`` data logic.
            dst_is_clocked = (dst_leaf in clk_names
                              or dst_leaf in edge_sensitive_leaves)

            src_signals = {e.src.split(".")[-1]: e for e in edges}
            src_leaves = set(src_signals)
            clk_in_rhs = {n for n in src_leaves if _is_clock_like(n)}
            if not clk_in_rhs:
                continue
            mux_sel = [n for n, e in src_signals.items()
                       if e.kind == DependencyKind.MUX_SELECT]
            non_clk_srcs = [n for n in src_leaves if not _is_clock_like(n)]

            # Clock mux: ternary between two clocks whose output is used
            # as a clock (or flagged as clock-like).
            if len(clk_in_rhs) >= 2 and mux_sel and dst_is_clocked:
                cand = {
                    "output": dst_leaf,
                    "sources": sorted(clk_in_rhs),
                    "select": mux_sel[0],
                    "evidence": [f"continuous assign to {dst_leaf} selects "
                                 f"between clocks {sorted(clk_in_rhs)}"],
                    "type": "clock_mux_candidate",
                    "user_confirmation_required": True,
                }
                self.clock_mux_candidates.append(cand)
                for cn in clk_in_rhs:
                    if cn in self.clocks:
                        self.clocks[cn].is_mux = True
                        self.clocks[cn].mux_select_signal = mux_sel[0]
                        self.clocks[cn].mux_sources = sorted(clk_in_rhs)
                        self.clocks[cn].add_evidence(ClockEvidenceKind.CLOCK_MUX,
                            f"participates in clock mux driving {dst_leaf}",
                            source=None)
                continue

            # Clock gate: single clock AND/OR enable, output used as clock.
            if (dst_is_clocked and len(clk_in_rhs) == 1
                    and len(non_clk_srcs) == 1
                    and not mux_sel):
                clk_src = next(iter(clk_in_rhs))
                en = non_clk_srcs[0]
                # Only flag binary AND/OR style combines; check edge context.
                op_ctxs = {e.context for e in edges}
                if op_ctxs & {"&", "|", "^", "and", "or", "&&", "||"}:
                    cand = {
                        "output": dst_leaf,
                        "clock": clk_src,
                        "enable": en,
                        "evidence": [f"continuous assign to {dst_leaf} combines "
                                     f"clock '{clk_src}' with signal '{en}' via '{next(iter(op_ctxs))}'"],
                        "type": "gated_clock_candidate",
                        "user_confirmation_required": True,
                    }
                    self.clock_gating_candidates.append(cand)
                    if clk_src in self.clocks:
                        self.clocks[clk_src].is_gated = True
                        self.clocks[clk_src].gate_enable_signal = en
                        self.clocks[clk_src].add_evidence(ClockEvidenceKind.GATED_CLOCK,
                            f"possible clock gating via '{en}' driving {dst_leaf}",
                            source=None)
        self.clock_mux_candidates.sort(key=lambda c: c["output"])
        self.clock_gating_candidates.sort(key=lambda c: c["output"])

    # ------------------------------------------------------------------
    # Generated-clock candidates
    # ------------------------------------------------------------------

    def _detect_generated_clock_candidates(self, design: Design) -> None:
        """Identify *possible* generated clocks: register Q outputs that
        are themselves used as edge-sensitive clocks for other registers
        (classic clock-divider pattern).  These are POSSIBLE candidates;
        confirmation is required before we treat them as clocks.  We
        don't infer divide-by, source relationship, etc., unless we can
        prove it structurally."""
        # A signal is a candidate generated clock when it (a) is itself
        # a register Q output, and (b) is used edge-sensitively (it
        # drives some other register's clock).  This covers both the
        # case where it was promoted to a Clock by _discover_clocks and
        # the case where it wasn't.
        reg_by_local: dict[str, list[Any]] = defaultdict(list)
        for r in design.registers.values():
            reg_by_local[r.local_name].append(r)

        # Collect edge-sensitive clock signals from all processes.
        edge_sensitive: set[str] = set()
        for p in design.processes.values():
            for s in p.clock_signals:
                edge_sensitive.add(s.split(".")[-1])

        seen: set[str] = set()
        for leaf in sorted(edge_sensitive):
            if leaf in seen:
                continue
            src_regs = reg_by_local.get(leaf, [])
            if not src_regs:
                continue
            # It's a register output used edge-sensitively. Skip primary
            # top-level input clocks that happen to also drive a reg of
            # the same name (impossible in Verilog, but defensive).
            src_reg = sorted(src_regs, key=lambda r: r.hierarchical_name)[0]
            master: str | None = None
            if src_reg.clock_signal:
                master = src_reg.clock_signal.split(".")[-1]
            if master == leaf:
                continue  # self-feeding; not generated
            seen.add(leaf)
            cand = {
                "output": leaf,
                "master_clock": master,
                "source_register": src_reg.hierarchical_name,
                "evidence": [f"register output '{leaf}' used as edge-sensitive clock"
                             + (f", master clock appears to be '{master}'" if master else "")],
                "type": "possible_generated_clock",
                "divide_by": None,
                "user_confirmation_required": True,
            }
            self.generated_clock_candidates.append(cand)
            # Mark the clock as generated if it exists in our clock table.
            if leaf in self.clocks:
                self.clocks[leaf].is_generated = True
                self.clocks[leaf].parent_clock = master
                self.clocks[leaf].add_evidence(
                    ClockEvidenceKind.GENERATED_CLOCK,
                    f"generated from master clock '{master}'" if master else "possible generated clock",
                    source=None,
                )

    # ------------------------------------------------------------------
    # Domains
    # ------------------------------------------------------------------

    def _build_domains(self) -> None:
        """One domain per primary clock, populated with its registers."""
        for clk_name in sorted(self.clocks.keys()):
            c = self.clocks[clk_name]
            did = f"dom_{clk_name}"
            d = ClockDomain(
                id=did,
                name=clk_name,
                clock_ids=[c.id],
                register_paths=list(c.registers_driven),
                sources=[c.source_object],
                evidence=[f"{len(c.registers_driven)} register(s) clocked by {clk_name}"],
            )
            c.domain_id = did
            self.domains[did] = d
        # Attach resets to the domain of their associated clock.
        for rname, r in self.resets.items():
            if r.associated_clock and r.associated_clock in self.clocks:
                did = self.clocks[r.associated_clock].domain_id
                if did and did in self.domains:
                    self.domains[did].reset_ids.append(r.id)
                    r.domain_id = did
        # Default all clock pairs to UNKNOWN.
        names = sorted(self.clocks.keys())
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                edge = ClockDomainEdge(
                    clock_a=names[i],
                    clock_b=names[j],
                    relationship=ClockDomainRelationship.UNKNOWN,
                    confidence="UNKNOWN",
                    user_confirmation_required=True,
                    evidence=["no relationship evidence; defaulting to UNKNOWN"],
                )
                self.domain_edges.append(edge)

    def _associate_port_clocks(self, design: Design) -> None:
        """For each top-level input/output port, try to identify which
        clock domain it feeds/is fed by.  Leave UNKNOWN when there is no
        structural evidence (we do not assume 'first clock')."""
        g = getattr(design, "_structural_graph_internal", None)
        data_fanout = g.data_fanout if g else {}
        data_fanin = g.data_fanin if g else {}
        for port in design.ports.values():
            if not _is_top_port(design, port):
                continue
            leaf = port.local_name
            if leaf in self.clocks or leaf in self.resets:
                # Clock/reset ports are their own association.
                continue
            # Input: find the registers this port eventually clocks via data_fanout.
            if port.direction.value == "input":
                dom = _domain_of_fanout(design, self, port.hierarchical_name)
                self.input_clock_assoc[port.hierarchical_name] = dom
            elif port.direction.value == "output":
                dom = _domain_of_fanin(design, self, port.hierarchical_name)
                self.output_clock_assoc[port.hierarchical_name] = dom

    def _apply_user_relationships(self, user_rels: list[dict[str, Any]]) -> None:
        rel_map = {
            "synchronous": ClockDomainRelationship.SYNCHRONOUS,
            "related": ClockDomainRelationship.RELATED,
            "asynchronous": ClockDomainRelationship.ASYNCHRONOUS,
            "unknown": ClockDomainRelationship.UNKNOWN,
        }
        for ur in user_rels:
            clocks = ur.get("clocks", [])
            rel = rel_map.get(ur.get("relationship", "").lower())
            if rel is None or len(clocks) < 2:
                continue
            fixed = bool(ur.get("fixed", True))
            for i in range(len(clocks)):
                for j in range(i + 1, len(clocks)):
                    a, b = clocks[i], clocks[j]
                    matched = False
                    for e in self.domain_edges:
                        if {e.clock_a, e.clock_b} == {a, b}:
                            e.set_relationship(
                                rel,
                                f"user-specified as {rel.value}",
                                confidence="HIGH",
                                user_confirm=not fixed,
                            )
                            matched = True
                            break
                    if not matched:
                        # Both clocks may not be in the design (e.g. user
                        # declares a virtual clock); create a new edge.
                        de = ClockDomainEdge(
                            clock_a=a, clock_b=b,
                            relationship=rel,
                            confidence="HIGH",
                            user_confirmation_required=not fixed,
                            evidence=[f"user-specified as {rel.value}"],
                        )
                        self.domain_edges.append(de)

    # ------------------------------------------------------------------
    # Path classification (Manual §17)
    # ------------------------------------------------------------------

    def _classify_paths_structural(self, design: Design) -> None:
        """Promote structural paths from Step 1 into TimingPath records.
        CDC classification follows launch/capture domain mismatch.  When
        a CDC path is observed we record evidence on the corresponding
        domain edge but do NOT mark the clocks asynchronous."""
        reg_clk: dict[str, str | None] = {}
        for r in design.registers.values():
            leaf = r.clock_signal.split(".")[-1] if r.clock_signal else None
            reg_clk[r.hierarchical_name] = leaf

        pid = 0
        sp_iter = list(getattr(design, "structural_paths", []))
        cdc_iter = list(getattr(design, "cdc_paths", []))
        all_paths = sp_iter + cdc_iter
        for sp in sorted(all_paths, key=lambda p: (p.startpoint, p.endpoint, p.path_class.value)):
            pid += 1
            start = sp.startpoint
            end = sp.endpoint
            pcls = sp.path_class
            launch = sp.launch_clock
            capture = sp.capture_clock
            if start in reg_clk and launch is None:
                launch = reg_clk[start]
            if end in reg_clk and capture is None:
                capture = reg_clk[end]
            is_cdc = (pcls == TimingPathClass.CDC
                      or (launch and capture and launch != capture))
            if is_cdc:
                pcls = TimingPathClass.CDC
            tp = TimingPath(
                id=f"p{pid:04d}",
                startpoint=start,
                endpoint=end,
                launch_clock=launch,
                capture_clock=capture,
                path_type=pcls,
                combinational_elements=list(getattr(sp, "combinational_via", [])),
            )
            self.paths.append(tp)

        # Record CDC observation on the domain edges.
        edge_idx = {(e.clock_a, e.clock_b): e for e in self.domain_edges}
        edge_idx.update({(e.clock_b, e.clock_a): e for e in self.domain_edges})
        for tp in self.paths:
            if tp.path_type != TimingPathClass.CDC:
                continue
            if not (tp.launch_clock and tp.capture_clock):
                continue
            e = edge_idx.get((tp.launch_clock, tp.capture_clock))
            if e is None:
                continue
            e.cdc_paths_observed += 1
            if "structural CDC path observed" not in e.evidence:
                e.evidence.append("structural CDC path observed")

    def _finalize(self) -> None:
        """Final sorting and confidence recomputation after all mutations."""
        for c in self.clocks.values():
            c._recompute_confidence()
        for r in self.resets.values():
            r._recompute_confidence()
        # Deterministic ordering.
        self.paths.sort(key=lambda p: (p.startpoint, p.endpoint, p.path_type.value))
        self.domain_edges.sort(key=lambda e: (e.clock_a, e.clock_b))
        self.generated_clock_candidates.sort(key=lambda c: (c["output"], c.get("master_clock") or ""))
        self.clock_mux_candidates.sort(key=lambda c: c["output"])
        self.clock_gating_candidates.sort(key=lambda c: c["output"])

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def clock_by_name(self, name: str) -> Clock | None:
        return self.clocks.get(name)

    def reset_by_name(self, name: str) -> Reset | None:
        return self.resets.get(name)

    def domain_of(self, register_hier: str) -> ClockDomain | None:
        for d in self.domains.values():
            if register_hier in d.register_paths:
                return d
        return None

    def missing_information(self) -> list[dict[str, str]]:
        """Return structured list of missing/ambiguous information (Manual §22)."""
        out: list[dict[str, str]] = []
        for c in sorted(self.clocks.values(), key=lambda c: c.name):
            if c.period_seconds is None and c.source_of_value != "EXISTING_SDC":
                out.append({
                    "category": "clock_period",
                    "object": c.name,
                    "severity": "required",
                    "message": f"Clock '{c.name}' detected ({c.confidence} confidence) "
                               f"but its period is unknown.",
                })
        for e in sorted(self.domain_edges, key=lambda x: (x.clock_a, x.clock_b)):
            if e.relationship == ClockDomainRelationship.UNKNOWN and e.user_confirmation_required:
                cdc_note = (f"; {e.cdc_paths_observed} CDC path(s) observed"
                            if e.cdc_paths_observed else "")
                out.append({
                    "category": "clock_relationship",
                    "object": f"{e.clock_a} <-> {e.clock_b}",
                    "severity": "recommended",
                    "message": (f"Relationship between '{e.clock_a}' and '{e.clock_b}' "
                                f"is unknown{cdc_note}; CDC handling needs confirmation."),
                })
        # Inputs/outputs with unknown clock association.
        for h in sorted(self.input_clock_assoc):
            if self.input_clock_assoc[h] is None:
                leaf = h.split(".")[-1]
                out.append({
                    "category": "input_clock_association",
                    "object": leaf,
                    "severity": "recommended",
                    "message": f"Input port '{leaf}' has no identified clock association.",
                })
        for h in sorted(self.output_clock_assoc):
            if self.output_clock_assoc[h] is None:
                leaf = h.split(".")[-1]
                out.append({
                    "category": "output_clock_association",
                    "object": leaf,
                    "severity": "recommended",
                    "message": f"Output port '{leaf}' has no identified clock association.",
                })
        # Generated-clock candidates require confirmation.
        for gc in self.generated_clock_candidates:
            out.append({
                "category": "generated_clock_candidate",
                "object": gc["output"],
                "severity": "confirmation_required",
                "message": (f"Possible generated clock at '{gc['output']}' "
                            f"(master: {gc.get('master_clock')}); confirmation required."),
            })
        for cm in self.clock_mux_candidates:
            out.append({
                "category": "clock_mux_candidate",
                "object": cm["output"],
                "severity": "confirmation_required",
                "message": (f"Possible clock mux at '{cm['output']}' between "
                            f"{cm['sources']} (sel={cm['select']}); intent confirmation required."),
            })
        for cg in self.clock_gating_candidates:
            out.append({
                "category": "clock_gating_candidate",
                "object": cg["output"],
                "severity": "confirmation_required",
                "message": (f"Possible gated clock at '{cg['output']}' "
                            f"(clk={cg['clock']}, en={cg['enable']}); intent confirmation required."),
            })
        return out

    def summary(self) -> dict[str, Any]:
        return {
            "clocks": {n: c.summary() for n, c in sorted(self.clocks.items())},
            "resets": {n: r.summary() for n, r in sorted(self.resets.items())},
            "domains": {d.id: d.summary() for d in sorted(self.domains.values(), key=lambda x: x.id)},
            "domain_edges": [e.summary() for e in self.domain_edges],
            "paths": len(self.paths),
            "cdc_paths": sum(1 for p in self.paths if p.path_type == TimingPathClass.CDC),
            "generated_clock_candidates": list(self.generated_clock_candidates),
            "clock_mux_candidates": list(self.clock_mux_candidates),
            "clock_gating_candidates": list(self.clock_gating_candidates),
            "input_clock_assoc": dict(sorted(self.input_clock_assoc.items())),
            "output_clock_assoc": dict(sorted(self.output_clock_assoc.items())),
            "missing_info": self.missing_information(),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_top_port(design: Design, port) -> bool:
    """True when the port belongs to the top module."""
    top = design.top_module
    if hasattr(port, "parent_module"):
        return port.parent_module == top
    return port.hierarchical_name.split(".")[0] == top if "." in port.hierarchical_name else True


def _associated_clock_for(design: Design, rst_leaf: str, clk_names: set[str]) -> str | None:
    """Best-effort: associate a reset with the clock used by the registers
    it resets. Returns a single clock leaf name or None."""
    assoc: dict[str, int] = defaultdict(int)
    for r in design.registers.values():
        if r.reset_signal and r.reset_signal.split(".")[-1] == rst_leaf and r.clock_signal:
            assoc[r.clock_signal.split(".")[-1]] += 1
    if not assoc:
        return None
    return sorted(assoc.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _looks_like_sync_reset(design, register, ctrl_signal_hier: str) -> bool:
    """Heuristic: a control signal that guards a constant-0 assignment.
    Conservative — returns True only when the RHS appears to be a simple
    constant zero/one load without other data inputs. Used only to flag
    sync-reset CANDIDATES; these get MEDIUM confidence."""
    # If register has data_sources beyond self-feedback and we have a
    # control guard, that guard is a normal if-enable, not a reset.
    non_self_data = [s for s in register.data_sources
                     if s.split(".")[-1] != register.local_name]
    if non_self_data:
        return False
    # If any other control source exists it's more likely an enable/mux.
    other_ctrl = [c for c in register.control_sources if c != ctrl_signal_hier]
    if other_ctrl:
        return False
    return True


def _infer_sync_reset_polarity(design, rst_leaf: str) -> ResetPolarity:
    """Attempt to determine sync-reset polarity by looking for a
    negated/non-negated reference in the controlling predicate. This is
    best-effort and defaults to UNKNOWN."""
    for e in design.comb_edges:
        if e.src.split(".")[-1] != rst_leaf:
            continue
        if e.kind != DependencyKind.CONDITIONAL:
            continue
        ctx = (e.context or "").lower()
        if ctx in ("!", "~", "logicalnot", "bitwisenot"):
            return ResetPolarity.ACTIVE_LOW
        # Plain identifier → active high (if <rst>).
        if ctx in ("if", "ifcase") or ctx == "":
            return ResetPolarity.ACTIVE_HIGH
    return ResetPolarity.UNKNOWN


def _domain_of_fanout(design, tg: TimingGraph, start_hier: str) -> str | None:
    g = getattr(design, "_structural_graph_internal", None)
    if g is None:
        return None
    # Direct: start_hier directly feeds a register's D (from data_sources)?
    reg_by_d: dict[str, list[str]] = defaultdict(list)
    for r in design.registers.values():
        for ds in r.data_sources:
            reg_by_d[ds].append(r.hierarchical_name)
    visited = {start_hier}
    queue = [start_hier]
    domains: dict[str, int] = defaultdict(int)
    while queue:
        sig = queue.pop(0)
        if sig in reg_by_d:
            for rh in reg_by_d[sig]:
                r = design.registers.get(rh)
                if r and r.clock_signal:
                    domains[r.clock_signal.split(".")[-1]] += 1
        for nxt in sorted(g.data_fanout.get(sig, ())):
            if nxt in visited:
                continue
            visited.add(nxt)
            queue.append(nxt)
    if not domains:
        return None
    return sorted(domains.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _domain_of_fanin(design, tg: TimingGraph, end_hier: str) -> str | None:
    g = getattr(design, "_structural_graph_internal", None)
    if g is None:
        return None
    # BFS backwards through data_fanin to find a register Q (which has a clock).
    visited = {end_hier}
    queue = [end_hier]
    domains: dict[str, int] = defaultdict(int)
    reg_q_names = {r.q_name(): r for r in design.registers.values()}
    while queue:
        sig = queue.pop(0)
        if sig in reg_q_names:
            r = reg_q_names[sig]
            if r.clock_signal:
                domains[r.clock_signal.split(".")[-1]] += 1
        for prev in sorted(g.data_fanin.get(sig, ())):
            if prev in visited:
                continue
            visited.add(prev)
            queue.append(prev)
    if not domains:
        return None
    return sorted(domains.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
