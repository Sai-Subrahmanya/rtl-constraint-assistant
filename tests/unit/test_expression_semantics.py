"""Expression-context semantic tests (Step-1 corrective pass, requirement 7).

These tests verify that dependency classification depends on semantic
CONTEXT (RHS value vs procedural predicate vs ternary select), not on
the operator alone or on signal names.

Each test asserts:
  * Register ``data_sources`` (the D-cone — signals that compute the
    loaded value, including operands of comparisons/logical ops on RHS).
  * Register ``control_sources`` (if/case predicates and ternary selects).
  * Register ``clock_signal`` / ``reset_signal`` / ``enable_signal``.
  * Structural edge kinds (DATA/CONDITIONAL/MUX_SELECT) so the graph
    preserves the distinction.
  * Timing paths: CONTROL edges do NOT create ordinary data timing
    paths (requirement 4); MUX_SELECT edges DO participate.
"""
from __future__ import annotations

import textwrap
from typing import Any

import pytest

from rca.design_model.connectivity import build_structural_connectivity
from rca.parser import SlangAdapter
from rca.utils.enums import DependencyKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_sv(sv: str, top: str = "top") -> Any:
    import tempfile, os
    sv = textwrap.dedent(sv)
    with tempfile.NamedTemporaryFile("w", suffix=".sv", delete=False) as f:
        f.write(sv); path = f.name
    try:
        a = SlangAdapter()
        d = a.parse([path], top=top)
        d.build_connectivity()
        return d
    finally:
        os.unlink(path)


def reg(d, leaf: str):
    for r in d.registers.values():
        if r.local_name == leaf:
            return r
    raise AssertionError(f"register '{leaf}' not found; "
                         f"have: {[r.local_name for r in d.registers.values()]}")


def leaves(lst: list[str]) -> set[str]:
    return {s.split(".")[-1] for s in lst}


def edge_map(d) -> dict[tuple[str, str], set[str]]:
    """Return {(src_leaf, dst_leaf): {kind_values}} for all comb_edges."""
    out: dict[tuple[str, str], set[str]] = {}
    for e in d.comb_edges:
        k = (e.src.split(".")[-1], e.dst.split(".")[-1])
        out.setdefault(k, set()).add(e.kind.value)
    return out


def path_set(d) -> set[tuple[str, str, str]]:
    out = set()
    for p in d.structural_paths + d.cdc_paths:
        out.add((p.startpoint.split(".")[-1],
                 p.endpoint.split(".")[-1],
                 p.path_class.value))
    return out


# ---------------------------------------------------------------------------
# 1. RHS comparison: q <= (a == b) → a,b DATA
# ---------------------------------------------------------------------------


def test_rhs_comparison_is_data():
    d = parse_sv("""
        module top(input clk, a, b, output reg q);
            always_ff @(posedge clk) q <= (a == b);
        endmodule""")
    r = reg(d, "q")
    assert leaves(r.data_sources) == {"a", "b"}, r.data_sources
    assert leaves(r.control_sources) == set()
    assert r.enable_signal is None
    em = edge_map(d)
    assert em[("a", "q")] == {"nonblocking_assign"}
    assert em[("b", "q")] == {"nonblocking_assign"}
    # Both a->q and b->q are real data timing paths (1-bit comparator).
    ps = path_set(d)
    assert ("a", "q", "input_to_register") in ps
    assert ("b", "q", "input_to_register") in ps


# ---------------------------------------------------------------------------
# 2. RHS logical: q <= (a && b) → a,b DATA
# ---------------------------------------------------------------------------


def test_rhs_logical_is_data():
    d = parse_sv("""
        module top(input clk, a, b, output reg q);
            always_ff @(posedge clk) q <= (a && b);
        endmodule""")
    r = reg(d, "q")
    assert leaves(r.data_sources) == {"a", "b"}
    assert leaves(r.control_sources) == set()
    ps = path_set(d)
    assert ("a", "q", "input_to_register") in ps
    assert ("b", "q", "input_to_register") in ps


# ---------------------------------------------------------------------------
# 3. RHS arithmetic: q <= a + b → a,b DATA
# ---------------------------------------------------------------------------


def test_rhs_arithmetic_is_data():
    d = parse_sv("""
        module top(input clk, input [7:0] a, b, output reg [7:0] q);
            always_ff @(posedge clk) q <= a + b;
        endmodule""")
    r = reg(d, "q")
    assert leaves(r.data_sources) == {"a", "b"}
    assert leaves(r.control_sources) == set()
    ps = path_set(d)
    assert ("a", "q", "input_to_register") in ps
    assert ("b", "q", "input_to_register") in ps


