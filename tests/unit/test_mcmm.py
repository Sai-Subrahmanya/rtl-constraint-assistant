"""
MCMM (Multi-Mode / Multi-Corner) tests — Step 12.

Covers the 30 required cases (Step 12 §17).  All tests use the deterministic
mock backend (clearly labelled) and run offline.  No real EDA is invoked.
"""
import copy
import pytest
from pathlib import Path

from rca.constraint_model import Constraint, ConstraintSet, Scenario
from rca.constraint_model.constraint import Constraint as _C
from rca.config.model import (
    MCMMConfig, OptimizationConfig, OptimizationPerturbation,
    OptimizationThresholds, ProjectConfig, ProjectInfo, ScenarioSpec,
    FlowConfig,
)
from rca.mcmm import (
    MCMMEvaluator, MCMMResult, ObjectiveAggregate, ScenarioMatrix,
    ScenarioQoR, aggregate_objectives, build_scenario_matrix,
    finalize_limiting, global_feasibility, global_margin, mock_mcmm_evaluator,
    mcmm_is_dominating, mcmm_pareto_front, mcmm_scalar_score,
    mcmm_select_final, scenario_cache_key, scenario_semantic_key,
)
from rca.optimizer import Candidate, Optimizer, generate_candidates
from rca.qor.model import QoRResult
from rca.sdc import get_backend
from rca.utils.enums import (
    CandidateDecision, Confidence, ConstraintStatus, ConstraintType,
    OptimizationStatus, PowerStatus, Priority, SourceKind,
)
from rca.utils.enums import SafeMode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(*, enabled=True, active=None, scenarios=None):
    if scenarios is None:
        scs = [
            ScenarioSpec(id="S1", mode="functional", corner="slow"),
            ScenarioSpec(id="S2", mode="functional", corner="fast"),
        ]
    else:
        scs = scenarios
    return ProjectConfig(
        project=ProjectInfo(name="m", top="m"),
        scenarios=scs,
        mcmm=MCMMConfig(enabled=enabled, active_scenario_ids=list(active or [])),
    )


def _cset(scenarios=None):
    cs = ConstraintSet(name="m")
    defs = scenarios or {
        "S1": ("functional", "slow"),
        "S2": ("functional", "fast"),
    }
    for sid, (mode, corner) in defs.items():
        cs.scenarios[sid] = Scenario(id=sid, mode=mode, corner=corner)
    return cs


def _qor(*, wns_ns=0.5, hold_ns=0.2, area=None, area_proxy=None, power=None,
         setup_tns=None, hold_tns=None, cq=None, tool="mock", notes=None,
         validation_errors=0, unsafe_exceptions=0, scenario="S1",
         corner="slow", mode="functional", run_id=""):
    setup_ok = wns_ns is not None and wns_ns >= 0
    hold_ok = hold_ns is not None and hold_ns >= 0
    return QoRResult(
        backend="mock", tool=tool, tool_version="0.1", is_mock=True,
        flow_stage="synthesis_sta", scenario=scenario, mode=mode, corner=corner,
        setup_wns=wns_ns * 1e-9 if wns_ns is not None else None,
        hold_wns=hold_ns * 1e-9 if hold_ns is not None else None,
        setup_tns=((setup_tns if setup_tns is not None else 0.0) * 1e-9
                   if wns_ns is not None else None),
        hold_tns=((hold_tns if hold_tns is not None else 0.0) * 1e-9
                  if hold_ns is not None else None),
        setup_violations=0 if setup_ok else 1,
        hold_violations=0 if hold_ok else 1,
        area=area, area_total=area, area_proxy=area_proxy,
        power=power,
        power_status=(PowerStatus.AVAILABLE.value if power is not None
                      else PowerStatus.UNAVAILABLE.value),
        constraint_quality=cq,
        validation_errors=validation_errors,
        unsafe_exceptions=unsafe_exceptions,
        cell_count=50, ff_count=25,
        run_id=run_id,
        notes=list(notes or []),
    )


def _sqor(sid, **qkw):
    qkw.setdefault("scenario", sid)
    q = _qor(**qkw)
    return ScenarioQoR(
        candidate_id="C0", scenario_id=sid, mode=qkw.get("mode", "functional"),
        corner=qkw.get("corner", "slow"), name=f"{qkw.get('mode','functional')}_{qkw.get('corner','slow')}",
        qor=q, backend="mock", tool="mock", tool_version="0.1",
    )


def _result(scenarios_qoR, *, candidate_id="C0", active=None):
    active = active or list(scenarios_qoR.keys())
    r = MCMMResult(candidate_id=candidate_id, active_scenario_ids=active)
    for sid, sq in scenarios_qoR.items():
        r.scenario_results[sid] = sq
    return r


