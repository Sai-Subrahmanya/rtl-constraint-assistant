"""
MCMM scenario model (Manual §34, §84, §85; Step 12).

The canonical ``Scenario`` model lives in :mod:`rca.constraint_model` and is
the single source of truth (it is the type carried by ``ConstraintSet`` and
referenced by ``Constraint.scenario_ids``).  This module re-exports that model
and provides :func:`build_scenarios`, which derives scenario definitions from
the project configuration without introducing a second scenario model.
"""

from __future__ import annotations

from typing import Any

from ..constraint_model import Scenario

__all__ = ["Scenario", "build_scenarios"]


def build_scenarios(cfg) -> list[Scenario]:
    """Build the canonical :class:`Scenario` definitions from project config.

    Uses ``cfg.scenarios`` (``ScenarioSpec``).  If none are configured, a single
    legacy ``functional/default`` scenario is returned.  MCMM enablement is
    handled separately by :func:`rca.mcmm.build_scenario_matrix`.
    """
    scenarios: list[Scenario] = []
    cfg_scenarios = list(getattr(cfg, "scenarios", None) or [])
    if cfg_scenarios:
        for i, s in enumerate(cfg_scenarios):
            sid = getattr(s, "id", None) or f"S{i:02d}"
            libs = list(getattr(s, "libraries", None) or [])
            if not libs:
                libs = _config_libraries(cfg)
            scenarios.append(Scenario(
                id=sid,
                mode=getattr(s, "mode", "functional"),
                corner=getattr(s, "corner", "slow"),
                libraries=list(libs),
                parasitics=getattr(s, "parasitics", None),
                sdc_set_id=getattr(s, "sdc_set_id", None) or getattr(s, "constraints_id", None),
                environment=dict(getattr(s, "environment", None) or {}),
                active=bool(getattr(s, "active", True)),
                parent_scenario_id=None,
            ))
    else:
        scenarios.append(Scenario(
            id="S000", mode="functional", corner="default",
            libraries=_config_libraries(cfg),
        ))
    return scenarios


def _config_libraries(cfg) -> list[str]:
    flow = getattr(cfg, "flow", None)
    if flow is None:
        return []
    return list(flow.liberty_files() or [])
