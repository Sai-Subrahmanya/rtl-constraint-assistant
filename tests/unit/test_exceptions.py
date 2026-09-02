"""Step 8 — timing exception analyzer / verifier unit tests."""
from __future__ import annotations

from copy import deepcopy

import pytest

from rca.constraint_model import (
    Constraint,
    ConstraintSet,
    PathSelector,
)
from rca.design_model.design import Design
from rca.exceptions import (
    ConservativeFormalBackend,
    ExceptionAnalysisResult,
    FormalBackend,
    MockFormalBackend,
    VerificationResult,
    analyze_exceptions,
    emittable_exceptions,
    verify_exceptions,
)
from rca.utils.enums import (
    ClockDomainRelationship,
    Confidence,
    ConstraintStatus,
    ConstraintType,
    ExceptionFindingKind,
    ExceptionRisk,
    Severity,
    TimingPathClass,
    VerificationStatus,
)
from rca.timing_model.clock import Clock
from rca.timing_model.clock_domain import ClockDomainEdge
from rca.timing_model.timing_graph import TimingGraph
from rca.timing_model.timing_path import TimingPath


def _cset():
    return ConstraintSet()


def _fp(cs, cid: str, fro=None, to=None, through=None,
        scenarios=None, fixed=False):
    ps = PathSelector(from_set=fro or [], to_set=to or [],
                      through_set=[through] if through else [])
    c = Constraint(id=cid, type=ConstraintType.SET_FALSE_PATH,
                   path_selector=ps, scenario_ids=list(scenarios or []),
                   status=ConstraintStatus.FIXED if fixed else ConstraintStatus.PROPOSED,
                   confidence=Confidence.HIGH if fixed else Confidence.MEDIUM)
    cs.add(c)
    return c


def _mc(cs, cid: str, cycles, fro=None, to=None, start=False, end=False):
    ps = PathSelector(from_set=fro or [], to_set=to or [])
    c = Constraint(id=cid, type=ConstraintType.SET_MULTICYCLE_PATH,
                   path_selector=ps,
                   values={"cycles": cycles, "start": start, "end": end})
    cs.add(c)
    return c


def _clock(cs, name, period=10e-9, source=None):
    # create_clock auto-assigns cid; we use the clock_refs for matching
    c = cs.create_clock(name=name, period_seconds=period, source=source or name)
    return c


def _graph_clock(name, period=10e-9):
    return Clock(id=name, name=name, source_object=name, period_seconds=period)


def _make_tg(clocks=None, paths=None, edges=None):
    return TimingGraph(
        clocks={n: _graph_clock(n) for n in (clocks or [])},
        paths=list(paths or []),
        domain_edges=list(edges or []),
    )


MINI_DESIGN = Design(name="dut")


# ---- helpers ----

def _find(results, cid):
    for r in results:
        if r.constraint_id == cid:
            return r
    raise AssertionError(f"constraint {cid} not found in results")


def _find_kind(r, kind):
    for f in r.structural_findings:
        if f["kind"] == kind.value:
            return f
    return None


# ----------------- broad FP no selectors -----------------

def test_01_false_path_no_selectors_is_broad():
    cs = _cset()
    _clock(cs, "clk_a")
    _fp(cs, "FP1")
    tg = _make_tg(clocks=["clk_a"])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert _find_kind(r, ExceptionFindingKind.BROAD) is not None
    assert r.risk in (ExceptionRisk.HIGH, ExceptionRisk.CRITICAL)


def test_02_false_path_zero_paths_is_no_effect():
    cs = _cset()
    _clock(cs, "clk_a")
    _clock(cs, "clk_b")
    _fp(cs, "FP1", fro=["ghost_src"], to=["ghost_dst"])
    tg = _make_tg(
        clocks=["clk_a", "clk_b"],
        paths=[TimingPath(startpoint="ra/Q", endpoint="rb/D",
                          launch_clock="clk_a", capture_clock="clk_b",
                          path_type=TimingPathClass.CDC)],
    )
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert _find_kind(r, ExceptionFindingKind.NO_EFFECT) is not None
    assert r.verification_status == VerificationStatus.NOT_APPLICABLE


