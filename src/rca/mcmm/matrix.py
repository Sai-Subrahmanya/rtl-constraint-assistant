"""
Scenario matrix for MCMM (Step 12 §1, §2).

A :class:`ScenarioMatrix` is the single authoritative view of which
(mode, corner) scenarios are active for an optimization / analysis run, and
how constraints map onto them:

- empty ``scenario_ids`` on a constraint => applies to ALL active scenarios;
- non-empty ``scenario_ids`` => applies ONLY to the listed scenarios
  (those that are active).

The matrix is explicitly enableable / disableable.  When disabled, or when
only one scenario is active, behaviour reduces to the legacy single-scenario
model (Step 12 §1, §22).

No mode/corner names are hard-coded; the matrix is driven entirely by the
``Scenario`` definitions on the ``ConstraintSet`` and the active-scenario
selection from configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..constraint_model import Constraint, ConstraintSet, Scenario
from .model import FEASIBLE, ScenarioQoR


@dataclass
class ScenarioMatrix:
    """Active scenario matrix.

    ``scenarios`` is the full definition set (from the ConstraintSet); only
    those in ``active_ids`` (default: all active scenarios) are evaluated.
    """

    scenarios: dict[str, Scenario] = field(default_factory=dict)
    active_ids: list[str] = field(default_factory=list)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.active_ids:
            # Default: every scenario marked active.
            self.active_ids = [sid for sid, s in sorted(self.scenarios.items())
                               if getattr(s, "active", True)]
        # Deterministic order.
        self.active_ids = _dedupe(self.active_ids)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @property
    def is_enabled(self) -> bool:
        return bool(self.enabled)

    @property
    def scenario_count(self) -> int:
        return len(self.active_ids)

    @property
    def single_scenario(self) -> bool:
        """Legacy single-scenario mode when disabled or only one active."""
        if not self.enabled:
            return True
        return len(self.active_ids) <= 1

    def active_scenarios(self) -> list[Scenario]:
        return [self.scenarios[sid] for sid in self.active_ids
                if sid in self.scenarios]

    def scenario(self, sid: str) -> Scenario | None:
        return self.scenarios.get(sid)

    def get_active(self, sid: str) -> Scenario | None:
        if sid not in self.active_ids:
            return None
        return self.scenarios.get(sid)

    def semantic_key(self) -> tuple:
        """Deterministic identity of the active matrix (cache-relevant)."""
        return tuple(
            (sid, self.scenarios[sid].semantic_key()) for sid in self.active_ids
        )

    def hash_key(self) -> str:
        """Stable hash string used as part of cache identity."""
        from ..utils.hashing import stable_hash
        return stable_hash({
            "enabled": self.enabled,
            "active": list(self.active_ids),
            "scenarios": {sid: self.scenarios[sid].semantic_key()
                          for sid in self.active_ids},
        })

    # ------------------------------------------------------------------
    # Scenario-aware constraint applicability (Step 12 §2)
    # ------------------------------------------------------------------

    def constraint_applies_to(self, c: Constraint, sid: str) -> bool:
        """Return True if constraint ``c`` applies to scenario ``sid``.

        empty ``scenario_ids`` => all active scenarios.
        non-empty ``scenario_ids`` => only the listed (must be active).
        """
        if sid not in self.active_ids:
            return False
        sids = list(getattr(c, "scenario_ids", None) or [])
        if not sids:
            return True
        return sid in sids

    def constraints_for_scenario(self, cset: ConstraintSet, sid: str) -> list[Constraint]:
        return [c for c in cset if self.constraint_applies_to(c, sid)]

    def all_scenario_ids_for_constraint(self, c: Constraint) -> list[str]:
        """The active scenarios a constraint applies to."""
        sids = list(getattr(c, "scenario_ids", None) or [])
        if not sids:
            return list(self.active_ids)
        return [sid for sid in self.active_ids if sid in sids]

    def has_scenario_specific_constraints(self, cset: ConstraintSet) -> bool:
        return any(bool(list(getattr(c, "scenario_ids", None) or [])) for c in cset)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "scenario_count": self.scenario_count,
            "single_scenario": self.single_scenario,
            "active_scenarios": [
                {
                    "id": s.id,
                    "mode": s.mode,
                    "corner": s.corner,
                    "name": s.name,
                    "libraries": list(s.libraries),
                    "parasitics": s.parasitics,
                    "environment": dict(s.environment),
                }
                for s in self.active_scenarios()
            ],
        }


def _dedupe(seq: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def build_scenario_matrix(cfg: Any,
                          cset: ConstraintSet | None = None) -> ScenarioMatrix:
    """Build a :class:`ScenarioMatrix` from config (+ optional ConstraintSet).

    Scenario *definitions* come from the UCM when present; otherwise they are
    derived from ``cfg.scenarios`` (config ``ScenarioSpec``).  If both exist
    the UCM wins for the definitions while config supplies which are active.

    When ``cfg.mcmm`` is absent or ``enabled`` is False the matrix is built
    disabled (legacy single-scenario compatibility).
    """
    mcmm = getattr(cfg, "mcmm", None)
    enabled = bool(getattr(mcmm, "enabled", False)) if mcmm is not None else False

    # Prefer authoritative definitions from the ConstraintSet.
    definitions: dict[str, Scenario] = {}
    if cset is not None and getattr(cset, "scenarios", None):
        definitions = {sid: s for sid, s in cset.scenarios.items() if s is not None}

    # Supplement / fall back to config-defined scenarios.
    cfg_scenarios = list(getattr(cfg, "scenarios", None) or [])
    for spec in cfg_scenarios:
        sid = getattr(spec, "id", None)
        if not sid:
            continue
        if sid in definitions:
            continue
        definitions[sid] = _scenario_from_spec(sid, spec, cfg)

    if not definitions:
        # Legacy default single scenario.
        libs = _config_libraries(cfg)
        definitions["S000"] = Scenario(
            id="S000", mode="functional", corner="default", libraries=libs,
        )
        # Default single-scenario -> legacy (disabled) behaviour.
        enabled = False

    # Active selection from config.
    active_ids: list[str] = []
    if mcmm is not None and getattr(mcmm, "active_scenario_ids", None):
        active_ids = list(mcmm.active_scenario_ids)
    else:
        active_ids = [sid for sid, s in sorted(definitions.items())
                      if getattr(s, "active", True)]

    # If nothing selected but enabled, keep all active scenarios.
    if enabled and not active_ids:
        active_ids = [sid for sid, s in sorted(definitions.items())
                      if getattr(s, "active", True)]

    return ScenarioMatrix(scenarios=definitions, active_ids=active_ids,
                          enabled=enabled)


def _scenario_from_spec(sid: str, spec: Any, cfg: Any) -> Scenario:
    libs = list(getattr(spec, "libraries", None) or [])
    if not libs:
        libs = _config_libraries(cfg)
    return Scenario(
        id=sid,
        mode=getattr(spec, "mode", "functional"),
        corner=getattr(spec, "corner", "slow"),
        libraries=list(libs),
        parasitics=getattr(spec, "parasitics", None),
        sdc_set_id=getattr(spec, "sdc_set_id", None) or getattr(spec, "constraints_id", None),
        environment=dict(getattr(spec, "environment", None) or {}),
        active=bool(getattr(spec, "active", True)),
        parent_scenario_id=None,
    )


def _config_libraries(cfg: Any) -> list[str]:
    flow = getattr(cfg, "flow", None)
    if flow is None:
        return []
    return list(flow.liberty_files() or [])


__all__ = ["ScenarioMatrix", "build_scenario_matrix"]