def _make(sid, **qkw):
    return _sqor(sid, **qkw)


# ---------------------------------------------------------------------------
# 1. scenario matrix creation
# ---------------------------------------------------------------------------

def test_scenario_matrix_creation():
    cfg = _cfg(enabled=True)
    cs = _cset()
    mat = build_scenario_matrix(cfg, cs)
    assert isinstance(mat, ScenarioMatrix)
    assert mat.is_enabled
    assert mat.active_ids == ["S1", "S2"]
    assert mat.scenario_count == 2
    assert not mat.single_scenario


def test_scenario_matrix_uses_config_definitions_when_cset_empty():
    cfg = _cfg(enabled=True, scenarios=[
        ScenarioSpec(id="A", mode="functional", corner="slow"),
        ScenarioSpec(id="B", mode="test", corner="fast"),
    ])
    cs = ConstraintSet(name="m")  # no scenarios attached
    mat = build_scenario_matrix(cfg, cs)
    assert mat.active_ids == ["A", "B"]
    assert mat.scenario("A").mode == "functional"
    assert mat.scenario("B").corner == "fast"


# ---------------------------------------------------------------------------
# 2. single-scenario backward compatibility
# ---------------------------------------------------------------------------

def test_single_scenario_backward_compat():
    cfg = _cfg(enabled=True, active=["S1"])
    cs = _cset()
    mat = build_scenario_matrix(cfg, cs)
    assert mat.single_scenario
    assert mat.active_ids == ["S1"]
    assert mat.scenario_count == 1


def test_disabled_mcmm_legacy_single_scenario():
    cfg = _cfg(enabled=False)
    cs = _cset()
    mat = build_scenario_matrix(cfg, cs)
    assert mat.single_scenario
    assert not mat.is_enabled


# ---------------------------------------------------------------------------
# 3. multiple modes
# ---------------------------------------------------------------------------

def test_multiple_modes():
    cfg = _cfg(enabled=True, scenarios=[
        ScenarioSpec(id="F", mode="functional", corner="slow"),
        ScenarioSpec(id="T", mode="test", corner="slow"),
    ])
    cs = ConstraintSet(name="m")
    mat = build_scenario_matrix(cfg, cs)
    modes = {s.mode for s in mat.active_scenarios()}
    assert modes == {"functional", "test"}


# ---------------------------------------------------------------------------
# 4. multiple corners
# ---------------------------------------------------------------------------

def test_multiple_corners():
    cfg = _cfg(enabled=True, scenarios=[
        ScenarioSpec(id="SLOW", mode="functional", corner="slow"),
        ScenarioSpec(id="FAST", mode="functional", corner="fast"),
    ])
    cs = ConstraintSet(name="m")
    mat = build_scenario_matrix(cfg, cs)
    corners = {s.corner for s in mat.active_scenarios()}
    assert corners == {"slow", "fast"}


# ---------------------------------------------------------------------------
# 5. scenario-specific constraints
# ---------------------------------------------------------------------------

def test_scenario_specific_constraints():
    cs = _cset()
    c = Constraint(id="U1", type=ConstraintType.SET_CLOCK_UNCERTAINTY,
                   target_objects=["clk"], clock_refs=["clk"],
                   values={"uncertainty": 0.1e-9}, scenario_ids=["S1"])
    cs.add(c)
    mat = build_scenario_matrix(_cfg(enabled=True), cs)
    assert mat.constraint_applies_to(c, "S1")
    assert not mat.constraint_applies_to(c, "S2")
    # clone preserves scenario_ids
    clone = cs.clone()
    c2 = clone.get("U1")
    assert c2.scenario_ids == ["S1"]


# ---------------------------------------------------------------------------
# 6. all-scenario constraints
# ---------------------------------------------------------------------------

def test_all_scenario_constraints():
    cs = _cset()
    c = Constraint(id="U1", type=ConstraintType.SET_CLOCK_UNCERTAINTY,
                   target_objects=["clk"], clock_refs=["clk"],
                   values={"uncertainty": 0.1e-9})  # empty scenario_ids
    cs.add(c)
    mat = build_scenario_matrix(_cfg(enabled=True), cs)
    assert mat.constraint_applies_to(c, "S1")
    assert mat.constraint_applies_to(c, "S2")
    assert mat.all_scenario_ids_for_constraint(c) == ["S1", "S2"]


# ---------------------------------------------------------------------------
# 7. per-candidate evaluation of every scenario
# ---------------------------------------------------------------------------