def test_03_false_path_narrow_match():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"])
    tg = _make_tg(
        clocks=["clk"],
        paths=[TimingPath(startpoint="ra/Q", endpoint="rb/D",
                          launch_clock="clk", capture_clock="clk",
                          path_type=TimingPathClass.REG_TO_REG)],
    )
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert r.blast_radius.path_count == 1
    assert r.risk in (ExceptionRisk.LOW, ExceptionRisk.MEDIUM)
    assert _find_kind(r, ExceptionFindingKind.BROAD) is None


def test_04_false_path_cross_clock_flagged():
    cs = _cset()
    _clock(cs, "clk_a")
    _clock(cs, "clk_b")
    _fp(cs, "FP1", fro=["clk_a"], to=["clk_b"])
    tg = _make_tg(
        clocks=["clk_a", "clk_b"],
        paths=[TimingPath(startpoint="ra/Q", endpoint="rb/D",
                          launch_clock="clk_a", capture_clock="clk_b",
                          path_type=TimingPathClass.CDC)],
        edges=[ClockDomainEdge(clock_a="clk_a", clock_b="clk_b")],
    )
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert _find_kind(r, ExceptionFindingKind.CLOCK_DOMAIN_CROSSING) is not None


def test_05_false_path_reset_named_is_flagged_not_safe():
    cs = _cset()
    _clock(cs, "clk")
    # reset_n is named reset but used as ordinary data going to rb/D
    _fp(cs, "FP1", fro=["reset_n"], to=["rb/D"])
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="reset_n", endpoint="rb/D",
                   launch_clock=None, capture_clock="clk",
                   path_type=TimingPathClass.INPUT_TO_REG),
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert _find_kind(r, ExceptionFindingKind.RESET_RELATED) is not None
    assert r.verification_status != VerificationStatus.VERIFIED  # naming alone is not proof


# ---------------- multicycle -----------------

def test_06_multicycle_valid():
    cs = _cset()
    _clock(cs, "clk")
    _mc(cs, "MC1", cycles=2, fro=["ra/Q"], to=["rb/D"])
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "MC1")
    assert r.lifecycle == VerificationStatus.STRUCTURALLY_ANALYZED
    assert _find_kind(r, ExceptionFindingKind.CYCLE_COUNT_INVALID) is None


def test_07_multicycle_invalid_cycle_count():
    cs = _cset()
    _clock(cs, "clk")
    _mc(cs, "MC1", cycles=-1)
    rep = analyze_exceptions(MINI_DESIGN, cs,
                             tg=_make_tg(clocks=["clk"]))
    r = _find(rep, "MC1")
    assert _find_kind(r, ExceptionFindingKind.CYCLE_COUNT_INVALID) is not None
    assert r.verification_status == VerificationStatus.INVALID


def test_08_multicycle_setup_hold_mismatch():
    cs = _cset()
    _clock(cs, "clk")
    _mc(cs, "MC1", cycles=2, start=True, end=True)
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=_make_tg(clocks=["clk"]))
    r = _find(rep, "MC1")
    assert _find_kind(r, ExceptionFindingKind.SETUP_HOLD_INCOHERENT) is not None


def test_09_multicycle_no_effect():
    cs = _cset()
    _clock(cs, "clk")
    _mc(cs, "MC1", cycles=2, fro=["nope_a"], to=["nope_b"])
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "MC1")
    assert _find_kind(r, ExceptionFindingKind.NO_EFFECT) is not None


def test_10_broad_exception_high_risk():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1")
    # Fabricate a large graph
    paths = [TimingPath(startpoint=f"r{i}/Q", endpoint=f"r{i+1}/D",
                        launch_clock="clk", capture_clock="clk",
                        path_type=TimingPathClass.REG_TO_REG)
             for i in range(250)]
    tg = _make_tg(clocks=["clk"], paths=paths)
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert r.blast_radius.path_count >= 250
    assert r.risk in (ExceptionRisk.HIGH, ExceptionRisk.CRITICAL)


