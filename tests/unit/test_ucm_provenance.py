"""Unit tests for Step 3 UCM / Provenance / Assumption hardening.

Scenarios (numbered per Step 3 item 17):

 1.  Constraint lifecycle/status transitions
 2.  Confidence is independent from status
 3.  Optimization mutability (FIXED/TUNABLE/DERIVED)
 4.  Provenance creation with structured fields
 5.  Multiple evidence records (append-only, dedup by semantic key)
 6.  Assumption ledger registration + queries
 7.  Reverse dependency lookup (constraint/analysis)
 8.  Scenario attachment and per-scenario applicability
 9.  Constraint cloning/isolation (no aliasing)
10.  Deterministic serialization
11.  Semantic identity independent of formatting/units
12.  Invalid references detected by validate()
13.  FIXED constraint immutability (cannot mutate values, cannot reject)
14.  Rejected/deprecated filtering (is_safe_to_emit False)
15.  Dependency invalidation / stale_set transitive closure
16.  Imported-SDC provenance (ImportMetadata preserved)
17.  Missing-information representation via AssumptionLedger
18.  Source-kind correctness (enum normalization)
19.  Path selector semantics (from/through/to/edge/minmax/setuphold)
20.  Duplicate semantic constraint detection
21. (negative) Setting TUNABLE on a FIXED constraint is invalid
22. (negative) Cloning does not mutate baseline
"""

from __future__ import annotations

import copy
import json
import sys
import os
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from rca.constraint_model import (
    Constraint,
    ConstraintSet,
    PathSelector,
    Scenario,
    SnapshotFormatError,
)
from rca.constraint_model.constraint_set import ValidationIssue
from rca.provenance import Assumption, AssumptionLedger, Evidence, ImportMetadata, ProvenanceRecord, make_provenance
from rca.utils.enums import (
    Confidence,
    ConstraintStatus,
    ConstraintType,
    OptimizationStatus,
    SafeMode,
    SourceKind,
)


# ---------------------------------------------------------------------------
# 1. Lifecycle
# ---------------------------------------------------------------------------


def test_01_lifecycle_status_transitions():
    c = Constraint(id="CLK0001", type=ConstraintType.CREATE_CLOCK,
                   target_objects=["clk"], values={"name": "clk", "period": 10e-9})
    assert c.status == ConstraintStatus.PROPOSED
    assert c.is_safe_to_emit("balanced")  # PROPOSED emits in balanced
    c.confirm(Confidence.HIGH)
    assert c.status == ConstraintStatus.CONFIRMED
    assert c.confidence == Confidence.HIGH
    c.fix()
    assert c.status == ConstraintStatus.FIXED
    assert c.opt_status == OptimizationStatus.FIXED
    assert c.is_fixed()
    # Rejecting a fixed constraint raises.
    with pytest.raises(ValueError):
        c.mark_rejected("intent changed")


def test_01_rejected_not_emittable():
    c = Constraint(id="FP0001", type=ConstraintType.SET_FALSE_PATH,
                   confidence=Confidence.HIGH, status=ConstraintStatus.PROPOSED)
    assert c.is_safe_to_emit("exploratory")
    c.mark_rejected("just a test")
    assert c.status == ConstraintStatus.REJECTED
    assert not c.is_safe_to_emit("exploratory")
    assert not c.is_safe_to_emit("balanced")
    assert not c.is_safe_to_emit("strict")


# ---------------------------------------------------------------------------
# 2. Confidence independent of status
# ---------------------------------------------------------------------------


def test_02_high_confidence_proposed_is_legal():
    c = Constraint(id="X1", type=ConstraintType.CREATE_CLOCK,
                   confidence=Confidence.HIGH, status=ConstraintStatus.PROPOSED)
    assert c.confidence == Confidence.HIGH
    assert c.status == ConstraintStatus.PROPOSED


def test_02_low_confidence_confirmed_is_representable():
    """User may explicitly confirm a weakly-evidenced constraint."""
    c = Constraint(id="X2", type=ConstraintType.SET_FALSE_PATH,
                   confidence=Confidence.LOW, status=ConstraintStatus.CONFIRMED,
                   opt_status=OptimizationStatus.FIXED)
    # Validate should NOT complain about LOW+CONFIRMED+FIXED:
    assert not any("FIXED" in p and "TUNABLE" in p for p in c.validate_invariants())


# ---------------------------------------------------------------------------
# 3. Mutability
# ---------------------------------------------------------------------------


def test_03_fixed_immutable():
    c = Constraint(id="X3", type=ConstraintType.CREATE_CLOCK,
                   status=ConstraintStatus.FIXED, opt_status=OptimizationStatus.FIXED,
                   values={"period": 10e-9})
    assert c.is_fixed()
    with pytest.raises(ValueError):
        c.add_value("period", 8e-9)


def test_03_tunable_optimizer_can_modify():
    c = Constraint(id="X4", type=ConstraintType.SET_INPUT_DELAY,
                   opt_status=OptimizationStatus.TUNABLE,
                   values={"delay": 1.0e-9})
    assert not c.is_fixed()
    c.add_value("delay", 1.2e-9)  # must not raise


# ---------------------------------------------------------------------------
# 4. Provenance creation
# ---------------------------------------------------------------------------


def test_04_provenance_structured_fields():
    prov = ProvenanceRecord(
        source_kind=SourceKind.INFERENCE,
        rule_id="CLK-001",
        confidence="HIGH",
        created_by="rca",
        explanation="clock detected from posedge usage",
    )
    d = prov.to_dict()
    assert d["rule_id"] == "CLK-001"
    assert d["source_kind"] == "INFERENCE"
    assert d["created_by"] == "rca"
    assert "evidence" in d and "assumption_ids" in d


def test_04_make_provenance_helper():
    p = make_provenance(SourceKind.USER, rule_id="U1", explanation="user")
    assert p.source_kind == SourceKind.USER
    assert p.rule_id == "U1"


# ---------------------------------------------------------------------------
# 5. Multiple evidence records, dedup, append-only
# ---------------------------------------------------------------------------


