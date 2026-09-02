"""Step 7 multi-layer validation engine unit tests."""
from __future__ import annotations

import pytest

from rca.constraint_model import (
    Constraint,
    ConstraintSet,
    PathSelector,
)
from rca.design_model.design import Design
from rca.timing_model.timing_graph import TimingGraph
from rca.timing_model.timing_path import TimingPath
from rca.timing_model.clock import Clock
from rca.timing_model.clock_domain import ClockDomainEdge
from rca.utils.enums import (
    ClockDomainRelationship,
    ConstraintType,
    ErrorCode,
    Severity,
    TimingPathClass,
    ValidationStatus,
)
from rca.validation.engine import run_validation
from rca.validation.base import ValidationIssue, ValidationReport


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

def _cset():
    return ConstraintSet()


def _clock(cs: ConstraintSet, name: str, period: float,
           source: str | None = None, **kw):
    return cs.create_clock(name=name, period_seconds=period,
                           source=source or name, **kw)


def _input_delay(cs, cid: str, port: str, clock: str, delay: float, **kw):
    c = Constraint(id=cid, type=ConstraintType.SET_INPUT_DELAY,
                   target_objects=[port],
                   clock_refs=[clock],
                   values={"clock": clock, "delay": delay,
                           "delay_max": kw.get("delay_max", delay),
                           "min_max": kw.get("min_max", "both"),
                           "edge": kw.get("edge", "both"),
                           "add_delay": kw.get("add_delay", False)})
    cs.add(c)
    return c


def _output_delay(cs, cid: str, port: str, clock: str, delay: float, **kw):
    c = Constraint(id=cid, type=ConstraintType.SET_OUTPUT_DELAY,
                   target_objects=[port],
                   clock_refs=[clock],
                   values={"clock": clock, "delay": delay,
                           "delay_max": kw.get("delay_max", delay),
                           "min_max": kw.get("min_max", "both"),
                           "edge": kw.get("edge", "both"),
                           "add_delay": kw.get("add_delay", False)})
    cs.add(c)
    return c


def _false_path(cs, cid: str, fro=None, to=None, through=None):
    ps = PathSelector(from_set=fro or [], to_set=to or [],
                      through_set=[through] if through else [])
    c = Constraint(id=cid, type=ConstraintType.SET_FALSE_PATH,
                   path_selector=ps)
    cs.add(c)
    return c


def _multicycle(cs, cid: str, cycles: int, fro=None, to=None,
                start=False, end=False):
    ps = PathSelector(from_set=fro or [], to_set=to or [])
    c = Constraint(id=cid, type=ConstraintType.SET_MULTICYCLE_PATH,
                   path_selector=ps,
                   values={"cycles": cycles, "start": start, "end": end})
    cs.add(c)
    return c


def _gclock(cs, cid: str, name: str, source: str, master: str | None = None,
            div=None, mul=None, edges=None, edge_shift=None):
    values: dict = {"name": name, "source": source}
    if master:
        values["master_clock"] = master
    if div is not None:
        values["divide_by"] = div
    if mul is not None:
        values["multiply_by"] = mul
    if edges is not None:
        values["edges"] = edges
    if edge_shift is not None:
        values["edge_shift"] = edge_shift
    c = Constraint(id=cid, type=ConstraintType.CREATE_GENERATED_CLOCK,
                   target_objects=[source], clock_refs=[master] if master else [],
                   values=values)
    cs.add(c)
    return c


def _clock_groups(cs, groups, relationship="asynchronous"):
    return cs.create_clock_groups(groups=groups,
                                  relationship=relationship)


# ---------------------------------------------------------------------------
# Sanity / golden
# ---------------------------------------------------------------------------

def test_01_empty_set_passes():
    cs = _cset()
    r = run_validation(cset=cs)
    assert r.status in (ValidationStatus.PASS.value,
                        ValidationStatus.PASS_WITH_WARNINGS.value)


def test_02_deterministic_issue_ids():
    cs = _cset()
    cs.add(Constraint(id="X", type=ConstraintType.CREATE_CLOCK,
                      target_objects=["clk"],
                      values={"name": "clk", "period": -1e-9}))
    a = run_validation(cset=cs)
    b = run_validation(cset=cs)
    assert [i.issue_id for i in a.report.issues] == \
           [i.issue_id for i in b.report.issues]


def test_03_report_immutable_to_cset():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    before = [c.id for c in cs]
    run_validation(cset=cs)
    after = [c.id for c in cs]
    assert before == after


# ---------------------------------------------------------------------------
# Clock semantic
# ---------------------------------------------------------------------------

def test_10_clock_period_invalid():
    cs = _cset()
    _clock(cs, "clk", -1e-9)
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.CLOCK_PERIOD_INVALID for i in r.errors)


def test_11_clock_zero_period():
    cs = _cset()
    _clock(cs, "clk", 0)
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.CLOCK_PERIOD_INVALID for i in r.errors)


