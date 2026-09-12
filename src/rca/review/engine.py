"""Read-only review assessment and explicit immutable review decisions (Step 31).

This module is a governance boundary over already-authoritative UCM,
validation/coverage, readiness, formal, MCMM, and lineage results. It neither
creates constraints nor runs inference, application, validation, formal, EDA,
SDC generation, artifact persistence, or SQLite history.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from ..constraint_model import ConstraintSet, stable_hash_cset
from ..equivalence import has_unsupported_options, normalize_constraint
from ..lineage import ConstraintLineageReport
from ..mcmm import build_scenario_matrix
from ..readiness import ConstraintReadinessReport
from ..utils.hashing import stable_hash
from ..validation import ValidationResult
from .models import (
    ConstraintReview,
    ConstraintReviewAssessment,
    ConstraintReviewStatus,
    ExternalEDASignoffStatus,
    ReviewActor,
    ReviewApproval,
    ReviewBlocker,
    ReviewDecisionKind,
    ReviewEvidence,
    ReviewEvidenceRef,
    ReviewFinding,
    ReviewFindingSeverity,
    ReviewPolicy,
    ReviewScope,
    ReviewSnapshot,
)

_VOLATILE_ID_KEYS = {
    "created_at", "recorded_at", "timestamp", "import_timestamp", "runtime_seconds",
    "duration_seconds", "enabled_at", "started_at", "finished_at",
}


class ReviewDecisionError(ValueError):
    """Raised when an explicit review decision fails closed under its policy."""


def create_constraint_review(
    cset: ConstraintSet,
    *,
    policy: ReviewPolicy | None = None,
    scenario_ids: Iterable[str] = (),
    all_active_scenarios: bool = False,
    config: Any | None = None,
    design: Any | None = None,
    timing_graph: Any | None = None,
    lineage: ConstraintLineageReport | None = None,
    readiness: ConstraintReadinessReport | None = None,
    validation: ValidationResult | None = None,
    formal_results: Iterable[Any] = (),
    external_eda_evidence_refs: Iterable[str] = (),
    supersedes_review_id: str | None = None,
) -> ConstraintReview:
    """Create an unapproved immutable review record for the supplied UCM.

    This operation only projects supplied evidence. A clean assessment remains
    ``NEEDS_REVIEW`` until :func:`decide_review` receives an explicit decision.
    """
    formal_results = tuple(formal_results)
    external_eda_evidence_refs = tuple(sorted({str(item) for item in external_eda_evidence_refs if item}))
    active, scenario_definition_identity = _active_scenarios(cset, config)
    scope = _scope(cset, active, scenario_definition_identity, scenario_ids, all_active_scenarios)
    policy = policy or ReviewPolicy()
    snapshot = _snapshot(cset, config, design, timing_graph, lineage, readiness, validation, formal_results)
    evidence, findings, blockers = _review_evidence_and_gates(
        cset, scope, policy, lineage, readiness, validation, formal_results,
    )
    status = _initial_review_status(scope, evidence, policy)
    root_id = _review_root_id(snapshot, scope, policy, evidence, supersedes_review_id)
    return ConstraintReview(
        id=root_id,
        review_root_id=root_id,
        reviewed_snapshot=snapshot,
        scope=scope,
        policy=policy,
        status=status,
        evidence=evidence,
        findings=findings,
        blockers=blockers,
        assumptions=_assumption_ids(cset),
        supersedes_review_id=supersedes_review_id,
        external_eda_signoff=(ExternalEDASignoffStatus.SUPPLIED_EVIDENCE
                              if external_eda_evidence_refs else ExternalEDASignoffStatus.UNKNOWN),
        external_eda_evidence_refs=external_eda_evidence_refs,
    )


def assess_constraint_review(
    cset: ConstraintSet,
    *,
    review: ConstraintReview | None = None,
    policy: ReviewPolicy | None = None,
    scenario_ids: Iterable[str] = (),
    all_active_scenarios: bool = False,
    config: Any | None = None,
    design: Any | None = None,
    timing_graph: Any | None = None,
    lineage: ConstraintLineageReport | None = None,
    readiness: ConstraintReadinessReport | None = None,
    validation: ValidationResult | None = None,
    formal_results: Iterable[Any] = (),
    external_eda_evidence_refs: Iterable[str] = (),
) -> ConstraintReviewAssessment:
    """Assess review eligibility/currentness without creating an approval.

    When a prior ``review`` is supplied it is never altered: this returns a
    current projection and reports ``STALE`` if the exact reviewed identity no
    longer matches the supplied canonical UCM or relevant current evidence.
    """
    formal_results = tuple(formal_results)
    if review is None:
        review = create_constraint_review(
            cset, policy=policy, scenario_ids=scenario_ids, all_active_scenarios=all_active_scenarios,
            config=config, design=design, timing_graph=timing_graph, lineage=lineage,
            readiness=readiness, validation=validation, formal_results=formal_results,
            external_eda_evidence_refs=external_eda_evidence_refs,
        )
    elif policy is not None and policy != review.policy:
        raise ReviewDecisionError("A stored review must be assessed under its recorded policy.")
    if not review.id or not review.reviewed_snapshot.ucm_content_identity:
        raise ReviewDecisionError("Review record is missing its exact reviewed UCM identity.")

    active, scenario_definition_identity = _active_scenarios(cset, config)
    current_scope = _scope(
        cset, active, scenario_definition_identity, review.scope.requested_scenario_ids,
        review.scope.scope_kind == "ALL_ACTIVE_SCENARIOS",
    )
    current_snapshot = _snapshot(cset, config, design, timing_graph, lineage, readiness, validation, formal_results)
    use_recorded_evidence = review is not None and lineage is None and readiness is None and validation is None \
        and not formal_results
    if use_recorded_evidence:
        # A decision can be made from the immutable evidence captured during
        # review creation. Callers need not reconstruct an authoritative
        # readiness/validation object merely to record an explicit human
        # decision; if they do supply current evidence, it is assessed below.
        evidence, findings, blockers = review.evidence, review.findings, review.blockers
    else:
        evidence, findings, blockers = _review_evidence_and_gates(
            cset, current_scope, review.policy, lineage, readiness, validation, formal_results,
        )
    # A stored review remains historical evidence. Its own blockers/findings
    # are preserved in ``review`` while assessment returns current supplied
    # evidence separately.
    staleness = _staleness_reasons(review, current_snapshot, current_scope, lineage, readiness)
    if staleness:
        current_status = ConstraintReviewStatus.STALE
    elif review.status == ConstraintReviewStatus.INVALID or current_scope.unknown_scenario_ids:
        current_status = ConstraintReviewStatus.INVALID
    else:
        current_status = review.status
    hard_blocked = bool(blockers)
    has_warnings = any(item.severity == ReviewFindingSeverity.WARNING for item in findings)
    eligible_state = current_status in {ConstraintReviewStatus.NEEDS_REVIEW, ConstraintReviewStatus.DEFERRED,
                                       ConstraintReviewStatus.REJECTED}
    approval_possible = eligible_state and not staleness and not hard_blocked and not has_warnings
    approval_with_warnings_possible = (
        eligible_state and not staleness and not hard_blocked and review.policy.allow_approval_with_warnings
    )
    return ConstraintReviewAssessment(
        review=review,
        current_snapshot=current_snapshot,
        current_status=current_status,
        approval_possible=approval_possible,
        approval_with_warnings_possible=approval_with_warnings_possible,
        snapshot_current=not staleness,
        staleness_reasons=staleness,
        evidence=evidence,
        findings=findings,
        blockers=blockers,
    )


def decide_review(
    review: ConstraintReview,
    cset: ConstraintSet,
    decision: ReviewDecisionKind,
    *,
    actor: ReviewActor | None = None,
    comment: str = "",
    config: Any | None = None,
    design: Any | None = None,
    timing_graph: Any | None = None,
    lineage: ConstraintLineageReport | None = None,
    readiness: ConstraintReadinessReport | None = None,
    validation: ValidationResult | None = None,
    formal_results: Iterable[Any] = (),
    external_eda_evidence_refs: Iterable[str] = (),
    recorded_at: str | None = None,
) -> ConstraintReview:
    """Return an explicit immutable successor review decision.

    Approval decisions require a current exact snapshot and the recorded policy
    gates. Reject/defer are explicit governance outcomes; they cannot cause a
    UCM or candidate mutation. ``recorded_at`` is optional display metadata and
    is deliberately excluded from deterministic identities.
    """
    formal_results = tuple(formal_results)
    assessment = assess_constraint_review(
        cset, review=review, config=config, design=design, timing_graph=timing_graph,
        lineage=lineage, readiness=readiness, validation=validation, formal_results=formal_results,
        external_eda_evidence_refs=external_eda_evidence_refs,
    )
    if decision in {ReviewDecisionKind.APPROVE, ReviewDecisionKind.APPROVE_WITH_WARNINGS}:
        if assessment.current_status == ConstraintReviewStatus.STALE:
            raise ReviewDecisionError("Cannot approve a stale reviewed constraint snapshot.")
        if assessment.current_status == ConstraintReviewStatus.INVALID:
            raise ReviewDecisionError("Cannot approve an invalid review scope or record.")
        if decision == ReviewDecisionKind.APPROVE and not assessment.approval_possible:
            raise ReviewDecisionError(_decision_failure("APPROVE", assessment))
        if decision == ReviewDecisionKind.APPROVE_WITH_WARNINGS and not assessment.approval_with_warnings_possible:
            raise ReviewDecisionError(_decision_failure("APPROVE_WITH_WARNINGS", assessment))
    if decision == ReviewDecisionKind.REVOKE:
        return revoke_review(
            review, cset, actor=actor, comment=comment, config=config, design=design,
            timing_graph=timing_graph, lineage=lineage, readiness=readiness, validation=validation,
            formal_results=formal_results, external_eda_evidence_refs=external_eda_evidence_refs,
            recorded_at=recorded_at,
        )

    actor = actor or ReviewActor()
    approval = _approval(review.review_root_id, decision, actor, comment, _evidence_ids(assessment.evidence), recorded_at)
    status = {
        ReviewDecisionKind.APPROVE: ConstraintReviewStatus.APPROVED,
        ReviewDecisionKind.APPROVE_WITH_WARNINGS: ConstraintReviewStatus.APPROVED_WITH_WARNINGS,
        ReviewDecisionKind.REJECT: ConstraintReviewStatus.REJECTED,
        ReviewDecisionKind.DEFER: ConstraintReviewStatus.DEFERRED,
    }[decision]
    history = (*review.decision_history, approval)
    return replace(
        review,
        id=_decision_review_id(review, approval),
        previous_review_id=review.id,
        status=status,
        approval=approval,
        decision_history=history,
        evidence=assessment_evidence(assessment),
        findings=assessment.findings,
        blockers=assessment.blockers,
        assumptions=_assumption_ids(cset),
    )


def approve_review(review: ConstraintReview, cset: ConstraintSet, *, actor: ReviewActor | None = None,
                   comment: str = "", **kwargs: Any) -> ConstraintReview:
    """Explicitly approve a current clean review; never changes the UCM."""
    return decide_review(review, cset, ReviewDecisionKind.APPROVE, actor=actor, comment=comment, **kwargs)


def approve_review_with_warnings(review: ConstraintReview, cset: ConstraintSet, *, actor: ReviewActor | None = None,
                                 comment: str = "", **kwargs: Any) -> ConstraintReview:
    """Explicitly approve with warnings only when recorded policy permits it."""
    return decide_review(review, cset, ReviewDecisionKind.APPROVE_WITH_WARNINGS,
                         actor=actor, comment=comment, **kwargs)


def reject_review(review: ConstraintReview, cset: ConstraintSet, *, actor: ReviewActor | None = None,
                  comment: str = "", **kwargs: Any) -> ConstraintReview:
    """Record an explicit rejection without changing any engineering state."""
    return decide_review(review, cset, ReviewDecisionKind.REJECT, actor=actor, comment=comment, **kwargs)


def defer_review(review: ConstraintReview, cset: ConstraintSet, *, actor: ReviewActor | None = None,
                 comment: str = "", **kwargs: Any) -> ConstraintReview:
    """Record an explicit defer decision without implicit approval."""
    return decide_review(review, cset, ReviewDecisionKind.DEFER, actor=actor, comment=comment, **kwargs)


def revoke_review(
    review: ConstraintReview,
    cset: ConstraintSet,
    *,
    actor: ReviewActor | None = None,
    comment: str = "",
    config: Any | None = None,
    design: Any | None = None,
    timing_graph: Any | None = None,
    lineage: ConstraintLineageReport | None = None,
    readiness: ConstraintReadinessReport | None = None,
    validation: ValidationResult | None = None,
    formal_results: Iterable[Any] = (),
    external_eda_evidence_refs: Iterable[str] = (),
    recorded_at: str | None = None,
) -> ConstraintReview:
    """Explicitly revoke a prior decision by returning a successor record.

    The original record is never overwritten; its decision remains in the
    successor's decision history. Revocation may be recorded after a snapshot
    has gone stale because it is a governance action, never an approval.
    """
    if review.status not in {ConstraintReviewStatus.APPROVED, ConstraintReviewStatus.APPROVED_WITH_WARNINGS}:
        raise ReviewDecisionError("Only an approved review can be explicitly revoked.")
    assessment = assess_constraint_review(
        cset, review=review, config=config, design=design, timing_graph=timing_graph,
        lineage=lineage, readiness=readiness, validation=validation, formal_results=tuple(formal_results),
        external_eda_evidence_refs=external_eda_evidence_refs,
    )
    actor = actor or ReviewActor()
    revocation = _approval(review.review_root_id, ReviewDecisionKind.REVOKE, actor, comment,
                           _evidence_ids(assessment.evidence), recorded_at)
    history = (*review.decision_history, revocation)
    return replace(
        review,
        id=_decision_review_id(review, revocation),
        previous_review_id=review.id,
        status=ConstraintReviewStatus.REVOKED,
        approval=revocation,
        decision_history=history,
        evidence=assessment_evidence(assessment),
        findings=assessment.findings,
        blockers=assessment.blockers,
        assumptions=_assumption_ids(cset),
    )


def supersede_review(
    review: ConstraintReview,
    cset: ConstraintSet,
    **kwargs: Any,
) -> ConstraintReview:
    """Create a separate unapproved review for a new snapshot, preserving linkage."""
    return create_constraint_review(cset, supersedes_review_id=review.id, **kwargs)


@dataclass(frozen=True)
class ConstraintReviewEngine:
    """Stateless façade over the one Step-31 governance/review projection."""

    def assess(self, cset: ConstraintSet, **kwargs: Any) -> ConstraintReviewAssessment:
        return assess_constraint_review(cset, **kwargs)

    def create(self, cset: ConstraintSet, **kwargs: Any) -> ConstraintReview:
        return create_constraint_review(cset, **kwargs)

    def decide(self, review: ConstraintReview, cset: ConstraintSet,
               decision: ReviewDecisionKind, **kwargs: Any) -> ConstraintReview:
        return decide_review(review, cset, decision, **kwargs)


def assessment_evidence(assessment: ConstraintReviewAssessment) -> tuple[ReviewEvidence, ...]:
    """Return the current projection's existing evidence references unchanged."""
    return assessment.evidence