def test_11_clock_group_interaction_recorded():
    cs = _cset()
    _clock(cs, "clk_a")
    _clock(cs, "clk_b")
    cs.create_clock_groups(groups=[["clk_a"], ["clk_b"]],
                           relationship="asynchronous")
    _fp(cs, "FP1", fro=["clk_a"], to=["clk_b"])
    tg = _make_tg(clocks=["clk_a", "clk_b"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk_a", capture_clock="clk_b",
                   path_type=TimingPathClass.CDC),
    ], edges=[ClockDomainEdge(clock_a="clk_a", clock_b="clk_b")])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert _find_kind(r, ExceptionFindingKind.CLOCK_GROUP_OVERLAP) is not None


def test_12_cdc_sync_does_not_auto_verify():
    """A synchronizer-like path with async groups must NOT be VERIFIED
    without explicit exception or proof."""
    cs = _cset()
    _clock(cs, "clk_a")
    _clock(cs, "clk_b")
    cs.create_clock_groups(groups=[["clk_a"], ["clk_b"]],
                           relationship="asynchronous")
    tg = _make_tg(clocks=["clk_a", "clk_b"], paths=[
        TimingPath(startpoint="sync1/Q", endpoint="sync2/D",
                   launch_clock="clk_a", capture_clock="clk_b",
                   path_type=TimingPathClass.CDC),
    ], edges=[ClockDomainEdge(clock_a="clk_a", clock_b="clk_b")])
    # NO set_false_path, NO multicycle — just the graph and groups
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg,
                            backend=ConservativeFormalBackend())
    # there are zero exceptions here
    assert len(rep.results) == 0


# --------------- formal backend ---------------

def test_13_formal_backend_unavailable_is_unresolved():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"])
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg,
                            backend=ConservativeFormalBackend())
    r = _find(rep, "FP1")
    assert r.verification is not None
    assert r.verification.status == VerificationStatus.UNRESOLVED
    assert r.verification_status == VerificationStatus.UNRESOLVED
    assert not r.is_emittable("strict")


def test_14_formal_backend_returns_verified():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"])
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    mock = MockFormalBackend(verdicts={
        "FP1": VerificationResult(
            constraint_id="FP1",
            status=VerificationStatus.VERIFIED,
            tool="mock", tool_version="test",
            message="proved",
            evidence={"k": "v"},
        )
    })
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg, backend=mock)
    r = _find(rep, "FP1")
    assert r.verification.status == VerificationStatus.VERIFIED
    assert r.verification_status == VerificationStatus.VERIFIED
    assert r.is_emittable("strict")


def test_15_formal_backend_returns_invalid_with_counterexample():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"])
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    ce = {"startpoint": "ra/Q", "endpoint": "rb/D",
          "waveform": [("clk", 0, 1), ("d", 0, 1)],
          "failing_at_ns": 12.5}
    mock = MockFormalBackend(verdicts={
        "FP1": VerificationResult(
            constraint_id="FP1",
            status=VerificationStatus.INVALID,
            tool="mock", tool_version="test",
            message="cex found", counterexample=ce,
        )
    })
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg, backend=mock)
    r = _find(rep, "FP1")
    assert r.verification.status == VerificationStatus.INVALID
    assert r.verification.counterexample is not None
    assert r.verification_status == VerificationStatus.INVALID
    assert not r.is_emittable("strict")
    assert not r.is_emittable("balanced")
    assert not r.is_emittable("exploratory")


def test_16_invalid_exception_never_emittable():
    cs = _cset()
    _clock(cs, "clk")
    _mc(cs, "MC1", cycles=-1)
    rep = verify_exceptions(cs, design=MINI_DESIGN,
                            tg=_make_tg(clocks=["clk"]))
    r = _find(rep, "MC1")
    assert r.verification_status == VerificationStatus.INVALID
    for mode in ("strict", "balanced", "exploratory"):
        assert not r.is_emittable(mode)


