"""QoR objective vector, dominance, margin utilization, and feasibility
classification (Step 11 second correction pass).

Policies:
- UNKNOWN metrics compare conservatively (UNKNOWN never dominates KNOWN).
- area vs area_proxy: real vs real; proxy vs proxy; real-vs-proxy INCOMPARABLE.
- constraint_quality is UNKNOWN (None) unless explicitly measured; unsafe
  exceptions INFEASIBLE under normal safe policy.
- _feasible_bool relies on the AUTHORITATIVE `hard_feasible` flag set by
  classify_feasibility(); it never falls back to re-deriving feasibility from
  raw WNS, so validation/unsafe/blocked candidates stay out of Pareto.
- margin_utilization is a DIAGNOSTIC / secondary tie-break signal measuring
  the fraction of usable baseline headroom consumed. It is NOT a standalone
  Pareto objective (consuming slack is not intrinsically beneficial). Pareto
  dominance uses actual PPA + timing + quality objectives only.
- margin_headroom_ns is diagnostic only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, TYPE_CHECKING

from ..utils.enums import PowerStatus

if TYPE_CHECKING:
    from ..optimizer.candidate import Candidate


class Direction(Enum):
    MAXIMIZE = 1
    MINIMIZE = -1


@dataclass(frozen=True)
class ObjectiveSpec:
    name: str
    direction: Direction
    group: str
    eps: float = 1e-12
    unknown_conservative: bool = True


OBJECTIVE_SPECS: dict[str, ObjectiveSpec] = {
    "setup_wns":   ObjectiveSpec("setup_wns",   Direction.MAXIMIZE, "timing"),
    "hold_wns":    ObjectiveSpec("hold_wns",    Direction.MAXIMIZE, "timing"),
    "setup_tns":   ObjectiveSpec("setup_tns",   Direction.MAXIMIZE, "timing"),
    "hold_tns":    ObjectiveSpec("hold_tns",    Direction.MAXIMIZE, "timing"),
    "area":        ObjectiveSpec("area",        Direction.MINIMIZE, "area"),
    "power":       ObjectiveSpec("power",       Direction.MINIMIZE, "power"),
    "constraint_quality":
        ObjectiveSpec("constraint_quality", Direction.MAXIMIZE, "quality"),
    # NOTE: margin_utilization is intentionally NOT a Pareto objective.
    # Consuming slack is not a benefit by itself; it is a secondary tie-break
    # / tradeoff signal used only after Pareto filtering and priority ordering.
}

AREA_REAL = "real"
AREA_PROXY = "proxy"
AREA_UNKNOWN = "unknown"


class CompareResult(Enum):
    BETTER = "better"
    WORSE = "worse"
    EQUAL = "equal"
    INCOMPARABLE = "incomparable"


# ---------- Feasibility ----------

@dataclass
class FeasibilityResult:
    feasible: bool
    blocked: bool
    infeasible_reason: str = ""
    diagnostics: list[str] = field(default_factory=list)
    unsafe: bool = False
    exploratory: bool = False


def classify_feasibility(qor, *, required_setup_ns: float = 0.0,
                         required_hold_ns: float = 0.0,
                         allow_unsafe_exceptions: bool = False
                         ) -> FeasibilityResult:
    if qor is None:
        return FeasibilityResult(False, True, "blocked_no_qor",
                                 ["No QoR (experiment did not complete)."])
    diag: list[str] = []
    unsafe = False
    exploratory = False
    if qor.setup_wns is None:
        diag.append("setup WNS unavailable")
        return FeasibilityResult(False, False, "setup_unknown", diag)
    if qor.setup_wns < required_setup_ns * 1e-9 - 1e-12:
        diag.append(f"setup WNS {qor.setup_wns*1e9:.3f}ns < required {required_setup_ns}ns")
        return FeasibilityResult(False, False, "setup_violation", diag)
    if qor.hold_wns is None:
        diag.append("hold WNS unavailable")
        return FeasibilityResult(False, False, "hold_unknown", diag)
    if qor.hold_wns < required_hold_ns * 1e-9 - 1e-12:
        diag.append(f"hold WNS {qor.hold_wns*1e9:.3f}ns < required {required_hold_ns}ns")
        return FeasibilityResult(False, False, "hold_violation", diag)
    val_err = getattr(qor, "validation_errors", 0) or 0
    if val_err > 0:
        diag.append(f"validation errors: {val_err}")
        return FeasibilityResult(False, False, "validation_error", diag)
    unsafe_n = getattr(qor, "unsafe_exceptions", 0) or 0
    if unsafe_n > 0:
        diag.append(f"unsafe exceptions: {unsafe_n}")
        if not allow_unsafe_exceptions:
            return FeasibilityResult(False, False, "unsafe_exceptions", diag)
        unsafe = True
        exploratory = True
    if getattr(qor, "tool", "") == "error":
        return FeasibilityResult(False, True, "tool_error", list(getattr(qor, "notes", [])))
    return FeasibilityResult(True, False, "", diag, unsafe=unsafe,
                             exploratory=exploratory)


# ---------- Objective vector ----------

@dataclass
class AreaValue:
    value: float | None
    source: str


def _area_value(qor) -> AreaValue:
    if qor is None:
        return AreaValue(None, AREA_UNKNOWN)
    if getattr(qor, "area", None) is not None:
        return AreaValue(float(qor.area), AREA_REAL)
    if getattr(qor, "area_proxy", None) is not None:
        return AreaValue(float(qor.area_proxy), AREA_PROXY)
    return AreaValue(None, AREA_UNKNOWN)


def _raw_metric(qor, name: str) -> tuple[float | None, str | None]:
    if qor is None:
        return None, None
    if name == "setup_wns":   return qor.setup_wns, None
    if name == "hold_wns":    return qor.hold_wns, None
    if name == "setup_tns":
        if qor.setup_tns is not None: return qor.setup_tns, None
        if qor.setup_wns is not None and qor.setup_wns >= 0:
            return 0.0, None
        return None, None
    if name == "hold_tns":
        if qor.hold_tns is not None: return qor.hold_tns, None
        if qor.hold_wns is not None and qor.hold_wns >= 0:
            return 0.0, None
        return None, None
    if name == "area":
        av = _area_value(qor)
        return av.value, av.source
    if name == "power":
        # Only canonical usable power evidence participates.  Detailed parser
        # failures are mapped to canonical UNAVAILABLE and retained solely in
        # report provenance; never let an arbitrary status plus a value become
        # a fabricated objective.
        usable_power_statuses = {PowerStatus.AVAILABLE.value, PowerStatus.ESTIMATED.value}
        if qor.power_status in usable_power_statuses and qor.power is not None:
            return float(qor.power), None
        return None, None
    if name == "constraint_quality":
        return qor.constraint_quality, None
    return None, None


def objective_vector(qor) -> dict[str, tuple[float | None, str | None]]:
    vec: dict[str, tuple[float | None, str | None]] = {}
    for name in ("setup_wns", "hold_wns", "setup_tns", "hold_tns",
                 "area", "power", "constraint_quality"):
        vec[name] = _raw_metric(qor, name)
    return vec


# ---------- Margins ----------

def compute_margin(qor, required_setup_ns: float = 0.0,
                   required_hold_ns: float = 0.0,
                   baseline_setup_wns: float | None = None,
                   baseline_hold_wns: float | None = None
                   ) -> dict[str, float | None]:
    """Candidate binding headroom + margin utilization (diagnostic).

    margin_headroom_ns (diagnostic, ns):
        Candidate headroom above the required timing floors, per dimension
        clamped at zero:
            setup_headroom_ns = max(0, cand_setup_wns - req_s) * 1e9
            hold_headroom_ns  = max(0, cand_hold_wns  - req_h) * 1e9
        margin_headroom_ns = min(setup_headroom_ns, hold_headroom_ns) when
        both candidate WNS are known; the known single dimension when only
        one is known; None when neither is known.

    margin_utilization (diagnostic / secondary tie-break signal):
        Fraction of the usable baseline timing margin consumed by the
        candidate, measured per dimension and combined as the maximum
        (binding-dimension) utilization:

            setup_headroom  = max(0, baseline_setup_wns - req_s)
            hold_headroom   = max(0, baseline_hold_wns  - req_h)
            setup_consumed  = max(0, baseline_setup_wns - cand_setup_wns)
            hold_consumed   = max(0, baseline_hold_wns  - cand_hold_wns)
            setup_util      = clamp(setup_consumed / setup_headroom, 0, 1)
            hold_util       = clamp(hold_consumed  / hold_headroom,  0, 1)
            margin_utilization = max(setup_util, hold_util)

    PREREQUISITES (ALL must hold; otherwise margin_utilization is None):
      1. Baseline setup AND baseline hold WNS are both known.
      2. Candidate setup AND candidate hold WNS are both known.
      3. Baseline setup headroom is positive (baseline is above the
         required setup floor).
      4. Baseline hold headroom is positive (baseline is above the
         required hold floor).
      There is NO one-dimensional setup-only fallback: if any required
      information is missing, or either baseline headroom is zero, the
      result is None — a positive common margin on both dimensions is
      required to measure a meaningful consumed fraction.

    PER-DIMENSION POLICY:
      - Negative consumption (the candidate improves timing relative to the
        baseline on that dimension) is clamped at zero on that dimension.
      - Per-dimension utilization is clamped to [0, 1].
      - Combining as max(setup_util, hold_util) means the most-constrained
        dimension governs: a candidate that destroys hold slack cannot look
        "low utilization" simply because setup is generous.

    ROLE IN SELECTION:
      - margin_utilization is diagnostic only; it is NOT a Pareto objective
        (consuming slack is not a benefit by itself).
      - It is excluded from scalar_score().
      - It is used only as a secondary final-selection tie-break, applied
        after feasibility and Pareto/priority ordering, preferring LOWER
        utilization (greater residual timing margin) among otherwise
        equivalent candidates.
      - Hard feasibility (classify_feasibility) is evaluated BEFORE margin
        utilization is considered: candidates violating a required timing
        floor are INFEASIBLE and never reach Pareto or final selection.
    """
    if qor is None:
        return {"margin_headroom_ns": None, "margin_utilization": None}
    req_s = required_setup_ns * 1e-9
    req_h = required_hold_ns * 1e-9

    # Candidate binding headroom (diagnostic ns).
    setup_hr_ns = hold_hr_ns = None
    if qor.setup_wns is not None:
        setup_hr_ns = max(0.0, qor.setup_wns - req_s) * 1e9
    if qor.hold_wns is not None:
        hold_hr_ns = max(0.0, qor.hold_wns - req_h) * 1e9
    if setup_hr_ns is None and hold_hr_ns is None:
        headroom_ns = None
    elif setup_hr_ns is None:
        headroom_ns = hold_hr_ns
    elif hold_hr_ns is None:
        headroom_ns = setup_hr_ns
    else:
        headroom_ns = min(setup_hr_ns, hold_hr_ns)

    # Utilization: requires BOTH setup and hold dimensions to be known and
    # positive at baseline. Otherwise None.
    utilization: float | None = None
    if (baseline_setup_wns is not None and baseline_hold_wns is not None
            and qor.setup_wns is not None and qor.hold_wns is not None):
        b_setup_hr = max(0.0, baseline_setup_wns - req_s) * 1e9
        b_hold_hr = max(0.0, baseline_hold_wns - req_h) * 1e9
        # Need positive headroom on BOTH dimensions to speak meaningfully
        # about "fraction of margin consumed" on both sides.
        if b_setup_hr > 1e-15 and b_hold_hr > 1e-15:
            setup_consumed = max(0.0, (baseline_setup_wns - qor.setup_wns) * 1e9)
            hold_consumed = max(0.0, (baseline_hold_wns - qor.hold_wns) * 1e9)
            setup_util = max(0.0, min(1.0, setup_consumed / b_setup_hr))
            hold_util = max(0.0, min(1.0, hold_consumed / b_hold_hr))
            utilization = max(setup_util, hold_util)
    return {
        "margin_headroom_ns": headroom_ns,
        "margin_utilization": utilization,
    }


# ---------- Comparison & dominance ----------

def _cmp_metric(name: str, a_entry, b_entry) -> CompareResult:
    av, atag = a_entry if isinstance(a_entry, tuple) else (a_entry, None)
    bv, btag = b_entry if isinstance(b_entry, tuple) else (b_entry, None)
    spec = OBJECTIVE_SPECS[name]
    if name == "area":
        if atag != btag:
            return CompareResult.INCOMPARABLE
        if atag == AREA_UNKNOWN:
            return (CompareResult.EQUAL if av is None and bv is None
                    else CompareResult.INCOMPARABLE)
    if av is None or bv is None:
        if spec.unknown_conservative:
            if av is None and bv is None:
                return CompareResult.EQUAL
            return CompareResult.INCOMPARABLE
        return CompareResult.EQUAL
    d = spec.direction
    eps = spec.eps
    delta = (av - bv) * d.value
    if abs(delta) <= eps:
        return CompareResult.EQUAL
    return CompareResult.BETTER if delta > 0 else CompareResult.WORSE


def compare_objectives(a, b) -> dict[str, CompareResult]:
    va = objective_vector(a.qor); vb = objective_vector(b.qor)
    return {k: _cmp_metric(k, va.get(k), vb.get(k)) for k in OBJECTIVE_SPECS}


def _feasible_bool(c) -> bool:
    """Authoritative feasibility. Returns `c.hard_feasible` only — never falls
    back to re-deriving feasibility from raw WNS on QoR, so candidates that
    were classified INFEASIBLE (validation errors, unsafe exceptions, blocked
    experiments, hard margin-floor misses) can never re-enter Pareto."""
    return bool(getattr(c, "hard_feasible", False))


def _is_unsafe(c) -> bool:
    if getattr(c, "blocked", False):
        return False
    if getattr(c, "infeasible_reason", "") == "unsafe_exceptions":
        return True
    q = getattr(c, "qor", None)
    if q is not None and (getattr(q, "unsafe_exceptions", 0) or 0) > 0:
        return True
    return False


def _scenario_field(c, q, name, default):
    v = getattr(c, name, None)
    if v is None:
        v = getattr(q, name, None)
    return v if v is not None else default


def is_dominating(a, b) -> bool:
    qa, qb = a.qor, b.qor
    if qa is None or qb is None:
        return False
    if not _feasible_bool(a) or not _feasible_bool(b):
        return False
    if _is_unsafe(a) or _is_unsafe(b):
        return False
    if _scenario_field(a, qa, "scenario", "default") != _scenario_field(b, qb, "scenario", "default"):
        return False
    if _scenario_field(a, qa, "corner", "default") != _scenario_field(b, qb, "corner", "default"):
        return False
    if getattr(qa, "flow_stage", "synthesis_sta") != getattr(qb, "flow_stage", "synthesis_sta"):
        return False
    va = objective_vector(qa); vb = objective_vector(qb)
    any_better = False; all_ge = True
    for k in OBJECTIVE_SPECS:
        r = _cmp_metric(k, va.get(k), vb.get(k))
        # Conservative baseline policy: ANY incomparable objective blocks
        # full dominance. This covers (a) mixed area sources (real vs proxy)
        # and (b) UNKNOWN-vs-KNOWN on any metric. We do not convert
        # INCOMPARABLE into EQUAL, and we do not pick sides between sources.
        if r == CompareResult.INCOMPARABLE:
            return False
        if r == CompareResult.WORSE:
            all_ge = False; break
        if r == CompareResult.BETTER:
            any_better = True
    return all_ge and any_better


def pareto_front(candidates) -> list:
    cands = [c for c in candidates if _feasible_bool(c) and not _is_unsafe(c)]
    front: list = []
    for c in cands:
        dominated = False; new_front: list = []
        for existing in front:
            if is_dominating(existing, c):
                dominated = True; new_front.append(existing)
            elif is_dominating(c, existing):
                continue
            else:
                new_front.append(existing)
        if not dominated:
            new_front.append(c)
        front = new_front
    for c in front:
        c.decision = _decision("PARETO"); c.pareto_member = True
    return front


def _decision(s: str):
    try:
        from ..utils.enums import CandidateDecision
        return getattr(CandidateDecision, s, CandidateDecision.PARETO)
    except Exception:
        return s


PRIORITY_WEIGHTS = {"high": 3.0, "medium": 2.0, "low": 1.0, "off": 0.0}


def _area_source(q) -> str:
    """Return 'real' / 'proxy' / 'unknown' for a QoRResult."""
    if q is None:
        return "unknown"
    if getattr(q, "area", None) is not None:
        return "real"
    if getattr(q, "area_proxy", None) is not None:
        return "proxy"
    return "unknown"


def _area_scalar(q):
    """Return a numeric area value ONLY when source is real or proxy; None
    otherwise. Used for priority comparison when both sides match source."""
    s = _area_source(q)
    if s == "real": return float(q.area)
    if s == "proxy": return float(q.area_proxy)
    return None


def _cmp_higher_better(xa, xb) -> int | None:
    """Return +1 if xa > xb, -1 if xa < xb, 0 if equal, None if incomparable."""
    if xa is None or xb is None:
        return None
    if abs(xa - xb) <= 1e-12:
        return 0
    return 1 if xa > xb else -1


def _cmp_lower_better(xa, xb) -> int | None:
    """For MIN objectives (smaller better): area, power."""
    if xa is None or xb is None:
        return None
    if abs(xa - xb) <= 1e-12:
        return 0
    return -1 if xa > xb else 1  # xa bigger -> xa worse -> a loses -> -1


def _cmp_timing(qa, qb) -> int | None:
    """Compare timing MAX objectives pair-wise; requires both WNS values known
    on both sides (setup+hold). If any side is missing, timing is
    INCOMPARABLE (skip) — we do not fake -1e18 sentinels."""
    # Need both setup and hold on both sides to rank on timing.
    if (qa.setup_wns is None or qa.hold_wns is None or
        qb.setup_wns is None or qb.hold_wns is None):
        return None
    # Lexicographic within timing: setup first, then hold. A missing component
    # on either side already returned None above.
    c = _cmp_higher_better(qa.setup_wns, qb.setup_wns)
    if c is None or c != 0:
        return c
    return _cmp_higher_better(qa.hold_wns, qb.hold_wns)


def _cmp_quality(qa, qb) -> int | None:
    if qa.constraint_quality is None or qb.constraint_quality is None:
        return None
    return _cmp_higher_better(qa.constraint_quality, qb.constraint_quality)


def _cmp_area(qa, qb) -> int | None:
    """Compare area ONLY when sources match (real-vs-real, proxy-vs-proxy).
    Real↔proxy, known↔unknown -> INCOMPARABLE (None)."""
    sa, sb = _area_source(qa), _area_source(qb)
    if sa != sb or sa == "unknown":
        return None
    return _cmp_lower_better(_area_scalar(qa), _area_scalar(qb))


def _cmp_power(qa, qb) -> int | None:
    """Compare power (MIN) only when both canonical values are usable."""
    if qa.power is None or qb.power is None:
        return None
    usable_power_statuses = {PowerStatus.AVAILABLE.value, PowerStatus.ESTIMATED.value}
    if (getattr(qa, "power_status", None) not in usable_power_statuses or
            getattr(qb, "power_status", None) not in usable_power_statuses):
        return None
    return _cmp_lower_better(qa.power, qb.power)


def _pstr(p: Any) -> str:
    return p.value if hasattr(p, "value") else str(p).lower()


def _area_for_score(q, bq) -> float:
    """Secondary scalar score: contributes area improvement only when sources
    match (real/real or proxy/proxy). Returns 0.0 (neutral) when sources
    differ or baseline area is unavailable — never fabricates a numeric
    value across incomparable area sources."""
    if bq is None:
        return 0.0
    sa, sb = _area_source(q), _area_source(bq)
    if sa != sb or sa == "unknown":
        return 0.0
    bv = _area_scalar(bq); v = _area_scalar(q)
    if bv is None or v is None or bv == 0:
        return 0.0
    return 1.0 - v / bv


def _ordered_buckets(priorities) -> list[tuple[str, float]]:
    """Return [(metric_name, weight), ...] sorted by descending weight. OFF
    weights are dropped. Metrics: 'timing', 'quality', 'area', 'power'."""
    from ..utils.enums import Priority
    weights = {
        "timing":  PRIORITY_WEIGHTS.get(_pstr(priorities.get("timing", Priority.HIGH)), 0.0),
        "quality": PRIORITY_WEIGHTS.get(_pstr(priorities.get("constraint_quality", Priority.LOW)), 0.0),
        "area":    PRIORITY_WEIGHTS.get(_pstr(priorities.get("area", Priority.MEDIUM)), 0.0),
        "power":   PRIORITY_WEIGHTS.get(_pstr(priorities.get("power", Priority.MEDIUM)), 0.0),
    }
    items = sorted([(w, name) for name, w in weights.items()], key=lambda x: -x[0])
    return [(name, w) for w, name in items if w > 0]


def _cmp_metric_by_name(name, qa, qb) -> int | None:
    if name == "timing":  return _cmp_timing(qa, qb)
    if name == "quality": return _cmp_quality(qa, qb)
    if name == "area":    return _cmp_area(qa, qb)
    if name == "power":   return _cmp_power(qa, qb)
    return None


def _priority_compare(a, b, baseline, priorities) -> int:
    """Pairwise comparator for Pareto-member selection. Returns -1/0/+1.

    Ordering policy:
      1. Feasibility first (infeasible/unsafe sort after feasible).
      2. User priority buckets in descending weight order
         (timing / quality / area / power). For each metric, the pair is
         compared ONLY when both sides have a comparable value (e.g. both
         real area or both proxy; power known on both; quality known on
         both; both setup/hold WNS present). INCOMPARABLE / UNKNOWN on a
         metric SKIPS that dimension rather than inventing a fake
         sentinel number.
      3. If all prioritized metrics are equal or incomparable, prefer LOWER
         margin_utilization (preserve residual slack).
      4. Deterministic candidate-id tie-break.
    """
    qa, qb = a.qor, b.qor
    fa = (qa is not None and _feasible_bool(a) and not _is_unsafe(a))
    fb = (qb is not None and _feasible_bool(b) and not _is_unsafe(b))
    if fa and not fb: return 1
    if fb and not fa: return -1
    if not fa and not fb:
        ia, ib = getattr(a, "id", ""), getattr(b, "id", "")
        if ia < ib: return 1
        if ia > ib: return -1
        return 0

    for name, _w in _ordered_buckets(priorities):
        c = _cmp_metric_by_name(name, qa, qb)
        if c is None or c == 0:
            continue
        return c

    # Margin tie-break: LOWER utilization wins (preserve residual slack).
    ua = qa.margin_utilization if qa.margin_utilization is not None else None
    ub = qb.margin_utilization if qb.margin_utilization is not None else None
    if ua is not None and ub is not None and abs(ua - ub) > 1e-12:
        return 1 if ua < ub else -1
    # Deterministic id tie-break: smaller id wins (prefer first-issued/baseline).
    # cmp returns +1 when `a` is preferred; if ia < ib then a is preferred.
    ia, ib = getattr(a, "id", ""), getattr(b, "id", "")
    if ia < ib: return 1
    if ia > ib: return -1
    return 0


def select_final(front, baseline, priorities):
    from functools import cmp_to_key
    if not front:
        return None
    pool = list(front)
    if baseline is not None and baseline not in pool and _feasible_bool(baseline) \
            and not _is_unsafe(baseline):
        pool.append(baseline)
    safe = [c for c in pool if not _is_unsafe(c) and _feasible_bool(c)]
    if not safe:
        return None
    return max(safe, key=cmp_to_key(lambda a, b: _priority_compare(a, b, baseline, priorities)))


def scalar_score(c, baseline, priorities) -> float:
    q = c.qor
    if q is None or not _feasible_bool(c) or _is_unsafe(c):
        return float("-inf")
    s = 0.0
    t = PRIORITY_WEIGHTS.get(_pstr(priorities.get("timing", "high")), 2.0)
    a = PRIORITY_WEIGHTS.get(_pstr(priorities.get("area", "medium")), 1.0)
    pw = PRIORITY_WEIGHTS.get(_pstr(priorities.get("power", "medium")), 1.0)
    if q.setup_wns is not None:
        s += t * q.setup_wns * 1e9
    if q.hold_wns is not None:
        s += t * q.hold_wns * 1e9
    bq = baseline.qor if baseline and getattr(baseline, "qor", None) else None
    if bq is not None:
        s += a * _area_for_score(q, bq)
        # Power contributes only when known on both sides (conservative).
        # UNKNOWN power is not zero and not a free win.
        usable_power_statuses = {PowerStatus.AVAILABLE.value, PowerStatus.ESTIMATED.value}
        if (q.power is not None and bq.power is not None
                and getattr(q, "power_status", None) in usable_power_statuses
                and getattr(bq, "power_status", None) in usable_power_statuses
                and bq.power != 0):
            s += pw * (1.0 - q.power / bq.power)
    # NOTE: margin_utilization is deliberately NOT added to scalar_score —
    # consuming slack is not a benefit. It is a diagnostic, consulted only as
    # a last-resort tie-break inside _priority_compare.
    return s


def explanation_for(final, baseline, front, all_candidates, priorities) -> dict[str, Any]:
    q = final.qor
    bq = baseline.qor if baseline and getattr(baseline, "qor", None) else None

    def _ns(v): return round(v*1e9, 4) if v is not None else None
    delta: dict[str, Any] = {}
    if bq is not None and q is not None:
        for k in ("setup_wns","hold_wns","area","power","margin_headroom_ns",
                  "margin_utilization","constraint_quality"):
            bv = getattr(bq, k, None); fv = getattr(q, k, None)
            if k.endswith("_wns") or k == "margin_headroom_ns":
                bv_ns = _ns(bv); fv_ns = _ns(fv)
                if bv_ns is None or fv_ns is None:
                    delta[k] = {"final": fv_ns, "baseline": bv_ns}
                else:
                    delta[k] = {"final": fv_ns, "baseline": bv_ns, "delta_ns": round(fv_ns-bv_ns, 4)}
            else:
                if bv is None or fv is None:
                    delta[k] = {"final": fv, "baseline": bv}
                else:
                    delta[k] = {"final": fv, "baseline": bv, "delta": round(fv-bv, 6)}
        delta["area_source"] = {"final": _area_value(q).source,
                                "baseline": _area_value(bq).source}
    safety = "UNSAFE (exploratory only — NOT eligible for final)" if _is_unsafe(final) else "safe"
    reasons: list[str] = [
        f"timing feasibility: setup_wns={_ns(q.setup_wns) if q else None}ns, "
        f"hold_wns={_ns(q.hold_wns) if q else None}ns",
        f"safety status: {safety}",
    ]
    if baseline is not None and final is baseline:
        reasons.append("baseline already optimal under current policy (no dominating Pareto candidate)")
    else:
        reasons.append("candidate passes hard feasibility gates (timing/hold/validation/safety)")
        reasons.append("candidate is on the Pareto front")
        reasons.append("lexicographic priority ordering (timing -> quality -> area -> power) selected it over other Pareto members; margin_utilization used only as a residual-slack tie-break")
        if q is not None and q.margin_utilization is not None:
            reasons.append(f"margin_utilization={q.margin_utilization:.2f}")
    rejected = []
    for c in all_candidates:
        if c is final: continue
        if c in front:
            rejected.append({"id": c.id, "reason": "pareto_front_but_not_selected"}); continue
        if not _feasible_bool(c):
            r = c.infeasible_reason or "infeasible"
            if r == "unsafe_exceptions":
                r = "unsafe_exceptions (blocked under safe policy)"
            rejected.append({"id": c.id, "reason": r}); continue
        if _is_unsafe(c):
            rejected.append({"id": c.id, "reason": "unsafe_exceptions"}); continue
        rejected.append({"id": c.id, "reason": "dominated"})
    return {
        "selected_id": getattr(final, "id", None),
        "feasible": _feasible_bool(final),
        "safety": safety,
        "pareto_members": [getattr(c, "id", "") for c in front],
        "changed_constraints": list(getattr(final, "generated_changes", []) or []),
        "delta_vs_baseline": delta,
        "priority_weights": {k: _pstr(v) for k, v in priorities.items()},
        "reasons": reasons,
        "rejected": rejected,
    }