def _snapshot(cset: ConstraintSet, config: Any | None, design: Any | None, timing_graph: Any | None,
              lineage: ConstraintLineageReport | None, readiness: ConstraintReadinessReport | None,
              validation: ValidationResult | None, formal_results: Iterable[Any]) -> ReviewSnapshot:
    formal_results = tuple(formal_results)
    coverage = getattr(validation, "coverage", None) if validation is not None else None
    lineage_snapshot = lineage.snapshot.snapshot_identity if lineage is not None else ""
    return ReviewSnapshot(
        ucm_content_identity=stable_hash_cset(cset),
        ucm_semantic_identity=_semantic_ucm_identity(cset),
        lineage_snapshot_identity=lineage_snapshot,
        lineage_report_identity=_identity(lineage.to_dict()) if lineage is not None else "",
        readiness_evidence_identity=_identity(readiness.to_dict()) if readiness is not None else "",
        validation_evidence_identity=_identity(validation.as_dict()) if validation is not None else "",
        coverage_evidence_identity=_identity(coverage.as_dict()) if coverage is not None else "",
        formal_evidence_identity=_identity([_as_dict(item) for item in formal_results]) if formal_results else "",
        configuration_identity=_identity(_as_dict(config)) if config is not None else "",
        design_identity=_identity(_as_dict(design)) if design is not None else "",
        timing_graph_identity=_identity(_as_dict(timing_graph)) if timing_graph is not None else "",
        constraint_ids=tuple(sorted(cset.constraints)),
        scenario_ids=tuple(sorted(cset.scenarios)),
    )