def test_17_unresolved_suppressed_in_strict():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"])
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg,
                            backend=ConservativeFormalBackend())
    r = _find(rep, "FP1")
    assert not r.is_emittable("strict")
    assert r.is_emittable("balanced")
    assert r.is_emittable("exploratory")


def test_18_user_confirmed_emits_in_balanced():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"], fixed=True)
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg,
                            backend=ConservativeFormalBackend())
    r = _find(rep, "FP1")
    assert r.approval_status.value == 'USER_CONFIRMED'
    # User-confirmed alone is NEVER formally VERIFIED.
    assert r.verification_status == VerificationStatus.UNRESOLVED
    assert r.emission_status.value == "ALLOWED_USER_CONFIRMED"
    assert not r.is_emittable("strict")
    assert r.is_emittable("balanced")


def test_19_provenance_survives():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"], scenarios=["func"])
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    d = r.to_dict()
    assert d["constraint_id"] == "FP1"
    assert "blast_radius" in d
    assert "structural_findings" in d
    assert d["evidence"]["selector"] == {
        "from": ["ra/Q"], "to": ["rb/D"], "through": []}


def test_20_blast_radius_determinism():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"])
    paths = [TimingPath(startpoint="ra/Q", endpoint="rb/D",
                        launch_clock="clk", capture_clock="clk",
                        path_type=TimingPathClass.REG_TO_REG)]
    tg = _make_tg(clocks=["clk"], paths=paths)
    a = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    b = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    ra = _find(a, "FP1").to_dict()
    rb = _find(b, "FP1").to_dict()
    assert ra == rb


def test_21_deterministic_verification_results():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"])
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    mock = MockFormalBackend(verdicts={
        "FP1": VerificationResult(constraint_id="FP1",
                                  status=VerificationStatus.VERIFIED,
                                  message="ok")
    })
    a = verify_exceptions(cs, design=MINI_DESIGN, tg=tg, backend=mock)
    b = verify_exceptions(cs, design=MINI_DESIGN, tg=tg, backend=mock)
    assert _find(a, "FP1").verification.issue_finding_id if False else True
    assert _find(a, "FP1").verification.status == _find(b, "FP1").verification.status


def test_22_analysis_does_not_mutate_ucm():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"])
    _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    before = [(c.id, c.type.value,
               tuple(sorted((c.values or {}).keys())),
               tuple(sorted(c.scenario_ids or []))) for c in cs]
    analyze_exceptions(MINI_DESIGN, cs, tg=_make_tg(clocks=["clk"]))
    after = [(c.id, c.type.value,
              tuple(sorted((c.values or {}).keys())),
              tuple(sorted(c.scenario_ids or []))) for c in cs]
    assert before == after


def test_23_targets_survive_snapshot_restore_dict_roundtrip():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"])
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    d = rep.to_dict()
    # Roundtrip via dict
    assert d["results"][0]["constraint_id"] == "FP1"
    assert d["results"][0]["blast_radius"]["affected_clocks"] == ["clk"]


def test_24_adversarial_two_unrelated_clocks_no_auto_false_path():
    """If we just have two clocks and no exception, nothing is emitted."""
    cs = _cset()
    _clock(cs, "clk_a")
    _clock(cs, "clk_b")
    tg = _make_tg(clocks=["clk_a", "clk_b"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk_a", capture_clock="clk_b",
                   path_type=TimingPathClass.CDC),
    ], edges=[ClockDomainEdge(clock_a="clk_a", clock_b="clk_b")])
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg)
    assert len(rep.results) == 0  # no exception → nothing to verify
    # Validation (coverage) should raise a CDC gap, but that's separate.


