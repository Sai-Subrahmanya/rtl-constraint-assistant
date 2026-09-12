"""Composition-only complete lifecycle projection using existing RCA authorities."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..constraint_model import ConstraintSet, stable_hash_cset
from ..equivalence import has_unsupported_options, normalize_constraint
from ..handoff import HandoffAssessment, HandoffStatus
from ..lineage import ConstraintLineageReport
from ..readiness import ConstraintReadinessReport
from ..release import ReleaseAssessment, ReleaseStatus, ReleaseVerification
from ..review import ConstraintReviewAssessment, ConstraintReviewStatus
from ..utils.hashing import stable_hash
from ..validation import ValidationResult
from .models import WorkflowReport, WorkflowStage, WorkflowStageStatus


@dataclass(frozen=True)
class WorkflowInputs:
    """References to existing results; this object neither owns nor creates them."""

    design: Any | None = None
    knowledge: Any | None = None
    inference: Any | None = None
    application: Any | None = None
    validation: ValidationResult | None = None
    readiness: ConstraintReadinessReport | None = None
    lineage: ConstraintLineageReport | None = None
    review: ConstraintReviewAssessment | None = None
    release: ReleaseAssessment | None = None
    package_verification: ReleaseVerification | None = None
    handoff: HandoffAssessment | None = None
    eda_manifest: Any | None = None
    formal_results: tuple[Any, ...] = ()


def build_complete_workflow(cset: ConstraintSet, *, inputs: WorkflowInputs | None = None) -> WorkflowReport:
    """Project the complete lifecycle without advancing any lifecycle stage.

    No candidate is accepted, no review/release is decided, no artifact is
    written, and no formal/EDA command is run. The report is intentionally a
    coordinator/presentation layer that preserves existing identities/statuses.
    """
    inputs = inputs or WorkflowInputs()
    ucm = stable_hash_cset(cset)
    semantic = _semantic_identity(cset)
    stages: list[WorkflowStage] = []
    stages.append(_analysis(inputs.design))
    stages.append(_knowledge(inputs.knowledge))
    stages.append(_inference(inputs.inference))
    stages.append(_application(inputs.application))
    stages.append(WorkflowStage("canonical_ucm", WorkflowStageStatus.COMPLETE, ucm,
                                "Existing canonical UCM is referenced; it was not modified."))
    stages.append(_validation(inputs.validation))
    stages.append(_readiness(inputs.readiness))
    stages.append(_lineage(inputs.lineage))
    stages.append(_review(inputs.review))
    stages.append(_release(inputs.release))
    stages.append(_package(inputs.package_verification))
    stages.append(_handoff(inputs.handoff))
    stages.append(_eda(inputs.eda_manifest))
    stages.append(_formal(inputs.formal_results))
    scope = ""
    if inputs.release is not None:
        scope = inputs.release.release.scope.scenario_definition_identity
    elif inputs.review is not None:
        scope = inputs.review.review.scope.scenario_definition_identity
    summary = (
        ("applied_constraint_count", str(len(cset))),
        ("governance", _governance_state(stages)),
        ("next_action", _next_action(stages)),
        ("scenario_scope_preserved", "true" if scope else "unknown"),
    )
    report_id = "WFL-" + stable_hash({
        "ucm": ucm, "semantic": semantic, "scope": scope,
        "stages": [item.to_dict() for item in stages], "summary": dict(summary),
    })[:20]
    return WorkflowReport(report_id, ucm, semantic, scope, tuple(stages), summary)


def explain_complete_workflow(report: WorkflowReport) -> str:
    """Human-readable companion for the deterministic workflow JSON."""
    lines = ["RCA COMPLETE LIFECYCLE", "=" * 22]
    for stage in report.stages:
        suffix = f" — {stage.message}" if stage.message else ""
        lines.append(f"{stage.name}: {stage.status.value}{suffix}")
    summary = dict(report.summary)
    lines.append(f"Next action: {summary.get('next_action', 'UNKNOWN')}")
    lines.append("This projection does not apply, approve, release, or execute EDA/formal.")
    return "\n".join(lines)


def _analysis(design: Any | None) -> WorkflowStage:
    return _present("analysis", design, "Design analysis is available.", "No design analysis was supplied.")


def _knowledge(knowledge: Any | None) -> WorkflowStage:
    return _present("knowledge", knowledge, "Offline advisory knowledge is available.", "No knowledge result was supplied.")


def _inference(inference: Any | None) -> WorkflowStage:
    if inference is None:
        return WorkflowStage("inference", WorkflowStageStatus.PENDING, message="No inference report was supplied.")
    candidates = tuple(getattr(inference, "candidates", ()) or ())
    missing = tuple(getattr(inference, "required_information", lambda: ())() or ())
    identity = stable_hash(inference.to_dict() if hasattr(inference, "to_dict") else str(inference))
    if missing:
        return WorkflowStage("inference", WorkflowStageStatus.CONFIRMATION_REQUIRED, identity,
                             "Inference retained required/unsafe-to-infer information.")
    if candidates:
        return WorkflowStage("inference", WorkflowStageStatus.PENDING, identity,
                             "Candidates are advisory until explicit application.")
    return WorkflowStage("inference", WorkflowStageStatus.COMPLETE, identity, "No unaccepted inference candidate remains.")


def _application(application: Any | None) -> WorkflowStage:
    if application is None:
        return WorkflowStage("application", WorkflowStageStatus.PENDING, message="No explicit application receipt was supplied.")
    status = str(getattr(application, "status", ""))
    identity = stable_hash(application.to_dict() if hasattr(application, "to_dict") else str(application))
    if "REJECT" in status or "STALE" in status or "FAIL" in status:
        return WorkflowStage("application", WorkflowStageStatus.FAILED, identity, f"Application status is {status or 'UNKNOWN'}.")
    return WorkflowStage("application", WorkflowStageStatus.COMPLETE, identity,
                         "An explicit application receipt was supplied.")


def _validation(validation: ValidationResult | None) -> WorkflowStage:
    if validation is None:
        return WorkflowStage("validation_coverage_mcmm", WorkflowStageStatus.UNKNOWN, message="No validation/coverage result was supplied.")
    identity = stable_hash(validation.as_dict())
    status = str(validation.status).upper()
    if status in {"PASS", "PASS_WITH_WARNINGS"}:
        return WorkflowStage("validation_coverage_mcmm", WorkflowStageStatus.COMPLETE, identity,
                             f"Validation status is {status}; coverage remains separately evidenced.")
    return WorkflowStage("validation_coverage_mcmm", WorkflowStageStatus.FAILED, identity,
                         f"Validation status is {status or 'UNKNOWN'}.")


def _readiness(readiness: ConstraintReadinessReport | None) -> WorkflowStage:
    if readiness is None:
        return WorkflowStage("readiness", WorkflowStageStatus.UNKNOWN, message="No readiness evidence was supplied.")
    identity = stable_hash(readiness.to_dict())
    status = readiness.status.value
    if status in {"READY", "READY_WITH_WARNINGS"}:
        return WorkflowStage("readiness", WorkflowStageStatus.COMPLETE, identity, f"Readiness is {status}.")
    return WorkflowStage("readiness", WorkflowStageStatus.FAILED, identity, f"Readiness is {status}.")


def _lineage(lineage: ConstraintLineageReport | None) -> WorkflowStage:
    if lineage is None:
        return WorkflowStage("lineage", WorkflowStageStatus.UNKNOWN, message="No lineage report was supplied.")
    return WorkflowStage("lineage", WorkflowStageStatus.COMPLETE, lineage.snapshot.snapshot_identity,
                         "Existing Step-30 lineage projection is available.")


def _review(review: ConstraintReviewAssessment | None) -> WorkflowStage:
    if review is None:
        return WorkflowStage("review", WorkflowStageStatus.REVIEW_REQUIRED, message="An explicit Step-31 review is required.")
    status = review.current_status
    if status in {ConstraintReviewStatus.APPROVED, ConstraintReviewStatus.APPROVED_WITH_WARNINGS}:
        return WorkflowStage("review", WorkflowStageStatus.COMPLETE, review.review.id, f"Review status is {status.value}.")
    if status == ConstraintReviewStatus.STALE:
        return WorkflowStage("review", WorkflowStageStatus.FAILED, review.review.id, "Review is stale.")
    return WorkflowStage("review", WorkflowStageStatus.REVIEW_REQUIRED, review.review.id,
                         f"Review status is {status.value}; no approval is implied.")


def _release(release: ReleaseAssessment | None) -> WorkflowStage:
    if release is None:
        return WorkflowStage("release", WorkflowStageStatus.REVIEW_REQUIRED, message="Release cannot be assessed until reviewed evidence is supplied.")
    status = release.current_status
    if status in {ReleaseStatus.RELEASED, ReleaseStatus.RELEASED_WITH_WARNINGS}:
        return WorkflowStage("release", WorkflowStageStatus.COMPLETE, release.release.id, f"Explicit release status is {status.value}.")
    if release.release_possible or release.release_with_warnings_possible:
        return WorkflowStage("release", WorkflowStageStatus.REVIEW_REQUIRED, release.release.id,
                             "Release is possible only through an explicit release action.")
    return WorkflowStage("release", WorkflowStageStatus.RELEASE_BLOCKED, release.release.id,
                         f"Release status is {status.value}; it is not released.")


def _package(verification: ReleaseVerification | None) -> WorkflowStage:
    if verification is None:
        return WorkflowStage("package_verification", WorkflowStageStatus.PENDING, message="No release package verification was supplied.")
    status = verification.status.value
    if status == "VERIFIED":
        return WorkflowStage("package_verification", WorkflowStageStatus.COMPLETE, verification.package_id,
                             "Existing release package verified statelessly.")
    return WorkflowStage("package_verification", WorkflowStageStatus.FAILED, verification.package_id,
                         f"Package verification is {status}.")


def _handoff(handoff: HandoffAssessment | None) -> WorkflowStage:
    if handoff is None:
        return WorkflowStage("handoff", WorkflowStageStatus.PENDING, message="No downstream handoff assessment was supplied.")
    status = handoff.handoff.status
    if status in {HandoffStatus.HANDOFF_READY, HandoffStatus.HANDOFF_PREPARED, HandoffStatus.HANDOFF_EXECUTED}:
        return WorkflowStage("handoff", WorkflowStageStatus.COMPLETE, handoff.handoff.id, f"Handoff status is {status.value}.")
    return WorkflowStage("handoff", WorkflowStageStatus.HANDOFF_BLOCKED, handoff.handoff.id,
                         f"Handoff status is {status.value}.")


def _eda(manifest: Any | None) -> WorkflowStage:
    if manifest is None:
        return WorkflowStage("eda", WorkflowStageStatus.EDA_UNAVAILABLE,
                             message="No actual EDA manifest was supplied; no tool execution is claimed.")
    data = manifest.to_dict() if hasattr(manifest, "to_dict") else manifest
    return WorkflowStage("eda", WorkflowStageStatus.COMPLETE, stable_hash(data),
                         "Existing EDA manifest supplied; inspect its explicit backend/status evidence.")


def _formal(results: Iterable[Any]) -> WorkflowStage:
    values = tuple(results)
    if not values:
        return WorkflowStage("formal", WorkflowStageStatus.UNKNOWN, message="No formal result was supplied.")
    statuses = sorted(str(getattr(item, "status", getattr(getattr(item, "verification", None), "status", "UNKNOWN"))) for item in values)
    identity = stable_hash([str(item) for item in values])
    if any(value.upper().endswith("FAIL") or value.upper().endswith("ERROR") for value in statuses):
        return WorkflowStage("formal", WorkflowStageStatus.FAILED, identity, "Supplied formal evidence includes FAIL/ERROR.")
    if any("UNKNOWN" in value.upper() or "UNRESOLVED" in value.upper() for value in statuses):
        return WorkflowStage("formal", WorkflowStageStatus.UNKNOWN, identity, "Formal evidence remains UNKNOWN/UNRESOLVED.")
    return WorkflowStage("formal", WorkflowStageStatus.COMPLETE, identity, "Supplied formal evidence is non-failing.")


def _present(name: str, value: Any | None, known: str, absent: str) -> WorkflowStage:
    if value is None:
        return WorkflowStage(name, WorkflowStageStatus.UNKNOWN, message=absent)
    if hasattr(value, "to_dict"):
        data = value.to_dict()
    elif hasattr(value, "as_dict"):
        data = value.as_dict()
    elif hasattr(value, "patterns"):
        data = [item.to_dict() if hasattr(item, "to_dict") else str(item) for item in value.patterns()]
    elif isinstance(value, (dict, list, tuple, str, int, float, bool)) or value is None:
        data = value
    else:
        # Unknown opaque objects may carry a memory-address repr. Preserve the
        # stage as available but do not make an unstable opaque repr identity.
        data = None
    identity = stable_hash(data) if data is not None else ""
    return WorkflowStage(name, WorkflowStageStatus.COMPLETE, identity, known)


def _semantic_identity(cset: ConstraintSet) -> str:
    items = list(cset)
    if any(has_unsupported_options(item) for item in items):
        return ""
    return stable_hash([{"id": item.id, "semantic": normalize_constraint(item)} for item in items])


def _governance_state(stages: list[WorkflowStage]) -> str:
    values = {item.status for item in stages}
    if WorkflowStageStatus.FAILED in values or WorkflowStageStatus.RELEASE_BLOCKED in values:
        return "BLOCKED"
    if WorkflowStageStatus.REVIEW_REQUIRED in values:
        return "REVIEW_REQUIRED"
    if WorkflowStageStatus.CONFIRMATION_REQUIRED in values:
        return "CONFIRMATION_REQUIRED"
    return "OBSERVED"


def _next_action(stages: list[WorkflowStage]) -> str:
    for desired, action in (
        (WorkflowStageStatus.FAILED, "RESOLVE_FAILED_EVIDENCE"),
        (WorkflowStageStatus.CONFIRMATION_REQUIRED, "SUPPLY_OR_CONFIRM_REQUIRED_INFORMATION"),
        (WorkflowStageStatus.REVIEW_REQUIRED, "EXPLICIT_HUMAN_REVIEW_OR_RELEASE_ACTION"),
        (WorkflowStageStatus.RELEASE_BLOCKED, "RESOLVE_RELEASE_POLICY_BLOCKERS"),
        (WorkflowStageStatus.HANDOFF_BLOCKED, "RESOLVE_HANDOFF_COMPATIBILITY"),
        (WorkflowStageStatus.EDA_UNAVAILABLE, "OPTIONALLY_PROVISION_REAL_EDA"),
    ):
        if any(stage.status == desired for stage in stages):
            return action
    return "INSPECT_EXISTING_EVIDENCE"


__all__ = ["WorkflowInputs", "build_complete_workflow", "explain_complete_workflow"]
