"""
MCMM scenario model (Manual §34, §84, §85).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Scenario:
    id: str
    mode: str = "functional"
    corner: str = "slow"
    libraries: list[str] = field(default_factory=list)
    parasitics: str | None = None
    constraints_id: str | None = None
    environment: dict[str, Any] = field(default_factory=dict)
    active: bool = True


def build_scenarios(cfg) -> list[Scenario]:
    """Build scenarios from project config (default functional/slow if none)."""
    scenarios: list[Scenario] = []
    if cfg.scenarios:
        for i, s in enumerate(cfg.scenarios):
            scenarios.append(Scenario(
                id=s.id or f"S{i:02d}",
                mode=s.mode, corner=s.corner,
                libraries=s.libraries or cfg.flow.liberty_files(),
                parasitics=s.parasitics,
            ))
    else:
        scenarios.append(Scenario(
            id="S000", mode="functional", corner="default",
            libraries=cfg.flow.liberty_files(),
        ))
    return scenarios


__all__ = ["Scenario", "build_scenarios"]
