"""Step 6 golden SDC tests (acc. #26): render representative UCMs and
confirm the generated SDC matches the semantic contract (not
byte-identical formatting).

Each golden case builds a ConstraintSet, renders with the generic
backend, and asserts specific canonical SDC constructs appear.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "..") + "/src")

from rca.constraint_model import Constraint, ConstraintSet, PathSelector
from rca.sdc import get_backend
from rca.sdc.generation.result import GenerationStatus
from rca.utils.enums import (
    ConstraintStatus, ConstraintType, Confidence, OptimizationStatus,
    SafeMode, SourceKind,
)


def _c(**kw):
    defaults = dict(source_kind=SourceKind.USER,
                    confidence=Confidence.HIGH,
                    status=ConstraintStatus.FIXED,
                    opt_status=OptimizationStatus.FIXED)
    defaults.update(kw)
    return Constraint(**defaults)


def _render(cs):
    return get_backend("generic").generate(cs, design_name="golden",
                                           mode=SafeMode.BALANCED,
                                           with_provenance=False)


CASES: list[tuple[str, callable]] = []


def case(name):
    def wrap(fn):
        CASES.append((name, fn))
        return fn
    return wrap


# ---- 13 representative UCMs (acc. #26) ----

@case("01_single_clock")
def _():
    cs = ConstraintSet(name="t")
    cs.add(_c(id="CLK", type=ConstraintType.CREATE_CLOCK,
              target_objects=["clk"], clock_refs=["clk"],
              values={"name": "clk", "period": 10e-9, "waveform": [0, 5e-9]}))
    return cs, ["create_clock -name clk -period 10 -waveform { 0 5 } [get_ports clk]"]


@case("02_generated_div2")
def _():
    cs = ConstraintSet(name="t")
    cs.add(_c(id="CLK", type=ConstraintType.CREATE_CLOCK,
              target_objects=["clk"], clock_refs=["clk"],
              values={"name": "clk", "period": 10e-9}))
    cs.add(_c(id="GCLK", type=ConstraintType.CREATE_GENERATED_CLOCK,
              source_objects=["U/Q"], target_objects=["U/Q"], clock_refs=["clk"],
              values={"name": "gclk", "source": "U/Q", "master_clock": "clk",
                      "divide_by": 2}))
    return cs, ["-divide_by 2", "-master_clock clk", "[get_pins U/Q]"]


@case("03_generated_mul")
def _():
    cs = ConstraintSet(name="t")
    cs.add(_c(id="G", type=ConstraintType.CREATE_GENERATED_CLOCK,
              source_objects=["U/Q"], target_objects=["U/Q"], clock_refs=["clk"],
              values={"name": "gp", "source": "U/Q", "master_clock": "clk",
                      "multiply_by": 2}))
    return cs, ["-multiply_by 2"]


@case("04_io_delays")
def _():
    cs = ConstraintSet(name="t")
    for e, mm, ed in [("in", "max", 2.0), ("in", "min", 0.5),
                      ("out", "max", 3.0), ("out", "min", 0.8)]:
        t = ConstraintType.SET_INPUT_DELAY if e == "in" else ConstraintType.SET_OUTPUT_DELAY
        cmd = "set_input_delay" if e == "in" else "set_output_delay"
        cs.add(_c(id=f"{e.upper()}{mm}", type=t,
                  target_objects=["d"], clock_refs=["clk"],
                  values={"delay": ed * 1e-9, "min_max": mm}))
    return cs, ["set_input_delay -max -clock clk 2 [get_ports d]",
                "set_input_delay -min -clock clk 0.5 [get_ports d]",
                "set_output_delay -max -clock clk 3 [get_ports d]",
                "set_output_delay -min -clock clk 0.8 [get_ports d]"]


@case("05_clock_uncertainty")
def _():
    cs = ConstraintSet(name="t")
    cs.add(_c(id="U", type=ConstraintType.SET_CLOCK_UNCERTAINTY,
              target_objects=["clk"], clock_refs=["clk"],
              values={"uncertainty": 0.1e-9, "setup_hold": "setup"}))
    cs.add(_c(id="U2", type=ConstraintType.SET_CLOCK_UNCERTAINTY,
              values={"uncertainty": 0.2e-9, "setup_hold": "hold"},
              path_selector=PathSelector(from_set=["a"], to_set=["b"])))
    return cs, ["set_clock_uncertainty -setup 0.1 [get_clocks clk]",
                "set_clock_uncertainty -hold 0.2"]


@case("06_clock_latency_source")
def _():
    cs = ConstraintSet(name="t")
    cs.add(_c(id="L", type=ConstraintType.SET_CLOCK_LATENCY,
              target_objects=["clk"], clock_refs=["clk"],
              values={"latency": 0.5e-9, "source": True, "late": True,
                      "min_max": "max"}))
    return cs, ["set_clock_latency -source -late -max 0.5 [get_clocks clk]"]


@case("07_propagated_clock")
def _():
    cs = ConstraintSet(name="t")
    cs.add(_c(id="P", type=ConstraintType.SET_PROPAGATED_CLOCK,
              target_objects=["clk"], clock_refs=["clk"], values={}))
    return cs, ["set_propagated_clock [get_clocks clk]"]


@case("08_clock_groups")
def _():
    cs = ConstraintSet(name="t")
    cs.add(_c(id="CG", type=ConstraintType.SET_CLOCK_GROUPS,
              values={"relationship": "asynchronous",
                      "groups": [["a", "b"], ["c"]]}))
    return cs, ["set_clock_groups -asynchronous -group [get_clocks { a b }] -group [get_clocks c]"]


@case("09_false_path_through")
def _():
    cs = ConstraintSet(name="t")
    cs.add(_c(id="FP", type=ConstraintType.SET_FALSE_PATH,
              path_selector=PathSelector(from_set=["A"],
                                         through_set=[["B"], ["C"]],
                                         to_set=["D"])))
    return cs, ["set_false_path -from [get_clocks A]",
                "-through [get_pins B]", "-through [get_pins C]",
                "-to [get_clocks D]"]


@case("10_multicycle")
def _():
    cs = ConstraintSet(name="t")
    cs.add(_c(id="MC", type=ConstraintType.SET_MULTICYCLE_PATH,
              values={"cycles": 2, "setup_hold": "setup"},
              path_selector=PathSelector(from_set=["a"], to_set=["b"],
                                         setup_hold="setup")))
    return cs, ["set_multicycle_path 2 -setup -from [get_clocks a] -to [get_clocks b]"]


@case("11_min_max_delay")
def _():
    cs = ConstraintSet(name="t")
    cs.add(_c(id="MN", type=ConstraintType.SET_MIN_DELAY,
              values={"delay": 0.1e-9},
              path_selector=PathSelector(from_set=["x"], to_set=["y"])))
    cs.add(_c(id="MX", type=ConstraintType.SET_MAX_DELAY,
              values={"delay": 5e-9},
              path_selector=PathSelector(from_set=["x"], through_set=[["A", "B"]],
                                         to_set=["y"])))
    return cs, ["set_min_delay 0.1 -from [get_clocks x]",
                "set_max_delay 5 -from [get_clocks x] -through [get_pins { A B }] -to [get_ports y]"]


@case("12_drc_constraints")
def _():
    cs = ConstraintSet(name="t")
    cs.add(_c(id="L", type=ConstraintType.SET_LOAD, target_objects=["q"],
              values={"value": 0.05}))
    cs.add(_c(id="T", type=ConstraintType.SET_INPUT_TRANSITION, target_objects=["d"],
              values={"transition": 0.1e-9}))
    cs.add(_c(id="MT", type=ConstraintType.SET_MAX_TRANSITION, target_objects=["d"],
              values={"transition": 0.5e-9}))
    cs.add(_c(id="DC", type=ConstraintType.SET_DRIVING_CELL, target_objects=["d"],
              values={"lib_cell": "BUF_X1"}))
    return cs, ["set_load 0.05 [get_ports q]",
                "set_input_transition 0.1 [get_ports d]",
                "set_max_transition 0.5 [get_ports d]",
                "set_driving_cell -lib_cell BUF_X1 [get_ports d]"]


@case("13_all_clocks_propagated")
def _():
    cs = ConstraintSet(name="t")
    cs.add(_c(id="P", type=ConstraintType.SET_PROPAGATED_CLOCK, values={}))
    return cs, ["set_propagated_clock [all_clocks]"]


@pytest.mark.parametrize("name,fn", CASES, ids=[n for n, _ in CASES])
def test_golden(name, fn):
    cs, expects = fn()
    r = _render(cs)
    assert r.status == GenerationStatus.COMPLETE, f"golden {name} not COMPLETE: {r.skipped_constraint_ids}"
    for exp in expects:
        assert exp in r.text, f"golden {name} missing: {exp!r}\n{r.text}"
    # Write golden .sdc file for inspection.
    out = Path(__file__).parent / f"{name}.sdc"
    out.write_text(r.text, encoding="utf-8")
