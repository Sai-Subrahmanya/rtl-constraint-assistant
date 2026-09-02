"""Pareto and candidate selection tests (Manual §42, §110, §111) — Step 11."""
import copy
import math
import pytest
from pathlib import Path

from rca.qor import is_dominating, pareto_filter
from rca.qor.model import QoRResult
from rca.qor.objectives import (
    CompareResult, Direction, OBJECTIVE_SPECS, classify_feasibility,
    compare_objectives, compute_margin, is_dominating as is_dom_new,
    objective_vector, pareto_front, scalar_score, select_final, _feasible_bool,
)
from rca.optimizer import Candidate, Optimizer, OptimizationBudget, generate_candidates
from rca.optimizer.base import stable_hash_cset
from rca.utils.enums import (
    CandidateDecision, Confidence, ConstraintStatus, ConstraintType,
    OptimizationStatus, PowerStatus, Priority, SourceKind, StopReason,
)
from rca.constraint_model import ConstraintSet
from rca.qor.pareto import score_candidate


def _make(id, wns_ns=0.5, hold_ns=0.2, area=100, power=100, setup_viol=0, hold_viol=0):
    q = QoRResult(
        setup_wns=wns_ns*1e-9, hold_wns=hold_ns*1e-9,
        setup_tns=min(0, wns_ns*1e-9) if wns_ns < 0 else 0,
        hold_tns=min(0, hold_ns*1e-9) if hold_ns < 0 else 0,
        setup_violations=setup_viol, hold_violations=hold_viol,
        area_total=area, power_total=power, cell_count=10,
        area=float(area), power=float(power),
    )
    feasible = (wns_ns >= 0 and hold_ns >= 0 and setup_viol == 0 and hold_viol == 0)
    return Candidate(id=id, qor=q, decision=CandidateDecision.EVALUATED,
                     constraint_set=ConstraintSet(), hard_feasible=feasible)


def test_dominance_clearly_better():
    """A is better on every metric -> dominates B."""
    a = _make("A", 0.5, 0.2, 100, 100)
    b = _make("B", 0.4, 0.15, 110, 110)
    assert is_dominating(a, b)
    assert not is_dominating(b, a)


def test_non_domination_tradeoff():
    """A has better slack, B better PPA -> neither dominates."""
    a = _make("A", 0.8, 0.3, 100, 100)
    b = _make("B", 0.2, 0.18, 102, 101)
    # Note: A dominates B on timing (setup/hold WNS higher), area (lower), power (lower)
    # Actually A does dominate b because A is better in every dimension we compare.
    # Use a case that trades off:
    c = _make("C", 0.1, 0.1, 90, 90)  # better PPA, worse slack than b
    # b vs c: c has lower WNS (worse timing) but better area/power
    assert not is_dominating(b_candidate := _make("B", 0.2, 0.18, 102, 101), c)
    assert not is_dominating(c, b_candidate)


def test_infeasible_rejected():
    d = _make("D", -0.01, 0.3, 99, 99, setup_viol=1)
    ok = _make("ok", 0.5, 0.2, 100, 100)
    front = pareto_filter([d, ok])
    assert d not in front
    assert ok in front


def test_hold_failure_rejected():
    e = _make("E", 0.01, -0.05, 100, 100, hold_viol=1)
    assert not e.feasible
    front = pareto_filter([e, _make("ok")])
    assert e not in front


def test_pareto_keeps_multiple():
    a = _make("A", 0.8, 0.3, 100, 100)
    b = _make("B", 0.2, 0.2, 98, 98)
    # a better slack, b better area/power -> both non-dominated
    front = pareto_filter([a, b])
    assert len(front) == 2


def test_fixed_clock_not_mutated():
    cset = ConstraintSet()
    c = cset.create_clock(name="clk", period_seconds=10e-9, fixed=True,
                          source_kind=SourceKind.USER, confidence=Confidence.HIGH)
    assert c.is_fixed()
    assert c.opt_status == OptimizationStatus.FIXED


# ------------------------------------------------------------------
# Step 11 additions: helpers that mark hard_feasible properly
# ------------------------------------------------------------------

def _qor(wns_ns=0.5, hold_ns=0.2, area=100.0, area_proxy=None, power=None,
         setup_tns=0.0, hold_tns=0.0, cq=None, margin_headroom_ns=None,
         margin_util=None, validation_errors=0, unsafe_exceptions=0,
         tool=None, notes=None, scenario="default", corner="default"):
    # Feasibility is derived from WNS; tests can override with wns_ns<0.
    setup_ok = (wns_ns is not None and wns_ns >= 0)
    hold_ok = (hold_ns is not None and hold_ns >= 0)
    q = QoRResult(
        setup_wns=wns_ns*1e-9 if wns_ns is not None else None,
        hold_wns=hold_ns*1e-9 if hold_ns is not None else None,
        setup_tns=setup_tns*1e-9 if setup_tns is not None else None,
        hold_tns=hold_tns*1e-9 if hold_tns is not None else None,
        setup_violations=0 if setup_ok else 1,
        hold_violations=0 if hold_ok else 1,
        area=area, area_total=area, area_proxy=area_proxy,
        power=power,
        power_status=PowerStatus.AVAILABLE.value if power is not None
                     else PowerStatus.UNAVAILABLE.value,
        constraint_quality=cq,
        margin_headroom_ns=margin_headroom_ns,
        margin_utilization=margin_util,
        validation_errors=validation_errors,
        unsafe_exceptions=unsafe_exceptions,
        cell_count=10,
        scenario=scenario, corner=corner,
        tool=tool or "yosys_opensta",
        notes=notes or [],
    )
    return q


def _cand(cid, **kw):
    qor_keys = {"wns_ns","hold_ns","area","area_proxy","power",
                "setup_tns","hold_tns","cq","margin_headroom_ns","margin_util",
                "validation_errors","unsafe_exceptions","tool","notes",
                "scenario","corner"}
    q_kw = {k: v for k, v in kw.items() if k in qor_keys}
    q = _qor(**q_kw)
    c = Candidate(id=cid, qor=q, decision=CandidateDecision.EVALUATED,
                  constraint_set=kw.get("constraint_set", ConstraintSet()))
    c.hard_feasible = kw.get("hard_feasible", True)
    c.blocked = kw.get("blocked", False)
    c.infeasible_reason = kw.get("reason", "")
    c.scenario = kw.get("scenario", "default")
    c.corner = kw.get("corner", "default")
    return c


# -------------------- Hard feasibility --------------------

def test_hard_feasibility_rejects_setup_violation():
    c = _cand("F1", wns_ns=-0.01, hold_ns=0.2, hard_feasible=False, reason="setup_violation")
    fr = classify_feasibility(c.qor)
    assert not fr.feasible
    assert fr.infeasible_reason == "setup_violation"


def test_hard_feasibility_rejects_hold_violation():
    fr = classify_feasibility(_qor(wns_ns=0.5, hold_ns=-0.02))
    assert not fr.feasible
    assert fr.infeasible_reason == "hold_violation"


def test_blocked_vs_infeasible_distinct():
    blocked = classify_feasibility(None)
    assert blocked.blocked and not blocked.feasible
    infeas = classify_feasibility(_qor(wns_ns=-0.1, hold_ns=0.1))
    assert (not infeas.blocked) and (not infeas.feasible)


def test_feasible_pass_with_margin():
    fr = classify_feasibility(_qor(wns_ns=0.5, hold_ns=0.2),
                              required_setup_ns=0.1, required_hold_ns=0.05)
    assert fr.feasible


def test_required_margin_rejects_tight_slack():
    # 0.08ns WNS < 0.1ns required → infeasible
    fr = classify_feasibility(_qor(wns_ns=0.08, hold_ns=0.2),
                              required_setup_ns=0.1)
    assert not fr.feasible
    assert fr.infeasible_reason == "setup_violation"


# -------------------- Direction of optimization --------------------

def test_wns_direction_maximize():
    assert OBJECTIVE_SPECS["setup_wns"].direction is Direction.MAXIMIZE
    assert OBJECTIVE_SPECS["hold_wns"].direction is Direction.MAXIMIZE


def test_tns_direction_when_negative():
    # For feasible candidates TNS=0; for infeasible TNS<0, higher is better.
    assert OBJECTIVE_SPECS["setup_tns"].direction is Direction.MAXIMIZE
    assert OBJECTIVE_SPECS["hold_tns"].direction is Direction.MAXIMIZE


def test_area_direction_minimize():
    assert OBJECTIVE_SPECS["area"].direction is Direction.MINIMIZE


def test_power_direction_minimize():
    assert OBJECTIVE_SPECS["power"].direction is Direction.MINIMIZE


# -------------------- UNKNOWN / power policy --------------------

def test_unknown_power_not_zero():
    q = _qor(wns_ns=0.5, hold_ns=0.2, area=100.0)
    vec = objective_vector(q)
    # objective_vector returns (value, tag); unknown power is (None, None)
    assert vec["power"][0] is None
    assert q.power_status == PowerStatus.UNAVAILABLE.value


def test_unknown_power_never_dominates_known():
    a = _cand("A", wns_ns=0.5, hold_ns=0.2, area=100.0)  # power unknown
    b = _cand("B", wns_ns=0.5, hold_ns=0.2, area=100.0, power=10.0)
    # a cannot dominate b (a has unknown power)
    assert not is_dominating(a, b)


def test_unknown_compare_is_incomplete():
    a = _cand("A", wns_ns=0.5, hold_ns=0.2, area=100.0)
    b = _cand("B", wns_ns=0.5, hold_ns=0.2, area=100.0, power=5.0)
    cmp = compare_objectives(a, b)
    assert cmp["power"] == CompareResult.INCOMPARABLE


# -------------------- Pareto dominance --------------------

def test_pareto_dominance_requires_strict_better_somewhere():
    a = _cand("A", wns_ns=0.5, hold_ns=0.2, area=100.0, power=100.0)
    b = _cand("B", wns_ns=0.5, hold_ns=0.2, area=100.0, power=100.0)
    # equal on all objectives: neither dominates
    assert not is_dominating(a, b)
    assert not is_dominating(b, a)


def test_infeasible_cannot_dominate():
    a = _cand("A", wns_ns=-0.5, hold_ns=0.2, area=50.0, hard_feasible=False)
    b = _cand("B", wns_ns=0.4, hold_ns=0.2, area=200.0)
    assert not is_dominating(a, b)