def test_05_multiple_evidence_dedup():
    c = Constraint(id="X5", type=ConstraintType.CREATE_CLOCK,
                   target_objects=["clk"], values={"name": "clk", "period": 10e-9})
    e1 = Evidence(id="E1", kind="structural", description="posedge in always_ff",
                  source_objects=["m.clk"])
    e2 = Evidence(id="E2", kind="structural", description="posedge in always_ff",
                  source_objects=["m.clk"])  # semantically equivalent
    e3 = Evidence(id="E3", kind="user", description="user declared clock")
    c.provenance.add_evidence(e1)
    c.provenance.add_evidence(e2)  # dedup
    c.provenance.add_evidence(e3)
    assert len(c.provenance.evidence) == 2
    kinds = {e.kind for e in c.provenance.evidence}
    assert kinds == {"structural", "user"}


# ---------------------------------------------------------------------------
# 6. Assumption ledger
# ---------------------------------------------------------------------------


def test_06_assumption_ledger_register_and_query():
    ledger = AssumptionLedger()
    ledger.reset_id_counter(0)
    a = ledger.make(
        "Clock 'clk' period is 10 ns",
        origin="USER", confidence="HIGH", severity="REQUIRED",
        fixed=True, default_value=10e-9, current_value=10e-9,
    )
    assert a.id == "A0001"
    assert ledger.get(a.id) is a
    assert len(ledger) == 1
    assert a in ledger
    # summary
    s = a.summary()
    assert s["id"] == "A0001"
    assert s["current_value"] == 10e-9


def test_06_assumption_confirm_updates_value():
    a = Assumption(id="AX", statement="x", origin="INFERENCE",
                   default_value=None, current_value=None)
    assert not a.user_confirmed
    a.confirm(8e-9)
    assert a.user_confirmed
    assert a.current_value == 8e-9


# ---------------------------------------------------------------------------
# 7. Reverse dependency lookup
# ---------------------------------------------------------------------------


def test_07_reverse_dependency_lookup():
    ledger = AssumptionLedger()
    ledger.reset_id_counter(0)
    a = ledger.make("clk period 10ns")
    a.mark_dependent_constraint("CLK0001")
    a.mark_dependent_analysis("STA_RUN_1")
    assert ledger.constraints_for(a.id) == ["CLK0001"]
    assert ledger.analyses_for(a.id) == ["STA_RUN_1"]
    info = ledger.stale_consumers({a.id})
    assert "CLK0001" in info["stale_constraints"]
    assert "STA_RUN_1" in info["stale_analyses"]


def test_07_bind_constraint_bidirectional():
    ledger = AssumptionLedger()
    ledger.reset_id_counter(0)
    a = ledger.make("rel")
    ledger.bind_constraint(a.id, "CG0001")
    assert "CG0001" in a.dependent_constraints


# ---------------------------------------------------------------------------
# 8. Scenario attachment
# ---------------------------------------------------------------------------


def test_08_scenario_attachment():
    c = Constraint(id="X8", type=ConstraintType.SET_INPUT_DELAY)
    c.add_scenario("func_slow")
    c.add_scenario("func_fast")
    # duplicate is ignored
    c.add_scenario("func_slow")
    assert c.scenario_ids == ["func_slow", "func_fast"]
    # legacy scalar property picks first
    assert c.scenario == "func_slow"
    c.scenario = "scan_slow"
    assert c.scenario_ids[0] == "scan_slow"


# ---------------------------------------------------------------------------
# 9. Cloning / isolation
# ---------------------------------------------------------------------------


def test_09_clone_isolates_mutable_state():
    c = Constraint(id="X9", type=ConstraintType.CREATE_CLOCK,
                   target_objects=["clk"],
                   values={"name": "clk", "period": 10e-9},
                   scenario_ids=["s1"], assumption_ids=["A1"],
                   dependency_ids=["X0"])
    c.provenance.add_evidence(Evidence(id="E1", kind="structural", description="e"))
    cand = c.clone(new_id="X9_cand")
    assert cand.id == "X9_cand"
    assert cand.values == c.values
    # mutate candidate
    cand.values["period"] = 8e-9
    cand.add_value("comment", "opt")
    cand.scenario_ids.append("s2")
    cand.assumption_ids.append("A2")
    cand.provenance.evidence.append(Evidence(id="E2", kind="heuristic", description="new"))
    # baseline unchanged
    assert c.values["period"] == 10e-9
    assert "comment" not in c.values
    assert c.scenario_ids == ["s1"]
    assert c.assumption_ids == ["A1"]
    assert len(c.provenance.evidence) == 1


def test_09_clone_constraint_set_isolates_baseline():
    cs = ConstraintSet(name="base")
    c = cs.create_clock(name="clk", period_seconds=10e-9, source="clk",
                        source_kind=SourceKind.USER, fixed=True)
    base_id = c.id
    cs2 = cs.clone(name="cand")
    assert cs2 is not cs
    assert len(cs2) == len(cs)
    # mutate cs2 clock period
    cs2.get(base_id).values["period"] = 8e-9
    assert cs.get(base_id).values["period"] == 10e-9


# ---------------------------------------------------------------------------
# 10. Deterministic serialization
# ---------------------------------------------------------------------------


def test_10_deterministic_snapshot():
    cs = ConstraintSet(name="d")
    cs.create_clock(name="b", period_seconds=10e-9, source="b")
    cs.create_clock(name="a", period_seconds=8e-9, source="a")
    s1 = json.dumps(cs.snapshot(), sort_keys=True, default=str)
    s2 = json.dumps(cs.snapshot(), sort_keys=True, default=str)
    assert s1 == s2
    # Constraints sorted by id in snapshot regardless of insertion order.
    snap = cs.snapshot()
    keys = list(snap["constraints"].keys())
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# 11. Semantic identity
# ---------------------------------------------------------------------------


def test_11_semantic_equivalence_units():
    c1 = Constraint(id="A", type=ConstraintType.CREATE_CLOCK,
                    target_objects=["clk"],
                    values={"name": "clk", "period": 10e-9})
    c2 = Constraint(id="B", type=ConstraintType.CREATE_CLOCK,
                    target_objects=["clk"],
                    values={"name": "clk", "period": 10000e-12})
    assert c1.semantically_equivalent(c2)