def _semantic_ucm_identity(cset: ConstraintSet) -> str:
    """Use existing Step-9 normalisation; unsupported semantics stay unknown."""
    constraints = list(cset)
    if any(has_unsupported_options(item) for item in constraints):
        return ""
    return stable_hash([{"id": item.id, "semantic": normalize_constraint(item)}
                        for item in sorted(constraints, key=lambda item: item.id)])


def _active_scenarios(cset: ConstraintSet, config: Any | None) -> tuple[tuple[str, ...], str]:
    if config is not None:
        matrix = build_scenario_matrix(config, cset)
        active = tuple(sorted(matrix.active_ids))
        definitions = matrix.summary().get("active_scenarios", [])
    else:
        active = tuple(sorted(sid for sid, item in cset.scenarios.items()
                              if item is not None and getattr(item, "active", True)))
        definitions = [item.to_dict() if hasattr(item, "to_dict") else _as_dict(item)
                       for _, item in sorted(cset.scenarios.items())]
    return active, _identity(definitions)


def _scope(cset: ConstraintSet, active: tuple[str, ...], scenario_identity: str,
           scenario_ids: Iterable[str], all_active: bool) -> ReviewScope:
    requested = tuple(sorted({str(item) for item in scenario_ids if item}))
    known = set(cset.scenarios) | set(active)
    unknown = tuple(sorted(set(requested) - known))
    if all_active and requested:
        # Preserve requested values, then make the inconsistency explicit as
        # unknown/incomplete scope rather than silently choosing one mode.
        unknown = tuple(sorted(set(unknown) | {"<all-active-with-selected-scenarios>"}))
    if all_active:
        kind, reviewed = "ALL_ACTIVE_SCENARIOS", active
    elif requested:
        kind, reviewed = "SELECTED_SCENARIOS", tuple(sorted(set(requested) & set(active)))
    else:
        kind, reviewed = "GLOBAL", active
    return ReviewScope(
        scope_kind=kind,
        requested_scenario_ids=requested,
        active_scenario_ids=active,
        reviewed_scenario_ids=reviewed,
        unknown_scenario_ids=unknown,
        scenario_definition_identity=scenario_identity,
    )