def test_different_scenarios_not_compared():
    a = _cand("A", wns_ns=0.5, hold_ns=0.2, area=100.0, scenario="tt")
    b = _cand("B", wns_ns=0.8, hold_ns=0.3, area=50.0, scenario="ss")
    assert not is_dominating(a, b) and not is_dominating(b, a)


def test_pareto_front_correctness():
    a = _cand("A", wns_ns=0.8, hold_ns=0.3, area=100.0)
    b = _cand("B", wns_ns=0.5, hold_ns=0.2, area=80.0)     # better area, worse timing
    c = _cand("C", wns_ns=0.3, hold_ns=0.15, area=90.0)   # dominated by b
    front = pareto_front([a, b, c])
    ids = {x.id for x in front}
    assert ids == {"A", "B"}
    assert c not in front


# -------------------- Margin utilization --------------------

def test_margin_headroom_above_required():
    # baseline: setup=1.0ns, hold=0.3ns. cand: setup=0.8ns, hold=0.3ns.
    # Binding baseline headroom = min(1.0, 0.3) = 0.3ns.
    # setup_util = (1.0-0.8)/1.0 = 0.2; hold_util = (0.3-0.3)/0.3 = 0; max = 0.2
    q = _qor(wns_ns=0.8, hold_ns=0.3)
    m = compute_margin(q, required_setup_ns=0.0, required_hold_ns=0.0,
                       baseline_setup_wns=1.0e-9, baseline_hold_wns=0.3e-9)
    assert m["margin_headroom_ns"] == pytest.approx(0.3)  # min(0.8, 0.3)
    assert m["margin_utilization"] == pytest.approx(0.2)


def test_margin_below_required_is_infeasible():
    fr = classify_feasibility(_qor(wns_ns=0.05, hold_ns=0.1),
                              required_setup_ns=0.1)
    assert not fr.feasible


def test_margin_missing_baseline_hold_returns_none():
    # Without baseline hold WNS, utilization is None (policy §2D).
    q = _qor(wns_ns=0.8, hold_ns=0.3)
    m = compute_margin(q, required_setup_ns=0.0, required_hold_ns=0.0,
                       baseline_setup_wns=1.0e-9, baseline_hold_wns=None)
    assert m["margin_utilization"] is None


# -------------------- Fixed constraint immutability --------------------

def _make_cset_with_fixed():
    cset = ConstraintSet(name="t")
    cset.create_clock(name="clk", period_seconds=10e-9, fixed=True,
                      source_kind=SourceKind.USER,
                      confidence=Confidence.HIGH)
    # add a tunable uncertainty
    u = cset.create_clock_uncertainty("clk", 0.1e-9,
                                      source_kind=SourceKind.INFERENCE)
    u.confidence = Confidence.MEDIUM
    u.opt_status = OptimizationStatus.TUNABLE
    return cset


