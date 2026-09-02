"""
Clock domain and clock-domain relationships (Manual §7.6, §18).

A ``ClockDomain`` groups all sequential elements clocked by the same
primary clock. Membership is derived from ``Register.clock_signal``
(structural evidence), not from names.

A ``ClockDomainEdge`` represents the (possibly unknown) timing
relationship between two clocks. The default state is UNKNOWN.
Relationships are only upgraded when concrete evidence exists (user
declaration, generated-clock derivation, observed synchronizer, etc.).
We NEVER declare two clocks ASYNCHRONOUS just because their names
differ, and never declare them SYNCHRONOUS just because they look
related.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..utils.enums import ClockDomainRelationship


class ClockDomain(BaseModel):
    id: str
    name: str
    clock_ids: list[str] = Field(default_factory=list)
    register_paths: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    reset_ids: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)

    def register_count(self) -> int:
        return len(self.register_paths)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "clocks": list(self.clock_ids),
            "registers": self.register_count(),
            "resets": list(self.reset_ids),
            "sources": list(self.sources),
        }


class ClockDomainEdge(BaseModel):
    clock_a: str
    clock_b: str
    relationship: ClockDomainRelationship = ClockDomainRelationship.UNKNOWN
    evidence: list[str] = Field(default_factory=list)
    confidence: str = "UNKNOWN"
    user_confirmation_required: bool = True
    active_scenarios: list[str] = Field(default_factory=list)
    cdc_paths_observed: int = 0

    def set_relationship(
        self,
        rel: ClockDomainRelationship,
        evidence_detail: str,
        confidence: str = "MEDIUM",
        user_confirm: bool = True,
    ) -> None:
        """Update the relationship, appending evidence.  Only upgrades
        confidence if it doesn't regress an existing HIGH-confidence
        classification unless ``user_confirm`` is False."""
        # If currently HIGH from USER, do not silently downgrade.
        if self.confidence == "HIGH" and confidence != "HIGH" and self.user_confirmation_required is False:
            return
        self.relationship = rel
        if evidence_detail not in self.evidence:
            self.evidence.append(evidence_detail)
        self.confidence = confidence
        self.user_confirmation_required = user_confirm

    def summary(self) -> dict[str, Any]:
        return {
            "a": self.clock_a,
            "b": self.clock_b,
            "relationship": self.relationship.value,
            "confidence": self.confidence,
            "user_confirm": self.user_confirmation_required,
            "cdc_paths": self.cdc_paths_observed,
            "evidence": list(self.evidence),
        }