def test_11_semantic_equivalence_order_independent():
    c1 = Constraint(id="A", type=ConstraintType.SET_FALSE_PATH,
                    path_selector=PathSelector(from_set=["a", "b"], to_set=["z"]))
    c2 = Constraint(id="B", type=ConstraintType.SET_FALSE_PATH,
                    path_selector=PathSelector(from_set=["b", "a"], to_set=["z"]))
    assert c1.semantically_equivalent(c2)


def test_11_semantic_inequality_when_values_differ():
    c1 = Constraint(id="A", type=ConstraintType.CREATE_CLOCK,
                    target_objects=["clk"],
                    values={"name": "clk", "period": 10e-9})
    c2 = Constraint(id="B", type=ConstraintType.CREATE_CLOCK,
                    target_objects=["clk"],
                    values={"name": "clk", "period": 8e-9})
    assert not c1.semantically_equivalent(c2)


# ---------------------------------------------------------------------------
# 12. Invalid references detected by validate()
# ---------------------------------------------------------------------------


def test_12_validate_detects_missing_assumption_and_dependency():
    cs = ConstraintSet(name="v")
    c = cs.add(Constraint(id="X", type=ConstraintType.CREATE_CLOCK,
                          target_objects=["clk"], values={"name": "clk"},
                          assumption_ids=["BOGUS"], dependency_ids=["NOPE"]))
    ledger = AssumptionLedger()
    issues = cs.validate(ledger=ledger)
    codes = {i.code for i in issues}
    assert "BAD_ASSUMPTION" in codes
    assert "BAD_DEPENDENCY" in codes


def test_12_validate_rejects_missing_scenario_reference():
    cs = ConstraintSet(name="v")
    cs.add(Constraint(id="X", type=ConstraintType.CREATE_CLOCK,
                      target_objects=["clk"], values={"name": "clk"},
                      scenario_ids=["ghost"]))
    issues = cs.validate()
    assert any(i.code == "BAD_SCENARIO" for i in issues)


def test_12_validate_clean_when_consistent():
    cs = ConstraintSet(name="v")
    cs.add_scenario(Scenario(id="func_slow", mode="functional", corner="slow"))
    ledger = AssumptionLedger()
    a = ledger.make("period of clk")
    c = cs.create_clock(name="clk", period_seconds=10e-9, source="clk",
                        assumption_ids=[a.id])
    # bind
    ledger.bind_constraint(a.id, c.id)
    issues = cs.validate(ledger=ledger)
    errors = [i for i in issues if i.level == "ERROR"]
    assert errors == []


# ---------------------------------------------------------------------------
# 13. Fixed immutability
# ---------------------------------------------------------------------------


def test_13_fixed_constraint_cannot_become_tunable():
    c = Constraint(id="FX", type=ConstraintType.CREATE_CLOCK,
                   status=ConstraintStatus.FIXED, opt_status=OptimizationStatus.FIXED,
                   values={"x": 1})
    problems = c.validate_invariants()
    assert not any("TUNABLE" in p for p in problems)
    # Attempt to force TUNABLE opt_status
    c.opt_status = OptimizationStatus.TUNABLE
    assert any("FIXED" in p and "TUNABLE" in p for p in c.validate_invariants())


# ---------------------------------------------------------------------------
# 14. Rejected/deprecated filter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [ConstraintStatus.REJECTED, ConstraintStatus.DEPRECATED])
def test_14_rejected_not_emittable_all_modes(status):
    c = Constraint(id="RJ", type=ConstraintType.SET_FALSE_PATH,
                   confidence=Confidence.HIGH, status=status)
    for mode in ("strict", "balanced", "exploratory"):
        assert not c.is_safe_to_emit(mode)


def test_14_emittable_excludes_rejected_in_set():
    cs = ConstraintSet(name="e")
    cs.add(Constraint(id="K1", type=ConstraintType.CREATE_CLOCK,
                      target_objects=["clk"], values={"name": "clk", "period": 10e-9},
                      source_kind=SourceKind.USER, confidence=Confidence.HIGH,
                      status=ConstraintStatus.CONFIRMED))
    cs.add(Constraint(id="K2", type=ConstraintType.SET_FALSE_PATH,
                      status=ConstraintStatus.REJECTED))
    emit = cs.emittable(SafeMode.EXPLORATORY)
    ids = {c.id for c in emit}
    assert "K1" in ids
    assert "K2" not in ids


# ---------------------------------------------------------------------------
# 15. Invalidation / stale set
# ---------------------------------------------------------------------------


def test_15_stale_set_transitive_closure():
    cs = ConstraintSet(name="t")
    clk = cs.create_clock(name="clk", period_seconds=10e-9, source="clk")
    ind = cs.create_input_delay(port="d", clock="clk", delay_seconds=1e-9)
    ind.add_dependency(clk.id)
    clk.add_downstream(ind.id)
    # A downstream "analysis" consumer of INP
    ind.dependent_analyses.append("STA01")
    stale = cs.stale_set(changed_constraint_ids={clk.id})
    assert clk.id in stale["stale_constraints"]
    assert ind.id in stale["stale_constraints"]
    assert "STA01" in stale["stale_analyses"]


def test_15_stale_set_from_assumption_change():
    cs = ConstraintSet(name="t")
    ledger = AssumptionLedger()
    ledger.reset_id_counter(0)
    a = ledger.make("clk period 10", default_value=10e-9)
    clk = cs.create_clock(name="clk", period_seconds=10e-9, source="clk",
                          assumption_ids=[a.id])
    ledger.bind_constraint(a.id, clk.id)
    clk.dependent_analyses.append("STA01")
    stale = cs.stale_set(changed_assumption_ids={a.id}, ledger=ledger)
    assert clk.id in stale["stale_constraints"]
    assert "STA01" in stale["stale_analyses"]


# ---------------------------------------------------------------------------
# 16. Imported-SDC provenance
# ---------------------------------------------------------------------------


