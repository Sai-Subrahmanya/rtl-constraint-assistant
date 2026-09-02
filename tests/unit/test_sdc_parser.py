"""SDC parser / importer tests."""
import pytest
from rca.sdc import SDCParser
from rca.utils.enums import ConstraintType


SDC_SAMPLE = """
create_clock -name clk -period 10.000 [get_ports clk]
set_input_delay -max 2.000 -clock clk [get_ports d]
set_output_delay -max 2.000 -clock clk [get_ports q]
set_clock_groups -asynchronous -group {clk_a} -group {clk_b}
set_false_path -from [get_ports a] -to [get_ports b]
"""


def test_parse_basic_sdc():
    p = SDCParser()
    cset = p.parse_text(SDC_SAMPLE)
    types = {c.type for c in cset}
    assert ConstraintType.CREATE_CLOCK in types
    assert ConstraintType.SET_INPUT_DELAY in types
    assert ConstraintType.SET_OUTPUT_DELAY in types
    assert ConstraintType.SET_CLOCK_GROUPS in types
    assert ConstraintType.SET_FALSE_PATH in types


def test_clock_values():
    p = SDCParser()
    cset = p.parse_text(SDC_SAMPLE)
    clk = [c for c in cset if c.type == ConstraintType.CREATE_CLOCK][0]
    assert clk.values["name"] == "clk"
    assert clk.values["period"] == pytest.approx(10e-9, rel=1e-6)


def test_unknown_command_warning():
    p = SDCParser()
    cset = p.parse_text("some_weird_command -x 1\n")
    assert len(p.warnings) == 1
    assert "unknown" in p.warnings[0].lower() or "minimal support" in p.warnings[0].lower()


def test_imported_confidence():
    p = SDCParser()
    cset = p.parse_text(SDC_SAMPLE)
    for c in cset:
        assert c.confidence.value in ("HIGH", "MEDIUM", "LOW")