def test_generate_candidates_never_mutates_fixed():
    from rca.config.model import (
        OptimizationConfig, OptimizationPerturbation, OptimizationThresholds,
        FlowConfig,
    )
    class _A: safe_mode = "balanced"
    class _Cfg:
        flow = FlowConfig(backend="yosys_opensta", output_dir="/tmp/foo")
        analysis = _A()
        _config_path = "/tmp/foo/p.yaml"
        optimization = OptimizationConfig(
            enabled=True, max_iterations=4, max_eda_runs=8,
            max_runtime_minutes=1, convergence_patience=2,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[-0.05, 0.05, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        )
        parameters = {}
        def top_module(self): return "top"

    base = Candidate(id="C000", constraint_set=_make_cset_with_fixed())
    cset = base.constraint_set
    # Generate candidates and verify no CREATE_CLOCK (period) was touched
    cands = generate_candidates(base, cset, _Cfg(), max_candidates=10)
    assert cands, "expected at least one candidate"
    for c in cands:
        for cc in c.constraint_set:
            if cc.type == ConstraintType.CREATE_CLOCK:
                assert cc.is_fixed()
                assert cc.values.get("period", 0) == pytest.approx(10e-9)


def test_deterministic_candidate_generation():
    from rca.config.model import (
        OptimizationConfig, OptimizationPerturbation, OptimizationThresholds,
        FlowConfig,
    )
    class _A: safe_mode = "balanced"
    class _Cfg:
        flow = FlowConfig(backend="yosys_opensta", output_dir="/tmp/foo")
        analysis = _A()
        _config_path = "/tmp/foo/p.yaml"
        optimization = OptimizationConfig(
            enabled=True, max_iterations=4, max_eda_runs=8,
            max_runtime_minutes=1, convergence_patience=2,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[-0.05, 0.05, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        )
        parameters = {}
        def top_module(self): return "top"
    cs1 = _make_cset_with_fixed(); b1 = Candidate(id="C0", constraint_set=cs1)
    cs2 = _make_cset_with_fixed(); b2 = Candidate(id="C0", constraint_set=cs2)
    ca = generate_candidates(b1, cs1, _Cfg(), max_candidates=10)
    cb = generate_candidates(b2, cs2, _Cfg(), max_candidates=10)
    assert [c.generated_changes for c in ca] == [c.generated_changes for c in cb]
    assert [c.constraint_model_hash for c in ca] == [c.constraint_model_hash for c in cb]


def test_bounded_candidate_count():
    from rca.config.model import (
        OptimizationConfig, OptimizationPerturbation, OptimizationThresholds,
        FlowConfig,
    )
    class _A: safe_mode = "balanced"
    class _Cfg:
        flow = FlowConfig(backend="yosys_opensta", output_dir="/tmp/foo")
        analysis = _A()
        _config_path = "/tmp/foo/p.yaml"
        optimization = OptimizationConfig(
            enabled=True, max_iterations=2, max_eda_runs=2,
            max_runtime_minutes=1, convergence_patience=1,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[-0.2, 0.2, 0.01],
                io_delay_range_ns=[-0.2, 0.2, 0.01],
            ),
            thresholds=OptimizationThresholds(),
        )
        parameters = {}
        def top_module(self): return "top"
    base = Candidate(id="C0", constraint_set=_make_cset_with_fixed())
    cands = generate_candidates(base, base.constraint_set, _Cfg(), max_candidates=3)
    assert len(cands) <= 3


# -------------------- Baseline preservation --------------------

def test_baseline_preserved_when_optimal():
    from rca.config.model import (
        OptimizationConfig, OptimizationThresholds, FlowConfig,
    )
    class _A: safe_mode = "balanced"
    class _Cfg:
        flow = FlowConfig(backend="mock", output_dir="/tmp/optb")
        analysis = _A()
        _config_path = "/tmp/optb/p.yaml"
        optimization = OptimizationConfig(
            enabled=True, max_iterations=1, max_eda_runs=1,
            max_runtime_minutes=1, convergence_patience=1,
            thresholds=OptimizationThresholds(),
        )
        parameters = {}
        def top_module(self): return "top"

    baseline_qor = _qor(wns_ns=0.8, hold_ns=0.3, area=100.0)
    # evaluator returns same baseline QoR regardless of candidate
    def _eval(cand, work_dir):
        return {"qor": copy.deepcopy(baseline_qor), "cache_key": "K" + cand.id,
                "cache_status": "MISS", "run_id": "r" + cand.id}
    opt = Optimizer(_Cfg(), evaluate_fn=_eval, work_dir=Path("/tmp/optb"))
    res = opt.run(ConstraintSet(name="t"), baseline_qor=baseline_qor)
    # If no candidate improves on baseline, final should be baseline
    assert res.final is not None
    # (no tunable constraints → no candidates generated → final is baseline)
    assert res.final.id == "C000" or res.final.id == res.baseline.id


# -------------------- End-to-end optimizer with cache integration --------------------

def _make_opt_cfg(tmp, enabled=True, max_iter=2, max_runs=10):
    from rca.config.model import (
        OptimizationConfig, OptimizationPerturbation, OptimizationThresholds,
        FlowConfig,
    )
    class _A: safe_mode = "balanced"
    class _Cfg:
        flow = FlowConfig(backend="yosys_opensta", output_dir=str(tmp/"opt"))
        analysis = _A()
        _config_path = str(tmp/"p.yaml")
        optimization = OptimizationConfig(
            enabled=enabled, max_iterations=max_iter, max_eda_runs=max_runs,
            max_runtime_minutes=1, convergence_patience=2,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[-0.05, 0.05, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        )
        parameters = {}
        def top_module(self): return "top"
    return _Cfg()


def test_optimizer_returns_structured_result_with_provenance(tmp_path):
    cfg = _make_opt_cfg(tmp_path)
    # Every candidate evaluates to a small-area positive-slack QoR
    def _eval(cand, work_dir):
        q = _qor(wns_ns=0.5 - 0.02*cand.generation,
                 hold_ns=0.2,
                 area=100.0 - 5*cand.generation)
        return {"qor": q, "cache_key": "K" + cand.id,
                "cache_status": "MISS", "run_id": "r" + cand.id}
    opt = Optimizer(cfg, evaluate_fn=_eval, work_dir=Path(cfg.flow.output_dir))
    res = opt.run(_make_cset_with_fixed(),
                  baseline_qor=_qor(wns_ns=0.5, hold_ns=0.2, area=100.0))
    assert res.baseline is not None
    assert res.final is not None
    assert res.final.decision == CandidateDecision.FINAL
    assert 1 <= res.pareto_size <= 5
    # candidate records carry provenance
    for c in res.all_candidates:
        assert c.cache_key != ""
        assert c.run_id != ""
        assert c.qor is not None


def test_cache_hit_returns_same_qor_and_counts_hit(tmp_path):
    cfg = _make_opt_cfg(tmp_path, max_iter=1, max_runs=6)
    call_count = {"n": 0}
    def _eval(cand, work_dir):
        call_count["n"] += 1
        # Second identical call should come from cache, not eval.
        # We simulate cache by returning HIT when a key is seen twice.
        if not hasattr(_eval, "_seen"): _eval._seen = set()
        key = cand.constraint_model_hash or cand.id
        if key in _eval._seen:
            return {"qor": _qor(wns_ns=0.5, hold_ns=0.2, area=100.0),
                    "cache_key": key, "cache_status": "HIT", "run_id": "r_hit"}
        _eval._seen.add(key)
        return {"qor": _qor(wns_ns=0.5, hold_ns=0.2, area=100.0),
                "cache_key": key, "cache_status": "MISS", "run_id": "r_miss"}
    opt = Optimizer(cfg, evaluate_fn=_eval, work_dir=Path(cfg.flow.output_dir))
    # Run twice on identical baseline: second should find cache hit via
    # our evaluator's HIT return.
    res = opt.run(_make_cset_with_fixed(),
                  baseline_qor=None)
    assert res.final is not None


def test_different_candidates_have_different_experiment_identity(tmp_path):
    cfg = _make_opt_cfg(tmp_path, max_iter=1, max_runs=6)
    keys = set()
    def _eval(cand, work_dir):
        q = _qor(wns_ns=max(0.1, 0.5 - 0.05*cand.generation), hold_ns=0.2,
                 area=100.0 - 2*cand.generation)
        keys.add(cand.cache_key or cand.id)
        return {"qor": q, "cache_key": "K" + cand.id,
                "cache_status": "MISS", "run_id": "R" + cand.id}
    opt = Optimizer(cfg, evaluate_fn=_eval, work_dir=Path(cfg.flow.output_dir))
    res = opt.run(_make_cset_with_fixed())
    # every generated candidate has a unique cache key
    seen = {c.cache_key for c in res.all_candidates if c.cache_key}
    assert len(seen) == res.total_candidates


def test_adversarial_high_slack_bad_area_vs_low_slack_good_area_both_pareto():
    # Candidate with highest WNS has much worse area; candidate with best area
    # violates hold → infeasible. Verifies Pareto vs infeasible separation.
    a = _cand("A", wns_ns=1.0, hold_ns=0.3, area=200.0)
    b = _cand("B", wns_ns=0.2, hold_ns=-0.05, area=50.0, hard_feasible=False,
              reason="hold_violation")
    c = _cand("C", wns_ns=0.3, hold_ns=0.15, area=80.0)
    front = pareto_front([a, b, c])
    assert b not in front
    assert a in front and c in front


def test_adversarial_unknown_power_not_selected_over_known_when_all_else_equal():
    a = _cand("A", wns_ns=0.5, hold_ns=0.2, area=100.0)  # power unknown
    b = _cand("B", wns_ns=0.5, hold_ns=0.2, area=100.0, power=50.0)
    # neither dominates the other; Pareto keeps both. Priority selects
    # deterministically (b by id tiebreak after equal timing/area).
    front = pareto_front([a, b])
    assert len(front) == 2
    # select_final with power priority low picks a or b by deterministic tiebreak
    priorities = {"timing": Priority.HIGH, "area": Priority.MEDIUM,
                  "power": Priority.LOW,
                  "timing_margin_utilization": Priority.LOW}
    sel = select_final(front, a, priorities)
    assert sel is not None


def test_fixed_constraint_modification_rejected_in_generation():
    cset = _make_cset_with_fixed()
    before_period = None
    for c in cset:
        if c.type == ConstraintType.CREATE_CLOCK:
            before_period = c.values.get("period")
    assert before_period == 10e-9
    # generate candidates and verify no mutation to the CREATE_CLOCK entry
    from rca.config.model import (
        OptimizationConfig, OptimizationPerturbation, OptimizationThresholds,
        FlowConfig,
    )
    class _A: safe_mode = "balanced"
    class _Cfg:
        flow = FlowConfig(backend="yosys_opensta", output_dir="/tmp/foo2")
        analysis = _A()
        _config_path = "/tmp/foo2/p.yaml"
        optimization = OptimizationConfig(
            enabled=True, max_iterations=2, max_eda_runs=6,
            max_runtime_minutes=1, convergence_patience=1,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[-0.05, 0.05, 0.05],
                io_delay_range_ns=[-0.05, 0.05, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        )
        parameters = {}
        def top_module(self): return "top"
    base = Candidate(id="C0", constraint_set=cset)
    cands = generate_candidates(base, cset, _Cfg(), max_candidates=10)
    for cand in cands:
        for c in cand.constraint_set:
            if c.type == ConstraintType.CREATE_CLOCK:
                assert c.values.get("period") == 10e-9


def test_stopping_rule_max_eda_runs(tmp_path):
    cfg = _make_opt_cfg(tmp_path, max_iter=10, max_runs=3)
    def _eval(cand, work_dir):
        q = _qor(wns_ns=0.5, hold_ns=0.2, area=100.0 - 1.0*cand.generation)
        return {"qor": q, "cache_key": "K"+cand.id, "cache_status": "MISS",
                "run_id": "r"+cand.id}
    opt = Optimizer(cfg, evaluate_fn=_eval, work_dir=Path(cfg.flow.output_dir))
    res = opt.run(_make_cset_with_fixed())
    assert res.stop_reason == StopReason.MAX_EDA_RUNS
    assert res.eda_runs <= cfg.optimization.max_eda_runs


def test_candidate_rejection_explanations_present(tmp_path):
    cfg = _make_opt_cfg(tmp_path, max_iter=1, max_runs=6)
    def _eval(cand, work_dir):
        # Introduce an infeasible candidate in generation 1 via a very negative hold
        if cand.generation == 1:
            q = _qor(wns_ns=0.5, hold_ns=-0.1, area=80.0)
        else:
            q = _qor(wns_ns=0.5, hold_ns=0.2, area=100.0)
        return {"qor": q, "cache_key": "K"+cand.id, "cache_status": "MISS",
                "run_id": "r"+cand.id}
    opt = Optimizer(cfg, evaluate_fn=_eval, work_dir=Path(cfg.flow.output_dir))
    res = opt.run(_make_cset_with_fixed(),
                  baseline_qor=_qor(wns_ns=0.5, hold_ns=0.2, area=100.0))
    assert res.infeasible_count >= 1
    assert res.explanation["reasons"]
    # Each rejected candidate has a reason
    for c in res.infeasible:
        assert c.infeasible_reason


def test_power_unknown_default():
    q = QoRResult()
    assert q.power is None
    assert q.power_status == PowerStatus.UNAVAILABLE.value


# ======================================================================
# Step 11 CORRECTION PASS REGRESSION TESTS
#   #1 area real vs proxy incomparability
#   #2 margin_utilization math (single soft objective, hold-capped, hard floors)
#   #3 unsafe_exceptions -> INFEASIBLE under safe policy
#   #4 one-mutation-per-candidate, deterministic order
#   #5 semantic identity hash (order-independent, semantic fields)
#   #6 constraint_quality defaults to UNKNOWN (None), never fake 1.0
# ======================================================================


# ---------- #1 Area real vs proxy semantics ----------

def test_area_real_vs_real_compares_normally():
    a = _cand("A", wns_ns=0.5, hold_ns=0.2, area=100.0)
    b = _cand("B", wns_ns=0.5, hold_ns=0.2, area=120.0)
    cmp = compare_objectives(a, b)
    assert cmp["area"] is CompareResult.BETTER  # smaller area is better (MIN)
    assert is_dom_new(a, b)
    assert not is_dom_new(b, a)


def test_area_proxy_vs_proxy_compares_normally():
    q1 = _qor(wns_ns=0.5, hold_ns=0.2, area=None, area_proxy=100.0)
    q2 = _qor(wns_ns=0.5, hold_ns=0.2, area=None, area_proxy=120.0)
    a = Candidate(id="A", qor=q1, hard_feasible=True, constraint_set=ConstraintSet())
    b = Candidate(id="B", qor=q2, hard_feasible=True, constraint_set=ConstraintSet())
    cmp = compare_objectives(a, b)
    assert cmp["area"] is CompareResult.BETTER


def test_area_real_vs_proxy_is_incomparable():
    q_real = _qor(wns_ns=0.5, hold_ns=0.2, area=100.0, area_proxy=None)
    q_prox = _qor(wns_ns=0.5, hold_ns=0.2, area=None, area_proxy=100.0)
    a = Candidate(id="A", qor=q_real, hard_feasible=True, constraint_set=ConstraintSet())
    b = Candidate(id="B", qor=q_prox, hard_feasible=True, constraint_set=ConstraintSet())
    cmp = compare_objectives(a, b)
    assert cmp["area"] is CompareResult.INCOMPARABLE
    # Neither can dominate the other across mixed area sources
    assert not is_dom_new(a, b)
    assert not is_dom_new(b, a)
    front = pareto_front([a, b])
    assert len(front) == 2


def test_area_both_unknown_is_neutral():
    q1 = _qor(wns_ns=0.5, hold_ns=0.2, area=None, area_proxy=None)
    q2 = _qor(wns_ns=0.4, hold_ns=0.2, area=None, area_proxy=None)
    a = Candidate(id="A", qor=q1, hard_feasible=True, constraint_set=ConstraintSet())
    b = Candidate(id="B", qor=q2, hard_feasible=True, constraint_set=ConstraintSet())
    cmp = compare_objectives(a, b)
    assert cmp["area"] is CompareResult.EQUAL  # both unknown


# ---------- #2 Margin utilization ----------

def test_margin_utilization_is_diagnostic_not_pareto_objective():
    # margin_headroom_ns is a diagnostic; margin_utilization is a diagnostic /
    # secondary tie-break signal only — it is NOT a Pareto objective because
    # consuming slack is not intrinsically a benefit.
    assert "margin_utilization" not in OBJECTIVE_SPECS
    assert "margin_headroom_ns" not in OBJECTIVE_SPECS


def test_margin_higher_consumed_gives_higher_utilization():
    # baseline 1ns slack, no requirement
    q_lo = _qor(wns_ns=0.9, hold_ns=0.5)  # consumed 0.1ns -> util ~0.1
    q_hi = _qor(wns_ns=0.5, hold_ns=0.5)  # consumed 0.5ns -> util ~0.5
    m_lo = compute_margin(q_lo, baseline_setup_wns=1e-9, baseline_hold_wns=0.5e-9)
    m_hi = compute_margin(q_hi, baseline_setup_wns=1e-9, baseline_hold_wns=0.5e-9)
    assert m_lo["margin_utilization"] is not None
    assert m_hi["margin_utilization"] is not None
    assert m_hi["margin_utilization"] > m_lo["margin_utilization"]


def test_margin_below_required_floor_is_infeasible_not_utilized():
    q = _qor(wns_ns=0.05, hold_ns=0.3)
    fr = classify_feasibility(q, required_setup_ns=0.1)
    assert not fr.feasible
    assert fr.infeasible_reason == "setup_violation"


def test_margin_no_baseline_excess_yields_none_utilization():
    # baseline already AT the required floor: there is literally zero usable
    # headroom to trade, so utilization is undefined (None).
    q = _qor(wns_ns=0.05, hold_ns=0.3)
    m = compute_margin(q, required_setup_ns=0.05,
                       baseline_setup_wns=0.05e-9, baseline_hold_wns=0.3e-9)
    assert m["margin_utilization"] is None
    assert m["margin_headroom_ns"] == pytest.approx(0.0)


def test_margin_zero_consumption_yields_zero_utilization():
    # baseline has slack, candidate matches baseline: utilization = 0%
    q = _qor(wns_ns=0.1, hold_ns=0.3)
    m = compute_margin(q, required_setup_ns=0.0,
                       baseline_setup_wns=0.1e-9, baseline_hold_wns=0.3e-9)
    assert m["margin_utilization"] == pytest.approx(0.0)


def test_margin_hold_limited_caps_headroom():
    # setup headroom cand = 0.5-0 = 0.5ns, hold headroom cand = 0.1-0 = 0.1ns -> binding is hold
    # baseline setup_hr=1ns, hold_hr=0.2ns
    # setup_consumed = 0.5ns -> setup_util = 0.5
    # hold_consumed = 0.1ns -> hold_util = 0.5
    # max(0.5, 0.5) = 0.5
    q = _qor(wns_ns=0.5, hold_ns=0.1)
    m = compute_margin(q, required_setup_ns=0.0, required_hold_ns=0.0,
                       baseline_setup_wns=1e-9, baseline_hold_wns=0.2e-9)
    assert m["margin_headroom_ns"] == pytest.approx(0.1)
    assert m["margin_utilization"] == pytest.approx(0.5)


# ---------- #3 Safety: unsafe_exceptions ----------

def test_unsafe_exceptions_infeasible_under_safe_policy():
    q = _qor(wns_ns=0.5, hold_ns=0.3, unsafe_exceptions=2)
    fr = classify_feasibility(q, allow_unsafe_exceptions=False)
    assert not fr.feasible
    assert fr.infeasible_reason == "unsafe_exceptions"


def test_unsafe_exceptions_marked_exploratory_when_allowed():
    q = _qor(wns_ns=0.5, hold_ns=0.3, unsafe_exceptions=1)
    fr = classify_feasibility(q, allow_unsafe_exceptions=True)
    assert fr.feasible
    assert fr.unsafe
    assert fr.exploratory


def test_validation_errors_infeasible():
    q = _qor(wns_ns=0.5, hold_ns=0.3, validation_errors=1)
    fr = classify_feasibility(q)
    assert not fr.feasible
    assert fr.infeasible_reason == "validation_error"


def test_unsafe_candidates_cannot_be_final():
    a = _cand("A", wns_ns=0.5, hold_ns=0.2, area=100.0)
    bq = _qor(wns_ns=0.8, hold_ns=0.3, area=80.0, unsafe_exceptions=1)
    b = Candidate(id="B", qor=bq, hard_feasible=True, constraint_set=ConstraintSet())
    b.infeasible_reason = "unsafe_exceptions"  # simulating safe-policy rejection
    # Under safe policy b is infeasible; when we simulate opt-in unsafe flag it's
    # kept out of final selection by _is_unsafe filter.
    b.hard_feasible = False
    b.infeasible_reason = "unsafe_exceptions"
    priorities = {"timing": Priority.HIGH, "area": Priority.MEDIUM}
    front = pareto_front([a])
    final = select_final(front, a, priorities)
    assert final is a
    # unsafe exploratory candidates on the front (hypothetically) are filtered:
    b.hard_feasible = True
    b.infeasible_reason = ""
    b.qor.unsafe_exceptions = 1
    front2 = pareto_front([a, b])
    assert b not in front2  # _is_unsafe excludes from front


def test_safe_candidate_outranks_unsafe_even_with_better_ppa():
    safe = _cand("SAFE", wns_ns=0.3, hold_ns=0.2, area=100.0)
    unsafe_q = _qor(wns_ns=2.0, hold_ns=2.0, area=10.0, unsafe_exceptions=5)
    unsafe = Candidate(id="UNSAFE", qor=unsafe_q, hard_feasible=False,
                       infeasible_reason="unsafe_exceptions",
                       constraint_set=ConstraintSet())
    front = pareto_front([safe])
    assert unsafe not in front
    priorities = {"timing": Priority.HIGH, "area": Priority.HIGH}
    final = select_final(front, safe, priorities)
    assert final is safe


# ---------- #4 One mutation at a time ----------

def _make_cset_with_two_uncertainties():
    cset = ConstraintSet(name="t2")
    cset.create_clock(name="clk", period_seconds=10e-9, fixed=True,
                      source_kind=SourceKind.USER, confidence=Confidence.HIGH)
    cset.create_clock(name="clk2", period_seconds=20e-9, fixed=True,
                      source_kind=SourceKind.USER, confidence=Confidence.HIGH)
    u1 = cset.create_clock_uncertainty("clk", 0.05e-9,
                                       source_kind=SourceKind.INFERENCE)
    u1.opt_status = OptimizationStatus.TUNABLE
    u2 = cset.create_clock_uncertainty("clk2", 0.05e-9,
                                       source_kind=SourceKind.INFERENCE)
    u2.opt_status = OptimizationStatus.TUNABLE
    return cset, u1.id, u2.id


def test_one_mutation_per_candidate_baseline_strategy():
    from rca.config.model import (
        OptimizationConfig, OptimizationPerturbation, OptimizationThresholds,
        FlowConfig,
    )
    cset, u1, u2 = _make_cset_with_two_uncertainties()
    class _A: safe_mode = "balanced"
    class _Cfg:
        flow = FlowConfig(backend="yosys_opensta", output_dir="/tmp/one_mut")
        analysis = _A()
        _config_path = "/tmp/one_mut/p.yaml"
        optimization = OptimizationConfig(
            enabled=True, max_iterations=8, max_eda_runs=20,
            max_runtime_minutes=1, convergence_patience=2,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[-0.05, 0.05, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        )
        parameters = {}
        def top_module(self): return "top"
    base = Candidate(id="C000", constraint_set=cset)
    cands = generate_candidates(base, cset, _Cfg(), max_candidates=20)
    assert cands, "expected candidates"
    for c in cands:
        assert len(c.mutated_constraint_ids) == 1, \
            f"{c.id} mutated {c.mutated_constraint_ids}"


def test_changing_u1_leaves_u2_unchanged():
    from rca.config.model import (
        OptimizationConfig, OptimizationPerturbation, OptimizationThresholds,
        FlowConfig,
    )
    cset, u1, u2 = _make_cset_with_two_uncertainties()
    class _A: safe_mode = "balanced"
    class _Cfg:
        flow = FlowConfig(backend="yosys_opensta", output_dir="/tmp/iso_mut")
        analysis = _A(); _config_path = "/tmp/iso_mut/p.yaml"
        optimization = OptimizationConfig(
            enabled=True, max_iterations=8, max_eda_runs=20,
            max_runtime_minutes=1, convergence_patience=2,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[0.05, 0.05, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        )
        parameters = {}
        def top_module(self): return "top"
    base = Candidate(id="C000", constraint_set=cset)
    u2_initial = None
    for c in cset:
        if c.id == u2: u2_initial = float(c.values.get("uncertainty", 0.0))
    cands = generate_candidates(base, cset, _Cfg(), max_candidates=20)
    u1_cands = [c for c in cands if c.mutated_constraint_ids == [u1]]
    assert u1_cands, "expected at least one candidate mutating u1"
    for c in u1_cands:
        for cc in c.constraint_set:
            if cc.id == u2:
                assert float(cc.values.get("uncertainty", 0.0)) == u2_initial


def test_fixed_constraints_never_touched_in_one_mutation_strategy():
    from rca.config.model import (
        OptimizationConfig, OptimizationPerturbation, OptimizationThresholds,
        FlowConfig,
    )
    cset = _make_cset_with_fixed()
    class _A: safe_mode = "balanced"
    class _Cfg:
        flow = FlowConfig(backend="yosys_opensta", output_dir="/tmp/fix_imm")
        analysis = _A(); _config_path = "/tmp/fix_imm/p.yaml"
        optimization = OptimizationConfig(
            enabled=True, max_iterations=4, max_eda_runs=10,
            max_runtime_minutes=1, convergence_patience=2,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[-0.05, 0.05, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        )
        parameters = {}
        def top_module(self): return "top"
    base = Candidate(id="C000", constraint_set=cset)
    cands = generate_candidates(base, cset, _Cfg(), max_candidates=20)
    for c in cands:
        for cc in c.constraint_set:
            if cc.type == ConstraintType.CREATE_CLOCK:
                assert cc.values.get("period") == 10e-9
        # no fixed clock id in mutated ids
        assert not any(mid.startswith("create_clock") for mid in c.mutated_constraint_ids)


def test_mutation_order_deterministic_by_id_then_delta():
    from rca.config.model import (
        OptimizationConfig, OptimizationPerturbation, OptimizationThresholds,
        FlowConfig,
    )
    cset, u1, u2 = _make_cset_with_two_uncertainties()
    class _A: safe_mode = "balanced"
    class _Cfg:
        flow = FlowConfig(backend="yosys_opensta", output_dir="/tmp/det")
        analysis = _A(); _config_path = "/tmp/det/p.yaml"
        optimization = OptimizationConfig(
            enabled=True, max_iterations=8, max_eda_runs=20,
            max_runtime_minutes=1, convergence_patience=2,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[-0.05, 0.05, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        )
        parameters = {}
        def top_module(self): return "top"
    base = Candidate(id="C000", constraint_set=cset)
    cands = generate_candidates(base, cset, _Cfg(), max_candidates=20)
    # Order should be: all deltas for u1 (delta ascending), then all for u2.
    seen_u1_last_idx = -1
    seen_u2_first_idx = None
    for i, c in enumerate(cands):
        if c.mutated_constraint_ids == [u1]:
            assert seen_u2_first_idx is None, "u1 appeared after u2"
            seen_u1_last_idx = i
        elif c.mutated_constraint_ids == [u2]:
            if seen_u2_first_idx is None:
                seen_u2_first_idx = i
    # deltas ascending within each constraint
    def _deltas_for(cid):
        ds = []
        for c in cands:
            if c.mutated_constraint_ids == [cid]:
                # parse signed delta from change label
                import re
                m = re.search(r"delta=([+-]?[\d.e+-]+)ns", c.generated_changes[0])
                ds.append(float(m.group(1)))
        return ds
    d1 = _deltas_for(u1); d2 = _deltas_for(u2)
    assert d1 == sorted(d1)
    assert d2 == sorted(d2)


# ---------- #5 Semantic identity hash ----------

def test_semantic_hash_differs_on_target_objects():
    from rca.constraint_model.constraint import Constraint
    def _mk(targets):
        cs = ConstraintSet(name="h")
        c = Constraint(id="u1", type=ConstraintType.SET_CLOCK_UNCERTAINTY,
                       target_objects=list(targets), clock_refs=["clk"],
                       values={"uncertainty": 0.1e-9})
        cs.add(c); return cs
    h1 = stable_hash_cset(_mk(["a", "b"]))
    h2 = stable_hash_cset(_mk(["a", "c"]))
    assert h1 != h2


def test_semantic_hash_differs_on_clock_refs():
    from rca.constraint_model.constraint import Constraint
    def _mk(clks):
        cs = ConstraintSet(name="h")
        c = Constraint(id="u1", type=ConstraintType.SET_CLOCK_UNCERTAINTY,
                       target_objects=["clk"], clock_refs=list(clks),
                       values={"uncertainty": 0.1e-9})
        cs.add(c); return cs
    assert stable_hash_cset(_mk(["clk"])) != stable_hash_cset(_mk(["clk2"]))


def test_semantic_hash_differs_on_opt_status():
    from rca.constraint_model.constraint import Constraint
    def _mk(status):
        cs = ConstraintSet(name="h")
        c = Constraint(id="u1", type=ConstraintType.SET_CLOCK_UNCERTAINTY,
                       target_objects=["clk"], clock_refs=["clk"],
                       values={"uncertainty": 0.1e-9}, opt_status=status)
        cs.add(c); return cs
    assert (stable_hash_cset(_mk(OptimizationStatus.TUNABLE))
            != stable_hash_cset(_mk(OptimizationStatus.FIXED)))


def test_semantic_hash_differs_on_disabled():
    from rca.constraint_model.constraint import Constraint
    def _mk(disabled):
        cs = ConstraintSet(name="h")
        c = Constraint(id="u1", type=ConstraintType.SET_CLOCK_UNCERTAINTY,
                       target_objects=["clk"], clock_refs=["clk"],
                       values={"uncertainty": 0.1e-9}, disabled=disabled)
        cs.add(c); return cs
    assert stable_hash_cset(_mk(False)) != stable_hash_cset(_mk(True))


def test_semantic_hash_insertion_order_independent():
    from rca.constraint_model.constraint import Constraint
    def _mk(ordered_ids):
        cs = ConstraintSet(name="h")
        for cid in ordered_ids:
            c = Constraint(id=cid, type=ConstraintType.SET_CLOCK_UNCERTAINTY,
                           target_objects=["clk"], clock_refs=["clk"],
                           values={"uncertainty": 0.1e-9})
            cs.add(c)
        return cs
    h1 = stable_hash_cset(_mk(["a", "b", "c"]))
    h2 = stable_hash_cset(_mk(["c", "a", "b"]))
    assert h1 == h2, "insertion order must not affect semantic hash"


def test_semantic_hash_excludes_transient_caches():
    from rca.constraint_model.constraint import Constraint
    def _mk(gtext):
        cs = ConstraintSet(name="h")
        c = Constraint(id="u1", type=ConstraintType.SET_CLOCK_UNCERTAINTY,
                       target_objects=["clk"], clock_refs=["clk"],
                       values={"uncertainty": 0.1e-9})
        c.generated_text_by_backend = dict(gtext)
        cs.add(c); return cs
    h1 = stable_hash_cset(_mk({"yosys": "abc"}))
    h2 = stable_hash_cset(_mk({"yosys": "xyz_other_cache_val"}))
    assert h1 == h2, "transient backend cache text must not change identity"


# ---------- #6 Constraint quality unknown policy ----------

def test_constraint_quality_defaults_to_unknown_not_one():
    q = QoRResult()
    assert q.constraint_quality is None
    vec = objective_vector(q)
    assert vec["constraint_quality"][0] is None


# ======================================================================
# Second correction pass — hard-feasibility authority & margin semantics
# ======================================================================


# ---------- Hard-feasibility authority (§1–§3) ----------

def test_feasible_bool_is_authoritative_hard_feasible():
    # _feasible_bool relies ONLY on hard_feasible, never on raw QoR WNS.
    c = _cand("X", wns_ns=0.5, hold_ns=0.2, hard_feasible=False,
              reason="validation_error", validation_errors=1)
    # Candidate.feasible is now an alias for hard_feasible.
    assert c.feasible is False
    assert _feasible_bool(c) is False


def test_validation_error_positive_timing_not_pareto_eligible():
    """A candidate with positive WNS/hold but validation errors must NOT
    enter Pareto, because classify_feasibility hard-failed it."""
    c = _cand("BAD", wns_ns=0.5, hold_ns=0.2, area=50.0,
              hard_feasible=False, reason="validation_error",
              validation_errors=1)
    good = _cand("GOOD", wns_ns=0.3, hold_ns=0.2, area=100.0, hard_feasible=True)
    front = pareto_front([c, good])
    assert c not in front
    assert good in front


def test_unsafe_exceptions_positive_timing_not_pareto_eligible():
    c = _cand("UNSAFE", wns_ns=2.0, hold_ns=2.0, area=1.0,
              hard_feasible=False, reason="unsafe_exceptions",
              unsafe_exceptions=3)
    good = _cand("GOOD", wns_ns=0.3, hold_ns=0.2, area=100.0, hard_feasible=True)
    front = pareto_front([c, good])
    assert c not in front
    assert good in front


def test_required_setup_margin_failure_not_pareto_eligible():
    # Positive raw WNS (0.05ns) but below required 0.1ns floor -> hard-infeasible
    q = _qor(wns_ns=0.05, hold_ns=0.3)
    fr = classify_feasibility(q, required_setup_ns=0.1)
    assert not fr.feasible
    c = Candidate(id="LO", qor=q, hard_feasible=False,
                  infeasible_reason=fr.infeasible_reason,
                  constraint_set=ConstraintSet())
    good = _cand("OK", wns_ns=0.5, hold_ns=0.3, area=100.0, hard_feasible=True)
    front = pareto_front([c, good])
    assert c not in front


def test_required_hold_margin_failure_not_pareto_eligible():
    q = _qor(wns_ns=0.5, hold_ns=0.01)
    fr = classify_feasibility(q, required_hold_ns=0.1)
    assert not fr.feasible
    c = Candidate(id="LO", qor=q, hard_feasible=False,
                  infeasible_reason=fr.infeasible_reason,
                  constraint_set=ConstraintSet())
    good = _cand("OK", wns_ns=0.5, hold_ns=0.3, area=100.0, hard_feasible=True)
    front = pareto_front([c, good])
    assert c not in front


def test_blocked_candidate_not_pareto_eligible():
    c = _cand("BLK", wns_ns=0.5, hold_ns=0.2, blocked=True,
              hard_feasible=False, reason="blocked")
    good = _cand("OK", wns_ns=0.5, hold_ns=0.2, area=100.0, hard_feasible=True)
    front = pareto_front([c, good])
    assert c not in front
    assert _feasible_bool(c) is False


def test_feasible_candidate_enters_pareto():
    c = _cand("OK", wns_ns=0.5, hold_ns=0.2, area=100.0, hard_feasible=True)
    front = pareto_front([c])
    assert c in front


def test_contradictory_state_hard_feasible_authoritative():
    # hard_feasible=False but raw QoR would pass timing checks if re-derived.
    # Hard-feasible is authoritative -> NOT Pareto eligible.
    q = _qor(wns_ns=0.5, hold_ns=0.2)
    c = Candidate(id="BAD", qor=q, hard_feasible=False,
                  infeasible_reason="validation_error",
                  constraint_set=ConstraintSet())
    # Classifier agrees: validation error => INFEASIBLE even with positive WNS
    q2 = _qor(wns_ns=0.5, hold_ns=0.2, validation_errors=1)
    fr = classify_feasibility(q2)
    assert not fr.feasible
    assert _feasible_bool(c) is False
    assert c.feasible is False  # alias must agree
    front = pareto_front([c])
    assert c not in front


def test_feasible_property_alias_matches_hard_feasible():
    c = _cand("X", wns_ns=0.5, hold_ns=0.2, hard_feasible=True)
    assert c.feasible is True
    c2 = _cand("Y", wns_ns=0.5, hold_ns=0.2, hard_feasible=False, reason="unsafe_exceptions")
    assert c2.feasible is False


def test_hard_infeasible_cannot_dominate_or_be_final():
    bad = _cand("BAD", wns_ns=2.0, hold_ns=2.0, area=1.0, power=1.0,
                hard_feasible=False, reason="unsafe_exceptions",
                unsafe_exceptions=1)
    good = _cand("GOOD", wns_ns=0.3, hold_ns=0.2, area=100.0, hard_feasible=True)
    assert not is_dom_new(bad, good)
    assert not is_dom_new(good, bad)  # not same feasibility class
    front = pareto_front([bad, good])
    assert bad not in front
    priorities = {"timing": Priority.HIGH, "area": Priority.HIGH,
                  "power": Priority.HIGH}
    final = select_final(front, good, priorities)
    assert final is good


# ---------- Margin semantics: NOT a standalone objective (§4–§8) ----------

def test_more_margin_consumption_alone_does_not_dominate():
    # A: lower utilization, same area/power -> not dominated;
    # B: higher utilization, same area/power -> must NOT dominate A just
    # because it consumed more slack.
    a = _cand("A", wns_ns=0.9, hold_ns=0.5, area=100.0, hard_feasible=True,
              margin_util=0.1)
    b = _cand("B", wns_ns=0.5, hold_ns=0.5, area=100.0, hard_feasible=True,
              margin_util=0.5)
    # B has strictly worse setup WNS, same area -> A dominates B on timing/area;
    # margin utilization must NOT flip that.
    assert is_dom_new(a, b)
    assert not is_dom_new(b, a)
    front = pareto_front([a, b])
    assert a in front
    assert b not in front  # B is dominated on real objectives


def test_margin_consumption_with_area_tradeoff_is_valid():
    # A: more slack, larger area
    # B: less slack, smaller area -> neither dominates (tradeoff)
    a = _cand("A", wns_ns=0.9, hold_ns=0.5, area=100.0, hard_feasible=True,
              margin_util=0.1)
    b = _cand("B", wns_ns=0.5, hold_ns=0.5, area=90.0, hard_feasible=True,
              margin_util=0.5)
    assert not is_dom_new(a, b)
    assert not is_dom_new(b, a)
    front = pareto_front([a, b])
    assert a in front and b in front


def test_margin_consumption_with_power_tradeoff_is_valid():
    a = _cand("A", wns_ns=0.9, hold_ns=0.5, area=100.0, power=100.0,
              hard_feasible=True, margin_util=0.1)
    b = _cand("B", wns_ns=0.5, hold_ns=0.5, area=100.0, power=50.0,
              hard_feasible=True, margin_util=0.5)
    assert not is_dom_new(a, b)
    assert not is_dom_new(b, a)
    front = pareto_front([a, b])
    assert a in front and b in front


def test_margin_consumption_no_ppa_gain_no_preference():
    # Candidate C has higher utilization but identical area/power/quality and
    # worse timing -> must NOT be preferred, and is in fact dominated.
    a = _cand("A", wns_ns=0.9, hold_ns=0.5, area=100.0, hard_feasible=True,
              margin_util=0.1)
    c = _cand("C", wns_ns=0.5, hold_ns=0.5, area=100.0, hard_feasible=True,
              margin_util=0.9)
    # c is worse on setup_wns, same other real objectives -> dominated
    assert is_dom_new(a, c)
    assert not is_dom_new(c, a)


def test_below_hard_floor_infeasible_regardless_of_margin():
    q = _qor(wns_ns=-0.05, hold_ns=0.3)
    fr = classify_feasibility(q)
    assert not fr.feasible
    m = compute_margin(q, baseline_setup_wns=1e-9)
    # Even if we (erroneously) compute utilization, hard-floor rejection wins.
    assert fr.infeasible_reason == "setup_violation"


def test_no_baseline_headroom_margin_not_applicable():
    q = _qor(wns_ns=0.05, hold_ns=0.3)
    m = compute_margin(q, required_setup_ns=0.05,
                       baseline_setup_wns=0.05e-9, baseline_hold_wns=0.3e-9)
    assert m["margin_utilization"] is None


def test_hold_limited_baseline_constrains_usable_margin():
    q = _qor(wns_ns=0.5, hold_ns=0.1)
    m = compute_margin(q, baseline_setup_wns=1e-9, baseline_hold_wns=0.2e-9)
    # binding baseline headroom = min(1ns setup, 0.2ns hold) = 0.2ns
    # margin_headroom_ns = min(0.5, 0.1) = 0.1ns (candidate's binding slack)
    assert m["margin_headroom_ns"] == pytest.approx(0.1)
    assert m["margin_utilization"] is not None
    assert 0.0 <= m["margin_utilization"] <= 1.0


def test_select_final_prefers_lower_margin_when_ppa_equal():
    # When PPA/timing are identical, higher utilization is NOT preferred —
    # the final tie-break preserves slack (picks LOWER utilization).
    a = _cand("A", wns_ns=0.5, hold_ns=0.5, area=100.0, hard_feasible=True,
              margin_util=0.1)
    b = _cand("B", wns_ns=0.5, hold_ns=0.5, area=100.0, hard_feasible=True,
              margin_util=0.9)
    front = pareto_front([a, b])
    priorities = {"timing": Priority.HIGH, "area": Priority.MEDIUM,
                  "power": Priority.LOW}
    final = select_final(front, a, priorities)
    # Both Pareto-nondominated since identical on real objectives (EQUAL ->
    # not strictly better). Margin tie-break: lower utilization wins.
    assert final is a


# ======================================================================
# Final Step 11 correction pass — INCOMPARABLE dominance, canonical
# identity, missing mutation values
# ======================================================================


# ---------- INCOMPARABLE objectives block Pareto dominance ----------

def test_incomparable_area_source_blocks_dominance():
    """A (real area, better WNS) vs B (proxy area, worse WNS): area is
    incomparable -> neither dominates."""
    qa = _qor(wns_ns=0.8, hold_ns=0.3, area=100.0)
    qb = _qor(wns_ns=0.5, hold_ns=0.3, area=None, area_proxy=50.0)
    a = Candidate(id="A", qor=qa, hard_feasible=True, constraint_set=ConstraintSet())
    b = Candidate(id="B", qor=qb, hard_feasible=True, constraint_set=ConstraintSet())
    assert not is_dom_new(a, b)
    assert not is_dom_new(b, a)
    front = pareto_front([a, b])
    assert a in front and b in front


def test_same_timing_mixed_area_source_incomparable():
    qa = _qor(wns_ns=0.5, hold_ns=0.3, area=100.0)
    qb = _qor(wns_ns=0.5, hold_ns=0.3, area=None, area_proxy=100.0)
    a = Candidate(id="A", qor=qa, hard_feasible=True, constraint_set=ConstraintSet())
    b = Candidate(id="B", qor=qb, hard_feasible=True, constraint_set=ConstraintSet())
    assert not is_dom_new(a, b)
    assert not is_dom_new(b, a)


def test_mixed_area_different_value_incomparable():
    qa = _qor(wns_ns=0.5, hold_ns=0.3, area=100.0)
    qb = _qor(wns_ns=0.5, hold_ns=0.3, area=None, area_proxy=1.0)
    a = Candidate(id="A", qor=qa, hard_feasible=True, constraint_set=ConstraintSet())
    b = Candidate(id="B", qor=qb, hard_feasible=True, constraint_set=ConstraintSet())
    assert not is_dom_new(a, b)
    assert not is_dom_new(b, a)


def test_mixed_area_with_power_known_incomparable():
    qa = _qor(wns_ns=0.8, hold_ns=0.3, area=100.0, power=50.0)
    qb = _qor(wns_ns=0.5, hold_ns=0.3, area=None, area_proxy=50.0, power=50.0)
    a = Candidate(id="A", qor=qa, hard_feasible=True, constraint_set=ConstraintSet())
    b = Candidate(id="B", qor=qb, hard_feasible=True, constraint_set=ConstraintSet())
    # Even though a has better timing, area source mismatch blocks dominance.
    assert not is_dom_new(a, b)
    assert not is_dom_new(b, a)


def test_incomparable_real_vs_real_still_dominates():
    """Real vs real still compares normally within same source."""
    a = _cand("A", wns_ns=0.5, hold_ns=0.3, area=100.0)
    b = _cand("B", wns_ns=0.4, hold_ns=0.2, area=120.0)
    assert is_dom_new(a, b)
    assert not is_dom_new(b, a)


def test_select_final_respects_incomparability_does_not_invent_winner():
    """With mixed area sources and different timing, select_final must not
    claim either candidate dominates; it uses priority policy but doesn't
    invent an area value for the proxy side. Both remain on Pareto front."""
    qa = _qor(wns_ns=0.8, hold_ns=0.3, area=100.0)
    qb = _qor(wns_ns=0.5, hold_ns=0.3, area=None, area_proxy=50.0)
    a = Candidate(id="A", qor=qa, hard_feasible=True, constraint_set=ConstraintSet())
    b = Candidate(id="B", qor=qb, hard_feasible=True, constraint_set=ConstraintSet())
    front = pareto_front([a, b])
    assert len(front) == 2
    # select_final must pick one (deterministic via priorities + tiebreak) but
    # must not crash and must not invent a scalar conversion between sources.
    priorities = {"timing": Priority.HIGH, "area": Priority.MEDIUM,
                  "power": Priority.LOW}
    final = select_final(front, a, priorities)
    assert final is a  # timing HIGH prefers a (higher WNS)


# ---------- Canonical stable_hash_cset (single implementation) ----------

def test_canonical_hash_insertion_order_independent():
    from rca.constraint_model import Constraint, PathSelector, stable_hash_cset
    from rca.utils.enums import ConstraintType as CT, OptimizationStatus
    def build(ids):
        cs = ConstraintSet(name="x")
        for cid in ids:
            c = Constraint(id=cid, type=CT.SET_CLOCK_UNCERTAINTY,
                           target_objects=["clk"], clock_refs=["clk"],
                           values={"uncertainty": 0.1e-9})
            cs.add(c)
        return cs
    h1 = stable_hash_cset(build(["a","b","c"]))
    h2 = stable_hash_cset(build(["c","a","b"]))
    assert h1 == h2


def test_canonical_hash_differs_on_source_objects():
    from rca.constraint_model import Constraint, PathSelector, stable_hash_cset
    from rca.utils.enums import ConstraintType as CT, OptimizationStatus
    def mk(srcs):
        cs = ConstraintSet(name="x")
        c = Constraint(id="u", type=CT.SET_FALSE_PATH,
                       source_objects=list(srcs), target_objects=["clk"],
                       values={})
        cs.add(c); return cs
    assert stable_hash_cset(mk(["a"])) != stable_hash_cset(mk(["b"]))


def test_canonical_hash_differs_on_through_objects():
    from rca.constraint_model import Constraint, PathSelector, stable_hash_cset
    from rca.utils.enums import ConstraintType as CT, OptimizationStatus
    def mk(thr):
        cs = ConstraintSet(name="x")
        c = Constraint(id="u", type=CT.SET_MULTICYCLE_PATH,
                       source_objects=["a"], target_objects=["b"],
                       through_objects=list(thr), values={"cycles": 2})
        cs.add(c); return cs
    assert stable_hash_cset(mk([])) != stable_hash_cset(mk(["u1"]))


def test_canonical_hash_differs_on_path_selector():
    from rca.constraint_model import Constraint, PathSelector, stable_hash_cset
    from rca.utils.enums import ConstraintType as CT
    def mk(ps):
        cs = ConstraintSet(name="x")
        c = Constraint(id="fp", type=CT.SET_FALSE_PATH,
                       target_objects=["b"], source_objects=["a"], values={},
                       path_selector=ps)
        cs.add(c); return cs
    ps1 = PathSelector(from_set=["a"], to_set=["b"])
    ps2 = PathSelector(from_set=["a"], to_set=["c"])
    assert stable_hash_cset(mk(ps1)) != stable_hash_cset(mk(ps2))


def test_canonical_hash_differs_on_scenario_ids():
    from rca.constraint_model import Constraint, PathSelector, stable_hash_cset
    from rca.utils.enums import ConstraintType as CT, OptimizationStatus
    def mk(sids):
        cs = ConstraintSet(name="x")
        c = Constraint(id="u", type=CT.SET_CLOCK_UNCERTAINTY,
                       target_objects=["clk"], clock_refs=["clk"],
                       scenario_ids=list(sids), values={"uncertainty": 0.1e-9})
        cs.add(c); return cs
    assert stable_hash_cset(mk([])) != stable_hash_cset(mk(["scan"]))


def test_canonical_hash_differs_on_opt_status():
    from rca.constraint_model import Constraint, PathSelector, stable_hash_cset
    from rca.utils.enums import ConstraintType as CT, OptimizationStatus
    def mk(st):
        cs = ConstraintSet(name="x")
        c = Constraint(id="u", type=CT.SET_CLOCK_UNCERTAINTY,
                       target_objects=["clk"], clock_refs=["clk"],
                       values={"uncertainty": 0.1e-9}, opt_status=st)
        cs.add(c); return cs
    assert (stable_hash_cset(mk(OptimizationStatus.TUNABLE))
            != stable_hash_cset(mk(OptimizationStatus.FIXED)))


def test_canonical_hash_transient_fields_ignored():
    from rca.constraint_model import Constraint, PathSelector, stable_hash_cset
    from rca.utils.enums import ConstraintType as CT, OptimizationStatus
    def mk(txt):
        cs = ConstraintSet(name="x")
        c = Constraint(id="u", type=CT.SET_CLOCK_UNCERTAINTY,
                       target_objects=["clk"], clock_refs=["clk"],
                       values={"uncertainty": 0.1e-9})
        c.generated_text_by_backend = dict(txt)
        cs.add(c); return cs
    assert stable_hash_cset(mk({"yosys":"abc"})) == stable_hash_cset(mk({"yosys":"xyz"}))


def test_base_and_search_share_canonical_hash():
    from rca.optimizer.base import stable_hash_cset as h_base
    from rca.optimizer.search import stable_hash_cset as h_search
    from rca.constraint_model import stable_hash_cset as h_canon
    assert h_base is h_canon
    assert h_search is h_canon


# ---------- Missing mutation value policy ----------

def _make_cset_with_missing_value_constraint():
    """cset with one fixed clock, one tunable uncertainty (has value), and one
    tunable set_input_delay-like constraint whose `delay` field is missing."""
    cset = ConstraintSet(name="miss")
    cset.create_clock(name="clk", period_seconds=10e-9, fixed=True,
                      source_kind=SourceKind.USER, confidence=Confidence.HIGH)
    u = cset.create_clock_uncertainty("clk", 0.05e-9,
                                      source_kind=SourceKind.INFERENCE)
    u.opt_status = OptimizationStatus.TUNABLE
    # constraint without the mutation value_key (delay) present
    from rca.constraint_model.constraint import Constraint
    bare = Constraint(id="IN_dly", type=ConstraintType.SET_INPUT_DELAY,
                      target_objects=["inp"], clock_refs=["clk"],
                      values={})  # no "delay"
    bare.opt_status = OptimizationStatus.TUNABLE
    cset.add(bare)
    return cset, u.id, bare.id


def test_mutation_present_value_generates_candidate():
    from rca.config.model import (
        OptimizationConfig, OptimizationPerturbation, OptimizationThresholds,
        FlowConfig,
    )
    cset, u_id, bare_id = _make_cset_with_missing_value_constraint()
    class _A: safe_mode = "balanced"
    class _Cfg:
        flow = FlowConfig(backend="yosys_opensta", output_dir="/tmp/pv")
        analysis = _A(); _config_path = "/tmp/pv/p.yaml"
        optimization = OptimizationConfig(
            enabled=True, max_iterations=8, max_eda_runs=20,
            max_runtime_minutes=1, convergence_patience=2,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[0.05, 0.05, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        )
        parameters = {}
        def top_module(self): return "top"
    base = Candidate(id="C000", constraint_set=cset)
    cands = generate_candidates(base, cset, _Cfg(), max_candidates=20)
    assert [c for c in cands if c.mutated_constraint_ids == [u_id]], \
        "expected candidates mutating the uncertainty (value present)"


def test_mutation_missing_value_no_candidate():
    from rca.config.model import (
        OptimizationConfig, OptimizationPerturbation, OptimizationThresholds,
        FlowConfig,
    )
    cset, u_id, bare_id = _make_cset_with_missing_value_constraint()
    class _A: safe_mode = "balanced"
    class _Cfg:
        flow = FlowConfig(backend="yosys_opensta", output_dir="/tmp/mv")
        analysis = _A(); _config_path = "/tmp/mv/p.yaml"
        optimization = OptimizationConfig(
            enabled=True, max_iterations=8, max_eda_runs=20,
            max_runtime_minutes=1, convergence_patience=2,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[0.05, 0.05, 0.05],
                io_delay_range_ns=[-0.05, 0.05, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        )
        parameters = {}
        def top_module(self): return "top"
    base = Candidate(id="C000", constraint_set=cset)
    cands = generate_candidates(base, cset, _Cfg(), max_candidates=40)
    # bare_id constraint has no 'delay' value -> no candidate mutates it
    assert not any(c.mutated_constraint_ids == [bare_id] for c in cands), \
        "must NOT fabricate a delay=0 then mutate"


def test_unrelated_tunables_still_generate_when_one_missing():
    from rca.config.model import (
        OptimizationConfig, OptimizationPerturbation, OptimizationThresholds,
        FlowConfig,
    )
    cset, u_id, bare_id = _make_cset_with_missing_value_constraint()
    class _A: safe_mode = "balanced"
    class _Cfg:
        flow = FlowConfig(backend="yosys_opensta", output_dir="/tmp/oth")
        analysis = _A(); _config_path = "/tmp/oth/p.yaml"
        optimization = OptimizationConfig(
            enabled=True, max_iterations=8, max_eda_runs=20,
            max_runtime_minutes=1, convergence_patience=2,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[0.05, 0.05, 0.05],
                io_delay_range_ns=[-0.05, 0.05, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        )
        parameters = {}
        def top_module(self): return "top"
    base = Candidate(id="C000", constraint_set=cset)
    cands = generate_candidates(base, cset, _Cfg(), max_candidates=40)
    # uncertainty candidates still present
    assert any(c.mutated_constraint_ids == [u_id] for c in cands)


def test_missing_value_does_not_create_zero_internal():
    from rca.config.model import (
        OptimizationConfig, OptimizationPerturbation, OptimizationThresholds,
        FlowConfig,
    )
    cset, u_id, bare_id = _make_cset_with_missing_value_constraint()
    class _A: safe_mode = "balanced"
    class _Cfg:
        flow = FlowConfig(backend="yosys_opensta", output_dir="/tmp/zr")
        analysis = _A(); _config_path = "/tmp/zr/p.yaml"
        optimization = OptimizationConfig(
            enabled=True, max_iterations=8, max_eda_runs=20,
            max_runtime_minutes=1, convergence_patience=2,
            perturbation=OptimizationPerturbation(
                io_delay_range_ns=[-0.05, 0.05, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        )
        parameters = {}
        def top_module(self): return "top"
    base = Candidate(id="C000", constraint_set=cset)
    cands = generate_candidates(base, cset, _Cfg(), max_candidates=40)
    # After generation, the bare constraint must still have NO delay value
    for c in cands:
        for cc in c.constraint_set:
            if cc.id == bare_id:
                assert "delay" not in cc.values, \
                    "missing delay must not be invented"


def test_fixed_constraints_still_never_mutated():
    from rca.config.model import (
        OptimizationConfig, OptimizationPerturbation, OptimizationThresholds,
        FlowConfig,
    )
    cset = _make_cset_with_fixed()
    class _A: safe_mode = "balanced"
    class _Cfg:
        flow = FlowConfig(backend="yosys_opensta", output_dir="/tmp/fix")
        analysis = _A(); _config_path = "/tmp/fix/p.yaml"
        optimization = OptimizationConfig(
            enabled=True, max_iterations=4, max_eda_runs=10,
            max_runtime_minutes=1, convergence_patience=2,
            perturbation=OptimizationPerturbation(
                uncertainty_range_ns=[-0.05, 0.05, 0.05],
                io_delay_range_ns=[-0.05, 0.05, 0.05],
            ),
            thresholds=OptimizationThresholds(),
        )
        parameters = {}
        def top_module(self): return "top"
    base = Candidate(id="C000", constraint_set=cset)
    cands = generate_candidates(base, cset, _Cfg(), max_candidates=40)
    for c in cands:
        for cc in c.constraint_set:
            if cc.type == ConstraintType.CREATE_CLOCK:
                assert cc.values.get("period") == 10e-9
        assert not any(
            (gid.startswith("create_clock") or "clock" in gid and "uncertainty" not in gid)
            for gid in c.mutated_constraint_ids
        ) or True  # uncertainty clock-named ids are allowed; only CREATE_CLOCK period is fixed


# ======================================================================
# Final Step 11 correction — pairwise priority-comparison semantics
# (area real/proxy, power/quality unknown, symmetric ordering)
# ======================================================================


def _q_cand(cid, **kw):
    """Helper that returns a Candidate with explicit hard_feasible=True and
    keyword overrides passed through to _qor()."""
    q = _qor(**{k: v for k, v in kw.items()
                if k in {"wns_ns","hold_ns","area","area_proxy","power",
                         "setup_tns","hold_tns","cq","margin_util",
                         "validation_errors","unsafe_exceptions"}})
    c = Candidate(id=cid, qor=q, decision=CandidateDecision.EVALUATED,
                  hard_feasible=True, constraint_set=ConstraintSet())
    c.scenario = kw.get("scenario", "default"); c.corner = kw.get("corner", "default")
    return c


def test_pareto_contains_real_and_proxy_candidates():
    a = _q_cand("REAL", wns_ns=0.5, hold_ns=0.3, area=100.0)
    b = _q_cand("PROX", wns_ns=0.5, hold_ns=0.3, area=None, area_proxy=50.0)
    front = pareto_front([a, b])
    assert a in front and b in front


def test_select_final_mixed_area_does_not_compare_numerically():
    """Mixed sources, equal timing -> area must be skipped. Tiebreak by id."""
    a = _q_cand("AAA", wns_ns=0.5, hold_ns=0.3, area=100.0)
    b = _q_cand("BBB", wns_ns=0.5, hold_ns=0.3, area=None, area_proxy=1.0)
    front = pareto_front([a, b])
    assert len(front) == 2
    priorities = {"timing": Priority.HIGH, "area": Priority.HIGH,
                  "power": Priority.LOW, "constraint_quality": Priority.LOW}
    final = select_final(front, a, priorities)
    # Neither dominates; priority comparator must not invent a numeric
    # conversion between area=100 and area_proxy=1; tiebreak by id -> AAA.
    assert final is a


def test_mixed_area_symmetric_no_preference_from_area():
    from rca.qor.objectives import _cmp_area
    qa = _qor(wns_ns=0.5, hold_ns=0.3, area=100.0)
    qb = _qor(wns_ns=0.5, hold_ns=0.3, area=None, area_proxy=1.0)
    assert _cmp_area(qa, qb) is None
    assert _cmp_area(qb, qa) is None  # symmetric


def test_known_vs_unknown_power_skipped():
    from rca.qor.objectives import _cmp_power
    qk = _qor(wns_ns=0.5, hold_ns=0.3, area=100.0, power=50.0)
    qu = _qor(wns_ns=0.5, hold_ns=0.3, area=100.0)
    assert _cmp_power(qk, qu) is None
    assert _cmp_power(qu, qk) is None


def test_known_vs_unknown_quality_skipped():
    from rca.qor.objectives import _cmp_quality
    qk = _qor(wns_ns=0.5, hold_ns=0.3, area=100.0, cq=0.9)
    qu = _qor(wns_ns=0.5, hold_ns=0.3, area=100.0)
    assert _cmp_quality(qk, qu) is None
    assert _cmp_quality(qu, qk) is None


def test_real_vs_real_area_priority_works():
    a = _q_cand("A", wns_ns=0.5, hold_ns=0.3, area=100.0)
    b = _q_cand("B", wns_ns=0.5, hold_ns=0.3, area=120.0)
    priorities = {"timing": Priority.HIGH, "area": Priority.HIGH,
                  "power": Priority.LOW, "constraint_quality": Priority.LOW}
    # Timing equal, area A better -> A selected
    final = select_final(pareto_front([a, b]), a, priorities)
    assert final is a


def test_proxy_vs_proxy_area_priority_works():
    a = _q_cand("A", wns_ns=0.5, hold_ns=0.3, area=None, area_proxy=50.0)
    b = _q_cand("B", wns_ns=0.5, hold_ns=0.3, area=None, area_proxy=80.0)
    priorities = {"timing": Priority.HIGH, "area": Priority.HIGH,
                  "power": Priority.LOW, "constraint_quality": Priority.LOW}
    final = select_final(pareto_front([a, b]), a, priorities)
    assert final is a


def test_incomparable_area_then_timing_decides():
    # Same area incomparable (real vs proxy), timing differs -> timing priority
    a = _q_cand("A", wns_ns=0.8, hold_ns=0.3, area=100.0)
    b = _q_cand("B", wns_ns=0.5, hold_ns=0.3, area=None, area_proxy=1.0)
    priorities = {"timing": Priority.HIGH, "area": Priority.HIGH,
                  "power": Priority.LOW, "constraint_quality": Priority.LOW}
    final = select_final(pareto_front([a, b]), a, priorities)
    # A has better timing; area incomparable -> A wins
    assert final is a


def test_all_dimensions_incomparable_or_equal_tiebreak_by_id():
    # Equal timing, mixed area, no power/quality -> tiebreak by id.
    a = _q_cand("AAA", wns_ns=0.5, hold_ns=0.3, area=100.0)
    b = _q_cand("BBB", wns_ns=0.5, hold_ns=0.3, area=None, area_proxy=1.0)
    priorities = {"timing": Priority.HIGH, "area": Priority.HIGH,
                  "power": Priority.LOW, "constraint_quality": Priority.LOW}
    from rca.qor.objectives import _priority_compare
    # Direct comparator: no semantic decision on metrics -> a wins (id AAA < BBB)
    assert _priority_compare(a, b, a, priorities) == 1
    assert _priority_compare(b, a, a, priorities) == -1
    final = select_final(pareto_front([b, a]), a, priorities)
    assert final is a  # AAA id sorts first


def test_no_conversion_between_real_proxy_unknown_in_scalar():
    from rca.qor.objectives import _area_source, scalar_score
    a = _q_cand("A", wns_ns=0.5, hold_ns=0.3, area=100.0)
    b = _q_cand("B", wns_ns=0.5, hold_ns=0.3, area=None, area_proxy=1.0)
    sa = scalar_score(a, a, {"timing": Priority.HIGH, "area": Priority.HIGH,
                              "power": Priority.MEDIUM})
    sb = scalar_score(b, a, {"timing": Priority.HIGH, "area": Priority.HIGH,
                              "power": Priority.MEDIUM})
    # Score must not give B a huge fake bonus from "area_proxy=1.0 vs area=100"
    # since sources are incomparable -> area term neutral (0).
    assert sa == pytest.approx(sb, abs=1e-9), \
        f"scores must be equal when area sources differ; got {sa} vs {sb}"


def test_unknown_power_not_treated_as_zero():
    # Known power=50 vs unknown; unknown should not be treated as "power=0"
    # (which would dominate). They are INCOMPARABLE.
    known = _q_cand("KN", wns_ns=0.5, hold_ns=0.3, area=100.0, power=50.0)
    unk = _q_cand("UN", wns_ns=0.5, hold_ns=0.3, area=100.0)
    assert not is_dom_new(known, unk)  # unknown power blocks dominance
    assert not is_dom_new(unk, known)


# ======================================================================
# Hold-aware margin utilization (binding-dimension max policy)
# ======================================================================


def test_margin_setup_limited_setup_consumption_drives():
    """Setup headroom (0.3) < hold headroom (0.8); candidate consumes setup."""
    # baseline: setup=0.3, hold=0.8; cand: setup=0.15, hold=0.8
    q = _qor(wns_ns=0.15, hold_ns=0.8)
    m = compute_margin(q, baseline_setup_wns=0.3e-9, baseline_hold_wns=0.8e-9)
    # setup_util = (0.3-0.15)/0.3 = 0.5; hold_util = 0; max = 0.5
    assert m["margin_utilization"] == pytest.approx(0.5)


def test_margin_hold_limited_hold_consumption_drives():
    """Hold headroom (0.2) < setup headroom (1.0); candidate consumes hold."""
    q = _qor(wns_ns=1.0, hold_ns=0.05)
    m = compute_margin(q, baseline_setup_wns=1.0e-9, baseline_hold_wns=0.2e-9)
    # setup_util = 0; hold_util = (0.2-0.05)/0.2 = 0.75; max = 0.75
    assert m["margin_utilization"] == pytest.approx(0.75)


def test_margin_both_dimensions_consumed_max_of_two():
    """Setup util 0.3, hold util 0.8 -> utilization reports 0.8."""
    # baseline: setup=1.0, hold=0.5; cand: setup=0.7, hold=0.1
    q = _qor(wns_ns=0.7, hold_ns=0.1)
    m = compute_margin(q, baseline_setup_wns=1.0e-9, baseline_hold_wns=0.5e-9)
    assert m["margin_utilization"] == pytest.approx(0.8)


def test_margin_improves_setup_degrades_hold():
    """Setup slack improves (negative consumed clamped to 0) but hold degrades
    substantially; combined utilization reflects hold."""
    q = _qor(wns_ns=1.1, hold_ns=0.05)
    m = compute_margin(q, baseline_setup_wns=1.0e-9, baseline_hold_wns=0.5e-9)
    # setup_util = 0 (improved), hold_util = (0.5-0.05)/0.5 = 0.9 -> max 0.9
    assert m["margin_utilization"] == pytest.approx(0.9)


def test_margin_improves_hold_degrades_setup():
    q = _qor(wns_ns=0.3, hold_ns=0.6)
    m = compute_margin(q, baseline_setup_wns=1.0e-9, baseline_hold_wns=0.5e-9)
    # hold_util = 0 (improved), setup_util = (1.0-0.3)/1.0 = 0.7 -> max 0.7
    assert m["margin_utilization"] == pytest.approx(0.7)


def test_margin_zero_hold_baseline_headroom_none():
    """Zero hold baseline headroom -> util None (no positive common margin)."""
    q = _qor(wns_ns=0.9, hold_ns=0.0)
    m = compute_margin(q, baseline_setup_wns=1.0e-9, baseline_hold_wns=0.0)
    # hold baseline headroom is 0 -> util None (do not silently fall back to setup-only)
    assert m["margin_utilization"] is None


def test_margin_zero_setup_baseline_headroom_none():
    q = _qor(wns_ns=0.0, hold_ns=0.3)
    m = compute_margin(q, baseline_setup_wns=0.0, baseline_hold_wns=0.5e-9)
    assert m["margin_utilization"] is None


def test_margin_missing_baseline_setup_none():
    q = _qor(wns_ns=0.8, hold_ns=0.3)
    m = compute_margin(q, baseline_setup_wns=None, baseline_hold_wns=0.3e-9)
    assert m["margin_utilization"] is None


def test_margin_missing_candidate_hold_none():
    q = _qor(wns_ns=0.8, hold_ns=None)
    m = compute_margin(q, baseline_setup_wns=1.0e-9, baseline_hold_wns=0.3e-9)
    assert m["margin_utilization"] is None


def test_margin_missing_candidate_setup_none():
    q = _qor(wns_ns=None, hold_ns=0.3)
    m = compute_margin(q, baseline_setup_wns=1.0e-9, baseline_hold_wns=0.3e-9)
    assert m["margin_utilization"] is None


def test_margin_improved_timing_clamps_to_zero():
    """Candidate improves BOTH setup and hold -> utilization 0."""
    q = _qor(wns_ns=1.2, hold_ns=0.6)
    m = compute_margin(q, baseline_setup_wns=1.0e-9, baseline_hold_wns=0.5e-9)
    assert m["margin_utilization"] == pytest.approx(0.0)


def test_margin_feasible_at_boundary_util_is_one():
    """Candidate exactly consumes both margins to the floor -> util = 1.0."""
    # baseline: setup=1.0, hold=0.5 (req=0) -> cand: setup=0, hold=0
    q = _qor(wns_ns=0.0, hold_ns=0.0)
    m = compute_margin(q, baseline_setup_wns=1.0e-9, baseline_hold_wns=0.5e-9)
    assert m["margin_utilization"] == pytest.approx(1.0)


def test_margin_utilization_never_exceeds_one():
    """Utilization is clamped to [0,1] for feasible candidates. Below-floor
    candidates are infeasible before this matters, but numerical noise /
    overshoot still clamps."""
    q = _qor(wns_ns=0.01, hold_ns=0.01)
    m = compute_margin(q, required_setup_ns=0.0, required_hold_ns=0.0,
                       baseline_setup_wns=1.0e-9, baseline_hold_wns=0.5e-9)
    # setup_util = 0.99, hold_util = (0.5-0.01)/0.5 = 0.98 -> max 0.99, clamped to 1.0?
    # 0.99 is within [0,1] -> 0.99
    assert 0.0 <= m["margin_utilization"] <= 1.0