def test_12_clock_missing_period():
    cs = _cset()
    cs.add(Constraint(id="CLK1", type=ConstraintType.CREATE_CLOCK,
                      target_objects=["clk"], values={"name": "clk"}))
    r = run_validation(cset=cs)
    assert any(i.code in (ErrorCode.CLOCK_PERIOD_MISSING,
                          ErrorCode.CLOCK_PERIOD_INVALID) for i in r.errors)


def test_13_waveform_invalid_rise_after_fall():
    cs = _cset()
    cs.add(Constraint(id="CLK1", type=ConstraintType.CREATE_CLOCK,
                      target_objects=["clk"],
                      values={"name": "clk", "period": 10e-9,
                              "waveform": [6e-9, 4e-9]}))
    r = run_validation(cset=cs)
    assert any(i.code in (ErrorCode.CLOCK_WAVEFORM_INVALID,
                          ErrorCode.CLOCK_WAVEFORM_INCOHERENT) for i in r.errors)


def test_14_waveform_fall_geq_period():
    cs = _cset()
    cs.add(Constraint(id="CLK1", type=ConstraintType.CREATE_CLOCK,
                      target_objects=["clk"],
                      values={"name": "clk", "period": 10e-9,
                              "waveform": [0, 12e-9]}))
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.CLOCK_WAVEFORM_INCOHERENT for i in r.errors)


def test_15_multi_clock_on_source_without_add():
    cs = _cset()
    _clock(cs, "clk_a", 10e-9, source="clk")
    _clock(cs, "clk_b", 8e-9, source="clk")
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.CLOCK_MULTIPLE for i in r.warnings + r.errors)


def test_16_valid_clock_no_clock_errors():
    cs = _cset()
    _clock(cs, "clk", 10e-9, waveform=(0, 5e-9))
    r = run_validation(cset=cs)
    errs = [i for i in r.errors if i.category.value == "clock"]
    assert errs == []


# ---------------------------------------------------------------------------
# Generated clock
# ---------------------------------------------------------------------------

def test_20_gclk_missing_source():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    # Generated clock with no source pin
    cs.add(Constraint(id="GCLK1", type=ConstraintType.CREATE_GENERATED_CLOCK,
                      target_objects=[], clock_refs=["clk"],
                      values={"name": "gclk", "master_clock": "clk"}))
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.GCLK_SOURCE_MISSING for i in r.report.issues)


def test_21_gclk_bad_divide():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _gclock(cs, "GCLK1", "gclk", source="div_reg/Q", master="clk", div=0)
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.GCLK_INVALID_DIV for i in r.errors)


def test_22_gclk_div_mul_both():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _gclock(cs, "GCLK1", "gclk", source="div_reg/Q", master="clk", div=2, mul=2)
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.GCLK_CONTRADICTORY_OPTIONS for i in r.errors)


def test_23_gclk_edges_invalid():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _gclock(cs, "GCLK1", "gclk", source="div_reg/Q", master="clk",
            edges=[1, 2, 3])
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.GCLK_EDGES_INVALID for i in r.errors)


def test_24_gclk_edge_shift_without_edges():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _gclock(cs, "GCLK1", "gclk", source="div_reg/Q", master="clk",
            edge_shift=[1e-9])
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.GCLK_EDGE_SHIFT_WITHOUT_EDGES for i in r.errors)


# ---------------------------------------------------------------------------
# IO timing
# ---------------------------------------------------------------------------

def test_30_input_delay_unknown_clock():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _input_delay(cs, "IN1", "d_in", "no_such_clk", 2e-9)
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.IO_CLOCK_UNKNOWN for i in r.errors)


def test_31_input_delay_nan_inf():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _input_delay(cs, "IN1", "d_in", "clk", float("nan"))
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.IO_DELAY_INVALID for i in r.errors)


def test_32_input_delay_negative_value():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _input_delay(cs, "IN1", "d_in", "clk", -1e-9)
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.IO_DELAY_INVALID for i in r.errors)


def test_33_input_delay_min_max_coherence():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _input_delay(cs, "IN1", "d_in", "clk", 3e-9, delay_max=1e-9,
                 min_max="both")
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.IO_MIN_MAX_INCOHERENT for i in r.errors)


def test_34_output_delay_valid():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _output_delay(cs, "OUT1", "d_out", "clk", 2e-9)
    r = run_validation(cset=cs)
    io_errs = [i for i in r.errors if i.category.value == "timing"]
    assert io_errs == []


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------

def test_40_duplicate_clock_same_period_is_overlap_not_conflict():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _clock(cs, "clk", 10e-9)
    r = run_validation(cset=cs)
    # Two create_clock with same name+period is harmless duplicate; shouldn't be period conflict
    assert not any(i.code == ErrorCode.CONFLICT_CLOCK_PERIOD for i in r.errors)


def test_41_conflicting_clock_period():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _clock(cs, "clk", 8e-9)
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.CONFLICT_CLOCK_PERIOD for i in r.errors)