# ---------------------------------------------------------------------------
# 4. Procedural comparison: if (a == b) q <= d; → a,b CONTROL, d DATA
# ---------------------------------------------------------------------------


def test_procedural_comparison_is_control():
    d = parse_sv("""
        module top(input clk, a, b, d, output reg q);
            always_ff @(posedge clk) if (a == b) q <= d;
        endmodule""")
    r = reg(d, "q")
    assert leaves(r.data_sources) == {"d"}
    assert leaves(r.control_sources) == {"a", "b"}
    assert r.enable_signal is not None  # first conditional is enable
    em = edge_map(d)
    assert em[("d", "q")] == {"nonblocking_assign"}
    assert em[("a", "q")] == {"conditional"}
    assert em[("b", "q")] == {"conditional"}
    # d->q is a timing path; a->q and b->q are NOT (control edges).
    ps = path_set(d)
    assert ("d", "q", "input_to_register") in ps
    assert ("a", "q", "input_to_register") not in ps
    assert ("b", "q", "input_to_register") not in ps


# ---------------------------------------------------------------------------
# 5. Procedural enable: if (en) q <= d; → en CONTROL, d DATA
# ---------------------------------------------------------------------------


def test_procedural_enable_is_control():
    d = parse_sv("""
        module top(input clk, en, d, output reg q);
            always_ff @(posedge clk) if (en) q <= d;
        endmodule""")
    r = reg(d, "q")
    assert leaves(r.data_sources) == {"d"}
    assert leaves(r.control_sources) == {"en"}
    assert r.enable_signal.split(".")[-1] == "en"
    em = edge_map(d)
    assert em[("en", "q")] == {"conditional"}
    ps = path_set(d)
    assert ("d", "q", "input_to_register") in ps
    assert ("en", "q", "input_to_register") not in ps


# ---------------------------------------------------------------------------
# 6. Ternary: q <= sel ? a : b → sel MUX_SELECT, a,b DATA
# ---------------------------------------------------------------------------


def test_ternary_select_is_mux_control():
    d = parse_sv("""
        module top(input clk, sel, a, b, output reg q);
            always_ff @(posedge clk) q <= sel ? a : b;
        endmodule""")
    r = reg(d, "q")
    assert leaves(r.data_sources) == {"a", "b"}
    assert leaves(r.control_sources) == {"sel"}
    em = edge_map(d)
    assert em[("a", "q")] == {"nonblocking_assign"}
    assert em[("b", "q")] == {"nonblocking_assign"}
    assert em[("sel", "q")] == {"mux_select"}
    ps = path_set(d)
    # MUX_SELECT arcs are data-path arcs (S pin of a mux is timing-relevant)
    assert ("a", "q", "input_to_register") in ps
    assert ("b", "q", "input_to_register") in ps
    assert ("sel", "q", "input_to_register") in ps


# ---------------------------------------------------------------------------
# 7. Nested ternary: q <= sel1 ? a : (sel2 ? b : c)
# ---------------------------------------------------------------------------


def test_nested_ternary():
    d = parse_sv("""
        module top(input clk, sel1, sel2, a, b, c, output reg q);
            always_ff @(posedge clk) q <= sel1 ? a : (sel2 ? b : c);
        endmodule""")
    r = reg(d, "q")
    assert leaves(r.data_sources) == {"a", "b", "c"}
    assert leaves(r.control_sources) == {"sel1", "sel2"}


# ---------------------------------------------------------------------------
# 8. Comparison used as data (computing select value): q <= (a < b) ? c : d
#    Here (a<b) is the ternary selector -> a,b MUX_SELECT, c,d DATA
# ---------------------------------------------------------------------------


def test_comparison_as_ternary_select():
    d = parse_sv("""
        module top(input clk, a, b, c, d, output reg q);
            always_ff @(posedge clk) q <= (a < b) ? c : d;
        endmodule""")
    r = reg(d, "q")
    assert leaves(r.data_sources) == {"c", "d"}
    assert leaves(r.control_sources) == {"a", "b"}
    em = edge_map(d)
    assert em[("a", "q")] == {"mux_select"}
    assert em[("b", "q")] == {"mux_select"}
    assert em[("c", "q")] == {"nonblocking_assign"}
    assert em[("d", "q")] == {"nonblocking_assign"}