def test_per_candidate_evaluation_of_every_scenario():
    cs = _cset()
    mat = build_scenario_matrix(_cfg(enabled=True), cs)
    eval_calls = []

    def evaluate_scenario(scenario, cand, work_dir):
        eval_calls.append(scenario.id)
        return _qor(scenario=scenario.id, mode=scenario.mode, corner=scenario.corner)

    ev = MCMMEvaluator(mat, evaluate_scenario=evaluate_scenario, base_cset=cs,
                       name="mock")
    cand = Candidate(id="C001", constraint_set=cs)
    res = ev(cand, Path("/tmp/mcmm"))
    assert set(eval_calls) == {"S1", "S2"}
    assert res.active_scenario_ids == ["S1", "S2"]
    assert set(res.scenario_results.keys()) == {"S1", "S2"}
    # per-scenario records are retained, not collapsed
    assert res.scenario_results["S1"].qor.setup_wns is not None
    assert res.scenario_results["S2"].qor.setup_wns is not None


# ---------------------------------------------------------------------------
# 8. one scenario infeasible => global infeasible
# ---------------------------------------------------------------------------

def test_one_scenario_infeasible_global_infeasible():
    r = _result({
        "S1": _make("S1", wns_ns=0.5, hold_ns=0.2),
        "S2": _make("S2", wns_ns=-0.1, hold_ns=0.2),
    })
    global_feasibility(r)
    assert not r.feasible
    assert r.infeasible
    assert not r.blocked
    assert r.global_status == "infeasible"
    assert r.limiting_scenarios == ["S2"]
    assert r.scenario_results["S2"].infeasible_reason == "setup_violation"


# ---------------------------------------------------------------------------
# 9. blocked scenario => correct global status
# ---------------------------------------------------------------------------

def test_blocked_scenario_global_blocked():
    r = _result({
        "S1": _make("S1", wns_ns=0.5, hold_ns=0.2),
        "S2": _make("S2", ),  # default wns positive
    })
    # Force blocking by clearing QoR (blocked_no_qor).
    r.scenario_results["S2"].qor = None
    global_feasibility(r)
    assert not r.feasible
    assert r.blocked
    assert r.global_status == "blocked"
    assert r.limiting_scenarios == ["S2"]


def test_invalid_scenario_global_invalid():
    r = _result({
        "S1": _make("S1", wns_ns=0.5, hold_ns=0.2),
        "S2": _make("S2", wns_ns=0.5, hold_ns=0.2, validation_errors=1),
    })
    global_feasibility(r)
    assert r.global_status == "invalid"
    assert r.invalid
    assert not r.blocked
    assert r.limiting_scenarios == ["S2"]


# ---------------------------------------------------------------------------
# 10. per-scenario diagnostics
# ---------------------------------------------------------------------------

def test_per_scenario_diagnostics():
    cs = _cset()
    mat = build_scenario_matrix(_cfg(enabled=True), cs)

    def evaluate_scenario(scenario, cand, work_dir):
        return _qor(scenario=scenario.id, mode=scenario.mode, corner=scenario.corner,
                    notes=["note-a", "note-b"])

    ev = MCMMEvaluator(mat, evaluate_scenario=evaluate_scenario, base_cset=cs,
                       name="mock")
    res = ev(Candidate(id="C1", constraint_set=cs), Path("/tmp/mcmm"))
    sq = res.scenario_results["S1"]
    assert "scenario=S1" in " ".join(sq.diagnostics)
    assert res.diagnostics  # global diagnostics present


# ---------------------------------------------------------------------------
# 11. limiting scenario selection
# ---------------------------------------------------------------------------

def test_limiting_scenario_selection():
    r = _result({
        "S1": _make("S1", wns_ns=0.9, hold_ns=0.2),
        "S2": _make("S2", wns_ns=0.3, hold_ns=0.2),  # binding (worst setup)
    })
    global_feasibility(r)
    aggregate_objectives(r)
    global_margin(r)
    finalize_limiting(r)
    # setup_wns binding is MIN over scenarios = S2 (0.3)
    assert r.objectives["setup_wns"].limiting == ["S2"]
    assert "S2" in r.limiting_scenarios  # S2 is the objective-level limiting scenario


# ---------------------------------------------------------------------------
# 12. UNKNOWN metrics
# ---------------------------------------------------------------------------

def test_unknown_setup_wns_stays_unknown():
    r = _result({
        "S1": _make("S1", wns_ns=None, hold_ns=0.2),
        "S2": _make("S2", wns_ns=0.5, hold_ns=0.2),
    })
    aggregate_objectives(r)
    agg = r.objectives["setup_wns"]
    assert agg.unknown
    assert agg.value is None
    assert agg.limiting == ["S1"]


# ---------------------------------------------------------------------------
# 13. real/proxy area incomparability
# ---------------------------------------------------------------------------

def test_real_proxy_area_incomparable():
    r = _result({
        "S1": _make("S1", wns_ns=0.5, hold_ns=0.2, area=100.0, area_proxy=None),
        "S2": _make("S2", wns_ns=0.5, hold_ns=0.2, area=None, area_proxy=90.0),
    })
    aggregate_objectives(r)
    agg = r.objectives["area"]
    assert agg.incomparable
    assert agg.value is None
    assert agg.area_source == "mixed"


