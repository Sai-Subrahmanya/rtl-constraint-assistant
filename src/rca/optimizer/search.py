"""
Deterministic bounded candidate generation (Step 11 §10, §11, §12 — corrected).

Rules:
  - ONLY TUNABLE constraints may be mutated; FIXED are immutable.
  - Clock period, functional timing requirements, hard user constraints are
    never modified unless policy explicitly enables period exploration.
  - ONE tunable constraint is mutated per candidate (correction #4). We do NOT
    bulk-apply a delta to every constraint of the same type — each candidate
    changes exactly one constraint id.
  - Mutations are bounded: parameter ranges, step sizes, per-constraint caps.
  - Deterministic ordering: sort eligible mutable constraints by (type, id)
    ascending; within each constraint iterate signed deltas ascending.
  - Candidates are deduplicated by a semantic hash of their modified UCM
    (correction #5): includes id, type, values, target/source/through objects,
    clock_refs, path_selector semantic key, scenario applicability, opt_status,
    and enabled status. Transient caches/timestamps/runtime state are excluded.
    Hashing is insertion-order independent (collections sorted).
  - Baseline is always preserved; we never force a modification.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..config.model import ProjectConfig
from ..constraint_model import ConstraintSet, stable_hash_cset
from ..constraint_model.constraint import Constraint
from ..utils.enums import (
    ConstraintStatus, ConstraintType, OptimizationStatus,
)
from .candidate import Candidate


# Per-mutation type descriptors. Each descriptor specifies:
#   - the set of eligible ConstraintTypes
#   - the values key being mutated
#   - the signed delta range (in ns), the per-step grid (in ns)
#   - absolute bounds for the resulting value (in seconds)
#   - a label factory
def _clone(cset: ConstraintSet) -> ConstraintSet:
    """Clone a ConstraintSet by rebuilding from copied constraints. This avoids
    Pydantic's deep-copy failing on transient locks inside the set.

    Scenarios (MCMM definitions) survive the clone so scenario semantics remain
    part of every candidate's UCM (Step 12 §2)."""
    new_cs = ConstraintSet(name=getattr(cset, "name", ""))
    for sid, s in (getattr(cset, "scenarios", {}) or {}).items():
        new_cs.scenarios[sid] = s.model_copy(deep=True)
    for c in cset:
        try:
            new_cs.add(deepcopy(c))
        except Exception:
            try:
                data = c.model_dump(exclude={"generated_text_by_backend",
                                             "equivalent_forms"})
                new_cs.add(Constraint(**data))
            except Exception:
                new_cs.add(c)
    return new_cs


# Which constraint types are eligible for mutation when TUNABLE.
# Clock period (CREATE_CLOCK) is NOT here — period exploration is opt-in via
# `allow_period_exploration` (Step 11 §12).
_TUNABLE_TYPES: set[ConstraintType] = {
    ConstraintType.SET_CLOCK_UNCERTAINTY,
    ConstraintType.SET_CLOCK_LATENCY,
    ConstraintType.SET_CLOCK_TRANSITION,
    ConstraintType.SET_INPUT_DELAY,
    ConstraintType.SET_OUTPUT_DELAY,
    ConstraintType.SET_MAX_DELAY,
    ConstraintType.SET_MIN_DELAY,
    ConstraintType.SET_INPUT_TRANSITION,
    ConstraintType.SET_LOAD,
    ConstraintType.SET_MAX_TRANSITION,
    ConstraintType.SET_MAX_CAPACITANCE,
    ConstraintType.SET_MAX_FANOUT,
}


def _is_mutable(c: Constraint) -> bool:
    if c.is_fixed():
        return False
    if c.opt_status == OptimizationStatus.FIXED:
        return False
    if c.status == ConstraintStatus.FIXED:
        return False
    if c.disabled:
        return False
    if c.type not in _TUNABLE_TYPES:
        return False
    return c.opt_status in (OptimizationStatus.TUNABLE, OptimizationStatus.UNKNOWN)


def _coerce_step(value: float, step: float, lo: float, hi: float) -> float:
    import math
    v = max(lo, min(hi, value))
    if step > 0:
        v = round(v / step) * step
    return v


def _candidate_id_seq(i: int) -> str:
    return f"C{i:03d}"


def _linspace_steps(lo: float, hi: float, step: float) -> list[float]:
    import math
    if step <= 0:
        return sorted({lo, hi})
    n_lo = math.ceil(lo / step)
    n_hi = math.floor(hi / step)
    vals = [round(i * step, 12) for i in range(n_lo, n_hi + 1)]
    out = []
    for v in vals:
        if abs(v) < 1e-15:
            continue
        out.append(v)
    if abs(lo) > 1e-15 and lo not in out:
        out.insert(0, lo)
    if abs(hi) > 1e-15 and hi not in out:
        out.append(hi)
    return sorted(set(out))