# ---------------------------------------------------------------------------
# 9. Comparison used as predicate: if (a < b) q <= c → a,b CONTROL, c DATA
# ---------------------------------------------------------------------------


def test_comparison_as_procedural_predicate():
    d = parse_sv("""
        module top(input clk, a, b, c, output reg q);
            always_ff @(posedge clk) if (a < b) q <= c;
        endmodule""")
    r = reg(d, "q")
    assert leaves(r.data_sources) == {"c"}
    assert leaves(r.control_sources) == {"a", "b"}
    em = edge_map(d)
    assert em[("c", "q")] == {"nonblocking_assign"}
    assert em[("a", "q")] == {"conditional"}
    assert em[("b", "q")] == {"conditional"}


# ---------------------------------------------------------------------------
# 10. Signal named 'sel' used as ordinary data: q <= sel + a
# ---------------------------------------------------------------------------


def test_signal_named_sel_as_data():
    """A signal literally named 'sel' that appears in an arithmetic RHS
    must be treated as DATA, not as a mux-select control.  Exact-name
    adversarial test."""
    d = parse_sv("""
        module top(input clk, input [7:0] sel, a, output reg [7:0] q);
            always_ff @(posedge clk) q <= sel + a;
        endmodule""")
    r = reg(d, "q")
    assert r.clock_signal.split(".")[-1] == "clk"
    assert leaves(r.data_sources) == {"a", "sel"}
    assert leaves(r.control_sources) == set()
    assert r.enable_signal is None
    g = d._structural_graph_internal
    assert not any(cs.split(".")[-1] == "sel" for cs in g.control_signals)
    ps = path_set(d)
    assert ("sel", "q", "input_to_register") in ps
    assert ("a", "q", "input_to_register") in ps


# ---------------------------------------------------------------------------
# 11. Signal named 'enable' used as ordinary data: q <= enable ^ a
# ---------------------------------------------------------------------------


def test_signal_named_enable_as_data():
    """A signal literally named 'enable' used on the RHS of an xor is
    ordinary DATA — naming does not create an enable pin."""
    d = parse_sv("""
        module top(input clk, enable, a, output reg q);
            always_ff @(posedge clk) q <= enable ^ a;
        endmodule""")
    r = reg(d, "q")
    assert r.clock_signal.split(".")[-1] == "clk"
    assert leaves(r.data_sources) == {"a", "enable"}
    assert leaves(r.control_sources) == set()
    assert r.enable_signal is None
    g = d._structural_graph_internal
    assert not any(cs.split(".")[-1] == "enable" for cs in g.control_signals)
    ps = path_set(d)
    assert ("enable", "q", "input_to_register") in ps
    assert ("a", "q", "input_to_register") in ps


# ---------------------------------------------------------------------------
# 12. Signal named 'reset' used as ordinary data (real reset is rst_n)
# ---------------------------------------------------------------------------


def test_signal_named_reset_as_data():
    """A signal literally named 'reset' that is ANDed into data is DATA —
    only rst_n (on the negedge sensitivity with a negated if-predicate)
    is the structural async reset."""
    d = parse_sv("""
        module top(input clk, rst_n, reset, a, output reg q);
            always_ff @(posedge clk or negedge rst_n) begin
                if (!rst_n) q <= 1'b0;
                else q <= reset & a;
            end
        endmodule""")
    r = reg(d, "q")
    assert r.clock_signal.split(".")[-1] == "clk"
    assert r.reset_signal.split(".")[-1] == "rst_n"
    assert leaves(r.data_sources) == {"a", "reset"}
    assert leaves(r.control_sources) == set()
    # 'reset' is NOT treated as a clock/reset control signal globally.
    g = d._structural_graph_internal
    assert not any(cs.split(".")[-1] == "reset" for cs in g.control_signals)
    assert any(cs.split(".")[-1] == "rst_n" for cs in g.control_signals)
    ps = path_set(d)
    assert ("reset", "q", "input_to_register") in ps
    assert ("a", "q", "input_to_register") in ps


# ---------------------------------------------------------------------------
# 13. Signal named 'clk' used as data (real clock is different)
#     EXACT-NAME adversarial test — uses a signal literally named 'clk'.
# ---------------------------------------------------------------------------


