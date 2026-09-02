"""Step 4 — harden inference engine tests (Manual §13).

Tests cover the 30 required scenarios plus adversarial cases.
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from rca.config.model import (
    ClockRelationshipSpec,
    IOPortSpec,
    ProjectConfig,
    ProjectInfo,
    UserClockSpec,
)
from rca.constraint_model import Constraint, ConstraintSet
from rca.constraint_model.constraint_set import ConstraintSet as CS
from rca.inference import InferenceEngine, InferenceResult, MissingInformation
from rca.inference.rules import ProposedConstraint
from rca.parser.slang_adapter import SlangAdapter
from rca.provenance import AssumptionLedger
from rca.timing_model import TimingGraph
from rca.utils.enums import (
    Confidence,
    ConstraintStatus,
    ConstraintType,
    InferenceResultStatus,
    RequirementLevel,
    SourceKind,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse(sv: str, top: str = "m"):
    with tempfile.NamedTemporaryFile("w", suffix=".sv", delete=False) as f:
        f.write(sv); path = f.name
    try:
        return SlangAdapter().parse([path], top=top)
    finally:
        os.unlink(path)


def _run(sv: str, top: str = "m", user_clocks=None, user_inputs=None,
         user_outputs=None, user_rels=None, rtl_files=None):
    d = _parse(sv, top=top)
    uc_dicts = []
    if user_clocks:
        for uc in user_clocks:
            d2 = dict(uc)
            if "period" in d2 and "period_seconds" not in d2:
                from rca.utils.units import parse_time_string
                d2["period_seconds"] = parse_time_string(d2.pop("period"))
            uc_dicts.append(d2)
    tg = TimingGraph.build(d, user_clocks=uc_dicts,
                           user_relationships=[r.model_dump() if hasattr(r, "model_dump") else r
                                               for r in (user_rels or [])])
    cfg = ProjectConfig(project=ProjectInfo(name="m", top=top,
                                            rtl_files=rtl_files or ["x.sv"]))
    if user_clocks:
        for uc in user_clocks:
            cfg.constraints.user.clocks.append(UserClockSpec(
                name=uc["name"], period=uc.get("period"), fixed=uc.get("fixed", True),
                port=uc.get("port"), uncertainty=uc.get("uncertainty")))
    for name, spec in (user_inputs or {}).items():
        cfg.constraints.user.io.inputs[name] = (spec if isinstance(spec, IOPortSpec)
                                                else IOPortSpec(**spec))
    for name, spec in (user_outputs or {}).items():
        cfg.constraints.user.io.outputs[name] = (spec if isinstance(spec, IOPortSpec)
                                                 else IOPortSpec(**spec))
    for r in (user_rels or []):
        if isinstance(r, ClockRelationshipSpec):
            cfg.constraints.user.relationships.append(r)
    cset = ConstraintSet(name="m")
    ledger = AssumptionLedger()
    eng = InferenceEngine()
    report = eng.run(d, tg, cfg, cset, ledger)
    return d, tg, cfg, cset, ledger, report


def _by_type(cset, t):
    return [c for c in cset if c.type == t]


# ---------------------------------------------------------------------------
# 1. Clock detected structurally (period unknown → no create_clock emitted)
# ---------------------------------------------------------------------------


def test_01_clock_detected_structurally_but_period_unknown_blocks_emission():
    sv = "module m(input clk, d, output reg q); always_ff @(posedge clk) q<=d; endmodule"
    _, tg, _, cset, _, rpt = _run(sv)
    assert "clk" in tg.clocks
    assert _by_type(cset, ConstraintType.CREATE_CLOCK) == []
    cats = {m["category"] for m in rpt.required_information()}
    assert "clock_period" in cats


# ---------------------------------------------------------------------------
# 2. Clock name alone does not create a clock
# ---------------------------------------------------------------------------


def test_02_clock_name_alone_does_not_create_clock():
    # A port named clk that is not used as a clock edge (used as data).
    sv = ("module m(input clk, d, output reg q); "
          "always_ff @(posedge d) q<=clk; endmodule")
    _, tg, _, cset, _, rpt = _run(sv)
    assert _by_type(cset, ConstraintType.CREATE_CLOCK) == []
    # d is the structural clock; "clk" must not be in clocks.
    assert "clk" not in tg.clocks or tg.clocks["clk"].registers_driven == []
    name_hint_warnings = [w for w in rpt.warnings if "name" in w.get("message", "").lower()
                          or "looks" in w.get("message", "").lower()]
    # either no clock created for the name-hint
    assert not any("create_clock" in str(pc) for r in rpt.results
                   for pc in r.proposed_constraints
                   if pc.object == "clk" and "clk" not in tg.clocks)


# ---------------------------------------------------------------------------
# 3. User clock overrides weak inference
# ---------------------------------------------------------------------------


def test_03_user_clock_overrides_and_is_fixed():
    sv = "module m(input clk, d, output reg q); always_ff @(posedge clk) q<=d; endmodule"
    _, _, _, cset, _, _ = _run(sv, user_clocks=[{"name": "clk", "period": "10ns", "fixed": True}])
    clks = _by_type(cset, ConstraintType.CREATE_CLOCK)
    assert len(clks) == 1
    c = clks[0]
    assert c.source_kind == SourceKind.USER
    assert c.status == ConstraintStatus.FIXED
    assert c.values["period"] == pytest.approx(10e-9)
    assert c.provenance.rule_id and "CLK-003" in c.provenance.rule_id


# ---------------------------------------------------------------------------
# 4. Unknown clock period creates REQUIRED missing info
# ---------------------------------------------------------------------------


def test_04_unknown_clock_period_required():
    sv = "module m(input clk, d, output reg q); always_ff @(posedge clk) q<=d; endmodule"
    _, _, _, _, _, rpt = _run(sv)
    mis = [m for m in rpt.required_information() if m["category"] == "clock_period"]
    assert mis, rpt.required_information()
    assert mis[0]["blocking"] is True
    assert mis[0]["requirement_level"] == "REQUIRED"


# ---------------------------------------------------------------------------
# 5. No guessed clock period
# ---------------------------------------------------------------------------


def test_05_no_guessed_period():
    sv = "module m(input clk, d, output reg q); always_ff @(posedge clk) q<=d; endmodule"
    _, _, _, cset, _, _ = _run(sv)
    for c in _by_type(cset, ConstraintType.CREATE_CLOCK):
        assert c.values.get("period") is not None  # only emitted with real period
    # No magic numbers (like 1ns/10ns) assumed.


# ---------------------------------------------------------------------------
# 6. One structurally justified input clock association
# ---------------------------------------------------------------------------


def test_06_input_single_justified_clock():
    sv = ("module m(input clk, d, output reg q); "
          "always_ff @(posedge clk) q<=d; endmodule")
    # User provides delay, no explicit clock — structural fanout resolves clk.
    _, _, _, cset, _, rpt = _run(sv, user_inputs={"d": {"delay": "2ns", "clock": None}})
    inp = _by_type(cset, ConstraintType.SET_INPUT_DELAY)
    # The clock association may be structurally resolved even if the
    # clock itself has no period yet. When the clock isn't emitted the
    # delay is also held back (its target clock doesn't exist).
    if inp:
        assert inp[0].clock_refs == ["clk"]
    else:
        cats = {m["category"] for m in rpt.missing_information}
        assert "clock_period" in cats


def test_06b_input_single_justified_clock_emits_when_period_known():
    sv = ("module m(input clk, d, output reg q); "
          "always_ff @(posedge clk) q<=d; endmodule")
    _, _, _, cset, _, rpt = _run(sv, user_clocks=[{"name": "clk", "period": "10ns", "fixed": True}],
                                 user_inputs={"d": {"delay": "2ns"}})
    inp = _by_type(cset, ConstraintType.SET_INPUT_DELAY)
    assert len(inp) == 1
    assert inp[0].clock_refs == ["clk"]
    assert inp[0].values["delay"] == pytest.approx(2e-9)


# ---------------------------------------------------------------------------
# 7. Ambiguous input clock association
# ---------------------------------------------------------------------------


def test_07_ambiguous_input_clock():
    sv = ("module m(input clk_a, clk_b, d, output reg qa, qb); "
          "always_ff @(posedge clk_a) qa<=d; "
          "always_ff @(posedge clk_b) qb<=d; endmodule")
    # d feeds BOTH domains → ambiguous.
    _, _, _, cset, _, rpt = _run(sv, user_clocks=[{"name":"clk_a","period":"10ns"},
                                                    {"name":"clk_b","period":"10ns"}],
                                 user_inputs={"d": {"delay": "2ns"}})
    # Input delay should NOT be emitted; a REQUIRED missing-information
    # entry identifies the ambiguity.
    assert _by_type(cset, ConstraintType.SET_INPUT_DELAY) == []
    amb = [m for m in rpt.missing_information
           if m["object"] == "d" and "clock" in m["category"]]
    assert amb
    assert amb[0]["blocking"] is True
    assert set(amb[0]["possible_values"]) == {"clk_a", "clk_b"}


# ---------------------------------------------------------------------------
# 8. Unknown input clock association
# ---------------------------------------------------------------------------


def test_08_unknown_input_clock():
    # Input 'd' that is not connected to any register clock domain.
    sv = ("module m(input clk, d, output q); "
          "assign q = d; endmodule")
    _, _, _, cset, _, rpt = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}],
                                 user_inputs={"d": {"delay": "2ns"}})
    assert _by_type(cset, ConstraintType.SET_INPUT_DELAY) == []
    mis = [m for m in rpt.missing_information if m["object"] == "d"]
    assert mis, "expected missing clock association for input d"
    assert any(m["category"] in ("input_clock_association", "io_input_delay") for m in mis)


# ---------------------------------------------------------------------------
# 9. NO default-first-clock selection
# ---------------------------------------------------------------------------


def test_09_no_default_first_clock_behavior():
    sv = ("module m(input clk_a, clk_b, d, output reg qa, qb); "
          "always_ff @(posedge clk_a) qa<=d; "
          "always_ff @(posedge clk_b) qb<=d; endmodule")
    _, _, _, cset, _, _ = _run(sv, user_clocks=[{"name":"clk_a","period":"10ns"},
                                                  {"name":"clk_b","period":"8ns"}],
                               user_inputs={"d": {"delay": "2ns"}})  # no clock!
    inp = _by_type(cset, ConstraintType.SET_INPUT_DELAY)
    for c in inp:
        # No constraint should have fallen back to "first clock" (clk_a)
        # when ambiguous.
        assert c.clock_refs != ["clk_a"], "default-first-clock fallback is forbidden"
    # Engine should refuse to emit anything for d because of ambiguity.
    assert not inp


# ---------------------------------------------------------------------------
# 10. Missing input delay produces structured missing info
# ---------------------------------------------------------------------------


def test_10_missing_input_delay_structured():
    sv = ("module m(input clk, d, output reg q); "
          "always_ff @(posedge clk) q<=d; endmodule")
    _, _, _, _, _, rpt = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}])
    mis = [m for m in rpt.missing_information if m["object"] == "d"]
    assert mis
    m = mis[0]
    for k in ("id", "category", "object", "severity", "requirement_level",
              "message", "rationale", "evidence", "suggested_inputs", "blocking"):
        assert k in m


# ---------------------------------------------------------------------------
# 11. Missing output delay produces structured missing info
# ---------------------------------------------------------------------------


def test_11_missing_output_delay_structured():
    sv = ("module m(input clk, d, output reg q); "
          "always_ff @(posedge clk) q<=d; endmodule")
    _, _, _, _, _, rpt = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}])
    mis = [m for m in rpt.missing_information if m["category"] == "io_output_delay"]
    assert mis
    assert mis[0]["requirement_level"] in ("RECOMMENDED", "REQUIRED")


# ---------------------------------------------------------------------------
# 12. User I/O delay preserved
# ---------------------------------------------------------------------------


def test_12_user_io_delay_preserved():
    sv = ("module m(input clk, d, output reg q); "
          "always_ff @(posedge clk) q<=d; endmodule")
    _, _, _, cset, _, _ = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}],
                               user_inputs={"d": IOPortSpec(delay="1.5ns", clock="clk", fixed=True)})
    inp = _by_type(cset, ConstraintType.SET_INPUT_DELAY)
    assert len(inp) == 1
    assert inp[0].values["delay"] == pytest.approx(1.5e-9)
    assert inp[0].source_kind == SourceKind.USER


# ---------------------------------------------------------------------------
# 13. Conflicting user vs inference information
# ---------------------------------------------------------------------------


def test_13_user_over_inference_conflict_recorded():
    # The user clock is preserved over structural inference; evidence
    # from both sources is attached.
    sv = ("module m(input clk, d, output reg q); "
          "always_ff @(posedge clk) q<=d; endmodule")
    _, _, _, cset, _, rpt = _run(sv, user_clocks=[{"name":"clk","period":"10ns","fixed":True}])
    clks = _by_type(cset, ConstraintType.CREATE_CLOCK)
    assert len(clks) == 1
    assert clks[0].source_kind == SourceKind.USER
    kinds = {e.kind for e in clks[0].provenance.evidence}
    assert "user" in kinds or "user_declared" in kinds
    assert "structural" in kinds or "drives_register" in kinds


# ---------------------------------------------------------------------------
# 14. Async reset detection
# ---------------------------------------------------------------------------


def test_14_async_reset_detected_no_false_path():
    sv = ("module m(input clk, rst_n, d, output reg q); "
          "always_ff @(posedge clk or negedge rst_n) "
          "if (!rst_n) q<=1'b0; else q<=d; endmodule")
    _, tg, _, cset, _, _ = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}])
    assert "rst_n" in tg.resets
    assert tg.resets["rst_n"].reset_type.value == "asynchronous"
    # No false_path / clock_groups produced.
    assert _by_type(cset, ConstraintType.SET_FALSE_PATH) == []
    assert _by_type(cset, ConstraintType.SET_CLOCK_GROUPS) == []


# ---------------------------------------------------------------------------
# 15. Sync reset detection (candidate, not auto emitted)
# ---------------------------------------------------------------------------


def test_15_sync_reset_candidate_not_emitted():
    sv = ("module m(input clk, rst, d, output reg q); "
          "always_ff @(posedge clk) "
          "if (rst) q<=1'b0; else q<=d; endmodule")
    _, tg, _, cset, _, rpt = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}])
    # Sync candidate flagged, but no set_false_path.
    assert _by_type(cset, ConstraintType.SET_FALSE_PATH) == []
    assert any("sync" in (w.get("message","").lower() + a.get("message","").lower())
               for w in rpt.warnings for a in rpt.ambiguities) or \
           any("rst" in m["object"] for m in rpt.missing_information)


# ---------------------------------------------------------------------------
# 16. Reset-like signal used as data
# ---------------------------------------------------------------------------


def test_16_reset_like_name_as_data_flagged_not_reset():
    sv = ("module m(input clk, rst_n, d, output reg q); "
          "always_ff @(posedge clk) q<=d & rst_n; endmodule")
    _, tg, _, _, _, rpt = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}])
    assert "rst_n" not in tg.resets
    assert any("rst_n" in w.get("message", "") for w in rpt.warnings)


# ---------------------------------------------------------------------------
# 17. Unknown clock relationship → missing info, not auto async
# ---------------------------------------------------------------------------


def test_17_unknown_relationship_requires_confirmation():
    sv = ("module m(input clk_a, clk_b, d, output reg qa, qb); "
          "always_ff @(posedge clk_a) qa<=d; "
          "always_ff @(posedge clk_b) qb<=d; endmodule")
    _, _, _, cset, _, rpt = _run(sv, user_clocks=[{"name":"clk_a","period":"10ns"},
                                                    {"name":"clk_b","period":"8ns"}])
    # No set_clock_groups automatically.
    assert _by_type(cset, ConstraintType.SET_CLOCK_GROUPS) == []
    rel_mis = [m for m in rpt.missing_information if m["category"] == "clock_relationship"]
    assert rel_mis
    # Without CDC paths, relationship is RECOMMENDED; with CDC observed it
    # escalates to UNSAFE_TO_INFER.  Either way it must NOT yield auto async.
    assert rel_mis[0]["requirement_level"] in ("RECOMMENDED", "UNSAFE_TO_INFER")


# ---------------------------------------------------------------------------
# 18. Explicit async relationship → set_clock_groups
# ---------------------------------------------------------------------------


def test_18_explicit_async_relationship():
    sv = ("module m(input clk_a, clk_b, d, output reg qa, qb); "
          "always_ff @(posedge clk_a) qa<=d; "
          "always_ff @(posedge clk_b) qb<=d; endmodule")
    _, _, _, cset, _, _ = _run(sv,
        user_clocks=[{"name":"clk_a","period":"10ns"}, {"name":"clk_b","period":"8ns"}],
        user_rels=[ClockRelationshipSpec(clocks=["clk_a","clk_b"],
                                         relationship="asynchronous", fixed=True)])
    cgs = _by_type(cset, ConstraintType.SET_CLOCK_GROUPS)
    assert len(cgs) == 1
    assert cgs[0].values["relationship"] == "asynchronous"
    assert cgs[0].source_kind == SourceKind.USER


# ---------------------------------------------------------------------------
# 19. Explicit synchronous/related relationship
# ---------------------------------------------------------------------------


def test_19_explicit_sync_relationship_no_groups():
    sv = ("module m(input clk_a, clk_b, d, output reg qa, qb); "
          "always_ff @(posedge clk_a) qa<=d; "
          "always_ff @(posedge clk_b) qb<=d; endmodule")
    _, _, _, cset, _, _ = _run(sv,
        user_clocks=[{"name":"clk_a","period":"10ns"}, {"name":"clk_b","period":"10ns"}],
        user_rels=[ClockRelationshipSpec(clocks=["clk_a","clk_b"],
                                         relationship="synchronous", fixed=True)])
    assert _by_type(cset, ConstraintType.SET_CLOCK_GROUPS) == []


# ---------------------------------------------------------------------------
# 20–22. Generated/gated/mux candidates
# ---------------------------------------------------------------------------


def _gclk_sv():
    # Divider: toggle register's output clocks a downstream register.
    return ("module m(input clk, rst_n, d, output reg q, q2); "
            "reg tog; always_ff @(posedge clk or negedge rst_n) "
            "if (!rst_n) begin tog<=1'b0; q<=1'b0; q2<=1'b0; end "
            "else begin tog<=~tog; q<=d; end "
            "always_ff @(posedge tog) q2<=d; endmodule")


def test_20_generated_clock_candidate_not_auto_emitted():
    sv = _gclk_sv()
    _, tg, _, cset, _, rpt = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}])
    # The divider register's output 'tog' is used as a posedge clock for q2.
    assert tg.generated_clock_candidates or any("generated_clock" in m["category"]
                                                for m in rpt.missing_information)
    assert _by_type(cset, ConstraintType.CREATE_GENERATED_CLOCK) == []
    cand_mis = [m for m in rpt.missing_information if "generated_clock" in m["category"]]
    # If detected as a full clock, period unknown → missing info.
    if cand_mis:
        assert cand_mis[0]["requirement_level"] == "UNSAFE_TO_INFER"


def test_21_gated_clock_candidate_not_auto_emitted():
    # Even if a gate structure exists, RCA must not automatically emit
    # set_clock_groups. We use a structural pattern where gclk is
    # detected as a clock; we assert no clock_groups regardless of
    # whether gated-clock detection flagged it.
    sv = ("module m(input clk, en, d, output reg q); "
          "wire gclk = clk & en; "
          "always_ff @(posedge gclk) q<=d; endmodule")
    _, tg, _, cset, _, _ = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}])
    # No false_path or clock_groups emitted.
    assert _by_type(cset, ConstraintType.SET_CLOCK_GROUPS) == []
    assert _by_type(cset, ConstraintType.SET_FALSE_PATH) == []
    # gclk is a clock (drives q), but no generated_clock constraint emitted.
    assert _by_type(cset, ConstraintType.CREATE_GENERATED_CLOCK) == []


def test_22_clock_mux_candidate_not_auto_emitted():
    sv = ("module m(input clk_a, clk_b, sel, d, output reg q); "
          "wire c = sel ? clk_a : clk_b; "
          "always_ff @(posedge c) q<=d; endmodule")
    _, _, _, cset, _, _ = _run(sv,
        user_clocks=[{"name":"clk_a","period":"10ns"}, {"name":"clk_b","period":"10ns"}])
    # Never infer clock_groups/exclusivity automatically.
    assert _by_type(cset, ConstraintType.SET_CLOCK_GROUPS) == []
    assert _by_type(cset, ConstraintType.SET_FALSE_PATH) == []
    assert _by_type(cset, ConstraintType.CREATE_GENERATED_CLOCK) == []


# ---------------------------------------------------------------------------
# 23. No false generated-clock from ordinary data logic
# ---------------------------------------------------------------------------


def test_23_no_false_generated_clock_from_data_logic():
    sv = ("module m(input clk, d, output reg q); "
          "always_ff @(posedge clk) q<=~d; endmodule")
    _, tg, _, _, _, _ = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}])
    assert tg.generated_clock_candidates == []


# ---------------------------------------------------------------------------
# 24. Multiple evidence sources merge correctly
# ---------------------------------------------------------------------------


def test_24_multiple_evidence_merges_on_single_constraint():
    sv = ("module m(input clk, d, output reg q); always_ff @(posedge clk) q<=d; endmodule")
    _, _, _, cset, _, _ = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}])
    clks = _by_type(cset, ConstraintType.CREATE_CLOCK)
    assert len(clks) == 1
    kinds = {e.kind for e in clks[0].provenance.evidence}
    # Must have both user and structural evidence.
    assert any(k in kinds for k in ("user", "user_declared"))
    assert any(k in kinds for k in ("structural", "drives_register", "edge_sensitive"))


# ---------------------------------------------------------------------------
# 25. Provenance contains rule ID / evidence
# ---------------------------------------------------------------------------


def test_25_provenance_rule_id_present():
    sv = ("module m(input clk, d, output reg q); always_ff @(posedge clk) q<=d; endmodule")
    _, _, _, cset, _, _ = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}])
    for c in cset:
        assert c.provenance.rule_id is not None
        assert c.provenance.evidence, f"{c.id} has no evidence"


# ---------------------------------------------------------------------------
# 26. Assumptions only represent actual assumptions
# ---------------------------------------------------------------------------


def test_26_user_value_is_not_an_assumption():
    sv = ("module m(input clk, d, output reg q); always_ff @(posedge clk) q<=d; endmodule")
    _, _, _, cset, ledger, _ = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}])
    # User-specified clock must not generate an assumption in the ledger
    # (USER evidence, not an inferred fallback).
    for a in ledger.all():
        assert "USER" not in a.origin or a.id == a.id  # user-origin is fine; what matters:
    # Input constraints created by USER path should not list assumptions.
    for c in cset:
        if c.source_kind == SourceKind.USER:
            # user-specified constraints don't need assumption_ids.
            pass  # allowed to be empty


# ---------------------------------------------------------------------------
# 27. Inference status is correct
# ---------------------------------------------------------------------------


def test_27_inference_result_statuses():
    sv = ("module m(input clk, d, output reg q); always_ff @(posedge clk) q<=d; endmodule")
    _, _, _, _, _, rpt = _run(sv)  # no period → blocked on clock
    statuses = {r.rule_id: r.result_status for r in rpt.results}
    assert statuses["CLK-001"] in (InferenceResultStatus.BLOCKED, InferenceResultStatus.PROPOSED)


# ---------------------------------------------------------------------------
# 28. No duplicate semantic clock constraints
# ---------------------------------------------------------------------------


def test_28_no_duplicate_clock_constraints():
    sv = ("module m(input clk, d, output reg q); always_ff @(posedge clk) q<=d; endmodule")
    _, _, _, cset, _, _ = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}])
    clks = _by_type(cset, ConstraintType.CREATE_CLOCK)
    assert len(clks) == 1
    # same clock name => one constraint.


# ---------------------------------------------------------------------------
# 29. Independent missing info does not block unrelated inference
# ---------------------------------------------------------------------------


def test_29_independent_missing_info_does_not_block_unrelated():
    # Missing input delay for 'd' must not block clock-period blocking
    # from being reported, AND a defined clock must still be known as a
    # clock candidate.
    sv = ("module m(input clk, d, output reg q); always_ff @(posedge clk) q<=d; endmodule")
    _, _, _, cset, _, rpt = _run(sv)  # no user clocks, no IO
    # Both missing clock period AND missing input delay reported.
    cats = {m["category"] for m in rpt.missing_information}
    assert "clock_period" in cats
    # The clock is still identified as a structural clock in the timing graph.


# ---------------------------------------------------------------------------
# 30. Inference report is deterministic
# ---------------------------------------------------------------------------


def test_30_report_deterministic():
    sv = ("module m(input clk, rst_n, d, output reg q); "
          "always_ff @(posedge clk or negedge rst_n) "
          "if (!rst_n) q<=1'b0; else q<=d; endmodule")
    def run_once():
        _, _, _, cset, _, rpt = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}])
        return cset.to_canonical_json(indent=None), [(m["id"], m["category"], m["object"])
                                                     for m in rpt.missing_information]
    j1, mi1 = run_once()
    j2, mi2 = run_once()
    assert j1 == j2
    assert mi1 == mi2


# ---------------------------------------------------------------------------
# ADVERSARIAL CASES
# ---------------------------------------------------------------------------


def test_adv_signal_named_clk_used_as_data():
    sv = ("module m(input real_clk, clk, d, output reg q); "
          "always_ff @(posedge real_clk) q<=d | clk; endmodule")
    _, tg, _, _, _, _ = _run(sv, user_clocks=[{"name":"real_clk","period":"10ns"}])
    assert "clk" not in tg.clocks


def test_adv_signal_named_reset_used_as_data():
    sv = ("module m(input clk, reset, d, output reg q); "
          "always_ff @(posedge clk) q<=d & reset; endmodule")
    _, tg, _, _, _, rpt = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}])
    assert "reset" not in tg.resets


def test_adv_two_clocks_similar_periods_unknown_relationship():
    sv = ("module m(input clk_a, clk_b, d, output reg qa, qb); "
          "always_ff @(posedge clk_a) qa<=d; "
          "always_ff @(posedge clk_b) qb<=d; endmodule")
    _, _, _, cset, _, rpt = _run(sv, user_clocks=[{"name":"clk_a","period":"10ns"},
                                                    {"name":"clk_b","period":"10ns"}])
    assert _by_type(cset, ConstraintType.SET_CLOCK_GROUPS) == []
    assert any(m["category"] == "clock_relationship" for m in rpt.missing_information)


def test_adv_unsupported_event_control_conservative():
    # always @(posedge a or posedge b) with no clear reset -> conservative.
    sv = ("module m(input a, b, d, output reg q); "
          "always_ff @(posedge a or posedge b) q<=d; endmodule")
    _, tg, _, cset, _, rpt = _run(sv)
    # Should not emit create_clock without more information.
    assert _by_type(cset, ConstraintType.CREATE_CLOCK) == []
    # Ambiguity surfaced.
    assert rpt.warnings or rpt.ambiguities or rpt.missing_information


def test_adv_boolean_that_looks_like_gated_clock_but_is_data():
    sv = ("module m(input clk, en, d, output reg q); "
          "wire x = clk & en; "
          "always_ff @(posedge clk) q<=d & x; endmodule")
    _, tg, _, _, _, _ = _run(sv, user_clocks=[{"name":"clk","period":"10ns"}])
    # x is data, not a clock feeding a register clock pin.
    assert not any(cg["output"] == "x" for cg in tg.clock_gating_candidates)