def _review_evidence_and_gates(cset: ConstraintSet, scope: ReviewScope, policy: ReviewPolicy,
                               lineage: ConstraintLineageReport | None, readiness: ConstraintReadinessReport | None,
                               validation: ValidationResult | None, formal_results: Iterable[Any]
                              ) -> tuple[tuple[ReviewEvidence, ...], tuple[ReviewFinding, ...], tuple[ReviewBlocker, ...]]:
    refs: dict[str, list[ReviewEvidenceRef]] = {}
    findings: list[ReviewFinding] = []
    blockers: list[ReviewBlocker] = []

    def evidence(category: str, reference_id: str, status: str, *, scenario_ids: Iterable[str] = (),
                 stale: bool = False, details: Mapping[str, Any] | None = None) -> ReviewEvidenceRef:
        ref = _evidence_ref(category, reference_id, status, scenario_ids, stale, details)
        refs.setdefault(category, []).append(ref)
        return ref

    def finding(severity: ReviewFindingSeverity, category: str, message: str,
                evidence_ids: Iterable[str] = (), scenario_id: str | None = None) -> None:
        payload = {"severity": severity.value, "category": category, "message": message,
                   "evidence_ref_ids": sorted(set(evidence_ids)), "scenario_id": scenario_id}
        item = ReviewFinding(id="RVF-" + stable_hash(payload)[:20], severity=severity, category=category,
                             message=message, evidence_ref_ids=tuple(sorted(set(evidence_ids))), scenario_id=scenario_id)
        findings.append(item)
        if severity == ReviewFindingSeverity.BLOCKER:
            blockers.append(ReviewBlocker(id="RVB-" + stable_hash(payload)[:20], category=category,
                                          message=message, evidence_ref_ids=item.evidence_ref_ids,
                                          scenario_id=scenario_id))

    ucm_ref = evidence("CANONICAL_UCM", stable_hash_cset(cset), "CURRENT",
                       scenario_ids=scope.reviewed_scenario_ids,
                       details={"constraint_ids": sorted(cset.constraints), "scenario_ids": sorted(cset.scenarios)})
    finding(ReviewFindingSeverity.INFORMATION, "CANONICAL_UCM",
            "Review references the supplied canonical UCM; it does not become a second UCM.", (ucm_ref.id,))

    if scope.unknown_scenario_ids:
        scope_ref = evidence("MCMM_SCOPE", scope.scenario_definition_identity, "INVALID",
                             scenario_ids=scope.requested_scenario_ids,
                             details={"unknown_scenario_ids": list(scope.unknown_scenario_ids)})
        finding(ReviewFindingSeverity.BLOCKER, "MCMM_SCOPE",
                "Requested review scope contains unknown or conflicting scenario selection.", (scope_ref.id,))
    else:
        scope_ref = evidence("MCMM_SCOPE", scope.scenario_definition_identity, "CURRENT",
                             scenario_ids=scope.reviewed_scenario_ids,
                             details={"scope_kind": scope.scope_kind, "active_scenario_ids": list(scope.active_scenario_ids)})
        if policy.require_all_active_scenarios and not set(scope.active_scenario_ids).issubset(scope.reviewed_scenario_ids):
            finding(ReviewFindingSeverity.BLOCKER, "MCMM_SCOPE",
                    "Recorded policy requires all active MCMM scenarios to be reviewed.", (scope_ref.id,))
        elif scope.scope_kind == "SELECTED_SCENARIOS":
            finding(ReviewFindingSeverity.INFORMATION, "MCMM_SCOPE",
                    "Review is explicitly limited to selected active scenarios.", (scope_ref.id,))

    if lineage is None:
        evidence("LINEAGE", "UNSUPPLIED", "UNKNOWN")
        finding(ReviewFindingSeverity.INFORMATION, "LINEAGE",
                "No Step-30 lineage report was supplied; lifecycle links are not reconstructed.")
    else:
        lineage_refs = [
            {"constraint_id": item.constraint_id, "candidate_id": item.candidate_id,
             "application_id": item.application_id,
             "event_ids": sorted(event.id for event in (*item.events, *item.validation_events, *item.formal_events)),
             "evidence_ids": sorted(evidence.id for evidence in item.evidence),
             "knowledge_reference_ids": sorted(str(reference.get("knowledge_item_id", ""))
                                               for reference in item.knowledge_references
                                               if reference.get("knowledge_item_id")),
             "assumption_ids": sorted(item.assumption_ids)}
            for item in lineage.constraints
        ]
        lineage_matches_ucm = lineage.snapshot.snapshot_identity == stable_hash_cset(cset)
        line_ref = evidence("LINEAGE", lineage.snapshot.snapshot_identity,
                            "CURRENT" if lineage_matches_ucm else "STALE",
                            scenario_ids=scope.reviewed_scenario_ids,
                            stale=(not lineage_matches_ucm) or any(event.stale for entry in lineage.constraints
                                                                    for event in (*entry.events, *entry.validation_events,
                                                                                  *entry.formal_events)),
                            details={"lineage_report_identity": _identity(lineage.to_dict()),
                                     "constraint_references": lineage_refs})
        if not lineage_matches_ucm:
            finding(ReviewFindingSeverity.BLOCKER, "LINEAGE",
                    "Supplied lineage snapshot does not match the canonical UCM being reviewed.", (line_ref.id,))
        elif line_ref.stale:
            finding(ReviewFindingSeverity.WARNING, "LINEAGE",
                    "Supplied lineage retains stale source evidence; it was not refreshed by review.", (line_ref.id,))

    _readiness_gates(cset, readiness, policy, scope, evidence, finding)
    _validation_and_coverage_gates(validation, policy, evidence, finding)
    _formal_gates(cset, formal_results, policy, evidence, finding)

    groups = tuple(ReviewEvidence(
        category=category,
        status=_group_status(items),
        references=tuple(sorted(items, key=lambda item: item.id)),
    ) for category, items in sorted(refs.items()))
    return groups, tuple(_sorted_findings(findings)), tuple(sorted(blockers, key=lambda item: item.id))