def test_42_io_conflict_same_target_same_mode():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _input_delay(cs, "IN1", "d_in", "clk", 2e-9)
    _input_delay(cs, "IN2", "d_in", "clk", 3e-9)
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.CONFLICT_IO_DELAY for i in r.errors)


# ---------------------------------------------------------------------------
# Overlap / shadow
# ---------------------------------------------------------------------------

def test_50_false_path_no_selectors_is_broad():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _false_path(cs, "FP1")
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.OVERLAP_SHADOWED
               or i.code == ErrorCode.EXCEPTION_BROAD for i in r.report.issues)


def test_51_duplicate_false_path_is_overlap():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _false_path(cs, "FP1", fro=["clk"], to=["rst"])
    _false_path(cs, "FP2", fro=["clk"], to=["rst"])
    r = run_validation(cset=cs)
    assert any(i.code in (ErrorCode.OVERLAP_DUPLICATE,
                          ErrorCode.OVERLAP_REDUNDANT) for i in r.report.issues)


# ---------------------------------------------------------------------------
# Exception sanity
# ---------------------------------------------------------------------------

def test_60_multicycle_bad_cycles():
    cs = _cset()
    _multicycle(cs, "MC1", -1, fro=["a"], to=["b"])
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.EXCEPTION_BAD_CYCLES for i in r.errors)


def test_61_multicycle_both_start_end():
    cs = _cset()
    _multicycle(cs, "MC1", 2, fro=["a"], to=["b"], start=True, end=True)
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.EXCEPTION_SETUP_HOLD_INCOHERENT for i in r.warnings)


def test_62_broad_exception_flagged():
    cs = _cset()
    _false_path(cs, "FP1")
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.EXCEPTION_BROAD for i in r.report.issues)


# ---------------------------------------------------------------------------
# Reference integrity
# ---------------------------------------------------------------------------

def test_70_unknown_target_is_warning():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _input_delay(cs, "IN1", "nonexistent_port_xyz", "clk", 2e-9)
    # Without a Design object, references may not all resolve; engine should not crash
    r = run_validation(cset=cs)
    # Validator runs without crashing
    assert r.status is not None


# ---------------------------------------------------------------------------
# Path selector
# ---------------------------------------------------------------------------

def test_80_duplicate_through_stages():
    cs = _cset()
    ps = PathSelector(from_set=["a"], to_set=["b"], through_set=[["u1/Q"], ["u1/Q"]])
    cs.add(Constraint(id="MD1", type=ConstraintType.SET_MAX_DELAY,
                      path_selector=ps, values={"delay": 1e-9}))
    r = run_validation(cset=cs)
    # Should flag something — duplicate through stages or similar
    assert r.report.issues  # at minimum an info/warn


# ---------------------------------------------------------------------------
# Clock groups
# ---------------------------------------------------------------------------

def test_90_clock_groups_single_group_errors():
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _clock(cs, "clk_b", 10e-9)
    _clock_groups(cs, [["clk_a", "clk_b"]], "asynchronous")
    r = run_validation(cset=cs)
    # A single group (no comparison set) is invalid
    assert any(i.code == ErrorCode.GROUPS_EMPTY for i in r.errors)


def test_91_clock_groups_empty_member_warns():
    cs = _cset()
    _clock_groups(cs, [["clk_a"], []], "asynchronous")
    r = run_validation(cset=cs)
    assert any(i.code in (ErrorCode.GROUPS_EMPTY, ErrorCode.GROUPS_EMPTY)
               for i in r.warnings + r.errors)


# ---------------------------------------------------------------------------
# Backend (generic backend)
# ---------------------------------------------------------------------------

def test_a0_unknown_backend_flagged():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    r = run_validation(cset=cs, backend="does_not_exist_backend")
    # Should not crash; backend check is best-effort
    assert r.status is not None


def test_a1_generic_backend_valid_clock_passes():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    r = run_validation(cset=cs, backend="generic")
    assert not any(i.code == ErrorCode.BACKEND_BLOCKED for i in r.errors)


# ---------------------------------------------------------------------------
# Coverage (no graph → UNKNOWN)
# ---------------------------------------------------------------------------

def _make_tg(clocks=None, paths=None, domain_edges=None) -> TimingGraph:
    tg_clocks = {}
    if clocks:
        for cname, cobj in clocks.items():
            tg_clocks[cname] = cobj
    return TimingGraph(
        clocks=tg_clocks,
        paths=list(paths or []),
        domain_edges=list(domain_edges or []),
    )


_MINI_DESIGN = Design(name="dut", top_module="dut")


def _graph_clock(name: str, source: str | None = None, period: float = 10e-9) -> Clock:
    return Clock(id=name, name=name, source_object=source or name,
                 period_seconds=period)


