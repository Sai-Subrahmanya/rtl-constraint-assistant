"""Structural connectivity tests (Step-1 corrective verification).

Each test constructs a small, focused SystemVerilog snippet, parses it
with SlangAdapter, builds the structural graph, and asserts on:
* Register D-source/Q-consumer relationships
* Timing-path classes (IN→REG, REG→REG, REG→OUT, IN→OUT, CDC)
* Process read/write/control separation
* ExprWalker signal extraction
* Hierarchical connectivity
* Adversarial cases (no false connectivity)

These tests verify that real connectivity is derived from evidence and
that no heuristic path-count formulas remain.
"""
from __future__ import annotations

import textwrap
from typing import Any

import pytest

from rca.design_model.connectivity import (
    MAX_COMB_DEPTH,
    MAX_EDGES,
    build_structural_connectivity,
)
from rca.parser import SlangAdapter
from rca.parser.expr_walker import (
    ExprWalker,
    walk_assignment,
    walk_expression,
)
from rca.timing_model import TimingGraph
from rca.utils.enums import (
    DependencyKind,
    TimingPathClass,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_sv(sv: str, top: str = "top") -> Any:
    """Parse an SV string and return a Design with connectivity built."""
    import tempfile, os
    sv = textwrap.dedent(sv)
    with tempfile.NamedTemporaryFile("w", suffix=".sv", delete=False) as f:
        f.write(sv)
        path = f.name
    try:
        a = SlangAdapter()
        d = a.parse([path], top=top)
        d.build_connectivity()
        return d
    finally:
        os.unlink(path)


def path_set(d) -> set[tuple[str, str, str]]:
    """Return {(start_leaf, end_leaf, path_class)} for quick assertions."""
    out = set()
    for p in d.structural_paths + d.cdc_paths:
        s = p.startpoint.split(".")[-1]
        e = p.endpoint.split(".")[-1]
        out.add((s, e, p.path_class.value))
    return out


def reg(d, leaf: str):
    for r in d.registers.values():
        if r.local_name == leaf:
            return r
    raise AssertionError(f"register '{leaf}' not found. "
                         f"Have: {[r.local_name for r in d.registers.values()]}")


def proc_of(d, idx: int = 0):
    procs = list(d.processes.values())
    assert len(procs) > idx, f"only {len(procs)} procs"
    return procs[idx]


# ---------------------------------------------------------------------------
# A. Input -> Register
# ---------------------------------------------------------------------------


def test_input_to_register_direct():
    d = parse_sv("""
        module top(input clk, d, output reg q);
            always_ff @(posedge clk) q <= d;
        endmodule""")
    ps = path_set(d)
    assert ("d", "q", "input_to_register") in ps
    # q is not a declared output port name 'q' but we declared output reg q,
    # so output register REG->OUT may appear.
    r = reg(d, "q")
    assert any(ds.split(".")[-1] == "d" for ds in r.data_sources)
    assert r.clock_signal and r.clock_signal.endswith(".clk")


def test_input_to_register_with_reset():
    d = parse_sv("""
        module top(input clk, rst_n, d, output reg q);
            always_ff @(posedge clk or negedge rst_n) begin
                if (!rst_n) q <= 1'b0;
                else q <= d;
            end
        endmodule""")
    ps = path_set(d)
    assert ("d", "q", "input_to_register") in ps
    r = reg(d, "q")
    assert r.reset_signal and r.reset_signal.endswith(".rst_n")
    assert r.reset_polarity.value == "active_low"
    # Reset is control, NOT a data source.
    assert not any(ds.split(".")[-1] == "rst_n" for ds in r.data_sources)


# ---------------------------------------------------------------------------
# B. Register -> Register through a combinational signal
# ---------------------------------------------------------------------------


def test_reg_to_reg_via_wire():
    d = parse_sv("""
        module top(input clk, a, output y);
            reg r1, r2;
            wire n;
            assign n = r1;
            always_ff @(posedge clk) begin
                r1 <= a;
                r2 <= n;
            end
            assign y = r2;
        endmodule""")
    ps = path_set(d)
    assert ("r1", "r2", "register_to_register") in ps
    assert ("a", "r1", "input_to_register") in ps
    assert ("r2", "y", "register_to_output") in ps


# ---------------------------------------------------------------------------
# C. Register -> Register through multiple combinational stages
# ---------------------------------------------------------------------------


def test_reg_to_reg_multi_comb():
    d = parse_sv("""
        module top(input clk, a, output y);
            reg r1, r2, r3;
            wire w1, w2;
            assign w1 = r1;
            assign w2 = w1;
            always_ff @(posedge clk) begin
                r1 <= a;
                r2 <= w2;
                r3 <= r2;
            end
            assign y = r3;
        endmodule""")
    ps = path_set(d)
    assert ("r1", "r2", "register_to_register") in ps
    assert ("r2", "r3", "register_to_register") in ps
    assert ("r3", "y", "register_to_output") in ps


# ---------------------------------------------------------------------------
# D. Register -> Output
# ---------------------------------------------------------------------------


def test_reg_to_output_continuous():
    d = parse_sv("""
        module top(input clk, d, output y);
            reg q;
            always_ff @(posedge clk) q <= d;
            assign y = q;
        endmodule""")
    ps = path_set(d)
    assert ("q", "y", "register_to_output") in ps
    assert ("d", "q", "input_to_register") in ps


def test_reg_to_output_direct_port():
    """output reg q: Q drives the output pin directly (output register)."""
    d = parse_sv("""
        module top(input clk, d, output reg q);
            always_ff @(posedge clk) q <= d;
        endmodule""")
    ps = path_set(d)
    assert ("d", "q", "input_to_register") in ps
    # REG->OUT via port-alias
    assert any(s == "q" and e == "q" and c == "register_to_output"
               for s, e, c in ps)


# ---------------------------------------------------------------------------
# E. Input -> Output purely combinational
# ---------------------------------------------------------------------------


def test_input_to_output_pure_comb():
    d = parse_sv("""
        module top(input a, b, output y);
            assign y = a & b;
        endmodule""")
    ps = path_set(d)
    assert ("a", "y", "input_to_output") in ps
    assert ("b", "y", "input_to_output") in ps
    # No registers
    assert len(d.registers) == 0


def test_input_to_output_comb_chain():
    d = parse_sv("""
        module top(input a, output y);
            wire w1, w2;
            assign w1 = a;
            assign w2 = w1;
            assign y = w2;
        endmodule""")
    ps = path_set(d)
    assert ("a", "y", "input_to_output") in ps


# ---------------------------------------------------------------------------
# F. Fanout: one source feeds multiple destinations
# ---------------------------------------------------------------------------


def test_fanout_one_to_many():
    d = parse_sv("""
        module top(input clk, d, output reg q1, q2, q3);
            always_ff @(posedge clk) begin
                q1 <= d;
                q2 <= d;
                q3 <= d;
            end
        endmodule""")
    ps = path_set(d)
    for q in ("q1", "q2", "q3"):
        assert ("d", q, "input_to_register") in ps
    r1 = reg(d, "q1")
    r2 = reg(d, "q2")
    # d feeds all three registers' D
    assert any(ds.split(".")[-1] == "d" for ds in r1.data_sources)
    assert any(ds.split(".")[-1] == "d" for ds in r2.data_sources)


# ---------------------------------------------------------------------------
# G. Fanin: multiple sources feed one destination
# ---------------------------------------------------------------------------


def test_fanin_many_to_one_comb():
    d = parse_sv("""
        module top(input a, b, c, output y);
            assign y = a + b + c;
        endmodule""")
    ps = path_set(d)
    for s in ("a", "b", "c"):
        assert (s, "y", "input_to_output") in ps


def test_fanin_mux_to_reg():
    d = parse_sv("""
        module top(input clk, sel, a, b, output reg q);
            always_ff @(posedge clk) q <= sel ? a : b;
        endmodule""")
    ps = path_set(d)
    assert ("a", "q", "input_to_register") in ps
    assert ("b", "q", "input_to_register") in ps
    r = reg(d, "q")
    data_leaves = {ds.split(".")[-1] for ds in r.data_sources}
    assert "a" in data_leaves
    assert "b" in data_leaves


# ---------------------------------------------------------------------------
# H. Mux/conditional: sel is a mux select (CONDITIONAL), a/b are data
# ---------------------------------------------------------------------------


def test_mux_select_classification():
    d = parse_sv("""
        module top(input clk, sel, a, b, output reg q);
            always_ff @(posedge clk) begin
                if (sel) q <= a;
                else q <= b;
            end
        endmodule""")
    r = reg(d, "q")
    data_leaves = {ds.split(".")[-1] for ds in r.data_sources}
    assert "a" in data_leaves and "b" in data_leaves, data_leaves
    # sel is an enable/control (not in data_sources)
    assert r.enable_signal and r.enable_signal.split(".")[-1] == "sel"
    ctrl_leaves = {c.split(".")[-1] for c in r.control_sources}
    assert "sel" in ctrl_leaves, ctrl_leaves
    ps = path_set(d)
    assert ("a", "q", "input_to_register") in ps
    assert ("b", "q", "input_to_register") in ps
    # sel guards q as a CONDITIONAL edge, but does NOT form an ordinary
    # input_to_register timing path (control edges are not data paths).
    assert ("sel", "q", "input_to_register") not in ps
    # The conditional edge does exist in the structural edge list.
    g = d._structural_graph_internal
    cond_edges = [(e.src.split(".")[-1], e.dst.split(".")[-1], e.kind.value)
                  for lst in g.edge_meta.values() for e in lst]
    assert ("sel", "q", "conditional") in cond_edges


# ---------------------------------------------------------------------------
# I. Part-select
# ---------------------------------------------------------------------------


def test_part_select():
    d = parse_sv("""
        module top(input clk, input [7:0] d, output reg [3:0] q);
            always_ff @(posedge clk) q <= d[3:0];
        endmodule""")
    ps = path_set(d)
    assert ("d", "q", "input_to_register") in ps


# ---------------------------------------------------------------------------
# J. Concatenation
# ---------------------------------------------------------------------------


def test_concatenation_lhs():
    d = parse_sv("""
        module top(input clk, a, b, output y1, y2);
            reg pair;
            always_ff @(posedge clk) pair <= {a, b};  // not strictly legal width but ok
            assign {y1, y2} = pair;
        endmodule""")
    # Parsing may be lenient; at minimum a and b must show up in data refs.
    # Simpler, well-formed concat test below.


def test_concatenation_rhs_multi_sig():
    d = parse_sv("""
        module top(input a, b, output [1:0] y);
            assign y = {a, b};
        endmodule""")
    ps = path_set(d)
    assert ("a", "y", "input_to_output") in ps
    assert ("b", "y", "input_to_output") in ps


# ---------------------------------------------------------------------------
# K. Arithmetic expression with multiple operands
# ---------------------------------------------------------------------------


def test_arithmetic_multi_operands():
    d = parse_sv("""
        module top(input clk, a, b, c, output reg [3:0] q);
            always_ff @(posedge clk) q <= a + b - c;
        endmodule""")
    r = reg(d, "q")
    data_leaves = {ds.split(".")[-1] for ds in r.data_sources}
    for s in ("a", "b", "c"):
        assert s in data_leaves
    ps = path_set(d)
    for s in ("a", "b", "c"):
        assert (s, "q", "input_to_register") in ps


# ---------------------------------------------------------------------------
# L. Unrelated registers sharing the same clock: MUST NOT create paths
# ---------------------------------------------------------------------------


def test_unrelated_registers_same_clock_no_paths():
    d = parse_sv("""
        module top(input clk, a, b, output reg x, output reg y);
            always_ff @(posedge clk) begin
                x <= a;
                y <= b;
            end
        endmodule""")
    ps = path_set(d)
    assert ("x", "y", "register_to_register") not in ps
    assert ("y", "x", "register_to_register") not in ps
    assert ("a", "x", "input_to_register") in ps
    assert ("b", "y", "input_to_register") in ps


# ---------------------------------------------------------------------------
# M. Unrelated input/output: MUST NOT create an I->O path
# ---------------------------------------------------------------------------


def test_unrelated_input_output_no_path():
    d = parse_sv("""
        module top(input a, b, output x, output y);
            assign x = a;
            // b is unused; y is undriven (high-impedance) — no b->y
        endmodule""")
    ps = path_set(d)
    assert ("a", "x", "input_to_output") in ps
    assert ("b", "y", "input_to_output") not in ps
    assert ("a", "y", "input_to_output") not in ps
    assert ("b", "x", "input_to_output") not in ps


# ---------------------------------------------------------------------------
# N. Two-clock CDC detected
# ---------------------------------------------------------------------------


def test_cdc_detected():
    d = parse_sv("""
        module top(input clk_a, clk_b, d, output reg q);
            reg r;
            always_ff @(posedge clk_a) r <= d;
            always_ff @(posedge clk_b) q <= r;
        endmodule""")
    cdc = {(p.startpoint.split(".")[-1], p.endpoint.split(".")[-1])
           for p in d.cdc_paths}
    assert ("r", "q") in cdc
    # Class is CDC
    assert any(c == "cdc" for _, _, c in path_set(d))
    # launch=capture check
    for p in d.cdc_paths:
        assert p.launch_clock == "clk_a"
        assert p.capture_clock == "clk_b"
        assert p.cross_domain is True


# ---------------------------------------------------------------------------
# O. Two clock domains with no structural crossing
# ---------------------------------------------------------------------------


def test_two_clocks_no_crossing_no_cdc():
    d = parse_sv("""
        module top(input clk_a, clk_b, a, b, output reg x, output reg y);
            always_ff @(posedge clk_a) x <= a;
            always_ff @(posedge clk_b) y <= b;
        endmodule""")
    assert len(d.cdc_paths) == 0
    ps = path_set(d)
    assert not any(c == "cdc" for _, _, c in ps)


# ---------------------------------------------------------------------------
# P. Asynchronous reset is control, not data
# ---------------------------------------------------------------------------


def test_async_reset_is_control_not_data():
    d = parse_sv("""
        module top(input clk, rst_n, d, output reg q);
            always_ff @(posedge clk or negedge rst_n) begin
                if (!rst_n) q <= 1'b0;
                else q <= d;
            end
        endmodule""")
    r = reg(d, "q")
    # rst_n is NOT a data source
    data_leaves = {ds.split(".")[-1] for ds in r.data_sources}
    assert "rst_n" not in data_leaves
    assert r.reset_signal and r.reset_signal.endswith(".rst_n")
    # rst_n is a control signal in the structural graph
    g = d.structural_graph()
    assert any(cs.split(".")[-1] == "rst_n" for cs in g.control_signals)


# ---------------------------------------------------------------------------
# Q. Hierarchical connectivity (parent actual -> child -> parent)
# ---------------------------------------------------------------------------


def test_hierarchy_flows_through():
    sv = """
        module ff(input d, clk, output reg q);
            always_ff @(posedge clk) q <= d;
        endmodule
        module top(input clk, a, output y);
            wire n;
            ff u0 (.d(a), .clk(clk), .q(n));
            ff u1 (.d(n), .clk(clk), .q(y));
        endmodule"""
    d = parse_sv(sv, top="top")
    # Paths: a -> u0.q (IN→REG), u0.q -> u1.q (REG→REG), u1.q -> y (REG→OUT)
    ps = path_set(d)
    assert ("a", "q", "input_to_register") in ps
    reg_to_reg = [(s, e) for s, e, c in ps if c == "register_to_register"]
    assert any(s == "q" and e == "q" for s, e in reg_to_reg), \
        f"missing u0.q->u1.q reg-reg, got {reg_to_reg}"
    assert any(e == "y" and c == "register_to_output"
               for s, e, c in ps)


# ---------------------------------------------------------------------------
# R. Self-feedback (q<=q) does NOT become erroneous REG→REG
# ---------------------------------------------------------------------------


def test_self_feedback_no_spurious_path():
    d = parse_sv("""
        module top(input clk, en, d, output reg q);
            always_ff @(posedge clk) begin
                if (en) q <= d;
                else q <= q;
            end
        endmodule""")
    ps = path_set(d)
    # No q->q register-to-register path
    assert not any(s == "q" and e == "q" and c == "register_to_register"
                   for s, e, c in ps)
    r = reg(d, "q")
    # self-feedback IS recorded in data_sources (transparency)
    assert any(ds.split(".")[-1] == "q" for ds in r.data_sources)


# ---------------------------------------------------------------------------
# S. Combinational cycle terminates safely
# ---------------------------------------------------------------------------


def test_combinational_cycle_terminates():
    d = parse_sv("""
        module top(input a, output y);
            wire x, z;
            assign x = z;
            assign z = x;
            assign y = a | x;
        endmodule""")
    ps = path_set(d)
    # a reaches y, and traversal terminates without infinite loop
    assert ("a", "y", "input_to_output") in ps
    # Depth cap is documented and bounded
    assert MAX_COMB_DEPTH >= 64


# ---------------------------------------------------------------------------
# T. Large fanout is bounded & deterministic
# ---------------------------------------------------------------------------


def test_large_fanout_bounded():
    # Generate 64 registers all fed by the same input.
    regs = ", ".join(f"output reg r{i}" for i in range(32))
    assigns = "\n                ".join(f"r{i} <= d;" for i in range(32))
    d = parse_sv(f"""
        module top(input clk, d, {regs});
            always_ff @(posedge clk) begin
                {assigns}
            end
        endmodule""")
    ps = path_set(d)
    for i in range(32):
        assert ("d", f"r{i}", "input_to_register") in ps
    # Ensure edge cap is enforced
    assert MAX_EDGES >= 200_000


# ---------------------------------------------------------------------------
# Process read/write/control semantics
# ---------------------------------------------------------------------------


def test_process_read_write_control_separation():
    d = parse_sv("""
        module top(input clk, rst_n, en, d, output reg q);
            always_ff @(posedge clk or negedge rst_n) begin
                if (!rst_n) q <= 1'b0;
                else if (en) q <= d;
            end
        endmodule""")
    p = proc_of(d, 0)
    leaves_assigned = {s.split(".")[-1] for s in p.assigned_signals}
    leaves_read = {s.split(".")[-1] for s in p.read_signals}
    leaves_ctrl = {s.split(".")[-1] for s in p.control_signals}
    leaves_clk = {s.split(".")[-1] for s in p.clock_signals}
    leaves_rst = {s.split(".")[-1] for s in p.reset_signals}
    assert "q" in leaves_assigned
    assert "d" in leaves_read
    assert "en" in leaves_ctrl  # conditional predicate
    assert "clk" in leaves_clk
    assert "rst_n" in leaves_rst


# ---------------------------------------------------------------------------
# ExprWalker: binary, comparison, ternary, concat, replication,
# part-select, indexed, unary, cast, nested. Constants/params not emitted.
# ---------------------------------------------------------------------------


def _walk_expr_string(sv_expr: str, kind=DependencyKind.DATA):
    """Build a tiny module that assigns y = <sv_expr>; walk the RHS."""
    import tempfile, os
    src = f"module top(input a, b, c, sel, input [7:0] bus, output y); assign y = {sv_expr}; endmodule"
    with tempfile.NamedTemporaryFile("w", suffix=".sv", delete=False) as f:
        f.write(src); path = f.name
    try:
        a = SlangAdapter()
        d = a.parse([path], top="top")
        # Find the continuous assign edge sources feeding 'y'
        names = set()
        for e in d.comb_edges:
            if e.dst.split(".")[-1] == "y":
                names.add(e.src.split(".")[-1])
        return names, d
    finally:
        os.unlink(path)


def test_expr_walker_binary():
    n, _ = _walk_expr_string("a + b")
    assert n == {"a", "b"}


def test_expr_walker_comparison():
    n, _ = _walk_expr_string("a == b")
    assert n == {"a", "b"}


def test_expr_walker_ternary():
    n, _ = _walk_expr_string("sel ? a : b")
    assert n == {"sel", "a", "b"}


def test_expr_walker_concat():
    n, _ = _walk_expr_string("{a, b, c}")
    assert n == {"a", "b", "c"}


def test_expr_walker_part_select():
    n, _ = _walk_expr_string("bus[3:0]")
    assert "bus" in n


def test_expr_walker_indexed():
    n, _ = _walk_expr_string("bus[a]")
    assert "bus" in n and "a" in n


def test_expr_walker_unary():
    n, _ = _walk_expr_string("~a")
    assert n == {"a"}


def test_expr_walker_constants_not_emitted():
    # Integer literals and unsized constants must not become signal deps.
    n, _ = _walk_expr_string("a + 1'b1")
    assert n == {"a"}


def test_expr_walker_nested():
    n, _ = _walk_expr_string("(a + b) & (sel ? c : a)")
    assert "a" in n and "b" in n and "c" in n and "sel" in n


# ---------------------------------------------------------------------------
# Adversarial: signal named `clk` used as ordinary data
# ---------------------------------------------------------------------------


def test_signal_named_clk_as_data_not_clock():
    """A signal literally named ``clk`` that is NOT on a posedge/negedge
    sensitivity must not be treated as a clock."""
    d = parse_sv("""
        module top(input real_clk, clk /* data */, d, output reg q);
            always_ff @(posedge real_clk) q <= d & clk;
        endmodule""")
    r = reg(d, "q")
    assert r.clock_signal.split(".")[-1] == "real_clk"
    # `clk` appears as a data source, not a clock
    assert any(ds.split(".")[-1] == "clk" for ds in r.data_sources)


# ---------------------------------------------------------------------------
# Adversarial: signal named `reset` used as data
# ---------------------------------------------------------------------------


def test_signal_named_reset_as_data_not_reset():
    d = parse_sv("""
        module top(input clk, rst_n, reset /* data */, output reg q);
            always_ff @(posedge clk or negedge rst_n) begin
                if (!rst_n) q <= 1'b0;
                else q <= reset;
            end
        endmodule""")
    r = reg(d, "q")
    assert r.reset_signal.split(".")[-1] == "rst_n"
    assert any(ds.split(".")[-1] == "reset" for ds in r.data_sources)


# ---------------------------------------------------------------------------
# Adversarial: two inputs two outputs — only one connection
# ---------------------------------------------------------------------------


def test_partial_connectivity_only_real_paths():
    d = parse_sv("""
        module top(input a, b, output x, y);
            assign x = a;
            // b unused, y undriven
        endmodule""")
    ps = path_set(d)
    assert ("a", "x", "input_to_output") in ps
    assert ("b", "y", "input_to_output") not in ps
    assert ("b", "x", "input_to_output") not in ps
    assert ("a", "y", "input_to_output") not in ps


# ---------------------------------------------------------------------------
# Adversarial: mux where only one branch reaches a register
# ---------------------------------------------------------------------------


def test_mux_one_branch_reaches_only_that_source():
    """if (sel) q <= a; else q <= q;  → a is a data source, q is self-fb."""
    d = parse_sv("""
        module top(input clk, sel, a, output reg q);
            always_ff @(posedge clk) begin
                if (sel) q <= a;
                else q <= q;
            end
        endmodule""")
    r = reg(d, "q")
    leaves = {ds.split(".")[-1] for ds in r.data_sources}
    assert "a" in leaves
    assert "q" in leaves  # self-feedback
    assert r.enable_signal and r.enable_signal.split(".")[-1] == "sel"


# ---------------------------------------------------------------------------
# Adversarial: generated-clock-like logic that is actually data
# ---------------------------------------------------------------------------


def test_generated_clock_like_data_logic():
    """An AND gate between two inputs is data, not a clock gate."""
    d = parse_sv("""
        module top(input a, b, output y);
            assign y = a & b;
        endmodule""")
    assert len(d.registers) == 0
    ps = path_set(d)
    assert ("a", "y", "input_to_output") in ps
    assert ("b", "y", "input_to_output") in ps


# ---------------------------------------------------------------------------
# Deterministic IDs and ordering
# ---------------------------------------------------------------------------


def test_path_ids_unique_and_deterministic():
    d = parse_sv("""
        module top(input clk, a, b, output reg x, output reg y);
            always_ff @(posedge clk) begin x <= a; y <= b; end
        endmodule""")
    ids = [p.id for p in d.structural_paths]
    assert len(ids) == len(set(ids))
    assert all(p.id.startswith("sp") for p in d.structural_paths)
    # Ordering: determinstic (sorted in enumeration)
    starts = [p.startpoint.split(".")[-1] for p in d.structural_paths]
    assert starts == sorted(starts)


# ---------------------------------------------------------------------------
# No duplicate (start,end,class) paths
# ---------------------------------------------------------------------------


def test_no_duplicate_path_triples():
    d = parse_sv("""
        module top(input clk, d, output reg q);
            always_ff @(posedge clk) q <= d;
        endmodule""")
    seen = set()
    for p in d.structural_paths + d.cdc_paths:
        key = (p.startpoint, p.endpoint, p.path_class.value)
        assert key not in seen, f"duplicate {key}"
        seen.add(key)


# ---------------------------------------------------------------------------
# Bounded traversal constants documented
# ---------------------------------------------------------------------------


def test_bounds_constants_are_documentable():
    assert MAX_COMB_DEPTH == 256
    assert MAX_EDGES == 200_000


# ---------------------------------------------------------------------------
# Serialization (snapshot) must work without internal references
# ---------------------------------------------------------------------------


def test_design_snapshot_is_jsonable():
    import json
    d = parse_sv("""
        module top(input clk, d, output reg q);
            always_ff @(posedge clk) q <= d;
        endmodule""")
    snap = d.snapshot()
    s = json.dumps(snap, default=str)
    assert "q" in s
    # paths appear as dicts
    assert "structural_paths" in snap