def _readiness_gates(cset: ConstraintSet, readiness: ConstraintReadinessReport | None, policy: ReviewPolicy,
                     scope: ReviewScope, evidence, finding) -> None:
    if readiness is None:
        ref = evidence("READINESS", "UNSUPPLIED", "MISSING")
        severity = ReviewFindingSeverity.BLOCKER if policy.require_readiness else ReviewFindingSeverity.INFORMATION
        finding(severity, "READINESS", "No existing Step-29 readiness report was supplied.", (ref.id,))
        return
    status = readiness.status.value
    ref = evidence("READINESS", _identity(readiness.to_dict()), status,
                   scenario_ids=scope.reviewed_scenario_ids,
                   stale=bool(readiness.stale_evidence),
                   details={"readiness_status": status,
                            "blocker_ids": sorted(item.id for item in readiness.blockers),
                            "warning_ids": sorted(item.id for item in readiness.warnings),
                            "scenario_result_ids": sorted(item.scenario_id for item in readiness.scenario_results)})
    if status == "READY" and not readiness.blockers:
        finding(ReviewFindingSeverity.INFORMATION, "READINESS", "Supplied readiness report is READY.", (ref.id,))
    elif status == "READY_WITH_WARNINGS":
        if policy.require_readiness_ready and not policy.allow_approval_with_warnings:
            finding(ReviewFindingSeverity.BLOCKER, "READINESS",
                    "Readiness has warnings and the recorded policy does not permit approval with warnings.", (ref.id,))
        else:
            finding(ReviewFindingSeverity.WARNING, "READINESS",
                    "Supplied readiness report is READY_WITH_WARNINGS; explicit acknowledgement is required.", (ref.id,))
    else:
        severity = ReviewFindingSeverity.BLOCKER if policy.require_readiness else ReviewFindingSeverity.WARNING
        finding(severity, "READINESS", f"Supplied readiness state {status} does not establish clean review readiness.",
                (ref.id,))
    readiness_snapshot = getattr(readiness, "source_snapshot_identity", {}).get("constraint_set")
    if readiness_snapshot and readiness_snapshot != stable_hash_cset(cset):
        finding(ReviewFindingSeverity.BLOCKER, "READINESS",
                "Supplied readiness report refers to a different canonical UCM snapshot.", (ref.id,))
    if readiness.blockers:
        finding(ReviewFindingSeverity.BLOCKER, "READINESS",
                "Supplied readiness report contains unresolved blockers.", (ref.id,))
    if readiness.stale_evidence:
        finding(ReviewFindingSeverity.WARNING, "READINESS",
                "Supplied readiness report retains stale evidence.", (ref.id,))


