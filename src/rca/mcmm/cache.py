"""
Scenario-aware cache identity for MCMM (Step 12 §10).

Scenario semantics MUST be part of the cache identity so that semantically
different scenarios (e.g. functional/slow vs functional/fast) can never
accidentally share results.  The cache key distinguishes, where applicable:

- scenario id
- mode
- corner
- libraries
- parasitics
- environment
- constraint-set identity / hash (stable_hash_cset)
- backend/tool identity
- tool/version information

``stable_hash_cset`` is preserved as-is; this module composes it with the
scenario semantic key and tool identity rather than re-implementing it.
"""

from __future__ import annotations

from typing import Any

from ..constraint_model import ConstraintSet, Scenario, stable_hash_cset
from ..utils.hashing import hash_file, stable_hash


def scenario_semantic_key(scenario: Scenario) -> tuple:
    """Deterministic semantic identity of one scenario (cache-relevant)."""
    return (
        scenario.id,
        scenario.mode,
        scenario.corner,
        tuple(sorted(scenario.libraries or [])),
        scenario.parasitics or "",
        tuple(sorted((str(k), _hashable_val(v))
                     for k, v in (scenario.environment or {}).items())),
        scenario.sdc_set_id or "",
    )


def _hashable_val(v: Any) -> Any:
    if isinstance(v, dict):
        return tuple(sorted((str(k), _hashable_val(vv)) for k, vv in v.items()))
    if isinstance(v, (list, tuple)):
        return tuple(_hashable_val(x) for x in v)
    if isinstance(v, set):
        return tuple(sorted(_hashable_val(x) for x in v))
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, str)) or v is None:
        return v
    return str(v)


def scenario_cache_key(scenario: Scenario,
                       cset: ConstraintSet | None = None,
                       *,
                       backend: str = "",
                       tool_version: str = "",
                       tool_path: str = "",
                       extra: dict[str, Any] | None = None) -> str:
    """Build a stable, scenario-aware cache key for one scenario evaluation.

    The key includes the full scenario semantic identity, the ConstraintSet
    semantic hash (``stable_hash_cset``), and the backend/tool identity so
    that (a) two different scenarios can never collide, and (b) a scenario's
    result is not re-used across tools/versions.
    """
    data: dict[str, Any] = {
        "version": 1,
        "scenario": scenario_semantic_key(scenario),
        "cset": stable_hash_cset(cset) if cset is not None else "",
        "backend": backend,
        "tool_version": tool_version,
        "tool_path": tool_path,
    }
    if extra:
        for k, v in sorted(extra.items()):
            data[k] = _hashable_val(v)
    return stable_hash(data)


def mcmm_run_cache_key(cset: ConstraintSet,
                       matrix_summary: dict[str, Any],
                       *,
                       backend: str = "",
                       tool_version: str = "",
                       tool_path: str = "",
                       extra: dict[str, Any] | None = None) -> str:
    """Aggregate cache identity for a full MCMM evaluation (all scenarios).

    Composes the ConstraintSet semantic hash with the active scenario matrix
    and the backend/tool identity.  Useful for coarse experiment-level invalidation.
    """
    data: dict[str, Any] = {
        "version": 1,
        "cset": stable_hash_cset(cset),
        "matrix": {
            "active": list(matrix_summary.get("active_scenarios", [])),
            "scenarios": {s["id"]: _hashable_val(s)
                          for s in matrix_summary.get("active_scenarios", [])},
        },
        "backend": backend,
        "tool_version": tool_version,
        "tool_path": tool_path,
    }
    if extra:
        for k, v in sorted(extra.items()):
            data[k] = _hashable_val(v)
    return stable_hash(data)


__all__ = [
    "scenario_semantic_key", "scenario_cache_key", "mcmm_run_cache_key",
]