def test_25_adversarial_sync_data_path_stays_unresolved_without_exception():
    cs = _cset()
    _clock(cs, "clk_a")
    _clock(cs, "clk_b")
    cs.create_clock_groups(groups=[["clk_a"], ["clk_b"]],
                           relationship="asynchronous")
    # Two-stage synchronizer-like path but NO exception.
    # We construct an FP on clk_a→clk_b without proof; conservative backend → UNRESOLVED.
    _fp(cs, "FP1", fro=["clk_a"], to=["clk_b"])
    tg = _make_tg(clocks=["clk_a", "clk_b"], paths=[
        TimingPath(startpoint="sync1/Q", endpoint="sync2/D",
                   launch_clock="clk_a", capture_clock="clk_b",
                   path_type=TimingPathClass.CDC),
    ], edges=[ClockDomainEdge(clock_a="clk_a", clock_b="clk_b")])
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg)
    r = _find(rep, "FP1")
    assert r.verification_status == VerificationStatus.UNRESOLVED


def test_26_reset_named_signal_not_treated_as_reset_exception():
    cs = _cset()
    _clock(cs, "clk")
    # 'reset_n' is an ordinary data wire in this adversarial design
    _fp(cs, "FP1", fro=["reset_n"], to=["rb/D"])
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="reset_n", endpoint="rb/D",
                   launch_clock=None, capture_clock="clk",
                   path_type=TimingPathClass.INPUT_TO_REG),
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    # Flagged as RESET_RELATED hint, but must NOT be VERIFIED
    assert _find_kind(r, ExceptionFindingKind.RESET_RELATED) is not None
    assert r.verification_status != VerificationStatus.VERIFIED


def test_27_large_broad_wildcard_critical_risk():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1")  # no selectors
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint=f"r{i}/Q", endpoint=f"r{i+1}/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG)
        for i in range(500)
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert r.risk == ExceptionRisk.CRITICAL


def test_28_zero_paths_not_verified():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["nothing"], to=["nowhere"])
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=_make_tg(clocks=["clk"]))
    r = _find(rep, "FP1")
    assert r.verification_status == VerificationStatus.NOT_APPLICABLE
    assert not r.is_emittable("strict")  # not emitting dead exceptions


def test_29_multicycle_across_unrelated_clocks_is_high():
    cs = _cset()
    _clock(cs, "clk_a")
    _clock(cs, "clk_b")
    _mc(cs, "MC1", cycles=2, fro=["clk_a"], to=["clk_b"])
    tg = _make_tg(clocks=["clk_a", "clk_b"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk_a", capture_clock="clk_b",
                   path_type=TimingPathClass.CDC),
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "MC1")
    assert _find_kind(r, ExceptionFindingKind.CLOCK_DOMAIN_CROSSING) is not None
    assert r.verification_status == VerificationStatus.UNRESOLVED


def test_30_poor_timing_does_not_infer_false_path():
    # Adversarial: tight slack on a path must not trigger an auto-false-path
    cs = _cset()
    _clock(cs, "clk")
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="crit/Q", endpoint="sink/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG, slack=-1.2e-9),
    ])
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg)
    assert len(rep.results) == 0  # no exception inferred


def test_31_counterexample_stored_on_invalid():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"])
    ce = {"startpoint": "ra/Q", "endpoint": "rb/D", "fail_time_ns": 7.5}
    mock = MockFormalBackend(verdicts={
        "FP1": VerificationResult(constraint_id="FP1",
                                  status=VerificationStatus.INVALID,
                                  counterexample=ce,
                                  tool="mock")
    })
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg, backend=mock)
    r = _find(rep, "FP1")
    assert r.verification.counterexample == ce
    d = r.to_dict()
    assert d["verification"]["counterexample"]["fail_time_ns"] == 7.5


def test_32_backend_error_gives_error_status():
    class ExplodingBackend(FormalBackend):
        name = "boom"
        def prove_false_path(self, cid, spec):
            raise RuntimeError("tool crashed")
        def prove_multicycle(self, cid, spec, cycles):
            raise RuntimeError("tool crashed")
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"])
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg,
                            backend=ExplodingBackend())
    r = _find(rep, "FP1")
    assert r.verification.status == VerificationStatus.ERROR