def _validation_and_coverage_gates(validation: ValidationResult | None, policy: ReviewPolicy, evidence, finding) -> None:
    if validation is None:
        ref = evidence("VALIDATION", "UNSUPPLIED", "MISSING")
        severity = ReviewFindingSeverity.BLOCKER if policy.require_validation_evidence else ReviewFindingSeverity.INFORMATION
        finding(severity, "VALIDATION", "No existing validation result was supplied.", (ref.id,))
        if policy.require_coverage_complete:
            coverage_ref = evidence("COVERAGE", "UNSUPPLIED", "MISSING")
            finding(ReviewFindingSeverity.BLOCKER, "COVERAGE", "No existing coverage result was supplied.", (coverage_ref.id,))
        return
    status = str(validation.status or "UNKNOWN")
    val_ref = evidence("VALIDATION", _identity(validation.as_dict()), status,
                       details={"issue_ids": _validation_issue_ids(validation), "status": status})
    normalized = status.upper()
    if normalized in {"PASS", "READY", "VALID", "COMPLETE"}:
        finding(ReviewFindingSeverity.INFORMATION, "VALIDATION", "Supplied validation result is complete without reported warnings.",
                (val_ref.id,))
    elif normalized in {"PASS_WITH_WARNINGS", "READY_WITH_WARNINGS", "WARNING", "WARNINGS"}:
        finding(ReviewFindingSeverity.WARNING, "VALIDATION",
                "Supplied validation result contains warnings requiring explicit review acknowledgement.", (val_ref.id,))
    elif "UNKNOWN" in normalized or "UNSUPPORTED" in normalized or "INCOMPLETE" in normalized:
        _unresolved(policy, "VALIDATION", f"Supplied validation state is {status}.", val_ref.id, finding)
    else:
        finding(ReviewFindingSeverity.BLOCKER, "VALIDATION",
                f"Supplied validation state {status} does not permit approval.", (val_ref.id,))
    for issue in getattr(validation.report, "issues", ()):
        issue_id = str(getattr(issue, "issue_id", "") or _identity(_as_dict(issue)))
        severity_text = _value(getattr(issue, "severity", ""))
        blocking = bool(getattr(issue, "blocking", False)) or severity_text in {"CRITICAL", "ERROR", "HIGH"}
        issue_ref = evidence("VALIDATION_ISSUE", issue_id, severity_text or "UNKNOWN",
                             details={"issue": _as_dict(issue)})
        if blocking:
            finding(ReviewFindingSeverity.BLOCKER, "VALIDATION",
                    f"Validation reports blocking issue {issue_id}.", (val_ref.id, issue_ref.id))
        elif severity_text in {"WARNING", "MEDIUM"}:
            finding(ReviewFindingSeverity.WARNING, "VALIDATION",
                    f"Validation reports warning issue {issue_id}.", (val_ref.id, issue_ref.id))

    coverage = getattr(validation, "coverage", None)
    if coverage is None:
        ref = evidence("COVERAGE", "UNSUPPLIED", "MISSING")
        severity = ReviewFindingSeverity.BLOCKER if policy.require_coverage_complete else ReviewFindingSeverity.INFORMATION
        finding(severity, "COVERAGE", "Validation result has no coverage evidence.", (ref.id,))
        return
    coverage_data = coverage.as_dict() if hasattr(coverage, "as_dict") else _as_dict(coverage)
    complete, reason = _coverage_complete(coverage_data)
    coverage_ref = evidence("COVERAGE", _identity(coverage_data), "COMPLETE" if complete else "INCOMPLETE",
                            details={"coverage": coverage_data})
    if complete:
        finding(ReviewFindingSeverity.INFORMATION, "COVERAGE", "Supplied coverage evidence is complete for its known scope.",
                (coverage_ref.id,))
    else:
        severity = ReviewFindingSeverity.BLOCKER if policy.require_coverage_complete else ReviewFindingSeverity.WARNING
        finding(severity, "COVERAGE", f"Coverage is incomplete or unknown: {reason}", (coverage_ref.id,))


