"""Golden compact projections covering Step-31 review policy/lifecycle boundaries."""

from __future__ import annotations

import json
from pathlib import Path

from rca.constraint_model import Constraint
from rca.exceptions.formal_backend import VerificationResult
from rca.lineage import build_constraint_lineage
from rca.readiness import ReadinessStatus
from rca.review import (
    ReviewActor,
    ReviewPolicy,
    approve_review,
    approve_review_with_warnings,
    assess_constraint_review,
    defer_review,
    reject_review,
    revoke_review,
    supersede_review,
)
from rca.utils.enums import ConstraintType, VerificationStatus
from rca.validation.coverage import CoverageReport
from tests.unit.test_constraint_lineage import _accepted_context
from tests.unit.test_constraint_review import _assessment, _readiness, _ucm, _validation

_FIXTURES = Path(__file__).with_name("review")


def _result(assessment):
    return {
        "record_status": assessment.review.status.value,
        "current_status": assessment.current_status.value,
        "approval_possible": assessment.approval_possible,
        "approval_with_warnings_possible": assessment.approval_with_warnings_possible,
        "blocker_categories": sorted({item.category for item in assessment.blockers}),
        "warning_categories": sorted({item.category for item in assessment.findings if item.severity.value == "WARNING"}),
        "scope": assessment.review.scope.to_dict(),
        "external_eda_signoff": assessment.review.external_eda_signoff.value,
    }


def _projections() -> dict:
    cset = _ucm()
    clean = _assessment(cset)
    warnings_policy = ReviewPolicy(allow_approval_with_warnings=True)
    ready_warnings = _assessment(cset, policy=warnings_policy,
                                 readiness=_readiness(ReadinessStatus.READY_WITH_WARNINGS))
    blocked = _assessment(cset, readiness=_readiness(ReadinessStatus.BLOCKED))
    incomplete = _assessment(cset, readiness=_readiness(ReadinessStatus.INCOMPLETE))
    unknown = _assessment(cset, readiness=_readiness(ReadinessStatus.UNKNOWN))
    unsupported = _assessment(cset, readiness=_readiness(ReadinessStatus.UNSUPPORTED))
    approved = approve_review(clean.review, cset, actor=ReviewActor.from_value("alice"), comment="clean")
    approved_warning = approve_review_with_warnings(
        ready_warnings.review, cset, actor=ReviewActor.from_value("alice"),
        readiness=_readiness(ReadinessStatus.READY_WITH_WARNINGS), validation=_validation(),
    )
    rejected = reject_review(clean.review, cset, actor=ReviewActor.from_value("alice"), comment="reject")
    deferred = defer_review(clean.review, cset, actor=ReviewActor.from_value("alice"), comment="defer")
    changed = cset.clone(); changed.get(cset.clocks()[0].id).values["period"] = 8e-9
    stale = assess_constraint_review(changed, review=approved)
    scoped = _assessment(_ucm(scenarios=("FAST", "SLOW")), scenario_ids=("FAST",),
                         policy=ReviewPolicy(require_all_active_scenarios=False))
    multi = _assessment(_ucm(scenarios=("FAST", "SLOW")), all_active_scenarios=True)
    unknown_scope = _assessment(_ucm(scenarios=("FAST",)), scenario_ids=("UNKNOWN",))
    design, timing, config, applied_ucm, advisory, _candidate, receipt = _accepted_context(knowledge=True)
    lineage = build_constraint_lineage(applied_ucm, config=config, design=design, timing_graph=timing,
                                       inference_report=advisory, application_receipts=(receipt,))
    chained = _assessment(applied_ucm, lineage=lineage)
    unsupported_ucm = _ucm()
    unsupported_ucm.add(Constraint(id="LOAD", type=ConstraintType.SET_LOAD, target_objects=["q"], values={"value": 2.0}))
    unsupported_lineage = _assessment(unsupported_ucm)
    formal_unverified = _assessment(cset, formal_results=(VerificationResult(
        constraint_id=cset.clocks()[0].id, status=VerificationStatus.UNRESOLVED, tool="mock"),))
    formal_policy = ReviewPolicy(require_formal_for_constraint_types=(ConstraintType.CREATE_CLOCK.value,))
    formal_verified = _assessment(cset, policy=formal_policy, formal_results=(VerificationResult(
        constraint_id=cset.clocks()[0].id, status=VerificationStatus.VERIFIED, tool="mock"),))
    coverage_incomplete = _assessment(cset, validation=_validation(coverage=CoverageReport(
        graph_available=True, uncovered=[{"path": "p"}],
    )))
    revoked = revoke_review(approved, cset, actor=ReviewActor.from_value("bob"), comment="revoke")
    superseding = supersede_review(approved, changed, readiness=_readiness(), validation=_validation())
    return {
        "clean_READY_review": _result(clean),
        "READY_WITH_WARNINGS": _result(ready_warnings),
        "BLOCKED": _result(blocked),
        "INCOMPLETE": _result(incomplete),
        "UNKNOWN": _result(unknown),
        "UNSUPPORTED": _result(unsupported),
        "explicit_APPROVE": {"status": approved.status.value, "decision": approved.approval.kind.value},
        "explicit_APPROVE_WITH_WARNINGS": {"status": approved_warning.status.value, "decision": approved_warning.approval.kind.value},
        "explicit_REJECT": {"status": rejected.status.value, "decision": rejected.approval.kind.value},
        "explicit_DEFER": {"status": deferred.status.value, "decision": deferred.approval.kind.value},
        "stale_approved_review": {"current_status": stale.current_status.value, "reasons": list(stale.staleness_reasons)},
        "scenario_specific_review": _result(scoped),
        "multi_scenario_review": _result(multi),
        "unknown_scenario": _result(unknown_scope),
        "inference_application_ucm_lineage_review": {
            "source": lineage.constraints[0].source.value,
            "candidate_id_present": bool(chained.review.reviewed_snapshot.lineage_report_identity),
            "lineage_reference": chained.review.reviewed_snapshot.lineage_snapshot_identity == lineage.snapshot.snapshot_identity,
        },
        "unsupported_unknown_lineage": {"semantic_identity": unsupported_lineage.review.reviewed_snapshot.ucm_semantic_identity or "UNKNOWN"},
        "formal_UNVERIFIED": _result(formal_unverified),
        "supplied_formal_VERIFIED": _result(formal_verified),
        "validation_warning_vs_blocker": {
            "warning_status": _result(_assessment(cset, validation=_validation(status="PASS_WITH_WARNINGS")))["approval_possible"],
            "blocker_status": _result(_assessment(cset, validation=_validation(status="FAIL")))["approval_possible"],
        },
        "coverage_incomplete": _result(coverage_incomplete),
        "review_policy_gating": {
            "strict_allow_warnings": ReviewPolicy().allow_approval_with_warnings,
            "explicit_allow_warnings": warnings_policy.allow_approval_with_warnings,
        },
        "review_invalidation_revocation": {"status": revoked.status.value, "history": [item.kind.value for item in revoked.decision_history]},
        "superseding_review": {"status": superseding.status.value, "supersedes": superseding.supersedes_review_id == approved.id},
        "deterministic_repeated_output": _assessment(cset).to_dict() == _assessment(cset).to_dict(),
    }


def test_golden_review_policy_and_lifecycle_projections():
    actual = _projections()
    expected = json.loads((_FIXTURES / "deterministic_review.json").read_text(encoding="utf-8"))
    assert actual == expected
    assert _projections()["deterministic_repeated_output"] is True
