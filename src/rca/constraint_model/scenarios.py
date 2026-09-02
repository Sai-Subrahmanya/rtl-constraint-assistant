"""
Multi-mode / multi-corner scenario model (Manual §34, §84).

A Scenario is an independently analyzed (mode, corner) pair.  Each
constraint can be associated with zero or more scenarios via
``constraint.scenario_ids``; an empty list means "applies to all active
scenarios" (policy-controlled).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Scenario(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    id: str
    mode: str = "functional"          # functional | test | scan | sleep | ...
    corner: str = "slow"              # slow | fast | typical | ...
    libraries: list[str] = Field(default_factory=list)
    parasitics: str | None = None
    sdc_set_id: str | None = None
    environment: dict[str, Any] = Field(default_factory=dict)
    active: bool = True
    analysis_count: int = 0           # number of EDA runs executed in this scenario
    parent_scenario_id: str | None = None   # for derived scenarios (e.g. func_fast derived from func_slow)

    @property
    def name(self) -> str:
        return f"{self.mode}_{self.corner}"

    def semantic_key(self) -> tuple:
        return (self.mode, self.corner, tuple(sorted(self.libraries)),
                self.parasitics or "", tuple(sorted(self.environment.items())))

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "corner": self.corner,
            "name": self.name,
            "libraries": list(self.libraries),
            "parasitics": self.parasitics,
            "active": self.active,
            "parent": self.parent_scenario_id,
        }