def test_signal_named_clk_as_data():
    d = parse_sv("""
        module top(input real_clk, input [7:0] clk, a,
                   output reg [7:0] q);
            always_ff @(posedge real_clk) q <= clk + a;
        endmodule""")
    r = reg(d, "q")
    # real_clk is the clock, not 'clk'
    assert r.clock_signal.split(".")[-1] == "real_clk"
    assert r.reset_signal is None
    # 'clk' is an ordinary data source — naming cannot override structure.
    assert leaves(r.data_sources) == {"a", "clk"}
    assert leaves(r.control_sources) == set()
    assert r.enable_signal is None
    # 'clk' is NOT promoted to the global clock/control exclusion set.
    g = d._structural_graph_internal
    clk_global = [c for c in g.control_signals if c.split(".")[-1] == "clk"]
    assert not clk_global, f"'clk' should not be in control_signals, got {clk_global}"
    # Real clock 'real_clk' IS in the control set.
    assert any(c.split(".")[-1] == "real_clk" for c in g.control_signals)
    # Structural input-to-register paths exist from BOTH clk and a to q.
    ps = path_set(d)
    assert ("clk", "q", "input_to_register") in ps, ps
    assert ("a", "q", "input_to_register") in ps, ps


# ---------------------------------------------------------------------------
# 14. if/else mux: data_sources = {a,b}; control = {sel}
# ---------------------------------------------------------------------------


def test_ifelse_data_and_control():
    d = parse_sv("""
        module top(input clk, sel, a, b, output reg q);
            always_ff @(posedge clk)
                if (sel) q <= a; else q <= b;
        endmodule""")
    r = reg(d, "q")
    assert leaves(r.data_sources) == {"a", "b"}
    assert leaves(r.control_sources) == {"sel"}


# ---------------------------------------------------------------------------
# 15. Clock/reset structural detection (not positional):
#     @(posedge clk or negedge rst_n) with if (!rst_n) reset branch
# ---------------------------------------------------------------------------


def test_clock_reset_structural_detection():
    d = parse_sv("""
        module top(input clk, rst_n, d, output reg q);
            always_ff @(posedge clk or negedge rst_n) begin
                if (!rst_n) q <= 1'b0;
                else q <= d;
            end
        endmodule""")
    r = reg(d, "q")
    assert r.clock_signal.split(".")[-1] == "clk"
    assert r.reset_signal.split(".")[-1] == "rst_n"
    assert r.reset_type.value == "asynchronous"
    assert r.reset_polarity.value == "active_low"
    assert leaves(r.data_sources) == {"d"}
    assert leaves(r.control_sources) == set()


# ---------------------------------------------------------------------------
# 16. Single-clock always_ff (no reset) still correctly classifies clock
# ---------------------------------------------------------------------------


def test_single_clock_no_reset():
    d = parse_sv("""
        module top(input clk, d, output reg q);
            always_ff @(posedge clk) q <= d;
        endmodule""")
    r = reg(d, "q")
    assert r.clock_signal.split(".")[-1] == "clk"
    assert r.reset_signal is None
    assert leaves(r.data_sources) == {"d"}


# ---------------------------------------------------------------------------
# 17. Continuous assign with ternary: mux select preserved
# ---------------------------------------------------------------------------


def test_cont_assign_ternary_preserves_mux_select():
    d = parse_sv("""
        module top(input sel, a, b, output y);
            assign y = sel ? a : b;
        endmodule""")
    em = edge_map(d)
    # sel is a mux_select edge, a and b are continuous_assign data edges.
    assert em[("sel", "y")] == {"mux_select"}
    assert em[("a", "y")] == {"continuous_assign"}
    assert em[("b", "y")] == {"continuous_assign"}
    ps = path_set(d)
    # In combinational logic the mux select is a real data path.
    assert ("sel", "y", "input_to_output") in ps
    assert ("a", "y", "input_to_output") in ps
    assert ("b", "y", "input_to_output") in ps


# ---------------------------------------------------------------------------
# 18. Determinism: running parse twice yields identical data/control sets
# ---------------------------------------------------------------------------


def test_data_control_deterministic_across_runs():
    sv = """
        module top(input clk, sel, a, b, output reg q);
            always_ff @(posedge clk)
                if (sel) q <= a; else q <= b;
        endmodule"""
    runs = []
    for _ in range(3):
        d = parse_sv(sv)
        r = reg(d, "q")
        runs.append((list(r.data_sources), list(r.control_sources)))
    assert runs[0] == runs[1] == runs[2]