def test_16_imported_sdc_provenance_metadata(tmp_path):
    from rca.sdc.parser import SDCParser
    sdc = "create_clock -name clk -period 10 [get_ports clk]\n"
    p = tmp_path / "in.sdc"
    p.write_text(sdc)
    cs = SDCParser().parse_file(str(p))
    clk = cs.clocks()[0]
    assert clk.source_kind == SourceKind.EXISTING_SDC
    assert clk.provenance is not None
    assert clk.provenance.import_meta is not None
    meta = clk.provenance.import_meta
    assert meta.source_file.endswith("in.sdc")
    assert meta.source_line == 1
    assert "create_clock" in (meta.original_command or "")
    assert meta.source_format == "sdc"


# ---------------------------------------------------------------------------
# 17. Missing info represented as assumptions
# ---------------------------------------------------------------------------


def test_17_missing_info_severity_levels():
    ledger = AssumptionLedger()
    ledger.reset_id_counter(0)
    req = ledger.make("Clock clk period missing", origin="INFERENCE",
                      confidence="UNKNOWN", severity="REQUIRED",
                      user_confirmed=False, fixed=False)
    rec = ledger.make("Relationship clka<->clkb unknown", origin="INFERENCE",
                      confidence="UNKNOWN", severity="RECOMMENDED",
                      user_confirmed=False, fixed=False)
    info = ledger.make("Input 'd' clock association unknown", origin="INFERENCE",
                       confidence="UNKNOWN", severity="INFO",
                       user_confirmed=False, fixed=False)
    by_sev = {a.severity for a in ledger}
    assert by_sev == {"REQUIRED", "RECOMMENDED", "INFO"}
    assert not req.user_confirmed


# ---------------------------------------------------------------------------
# 18. Source-kind correctness
# ---------------------------------------------------------------------------


def test_18_source_kind_enum_normalized_in_provenance():
    # Strings are accepted but the preferred path is SourceKind enum.
    p = ProvenanceRecord(source_kind=SourceKind.USER)
    assert p.source_kind == SourceKind.USER
    p2 = ProvenanceRecord(source_kind="EXISTING_SDC")
    # Either form works (string preserved for backward compat).
    assert str(p2.source_kind) in ("EXISTING_SDC", "SourceKind.EXISTING_SDC")


# ---------------------------------------------------------------------------
# 19. Path selector semantics
# ---------------------------------------------------------------------------


def test_19_path_selector_fields_and_key():
    ps = PathSelector(from_set=["a"], to_set=["z"], through_set=[["b", "c"]],
                      edge="rise", min_max="min", setup_hold="setup",
                      add_delay=True, from_clock=["clk"])
    s = ps.to_dict()
    assert s["from_set"] == ["a"]
    assert s["edge"] == "rise"
    assert s["min_max"] == "min"
    assert s["setup_hold"] == "setup"
    assert s["add_delay"] is True
    # is_empty
    assert not ps.is_empty()
    assert PathSelector().is_empty()
    # semantic key distinguishes edge/minmax
    ps2 = ps.model_copy(deep=True)
    ps2.edge = "fall"
    assert not ps.semantically_equivalent(ps2)


# ---------------------------------------------------------------------------
# 20. Semantic duplicate detection
# ---------------------------------------------------------------------------


def test_20_semantic_duplicates_detected():
    cs = ConstraintSet(name="d")
    cs.create_clock(name="clk", period_seconds=10e-9, source="clk")
    cs.create_clock(name="clk", period_seconds=10e-9, source="clk")
    dups = cs.find_semantic_duplicates()
    assert any(len(g) == 2 for g in dups)


# ---------------------------------------------------------------------------
# 21. Negative: fixed cannot be rejected
# ---------------------------------------------------------------------------


def test_21_negative_reject_fixed_raises():
    c = Constraint(id="N1", type=ConstraintType.CREATE_CLOCK,
                   status=ConstraintStatus.FIXED, opt_status=OptimizationStatus.FIXED)
    with pytest.raises(ValueError):
        c.mark_rejected("nope")


# ---------------------------------------------------------------------------
# 22. Negative: clone must not mutate baseline
# ---------------------------------------------------------------------------


def test_22_clone_baseline_unchanged_after_cand_mutation():
    cs = ConstraintSet(name="d")
    c = cs.create_clock(name="clk", period_seconds=10e-9, source="clk",
                        source_kind=SourceKind.USER, fixed=True)
    before = json.dumps(cs.snapshot(), sort_keys=True, default=str)
    cand = cs.clone(name="cand")
    cand.get(c.id).values["period"] = 6e-9
    after = json.dumps(cs.snapshot(), sort_keys=True, default=str)
    assert before == after


# ---------------------------------------------------------------------------
# Lifecycle integration test (Step 3 #18) — user → UCM → provenance →
# assumption → validation → clone → snapshot round-trip semantic equality
# ---------------------------------------------------------------------------


def test_lifecycle_integration_end_to_end():
    from rca.sdc.parser import SDCParser
    # Build a UCM via mixed sources: one user clock + one imported clock.
    cs = ConstraintSet(name="top")
    cs.add_scenario(Scenario(id="func_slow", mode="functional", corner="slow"))
    ledger = AssumptionLedger()
    ledger.reset_id_counter(0)
    a_period = ledger.make("clk period 10ns", default_value=10e-9,
                           current_value=10e-9, severity="REQUIRED")
    clk = cs.create_clock(name="clk", period_seconds=10e-9, source="clk",
                          source_kind=SourceKind.USER, fixed=True,
                          scenario_ids=["func_slow"], assumption_ids=[a_period.id])
    ledger.bind_constraint(a_period.id, clk.id)
    clk.provenance.add_evidence(Evidence(
        id="UE1", kind="user", description="user-provided clock period"))
    # Input delay depends on clk.
    ind = cs.create_input_delay(port="d", clock="clk", delay_seconds=0.5e-9,
                                scenario_ids=["func_slow"])
    ind.add_dependency(clk.id)
    clk.add_downstream(ind.id)
    ledger.bind_constraint(a_period.id, ind.id)
    ind.dependent_analyses.append("STA_top_func_slow")
    # Validate baseline
    issues = cs.validate(ledger=ledger)
    errors = [i for i in issues if i.level == "ERROR"]
    assert errors == [], f"unexpected errors: {errors}"
    # Clone as candidate
    cand = cs.clone(name="top_cand")
    # Mutate candidate clock period; baseline stays unchanged.
    cand.get(clk.id).values["period"] = 8e-9
    assert cs.get(clk.id).values["period"] == 10e-9
    # Deterministic snapshot
    snap1 = cs.snapshot()
    snap2 = cs.snapshot()
    assert json.dumps(snap1, sort_keys=True, default=str) == json.dumps(
        snap2, sort_keys=True, default=str)
    # Round-trip from snapshot and semantic equality on clone vs baseline
    rt = ConstraintSet.from_snapshot(snap1)
    assert rt.name == "top"
    assert set(rt.constraints.keys()) == {clk.id, ind.id}
    # Staleness when assumption changes
    stale = cs.stale_set(changed_assumption_ids={a_period.id}, ledger=ledger)
    assert clk.id in stale["stale_constraints"]
    assert ind.id in stale["stale_constraints"]
    assert "STA_top_func_slow" in stale["stale_analyses"]


