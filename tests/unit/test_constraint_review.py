"""Focused Step-31 governance-review contracts over existing evidence only."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from rca.constraint_model import Constraint, ConstraintSet
from rca.constraint_model.scenarios import Scenario
from rca.exceptions.formal_backend import VerificationResult
from rca.explanation import explain_constraint_review
from rca.lineage import build_constraint_lineage
from rca.readiness import (
    ConstraintReadinessReport,
    ReadinessBlocker,
    ReadinessRequirement,
    ReadinessStatus,
)
from rca.review import (
    ConstraintReview,
    ConstraintReviewEngine,
    ConstraintReviewStatus,
    ReviewActor,
    ReviewDecisionError,
    ReviewDecisionKind,
    ReviewPolicy,
    approve_review,
    approve_review_with_warnings,
    assess_constraint_review,
    create_constraint_review,
    defer_review,
    reject_review,
    revoke_review,
    supersede_review,
)
from rca.utils.enums import (
    ConstraintType,
    ErrorCode,
    Severity,
    SourceKind,
    ValidationCategory,
    VerificationStatus,
)
from rca.validation.base import ValidationIssue, ValidationReport
from rca.validation.coverage import CoverageReport
from rca.validation.engine import ValidationResult

_EPOCH = "2000-01-01T00:00:00+00:00"


def _ucm(*, scenarios: tuple[str, ...] = ()) -> ConstraintSet:
    cset = ConstraintSet(name="review", created_at=_EPOCH)
    cset.create_clock("clk", 10e-9, source="clk", source_kind=SourceKind.USER)
    for scenario in scenarios:
        cset.add_scenario(Scenario(id=scenario, mode="functional", corner=scenario.lower()))
    return cset


def _validation(*issues: ValidationIssue, status: str = "PASS", coverage: CoverageReport | None = None):
    return ValidationResult(
        status=status,
        report=ValidationReport(issues=list(issues)),
        coverage=coverage if coverage is not None else CoverageReport(graph_available=True),
    )


def _readiness(status: ReadinessStatus = ReadinessStatus.READY, *, blockers: bool = False):
    blocker_items = ()
    if blockers:
        blocker_items = (ReadinessBlocker(
            id="RB-1", requirement_id="VALIDATION", category="validation", status=ReadinessStatus.BLOCKED,
            message="Existing readiness blocker.",
        ),)
    return ConstraintReadinessReport(
        status=status,
        requirements=(ReadinessRequirement(
            id="VALIDATION", category="validation", title="Validation", status=status,
            required=True, rationale="Existing readiness result.",
        ),),
        blockers=blocker_items,
    )


def _assessment(cset: ConstraintSet | None = None, *, policy: ReviewPolicy | None = None,
                readiness: ConstraintReadinessReport | None = None,
                validation: ValidationResult | None = None, **kwargs):
    return assess_constraint_review(
        cset or _ucm(), policy=policy or ReviewPolicy(), readiness=readiness or _readiness(),
        validation=validation or _validation(), **kwargs,
    )


def test_assessment_never_implicitly_approves_a_clean_snapshot():
    assessment = _assessment()
    assert assessment.review.status == ConstraintReviewStatus.NEEDS_REVIEW
    assert assessment.current_status == ConstraintReviewStatus.NEEDS_REVIEW
    assert assessment.approval_possible is True
    assert assessment.review.approval is None


def test_typed_models_are_frozen_and_unknown_reviewer_is_explicit():
    assessment = _assessment()
    with pytest.raises(FrozenInstanceError):
        assessment.review.status = ConstraintReviewStatus.APPROVED
    assert ReviewActor.from_value(None).identity == "UNSPECIFIED"


def test_repeated_clean_assessments_have_deterministic_ids_and_json():
    cset = _ucm()
    first = _assessment(cset)
    second = _assessment(cset)
    assert first.review.id == second.review.id
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(second.to_dict(), sort_keys=True)


def test_explicit_approve_creates_successor_without_mutating_ucm():
    cset = _ucm(); original = cset.to_canonical_json()
    review = _assessment(cset).review
    approved = approve_review(review, cset, actor=ReviewActor.from_value("alice"), comment="reviewed")
    assert approved.status == ConstraintReviewStatus.APPROVED
    assert approved.approval.kind == ReviewDecisionKind.APPROVE
    assert approved.approval.actor.identity == "alice"
    assert approved.previous_review_id == review.id
    assert review.status == ConstraintReviewStatus.NEEDS_REVIEW
    assert cset.to_canonical_json() == original


def test_explicit_approve_with_warnings_requires_explicit_policy():
    cset = _ucm(); readiness = _readiness(ReadinessStatus.READY_WITH_WARNINGS)
    strict = _assessment(cset, readiness=readiness).review
    with pytest.raises(ReviewDecisionError):
        approve_review_with_warnings(strict, cset, actor=ReviewActor.from_value("alice"), readiness=readiness,
                                     validation=_validation())
    permissive = _assessment(cset, readiness=readiness,
                             policy=ReviewPolicy(allow_approval_with_warnings=True)).review
    approved = approve_review_with_warnings(
        permissive, cset, actor=ReviewActor.from_value("alice"), readiness=readiness, validation=_validation(),
    )
    assert approved.status == ConstraintReviewStatus.APPROVED_WITH_WARNINGS


def test_warning_requires_approve_with_warnings_not_clean_approve():
    cset = _ucm(); validation = _validation(status="PASS_WITH_WARNINGS")
    policy = ReviewPolicy(allow_approval_with_warnings=True)
    review = _assessment(cset, policy=policy, validation=validation).review
    with pytest.raises(ReviewDecisionError):
        approve_review(review, cset, validation=validation, readiness=_readiness())
    approved = approve_review_with_warnings(review, cset, validation=validation, readiness=_readiness())
    assert approved.status == ConstraintReviewStatus.APPROVED_WITH_WARNINGS


@pytest.mark.parametrize("status", [
    ReadinessStatus.BLOCKED, ReadinessStatus.INCOMPLETE, ReadinessStatus.UNKNOWN, ReadinessStatus.UNSUPPORTED,
])
def test_nonready_readiness_states_fail_closed(status):
    assessment = _assessment(readiness=_readiness(status))
    assert assessment.approval_possible is False
    assert assessment.blockers


def test_unknown_required_evidence_is_explicitly_unknown_not_approval():
    assessment = _assessment(readiness=_readiness(ReadinessStatus.UNKNOWN))
    assert assessment.review.status == ConstraintReviewStatus.UNKNOWN
    assert assessment.current_status == ConstraintReviewStatus.UNKNOWN
    assert not assessment.approval_possible


def test_readiness_blocker_fails_closed_even_when_status_is_ready():
    assessment = _assessment(readiness=_readiness(blockers=True))
    assert any(item.category == "READINESS" for item in assessment.blockers)


def test_validation_warning_is_distinct_from_blocking_validation_issue():
    warning = ValidationIssue(
        severity=Severity.WARNING, category=ValidationCategory.TIMING, code=ErrorCode.VALIDATION_ERROR,
        message="Existing warning", blocking=False,
    )
    warning_assessment = _assessment(validation=_validation(warning, status="PASS_WITH_WARNINGS"),
                                     policy=ReviewPolicy(allow_approval_with_warnings=True))
    assert warning_assessment.findings and not warning_assessment.blockers
    blocker = ValidationIssue(
        severity=Severity.ERROR, category=ValidationCategory.TIMING, code=ErrorCode.VALIDATION_ERROR,
        message="Existing error", blocking=True,
    )
    blocked = _assessment(validation=_validation(blocker, status="FAIL"))
    assert blocked.blockers


def test_missing_validation_and_coverage_evidence_are_not_pass():
    assessment = assess_constraint_review(_ucm(), readiness=_readiness())
    categories = {item.category for item in assessment.blockers}
    assert {"VALIDATION", "COVERAGE"}.issubset(categories)


def test_coverage_incomplete_is_a_policy_blocker():
    coverage = CoverageReport(graph_available=True, uncovered=[{"path": "p"}])
    assessment = _assessment(validation=_validation(coverage=coverage))
    assert any(item.category == "COVERAGE" for item in assessment.blockers)


def test_coverage_requirement_can_be_explicitly_relaxed_without_hiding_warning():
    coverage = CoverageReport(graph_available=False)
    policy = ReviewPolicy(require_coverage_complete=False)
    assessment = _assessment(policy=policy, validation=_validation(coverage=coverage))
    assert not any(item.category == "COVERAGE" for item in assessment.blockers)
    assert any(item.category == "COVERAGE" and item.severity.value == "WARNING" for item in assessment.findings)


def test_formal_unverified_is_evidence_not_approval_or_proof():
    cset = _ucm(); cid = cset.clocks()[0].id
    formal = VerificationResult(constraint_id=cid, status=VerificationStatus.UNRESOLVED, tool="mock")
    assessment = _assessment(cset, formal_results=(formal,))
    assert any(item.category == "FORMAL" and item.severity.value == "WARNING" for item in assessment.findings)
    assert assessment.review.external_eda_signoff.value == "EXTERNAL_EDA_SIGNOFF_UNKNOWN"


def test_policy_can_require_actual_supplied_formal_verification():
    cset = _ucm(); cid = cset.clocks()[0].id
    policy = ReviewPolicy(require_formal_for_constraint_types=(ConstraintType.CREATE_CLOCK.value,))
    blocked = _assessment(cset, policy=policy)
    assert any(item.category == "FORMAL" for item in blocked.blockers)
    verified = VerificationResult(constraint_id=cid, status=VerificationStatus.VERIFIED, tool="mock")
    complete = _assessment(cset, policy=policy, formal_results=(verified,))
    assert not any(item.category == "FORMAL" for item in complete.blockers)


def test_reject_and_defer_are_explicit_governance_decisions():
    cset = _ucm(); review = _assessment(cset).review
    rejected = reject_review(review, cset, actor=ReviewActor.from_value("alice"), comment="not ready")
    deferred = defer_review(review, cset, actor=ReviewActor.from_value("alice"), comment="later")
    assert rejected.status == ConstraintReviewStatus.REJECTED
    assert deferred.status == ConstraintReviewStatus.DEFERRED
    assert rejected.approval.kind == ReviewDecisionKind.REJECT
    assert deferred.approval.kind == ReviewDecisionKind.DEFER


def test_stale_approved_review_is_reported_stale_and_cannot_be_approved_again():
    cset = _ucm(); review = _assessment(cset).review
    approved = approve_review(review, cset, actor=ReviewActor.from_value("alice"))
    changed = cset.clone(); changed.get(cset.clocks()[0].id).values["period"] = 8e-9
    stale = assess_constraint_review(changed, review=approved, readiness=_readiness(), validation=_validation())
    assert stale.current_status == ConstraintReviewStatus.STALE
    assert not stale.snapshot_current
    with pytest.raises(ReviewDecisionError):
        approve_review(approved, changed, readiness=_readiness(), validation=_validation())


def test_configuration_design_and_timing_identity_changes_make_review_stale_when_supplied():
    cset = _ucm(); review = _assessment(cset, config={"v": 1}).review
    stale = assess_constraint_review(cset, review=review, config={"v": 2}, readiness=_readiness(), validation=_validation())
    assert stale.current_status == ConstraintReviewStatus.STALE
    assert any("configuration" in reason.lower() for reason in stale.staleness_reasons)


def test_selected_scenario_scope_is_not_global_and_can_be_explicitly_allowed():
    cset = _ucm(scenarios=("FAST", "SLOW"))
    blocked = _assessment(cset, scenario_ids=("FAST",))
    assert blocked.review.scope.scope_kind == "SELECTED_SCENARIOS"
    assert blocked.review.scope.reviewed_scenario_ids == ("FAST",)
    assert any(item.category == "MCMM_SCOPE" for item in blocked.blockers)
    scoped = _assessment(cset, scenario_ids=("FAST",), policy=ReviewPolicy(require_all_active_scenarios=False))
    assert not any(item.category == "MCMM_SCOPE" for item in scoped.blockers)


def test_all_active_scenarios_scope_preserves_mcmm_identity():
    cset = _ucm(scenarios=("FAST", "SLOW"))
    assessment = _assessment(cset, all_active_scenarios=True)
    assert assessment.review.scope.scope_kind == "ALL_ACTIVE_SCENARIOS"
    assert assessment.review.scope.reviewed_scenario_ids == ("FAST", "SLOW")
    assert assessment.review.scope.scenario_definition_identity


def test_unknown_or_conflicting_scenario_scope_is_invalid():
    cset = _ucm(scenarios=("FAST",))
    unknown = _assessment(cset, scenario_ids=("NOPE",))
    assert unknown.current_status == ConstraintReviewStatus.INVALID
    assert unknown.review.scope.unknown_scenario_ids == ("NOPE",)
    conflicting = _assessment(cset, scenario_ids=("FAST",), all_active_scenarios=True)
    assert conflicting.current_status == ConstraintReviewStatus.INVALID


def test_lineage_references_are_projected_not_reconstructed():
    cset = _ucm(); lineage = build_constraint_lineage(cset, validation=_validation(), readiness=_readiness())
    assessment = _assessment(cset, lineage=lineage)
    group = next(item for item in assessment.evidence if item.category == "LINEAGE")
    assert group.references[0].reference_id == lineage.snapshot.snapshot_identity
    assert assessment.review.reviewed_snapshot.lineage_report_identity


def test_mismatched_lineage_or_readiness_snapshot_fails_closed():
    cset = _ucm(); other = _ucm(); other.get(other.clocks()[0].id).values["period"] = 8e-9
    mismatched_lineage = build_constraint_lineage(other)
    lineage_assessment = _assessment(cset, lineage=mismatched_lineage)
    assert any(item.category == "LINEAGE" for item in lineage_assessment.blockers)
    mismatched_readiness = ConstraintReadinessReport(
        status=ReadinessStatus.READY,
        requirements=(), source_snapshot_identity={"constraint_set": "different"},
    )
    readiness_assessment = _assessment(cset, readiness=mismatched_readiness)
    assert any(item.category == "READINESS" for item in readiness_assessment.blockers)


def test_unknown_lineage_semantics_remain_unknown_not_equivalent():
    cset = _ucm()
    cset.add(Constraint(id="LOAD", type=ConstraintType.SET_LOAD, target_objects=["q"], values={"value": 2.0}))
    review = _assessment(cset).review
    assert review.reviewed_snapshot.ucm_semantic_identity == ""
    assert review.status == ConstraintReviewStatus.NEEDS_REVIEW


def test_revocation_creates_successor_and_preserves_prior_approval():
    cset = _ucm(); initial = _assessment(cset).review
    approved = approve_review(initial, cset, actor=ReviewActor.from_value("alice"))
    revoked = revoke_review(approved, cset, actor=ReviewActor.from_value("bob"), comment="withdrawn")
    assert approved.status == ConstraintReviewStatus.APPROVED
    assert revoked.status == ConstraintReviewStatus.REVOKED
    assert [item.kind for item in revoked.decision_history] == [ReviewDecisionKind.APPROVE, ReviewDecisionKind.REVOKE]
    assert revoked.previous_review_id == approved.id


def test_only_approved_review_can_be_revoked():
    with pytest.raises(ReviewDecisionError):
        revoke_review(_assessment().review, _ucm())


def test_supersession_creates_separate_unapproved_linked_review():
    cset = _ucm(); old = approve_review(_assessment(cset).review, cset, actor=ReviewActor.from_value("alice"))
    changed = cset.clone(); changed.get(cset.clocks()[0].id).values["period"] = 8e-9
    newer = supersede_review(old, changed, readiness=_readiness(), validation=_validation())
    assert newer.status == ConstraintReviewStatus.NEEDS_REVIEW
    assert newer.supersedes_review_id == old.id
    assert old.status == ConstraintReviewStatus.APPROVED


def test_decision_identity_includes_explicit_actor_and_comment_not_timestamp():
    cset = _ucm(); review = _assessment(cset).review
    first = approve_review(review, cset, actor=ReviewActor.from_value("alice"), comment="A", recorded_at="t1")
    same = approve_review(review, cset, actor=ReviewActor.from_value("alice"), comment="A", recorded_at="t2")
    changed = approve_review(review, cset, actor=ReviewActor.from_value("alice"), comment="B")
    assert first.approval.id == same.approval.id
    assert first.id == same.id
    assert first.approval.id != changed.approval.id


def test_from_dict_round_trip_is_deterministic_and_does_not_create_persistence():
    cset = _ucm(); approved = approve_review(_assessment(cset).review, cset, actor=ReviewActor.from_value("alice"))
    rebuilt = ConstraintReview.from_dict(approved.to_dict())
    assert rebuilt.to_dict() == approved.to_dict()


def test_assessment_engine_facade_is_stateless_and_matches_function():
    cset = _ucm(); kwargs = {"readiness": _readiness(), "validation": _validation()}
    first = ConstraintReviewEngine().assess(cset, **kwargs)
    second = assess_constraint_review(cset, **kwargs)
    assert first.to_dict() == second.to_dict()


def test_external_eda_evidence_is_only_referenced_never_inferred_as_signoff():
    assessment = _assessment(external_eda_evidence_refs=("runs/1/run_manifest.json",))
    assert assessment.review.external_eda_signoff.value == "EXTERNAL_EDA_SIGNOFF_EVIDENCE_SUPPLIED"
    assert assessment.review.external_eda_evidence_refs == ("runs/1/run_manifest.json",)


def test_review_explanation_keeps_governance_and_eda_signoff_distinct():
    explanation = explain_constraint_review(_assessment())
    assert "Explicit decision: none" in explanation
    assert "not external EDA signoff or timing proof" in explanation


def test_missing_reviewer_is_preserved_as_unspecified_in_explicit_decision():
    cset = _ucm(); approved = approve_review(_assessment(cset).review, cset)
    assert approved.approval.actor.identity == "UNSPECIFIED"


def test_invalid_manual_decision_kind_is_not_coerced():
    with pytest.raises(ValueError):
        ReviewDecisionKind("AUTO_APPROVE")


def test_malformed_review_record_fails_closed():
    with pytest.raises(ReviewDecisionError):
        assess_constraint_review(_ucm(), review=ConstraintReview.from_dict({}))


def test_review_does_not_call_application_or_mutate_provenance_metadata():
    cset = _ucm(); before = cset.to_canonical_json()
    create_constraint_review(cset, readiness=_readiness(), validation=_validation())
    assert cset.to_canonical_json() == before


def test_assessment_has_reference_ids_for_existing_validation_coverage_and_readiness():
    assessment = _assessment()
    categories = {group.category for group in assessment.evidence}
    assert {"CANONICAL_UCM", "COVERAGE", "READINESS", "VALIDATION"}.issubset(categories)
    assert all(ref.id.startswith("RVE-") for group in assessment.evidence for ref in group.references)


def test_review_scope_and_snapshot_capture_exact_constraint_and_scenario_ids():
    cset = _ucm(scenarios=("FAST",))
    review = _assessment(cset).review
    assert review.reviewed_snapshot.constraint_ids == tuple(sorted(cset.constraints))
    assert review.reviewed_snapshot.scenario_ids == ("FAST",)
    assert review.scope.reviewed_scenario_ids == ("FAST",)


def test_policy_allowed_unresolved_category_changes_only_that_explicit_gate():
    cset = _ucm(); policy = ReviewPolicy(allowed_unresolved_evidence_categories=("VALIDATION",),
                                         require_coverage_complete=False)
    assessment = _assessment(cset, policy=policy, validation=_validation(status="UNKNOWN"))
    assert not any(item.category == "VALIDATION" for item in assessment.blockers)
    assert any(item.category == "VALIDATION" and item.severity.value == "WARNING" for item in assessment.findings)


def test_lineage_readiness_and_review_do_not_claim_causality():
    cset = _ucm(); lineage = build_constraint_lineage(cset)
    review = _assessment(cset, lineage=lineage).review
    assert review.reviewed_snapshot.lineage_snapshot_identity == lineage.snapshot.snapshot_identity
    assert all("caus" not in item.message.lower() for item in review.findings)