def test_all_real_area_comparable():
    r = _result({
        "S1": _make("S1", wns_ns=0.5, hold_ns=0.2, area=100.0, area_proxy=None),
        "S2": _make("S2", wns_ns=0.5, hold_ns=0.2, area=120.0, area_proxy=None),
    })
    aggregate_objectives(r)
    agg = r.objectives["area"]
    assert not agg.unknown and not agg.incomparable
    assert agg.value == pytest.approx(120.0)  # binding = max (minimize)
    assert agg.area_source == "real"
    assert agg.limiting == ["S2"]


# ---------------------------------------------------------------------------
# 14. UNKNOWN power
# ---------------------------------------------------------------------------

def test_unknown_power_retains_scenario():
    r = _result({
        "S1": _make("S1", wns_ns=0.5, hold_ns=0.2, power=None),
        "S2": _make("S2", wns_ns=0.5, hold_ns=0.2, power=None),
    })
    aggregate_objectives(r)
    agg = r.objectives["power"]
    assert agg.unknown
    assert agg.value is None
    assert set(agg.limiting) == {"S1", "S2"}  # retains responsible scenarios


def test_unknown_power_not_converted_to_zero():
    r = _result({
        "S1": _make("S1", wns_ns=0.5, hold_ns=0.2, power=None),
        "S2": _make("S2", wns_ns=0.5, hold_ns=0.2, power=10.0),
    })
    aggregate_objectives(r)
    agg = r.objectives["power"]
    # Mixed known/unknown => conservative UNKNOWN, never fabricate a value.
    assert agg.unknown
    assert agg.value is None
    assert agg.limiting == ["S1"]


# ---------------------------------------------------------------------------
# 15. scenario-aware cache identity
# ---------------------------------------------------------------------------

def test_scenario_cache_key_differs_by_corner():
    cs = _cset()
    s1 = Scenario(id="S1", mode="functional", corner="slow")
    s2 = Scenario(id="S2", mode="functional", corner="fast")
    k1 = scenario_cache_key(s1, cs, backend="mock")
    k2 = scenario_cache_key(s2, cs, backend="mock")
    assert k1 != k2


def test_scenario_cache_key_includes_environment_and_libraries():
    cs = _cset()
    s1 = Scenario(id="S1", mode="functional", corner="slow",
                  libraries=["lib1"], parasitics="rc1", environment={"t": "25"})
    s2 = Scenario(id="S1", mode="functional", corner="slow",
                  libraries=["lib2"], parasitics="rc1", environment={"t": "25"})
    assert scenario_cache_key(s1, cs) != scenario_cache_key(s2, cs)


# ---------------------------------------------------------------------------
# 16. cache isolation (semantically different scenarios never share)
# ---------------------------------------------------------------------------

def test_cache_isolation_same_cset_different_scenario():
    cs = _cset()
    keys = {
        scenario_cache_key(Scenario(id="FUNC", mode="functional", corner="slow"), cs),
        scenario_cache_key(Scenario(id="FUNC", mode="functional", corner="fast"), cs),
    }
    assert len(keys) == 2


def test_cache_isolation_identical_scenario_same_key():
    cs = _cset()
    a = Scenario(id="S", mode="functional", corner="slow")
    b = Scenario(id="S", mode="functional", corner="slow")
    assert scenario_cache_key(a, cs) == scenario_cache_key(b, cs)


# ---------------------------------------------------------------------------
# 17. deterministic MCMM evaluation
# ---------------------------------------------------------------------------

def test_mcmm_evaluation_deterministic():
    cs = _cset()
    mat = build_scenario_matrix(_cfg(enabled=True), cs)
    ev = mock_mcmm_evaluator(mat, base_cset=cs)
    r1 = ev(Candidate(id="C001", constraint_set=cs), Path("/tmp/mcmm"))
    r2 = ev(Candidate(id="C001", constraint_set=cs), Path("/tmp/mcmm"))
    assert r1.to_dict() == r2.to_dict()


# ---------------------------------------------------------------------------
# 18. fixed constraint immutability
# ---------------------------------------------------------------------------

