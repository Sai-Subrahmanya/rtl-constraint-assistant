"""Validation result and issue types (Step 7 strengthened).

The validator emits structured :class:`ValidationIssue` records grouped
into a :class:`ValidationReport`.  Every issue carries a stable id
(deterministic, derived from category/code/constraint/object), the
category, severity, involved constraint ids, object names, evidence,
and an actionable suggestion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..utils.enums import (
    ErrorCode, Severity, ValidationCategory, ValidationStatus,
)
from ..utils.hashing import stable_hash


# Severity groups that are considered blocking under the default
# blocking policy.  Safe-mode tweaks are handled by the engine, not here.
_BLOCKING_SEVERITIES_DEFAULT = {Severity.CRITICAL, Severity.HIGH, Severity.ERROR}


@dataclass
class ValidationIssue:
    """A single, structured validation finding.

    Deterministic identity: ``issue_id`` is derived from
    (category, code, constraint_id, sorted(related_constraint_ids),
     sorted(object_names), scenario_id, message fingerprint) so that
    identical inputs always produce identical issue IDs regardless of
    iteration order.
    """

    severity: Severity
    category: ValidationCategory
    code: ErrorCode
    message: str
    issue_id: str = ""
    constraint_id: str | None = None
    related_constraint_ids: list[str] = field(default_factory=list)
    object_names: list[str] = field(default_factory=list)
    scenario_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    suggestion: str | None = None
    blocking: bool = False
    source_location: dict[str, Any] | None = None  # {file, line} if available
    # Step 13: provenance & explanation (Req 12). Answer "why was this
    # marked invalid/risky?".
    source_kind: str | None = None       # RTL / USER / INFERENCE / ...
    origin: str | None = None            # rule/inference origin (e.g. CLK-001)
    assumption_ids: list[str] = field(default_factory=list)
    # Resolution state — distinguishes known-valid / known-invalid /
    # requires-user-input / unknown / unresolved without inventing validity.
    # Default RESOLVED for compatibility; UNKNOWN/UNRESOLVED used whenever
    # the validator cannot prove the finding either way.
    resolution_status: str = "RESOLVED"  # RESOLVED | UNKNOWN | UNRESOLVED | REQUIRES_USER_INPUT

    # -- construction helpers ------------------------------------------------
    def __post_init__(self) -> None:
        if not self.issue_id:
            self.issue_id = self._make_id()

    def _make_id(self) -> str:
        rel = tuple(sorted(set(self.related_constraint_ids or [])))
        objs = tuple(sorted(set(self.object_names or [])))
        key = (
            self.category.value,
            self.code.value,
            self.constraint_id or "",
            rel,
            objs,
            self.scenario_id or "",
            (self.message or "")[:160],
            self.source_kind or "",
            self.origin or "",
            self.resolution_status,
        )
        return "V" + stable_hash(key)[:8].upper()

    def with_blocking(self, blocking: bool = True) -> "ValidationIssue":
        self.blocking = blocking
        return self

    # -- serialization -------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "severity": self.severity.value,
            "category": self.category.value,
            "code": self.code.value,
            "message": self.message,
            "constraint_id": self.constraint_id,
            "related_constraint_ids": list(self.related_constraint_ids),
            "object_names": list(self.object_names),
            "scenario_id": self.scenario_id,
            "evidence": dict(self.evidence),
            "suggestion": self.suggestion,
            "blocking": self.blocking,
            "source_location": dict(self.source_location) if self.source_location else None,
            "source_kind": self.source_kind,
            "origin": self.origin,
            "assumption_ids": list(self.assumption_ids),
            "resolution_status": self.resolution_status,
        }


@dataclass
class ValidationReport:
    """Collects all issues for a validation run plus phase bookkeeping."""

    issues: list[ValidationIssue] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)
    # Sub-reports
    conflict_summary: dict[str, Any] = field(default_factory=dict)
    overlap_summary: dict[str, Any] = field(default_factory=dict)
    reference_summary: dict[str, Any] = field(default_factory=dict)
    exception_summary: dict[str, Any] = field(default_factory=dict)
    scenario_summary: dict[str, Any] = field(default_factory=dict)
    completeness_summary: dict[str, Any] = field(default_factory=dict)

    # -- mutation ------------------------------------------------------------
    def add(self, issue: ValidationIssue) -> None:
        # Deduplicate by issue_id to preserve determinism.
        if not any(i.issue_id == issue.issue_id for i in self.issues):
            self.issues.append(issue)

    def extend(self, issues: Iterable[ValidationIssue]) -> None:
        for i in issues:
            self.add(i)

    # -- queries -------------------------------------------------------------
    def errors(self) -> list[ValidationIssue]:
        """CRITICAL / HIGH / ERROR severities."""
        return [i for i in self.issues if i.severity in
                (Severity.CRITICAL, Severity.HIGH, Severity.ERROR)]

    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity in
                (Severity.WARNING, Severity.MEDIUM)]

    def infos(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity in
                (Severity.LOW, Severity.INFO)]

    def blocking_issues(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.blocking]

    def by_category(self, cat: ValidationCategory) -> list[ValidationIssue]:
        return [i for i in self.issues if i.category == cat]

    def by_code(self, code: ErrorCode) -> list[ValidationIssue]:
        return [i for i in self.issues if i.code == code]

    def has_errors(self) -> bool:
        return bool(self.errors())

    def is_blocked(self) -> bool:
        return bool(self.blocking_issues())

    def overall_status(self) -> ValidationStatus:
        if any(i.severity == Severity.CRITICAL for i in self.issues):
            return ValidationStatus.BLOCKED
        if self.is_blocked() and self.has_errors():
            return ValidationStatus.BLOCKED
        if any(i.severity == Severity.ERROR for i in self.issues):
            return ValidationStatus.PASS_WITH_WARNINGS
        if self.warnings():
            return ValidationStatus.PASS_WITH_WARNINGS
        return ValidationStatus.PASS

    # -- summary -------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        by_sev: dict[str, int] = {}
        for i in self.issues:
            by_sev[i.severity.value] = by_sev.get(i.severity.value, 0) + 1
        return {
            "checks_run": list(self.checks_run),
            "status": self.overall_status().value,
            "total_issues": len(self.issues),
            "by_severity": by_sev,
            "errors": len(self.errors()),
            "warnings": len(self.warnings()),
            "blocking": len(self.blocking_issues()),
            "conflict_summary": dict(self.conflict_summary),
            "overlap_summary": dict(self.overlap_summary),
            "reference_summary": dict(self.reference_summary),
            "exception_summary": dict(self.exception_summary),
            "scenario_summary": dict(self.scenario_summary),
            "completeness_summary": dict(self.completeness_summary),
            "issues": [i.to_dict() for i in self.issues],
        }


# ---------------------------------------------------------------------------
# Internal helper for concise issue construction across validation modules.
# ---------------------------------------------------------------------------


def _issue(report: ValidationReport,
           severity: Severity,
           category: ValidationCategory,
           code: ErrorCode,
           message: str,
           *,
           constraint_id: str | None = None,
           related_constraint_ids: list[str] | None = None,
           object_names: list[str] | None = None,
           scenario_id: str | None = None,
           evidence: dict[str, Any] | None = None,
           suggestion: str | None = None,
           blocking: bool | None = None,
           source_location: dict[str, Any] | None = None,
           source_kind: str | None = None,
           origin: str | None = None,
           assumption_ids: list[str] | None = None,
           resolution_status: str | None = None) -> ValidationIssue:
    """Build a :class:`ValidationIssue` and add it to the report.

    Default blocking policy: CRITICAL/HIGH/ERROR severity blocks by
    default; callers may override explicitly with ``blocking=...``.
    """
    if blocking is None:
        blocking = severity in _BLOCKING_SEVERITIES_DEFAULT
    issue = ValidationIssue(
        severity=severity, category=category, code=code, message=message,
        constraint_id=constraint_id,
        related_constraint_ids=list(related_constraint_ids or []),
        object_names=list(object_names or []),
        scenario_id=scenario_id,
        evidence=dict(evidence or {}),
        suggestion=suggestion,
        blocking=blocking,
        source_location=dict(source_location) if source_location else None,
        source_kind=source_kind,
        origin=origin,
        assumption_ids=list(assumption_ids or []),
        resolution_status=resolution_status or "RESOLVED",
    )
    report.add(issue)
    return issue
