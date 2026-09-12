"""Read-only deterministic lifecycle replay-evidence projection (Step 43)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..utils.hashing import stable_hash


class ReplayEvidenceStatus(str, Enum):
    CURRENT = "CURRENT"
    NOT_SUPPLIED = "NOT_SUPPLIED"
    INCOMPLETE = "INCOMPLETE"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ReplayEvidenceComponent:
    name: str
    status: ReplayEvidenceStatus
    identity: str = ""
    detail: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "identity": self.identity or None,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class ReplayEvidenceReport:
    """A portable identity report, never a request or claim to execute tools."""

    id: str
    ucm_content_identity: str
    engineering_configuration_identity: str
    scenario_ids: tuple[str, ...]
    components: tuple[ReplayEvidenceComponent, ...]
    replay_readiness: str
    schema_version: int = 1
    kind: str = "rca_replay_evidence"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "schema_version": self.schema_version,
            "ucm_content_identity": self.ucm_content_identity,
            "engineering_configuration_identity": self.engineering_configuration_identity or None,
            "scenario_ids": list(self.scenario_ids),
            "components": [item.to_dict() for item in self.components],
            "replay_readiness": self.replay_readiness,
            "automatic_replay_supported": False,
            "meaning": ("Read-only evidence and identity assessment. It does not execute a replay nor "
                        "claims equivalent tool results or external signoff."),
        }


def make_replay_evidence_id(*, ucm_content_identity: str, engineering_configuration_identity: str,
                            scenario_ids: tuple[str, ...], components: tuple[ReplayEvidenceComponent, ...]) -> str:
    return "RPE-" + stable_hash({
        "schema": "rca-replay-evidence-v1",
        "ucm_content_identity": ucm_content_identity,
        "engineering_configuration_identity": engineering_configuration_identity,
        "scenario_ids": scenario_ids,
        "components": [item.to_dict() for item in components],
    })[:20]