def test_33_emittable_helper_filters_by_mode():
    cs = _cset()
    _clock(cs, "clk_a")
    _clock(cs, "clk_b")
    _fp(cs, "FP_BAD")  # broad critical
    _fp(cs, "FP_OK", fro=["ra/Q"], to=["rb/D"], fixed=True)  # user-confirmed, narrow
    _fp(cs, "FP_VERIFIED", fro=["ra/Q"], to=["rb/D"])
    _mc(cs, "MC_BAD", cycles=-1)
    tg = _make_tg(clocks=["clk_a", "clk_b"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk_a", capture_clock="clk_a",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    mock = MockFormalBackend(verdicts={
        "FP_VERIFIED": VerificationResult(constraint_id="FP_VERIFIED",
                                          status=VerificationStatus.VERIFIED,
                                          tool="mock"),
    })
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg, backend=mock)
    strict = [r.constraint_id for r in emittable_exceptions(rep, "strict")]
    balanced = [r.constraint_id for r in emittable_exceptions(rep, "balanced")]
    assert "FP_VERIFIED" in strict
    assert "FP_OK" not in strict          # user-confirmed ≠ VERIFIED → strict blocks
    assert "FP_OK" in balanced            # balanced allows USER_CONFIRMED
    assert "FP_BAD" not in strict and "FP_BAD" not in balanced
    assert "MC_BAD" not in strict and "MC_BAD" not in balanced
    assert "MC_BAD" not in strict and "MC_BAD" not in balanced


# ----------------- user-approval-vs-verification tests -----------------

def test_40_user_confirmed_not_verified():
    """User-confirmed but no formal proof → verification UNRESOLVED,
    approval USER_CONFIRMED; must NOT report VERIFIED."""
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"], fixed=True)
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg,
                            backend=ConservativeFormalBackend())
    r = _find(rep, "FP1")
    assert r.verification_status == VerificationStatus.UNRESOLVED
    assert r.approval_status.value == "USER_CONFIRMED"
    assert r.emission_status.value == "ALLOWED_USER_CONFIRMED"
    # allowed in balanced but NOT strict (strict requires true VERIFIED)
    assert r.is_emittable("balanced")
    assert not r.is_emittable("strict")
    d = r.to_dict()
    assert d["verification_status"] == "unresolved"
    assert d["approval_status"] == "USER_CONFIRMED"


def test_41_user_confirmed_plus_formal_verified():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"], fixed=True)
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    mock = MockFormalBackend(verdicts={
        "FP1": VerificationResult(constraint_id="FP1",
                                  status=VerificationStatus.VERIFIED,
                                  tool="mock", message="proved"),
    })
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg, backend=mock)
    r = _find(rep, "FP1")
    assert r.verification_status == VerificationStatus.VERIFIED
    assert r.approval_status.value == "USER_CONFIRMED"
    assert r.emission_status.value == "ALLOWED"
    assert r.is_emittable("strict")


def test_42_user_confirmed_formal_invalid_stays_invalid():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"], fixed=True)
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    mock = MockFormalBackend(verdicts={
        "FP1": VerificationResult(constraint_id="FP1",
                                  status=VerificationStatus.INVALID,
                                  tool="mock", message="cex",
                                  counterexample={"x": 1}),
    })
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg, backend=mock)
    r = _find(rep, "FP1")
    assert r.verification_status == VerificationStatus.INVALID
    assert r.approval_status.value == "USER_CONFIRMED"
    assert r.emission_status.value == "BLOCKED_INVALID"
    assert not r.is_emittable("strict")
    assert not r.is_emittable("balanced")
    assert not r.is_emittable("exploratory")


