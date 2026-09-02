"""Top-level validation engine (Step 7).

Runs a deterministic ordered pipeline over a UCM / constraint set and
produces a :class:`ValidationResult` suitable for both CLI and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..constraint_model import ConstraintSet
from ..design_model import Design
from ..exceptions.formal_backend import FormalBackend
from ..timing_model import TimingGraph
from ..utils.enums import ValidationStatus
from ..utils.logging import get_logger
from .backend import validate_backend
from .base import ValidationReport
from .completeness import validate_completeness
from .conflicts import validate_conflicts
from .coverage import compute_coverage
from .exceptions import validate_exceptions, validate_scenarios
from .references import validate_references
from .sdc_import import validate_sdc_import
from .semantic import validate_semantic

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
            "completeness_summary": self.report.completeness_summary,
        }


def run_validation(design: Design | None = None,
                   tg: TimingGraph | None = None,
                   cset: ConstraintSet | None = None,
                   backend: str = "generic",
                   mode: str = "balanced",
                   active_scenarios: set[str] | None = None,
                   parser: Any | None = None,
                   formal_backend: FormalBackend | None = None) -> ValidationResult:
    """Run the full multi-layer validation pipeline.

    Layers (in order):
      1. Reference integrity
      2. Semantic (clocks, generated clocks, IO, groups, selectors, values)
      3. Conflicts (+ precedence / user-vs-inference / exceptions)
      4. Exceptions + scenarios
      5. Completeness / missing information
      6. Backend capability
      7. Coverage (graph-aware)

    ``active_scenarios`` optionally restricts scenario validation to a
    given set of active scenario ids (used when MCMM is enabled);
    ``parser`` optionally supplies SDC importer diagnostics so the import
    is classified (syntactic/semantic/incomplete/complete/unresolved)
    without re-parsing; ``formal_backend`` optionally supplies a concrete
    proof backend.  If omitted, the conservative UNRESOLVED default keeps
    historical behavior unchanged.
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
    validate_exceptions(design, tg, cset, rep, formal_backend=formal_backend)
    validate_scenarios(cset, rep, active_scenarios=active_scenarios)

    # 5. completeness / missing-information
    validate_completeness(design, tg, cset, rep)

    # 6. backend capability
    try:
        validate_backend(cset, backend, rep, mode)
    except Exception as exc:  # noqa: BLE001 - vendor backend isolation boundary
        log.warning("backend validation skipped: %s", exc)

    # 7. coverage
    cov = compute_coverage(design, tg, cset, rep)

    # 8. SDC import classification (requires an importer/parser object).
    if parser is not None:
        validate_sdc_import(cset, parser=parser, report=rep)

    # 9. Hydrate provenance (Req 12): attach source_kind / origin so every
    # constraint-derived finding answers "why was this flagged?"  This is
    # derived from the UCM, never invented.
    _hydrate_provenance(cset, rep)

    res = ValidationResult(report=rep, coverage=cov)
    res.status = rep.overall_status().value
    return res


def _hydrate_provenance(cset: ConstraintSet, rep: ValidationReport) -> None:
    """Fill source_kind / origin on findings that reference a constraint.

    Keeps provenance authoritative and deterministic: the value comes from
    the constraint's ``source_kind``; the ``origin`` is derived from the
    constraint's provenance (rule_id) when available, otherwise from the
    issue's category.  Findings that are not tied to a constraint (e.g.
    coverage-only infos) keep ``source_kind`` None.
    """
    for issue in rep.issues:
        cid = issue.constraint_id
        if not cid:
            continue
        c = cset.get(cid)
        if c is None:
            continue
        if issue.source_kind is None:
            issue.source_kind = c.source_kind.value if hasattr(c.source_kind, "value") else str(c.source_kind)
        if issue.origin is None:
            issue.origin = _origin_for(c, issue)
        # Preserve scenario identity: if the finding is tied to a
        # constraint that targets exactly one scenario, inherit that
        # scenario so it is never collapsed (Req 9).  For multi-scenario
        # constraints we leave scenario_id None rather than invent one.
        if issue.scenario_id is None:
            sids = list(getattr(c, "scenario_ids", None) or [])
            if len(sids) == 1:
                issue.scenario_id = sids[0]


def _origin_for(c: Any, issue: Any) -> str:
    prov = getattr(c, "provenance", None)
    rule_id = getattr(prov, "rule_id", None) if prov is not None else None
    if rule_id:
        return str(rule_id)
    # Fall back to a deterministic, human-meaningful origin.
    return f"constraint:{issue.category.value.lower()}"