# =========================================================================
# CANONICAL SNAPSHOT LOSSLESS ROUND-TRIP TESTS (Step-3 follow-up)
# =========================================================================


def test_23_canonical_snapshot_has_schema_version():
    cs = ConstraintSet(name="t")
    snap = cs.to_snapshot_dict()
    assert snap["schema_version"] == 1
    assert "constraints" in snap and "scenarios" in snap
    assert "assumptions" in snap


def test_23_source_kind_normalized_string_equals_enum():
    p1 = ProvenanceRecord(source_kind="USER")
    p2 = ProvenanceRecord(source_kind=SourceKind.USER)
    assert p1.source_kind == SourceKind.USER
    assert p2.source_kind == SourceKind.USER
    assert p1.to_dict()["source_kind"] == p2.to_dict()["source_kind"] == "USER"


def test_23_confidence_normalized_across_layers():
    # Confidence string inputs normalize to Confidence enum in
    # Constraint, ProvenanceRecord, Evidence, and Assumption.
    e = Evidence(id="E", kind="structural", description="x", confidence="HIGH")
    assert e.confidence == Confidence.HIGH
    p = ProvenanceRecord(confidence="LOW")
    assert p.confidence == Confidence.LOW
    c = Constraint(id="C", type=ConstraintType.CREATE_CLOCK, confidence="medium")
    assert c.confidence == Confidence.MEDIUM
    a = Assumption(id="A", statement="x", confidence="low")
    assert a.confidence == Confidence.LOW


def test_23_canonical_roundtrip_preserves_full_provenance_and_evidence():
    cs = ConstraintSet(name="rt")
    cs.add_scenario(Scenario(id="func_slow", mode="functional", corner="slow"))
    a = cs.ledger.make("clk period 10ns", default_value=10e-9, current_value=10e-9,
                       severity="REQUIRED")
    clk = cs.create_clock(name="clk", period_seconds=10e-9, source="clk",
                          source_kind=SourceKind.USER, fixed=True,
                          confidence=Confidence.HIGH,
                          scenario_ids=["func_slow"], assumption_ids=[a.id])
    clk.provenance.add_evidence(Evidence(id="UE1", kind="user",
                                         description="user provided clock",
                                         source_objects=["clk"],
                                         location="project.rca:12",
                                         confidence=Confidence.HIGH,
                                         rule_id="U-CLK"))
    # Complex PathSelector false path
    fp = cs.add(Constraint(
        id="FP0001",
        type=ConstraintType.SET_FALSE_PATH,
        path_selector=PathSelector(
            from_set=["u_a/q"],
            through_set=[["u_b/d"]],
            to_set=["u_c/d"],
            edge="rise",
            min_max="max",
            setup_hold="setup",
            add_delay=False,
            from_clock=["clk"],
            to_clock=["clk"],
        ),
        source_kind=SourceKind.INFERENCE,
        confidence=Confidence.LOW,
        status=ConstraintStatus.REQUIRES_CONFIRMATION,
        opt_status=OptimizationStatus.TUNABLE,
        assumption_ids=[a.id],
        scenario_ids=["func_slow"],
        dependent_analyses=["STA01"],
    ))
    cs.ledger.bind_constraint(a.id, fp.id)
    # Dependency
    fp.add_dependency(clk.id)
    clk.add_downstream(fp.id)

    # Validation clean
    issues = cs.validate()
    errors = [i for i in issues if i.level == "ERROR"]
    assert errors == [], errors

    # Serialize canonical
    snap = cs.to_snapshot_dict()
    js = cs.to_canonical_json()
    assert "schema_version" in js

    # Restore
    r = ConstraintSet.from_snapshot_dict(snap)

    # Schema checks
    assert snap["schema_version"] == 1

    # Same constraint IDs, types, values, provenance, evidence
    rc = r.get(clk.id)
    assert rc is not None
    assert rc.type == ConstraintType.CREATE_CLOCK
    assert rc.values["period"] == 10e-9
    assert rc.source_kind == SourceKind.USER
    assert rc.status == ConstraintStatus.FIXED
    assert rc.opt_status == OptimizationStatus.FIXED
    assert rc.confidence == Confidence.HIGH
    assert set(rc.target_objects) == {"clk"}
    assert set(rc.clock_refs) == {"clk"}
    assert rc.scenario_ids == ["func_slow"]
    # Provenance preserved
    assert rc.provenance.source_kind == SourceKind.USER
    assert rc.provenance.confidence == Confidence.MEDIUM  # provenance confidence default
    kinds = {e.kind for e in rc.provenance.evidence}
    assert "user" in kinds
    ue = [e for e in rc.provenance.evidence if e.id == "UE1"][0]
    assert ue.description == "user provided clock"
    assert ue.source_objects == ["clk"]
    assert ue.rule_id == "U-CLK"
    assert ue.confidence == Confidence.HIGH
    # False path path_selector fully preserved
    rfp = r.get(fp.id)
    assert rfp is not None
    assert rfp.path_selector is not None
    assert rfp.path_selector.from_set == ["u_a/q"]
    assert rfp.path_selector.to_set == ["u_c/d"]
    assert rfp.path_selector.through_set == [["u_b/d"]]
    assert rfp.path_selector.edge == "rise"
    assert rfp.path_selector.min_max == "max"
    assert rfp.path_selector.setup_hold == "setup"
    assert rfp.path_selector.from_clock == ["clk"]
    assert rfp.path_selector.semantically_equivalent(fp.path_selector)

    # Assumption ledger restored with all fields
    ra = r.ledger.get(a.id)
    assert ra is not None
    assert ra.statement == "clk period 10ns"
    assert ra.current_value == 10e-9
    assert ra.default_value == 10e-9
    assert ra.user_confirmed is True
    assert ra.fixed is True
    assert set(ra.dependent_constraints) == {clk.id, fp.id}
    assert ra.severity == "REQUIRED"

    # Reverse dependencies consistent after restore
    assert fp.id in rc.downstream_ids
    assert clk.id in rfp.dependency_ids
    issues2 = r.validate()
    errors2 = [i for i in issues2 if i.level == "ERROR"]
    assert errors2 == [], errors2

    # Semantic keys match between original and restored.
    for cid in (clk.id, fp.id):
        assert cs.get(cid).semantically_equivalent(r.get(cid))


