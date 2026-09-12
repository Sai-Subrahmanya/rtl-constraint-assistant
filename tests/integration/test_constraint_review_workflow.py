"""Step-31 end-to-end governance boundary over the existing Steps 27–30 chain."""

from __future__ import annotations

from rca.inference import ApplicationStatus
from rca.lineage import LineageSource, build_constraint_lineage
from rca.readiness import assess_constraint_readiness
from rca.review import (
    ConstraintReviewStatus,
    ReviewActor,
    assess_constraint_review,
    reject_review,
    supersede_review,
)
from rca.validation import validate as run_validation
from tests.unit.test_constraint_lineage import _accepted_context


def test_rtl_advice_application_ucm_readiness_lineage_and_explicit_review_workflow():
    # Existing controlled Step-28 flow is the only mutation in this sequence.
    design, timing, config, canonical, advisory, candidate, receipt = _accepted_context(knowledge=True)
    assert receipt.status == ApplicationStatus.APPLIED
    after_application = canonical.to_canonical_json()

    validation = run_validation(design, timing, canonical, backend="generic")
    readiness = assess_constraint_readiness(
        config, canonical, design, timing, validation=validation, inference_report=advisory,
    )
    lineage = build_constraint_lineage(
        canonical, config=config, design=design, timing_graph=timing,
        inference_report=advisory, application_receipts=(receipt,), validation=validation,
        readiness=readiness,
    )
    linked = lineage.constraints[0]
    assert linked.source == LineageSource.EXPLICITLY_ACCEPTED
    assert linked.candidate_id == candidate.id and linked.application_id == receipt.application_id

    # Step 31 only consumes the established UCM/readiness/lineage evidence.
    assessment = assess_constraint_review(
        canonical, config=config, design=design, timing_graph=timing,
        lineage=lineage, readiness=readiness, validation=validation,
    )
    assert assessment.review.status in {ConstraintReviewStatus.NEEDS_REVIEW, ConstraintReviewStatus.INCOMPLETE,
                                        ConstraintReviewStatus.BLOCKED}
    assert canonical.to_canonical_json() == after_application

    # The actual existing evidence is blocked, so a human records a governance
    # rejection. No review path applies advice, writes SDC, or executes EDA/formal.
    rejected = reject_review(
        assessment.review, canonical, actor=ReviewActor.from_value("reviewer"),
        comment="Existing readiness blockers remain unresolved.",
    )
    assert rejected.status == ConstraintReviewStatus.REJECTED
    assert canonical.to_canonical_json() == after_application

    changed = canonical.clone()
    changed.get(receipt.applied_constraint_ids[0]).values["period"] = 8e-9
    stale = assess_constraint_review(changed, review=rejected)
    assert stale.current_status == ConstraintReviewStatus.STALE
    assert stale.staleness_reasons

    replacement = supersede_review(rejected, changed, readiness=readiness, validation=validation)
    assert replacement.status in {ConstraintReviewStatus.NEEDS_REVIEW, ConstraintReviewStatus.INCOMPLETE,
                                  ConstraintReviewStatus.BLOCKED}
    assert replacement.supersedes_review_id == rejected.id
    assert rejected.status == ConstraintReviewStatus.REJECTED