# ---------------------------------------------------------------------------
# 19. Hierarchy: child comparison-as-data is classified correctly
#     (top input → child input → child RHS comparison → child register)
# ---------------------------------------------------------------------------


def test_hierarchy_rhs_comparison_preserves_data_semantics():
    """The child module uses q <= (a == b), so from the child's perspective
    a and b are DATA (not CONTROL).  Cross-hierarchy projection then
    creates a timing path from parent in_a/in_b through the child
    ports to the child register — confirming DATA semantics survive
    hierarchy crossing (the comparison is not demoted to CONTROL
    anywhere)."""
    sv = """
        module child(input clk, a, b, output reg q);
            always_ff @(posedge clk) q <= (a == b);
        endmodule
        module top(input clk, in_a, in_b, output out_q);
            child u (.clk(clk), .a(in_a), .b(in_b), .q(out_q));
        endmodule"""
    d = parse_sv(sv, top="top")
    # Find the child register
    child_q = None
    for r in d.registers.values():
        if r.local_name == "q" and "u." in r.hierarchical_name:
            child_q = r; break
    assert child_q is not None, [r.hierarchical_name for r in d.registers.values()]
    # Within the child scope the data sources are its formal ports a,b.
    assert leaves(child_q.data_sources) == {"a", "b"}
    assert leaves(child_q.control_sources) == set()
    assert child_q.enable_signal is None
    # The hierarchical projection must expose timing paths from the
    # parent inputs through the child ports to the child register Q.
    ps = path_set(d)
    assert ("in_a", "q", "input_to_register") in ps
    assert ("in_b", "q", "input_to_register") in ps


# ---------------------------------------------------------------------------
# 20. RHS bitwise ops (not just logical / comparison) remain DATA
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op", ["+", "-", "*", "&", "|", "^", "<<", ">>"])
def test_rhs_binary_ops_all_data(op):
    d = parse_sv(f"""
        module top(input clk, input [7:0] a, b, output reg [7:0] q);
            always_ff @(posedge clk) q <= a {op} b;
        endmodule""")
    r = reg(d, "q")
    assert leaves(r.data_sources) == {"a", "b"}
    assert leaves(r.control_sources) == set()
    assert r.enable_signal is None


# ---------------------------------------------------------------------------
# 21b. Ternary in predicate position: if (sel ? a : b) q <= d
#      sel/a/b are all CONTROL (they decide whether the assignment fires).
# ---------------------------------------------------------------------------


def test_ternary_in_predicate_all_branches_are_control():
    d = parse_sv("""
        module top(input clk, sel, a, b, d, output reg q);
            always_ff @(posedge clk) if (sel ? a : b) q <= d;
        endmodule""")
    r = reg(d, "q")
    assert leaves(r.data_sources) == {"d"}
    # sel is MUX_SELECT (intrinsic), a/b are CONDITIONAL (predicate branches).
    # All three end up in control_sources.
    assert leaves(r.control_sources) == {"sel", "a", "b"}
    assert leaves(r.data_sources).isdisjoint({"sel", "a", "b"})


# ---------------------------------------------------------------------------
# 22. Synchronous reset (rst checked on posedge clk without rst in sensitivity)
#     is a CONTROL condition, not a structural reset signal.
# ---------------------------------------------------------------------------


def test_sync_reset_is_control_not_reset_signal():
    d = parse_sv("""
        module top(input clk, rst, d, output reg q);
            always_ff @(posedge clk) begin
                if (rst) q <= 1'b0;
                else q <= d;
            end
        endmodule""")
    r = reg(d, "q")
    assert r.clock_signal.split(".")[-1] == "clk"
    assert r.reset_signal is None  # no async reset detected
    assert r.reset_type.value == "unknown"
    assert leaves(r.data_sources) == {"d"}
    assert leaves(r.control_sources) == {"rst"}
    # rst is NOT added to global control_signals (not a clock/reset).
    g = d._structural_graph_internal
    assert not any(cs.split(".")[-1] == "rst" for cs in g.control_signals)


# ---------------------------------------------------------------------------
# 23. RHS unary ops: ~a, !a, -a all keep `a` as DATA
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op", ["~", "!", "-"])
def test_rhs_unary_ops_data(op):
    d = parse_sv(f"""
        module top(input clk, a, output reg q);
            always_ff @(posedge clk) q <= {op}a;
        endmodule""")
    r = reg(d, "q")
    assert leaves(r.data_sources) == {"a"}
    assert leaves(r.control_sources) == set()
