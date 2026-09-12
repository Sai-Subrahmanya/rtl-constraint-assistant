"""Read-only lifecycle orchestration projection records (Steps 34/36/42/43)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class WorkflowStageStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PENDING = "PENDING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    RELEASE_BLOCKED = "RELEASE_BLOCKED"
    HANDOFF_BLOCKED = "HANDOFF_BLOCKED"
    EDA_UNAVAILABLE = "EDA_UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


@dataclass(frozen=True)
class WorkflowStage:
    name: str
    status: WorkflowStageStatus
    identity: str = ""
    message: str = ""
    references: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status.value, "identity": self.identity or None,
                "message": self.message, "references": sorted(set(self.references))}


@dataclass(frozen=True)
class WorkflowReport:
    """A deterministic presentation/projection, never a lifecycle authority."""

    id: str
    ucm_content_identity: str
    ucm_semantic_identity: str
    scope_identity: str = ""
    stages: tuple[WorkflowStage, ...] = ()
    summary: tuple[tuple[str, str], ...] = ()
    schema_version: int = 1
    kind: str = "rca_constraint_workflow"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "schema_version": self.schema_version, "id": self.id,
            "ucm_content_identity": self.ucm_content_identity or None,
            "ucm_semantic_identity": self.ucm_semantic_identity or None,
            "scope_identity": self.scope_identity or None,
            "stages": [item.to_dict() for item in self.stages],
            "summary": {key: value for key, value in sorted(self.summary)},
        }


__all__ = ["WorkflowReport", "WorkflowStage", "WorkflowStageStatus"]