def test_fixed_constraint_immutability_in_mcmm():
    cs = _cset()
    cs.create_clock(name="clk", period_seconds=10e-9, fixed=True,
                    source_kind=SourceKind.USER, confidence=Confidence.HIGH)
    u = cs.create_clock_uncertainty("clk", 0.05e-9, source_kind=SourceKind.INFERENCE)
    u.opt_status = OptimizationStatus.TUNABLE
    cfg = _cfg(enabled=True)
    cfg.optimization.enabled = True
    cfg.optimization.perturbation.uncertainty_range_ns = [0.0, 0.1, 0.02]
    base = Candidate(id="C000", constraint_set=cs)
    cands = generate_candidates(base, cs, cfg, max_candidates=10)
    for c in cands:
        for cc in c.constraint_set:
            if cc.type == ConstraintType.CREATE_CLOCK:
                assert cc.values.get("period") == 10e-9
        assert not any(cid.startswith("CLK") and "UNC" not in cid
                       for cid in c.mutated_constraint_ids)


# ---------------------------------------------------------------------------
# 19. one-mutation-per-candidate
# ---------------------------------------------------------------------------

def test_one_mutation_per_candidate():
    cs = _cset()
    cs.create_clock(name="clk", period_seconds=10e-9, fixed=True,
                    source_kind=SourceKind.USER, confidence=Confidence.HIGH)
    u1 = cs.create_clock_uncertainty("clk", 0.05e-9, source_kind=SourceKind.INFERENCE)
    u1.opt_status = OptimizationStatus.TUNABLE
    u2 = cs.create_clock_uncertainty("clk", 0.08e-9, source_kind=SourceKind.INFERENCE)
    u2.opt_status = OptimizationStatus.TUNABLE
    cfg = _cfg(enabled=True)
    cfg.optimization.enabled = True
    cfg.optimization.perturbation.uncertainty_range_ns = [0.0, 0.1, 0.02]
    base = Candidate(id="C000", constraint_set=cs)
    cands = generate_candidates(base, cs, cfg, max_candidates=20)
    for c in cands:
        assert len(c.mutated_constraint_ids) == 1
        # scenario definitions survive candidate cloning
        assert set(c.constraint_set.scenarios.keys()) == {"S1", "S2"}


# ---------------------------------------------------------------------------
# 20. Pareto correctness
# ---------------------------------------------------------------------------

def _mcmm_candidate(cid, by_scenario, *, hard=True):
    """Build a Candidate populated with an MCMMResult for Pareto tests."""
    import types
    mcmm = _result(by_scenario, candidate_id=cid)
    global_feasibility(mcmm)
    aggregate_objectives(mcmm)
    global_margin(mcmm)
    finalize_limiting(mcmm)
    c = Candidate(id=cid, decision=CandidateDecision.EVALUATED)
    c.mcmm = mcmm
    c.hard_feasible = hard
    c.blocked = False
    return c


def test_mcmm_pareto_not_superior_via_improving_one_scenario():
    # A improves scenario S1 but degrades scenario S2 vs B => neither dominates.
    a_ss = {"S1": _make("S1", wns_ns=0.9, hold_ns=0.2),
            "S2": _make("S2", wns_ns=0.2, hold_ns=0.2)}
    b_ss = {"S1": _make("S1", wns_ns=0.5, hold_ns=0.2),
            "S2": _make("S2", wns_ns=0.5, hold_ns=0.2)}
    a = _mcmm_candidate("A", a_ss)
    b = _mcmm_candidate("B", b_ss)
    # A dominates in S1 (0.9>0.5) but is worse in S2 (0.2<0.5)
    assert not mcmm_is_dominating(a, b)
    assert not mcmm_is_dominating(b, a)
    front = mcmm_pareto_front([a, b])
    assert a in front and b in front


def test_mcmm_pareto_dominates_all_scenarios():
    a_ss = {"S1": _make("S1", wns_ns=0.9, hold_ns=0.3, area=90.0, power=50.0),
            "S2": _make("S2", wns_ns=0.8, hold_ns=0.3, area=90.0, power=50.0)}
    b_ss = {"S1": _make("S1", wns_ns=0.5, hold_ns=0.2, area=120.0, power=80.0),
            "S2": _make("S2", wns_ns=0.4, hold_ns=0.2, area=120.0, power=80.0)}
    a = _mcmm_candidate("A", a_ss)
    b = _mcmm_candidate("B", b_ss)
    assert mcmm_is_dominating(a, b)
    assert not mcmm_is_dominating(b, a)
    front = mcmm_pareto_front([a, b])
    assert a in front and b not in front


# ---------------------------------------------------------------------------
# 21. final selection correctness
# ---------------------------------------------------------------------------

def test_mcmm_final_selection_prefers_better_global_timing():
    a = _mcmm_candidate("A", {"S1": _make("S1", wns_ns=0.9, hold_ns=0.3),
                              "S2": _make("S2", wns_ns=0.8, hold_ns=0.3)})
    b = _mcmm_candidate("B", {"S1": _make("S1", wns_ns=0.4, hold_ns=0.2),
                              "S2": _make("S2", wns_ns=0.4, hold_ns=0.2)})
    front = mcmm_pareto_front([a, b])
    priorities = {"timing": Priority.HIGH, "area": Priority.MEDIUM,
                  "power": Priority.MEDIUM}
    final = mcmm_select_final(front, a, priorities)
    assert final is a


