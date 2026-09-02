"""Tests for the Universal Constraint Model, SDC generation, and validation."""
import pytest
from rca.constraint_model import ConstraintSet
from rca.sdc import SDCParser, get_backend
from rca.sdc.generic import GenericSDCBackend
from rca.utils.enums import ConstraintType, SourceKind, Confidence, SafeMode


def _make_cset():
    cset = ConstraintSet(name="test")
    cset.create_clock(name="clk", period_seconds=10e-9, source="clk",
                      source_kind=SourceKind.USER, confidence=Confidence.HIGH, fixed=True)
    cset.create_input_delay(port="d", clock="clk", delay_seconds=2e-9, source_kind=SourceKind.USER)
    cset.create_output_delay(port="q", clock="clk", delay_seconds=2e-9, source_kind=SourceKind.USER)
    return cset


def test_constraint_set_basics():
    cset = _make_cset()
    assert len(cset.clocks()) == 1
    assert len(cset.io_constraints()) == 2


def test_generic_sdc_generation():
    cset = _make_cset()
    backend = GenericSDCBackend()
    sdc = backend.render(cset, design_name="t", mode=SafeMode.BALANCED)
    assert "create_clock" in sdc
    assert "-period 10" in sdc
    assert "set_input_delay" in sdc
    assert "set_output_delay" in sdc
    assert "[get_ports clk]" in sdc


def test_opensta_sdc_has_units():
    cset = _make_cset()
    backend = get_backend("opensta")
    sdc = backend.render(cset)
    assert "set_units" in sdc


def test_sdc_import_roundtrip():
    cset = _make_cset()
    sdc = GenericSDCBackend().render(cset)
    # Use SdcImporter (Step 5) which understands collections/selectors,
    # not the raw SDCParser that returns ParsedSdc.
    from rca.sdc_importer import SdcImporter
    res = SdcImporter(run_ts="2025-01-01T00:00:00+00:00", run_id="rt").from_text(sdc, source_file="<s>")
    cset2 = res.constraint_set
    assert len(cset2.clocks()) == 1
    # The importer treats unqualified (no -min/-max) set_input_delay as
    # max-only per its default_min_max="max" policy (pre-existing Step 5
    # behavior; not modified in Step 6), expanding into max-rise and
    # max-fall per port -> 2 per I/O = 4 total.
    assert len(cset2.io_constraints()) == 4
    clk = cset2.clocks()[0]
    assert clk.values["name"] == "clk"
    assert clk.values["period"] == pytest.approx(10e-9)
    for c in cset2.io_constraints():
        assert c.values.get("min_max") == "max"


def test_emittable_respects_mode():
    cset = _make_cset()
    strict = cset.emittable(SafeMode.STRICT)
    balanced = cset.emittable(SafeMode.BALANCED)
    # Strict should not drop user/HIGH confidence constraints
    assert len(strict) == 3
    assert len(balanced) == 3