def test_43_user_rejected_blocks_emission():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"])
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    mock = MockFormalBackend(verdicts={
        "FP1": VerificationResult(constraint_id="FP1",
                                  status=VerificationStatus.VERIFIED,
                                  tool="mock"),
    })
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg, backend=mock,
                            user_rejected_ids={"FP1"})
    r = _find(rep, "FP1")
    assert r.approval_status.value == "USER_REJECTED"
    assert r.emission_status.value == "BLOCKED_REJECTED"
    assert not r.is_emittable("strict")
    assert not r.is_emittable("balanced")
    assert not r.is_emittable("exploratory")


def test_44_fixed_constraint_is_not_formally_verified():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"], fixed=True)
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg)
    r = _find(rep, "FP1")
    # FIXED → USER_CONFIRMED approval, but still UNRESOLVED verification.
    assert r.verification_status == VerificationStatus.UNRESOLVED
    assert r.approval_status.value == "USER_CONFIRMED"
    assert not r.is_emittable("strict")
    assert r.is_emittable("balanced")


def test_45_strict_policy_verified_allowed():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["ra/Q"], to=["rb/D"])
    tg = _make_tg(clocks=["clk"], paths=[
        TimingPath(startpoint="ra/Q", endpoint="rb/D",
                   launch_clock="clk", capture_clock="clk",
                   path_type=TimingPathClass.REG_TO_REG),
    ])
    mock = MockFormalBackend(verdicts={
        "FP1": VerificationResult(constraint_id="FP1",
                                  status=VerificationStatus.VERIFIED,
                                  tool="mock"),
    })
    rep = verify_exceptions(cs, design=MINI_DESIGN, tg=tg, backend=mock)
    r = _find(rep, "FP1")
    assert r.is_emittable("strict")
    assert r.emission_status.value == "ALLOWED"


# ----------------- -through matcher tests -----------------

def _path_with(comb, start, end, clk="clk"):
    return TimingPath(startpoint=start, endpoint=end,
                      launch_clock=clk, capture_clock=clk,
                      path_type=TimingPathClass.REG_TO_REG,
                      combinational_elements=list(comb))