def test_23_import_metadata_roundtrip_preserves_original_command(tmp_path):
    from rca.sdc.parser import SDCParser
    sdc = ("create_clock -name clk -period 10 [get_ports clk]\n"
           "set_input_delay -clock clk -max 0.5 [get_ports d]\n")
    p = tmp_path / "in.sdc"
    p.write_text(sdc)
    cs = SDCParser().parse_file(str(p))
    assert len(cs.clocks()) == 1
    snap = cs.to_snapshot_dict()
    r = ConstraintSet.from_snapshot_dict(snap)
    clk = r.clocks()[0]
    meta = clk.provenance.import_meta
    assert meta is not None
    assert meta.source_file.endswith("in.sdc")
    assert meta.source_format == "sdc"
    assert "create_clock" in (meta.original_command or "")
    assert meta.original_command is not None
    js = r.to_canonical_json()
    assert "original_command" in js
    assert "create_clock" in js


def test_23_assumption_invalidation_survives_roundtrip():
    cs = ConstraintSet(name="inv")
    a = cs.ledger.make("period", default_value=10e-9, current_value=10e-9)
    clk = cs.create_clock(name="clk", period_seconds=10e-9, source="clk",
                          assumption_ids=[a.id])
    cs.ledger.bind_constraint(a.id, clk.id)
    clk.dependent_analyses.append("STA01")
    snap = cs.to_snapshot_dict()
    r = ConstraintSet.from_snapshot_dict(snap)
    ra = r.ledger.get(a.id)
    # Change assumption value and verify stale_set still flags the clock.
    ra.current_value = 8e-9
    stale = r.stale_set(changed_assumption_ids={a.id})
    assert clk.id in stale["stale_constraints"]
    assert "STA01" in stale["stale_analyses"]


def test_23_clone_distinct_provenance_and_assumptions_not_aliased():
    cs = ConstraintSet(name="b")
    a = cs.ledger.make("p", default_value=10e-9, current_value=10e-9)
    clk = cs.create_clock(name="clk", period_seconds=10e-9, source="clk",
                          assumption_ids=[a.id])
    clk.provenance.add_evidence(Evidence(id="E1", kind="user", description="d"))
    cand = cs.clone(name="c")
    # Mutate candidate: add evidence and change value.
    cand.get(clk.id).values["period"] = 8e-9
    cand.get(clk.id).provenance.add_evidence(
        Evidence(id="E2", kind="heuristic", description="opt"))
    cand.ledger.get(a.id).current_value = 8e-9
    # Baseline unchanged
    assert cs.get(clk.id).values["period"] == 10e-9
    ids = {e.id for e in cs.get(clk.id).provenance.evidence}
    assert "E2" not in ids
    assert cs.ledger.get(a.id).current_value == 10e-9
    # Dependency graphs both valid
    assert cs.validate() == [] or all(i.level == "WARNING" for i in cs.validate())
    assert cand.validate() == [] or all(i.level == "WARNING" for i in cand.validate())


def test_23_reverse_edge_mismatch_default_raises():
    """By default from_snapshot_dict must refuse inconsistent reverse edges."""
    cs = ConstraintSet(name="m")
    c1 = cs.create_clock(name="clk", period_seconds=10e-9, source="clk")
    c2 = cs.create_input_delay(port="d", clock="clk", delay_seconds=0.5e-9)
    c2.add_dependency(c1.id)
    c1.add_downstream(c2.id)
    snap = cs.to_snapshot_dict()
    # Tamper: remove the reverse edge
    snap["constraints"][c1.id]["downstream_ids"] = []
    with pytest.raises(SnapshotFormatError) as exc:
        ConstraintSet.from_snapshot_dict(snap)
    details = exc.value.details
    assert any(d["code"] == "REVERSE_EDGE_MISMATCH" for d in details)
    # Original snapshot is unmodified.
    assert snap["constraints"][c1.id]["downstream_ids"] == []


def test_23_reverse_edge_mismatch_explicit_repair_records():
    """repair_reverse_edges=True rebuilds reverse edges AND records it."""
    cs = ConstraintSet(name="m")
    c1 = cs.create_clock(name="clk", period_seconds=10e-9, source="clk")
    c2 = cs.create_input_delay(port="d", clock="clk", delay_seconds=0.5e-9)
    c2.add_dependency(c1.id)
    c1.add_downstream(c2.id)
    snap = cs.to_snapshot_dict()
    snap["constraints"][c1.id]["downstream_ids"] = []
    r = ConstraintSet.from_snapshot_dict(snap, repair_reverse_edges=True)
    assert c2.id in r.get(c1.id).downstream_ids
    assert c1.id in r.get(c2.id).dependency_ids
    errors = [i for i in r.validate() if i.level == "ERROR"]
    assert errors == []
    codes = {rec.code for rec in r.snapshot_repairs}
    assert "REVERSE_EDGE_REPAIRED" in codes


def test_23_missing_dependency_reference_raises():
    cs = ConstraintSet(name="m")
    c1 = cs.create_clock(name="clk", period_seconds=10e-9, source="clk")
    snap = cs.to_snapshot_dict()
    snap["constraints"][c1.id]["dependency_ids"] = ["MISSING_X"]
    with pytest.raises(SnapshotFormatError) as exc:
        ConstraintSet.from_snapshot_dict(snap)
    assert any(d["code"] == "MISSING_DEPENDENCY" for d in exc.value.details)


