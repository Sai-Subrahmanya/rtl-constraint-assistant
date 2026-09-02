"""
Global MCMM aggregation policies (Step 12 §4–§9).

This module reduces a set of per-scenario QoR into:
- a **global feasibility** verdict (feasible / infeasible / blocked / invalid)
  that requires EVERY required active scenario to be feasible;
- **conservative / binding global objectives** that keep UNKNOWN as UNKNOWN
  and never average away a limiting scenario;
- a **global margin signal** that uses the exact Step-11 margin math per
  scenario and then reports the binding (worst) signal plus its limiting
  scenario;
- an **MCMM Pareto comparator** over the complete scenario set.

The aggregation never collapses per-scenario QoR into one number: the
per-scenario records are always retained by the caller (see
:mod:`rca.mcmm.model`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..qor.objectives import (
    AREA_PROXY,
    AREA_REAL,
    AREA_UNKNOWN,
    Direction,
    OBJECTIVE_SPECS,
    _area_value,
    _cmp_metric,
    compare_objectives,
    objective_vector,
)
from .model import (
    BLOCKED,
    FEASIBLE,
    GLOBAL_BLOCKED,
    GLOBAL_FEASIBLE,
    GLOBAL_INFEASIBLE,
    GLOBAL_INVALID,
    INFEASIBLE,
    INVALID,
    MCMMResult,
    ObjectiveAggregate,
    ScenarioQoR,
)

# Objective names that carry area-source semantics (INCOMPARABLE when mixed).
_AREA_OBJECTIVE = "area"

_KNOWN_STATUSES = {FEASIBLE, INFEASIBLE, BLOCKED, INVALID}


# ---------------------------------------------------------------------------
# Per-scenario feasibility classification
# ---------------------------------------------------------------------------

def scenario_feasibility(sqor: ScenarioQoR,
                         *,
                         required_setup_ns: float = 0.0,
                         required_hold_ns: float = 0.0,
                         allow_unsafe_exceptions: bool = False) -> str:
    """Classify a single scenario's feasibility status.

    Returns one of ``feasible`` / ``infeasible`` / ``blocked`` / ``invalid``.
    Reuses :func:`rca.qor.objectives.classify_feasibility` for the authoritative
    classification so the Step-11 math is preserved.
    """
    from ..qor.objectives import classify_feasibility

    if sqor.qor is None:
        sqor.status = BLOCKED
        sqor.blocked = True
        sqor.feasible = False
        sqor.infeasible_reason = sqor.infeasible_reason or "blocked_no_qor"
        return BLOCKED

    fr = classify_feasibility(
        sqor.qor,
        required_setup_ns=required_setup_ns,
        required_hold_ns=required_hold_ns,
        allow_unsafe_exceptions=allow_unsafe_exceptions,
    )
    # Preserve scenario-identity diagnostics (added by the evaluator) and append
    # classifier diagnostics so the per-scenario audit trail is not lost.
    existing = list(sqor.diagnostics)
    sqor.diagnostics = existing + list(fr.diagnostics)
    sqor.feasible = fr.feasible
    sqor.blocked = fr.blocked
    sqor.infeasible_reason = fr.infeasible_reason
    if fr.blocked:
        sqor.status = BLOCKED
    elif fr.infeasible_reason == "validation_error":
        sqor.status = INVALID
        sqor.invalid = True
    elif not fr.feasible:
        sqor.status = INFEASIBLE
    else:
        sqor.status = FEASIBLE
        sqor.invalid = False
    return sqor.status


def scenario_margin(sqor: ScenarioQoR,
                    *,
                    required_setup_ns: float = 0.0,
                    required_hold_ns: float = 0.0,
                    baseline_setup_wns: float | None = None,
                    baseline_hold_wns: float | None = None) -> None:
    """Compute the Step-11 margin math for one scenario (in place).

    Uses :func:`rca.qor.objectives.compute_margin`, which requires BOTH
    baseline setup/hold headroom to be known and positive.  Never invents a
    one-dimensional fallback.
    """
    from ..qor.objectives import compute_margin

    if sqor.qor is None:
        sqor.margin_headroom_ns = None
        sqor.margin_utilization = None
        return
    m = compute_margin(
        sqor.qor,
        required_setup_ns=required_setup_ns,
        required_hold_ns=required_hold_ns,
        baseline_setup_wns=baseline_setup_wns,
        baseline_hold_wns=baseline_hold_wns,
    )
    sqor.margin_headroom_ns = m["margin_headroom_ns"]
    sqor.margin_utilization = m["margin_utilization"]


# ---------------------------------------------------------------------------
# Global feasibility
# ---------------------------------------------------------------------------

def global_feasibility(result: MCMMResult,
                       *,
                       required_setup_ns: float = 0.0,
                       required_hold_ns: float = 0.0,
                       allow_unsafe_exceptions: bool = False,
                       ) -> MCMMResult:
    """Aggregate per-scenario feasibility into a global verdict (in place).

    A candidate is globally feasible ONLY if every required active scenario is
    feasible.  Any scenario that is invalid / blocked / infeasible drives the
    global verdict conservatively.  The reason for each failing scenario is
    retained on the per-scenario record.
    """
    active = list(result.active_scenario_ids)
    if not active:
        result.feasible = False
        result.infeasible = False
        result.blocked = True
        result.invalid = False
        result.global_status = GLOBAL_BLOCKED
        result.global_reason = "no active scenarios"
        result.limiting_scenarios = []
        return result

    # Every required/active scenario MUST have a result.  A missing result
    # (or a referenced scenario with no definition) is treated conservatively
    # as BLOCKED — it can NEVER make the candidate globally feasible.  This is
    # deliberately the opposite of silently skipping it.
    for sid in active:
        sqor = result.scenario_results.get(sid)
        if sqor is None:
            # A required scenario with no recorded result at all.
            sqor = ScenarioQoR(candidate_id=result.candidate_id, scenario_id=sid)
            sqor.status = BLOCKED
            sqor.blocked = True
            sqor.feasible = False
            sqor.infeasible_reason = "missing_scenario_result"
            sqor.diagnostics = [f"scenario={sid}: missing_scenario_result"]
            result.scenario_results[sid] = sqor
            continue
        scenario_feasibility(
            sqor,
            required_setup_ns=required_setup_ns,
            required_hold_ns=required_hold_ns,
            allow_unsafe_exceptions=allow_unsafe_exceptions,
        )

    invalid_sids = [sid for sid in active
                    if (result.scenario_results.get(sid) and
                        result.scenario_results[sid].status == INVALID)]
    blocked_sids = [sid for sid in active
                    if (result.scenario_results.get(sid) and
                        result.scenario_results[sid].status == BLOCKED)]
    infeasible_sids = [sid for sid in active
                       if (result.scenario_results.get(sid) and
                           result.scenario_results[sid].status == INFEASIBLE)]
    feasible_sids = [sid for sid in active
                     if (result.scenario_results.get(sid) and
                         result.scenario_results[sid].status == FEASIBLE)]

    # Conservative precedence: invalid > blocked > infeasible > feasible.
    if invalid_sids:
        result.feasible = False
        result.infeasible = True
        result.blocked = False
        result.invalid = True
        result.global_status = GLOBAL_INVALID
        result.global_reason = f"invalid in {','.join(sorted(invalid_sids))}"
        result.limiting_scenarios = sorted(invalid_sids)
    elif blocked_sids:
        result.feasible = False
        result.infeasible = True
        result.blocked = True
        result.invalid = False
        result.global_status = GLOBAL_BLOCKED
        result.global_reason = f"blocked in {','.join(sorted(blocked_sids))}"
        result.limiting_scenarios = sorted(blocked_sids)
    elif infeasible_sids:
        result.feasible = False
        result.infeasible = True
        result.blocked = False
        result.invalid = False
        result.global_status = GLOBAL_INFEASIBLE
        # Prefer infeasible scenarios over feasible when reporting reasons.
        reasons = []
        for sid in sorted(infeasible_sids):
            sqor = result.scenario_results[sid]
            reasons.append(f"{sid}:{sqor.infeasible_reason}")
        result.global_reason = "; ".join(reasons)
        result.limiting_scenarios = sorted(infeasible_sids)
    else:
        result.feasible = True
        result.infeasible = False
        result.blocked = False
        result.invalid = False
        result.global_status = GLOBAL_FEASIBLE
        result.global_reason = ""
        result.limiting_scenarios = sorted(feasible_sids)

    # Mark limiting scenarios on per-scenario records.
    limiting_set = set(result.limiting_scenarios)
    for sid, sqor in result.scenario_results.items():
        sqor.limiting = sid in limiting_set
        sqor.is_global_binding = sid in limiting_set
    return result


# ---------------------------------------------------------------------------
# Global objectives (conservative / binding aggregation)
# ---------------------------------------------------------------------------

def _scalar_value(qor, name: str) -> tuple[float | None, str | None]:
    """(value, area_source) for a QoRResult objective; None value => UNKNOWN."""
    if name == "area":
        av = _area_value(qor)
        return (av.value, av.source)
    vec = objective_vector(qor)
    entry = vec.get(name)
    if isinstance(entry, tuple):
        return (entry[0], entry[1] if len(entry) > 1 else None)
    return (None, None)


def aggregate_objectives(result: MCMMResult,
                         required_scenarios: Iterable[str] | None = None) -> MCMMResult:
    """Compute conservative/binding global objectives (in place).

    For MAXIMIZE objectives the binding value is the minimum across known
    scenarios; for MINIMIZE objectives it is the maximum.  If any required
    scenario is UNKNOWN the objective is UNKNOWN (no fabricated value).  For
    area, mixed real/proxy sources make the objective INCOMPARABLE.  The
    limiting scenario(s) are always retained.
    """
    active = list(required_scenarios if required_scenarios is not None
                  else result.active_scenario_ids)

    for name, spec in OBJECTIVE_SPECS.items():
        is_area = name == _AREA_OBJECTIVE
        direction = "maximize" if spec.direction == Direction.MAXIMIZE else "minimize"

        known: list[tuple[str, float]] = []
        area_sources: dict[str, str] = {}
        unknown_sids: list[str] = []

        for sid in active:
            sqor = result.scenario_results.get(sid)
            if sqor is None or sqor.qor is None:
                unknown_sids.append(sid)
                continue
            val, source = _scalar_value(sqor.qor, name)
            if val is None:
                unknown_sids.append(sid)
            else:
                known.append((sid, float(val)))
                if is_area:
                    area_sources[sid] = source or AREA_UNKNOWN

        agg = ObjectiveAggregate(
            name=name,
            direction=direction,
            scenarios=list(active),
        )

        if is_area:
            # Area source semantics (Step 12 §6).
            source_set = set(area_sources.values())
            if unknown_sids:
                # ANY required scenario with UNKNOWN area => global area is
                # UNKNOWN.  Never aggregate only the known scenarios and never
                # fabricate zero/another placeholder.  Retain the IDs that are
                # responsible for the unknown (Step 12 §6).
                agg.unknown = True
                agg.incomparable = False
                agg.value = None
                agg.area_source = AREA_UNKNOWN
                agg.limiting = sorted(unknown_sids)
            elif not known:
                # Every required scenario is UNKNOWN.
                agg.unknown = True
                agg.area_source = AREA_UNKNOWN
                agg.limiting = sorted(unknown_sids)
            elif len(source_set) == 1:
                # All known scenarios share a single source (real or proxy) and
                # none are unknown -> comparable (binding per direction).
                agg.area_source = next(iter(source_set))
                agg.value = _binding_value(known, direction)
                agg.limiting = _limiting_sids(known, agg.value)
            else:
                # Mixed real/proxy sources, all known -> INCOMPARABLE
                # (no numeric conversion ever implied).
                agg.unknown = True
                agg.incomparable = True
                agg.area_source = "mixed"
                agg.value = None
                agg.limiting = sorted(area_sources.keys())
            result.objectives[name] = agg
            continue

        # Non-area objectives.
        if not known:
            agg.unknown = True
            agg.limiting = sorted(unknown_sids)
        elif unknown_sids:
            # Some scenarios known, some UNKNOWN -> conservative UNKNOWN.
            agg.unknown = True
            agg.limiting = sorted(unknown_sids)
        else:
            agg.value = _binding_value(known, direction)
            agg.limiting = _limiting_sids(known, agg.value)
            agg.unknown = False
        result.objectives[name] = agg

    # For power specifically, retain the scenario responsible for missing power.
    _annotate_power(result, active)
    return result


def _binding_value(known: list[tuple[str, float]], direction: str) -> float:
    values = [v for _, v in known]
    if direction == "maximize":
        return min(values)
    return max(values)


def _limiting_sids(known: list[tuple[str, float]], binding: float) -> list[str]:
    return sorted(sid for sid, v in known if v == binding)


def _annotate_power(result: MCMMResult, active: Iterable[str]) -> None:
    """Attach the scenario(s) responsible for missing power data (Step 12 §7).

    Power that is UNKNOWN stays UNKNOWN: it is never converted to zero, and the
    scenario(s) carrying the missing information are retained on the aggregate.
    """
    agg = result.objectives.get("power")
    if agg is None:
        return
    missing_power: list[str] = []
    for sid in active:
        sqor = result.scenario_results.get(sid)
        if sqor is None or sqor.qor is None:
            missing_power.append(sid)
            continue
        if sqor.qor.power is None:
            missing_power.append(sid)
    if missing_power:
        agg.unknown = True
        agg.limiting = sorted(missing_power)


def finalize_limiting(result: MCMMResult) -> MCMMResult:
    """Derive the candidate-level limiting scenario(s) (in place).

    - When the candidate is NOT globally feasible, the limiting scenarios are
      the failing scenario(s) already recorded by :func:`global_feasibility`.
    - When globally feasible, the limiting scenarios are the union of the
      binding scenario(s) across the global objectives (and the margin), so a
      user can answer \"which mode/corner is limiting this candidate?\".
    """
    if not result.feasible:
        return result
    limiting: list[str] = []
    for agg in result.objectives.values():
        if agg.value is None:
            continue
        for sid in agg.limiting:
            if sid not in limiting:
                limiting.append(sid)
    for sid in result.margin_limiting_scenarios:
        if sid not in limiting:
            limiting.append(sid)
    if not limiting:
        limiting = list(result.active_scenario_ids)
    result.limiting_scenarios = sorted(limiting)
    for sid, sqor in result.scenario_results.items():
        sqor.limiting = sid in result.limiting_scenarios
    return result


# ---------------------------------------------------------------------------
# Global margin
# ---------------------------------------------------------------------------

def global_margin(result: MCMMResult,
                  *,
                  required_setup_ns: float = 0.0,
                  required_hold_ns: float = 0.0,
                  baseline_by_scenario: dict[str, tuple[float | None, float | None]]
                  | None = None) -> MCMMResult:
    """Aggregate per-scenario margin into a global binding signal (in place).

    margin_utilization is DIAGNOSTIC (not a Pareto objective).  The global
    binding signal uses the worst (highest) utilization across scenarios and
    the worst (lowest) headroom, retaining the limiting scenario(s).
    """
    baseline_by_scenario = baseline_by_scenario or {}
    headrooms: list[tuple[str, float]] = []
    utils: list[tuple[str, float]] = []
    for sid in result.active_scenario_ids:
        sqor = result.scenario_results.get(sid)
        if sqor is None:
            continue
        b = baseline_by_scenario.get(sid, (None, None))
        baseline_setup_wns = b[0] if isinstance(b, tuple) and len(b) >= 1 else None
        baseline_hold_wns = b[1] if isinstance(b, tuple) and len(b) >= 2 else None
        scenario_margin(
            sqor,
            required_setup_ns=required_setup_ns,
            required_hold_ns=required_hold_ns,
            baseline_setup_wns=baseline_setup_wns,
            baseline_hold_wns=baseline_hold_wns,
        )
        if sqor.margin_headroom_ns is not None:
            headrooms.append((sid, float(sqor.margin_headroom_ns)))
        if sqor.margin_utilization is not None:
            utils.append((sid, float(sqor.margin_utilization)))

    if headrooms:
        binding_headroom = min(v for _, v in headrooms)
        result.margin_headroom_ns = binding_headroom
    else:
        result.margin_headroom_ns = None

    if utils:
        binding_util = max(v for _, v in utils)
        result.margin_utilization = binding_util
        result.margin_limiting_scenarios = sorted(
            sid for sid, v in utils if v == binding_util)
    else:
        result.margin_utilization = None
        result.margin_limiting_scenarios = []
    return result


# ---------------------------------------------------------------------------
# MCMM Pareto (Step 12 §9)
# ---------------------------------------------------------------------------

def _candidate_active_set(c) -> list[str]:
    """Active scenario ids for a candidate, from its MCMM records."""
    if getattr(c, "mcmm", None) is not None:
        return list(getattr(c.mcmm, "active_scenario_ids", None) or [])
    # Single-scenario candidate.
    scenario = getattr(c, "scenario", "default")
    return [scenario] if scenario else ["default"]


def _candidate_qor_map(c, active: list[str]) -> dict[str, Any]:
    """Map scenario_id -> QoRResult for a candidate."""
    out: dict[str, Any] = {}
    mcmm = getattr(c, "mcmm", None)
    if mcmm is not None and getattr(mcmm, "scenario_results", None):
        for sid, sqor in mcmm.scenario_results.items():
            out[sid] = sqor.qor
        return out
    # Single-scenario shim: same QoR for the only active scenario.
    if getattr(c, "qor", None) is not None:
        for sid in active:
            if sid not in out:
                out[sid] = c.qor
    return out


def _globally_eligible(c) -> bool:
    """Return True when a candidate is globally feasible and usable in Pareto.

    A candidate is eligible only when every required active scenario is
    feasible (per the authoritative per-scenario classification) and it is not
    blocked / invalid / unsafe.
    """
    mcmm = getattr(c, "mcmm", None)
    if mcmm is None:
        return bool(getattr(c, "hard_feasible", False))
    # Global feasibility is captured on the candidate during classification.
    if getattr(c, "blocked", False) or not getattr(c, "hard_feasible", False):
        return False
    if getattr(c, "infeasible_reason", "") == "unsafe_exceptions":
        return False
    for sid, sqor in mcmm.scenario_results.items():
        if not sqor.feasible:
            return False
    return True


def mcmm_is_dominating(a, b) -> bool:
    """True when ``a`` dominates ``b`` over the complete scenario set.

    Requires:
    - both globally feasible / eligible;
    - identical active scenario sets;
    - in EVERY scenario, ``a`` is >= ``b`` on every objective;
    - in at least one scenario, ``a`` is strictly better;
    - no per-scenario INCOMPARABLE comparison (area source mismatch or
      UNKNOWN-vs-KNOWN) anywhere, which blocks dominance conservatively.
    """
    active_a = _candidate_active_set(a)
    active_b = _candidate_active_set(b)
    if set(active_a) != set(active_b):
        return False
    if not _globally_eligible(a) or not _globally_eligible(b):
        return False
    qa_map = _candidate_qor_map(a, active_a)
    qb_map = _candidate_qor_map(b, active_a)
    any_better = False
    for sid in active_a:
        qa = qa_map.get(sid)
        qb = qb_map.get(sid)
        if qa is None or qb is None:
            # One side missing a scenario's QoR -> conservative no dominance.
            return False
        va = objective_vector(qa)
        vb = objective_vector(qb)
        for name in OBJECTIVE_SPECS:
            r = _cmp_metric(name, va.get(name), vb.get(name))
            if r.value == "incomparable":
                return False
            if r.value == "worse":
                return False
            if r.value == "better":
                any_better = True
    return any_better


def mcmm_pareto_front(candidates: Iterable) -> list:
    """Compute the MCMM Pareto front over the complete scenario set."""
    cands = [c for c in candidates if _globally_eligible(c)]
    # Deterministic insertion order (by candidate id) for reproducibility.
    cands.sort(key=lambda c: getattr(c, "id", ""))
    front: list = []
    for c in cands:
        dominated = False
        new_front: list = []
        for existing in front:
            if mcmm_is_dominating(existing, c):
                dominated = True
                new_front.append(existing)
            elif mcmm_is_dominating(c, existing):
                continue
            else:
                new_front.append(existing)
        if not dominated:
            new_front.append(c)
        front = new_front
    for c in front:
        c.decision = _decision("PARETO")
        c.pareto_member = True
    return front


def mcmm_scalar_score(c, baseline, priorities) -> float:
    """Diagnostic scalar score computed over the global binding objectives.

    This is REPORTING ONLY (Step 12 §9): it is not used as the primary Pareto
    or final-selection mechanism.  UNKNOWN / INCOMPARABLE objectives contribute
    nothing rather than a fabricated number.
    """
    mcmm = getattr(c, "mcmm", None)
    if mcmm is None or not getattr(c, "hard_feasible", False):
        return float("-inf")
    if getattr(c, "blocked", False):
        return float("-inf")

    from ..qor.objectives import PRIORITY_WEIGHTS, _pstr

    score = 0.0
    t = PRIORITY_WEIGHTS.get(_pstr(priorities.get("timing", "high")), 2.0)
    a = PRIORITY_WEIGHTS.get(_pstr(priorities.get("area", "medium")), 1.0)
    pw = PRIORITY_WEIGHTS.get(_pstr(priorities.get("power", "medium")), 1.0)
    obj = mcmm.objectives

    for name in ("setup_wns", "hold_wns"):
        agg = obj.get(name)
        if agg is not None and not agg.unknown and agg.value is not None:
            score += t * agg.value * 1e9

    area = obj.get("area")
    if baseline is not None:
        base = getattr(baseline, "mcmm", None)
        base_area = base.objectives.get("area") if base else None
        if (area is not None and not area.unknown and not area.incomparable
                and area.value is not None and base_area is not None
                and not base_area.unknown and not base_area.incomparable
                and base_area.value not in (None, 0)
                and area.area_source == base_area.area_source):
            score += a * (1.0 - area.value / base_area.value)

    power = obj.get("power")
    if baseline is not None:
        base = getattr(baseline, "mcmm", None)
        base_power = base.objectives.get("power") if base else None
        if (power is not None and not power.unknown and power.value is not None
                and base_power is not None and not base_power.unknown
                and base_power.value not in (None, 0)):
            score += pw * (1.0 - power.value / base_power.value)
    return score


def mcmm_select_final(front, baseline, priorities):
    """Final selection over the MCMM Pareto front.

    Ordering policy (deterministic):
      1. Global feasibility first (infeasible/unsafe sort after feasible).
      2. Lexicographic priority buckets (timing -> quality -> area -> power),
         comparing the global binding objective values conservatively.
      3. Lower global margin_utilization (residual slack) wins as a secondary
         signal (NOT a Pareto objective).
      4. Deterministic candidate-id tie-break.
    """
    from functools import cmp_to_key

    if not front:
        return None
    pool = list(front)
    if baseline is not None and baseline not in pool and _globally_eligible(baseline):
        pool.append(baseline)
    safe = [c for c in pool if _globally_eligible(c)]
    if not safe:
        return None
    return max(safe, key=cmp_to_key(
        lambda a, b: _priority_compare(a, b, baseline, priorities)))


def _priority_compare(a, b, baseline, priorities) -> int:
    """Return -1/0/+1 comparator used by :func:`mcmm_select_final`."""
    fa = _globally_eligible(a)
    fb = _globally_eligible(b)
    if fa and not fb:
        return 1
    if fb and not fa:
        return -1
    if not fa and not fb:
        ia, ib = getattr(a, "id", ""), getattr(b, "id", "")
        if ia < ib:
            return 1
        if ia > ib:
            return -1
        return 0

    for name, _w in _ordered_buckets(priorities):
        cmp = _compare_global_metric(name, a, b)
        if cmp is None or cmp == 0:
            continue
        return cmp

    ua = getattr(a.mcmm, "margin_utilization", None) if getattr(a, "mcmm", None) else None
    ub = getattr(b.mcmm, "margin_utilization", None) if getattr(b, "mcmm", None) else None
    if ua is not None and ub is not None and abs(ua - ub) > 1e-12:
        return 1 if ua < ub else -1

    ia, ib = getattr(a, "id", ""), getattr(b, "id", "")
    if ia < ib:
        return 1
    if ia > ib:
        return -1
    return 0


def _compare_global_metric(name: str, a, b) -> int | None:
    from ..qor.objectives import _cmp_higher_better, _cmp_lower_better

    a_mcmm = getattr(a, "mcmm", None)
    b_mcmm = getattr(b, "mcmm", None)
    if a_mcmm is None or b_mcmm is None:
        return None
    if name == "timing":
        return _glob_timing(a_mcmm, b_mcmm)
    if name == "quality":
        return _glob_value_cmp(a_mcmm, b_mcmm, "constraint_quality", higher=True)
    if name == "area":
        return _glob_area_cmp(a_mcmm, b_mcmm)
    if name == "power":
        return _glob_value_cmp(a_mcmm, b_mcmm, "power", higher=False)
    return None


def _glob_value_cmp(a_mcmm, b_mcmm, name: str, *, higher: bool) -> int | None:
    from ..qor.objectives import _cmp_higher_better, _cmp_lower_better

    aa = a_mcmm.objectives.get(name)
    bb = b_mcmm.objectives.get(name)
    if aa is None or bb is None or aa.unknown or bb.unknown or aa.incomparable or bb.incomparable:
        return None
    if aa.value is None or bb.value is None:
        return None
    if higher:
        return _cmp_higher_better(aa.value, bb.value)
    return _cmp_lower_better(aa.value, bb.value)


def _glob_timing(a_mcmm, b_mcmm) -> int | None:
    from ..qor.objectives import _cmp_higher_better

    a_s = a_mcmm.objectives.get("setup_wns")
    b_s = b_mcmm.objectives.get("setup_wns")
    a_h = a_mcmm.objectives.get("hold_wns")
    b_h = b_mcmm.objectives.get("hold_wns")
    if (a_s is None or b_s is None or a_h is None or b_h is None):
        return None
    if (a_s.unknown or b_s.unknown or a_h.unknown or b_h.unknown
            or a_s.incomparable or b_s.incomparable or a_h.incomparable or b_h.incomparable):
        return None
    if a_s.value is None or b_s.value is None:
        return None
    c = _cmp_higher_better(a_s.value, b_s.value)
    if c is None or c != 0:
        return c
    if a_h.value is None or b_h.value is None:
        return None
    return _cmp_higher_better(a_h.value, b_h.value)


def _glob_area_cmp(a_mcmm, b_mcmm) -> int | None:
    from ..qor.objectives import _cmp_lower_better

    aa = a_mcmm.objectives.get("area")
    bb = b_mcmm.objectives.get("area")
    if aa is None or bb is None:
        return None
    if aa.unknown or bb.unknown or aa.incomparable or bb.incomparable:
        return None
    if aa.value is None or bb.value is None:
        return None
    if aa.area_source != bb.area_source:
        return None
    return _cmp_lower_better(aa.value, bb.value)


def _ordered_buckets(priorities) -> list[tuple[str, float]]:
    from ..qor.objectives import PRIORITY_WEIGHTS, _pstr

    weights = {
        "timing": PRIORITY_WEIGHTS.get(_pstr(priorities.get("timing", "high")), 0.0),
        "quality": PRIORITY_WEIGHTS.get(_pstr(priorities.get("constraint_quality", "low")), 0.0),
        "area": PRIORITY_WEIGHTS.get(_pstr(priorities.get("area", "medium")), 0.0),
        "power": PRIORITY_WEIGHTS.get(_pstr(priorities.get("power", "medium")), 0.0),
    }
    items = sorted([(w, name) for name, w in weights.items()], key=lambda x: -x[0])
    return [(name, w) for w, name in items if w > 0]


def _decision(s: str):
    from ..utils.enums import CandidateDecision

    try:
        return getattr(CandidateDecision, s, CandidateDecision.PARETO)
    except Exception:
        return s


def mcmm_explanation_for(final, baseline, front, all_candidates,
                         priorities) -> dict[str, Any]:
    """Structured MCMM explanation (Step 12 §14).

    Keeps the per-scenario detail and the global verdict so a user can answer
    \"which mode/corner is limiting this candidate?\".
    """
    final_mcmm = getattr(final, "mcmm", None) if final is not None else None
    if final_mcmm is None:
        return {"selected_id": getattr(final, "id", None),
                "mcmm": False, "reasons": ["no MCMM result"]}

    per_scenario: dict[str, Any] = {}
    for sid in final_mcmm.active_scenario_ids:
        sqor = final_mcmm.scenario_results.get(sid)
        if sqor is None:
            continue
        per_scenario[sid] = {
            "mode": sqor.mode,
            "corner": sqor.corner,
            "status": sqor.status,
            "feasible": sqor.feasible,
            "limiting": sqor.limiting,
            "margin_utilization": sqor.margin_utilization,
            "qor": sqor.qor.summary() if sqor.qor else None,
        }

    objective_limiting: dict[str, Any] = {}
    for name, agg in final_mcmm.objectives.items():
        objective_limiting[name] = {
            "value": agg.value,
            "unknown": agg.unknown,
            "incomparable": agg.incomparable,
            "limiting": list(agg.limiting),
        }

    reasons: list[str] = [
        f"global_status={final_mcmm.global_status}",
        f"active_scenarios={final_mcmm.active_scenario_ids}",
        f"limiting_scenarios={final_mcmm.limiting_scenarios}",
        f"margin_utilization={final_mcmm.margin_utilization}",
    ]
    if final_mcmm.global_status != GLOBAL_FEASIBLE:
        reasons.append(f"global_reason={final_mcmm.global_reason}")
    if final is not None and baseline is not None and final is baseline:
        reasons.append("baseline already optimal under current MCMM policy")
    else:
        reasons.append("candidate passes global hard feasibility gates across all active scenarios")
        reasons.append("candidate is on the MCMM Pareto front; lexicographic priority ordering selected it")

    rejected: list[dict[str, Any]] = []
    for c in all_candidates:
        if c is final:
            continue
        if c in (front or []):
            rejected.append({"id": c.id, "reason": "pareto_front_but_not_selected"})
            continue
        if getattr(c, "mcmm", None) is None:
            rejected.append({"id": c.id, "reason": "single_scenario"})
            continue
        if not getattr(c, "hard_feasible", False):
            rejected.append({"id": c.id,
                             "reason": c.infeasible_reason or "infeasible",
                             "global_status": getattr(c, "global_status", "")})
            continue
        rejected.append({"id": c.id, "reason": "dominated"})

    return {
        "selected_id": getattr(final, "id", None),
        "mcmm": True,
        "global_status": final_mcmm.global_status,
        "limiting_scenarios": list(final_mcmm.limiting_scenarios),
        "active_scenarios": list(final_mcmm.active_scenario_ids),
        "per_scenario": per_scenario,
        "objective_limiting": objective_limiting,
        "margin_headroom_ns": final_mcmm.margin_headroom_ns,
        "margin_utilization": final_mcmm.margin_utilization,
        "margin_limiting_scenarios": list(final_mcmm.margin_limiting_scenarios),
        "priority_weights": {k: _pstr(v) for k, v in priorities.items()},
        "reasons": reasons,
        "rejected": rejected,
    }


def _pstr(p: Any) -> str:
    return p.value if hasattr(p, "value") else str(p).lower()


__all__ = [
    "scenario_feasibility", "scenario_margin",
    "global_feasibility", "aggregate_objectives", "global_margin",
    "mcmm_is_dominating", "mcmm_pareto_front",
    "mcmm_scalar_score", "mcmm_select_final",
]