def test_mcmm_select_final_uses_margin_tiebreak_when_equal():
    a = _mcmm_candidate("A", {"S1": _make("S1", wns_ns=0.5, hold_ns=0.2),
                              "S2": _make("S2", wns_ns=0.5, hold_ns=0.2)})
    b = _mcmm_candidate("B", {"S1": _make("S1", wns_ns=0.5, hold_ns=0.2),
                              "S2": _make("S2", wns_ns=0.5, hold_ns=0.2)})
    a.mcmm.margin_utilization = 0.1
    b.mcmm.margin_utilization = 0.9
    front = mcmm_pareto_front([a, b])
    priorities = {"timing": Priority.HIGH, "area": Priority.MEDIUM,
                  "power": Priority.MEDIUM}
    final = mcmm_select_final(front, a, priorities)
    # Lower utilization (residual slack) preferred as secondary signal.
    assert final is a


# ---------------------------------------------------------------------------
# 22. reporting
# ---------------------------------------------------------------------------

def test_mcmm_reporting_contains_all_fields():
    r = _result({
        "S1": _make("S1", wns_ns=0.5, hold_ns=0.2),
        "S2": _make("S2", wns_ns=0.5, hold_ns=0.2),
    })
    aggregate_objectives(r)
    global_margin(r)
    d = r.to_dict()
    for key in ("active_scenarios", "scenario_results", "objectives",
                "limiting_scenarios", "global_status", "margin_utilization",
                "cache_hits", "cache_misses", "eda_runs", "provenance"):
        assert key in d
    sq = r.scenario_results["S1"]
    for key in ("scenario_id", "mode", "corner", "qor", "feasible", "blocked",
                "cache_key", "cache_status", "run_id", "backend", "tool",
                "diagnostics", "provenance", "status", "infeasible_reason"):
        assert key in sq.to_dict()


# ---------------------------------------------------------------------------
# 23. provenance
# ---------------------------------------------------------------------------

def test_mcmm_provenance_attaches_scenario_identity():
    cs = _cset()
    mat = build_scenario_matrix(_cfg(enabled=True), cs)

    def evaluate_scenario(scenario, cand, work_dir):
        return _qor(scenario=scenario.id, mode=scenario.mode, corner=scenario.corner)

    ev = MCMMEvaluator(mat, evaluate_scenario=evaluate_scenario, base_cset=cs,
                       name="mock")
    res = ev(Candidate(id="C1", constraint_set=cs), Path("/tmp/mcmm"))
    p = res.scenario_results["S1"].provenance
    assert p["scenario_id"] == "S1"
    assert p["mode"] == "functional"
    assert p["corner"] == "slow"
    assert p["backend"] == "mock"
    assert res.provenance["active_scenarios"] == ["S1", "S2"]
    assert res.provenance["cset_hash"]


# ---------------------------------------------------------------------------
# 24. enable/disable MCMM
# ---------------------------------------------------------------------------

def test_enable_disable_mcmm():
    cs = _cset()
    on = build_scenario_matrix(_cfg(enabled=True), cs)
    off = build_scenario_matrix(_cfg(enabled=False), cs)
    assert on.is_enabled and not on.single_scenario
    assert not off.is_enabled and off.single_scenario


# ---------------------------------------------------------------------------
# 25. invalid/empty scenario configuration
# ---------------------------------------------------------------------------

def test_empty_scenario_configuration_blocked():
    cfg = _cfg(enabled=True, scenarios=[])
    cs = ConstraintSet(name="m")
    mat = build_scenario_matrix(cfg, cs)
    # No definitions -> legacy disabled default scenario
    assert not mat.is_enabled
    assert mat.single_scenario
    r = _result({}, active=[])
    global_feasibility(r)
    assert not r.feasible
    assert r.global_status == "blocked"
    assert r.global_reason == "no active scenarios"


def test_invalid_scenario_id_on_constraint_warns():
    cs = _cset()
    c = Constraint(id="U1", type=ConstraintType.SET_CLOCK_UNCERTAINTY,
                   target_objects=["clk"], clock_refs=["clk"],
                   values={"uncertainty": 0.1e-9}, scenario_ids=["NOPE"])
    cs.add(c)
    issues = cs.validate()
    assert any(i.code == "BAD_SCENARIO" for i in issues)


# ---------------------------------------------------------------------------
# 26. optimizer backward compatibility (MCMM disabled)
# ---------------------------------------------------------------------------