# Per-type mutation plans. Each entry: (types, value_key, range_key, lo_sec, hi_sec, label)
# ranges come from `cfg.optimization.perturbation` by name.
_MUTATION_PLANS = [
    # Clock uncertainty — only increases make slack tighter (the useful
    # direction for trading margin for PPA); decreases are allowed for exploration.
    ({ConstraintType.SET_CLOCK_UNCERTAINTY}, "uncertainty",
     "uncertainty_range_ns", 0.0, 1.0e-6, "clock_uncertainty"),
    ({ConstraintType.SET_INPUT_DELAY, ConstraintType.SET_OUTPUT_DELAY}, "delay",
     "io_delay_range_ns", -100e-9, 100e-9, "io_delay"),
]


def _sorted_mutable_constraints(cset: ConstraintSet, types: set[ConstraintType]
                                ) -> list[Constraint]:
    out = [c for c in cset if c.type in types and _is_mutable(c)]
    # Deterministic order: (type.value, id) ascending.
    out.sort(key=lambda c: (c.type.value, c.id))
    return out


def generate_candidates(
    base: Candidate,
    cset: ConstraintSet,
    cfg: ProjectConfig,
    *,
    max_candidates: int | None = None,
    _id_start: int = 1,
) -> list[Candidate]:
    """Generate bounded, deterministic, ONE-MUTATION-PER-CANDIDATE perturbations.

    Order: for each mutation plan (uncertainty, io_delay, ...):
      1. collect eligible mutable constraints sorted by (type, id) ascending;
      2. iterate signed deltas ascending;
      3. emit one candidate per (constraint, delta) where the value actually changes.
    """
    opt = cfg.optimization
    pert = opt.perturbation
    max_candidates = (max_candidates if max_candidates is not None
                      else opt.max_iterations * 4)

    candidates: list[Candidate] = []
    seen_hashes: set[str] = set()

    for types, value_key, range_attr, lo_sec, hi_sec, label_prefix in _MUTATION_PLANS:
        rng = getattr(pert, range_attr, None)
        if not rng or len(rng) < 2:
            continue
        lo_ns, hi_ns = rng[0], rng[1]
        step_ns = rng[2] if len(rng) >= 3 else max(abs(lo_ns), abs(hi_ns)) or 1.0
        deltas_ns = _linspace_steps(lo_ns, hi_ns, step_ns)
        step_sec = abs(step_ns) * 1e-9

        mutable = _sorted_mutable_constraints(cset, types)
        for con in mutable:
            for delta_ns in deltas_ns:
                if abs(delta_ns) < 1e-15:
                    continue
                cand = _mutate_one(
                    base, cset,
                    target_id=con.id,
                    value_key=value_key,
                    delta_seconds=delta_ns * 1e-9,
                    lo_seconds=lo_sec,
                    hi_seconds=hi_sec,
                    step_seconds=step_sec,
                    change_label=(f"{label_prefix}[{con.id}]_"
                                  f"delta={delta_ns:+.3g}ns"),
                )
                if cand is None:
                    continue
                if not _record_unique(cand, seen_hashes):
                    continue
                # Exactly one constraint id per baseline-strategy candidate
                assert len(cand.mutated_constraint_ids) == 1, \
                    f"expected one mutated constraint, got {cand.mutated_constraint_ids}"
                cand.id = _candidate_id_seq(_id_start + len(candidates))
                candidates.append(cand)
                if len(candidates) >= max_candidates:
                    return candidates

    return candidates


def _mutate_one(
    base: Candidate, cset: ConstraintSet, *,
    target_id: str,
    value_key: str,
    delta_seconds: float,
    lo_seconds: float,
    hi_seconds: float,
    step_seconds: float,
    change_label: str,
) -> Candidate | None:
    """Return a candidate that mutates exactly `target_id`; returns None if no
    real change occurs or the constraint isn't present/mutable."""
    new_cset = _clone(cset)
    target = None
    for c in new_cset:
        if c.id == target_id:
            target = c; break
    if target is None or not _is_mutable(target):
        return None
    # Do NOT fabricate a missing tunable value with a default of zero.
    # If the constraint does not already carry the value being mutated,
    # skip it; the optimizer only perturbs values that actually exist
    # on the baseline UCM and have been classified as tunable.
    raw_v = target.values.get(value_key, None)
    if raw_v is None:
        return None
    try:
        base_v = float(raw_v)
    except (TypeError, ValueError):
        return None
    new_v = _coerce_step(base_v + delta_seconds, step_seconds,
                         lo_seconds, hi_seconds)
    if abs(new_v - base_v) < 1e-15:
        return None
    try:
        target.add_value(value_key, new_v)
    except ValueError:
        return None
    identity = stable_hash_cset(new_cset)
    return Candidate(
        parent_id=base.id,
        generation=base.generation + 1,
        constraint_set=new_cset,
        generated_changes=[change_label],
        mutated_constraint_ids=[target_id],
        decision_reason=f"Perturbation: {change_label}",
        constraint_model_hash=identity,
        scenario=base.scenario, corner=base.corner, mode=base.mode,
    )


def _record_unique(cand: Candidate, seen: set[str]) -> bool:
    if cand.constraint_model_hash in seen:
        return False
    seen.add(cand.constraint_model_hash)
    return True
