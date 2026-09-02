"""
Timing path representation (Manual §7.7, §17).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..utils.enums import TimingPathClass


class TimingPath(BaseModel):
    id: str | None = None
    startpoint: str
    endpoint: str
    launch_clock: str | None = None
    capture_clock: str | None = None
    path_type: TimingPathClass = TimingPathClass.REG_TO_REG
    combinational_elements: list[str] = Field(default_factory=list)
    arrival_time: float | None = None       # seconds
    required_time: float | None = None      # seconds
    slack: float | None = None              # seconds
    path_group: str | None = None
    scenario: str | None = None
    exceptions_applied: list[str] = Field(default_factory=list)

    def is_violating(self) -> bool:
        return self.slack is not None and self.slack < 0

    def summary(self) -> dict[str, Any]:
        return {
            "start": self.startpoint,
            "end": self.endpoint,
            "launch": self.launch_clock,
            "capture": self.capture_clock,
            "type": self.path_type.value,
            "slack_ns": (self.slack * 1e9) if self.slack is not None else None,
        }
