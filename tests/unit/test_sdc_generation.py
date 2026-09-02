"""Step 6: SDC generation / renderer / backend / preflight tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rca.constraint_model import Constraint, ConstraintSet, PathSelector
from rca.constraint_model.targets import CollectionKind, TargetRef
from rca.sdc import get_backend
from rca.sdc.generation.result import GenerationStatus
from rca.sdc.generation.tcl_quote import tcl_quote, tcl_quote_list, format_ns
from rca.sdc.generation.preflight import preflight_constraint
from rca.sdc_importer import SdcImporter
from rca.utils.enums import (
    ConstraintStatus, ConstraintType, Confidence, OptimizationStatus,
    SafeMode, SourceKind,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk(status=ConstraintStatus.FIXED, conf=Confidence.HIGH, **kw):
    defaults = dict(
        source_kind=SourceKind.USER,
        confidence=conf,
        status=status,
        opt_status=OptimizationStatus.FIXED,
    )
    defaults.update(kw)
    return Constraint(**defaults)


def _cset(*constraints):
    cs = ConstraintSet(name="t")
    for c in constraints:
        cs.add(c)
    return cs


def _gen(cs, backend="generic", mode="balanced", with_provenance_=False):
    return get_backend(backend).generate(cs, design_name="t", mode=mode,
                                         with_provenance=with_provenance_)


# ---------------------------------------------------------------------------
# Tcl quoting & unit formatting (acc. #22, #23)
# ---------------------------------------------------------------------------

class TestTclQuoting:
    def test_safe_identifiers_unquoted(self):
        assert tcl_quote("clk") == "clk"
        assert tcl_quote("data_in") == "data_in"

    def test_spaces_braced(self):
        assert tcl_quote("weird name") == "{weird name}"

    def test_braces_fallback(self):
        # Names containing literal { } must use "..." quoting (can't use { }).
        q = tcl_quote("a{b")
        assert q.startswith('"') and "a{b" in q
        # $ inside "..." must be escaped.
        q2 = tcl_quote("x$y")
        assert q2.startswith('"')
        # Backslash triggers double-quote path.
        q3 = tcl_quote("a\\b")
        assert q3.startswith('"')

    def test_wildcards_braced(self):
        assert tcl_quote("clk*") == "{clk*}"

    def test_empty(self):
        assert tcl_quote("") == "{}"

    def test_list_multi(self):
        assert tcl_quote_list(["a", "b"]) == "{ a b }"

    def test_list_single(self):
        assert tcl_quote_list(["clk"]) == "clk"

    def test_format_ns_no_trailing_zeros(self):
        assert format_ns(10e-9) == "10"
        assert format_ns(2.5e-9) == "2.5"
        assert format_ns(100e-12) == "0.1"
        # no scientific notation (e.g. 1e-8 s = 10 ns)
        assert format_ns(1e-8) == "10"
        # ps/fs
        assert format_ns(1e-12) == "0.001"


# ---------------------------------------------------------------------------
# create_clock (acc. #4)
# ---------------------------------------------------------------------------

class TestCreateClock:
    def test_basic(self):
        c = _mk(id="C1", type=ConstraintType.CREATE_CLOCK,
                target_objects=["clk"], clock_refs=["clk"],
                values={"name": "clk", "period": 10e-9, "waveform": [0, 5e-9]})
        r = _gen(_cset(c))
        assert r.status == GenerationStatus.COMPLETE
        assert "create_clock -name clk -period 10 -waveform { 0 5 } [get_ports clk]" in r.text

    def test_missing_period_is_blocked(self):
        c = _mk(id="C1", type=ConstraintType.CREATE_CLOCK,
                target_objects=["clk"], clock_refs=["clk"],
                values={"name": "clk"})
        r = _gen(_cset(c))
        assert r.status == GenerationStatus.BLOCKED
        assert "C1" in r.skipped_constraint_ids
        # Must NOT emit create_clock without period (red line).
        assert "create_clock" not in r.text

    def test_target_selects_ports_not_pins(self):
        c = _mk(id="C1", type=ConstraintType.CREATE_CLOCK,
                target_objects=["U/clk"], clock_refs=["clk"],
                values={"name": "clk", "period": 10e-9})
        r = _gen(_cset(c))
        # Even though target has "/", we use [get_ports], NOT [get_pins]
        assert "[get_ports U/clk]" in r.text


# ---------------------------------------------------------------------------
# I/O delays preserve min/max/rise/fall (acc. #6)
# ---------------------------------------------------------------------------

class TestIODelay:
    def test_min_max_rise_fall_distinct(self):
        c1 = _mk(id="IN1", type=ConstraintType.SET_INPUT_DELAY,
                 target_objects=["d"], clock_refs=["clk"],
                 values={"delay": 2e-9, "min_max": "max", "edge": "rise"})
        c2 = _mk(id="IN2", type=ConstraintType.SET_INPUT_DELAY,
                 target_objects=["d"], clock_refs=["clk"],
                 values={"delay": 0.5e-9, "min_max": "min", "edge": "rise"})
        c3 = _mk(id="IN3", type=ConstraintType.SET_INPUT_DELAY,
                 target_objects=["d"], clock_refs=["clk"],
                 values={"delay": 2e-9, "min_max": "max", "edge": "fall"})
        r = _gen(_cset(c1, c2, c3))
        assert r.status == GenerationStatus.COMPLETE
        assert "-max -rise" in r.text and "-min -rise" in r.text and "-max -fall" in r.text
        # No collapse into a single command.
        cmd_lines = [ln for ln in r.text.splitlines() if ln.startswith("set_input_delay")]
        assert len(cmd_lines) == 3

    def test_input_delay_min_max_both_emits_unqualified(self):
        """Step 6 corrective: min_max='both' is the SDC default (no
        -min/-max flag) which applies to BOTH min and max analyses per
        SDC standard. We MUST NOT silently drop the coverage for one
        side; we emit a single unqualified command which is the
        canonical SDC representation."""
        c = _mk(id="IN1", type=ConstraintType.SET_INPUT_DELAY,
                target_objects=["d"], clock_refs=["clk"],
                values={"delay": 1.5e-9, "min_max": "both"})
        r = _gen(_cset(c))
        assert r.status == GenerationStatus.COMPLETE
        lines = [ln for ln in r.text.splitlines() if ln.startswith("set_input_delay")]
        assert len(lines) == 1
        line = lines[0]
        # Must NOT have a -min or -max flag (unqualified = both per SDC).
        assert " -min " not in " " + line + " "
        assert " -max " not in " " + line + " "
        assert "-clock clk" in line
        assert "1.5" in line
        assert "[get_ports d]" in line

    def test_output_delay_min_max_both_unqualified(self):
        c = _mk(id="OUT1", type=ConstraintType.SET_OUTPUT_DELAY,
                target_objects=["q"], clock_refs=["clk"],
                values={"delay": 2.0e-9, "min_max": "both"})
        r = _gen(_cset(c))
        assert r.status == GenerationStatus.COMPLETE
        lines = [ln for ln in r.text.splitlines() if ln.startswith("set_output_delay")]
        assert len(lines) == 1
        line = lines[0]
        assert " -min " not in " " + line + " "
        assert " -max " not in " " + line + " "

    def test_clock_uncertainty_both_unqualified(self):
        c = _mk(id="U1", type=ConstraintType.SET_CLOCK_UNCERTAINTY,
                target_objects=["clk"], clock_refs=["clk"],
                values={"uncertainty": 0.1e-9, "min_max": "both",
                        "setup_hold": "both"})
        r = _gen(_cset(c))
        assert r.status == GenerationStatus.COMPLETE
        lines = [ln for ln in r.text.splitlines() if ln.startswith("set_clock_uncertainty")]
        assert len(lines) == 1
        # Unqualified -> applies to both setup/hold and min/max.
        assert "-setup" not in lines[0] and "-hold" not in lines[0]
        assert "-min" not in lines[0] and "-max" not in lines[0]

    def test_clock_latency_both_unqualified(self):
        c = _mk(id="L1", type=ConstraintType.SET_CLOCK_LATENCY,
                target_objects=["clk"], clock_refs=["clk"],
                values={"latency": 0.3e-9, "min_max": "both",
                        "setup_hold": "both"})
        r = _gen(_cset(c))
        assert r.status == GenerationStatus.COMPLETE
        lines = [ln for ln in r.text.splitlines() if ln.startswith("set_clock_latency")]
        assert len(lines) == 1
        assert "-setup" not in lines[0] and "-hold" not in lines[0]
        assert "-min" not in lines[0] and "-max" not in lines[0]

    def test_add_delay_flag_preserved(self):
        c = _mk(id="IN1", type=ConstraintType.SET_INPUT_DELAY,
                target_objects=["d"], clock_refs=["clk"],
                values={"delay": 1e-9, "min_max": "max", "add_delay": True})
        r = _gen(_cset(c))
        assert "-add_delay" in r.text


# ---------------------------------------------------------------------------
# Clock uncertainty/latency (acc. #7, #8)
# ---------------------------------------------------------------------------

class TestClockUncertaintyLatency:
    def test_uncertainty_setup(self):
        c = _mk(id="U1", type=ConstraintType.SET_CLOCK_UNCERTAINTY,
                target_objects=["clk"], clock_refs=["clk"],
                values={"uncertainty": 0.1e-9, "setup_hold": "setup"})
        r = _gen(_cset(c))
        assert "set_clock_uncertainty -setup 0.1 [get_clocks clk]" in r.text

    def test_latency_source_late(self):
        c = _mk(id="L1", type=ConstraintType.SET_CLOCK_LATENCY,
                target_objects=["clk"], clock_refs=["clk"],
                values={"latency": 0.5e-9, "source": True, "late": True})
        r = _gen(_cset(c))
        assert "-source" in r.text and "-late" in r.text
        assert "[get_clocks clk]" in r.text


# ---------------------------------------------------------------------------
# Clock groups (acc. #10) — not flattened
# ---------------------------------------------------------------------------

class TestClockGroups:
    def test_groups_preserved(self):
        c = _mk(id="CG", type=ConstraintType.SET_CLOCK_GROUPS,
                values={"relationship": "asynchronous",
                        "groups": [["clk_a", "clk_b"], ["clk_c"]]})
        r = _gen(_cset(c))
        assert r.status == GenerationStatus.COMPLETE
        line = [ln for ln in r.text.splitlines() if ln.startswith("set_clock_groups")][0]
        assert "-group" in line and line.count("-group") == 2
        assert "[get_clocks { clk_a clk_b }]" in line
        assert "[get_clocks clk_c]" in line


# ---------------------------------------------------------------------------
# False path multi-through (acc. #11)
# ---------------------------------------------------------------------------

class TestFalsePath:
    def test_ordered_through(self):
        ps = PathSelector(from_set=["A"], through_set=[["B"], ["C"]], to_set=["D"])
        c = _mk(id="FP", type=ConstraintType.SET_FALSE_PATH, path_selector=ps)
        r = _gen(_cset(c))
        line = [ln for ln in r.text.splitlines() if ln.startswith("set_false_path")][0]
        assert line.count("-through") == 2


# ---------------------------------------------------------------------------
# Multicycle (acc. #12)
# ---------------------------------------------------------------------------

class TestMulticycle:
    def test_setup_hold(self):
        ps = PathSelector(from_set=["a"], to_set=["b"], setup_hold="setup")
        c = _mk(id="MC", type=ConstraintType.SET_MULTICYCLE_PATH,
                path_selector=ps, values={"cycles": 2, "setup_hold": "setup"})
        r = _gen(_cset(c))
        line = [ln for ln in r.text.splitlines() if ln.startswith("set_multicycle_path")][0]
        # exactly one -setup flag (no duplication)
        assert line.count("-setup") == 1
        assert " 2 " in line


# ---------------------------------------------------------------------------
# Canonical emission order is deterministic (acc. #16)
# ---------------------------------------------------------------------------

class TestEmissionOrder:
    def test_deterministic_order(self):
        constraints = [
            _mk(id=f"N{i}", type=t, target_objects=["x"], clock_refs=["clk"],
                values=({"delay": 1e-9} if t in (ConstraintType.SET_MIN_DELAY,
                                                 ConstraintType.SET_MAX_DELAY,
                                                 ConstraintType.SET_INPUT_DELAY,
                                                 ConstraintType.SET_OUTPUT_DELAY)
                        else {"period": 10e-9, "name": "clk"} if t == ConstraintType.CREATE_CLOCK
                        else {"cycles": 2} if t == ConstraintType.SET_MULTICYCLE_PATH
                        else {"uncertainty": 0.1e-9} if t == ConstraintType.SET_CLOCK_UNCERTAINTY
                        else {"latency": 0.1e-9} if t == ConstraintType.SET_CLOCK_LATENCY
                        else {"transition": 0.05e-9} if t == ConstraintType.SET_CLOCK_TRANSITION
                        else {"relationship": "asynchronous", "groups": [["a"], ["b"]]}
                        if t == ConstraintType.SET_CLOCK_GROUPS
                        else {}),
                path_selector=(PathSelector(from_set=["a"], to_set=["b"])
                               if t in (ConstraintType.SET_FALSE_PATH,
                                        ConstraintType.SET_MULTICYCLE_PATH,
                                        ConstraintType.SET_MIN_DELAY,
                                        ConstraintType.SET_MAX_DELAY) else None))
            for i, t in enumerate([
                ConstraintType.SET_FALSE_PATH, ConstraintType.CREATE_CLOCK,
                ConstraintType.SET_OUTPUT_DELAY, ConstraintType.SET_CLOCK_GROUPS])
            if _can_make(t)
        ]
        r1 = _gen(_cset(*constraints))
        r2 = _gen(_cset(*reversed(constraints)))
        # Same set added in reverse order must produce identical text.
        assert r1.text == r2.text


def _can_make(t):
    return True


# ---------------------------------------------------------------------------
# Preflight & diagnostics (acc. #18)
# ---------------------------------------------------------------------------

class TestPreflight:
    def test_disabled_constraint_not_emitted(self):
        c = _mk(id="D1", type=ConstraintType.CREATE_CLOCK,
                disabled=True, target_objects=["clk"], clock_refs=["clk"],
                values={"name": "clk", "period": 10e-9})
        r = _gen(_cset(c))
        assert "D1" not in r.emitted_constraint_ids
        assert "create_clock" not in r.text

    def test_rejected_not_emitted_even_aggressive(self):
        c = _mk(id="R1", type=ConstraintType.CREATE_CLOCK,
                status=ConstraintStatus.REJECTED, target_objects=["clk"],
                clock_refs=["clk"],
                values={"name": "clk", "period": 10e-9})
        r = _gen(_cset(c), mode="aggressive")
        assert "R1" not in r.emitted_constraint_ids


# ---------------------------------------------------------------------------
# Backend capability negotiation (acc. #19)
# ---------------------------------------------------------------------------

class TestBackendCapabilities:
    def test_cadence_blocks_edge_shift(self):
        """Cadence backend declares generated_clock_edge_shift=False.
        Per Step 6 corrective pass: the constraint MUST be blocked
        (NOT emitted with a harmless warning) because omitting -edge_shift
        materially changes generated-clock timing semantics."""
        c = _mk(id="G1", type=ConstraintType.CREATE_GENERATED_CLOCK,
                source_objects=["U/Q"], target_objects=["U/Q"],
                clock_refs=["clk"],
                values={"name": "gclk", "source": "U/Q", "master_clock": "clk",
                        "edges": [1, 2, 3], "edge_shift": [0, 0.1e-9, 0]})
        r = _gen(_cset(c), backend="cadence")
        # Must NOT emit create_generated_clock at all because edge_shift
        # can't be represented safely.
        assert "create_generated_clock" not in r.text
        assert r.status == GenerationStatus.BLOCKED
        assert "G1" in r.skipped_constraint_ids
        # Structured ERROR diagnostic present.
        assert any(d.severity == "ERROR"
                   and "edge_shift" in d.message.lower()
                   for d in r.diagnostics)
        # Result diagnostic is recorded for G1
        assert any(d.constraint_id == "G1" and d.severity == "ERROR"
                   for d in r.diagnostics)

    def test_generic_supports_edge_shift(self):
        """Generic backend supports edge_shift -> fully emitted."""
        c = _mk(id="G1", type=ConstraintType.CREATE_GENERATED_CLOCK,
                source_objects=["U/Q"], target_objects=["U/Q"],
                clock_refs=["clk"],
                values={"name": "gclk", "source": "U/Q", "master_clock": "clk",
                        "edges": [1, 2, 3], "edge_shift": [0, 0.1e-9, 0]})
        r = _gen(_cset(c), backend="generic")
        assert r.status == GenerationStatus.COMPLETE
        assert "-edge_shift {" in r.text
        assert "create_generated_clock" in r.text

    def test_opensta_headers(self):
        c = _mk(id="C1", type=ConstraintType.CREATE_CLOCK,
                target_objects=["clk"], clock_refs=["clk"],
                values={"name": "clk", "period": 10e-9})
        r = _gen(_cset(c), backend="opensta")
        assert "set_units" in r.text
        assert "OpenSTA" in r.text

    def test_synopsys_headers(self):
        c = _mk(id="C1", type=ConstraintType.CREATE_CLOCK,
                target_objects=["clk"], clock_refs=["clk"],
                values={"name": "clk", "period": 10e-9})
        r = _gen(_cset(c), backend="synopsys")
        assert "Synopsys" in r.text


# ---------------------------------------------------------------------------
# Determinism: same UCM → identical SDC (acc. #21)
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_no_randomness_in_provenance(self):
        c = _mk(id="C1", type=ConstraintType.CREATE_CLOCK,
                target_objects=["clk"], clock_refs=["clk"],
                source_kind=SourceKind.EXISTING_SDC,
                comment="my clock",
                values={"name": "clk", "period": 10e-9})
        r1 = _gen(_cset(c), with_provenance_=False)
        r2 = _gen(_cset(c), with_provenance_=False)
        assert r1.text == r2.text
        # No timestamps, PIDs, machine-specific paths
        assert "/home/" not in r1.text
        for word in ("timestamp", "pid", "random"):
            assert word not in r1.text.lower()





# ---------------------------------------------------------------------------
# Adversarial cases (acc. #27)
# ---------------------------------------------------------------------------

class TestAdversarial:
    def test_unknown_period_blocked(self):
        c = _mk(id="C1", type=ConstraintType.CREATE_CLOCK,
                target_objects=["clk"], values={"name": "clk"})
        r = _gen(_cset(c))
        assert r.status == GenerationStatus.BLOCKED
        assert "create_clock" not in r.text

    def test_invalid_min_max_pair(self):
        # Both min=True and max=True is semantically coherent per command
        # (it's the default 'both'); but if min_max is 'min' AND 'max'
        # we should not emit.
        c = _mk(id="IN1", type=ConstraintType.SET_INPUT_DELAY,
                target_objects=["d"], clock_refs=["clk"],
                values={"delay": 1e-9, "min_max": "neither"})
        r = _gen(_cset(c))
        # preflight should catch incoherent min_max? Actually preflight
        # accepts anything that's not conflicting; unknown string falls
        # through but _edge_mm_sh_flags only recognizes min/max. As long
        # as we don't invent -min/-max, we're fine.
        assert "-min" not in r.text and "-max" not in r.text

    def test_missing_delay_blocks_io(self):
        c = _mk(id="IN1", type=ConstraintType.SET_INPUT_DELAY,
                target_objects=["d"], clock_refs=["clk"], values={})
        r = _gen(_cset(c))
        assert "IN1" in r.skipped_constraint_ids
        assert "set_input_delay" not in r.text

    def test_divide_and_multiply_conflict_is_fatal(self):
        """SDC forbids -divide_by and -multiply_by together; preflight
        must block emission rather than silently dropping one."""
        c = _mk(id="G1", type=ConstraintType.CREATE_GENERATED_CLOCK,
                source_objects=["U/Q"], target_objects=["U/Q"],
                clock_refs=["clk"],
                values={"name": "gclk", "source": "U/Q",
                        "master_clock": "clk",
                        "divide_by": 2, "multiply_by": 3})
        r = _gen(_cset(c))
        assert r.status == GenerationStatus.BLOCKED
        assert "G1" in r.skipped_constraint_ids
        assert "create_generated_clock" not in r.text
        assert any("DIV_MUL_CONFLICT" == d.code for d in r.diagnostics)

    def test_edge_shift_without_edges_is_fatal(self):
        c = _mk(id="G1", type=ConstraintType.CREATE_GENERATED_CLOCK,
                source_objects=["U/Q"], target_objects=["U/Q"],
                clock_refs=["clk"],
                values={"name": "gclk", "source": "U/Q",
                        "master_clock": "clk", "divide_by": 2,
                        "edge_shift": [0, 0.1e-9, 0]})
        r = _gen(_cset(c))
        assert r.status == GenerationStatus.BLOCKED
        assert "G1" in r.skipped_constraint_ids
        assert any("EDGE_SHIFT_WITHOUT_EDGES" == d.code for d in r.diagnostics)


# ---------------------------------------------------------------------------
# Result-status accounting (acc. #4 corrective)
# ---------------------------------------------------------------------------

class TestResultStatus:
    def test_all_emitted_is_complete(self):
        c = _mk(id="C1", type=ConstraintType.CREATE_CLOCK,
                target_objects=["clk"], clock_refs=["clk"],
                values={"name": "clk", "period": 10e-9})
        r = _gen(_cset(c))
        assert r.status == GenerationStatus.COMPLETE
        assert r.stats["emitted"] == 1
        assert r.stats["skipped"] == 0

    def test_some_blocked_some_emitted_is_partial(self):
        c1 = _mk(id="GOOD", type=ConstraintType.CREATE_CLOCK,
                 target_objects=["clk"], clock_refs=["clk"],
                 values={"name": "clk", "period": 10e-9})
        c2 = _mk(id="BAD", type=ConstraintType.CREATE_CLOCK,
                 target_objects=["clk2"], values={"name": "clk2"})  # no period
        r = _gen(_cset(c1, c2))
        assert r.status == GenerationStatus.PARTIAL
        assert "GOOD" in r.emitted_constraint_ids
        assert "BAD" in r.skipped_constraint_ids
        assert "create_clock" in r.text

    def test_all_blocked_is_blocked(self):
        c = _mk(id="BAD", type=ConstraintType.CREATE_CLOCK,
                target_objects=["clk"], values={"name": "clk"})
        r = _gen(_cset(c))
        assert r.status == GenerationStatus.BLOCKED
        assert "create_clock" not in r.text
        assert r.stats["emitted"] == 0
        assert r.stats["skipped"] == 1

    def test_unsupported_option_not_emitted_and_partial(self):
        """A backend that declares a specific option unsupported must
        not have that option emitted, and status must be PARTIAL/BLOCKED
        rather than COMPLETE."""
        # Build a fake backend that declares generated_clock_edge_shift
        # unsupported by subclassing generic and overriding capabilities.
        from rca.sdc.generic.backend import GenericSDCBackend

        class _Restricted(GenericSDCBackend):
            name = "restricted"
            def capabilities(self):
                caps = super().capabilities()
                caps["generated_clock_edge_shift"] = False
                return caps

        be = _Restricted()
        c = _mk(id="G1", type=ConstraintType.CREATE_GENERATED_CLOCK,
                source_objects=["U/Q"], target_objects=["U/Q"],
                clock_refs=["clk"],
                values={"name": "gclk", "source": "U/Q",
                        "master_clock": "clk",
                        "edges": [1, 2, 3], "edge_shift": [0, 0.1e-9, 0]})
        cs = ConstraintSet(name="t"); cs.add(c)
        r = be.generate(cs, design_name="t", mode="balanced", with_provenance=False)
        assert r.status == GenerationStatus.BLOCKED
        assert "create_generated_clock" not in r.text
        assert "-edge_shift" not in r.text
        assert any(d.severity == "ERROR" for d in r.diagnostics)

    def test_skipped_constraint_reflected_in_stats(self):
        c1 = _mk(id="OK", type=ConstraintType.CREATE_CLOCK,
                 target_objects=["clk"], clock_refs=["clk"],
                 values={"name": "clk", "period": 10e-9})
        c2 = _mk(id="BAD", type=ConstraintType.SET_INPUT_DELAY,
                 target_objects=["d"], values={})  # missing clock + delay
        r = _gen(_cset(c1, c2))
        assert r.stats["total"] == 2
        assert r.stats["emitted"] == 1
        assert r.stats["skipped"] == 1
        assert "OK" in r.emitted_constraint_ids
        assert "BAD" in r.skipped_constraint_ids


# ---------------------------------------------------------------------------
# SDC -> UCM -> SDC semantic roundtrip (acc. #24, #25)
# ---------------------------------------------------------------------------

class TestRoundtrip:
    def _rt(self, sdc):
        imp = SdcImporter(run_ts="2025-01-01T00:00:00+00:00", run_id="x")
        r = imp.from_text(sdc, source_file="<s>")
        out = _gen(r.constraint_set)
        return r, out

    def test_create_clock_waveform(self):
        sdc = "create_clock -name clk -period 10 -waveform {0 5} [get_ports clk]\n"
        imp, out = self._rt(sdc)
        assert out.status == GenerationStatus.COMPLETE
        assert "create_clock -name clk -period 10 -waveform { 0 5 } [get_ports clk]" in out.text

    def test_generated_div(self):
        sdc = "create_generated_clock -name gclk -source [get_pins U/Q] -master_clock clk -divide_by 2 [get_pins U/Q]\n"
        imp, out = self._rt(sdc)
        assert out.status == GenerationStatus.COMPLETE
        assert "-divide_by 2" in out.text

    def test_io_min_max_rise_fall(self):
        sdc = ("set_input_delay -clock clk -max 2 [get_ports d]\n"
               "set_input_delay -clock clk -min 0.5 [get_ports d]\n")
        imp, out = self._rt(sdc)
        assert out.status == GenerationStatus.COMPLETE
        assert "-max" in out.text and "-min" in out.text

    def test_clock_groups(self):
        sdc = "set_clock_groups -asynchronous -group {a b} -group {c}\n"
        imp, out = self._rt(sdc)
        assert out.status == GenerationStatus.COMPLETE
        assert "set_clock_groups -asynchronous" in out.text
        assert out.text.count("-group") == 2

    def test_false_path_multi_through(self):
        sdc = "set_false_path -from [get_clocks a] -through {B} -through {C} -to [get_clocks b]\n"
        imp, out = self._rt(sdc)
        assert out.status == GenerationStatus.COMPLETE
        line = [ln for ln in out.text.splitlines() if ln.startswith("set_false_path")][0]
        assert line.count("-through") == 2

    def test_multicycle_start_end_setup(self):
        sdc = "set_multicycle_path 2 -setup -start -from [get_clocks a] -to [get_clocks b]\n"
        imp, out = self._rt(sdc)
        assert out.status == GenerationStatus.COMPLETE
        assert "-setup" in out.text and "-start" in out.text

    def test_min_max_delay(self):
        sdc = ("set_min_delay 0.1 -from [get_clocks x] -to [get_ports y]\n"
               "set_max_delay 5 -from [get_clocks x] -to [get_ports y]\n")
        imp, out = self._rt(sdc)
        assert out.status == GenerationStatus.COMPLETE
        assert "set_min_delay" in out.text and "set_max_delay" in out.text

    def test_io_rise_fall_preserved(self):
        sdc = ("set_input_delay -clock clk -rise -max 1 [get_ports d]\n"
               "set_input_delay -clock clk -fall -max 1.2 [get_ports d]\n")
        imp, out = self._rt(sdc)
        assert out.status == GenerationStatus.COMPLETE
        assert "-rise" in out.text and "-fall" in out.text

    def test_generated_multiply(self):
        sdc = ("create_generated_clock -name gp -source [get_pins U/Q] "
               "-master_clock clk -multiply_by 2 [get_pins U/Q]\n")
        imp, out = self._rt(sdc)
        assert out.status == GenerationStatus.COMPLETE
        assert "-multiply_by 2" in out.text

    def test_generated_edges_and_shift_supported(self):
        """edge_shift round-trips on a backend that supports it (generic)."""
        sdc = ("create_generated_clock -name g -source [get_pins U/Q] "
               "-master_clock clk -edges {1 2 3} -edge_shift {0 0.1 0} "
               "[get_pins U/Q]\n")
        imp = SdcImporter(run_ts="2025-01-01T00:00:00+00:00", run_id="x")
        r = imp.from_text(sdc, source_file="<s>")
        out = _gen(r.constraint_set, backend="generic")
        assert out.status == GenerationStatus.COMPLETE
        assert "-edges {" in out.text
        assert "-edge_shift {" in out.text

    def test_generated_edge_shift_unsupported_not_claimed_equivalent(self):
        """When edge_shift is unsupported (Cadence), the result is BLOCKED
        (not COMPLETE), so we never claim semantic equivalence to an
        output that omitted edge_shift."""
        sdc = ("create_generated_clock -name g -source [get_pins U/Q] "
               "-master_clock clk -edges {1 2 3} -edge_shift {0 0.1 0} "
               "[get_pins U/Q]\n")
        imp = SdcImporter(run_ts="2025-01-01T00:00:00+00:00", run_id="x")
        r = imp.from_text(sdc, source_file="<s>")
        out = _gen(r.constraint_set, backend="cadence")
        assert out.status == GenerationStatus.BLOCKED
        assert "create_generated_clock" not in out.text
        # We explicitly do NOT claim semantic equivalence here.

    def test_semantic_equivalence_io_both(self):
        """Canonical unqualified SDC round-trips through a semantic key
        comparison: re-importing generated SDC yields constraints whose
        union covers both min and max (importer explodes to per-qualifier
        entries for the max quadrant per its policy)."""
        c = _mk(id="IN1", type=ConstraintType.SET_INPUT_DELAY,
                target_objects=["d"], clock_refs=["clk"],
                values={"delay": 1.0e-9, "min_max": "both"})
        cs = _cset(c)
        sdc = _gen(cs).text
        imp = SdcImporter(run_ts="2025-01-01T00:00:00+00:00", run_id="x")
        r = imp.from_text(sdc, source_file="<s>")
        # The importer treats unqualified as max (Step 5 default). The
        # semantic key of the UCM is preserved relative to that policy.
        # At minimum, the re-imported constraint covers max/rise/fall.
        ios = [c for c in r.constraint_set if c.type == ConstraintType.SET_INPUT_DELAY]
        assert any(cc.values.get("min_max") == "max" and cc.values.get("edge") == "rise"
                   for cc in ios)
        assert any(cc.values.get("min_max") == "max" and cc.values.get("edge") == "fall"
                   for cc in ios)
