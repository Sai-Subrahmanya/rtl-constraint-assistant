"""
Lightweight structural-path record used during connectivity analysis.

This object is *distinct* from ``rca.timing_model.TimingPath`` because it
is produced before any clocks/domains/STA results are known; it carries
only structural facts (start/end, class, combinational hops, launch/capture
clock names where evident).  ``TimingGraph`` later promotes these to full
:class:`TimingPath` objects alongside timing arcs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..utils.enums import TimingPathClass


class StructuralPath(BaseModel):
    id: str
    startpoint: str
    endpoint: str
    path_class: TimingPathClass
    launch_clock: str | None = None
    capture_clock: str | None = None
    combinational_via: list[str] = Field(default_factory=list)
    cross_domain: bool = False
    evidence: list[str] = Field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "class": self.path_class.value,
            "start": self.startpoint,
            "end": self.endpoint,
            "launch": self.launch_clock,
            "capture": self.capture_clock,
            "hops": len(self.combinational_via),
        }