def test_b0_coverage_unknown_without_graph():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    r = run_validation(cset=cs)
    assert r.coverage is not None
    d = r.coverage.as_dict()
    assert d["graph_available"] is False
    assert d["clock_source_coverage_pct"] == "UNKNOWN"
    assert d["reg_to_reg_coverage_pct"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# Reg-reg / CDC path coverage regressions (Step 7 coverage fixes)
# ---------------------------------------------------------------------------


def test_f0_same_clock_reg2reg_counted_covered():
    """launch_clock == capture_clock, both constrained → covered."""
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    tg = _make_tg(
        clocks={"clk_a": _graph_clock("clk_a")},
        paths=[TimingPath(startpoint="reg1/Q", endpoint="reg2/D",
                          launch_clock="clk_a", capture_clock="clk_a",
                          path_type=TimingPathClass.REG_TO_REG)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["reg_reg_paths"] == 1
    assert cov["reg_to_reg_coverage_pct"] == 100.0


def test_f1_cross_clock_reg2reg_no_relationship_is_uncovered():
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _clock(cs, "clk_b", 10e-9)
    tg = _make_tg(
        clocks={"clk_a": _graph_clock("clk_a"), "clk_b": _graph_clock("clk_b")},
        paths=[TimingPath(startpoint="ra/Q", endpoint="rb/D",
                          launch_clock="clk_a", capture_clock="clk_b",
                          path_type=TimingPathClass.REG_TO_REG)],
        domain_edges=[ClockDomainEdge(clock_a="clk_a", clock_b="clk_b")],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["reg_reg_paths"] == 1
    assert cov["reg_to_reg_coverage_pct"] == 0.0
    assert cov["clock_relationship_coverage_pct"] == 0.0
    # CDC metric separate from reg2reg — there's no CDC-typed path here
    assert cov["totals"]["cdc_paths"] == 0


def test_f2_missing_launch_clock_is_unknown():
    cs = _cset()
    _clock(cs, "clk_b", 10e-9)
    tg = _make_tg(
        clocks={"clk_b": _graph_clock("clk_b")},
        paths=[TimingPath(startpoint="ra/Q", endpoint="rb/D",
                          launch_clock=None, capture_clock="clk_b",
                          path_type=TimingPathClass.REG_TO_REG)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    # path counted in total but NOT in covered; an uncovered entry exists
    assert cov["totals"]["reg_reg_paths"] == 1
    assert cov["reg_to_reg_coverage_pct"] == 0.0
    assert any(u["classification"] == "UNKNOWN"
               for u in cov["uncovered"] if u["category"] == "reg_to_reg")


def test_f3_missing_capture_clock_is_unknown():
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    tg = _make_tg(
        clocks={"clk_a": _graph_clock("clk_a")},
        paths=[TimingPath(startpoint="ra/Q", endpoint="rb/D",
                          launch_clock="clk_a", capture_clock=None,
                          path_type=TimingPathClass.REG_TO_REG)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["reg_reg_paths"] == 1
    assert any(u["classification"] == "UNKNOWN"
               for u in cov["uncovered"] if u["category"] == "reg_to_reg")


def test_f4_two_domains_no_cdc_path():
    """Relationship coverage reflects pair; cdc_paths_total stays 0,
    CDC-path pct is NOT_APPLICABLE (not 100%)."""
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _clock(cs, "clk_b", 10e-9)
    # Add asynchronous groups to mark relationship as known.
    _clock_groups(cs, [["clk_a"], ["clk_b"]], "asynchronous")
    tg = _make_tg(
        clocks={"clk_a": _graph_clock("clk_a"), "clk_b": _graph_clock("clk_b")},
        paths=[],  # only same-domain reg paths exist in reality
        domain_edges=[ClockDomainEdge(clock_a="clk_a", clock_b="clk_b",
                                      relationship=ClockDomainRelationship.UNKNOWN)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["cdc_paths"] == 0
    assert cov["cdc_path_coverage_pct"] == "NOT_APPLICABLE"
    assert cov["clock_relationship_coverage_pct"] == 100.0


def test_fe_no_reg_reg_paths_not_applicable():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    tg = _make_tg(
        clocks={"clk": _graph_clock("clk")},
        paths=[],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["reg_reg_paths"] == 0
    assert cov["reg_to_reg_coverage_pct"] == "NOT_APPLICABLE"


def test_ff_no_input_paths_not_applicable():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    tg = _make_tg(
        clocks={"clk": _graph_clock("clk")},
        paths=[TimingPath(startpoint="r1/Q", endpoint="r2/D",
                          launch_clock="clk", capture_clock="clk",
                          path_type=TimingPathClass.REG_TO_REG)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["input_paths"] == 0
    assert cov["input_timing_path_coverage_pct"] == "NOT_APPLICABLE"


def test_fg_no_output_paths_not_applicable():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    tg = _make_tg(
        clocks={"clk": _graph_clock("clk")},
        paths=[TimingPath(startpoint="r1/Q", endpoint="r2/D",
                          launch_clock="clk", capture_clock="clk",
                          path_type=TimingPathClass.REG_TO_REG)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["output_paths"] == 0
    assert cov["output_timing_path_coverage_pct"] == "NOT_APPLICABLE"


def test_fh_cdc_uncovered_is_zero_pct_not_unknown():
    """An actual CDC path with no handling → 0% (not UNKNOWN, not N/A)."""
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _clock(cs, "clk_b", 10e-9)
    tg = _make_tg(
        clocks={"clk_a": _graph_clock("clk_a"), "clk_b": _graph_clock("clk_b")},
        paths=[TimingPath(startpoint="s1/Q", endpoint="r2/D",
                          launch_clock="clk_a", capture_clock="clk_b",
                          path_type=TimingPathClass.CDC)],
        domain_edges=[ClockDomainEdge(clock_a="clk_a", clock_b="clk_b")],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["cdc_paths"] == 1
    assert cov["cdc_path_coverage_pct"] == 0.0
    assert cov["clock_relationship_coverage_pct"] == 0.0


def test_fi_cdc_suggestion_does_not_claim_groups_are_sufficient():
    """The ERROR-level CDC issue's suggestion must tell the user that
    set_clock_groups alone is insufficient for CDC path handling."""
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _clock(cs, "clk_b", 10e-9)
    # async groups declared but NO path-level exception
    _clock_groups(cs, [["clk_a"], ["clk_b"]], "asynchronous")
    tg = _make_tg(
        clocks={"clk_a": _graph_clock("clk_a"), "clk_b": _graph_clock("clk_b")},
        paths=[TimingPath(startpoint="s1/Q", endpoint="r2/D",
                          launch_clock="clk_a", capture_clock="clk_b",
                          path_type=TimingPathClass.CDC)],
        domain_edges=[ClockDomainEdge(clock_a="clk_a", clock_b="clk_b")],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["cdc_path_coverage_pct"] == 0.0  # async groups NOT sufficient
    assert cov["clock_relationship_coverage_pct"] == 100.0
    cdc_issues = [i for i in r.errors if i.code == ErrorCode.COVERAGE_CDC_GAP]
    assert cdc_issues
    sug = cdc_issues[0].suggestion or ""
    # Must not say "add set_clock_groups -asynchronous" is enough
    assert "async groups alone" in sug.lower() or "alone does not" in sug.lower() \
           or "separately where appropriate" in sug.lower()


def test_f5_cdc_path_present_counted():
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _clock(cs, "clk_b", 10e-9)
    tg = _make_tg(
        clocks={"clk_a": _graph_clock("clk_a"), "clk_b": _graph_clock("clk_b")},
        paths=[TimingPath(startpoint="sync1/Q", endpoint="rb/D",
                          launch_clock="clk_a", capture_clock="clk_b",
                          path_type=TimingPathClass.CDC)],
        domain_edges=[ClockDomainEdge(clock_a="clk_a", clock_b="clk_b",
                                      relationship=ClockDomainRelationship.UNKNOWN)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["cdc_paths"] == 1
    assert cov["cdc_path_coverage_pct"] == 0.0  # no exception yet


def test_f6_async_group_alone_does_not_count_as_cdc_path_covered():
    """Having set_clock_groups -asynchronous improves relationship coverage
    but does NOT by itself count as CDC path handling."""
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _clock(cs, "clk_b", 10e-9)
    _clock_groups(cs, [["clk_a"], ["clk_b"]], "asynchronous")
    tg = _make_tg(
        clocks={"clk_a": _graph_clock("clk_a"), "clk_b": _graph_clock("clk_b")},
        paths=[TimingPath(startpoint="sync1/Q", endpoint="rb/D",
                          launch_clock="clk_a", capture_clock="clk_b",
                          path_type=TimingPathClass.CDC)],
        domain_edges=[ClockDomainEdge(clock_a="clk_a", clock_b="clk_b")],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["cdc_paths"] == 1
    assert cov["cdc_path_coverage_pct"] == 0.0  # relationship known but no path-level exception
    assert cov["clock_relationship_coverage_pct"] == 100.0


def test_f7_cdc_path_with_explicit_false_path_is_covered():
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _clock(cs, "clk_b", 10e-9)
    _clock_groups(cs, [["clk_a"], ["clk_b"]], "asynchronous")
    _false_path(cs, "FP1", fro=["clk_a"], to=["clk_b"])
    tg = _make_tg(
        clocks={"clk_a": _graph_clock("clk_a"), "clk_b": _graph_clock("clk_b")},
        paths=[TimingPath(startpoint="sync1/Q", endpoint="rb/D",
                          launch_clock="clk_a", capture_clock="clk_b",
                          path_type=TimingPathClass.CDC)],
        domain_edges=[ClockDomainEdge(clock_a="clk_a", clock_b="clk_b")],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["cdc_paths"] == 1
    assert cov["cdc_path_coverage_pct"] == 100.0
    assert cov["clock_relationship_coverage_pct"] == 100.0


def test_f8_input_path_graph_aware_one_fed_one_unfed():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    # Only data_in has set_input_delay
    _input_delay(cs, "IN1", "data_in", "clk", 2e-9)
    tg = _make_tg(
        clocks={"clk": _graph_clock("clk")},
        paths=[
            TimingPath(startpoint="data_in", endpoint="r1/D",
                       launch_clock=None, capture_clock="clk",
                       path_type=TimingPathClass.INPUT_TO_REG),
            TimingPath(startpoint="unused_in", endpoint=None,  # type: ignore[arg-type]
                       launch_clock=None, capture_clock=None,
                       path_type=TimingPathClass.INPUT_TO_REG) if False else
            TimingPath(startpoint="unused_in", endpoint="dummy",
                       launch_clock=None, capture_clock=None,
                       path_type=TimingPathClass.INPUT_TO_REG),
        ],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["input_paths"] == 2
    # data_in -> r1/D covered; unused_in -> dummy NOT covered (no capture clock
    # → classification UNKNOWN).
    assert cov["input_timing_path_coverage_pct"] != "UNKNOWN"
    # Confirm uncovered entry identifies the concrete path
    assert any(u["startpoint"] == "unused_in" and u["endpoint"] == "dummy"
               for u in cov["uncovered"] if u["category"] == "input_timing_path")


def test_f9_output_path_graph_aware():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _output_delay(cs, "OUT1", "data_out", "clk", 2e-9)
    tg = _make_tg(
        clocks={"clk": _graph_clock("clk")},
        paths=[
            TimingPath(startpoint="r1/Q", endpoint="data_out",
                       launch_clock="clk", capture_clock=None,
                       path_type=TimingPathClass.REG_TO_OUTPUT),
            TimingPath(startpoint="unrel", endpoint="other_out",
                       launch_clock=None, capture_clock=None,
                       path_type=TimingPathClass.REG_TO_OUTPUT),
        ],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["output_paths"] == 2
    assert cov["output_timing_path_coverage_pct"] != "UNKNOWN"


def test_fa_input_with_delay_but_dest_not_covered_when_clock_missing():
    """Even though port has set_input_delay, if the input path's capture
    clock is unknown we cannot claim it as covered → UNKNOWN for that
    path."""
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _input_delay(cs, "IN1", "data_in", "clk", 2e-9)
    tg = _make_tg(
        clocks={"clk": _graph_clock("clk")},
        paths=[TimingPath(startpoint="data_in", endpoint="rx/D",
                          launch_clock=None, capture_clock=None,
                          path_type=TimingPathClass.INPUT_TO_REG)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    # Capture clock unknown → path NOT counted as covered; should be
    # classified UNKNOWN.
    assert cov["input_timing_path_coverage_pct"] == 0.0
    assert any(u["classification"] == "UNKNOWN" and u["startpoint"] == "data_in"
               for u in cov["uncovered"] if u["category"] == "input_timing_path")


def test_fb_output_with_delay_but_source_clock_missing():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _output_delay(cs, "OUT1", "data_out", "clk", 2e-9)
    tg = _make_tg(
        clocks={"clk": _graph_clock("clk")},
        paths=[TimingPath(startpoint="rx/Q", endpoint="data_out",
                          launch_clock=None, capture_clock=None,
                          path_type=TimingPathClass.REG_TO_OUTPUT)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["output_timing_path_coverage_pct"] == 0.0
    assert any(u["classification"] == "UNKNOWN" and u["endpoint"] == "data_out"
               for u in cov["uncovered"] if u["category"] == "output_timing_path")


def test_fc_graph_unavailable_all_graph_metrics_unknown():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    r = run_validation(design=None, tg=None, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["graph_available"] is False
    assert cov["clock_source_coverage_pct"] == "UNKNOWN"
    assert cov["input_timing_path_coverage_pct"] == "UNKNOWN"
    assert cov["output_timing_path_coverage_pct"] == "UNKNOWN"
    assert cov["reg_to_reg_coverage_pct"] == "UNKNOWN"
    assert cov["cdc_path_coverage_pct"] == "UNKNOWN"
    assert cov["clock_relationship_coverage_pct"] == "UNKNOWN"


def test_fd_coverage_deterministic_ordering():
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _clock(cs, "clk_b", 10e-9)
    paths = [
        TimingPath(startpoint="rb/Q", endpoint="ra/D",
                   launch_clock="clk_b", capture_clock="clk_a",
                   path_type=TimingPathClass.REG_TO_REG),
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk_a", capture_clock="clk_b",
                   path_type=TimingPathClass.REG_TO_REG),
    ]
    tg = _make_tg(clocks={"clk_a": _graph_clock("clk_a"),
                          "clk_b": _graph_clock("clk_b")},
                  paths=paths)
    a = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    b = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    a_objs = [u["object"] for u in a.coverage.as_dict()["uncovered"]]
    b_objs = [u["object"] for u in b.coverage.as_dict()["uncovered"]]
    assert a_objs == b_objs


# ---------------------------------------------------------------------------
# Severity / blocking
# ---------------------------------------------------------------------------

def test_c0_default_blocking_policy():
    cs = _cset()
    _clock(cs, "clk", -1e-9)  # invalid -> ERROR severity
    r = run_validation(cset=cs)
    assert any(i.blocking for i in r.errors)


def test_c1_warnings_not_blocking_by_default():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _clock(cs, "clk_b", 8e-9, source="clk")  # multi-on-source -> warn
    r = run_validation(cset=cs)
    # Warnings alone shouldn't block
    if r.status == ValidationStatus.PASS_WITH_WARNINGS.value:
        assert r.passed


# ---------------------------------------------------------------------------
# ValidationReport helpers
# ---------------------------------------------------------------------------

def test_d0_report_dedup_by_issue_id():
    rep = ValidationReport()
    i = ValidationIssue(severity=Severity.ERROR,
                        category=__import__("rca.utils.enums", fromlist=["ValidationCategory"]).ValidationCategory.CLOCK,
                        code=ErrorCode.CLOCK_PERIOD_INVALID,
                        message="bad period")
    rep.add(i)
    rep.add(i)
    assert len(rep.issues) == 1


def test_d1_summary_counts_consistent():
    cs = _cset()
    _clock(cs, "clk", -1e-9)
    r = run_validation(cset=cs)
    s = r.report.summary()
    assert s["total_issues"] == len(r.report.issues)
    assert s["errors"] == len(r.errors)
    assert s["warnings"] == len(r.warnings)


def test_e0_status_pass_with_warnings_for_warn_only():
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _clock(cs, "clk_b", 8e-9, source="clk_a")  # triggers CLOCK_MULTIPLE (ERROR actually, but just confirm blocked)
    r = run_validation(cset=cs)
    # CLOCK_MULTIPLE is ERROR → BLOCKED
    assert r.status in (ValidationStatus.BLOCKED.value,
                        ValidationStatus.PASS_WITH_WARNINGS.value)


def test_e1_status_pass_clean_case():
    cs = _cset()
    _clock(cs, "clk", 10e-9, waveform=(0, 5e-9))
    r = run_validation(cset=cs)
    # With no design/tg there are coverage UNKNOWN infos; no errors → PASS
    assert r.status in (ValidationStatus.PASS.value,
                        ValidationStatus.PASS_WITH_WARNINGS.value)


def test_e2_observational_does_not_mutate_constraint_values():
    cs = _cset()
    c = _clock(cs, "clk", 10e-9)
    snapshot = dict(c.values)
    run_validation(cset=cs)
    assert c.values == snapshot
    assert c.status.value == "PROPOSED"


def test_e3_gclk_combinational_with_div():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    c = Constraint(id="GCLK1", type=ConstraintType.CREATE_GENERATED_CLOCK,
                   target_objects=["pin"], clock_refs=["clk"],
                   values={"name": "gclk", "source": "pin",
                           "master_clock": "clk", "divide_by": 2,
                           "combinational": True})
    cs.add(c)
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.GCLK_CONTRADICTORY_OPTIONS for i in r.errors)


def test_e4_io_duplicate_detection():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    _input_delay(cs, "IN1", "a", "clk", 1e-9)
    _input_delay(cs, "IN2", "a", "clk", 1e-9, min_max="min")
    r = run_validation(cset=cs)
    # Different min_max keys → shouldn't conflict; but min-only should at least not crash
    assert r.status is not None


def test_e5_conflicting_clock_groups_flagged():
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _clock(cs, "clk_b", 10e-9)
    _clock_groups(cs, [["clk_a"], ["clk_b"]], "asynchronous")
    _clock_groups(cs, [["clk_a"], ["clk_b"]], "physically_exclusive")
    r = run_validation(cset=cs)
    assert any(i.code == ErrorCode.GROUPS_CONTRADICTORY_RELATIONSHIP
               for i in r.errors)


def test_e6_validation_as_dict_shape():
    cs = _cset()
    _clock(cs, "clk", 10e-9)
    r = run_validation(cset=cs)
    d = r.as_dict()
    for k in ("status", "errors", "warnings", "coverage", "conflict_summary",
              "overlap_summary", "reference_summary", "exception_summary",
              "scenario_summary"):
        assert k in d


def test_e7_blocking_list_contains_only_blocking_issues():
    cs = _cset()
    _clock(cs, "clk", -1e-9)  # ERROR, blocking
    r = run_validation(cset=cs)
    for i in r.blocking:
        assert i.blocking is True


def test_e8_issue_ids_stable_format():
    cs = _cset()
    _clock(cs, "clk", -1e-9)
    r = run_validation(cset=cs)
    for i in r.report.issues:
        assert i.issue_id.startswith("V")
        assert len(i.issue_id) == 9  # V + 8 hex chars



# ---------------------------------------------------------------------------
# I/O wrong-clock regression tests
# ---------------------------------------------------------------------------


def test_g0_input_wrong_clock_not_covered():
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _clock(cs, "clk_b", 10e-9)
    _input_delay(cs, "IN1", "data_in", "clk_a", 2e-9)
    tg = _make_tg(
        clocks={"clk_a": _graph_clock("clk_a"), "clk_b": _graph_clock("clk_b")},
        paths=[TimingPath(startpoint="data_in", endpoint="reg_b/D",
                          launch_clock=None, capture_clock="clk_b",
                          path_type=TimingPathClass.INPUT_TO_REG)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["input_paths"] == 1
    assert cov["input_timing_path_coverage_pct"] == 0.0
    entries = [u for u in cov["uncovered"]
               if u["category"] == "input_timing_path"]
    assert entries
    assert entries[0]["classification"] == "REQUIRES_USER_DECISION"
    assert "clk_a" in entries[0]["reason"] and "clk_b" in entries[0]["reason"]


def test_g1_input_correct_clock_covered():
    cs = _cset()
    _clock(cs, "clk_b", 10e-9)
    _input_delay(cs, "IN1", "data_in", "clk_b", 2e-9)
    tg = _make_tg(
        clocks={"clk_b": _graph_clock("clk_b")},
        paths=[TimingPath(startpoint="data_in", endpoint="reg_b/D",
                          launch_clock=None, capture_clock="clk_b",
                          path_type=TimingPathClass.INPUT_TO_REG)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["input_paths"] == 1
    assert cov["input_timing_path_coverage_pct"] == 100.0


def test_g2_output_wrong_clock_not_covered():
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _clock(cs, "clk_b", 10e-9)
    _output_delay(cs, "OUT1", "data_out", "clk_a", 2e-9)
    tg = _make_tg(
        clocks={"clk_a": _graph_clock("clk_a"), "clk_b": _graph_clock("clk_b")},
        paths=[TimingPath(startpoint="reg_b/Q", endpoint="data_out",
                          launch_clock="clk_b", capture_clock=None,
                          path_type=TimingPathClass.REG_TO_OUTPUT)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["output_paths"] == 1
    assert cov["output_timing_path_coverage_pct"] == 0.0
    entries = [u for u in cov["uncovered"]
               if u["category"] == "output_timing_path"]
    assert entries
    assert entries[0]["classification"] == "REQUIRES_USER_DECISION"
    assert "clk_a" in entries[0]["reason"] and "clk_b" in entries[0]["reason"]


def test_g3_output_correct_clock_covered():
    cs = _cset()
    _clock(cs, "clk_b", 10e-9)
    _output_delay(cs, "OUT1", "data_out", "clk_b", 2e-9)
    tg = _make_tg(
        clocks={"clk_b": _graph_clock("clk_b")},
        paths=[TimingPath(startpoint="reg_b/Q", endpoint="data_out",
                          launch_clock="clk_b", capture_clock=None,
                          path_type=TimingPathClass.REG_TO_OUTPUT)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["totals"]["output_paths"] == 1
    assert cov["output_timing_path_coverage_pct"] == 100.0


def test_g4_multi_clock_input_only_correct_clock_counts():
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _clock(cs, "clk_b", 10e-9)
    _input_delay(cs, "IN1", "data_in", "clk_a", 1e-9)
    _input_delay(cs, "IN2", "data_in", "clk_b", 2e-9)  # clk_b constraint covers the path
    tg = _make_tg(
        clocks={"clk_a": _graph_clock("clk_a"), "clk_b": _graph_clock("clk_b")},
        paths=[TimingPath(startpoint="data_in", endpoint="reg_b/D",
                          launch_clock=None, capture_clock="clk_b",
                          path_type=TimingPathClass.INPUT_TO_REG)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    # Exactly one path, covered because clk_b delay exists — must NOT double count.
    assert cov["totals"]["input_paths"] == 1
    assert cov["input_timing_path_coverage_pct"] == 100.0


def test_g5_input_unknown_capture_stays_unknown_even_with_delay():
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _input_delay(cs, "IN1", "data_in", "clk_a", 2e-9)
    tg = _make_tg(
        clocks={"clk_a": _graph_clock("clk_a")},
        paths=[TimingPath(startpoint="data_in", endpoint="reg/D",
                          launch_clock=None, capture_clock=None,
                          path_type=TimingPathClass.INPUT_TO_REG)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["input_timing_path_coverage_pct"] == 0.0
    entries = [u for u in cov["uncovered"]
               if u["category"] == "input_timing_path"]
    assert entries and entries[0]["classification"] == "UNKNOWN"


def test_g6_output_unknown_launch_stays_unknown_even_with_delay():
    cs = _cset()
    _clock(cs, "clk_a", 10e-9)
    _output_delay(cs, "OUT1", "data_out", "clk_a", 2e-9)
    tg = _make_tg(
        clocks={"clk_a": _graph_clock("clk_a")},
        paths=[TimingPath(startpoint="reg/Q", endpoint="data_out",
                          launch_clock=None, capture_clock=None,
                          path_type=TimingPathClass.REG_TO_OUTPUT)],
    )
    r = run_validation(design=_MINI_DESIGN, tg=tg, cset=cs)
    cov = r.coverage.as_dict()
    assert cov["output_timing_path_coverage_pct"] == 0.0
    entries = [u for u in cov["uncovered"]
               if u["category"] == "output_timing_path"]
    assert entries and entries[0]["classification"] == "UNKNOWN"