def _formal_gates(cset: ConstraintSet, formal_results: Iterable[Any], policy: ReviewPolicy, evidence, finding) -> None:
    results: dict[str, str] = {}
    for result in formal_results:
        data = _as_dict(getattr(result, "verification", result))
        constraint_id = str(data.get("constraint_id") or getattr(result, "constraint_id", "") or "")
        status = _value(data.get("status")) or "UNKNOWN"
        reference = constraint_id or _identity(data)
        ref = evidence("FORMAL", reference, status, details={"formal_result": data})
        if constraint_id:
            results[constraint_id] = status
        if status.upper() in {"UNVERIFIED", "UNRESOLVED", "UNKNOWN", "UNSUPPORTED"}:
            finding(ReviewFindingSeverity.WARNING, "FORMAL",
                    f"Supplied formal evidence for {reference} remains {status}.", (ref.id,))
        elif status.upper() in {"FAILED", "INVALID", "ERROR"}:
            finding(ReviewFindingSeverity.BLOCKER, "FORMAL",
                    f"Supplied formal evidence for {reference} reports {status}.", (ref.id,))
    required_types = set(policy.require_formal_for_constraint_types)
    if not required_types:
        return
    for constraint in cset:
        ctype = _value(getattr(constraint, "type", ""))
        if ctype not in required_types:
            continue
        status = results.get(constraint.id, "MISSING")
        if status.upper() not in {"VERIFIED", "PASS", "PROVEN"}:
            ref = evidence("FORMAL", constraint.id, status,
                           details={"required_constraint_type": ctype})
            _unresolved(policy, "FORMAL",
                        f"Recorded policy requires verified formal evidence for {constraint.id} ({ctype}); status is {status}.",
                        ref.id, finding)


def _coverage_complete(coverage: Mapping[str, Any]) -> tuple[bool, str]:
    if not coverage.get("graph_available", False):
        return False, "coverage graph is unavailable"
    if coverage.get("uncovered"):
        return False, "uncovered paths or objects are retained"
    metrics = [value for key, value in coverage.items() if key.endswith("_coverage_pct")]
    if any(value == "UNKNOWN" or value is None for value in metrics):
        return False, "one or more coverage metrics are unknown"
    if any(isinstance(value, (int, float)) and value < 100 for value in metrics):
        return False, "one or more coverage metrics are below 100 percent"
    return True, "complete"


def _unresolved(policy: ReviewPolicy, category: str, message: str, evidence_id: str, finding) -> None:
    severity = (ReviewFindingSeverity.WARNING if category in set(policy.allowed_unresolved_evidence_categories)
                else ReviewFindingSeverity.BLOCKER)
    finding(severity, category, message, (evidence_id,))


def _staleness_reasons(review: ConstraintReview, current: ReviewSnapshot, current_scope: ReviewScope,
                       lineage: ConstraintLineageReport | None, readiness: ConstraintReadinessReport | None) -> tuple[str, ...]:
    reviewed = review.reviewed_snapshot
    reasons: list[str] = []
    if reviewed.ucm_content_identity != current.ucm_content_identity:
        reasons.append("Canonical UCM content identity differs from the reviewed snapshot.")
    if reviewed.ucm_semantic_identity != current.ucm_semantic_identity:
        reasons.append("Canonical UCM semantic identity differs from the reviewed snapshot or is unsupported.")
    if reviewed.scenario_ids != current.scenario_ids:
        reasons.append("Canonical UCM scenario identifiers differ from the reviewed snapshot.")
    if review.scope.scenario_definition_identity != current_scope.scenario_definition_identity:
        reasons.append("MCMM scenario definitions differ from the reviewed scope.")
    for label, old, new in (
        ("configuration", reviewed.configuration_identity, current.configuration_identity),
        ("design", reviewed.design_identity, current.design_identity),
        ("timing graph", reviewed.timing_graph_identity, current.timing_graph_identity),
    ):
        if old and new and old != new:
            reasons.append(f"Supplied {label} identity differs from review-time evidence.")
    if reviewed.lineage_report_identity and current.lineage_report_identity and \
            reviewed.lineage_report_identity != current.lineage_report_identity:
        reasons.append("Supplied lineage evidence identity differs from review-time evidence.")
    if reviewed.readiness_evidence_identity and current.readiness_evidence_identity and \
            reviewed.readiness_evidence_identity != current.readiness_evidence_identity:
        reasons.append("Supplied readiness evidence identity differs from review-time evidence.")
    if lineage is not None and lineage.snapshot.snapshot_identity != current.ucm_content_identity:
        reasons.append("Supplied lineage snapshot does not match the current canonical UCM identity.")
    readiness_identity = getattr(readiness, "source_snapshot_identity", {}).get("constraint_set") if readiness else ""
    if readiness_identity and readiness_identity != current.ucm_content_identity:
        reasons.append("Supplied readiness evidence refers to a different canonical UCM snapshot.")
    return tuple(sorted(set(reasons)))


def _review_root_id(snapshot: ReviewSnapshot, scope: ReviewScope, policy: ReviewPolicy,
                    evidence: Iterable[ReviewEvidence], supersedes_review_id: str | None) -> str:
    return "REV-" + stable_hash({
        "snapshot": snapshot.to_dict(), "scope": scope.to_dict(), "policy": policy.to_dict(),
        "evidence": [item.to_dict() for item in sorted(evidence, key=lambda item: item.category)],
        "supersedes_review_id": supersedes_review_id,
    })[:20]


def _approval(review_root_id: str, kind: ReviewDecisionKind, actor: ReviewActor, comment: str,
              evidence_ids: tuple[str, ...], recorded_at: str | None) -> ReviewApproval:
    # Human-entered actor/comment are deliberately identity-bearing decision
    # content. ``recorded_at`` is retained only as display metadata.
    identity = {
        "review_root_id": review_root_id, "kind": kind.value, "actor": actor.to_dict(),
        "comment": comment, "evidence_ref_ids": list(evidence_ids),
    }
    return ReviewApproval(
        id="DEC-" + stable_hash(identity)[:20], kind=kind, actor=actor, comment=comment,
        review_id=review_root_id, evidence_ref_ids=evidence_ids, recorded_at=recorded_at,
    )


