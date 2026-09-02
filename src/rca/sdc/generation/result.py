"""Generation result model (Step 6 §20)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...utils.enums import DiagnosticSeverity as Diag


@dataclass
class GenerationDiagnostic:
    severity: str            # INFO/WARNING/ERROR/SECURITY
    code: str
    message: str
    constraint_id: str | None = None
    constraint_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "constraint_id": self.constraint_id,
            "constraint_type": self.constraint_type,
        }


class GenerationStatus:
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


@dataclass
class SdcGenerationResult:
    text: str = ""
    backend: str = "generic"
    safe_mode: str = "balanced"
    status: str = GenerationStatus.COMPLETE
    emitted_constraint_ids: list[str] = field(default_factory=list)
    skipped_constraint_ids: list[str] = field(default_factory=list)
    diagnostics: list[GenerationDiagnostic] = field(default_factory=list)
    capabilities: dict[str, bool] = field(default_factory=dict)
    semantic_hash: str | None = None
    design_name: str = "top"
    stats: dict[str, int] = field(default_factory=dict)

    def add(self, diag: GenerationDiagnostic) -> None:
        self.diagnostics.append(diag)

    def errors(self) -> list[GenerationDiagnostic]:
        return [d for d in self.diagnostics if d.severity == "ERROR"]

    def warnings(self) -> list[GenerationDiagnostic]:
        return [d for d in self.diagnostics if d.severity == "WARNING"]

    def summary(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "safe_mode": self.safe_mode,
            "status": self.status,
            "design": self.design_name,
            "emitted": len(self.emitted_constraint_ids),
            "skipped": len(self.skipped_constraint_ids),
            "errors": len(self.errors()),
            "warnings": len(self.warnings()),
        }