def test_23_self_dependency_raises():
    cs = ConstraintSet(name="m")
    c1 = cs.create_clock(name="clk", period_seconds=10e-9, source="clk")
    snap = cs.to_snapshot_dict()
    snap["constraints"][c1.id]["dependency_ids"] = [c1.id]
    with pytest.raises(SnapshotFormatError) as exc:
        ConstraintSet.from_snapshot_dict(snap)
    assert any(d["code"] == "SELF_DEPENDENCY" for d in exc.value.details)


def test_23_cycle_rejected_when_policy_says_so():
    cs = ConstraintSet(name="m")
    c1 = cs.create_clock(name="a", period_seconds=10e-9, source="a")
    c2 = cs.create_clock(name="b", period_seconds=10e-9, source="b")
    c1.dependency_ids.append(c2.id)
    c2.dependency_ids.append(c1.id)
    c1.downstream_ids.append(c2.id)
    c2.downstream_ids.append(c1.id)
    snap = cs.to_snapshot_dict()
    # Default: allow_cycles=True, cycle warned but not rejected
    r = ConstraintSet.from_snapshot_dict(snap)
    assert any(rec.code == "DEPENDENCY_CYCLE_DETECTED" for rec in r.snapshot_repairs)
    # Explicit strict: raises
    with pytest.raises(SnapshotFormatError) as exc:
        ConstraintSet.from_snapshot_dict(snap, allow_cycles=False)
    assert any(d["code"] == "DEPENDENCY_CYCLE" for d in exc.value.details)


def test_in_memory_validate_detects_reverse_mismatch():
    broken = ConstraintSet(name="br")
    b1 = broken.create_clock(name="clk", period_seconds=10e-9, source="clk")
    b2 = broken.create_input_delay(port="d", clock="clk", delay_seconds=0.5e-9)
    b2.dependency_ids.append(b1.id)
    issues = broken.validate()
    assert any(i.code == "REVERSE_EDGE_MISMATCH" for i in issues)


def test_23_deterministic_canonical_json():
    cs = ConstraintSet(name="d")
    cs.create_clock(name="b", period_seconds=10e-9, source="b")
    cs.create_clock(name="a", period_seconds=8e-9, source="a")
    j1 = cs.to_canonical_json()
    j2 = cs.to_canonical_json()
    assert j1 == j2
    # Loading and re-serializing yields identical JSON (lossless).
    r = ConstraintSet.from_canonical_json(j1)
    j3 = r.to_canonical_json()
    assert j1 == j3


def test_23_unknown_top_level_field_error_mode():
    cs = ConstraintSet(name="u")
    cs.create_clock(name="clk", period_seconds=10e-9, source="clk")
    snap = cs.to_snapshot_dict()
    snap["constraints"][list(snap["constraints"].keys())[0]]["bogus_field"] = 42
    with pytest.raises(ValueError):
        ConstraintSet.from_snapshot_dict(snap, unknown_field_policy="error")


def test_23_future_schema_version_rejected():
    snap = {"schema_version": 99, "name": "future", "constraints": {}, "scenarios": {},
            "metadata": {}, "assumptions": None}
    with pytest.raises(ValueError) as ei:
        ConstraintSet.from_snapshot_dict(snap)
    assert "schema_version" in str(ei.value)


def test_23_canonical_path_selector_semantic_key_after_restore():
    ps = PathSelector(
        from_set=["a", "b"],
        through_set=[["c"]],
        to_set=["z"],
        edge="fall",
        min_max="min",
        setup_hold="hold",
        add_delay=True,
        from_clock=["clka"],
        to_clock=["clkb"],
        through_clock=["clkc"],
    )
    d = ps.to_dict()
    ps2 = PathSelector.from_dict(d)
    assert ps.semantically_equivalent(ps2)


def test_23_validation_catches_un_normalized_source_kind():
    # Directly construct a Constraint with a weird string source_kind via
    # object.__setattr__ to ensure validate flags it. We simulate this by
    # constructing via pydantic parse with a raw dict where provenance's
    # source_kind was forced to a non-normalized value through a backdoor.
    c = Constraint(id="X", type=ConstraintType.CREATE_CLOCK,
                   target_objects=["clk"], values={"name": "clk"})
    # Provenance is already normalized. Force an invalid state.
    object.__setattr__(c.provenance, "source_kind", "WEIRD")
    issues = c.validate_invariants()
    assert any("source_kind" in i for i in issues)