def test_optimizer_backward_compat_when_mcmm_disabled(tmp_path):
    cfg = _cfg(enabled=False)
    cfg.optimization.enabled = True
    cfg.optimization.max_iterations = 2
    cfg.optimization.max_eda_runs = 10
    cfg.optimization.perturbation.uncertainty_range_ns = [0.0, 0.1, 0.02]
    cs = _cset()
    cs.create_clock(name="clk", period_seconds=10e-9, fixed=True,
                    source_kind=SourceKind.USER, confidence=Confidence.HIGH)
    u = cs.create_clock_uncertainty("clk", 0.05e-9, source_kind=SourceKind.INFERENCE)
    u.opt_status = OptimizationStatus.TUNABLE

    def _eval(cand, work_dir):
        q = _qor(wns_ns=0.5, hold_ns=0.2, area=100.0)
        return {"qor": q, "cache_key": "K" + cand.id, "cache_status": "MISS",
                "run_id": "r" + cand.id}
    opt = Optimizer(cfg, evaluate_fn=_eval, work_dir=Path(tmp_path))
    res = opt.run(cs)
    assert res.final is not None
    # Non-MCMM path keeps a single QoR (not collapsed from MCMM).
    assert res.final.qor is not None
    assert res.final.mcmm is None
    assert res.final.cache_status in ("MISS", "HIT", "N_A")


def test_optimizer_mcmm_enabled_produces_mcmm_result(tmp_path):
    cfg = _cfg(enabled=True)
    cfg.optimization.enabled = True
    cfg.optimization.max_iterations = 2
    cfg.optimization.max_eda_runs = 30
    cfg.optimization.perturbation.uncertainty_range_ns = [0.0, 0.1, 0.02]
    cs = _cset()
    cs.create_clock(name="clk", period_seconds=10e-9, fixed=True,
                    source_kind=SourceKind.USER, confidence=Confidence.HIGH)
    u = cs.create_clock_uncertainty("clk", 0.05e-9, source_kind=SourceKind.INFERENCE)
    u.opt_status = OptimizationStatus.TUNABLE
    mat = build_scenario_matrix(cfg, cs)
    ev = mock_mcmm_evaluator(mat, base_cset=cs)
    opt = Optimizer(cfg, evaluate_fn=ev, work_dir=Path(tmp_path))
    res = opt.run(cs)
    assert res.final is not None
    assert res.final.mcmm is not None
    assert res.final.qor is None
    assert res.final.global_status


# ---------------------------------------------------------------------------
# 27. scenario-specific SDC
# ---------------------------------------------------------------------------

def test_scenario_specific_sdc():
    cs = _cset()
    # All-scenario constraint
    cs.create_clock(name="clk", period_seconds=10e-9, fixed=True,
                    source_kind=SourceKind.USER, confidence=Confidence.HIGH)
    # Scenario-specific uncertainty only applies to S1
    u = Constraint(id="U_S1", type=ConstraintType.SET_CLOCK_UNCERTAINTY,
                   target_objects=["clk"], clock_refs=["clk"],
                   values={"uncertainty": 0.1e-9}, scenario_ids=["S1"])
    cs.add(u)
    backend = get_backend("generic")
    sdc_s1 = backend.generate(cs, design_name="m", mode=SafeMode.BALANCED,
                              scenario="S1").text
    sdc_s2 = backend.generate(cs, design_name="m", mode=SafeMode.BALANCED,
                              scenario="S2").text
    # U_S1 must appear only in S1 SDC
    assert "set_clock_uncertainty" in sdc_s1
    assert "set_clock_uncertainty" not in sdc_s2
    # create_clock (all scenarios) appears in both
    assert "create_clock" in sdc_s1 and "create_clock" in sdc_s2


# ---------------------------------------------------------------------------
# 28. repeated evaluation determinism
# ---------------------------------------------------------------------------

def test_repeated_evaluation_determinism():
    cs = _cset()
    mat = build_scenario_matrix(_cfg(enabled=True), cs)
    ev = mock_mcmm_evaluator(mat, base_cset=cs)
    res_a = ev(Candidate(id="C7", constraint_set=cs), Path("/tmp/mcmm"))
    res_b = ev(Candidate(id="C7", constraint_set=cs), Path("/tmp/mcmm"))
    assert res_a.feasible == res_b.feasible
    assert res_a.global_status == res_b.global_status
    assert res_a.objectives["setup_wns"].value == res_b.objectives["setup_wns"].value
    # Deterministic per-scenario values too
    for sid in res_a.active_scenario_ids:
        assert (res_a.scenario_results[sid].qor.setup_wns
                == res_b.scenario_results[sid].qor.setup_wns)


# ---------------------------------------------------------------------------
# 29. same-scenario cache hit
# ---------------------------------------------------------------------------