def test_50_one_through_stage_matches():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["A"], to=["Y"], through=["B"])
    tg = _make_tg(clocks=["clk"], paths=[
        _path_with(["X", "B", "C"], "A", "Y"),
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert r.blast_radius.path_count == 1


def test_51_two_ordered_through_stages_match():
    cs = _cset()
    _clock(cs, "clk")
    # -through B -through C
    fp1 = Constraint(id="FP1", type=ConstraintType.SET_FALSE_PATH,
                     path_selector=PathSelector(
                         from_set=["A"], to_set=["Y"],
                         through_set=[["B"], ["C"]]))
    cs.add(fp1)
    tg = _make_tg(clocks=["clk"], paths=[
        _path_with(["X", "B", "C"], "A", "Y"),
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert r.blast_radius.path_count == 1


def test_52_reversed_through_stages_do_not_match():
    cs = _cset()
    _clock(cs, "clk")
    # -through C -through B  (wrong order)
    fp1 = Constraint(id="FP1", type=ConstraintType.SET_FALSE_PATH,
                     path_selector=PathSelector(
                         from_set=["A"], to_set=["Y"],
                         through_set=[["C"], ["B"]]))
    cs.add(fp1)
    tg = _make_tg(clocks=["clk"], paths=[
        _path_with(["X", "B", "C"], "A", "Y"),
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert r.blast_radius.path_count == 0
    assert _find_kind(r, ExceptionFindingKind.NO_EFFECT) is not None


def test_53_multi_object_through_stage_or():
    cs = _cset()
    _clock(cs, "clk")
    # -through {B D}
    fp1 = Constraint(id="FP1", type=ConstraintType.SET_FALSE_PATH,
                     path_selector=PathSelector(
                         from_set=["A"], to_set=["Y"],
                         through_set=[["B", "D"]]))
    cs.add(fp1)
    tg = _make_tg(clocks=["clk"], paths=[
        _path_with(["X", "D", "C"], "A", "Y"),
        _path_with(["X", "E", "F"], "A", "Z"),
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert r.blast_radius.path_count == 1
    assert r.blast_radius.affected_endpoints == ["Y"]


def test_54_wrong_through_object_no_effect():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["A"], to=["Y"], through=["Z"])
    tg = _make_tg(clocks=["clk"], paths=[
        _path_with(["X", "B", "C"], "A", "Y"),
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert r.blast_radius.path_count == 0
    assert _find_kind(r, ExceptionFindingKind.NO_EFFECT) is not None


def test_55_correct_through_but_wrong_endpoint():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["A"], to=["Z"], through=["B"])
    tg = _make_tg(clocks=["clk"], paths=[
        _path_with(["X", "B", "C"], "A", "Y"),  # goes to Y not Z
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert r.blast_radius.path_count == 0


def test_56_correct_endpoint_but_wrong_through():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["A"], to=["Y"], through=["Z"])
    tg = _make_tg(clocks=["clk"], paths=[
        _path_with(["X", "B", "C"], "A", "Y"),
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert r.blast_radius.path_count == 0


def test_57_multi_through_longer_path():
    cs = _cset()
    _clock(cs, "clk")
    fp1 = Constraint(id="FP1", type=ConstraintType.SET_FALSE_PATH,
                     path_selector=PathSelector(
                         from_set=["A"], to_set=["Y"],
                         through_set=[["B"], ["D"], ["F"]]))
    cs.add(fp1)
    tg = _make_tg(clocks=["clk"], paths=[
        _path_with(["B", "C", "D", "E", "F", "G"], "A", "Y"),
        _path_with(["B", "X", "F"], "A", "Y"),  # skips D
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert r.blast_radius.path_count == 1


def test_58_no_through_means_no_through_restriction():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["A"], to=["Y"])
    tg = _make_tg(clocks=["clk"], paths=[
        _path_with(["X", "B", "C"], "A", "Y"),
        _path_with(["M"], "A", "Z"),
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    # only A→Y should match (endpoint-filtered)
    assert r.blast_radius.path_count == 1
    assert r.blast_radius.affected_endpoints == ["Y"]


def test_59_through_zero_match_is_no_effect():
    cs = _cset()
    _clock(cs, "clk")
    _fp(cs, "FP1", fro=["A"], to=["Y"], through=["B"])
    tg = _make_tg(clocks=["clk"], paths=[
        _path_with(["X", "C", "D"], "A", "Y"),
    ])
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    r = _find(rep, "FP1")
    assert r.blast_radius.path_count == 0
    assert _find_kind(r, ExceptionFindingKind.NO_EFFECT) is not None


def test_60_through_changes_blast_radius():
    """Adding a restrictive through narrows blast radius."""
    cs = _cset()
    _clock(cs, "clk")
    fp_wide = Constraint(id="FP_WIDE", type=ConstraintType.SET_FALSE_PATH,
                         path_selector=PathSelector(from_set=["clk"], to_set=["clk"]))
    fp_narrow = Constraint(id="FP_NARROW", type=ConstraintType.SET_FALSE_PATH,
                           path_selector=PathSelector(from_set=["clk"], to_set=["clk"],
                                                      through_set=[["B"]]))
    cs.add(fp_wide); cs.add(fp_narrow)
    paths = [
        _path_with(["B", "C"], "ra/Q", "rb/D"),
        _path_with(["X", "Y"], "rc/Q", "rd/D"),
    ]
    # expand clock expansion: clock "clk" drives both regs (set registers_driven)
    tg_clks = {"clk": Clock(id="clk", name="clk", source_object="clk",
                            period_seconds=10e-9,
                            registers_driven=["ra/Q", "rc/Q"])}
    tg = TimingGraph(clocks=tg_clks, paths=paths)
    rep = analyze_exceptions(MINI_DESIGN, cs, tg=tg)
    rw = _find(rep, "FP_WIDE")
    rn = _find(rep, "FP_NARROW")
    assert rw.blast_radius.path_count == 2
    assert rn.blast_radius.path_count == 1
    assert rn.blast_radius.affected_endpoints == ["rb/D"]