def test_24_canonical_json_deterministic_across_insertion_orders():
    """Two semantically identical UCMs built with different insertion
    orders for constraints, scenarios, assumptions, evidence, and
    dependency edges must produce byte-identical canonical JSON.

    Construction pins IDs and timestamps so counter-generated IDs and
    `created_at` values do not differ across the two builders.
    """
    from datetime import datetime, timezone

    TS = "2025-01-01T00:00:00+00:00"

    def build(order="AB"):
        cs = ConstraintSet(name="det", run_id="r1", created_at=TS)
        s1 = Scenario(id="s_fast", mode="functional", corner="fast",
                      libraries=["lib_fast"], active=True)
        s2 = Scenario(id="s_slow", mode="functional", corner="slow",
                      libraries=["lib_slow"], active=True)
        # Reset counter so fixed ids line up regardless of order.
        cs._counter = 0
        for sid in (["s_fast", "s_slow"] if order == "AB" else ["s_slow", "s_fast"]):
            cs.scenarios[sid] = s1 if sid == "s_fast" else s2
        a1 = cs.ledger.add(Assumption(
            id="A0001", statement="io registers", origin="parser",
            confidence=Confidence.LOW, default_value=True, current_value=True,
            dependent_constraints=[], dependent_analyses=["setup"],
            created_at=TS))
        a2 = cs.ledger.add(Assumption(
            id="A0002", statement="clock naming", origin="hint",
            confidence=Confidence.LOW, default_value="clk", current_value="clk",
            created_at=TS))
        ev1 = Evidence(id="EV1", kind="PARSER", description="SDC file", confidence=Confidence.HIGH,
                       source_objects=["top.sdc"], location="top.sdc:1",
                       created_by="rca", created_at=TS)
        ev2 = Evidence(id="EV2", kind="USER", description="user said so", confidence=Confidence.HIGH,
                       source_objects=["user"], location=None,
                       created_by="rca", created_at=TS)

        # Manually build constraints with fixed IDs to avoid order-dependent
        # CLK0001/INP0002 counter assignment.
        clk = Constraint(id="CLK1", type=ConstraintType.CREATE_CLOCK,
                         target_objects=["clk"], clock_refs=["clk"],
                         values={"name": "clk", "period": 10e-9},
                         source_kind=SourceKind.USER, confidence=Confidence.HIGH,
                         status=ConstraintStatus.FIXED, opt_status=OptimizationStatus.FIXED,
                         scenario_ids=["s_slow", "s_fast"],
                         assumption_ids=["A0001"],
                         provenance=ProvenanceRecord(created_by="rca", created_at=TS,
                                                     source_kind=SourceKind.USER))
        ind = Constraint(id="INP1", type=ConstraintType.SET_INPUT_DELAY,
                         target_objects=["d"], clock_refs=["clk"],
                         values={"clock": "clk", "delay": 0.5e-9, "min_max": "max"},
                         source_kind=SourceKind.USER, confidence=Confidence.HIGH,
                         status=ConstraintStatus.CONFIRMED,
                         opt_status=OptimizationStatus.TUNABLE,
                         scenario_ids=["s_slow"],
                         provenance=ProvenanceRecord(created_by="rca", created_at=TS,
                                                     source_kind=SourceKind.USER))
        fp = Constraint(id="FP1", type=ConstraintType.SET_FALSE_PATH,
                        path_selector=PathSelector(from_set=["rst"], to_set=["q"]),
                        source_kind=SourceKind.INFERENCE, confidence=Confidence.LOW,
                        status=ConstraintStatus.REQUIRES_CONFIRMATION,
                        opt_status=OptimizationStatus.TUNABLE,
                        scenario_ids=["s_fast"], assumption_ids=["A0002"],
                        provenance=ProvenanceRecord(created_by="rca", created_at=TS,
                                                    source_kind=SourceKind.INFERENCE))
        # Insert in different orders.
        items = [("CLK1", clk), ("INP1", ind), ("FP1", fp)] if order == "AB" \
                else [("FP1", fp), ("INP1", ind), ("CLK1", clk)]
        for cid, c in items:
            cs.constraints[cid] = c
        cs._counter = 1

        # Dependency edge order varies
        if order == "AB":
            ind.dependency_ids = ["CLK1"]; clk.downstream_ids = ["FP1", "INP1"]
            fp.dependency_ids = ["CLK1"]
        else:
            fp.dependency_ids = ["CLK1"]; ind.dependency_ids = ["CLK1"]
            clk.downstream_ids = ["INP1", "FP1"]

        ev_order = [ev1, ev2] if order == "AB" else [ev2, ev1]
        for ev in ev_order:
            clk.provenance.add_evidence(ev)
            clk.evidence_ids.append(ev.id)
        ind.provenance.add_evidence(ev1)
        ind.evidence_ids.append(ev1.id)
        cs.ledger.bind_constraint("A0001", "CLK1")
        cs.ledger.bind_constraint("A0002", "FP1")
        return cs

    a = build("AB")
    b = build("BA")
    ja = a.to_canonical_json(indent=None)
    jb = b.to_canonical_json(indent=None)
    assert ja == jb
    r = ConstraintSet.from_canonical_json(ja)
    assert r.to_canonical_json(indent=None) == ja


def test_24_full_canonical_roundtrip_double_json_equivalence(tmp_path):
    """Comprehensive UCM -> JSON -> restore -> JSON must be byte identical."""
    import tempfile, os
    from rca.sdc.parser import SDCParser

    cs = ConstraintSet(name="full", run_id="r")
    s = Scenario(id="func", mode="functional", corner="slow",
                 libraries=["tt"], active=True)
    cs.scenarios[s.id] = s
    clk = cs.create_clock(name="clk", period_seconds=10e-9, source="clk",
                          source_kind=SourceKind.USER, confidence=Confidence.HIGH,
                          fixed=True, scenario_ids=["func"])
    ind = cs.create_input_delay(port="d", clock="clk", delay_seconds=0.5e-9,
                                scenario_ids=["func"])
    fp = cs.create_false_path(from_set=["rst"], to_set=["q"],
                              from_clock=["clk"], to_clock=["clk"],
                              through_set=[["u1"]])
    cs.add_dependency_edge(clk.id, ind.id)
    cs.add_dependency_edge(clk.id, fp.id)
    ev = Evidence(id="EVU", kind="USER", description="user declared", confidence=Confidence.HIGH,
                  source_objects=["interactive"], location=None)
    clk.provenance.add_evidence(ev); clk.evidence_ids.append(ev.id)
    a = cs.ledger.add(Assumption(
        id="A0001", statement="input registered", origin="inference",
        confidence=Confidence.LOW, default_value=True, current_value=True,
        dependent_constraints=[ind.id], dependent_analyses=["setup_check"]))
    cs.ledger.bind_constraint("A0001", ind.id)
    ind.add_assumption("A0001")
    # Add import_meta to clk.provenance
    clk.provenance.import_meta = ImportMetadata(
        source_file="user.sdc", source_line=1, original_command="create_clock ...",
        source_format="sdc", import_run_id="r", extra={"note": "ok"})

    j1 = cs.to_canonical_json(indent=2)
    r = ConstraintSet.from_canonical_json(j1)
    # Validate clean
    errs = [i for i in r.validate() if i.level == "ERROR"]
    assert errs == [], errs
    j2 = r.to_canonical_json(indent=2)
    assert j1 == j2
    # Stale-set behavior survives
    r.ledger.get("A0001").current_value = False
    stale = r.stale_set(changed_assumption_ids={"A0001"})
    assert ind.id in stale["stale_constraints"]
    assert "setup_check" in stale["stale_analyses"]