def test_same_scenario_cache_hit_counts():
    cs = _cset()
    mat = build_scenario_matrix(_cfg(enabled=True), cs)
    seen = {"n": 0}

    def evaluate_scenario(scenario, cand, work_dir):
        q = _qor(scenario=scenario.id, mode=scenario.mode, corner=scenario.corner)
        # Simulate cache: identical (candidate, scenario) returns HIT after first.
        key = f"{scenario.id}-{cand.id}"
        if key in seen:
            return {"qor": q, "cache_key": key, "cache_status": "HIT",
                    "run_id": f"r_hit_{scenario.id}"}
        seen[key] = 1
        return {"qor": q, "cache_key": key, "cache_status": "MISS",
                "run_id": f"r_miss_{scenario.id}"}

    ev = MCMMEvaluator(mat, evaluate_scenario=evaluate_scenario, base_cset=cs,
                       name="mock")
    # Evaluate same candidate twice -> 2nd all cache hits
    ev(Candidate(id="C1", constraint_set=cs), Path("/tmp/mcmm"))
    res2 = ev(Candidate(id="C1", constraint_set=cs), Path("/tmp/mcmm"))
    assert res2.cache_hits == 2
    assert res2.cache_status == "HIT"


# ---------------------------------------------------------------------------
# 30. different-scenario cache separation
# ---------------------------------------------------------------------------

def test_different_scenario_cache_separation():
    cs = _cset()
    k1 = scenario_cache_key(Scenario(id="FUNC", mode="functional", corner="slow"), cs)
    k2 = scenario_cache_key(Scenario(id="TEST", mode="test", corner="slow"), cs)
    assert k1 != k2


def test_semantic_key_distinguishes_mode_corner():
    s1 = Scenario(id="A", mode="functional", corner="slow")
    s2 = Scenario(id="A", mode="test", corner="slow")
    s3 = Scenario(id="A", mode="functional", corner="fast")
    assert scenario_semantic_key(s1) != scenario_semantic_key(s2)
    assert scenario_semantic_key(s1) != scenario_semantic_key(s3)


# ---------------------------------------------------------------------------
# Objective directions / conservative aggregation
# ---------------------------------------------------------------------------

def test_global_objective_directions_conservative():
    r = _result({
        "S1": _make("S1", wns_ns=0.9, hold_ns=0.4, area=100.0, power=50.0),
        "S2": _make("S2", wns_ns=0.3, hold_ns=0.1, area=120.0, power=60.0),
    })
    aggregate_objectives(r)
    # setup_wns (maximize) binding = MIN = 0.3 (S2)
    assert r.objectives["setup_wns"].value == pytest.approx(0.3e-9)
    assert r.objectives["setup_wns"].limiting == ["S2"]
    # hold_wns (maximize) binding = MIN = 0.1 (S2)
    assert r.objectives["hold_wns"].value == pytest.approx(0.1e-9)
    # area (minimize) binding = MAX = 120 (S2)
    assert r.objectives["area"].value == pytest.approx(120.0)
    # power (minimize) binding = MAX = 60 (S2)
    assert r.objectives["power"].value == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# Margin math per scenario + global binding (Step 12 §8)
# ---------------------------------------------------------------------------

def test_margin_per_scenario_and_global_binding():
    baseline_by_scenario = {
        "S1": (1.0e-9, 0.5e-9),  # broad headroom
        "S2": (0.3e-9, 0.2e-9),  # tight headroom
    }
    r = _result({
        "S1": _make("S1", wns_ns=0.8, hold_ns=0.5),
        "S2": _make("S2", wns_ns=0.1, hold_ns=0.2),
    })
    global_margin(r, baseline_by_scenario=baseline_by_scenario)
    # S1: baseline setup=1.0ns, cand setup=0.8ns -> setup_util=(1.0-0.8)/1.0=0.2
    #     hold util = 0 -> max = 0.2
    # S2: baseline setup=0.3ns, cand setup=0.1ns -> setup_util=(0.3-0.1)/0.3=0.666
    #     hold util = 0 -> max = 0.666...
    assert r.scenario_results["S1"].margin_utilization == pytest.approx(0.2)
    assert r.scenario_results["S2"].margin_utilization == pytest.approx(2.0 / 3.0)
    # Global binding util = max = S2
    assert r.margin_utilization == pytest.approx(2.0 / 3.0)
    assert r.margin_limiting_scenarios == ["S2"]


def test_margin_is_diagnostic_not_objective():
    bss = {"S1": (1.0e-9, 0.5e-9), "S2": (1.0e-9, 0.5e-9)}
    r = _result({
        "S1": _make("S1", wns_ns=0.8, hold_ns=0.5),
        "S2": _make("S2", wns_ns=0.8, hold_ns=0.5),
    })
    global_margin(r, baseline_by_scenario=bss)
    assert "margin_utilization" not in r.objectives
    assert r.margin_utilization is not None
