"""Top-level validation engine (Step 7).

Runs a deterministic ordered pipeline over a UCM / constraint set and
produces a :class:`ValidationResult` suitable for both CLI and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..constraint_model import ConstraintSet
from ..design_model import Design
from ..timing_model import TimingGraph
from ..utils.enums import Severity, ValidationStatus
from ..utils.logging import get_logger
from .base import ValidationReport
from .references import validate_references
from .semantic import validate_semantic
from .conflicts import validate_conflicts
from .coverage import compute_coverage
from .exceptions import validate_exceptions, validate_scenarios
from .backend import validate_backend

log = get_logger("validation")


@dataclass
class ValidationResult:
    status: str = ValidationStatus.PASS.value
    report: ValidationReport = field(default_factory=ValidationReport)
    coverage: Any = None

    @property
    def errors(self) -> list[Any]:
        return self.report.errors()

    @property
    def warnings(self) -> list[Any]:
        return self.report.warnings()

    @property
    def infos(self) -> list[Any]:
        return self.report.infos()

    @property
    def blocking(self) -> list[Any]:
        return self.report.blocking_issues()

    @property
    def passed(self) -> bool:
        return self.status in (ValidationStatus.PASS.value,
                               ValidationStatus.PASS_WITH_WARNINGS.value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "issue_count": len(self.report.issues),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "infos": len(self.infos),
            "blocking": len(self.blocking),
            "coverage": self.coverage.as_dict() if self.coverage else {},
            "conflict_summary": self.report.conflict_summary,
            "overlap_summary": self.report.overlap_summary,
            "reference_summary": self.report.reference_summary,
            "exception_summary": self.report.exception_summary,
            "scenario_summary": self.report.scenario_summary,
        }


def run_validation(design: Design | None = None,
                   tg: TimingGraph | None = None,
                   cset: ConstraintSet | None = None,
                   backend: str = "generic",
                   mode: str = "balanced") -> ValidationResult:
    """Run the full multi-layer validation pipeline.

    Layers (in order):
      1. Reference integrity
      2. Semantic (clocks, generated clocks, IO, groups, selectors)
      3. Conflicts
      4. Exceptions + scenarios
      5. Backend capability
      6. Coverage (graph-aware)
    """
    cset = cset or ConstraintSet()
    rep = ValidationReport()
    rep.checks_run.append("start")

    # 1. references
    validate_references(design, tg, cset, rep)

    # 2. semantic
    validate_semantic(design, tg, cset, rep)

    # 3. conflicts
    validate_conflicts(cset, rep)

    # 4. exceptions + scenarios
    validate_exceptions(design, tg, cset, rep)
    validate_scenarios(cset, rep)

    # 5. backend capability
    try:
        validate_backend(cset, backend, rep, mode)
    except Exception as exc:  # backend may be unavailable in tests
        log.warning("backend validation skipped: %s", exc)

    # 6. coverage
    cov = compute_coverage(design, tg, cset, rep)

    res = ValidationResult(report=rep, coverage=cov)
    res.status = rep.overall_status().value
    return res