def _decision_review_id(review: ConstraintReview, approval: ReviewApproval) -> str:
    return "REV-" + stable_hash({
        "review_root_id": review.review_root_id, "previous_review_id": review.id,
        "decision_id": approval.id, "decision_history": [item.id for item in review.decision_history] + [approval.id],
    })[:20]


def _evidence_ref(authority: str, reference_id: str, status: str, scenario_ids: Iterable[str], stale: bool,
                  details: Mapping[str, Any] | None) -> ReviewEvidenceRef:
    payload = {
        "authority": authority, "reference_id": reference_id, "status": status,
        "scenario_ids": sorted({str(item) for item in scenario_ids if item}), "stale": stale,
        "details": _identity_value(details or {}),
    }
    return ReviewEvidenceRef(id="RVE-" + stable_hash(payload)[:20], authority=authority,
                             reference_id=reference_id, status=status,
                             scenario_ids=tuple(payload["scenario_ids"]), stale=stale,
                             details=dict(details or {}))


def _initial_review_status(scope: ReviewScope, evidence: Iterable[ReviewEvidence],
                           policy: ReviewPolicy) -> ConstraintReviewStatus:
    """Expose blocking evidence state without mistaking it for a decision."""
    if scope.unknown_scenario_ids:
        return ConstraintReviewStatus.INVALID
    required = set()
    if policy.require_readiness:
        required.add("READINESS")
    if policy.require_validation_evidence:
        required.add("VALIDATION")
    if policy.require_coverage_complete:
        required.add("COVERAGE")
    rank = {"BLOCKED": ConstraintReviewStatus.BLOCKED,
            "UNSUPPORTED": ConstraintReviewStatus.UNSUPPORTED,
            "UNKNOWN": ConstraintReviewStatus.UNKNOWN,
            "INCOMPLETE": ConstraintReviewStatus.INCOMPLETE}
    for status, review_status in rank.items():
        if any(group.category in required and any(ref.status == status for ref in group.references)
               for group in evidence):
            return review_status
    return ConstraintReviewStatus.NEEDS_REVIEW


def _group_status(refs: Iterable[ReviewEvidenceRef]) -> str:
    statuses = {item.status for item in refs}
    if any(value in {"MISSING", "UNKNOWN", "INCOMPLETE", "INVALID", "UNSUPPORTED"} for value in statuses):
        return "INCOMPLETE"
    if any(item.stale for item in refs):
        return "STALE"
    return min(statuses) if statuses else "UNKNOWN"


def _assumption_ids(cset: ConstraintSet) -> tuple[str, ...]:
    values: list[str] = []
    for constraint in cset:
        values.extend(str(item) for item in getattr(constraint, "assumption_ids", ()) if item)
        values.extend(str(item) for item in getattr(getattr(constraint, "provenance", None), "assumption_ids", ()) if item)
    return tuple(sorted(set(values)))


def _validation_issue_ids(validation: ValidationResult) -> list[str]:
    return sorted(str(getattr(item, "issue_id", "") or _identity(_as_dict(item)))
                  for item in getattr(validation.report, "issues", ()))


def _evidence_ids(evidence: Iterable[ReviewEvidence]) -> tuple[str, ...]:
    return tuple(sorted(ref.id for group in evidence for ref in group.references))


def _decision_failure(decision: str, assessment: ConstraintReviewAssessment) -> str:
    reasons = [item.message for item in assessment.blockers]
    if not reasons and decision == "APPROVE":
        reasons = ["Warnings require APPROVE_WITH_WARNINGS under an explicit policy."]
    if not reasons and decision == "APPROVE_WITH_WARNINGS":
        reasons = ["Recorded policy does not allow approval with warnings."]
    return f"Cannot {decision}: " + " ".join(reasons)


def _identity(value: Any) -> str:
    return stable_hash(_identity_value(value))


def _identity_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _identity_value(value[key]) for key in sorted(value, key=str)
                if str(key) not in _VOLATILE_ID_KEYS}
    if isinstance(value, (list, tuple)):
        return [_identity_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_identity_value(item) for item in value), key=repr)
    enum_value = getattr(value, "value", None)
    if enum_value is not None:
        return _identity_value(enum_value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return _identity_value(_as_dict(value))


def _as_dict(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, Mapping):
        return dict(value)
    return str(value)


def _value(value: Any) -> str:
    return str(value.value) if hasattr(value, "value") else str(value or "")


def _sorted_findings(findings: Iterable[ReviewFinding]) -> list[ReviewFinding]:
    order = {ReviewFindingSeverity.BLOCKER: 0, ReviewFindingSeverity.WARNING: 1,
             ReviewFindingSeverity.INFORMATION: 2}
    return sorted(findings, key=lambda item: (order[item.severity], item.category, item.scenario_id or "", item.id))


__all__ = [
    "ConstraintReviewEngine",
    "ReviewDecisionError",
    "approve_review",
    "approve_review_with_warnings",
    "assess_constraint_review",
    "create_constraint_review",
    "decide_review",
    "defer_review",
    "reject_review",
    "revoke_review",
    "supersede_review",
]
