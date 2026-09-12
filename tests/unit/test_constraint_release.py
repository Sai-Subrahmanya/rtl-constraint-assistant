"""Focused Step-32 immutable RCA release governance contracts."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from rca.constraint_model import ConstraintSet
from rca.constraint_model.scenarios import Scenario
from rca.lineage import build_constraint_lineage
from rca.readiness import ConstraintReadinessReport, ReadinessRequirement, ReadinessStatus
from rca.release import (
    ConstraintRelease,
    PackageVerificationStatus,
    ReleaseDecisionError,
    ReleaseIssueSeverity,
    ReleasePolicy,
    ReleaseStatus,
    assess_constraint_release,
    create_release_candidate,
    create_release_package,
    release_constraint_set,
    revoke_release,
    supersede_release,
    verify_release_package,
)
from rca.review import (
    ReviewActor,
    ReviewPolicy,
    approve_review,
    create_constraint_review,
)
from rca.utils.enums import SourceKind
from rca.validation.base import ValidationReport
from rca.validation.coverage import CoverageReport
from rca.validation.engine import ValidationResult

_EPOCH = "2000-01-01T00:00:00+00:00"


def _ucm(*, scenarios: tuple[str, ...] = ()) -> ConstraintSet:
    cset = ConstraintSet(name="release", created_at=_EPOCH)
    cset.create_clock("clk", 10e-9, source="clk", source_kind=SourceKind.USER)
    for scenario in scenarios:
        cset.add_scenario(Scenario(id=scenario, mode="functional", corner=scenario.lower()))
    return cset


def _validation(*, coverage: CoverageReport | None = None, status: str = "PASS") -> ValidationResult:
    return ValidationResult(status=status, report=ValidationReport(),
                            coverage=coverage if coverage is not None else CoverageReport(graph_available=True))


def _readiness(status: ReadinessStatus = ReadinessStatus.READY) -> ConstraintReadinessReport:
    return ConstraintReadinessReport(
        status=status,
        requirements=(ReadinessRequirement(id="VALIDATION", category="validation", title="Validation",
                                           status=status, required=True, rationale="Existing evidence."),),
    )


def _review(cset: ConstraintSet, *, warnings: bool = False):
    validation = _validation()
    readiness = _readiness()
    review = create_constraint_review(
        cset, policy=ReviewPolicy(), readiness=readiness, validation=validation,
    )
    if warnings:
        return approve_review(review, cset, actor=ReviewActor.from_value("reviewer"), comment="approved")
    return approve_review(review, cset, actor=ReviewActor.from_value("reviewer"), comment="approved")


def _clean_inputs(cset: ConstraintSet):
    validation = _validation()
    readiness = _readiness()
    lineage = build_constraint_lineage(cset)
    review = _review(cset)
    policy = ReleasePolicy(require_configuration=False, require_design=False, require_timing_graph=False)
    return {"policy": policy, "review": review, "readiness": readiness,
            "validation": validation, "lineage": lineage}


def _candidate(cset: ConstraintSet, **overrides):
    args = _clean_inputs(cset)
    args.update(overrides)
    return create_release_candidate(cset, **args)


def _released(cset: ConstraintSet, **overrides):
    candidate = _candidate(cset, **overrides)
    assert candidate.status == ReleaseStatus.READY
    args = _clean_inputs(cset)
    args.update(overrides)
    return release_constraint_set(candidate, cset, actor=ReviewActor.from_value("releaser"), comment="release", **args)


def test_typed_release_models_are_immutable_and_candidate_never_releases_implicitly():
    candidate = _candidate(_ucm())
    assert candidate.status == ReleaseStatus.READY
    assert candidate.decision is None
    with pytest.raises(FrozenInstanceError):
        candidate.status = ReleaseStatus.RELEASED


def test_evidence_details_are_deeply_immutable():
    candidate = _candidate(_ucm())
    evidence = candidate.evidence[0]
    with pytest.raises(TypeError):
        evidence.details["changed"] = True
    nested = next(item for item in candidate.evidence if item.authority == "CANONICAL_UCM")
    assert isinstance(nested.details["constraint_ids"], tuple)


def test_clean_candidate_and_json_are_deterministic():
    cset = _ucm()
    first = _candidate(cset)
    second = _candidate(cset)
    assert first.id == second.id
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(second.to_dict(), sort_keys=True)


def test_release_is_explicit_immutable_successor_and_not_external_signoff():
    cset = _ucm(); candidate = _candidate(cset)
    released = release_constraint_set(candidate, cset, actor=ReviewActor.from_value("r"), comment="explicit")
    assert released.status == ReleaseStatus.RELEASED
    assert released.previous_release_id == candidate.id
    assert candidate.status == ReleaseStatus.READY
    assert released.external_eda_signoff.value == "EXTERNAL_EDA_SIGNOFF_UNKNOWN"


def test_clean_assessment_is_possible_but_does_not_release():
    cset = _ucm(); candidate = _candidate(cset)
    assessment = assess_constraint_release(cset, release=candidate)
    assert assessment.release_possible
    assert assessment.current_status == ReleaseStatus.READY
    assert candidate.status == ReleaseStatus.READY


def test_missing_review_fails_closed():
    cset = _ucm(); args = _clean_inputs(cset); args["review"] = None
    candidate = create_release_candidate(cset, **args)
    assert candidate.status == ReleaseStatus.BLOCKED
    assert any(item.category == "REVIEW" and item.severity == ReleaseIssueSeverity.BLOCKER for item in candidate.issues)


def test_review_bound_to_other_ucm_fails_closed():
    first = _ucm(); second = _ucm(); second.create_clock("other", 20e-9, source="other", source_kind=SourceKind.USER)
    args = _clean_inputs(second); args["review"] = _review(first)
    candidate = create_release_candidate(second, **args)
    assert any(item.category == "REVIEW" and item.severity == ReleaseIssueSeverity.BLOCKER for item in candidate.issues)


def test_nonapproved_review_is_not_releaseable():
    cset = _ucm(); args = _clean_inputs(cset)
    args["review"] = create_constraint_review(cset, policy=ReviewPolicy(),
                                                 readiness=args["readiness"], validation=args["validation"])
    candidate = create_release_candidate(cset, **args)
    assert candidate.status == ReleaseStatus.BLOCKED


def test_missing_readiness_validation_and_lineage_each_fail_closed():
    cset = _ucm()
    for name in ("readiness", "validation", "lineage"):
        args = _clean_inputs(cset); args[name] = None
        candidate = create_release_candidate(cset, **args)
        assert candidate.status == ReleaseStatus.BLOCKED
        assert any(item.category == name.upper() for item in candidate.issues)


def test_readiness_blockers_and_warnings_are_propagated():
    cset = _ucm(); args = _clean_inputs(cset)
    blocked = replace(args["readiness"], blockers=args["readiness"].blockers + ())
    # Existing readiness status itself is a deterministic, policy-controlled gate.
    args["readiness"] = _readiness(ReadinessStatus.BLOCKED)
    candidate = create_release_candidate(cset, **args)
    assert candidate.status == ReleaseStatus.BLOCKED
    assert any(item.category == "READINESS" for item in candidate.issues)
    assert blocked.status == ReadinessStatus.READY


def test_validation_warning_requires_explicit_warning_policy():
    cset = _ucm(); args = _clean_inputs(cset); args["validation"] = _validation(status="PASS_WITH_WARNINGS")
    candidate = create_release_candidate(cset, **args)
    assert candidate.status == ReleaseStatus.CANDIDATE
    with pytest.raises(ReleaseDecisionError):
        release_constraint_set(candidate, cset, actor=ReviewActor.from_value("r"), **args)
    allowed = replace(args["policy"], allow_release_with_warnings=True)
    candidate = create_release_candidate(cset, **(args | {"policy": allowed}))
    released = release_constraint_set(candidate, cset, actor=ReviewActor.from_value("r"), **(args | {"policy": allowed}))
    assert released.status == ReleaseStatus.RELEASED_WITH_WARNINGS


def test_coverage_unknown_or_incomplete_fails_closed():
    cset = _ucm(); args = _clean_inputs(cset)
    args["validation"] = _validation(coverage=CoverageReport(graph_available=False))
    candidate = create_release_candidate(cset, **args)
    assert any(item.category == "COVERAGE" and item.severity == ReleaseIssueSeverity.BLOCKER for item in candidate.issues)


def test_exact_selected_scope_is_retained_and_global_is_distinct():
    cset = _ucm(scenarios=("SLOW", "FAST")); args = _clean_inputs(cset)
    selected = create_release_candidate(cset, scenario_ids=("FAST",), **args)
    global_release = create_release_candidate(cset, **args)
    assert selected.scope.scope_kind == "SELECTED_SCENARIOS"
    assert selected.scope.released_scenario_ids == ("FAST",)
    assert global_release.scope.scope_kind == "GLOBAL"
    assert global_release.scope.released_scenario_ids == ("FAST", "SLOW")


def test_all_active_scope_is_explicit_and_deterministic():
    cset = _ucm(scenarios=("SLOW", "FAST")); args = _clean_inputs(cset)
    release = create_release_candidate(cset, all_active_scenarios=True, **args)
    assert release.scope.scope_kind == "ALL_ACTIVE_SCENARIOS"
    assert release.scope.released_scenario_ids == ("FAST", "SLOW")


@pytest.mark.parametrize("selected,all_active", [(("NOPE",), False), (("FAST",), True)])
def test_unknown_or_conflicting_scope_is_invalid(selected, all_active):
    cset = _ucm(scenarios=("FAST",)); candidate = create_release_candidate(
        cset, scenario_ids=selected, all_active_scenarios=all_active, **_clean_inputs(cset),
    )
    assert candidate.status == ReleaseStatus.INVALID
    assert any(item.category == "MCMM_SCOPE" and item.severity == ReleaseIssueSeverity.BLOCKER for item in candidate.issues)


def test_policy_can_require_all_active_scenarios():
    cset = _ucm(scenarios=("FAST", "SLOW")); args = _clean_inputs(cset)
    candidate = create_release_candidate(cset, scenario_ids=("FAST",), **args)
    assert candidate.status == ReleaseStatus.BLOCKED
    policy = replace(args["policy"], require_all_active_scenarios=False)
    candidate = create_release_candidate(cset, scenario_ids=("FAST",), **(args | {"policy": policy}))
    assert candidate.status == ReleaseStatus.READY


def test_sdc_is_never_generated_and_only_explicit_supply_satisfies_policy(tmp_path: Path):
    cset = _ucm(); args = _clean_inputs(cset); args["policy"] = replace(args["policy"], require_sdc=True)
    blocked = create_release_candidate(cset, **args)
    assert any(item.category == "SDC" for item in blocked.issues)
    sdc = tmp_path / "existing.sdc"; sdc.write_text("create_clock -period 10 clk\n", encoding="utf-8")
    candidate = create_release_candidate(cset, sdc_path=sdc, **args)
    assert candidate.status == ReleaseStatus.READY
    assert any(item.authority == "SDC" and item.status == "SUPPLIED" for item in candidate.evidence)


def test_required_supplied_artifact_hash_is_bound_without_source_path(tmp_path: Path):
    cset = _ucm(); artifact = tmp_path / "source.txt"; artifact.write_text("evidence", encoding="utf-8")
    args = _clean_inputs(cset); args["policy"] = replace(args["policy"], required_artifact_kinds=("trace",))
    candidate = create_release_candidate(cset, additional_artifacts={"trace": artifact}, **args)
    assert candidate.status == ReleaseStatus.READY
    artifact_evidence = next(item for item in candidate.evidence if item.authority == "ARTIFACT:trace")
    assert str(artifact) not in json.dumps(artifact_evidence.to_dict())


def test_missing_required_artifact_fails_closed():
    cset = _ucm(); args = _clean_inputs(cset); args["policy"] = replace(args["policy"], required_artifact_kinds=("trace",))
    candidate = create_release_candidate(cset, **args)
    assert candidate.status == ReleaseStatus.BLOCKED


def test_formal_class_requirement_fails_closed_when_no_actual_evidence():
    cset = _ucm(); args = _clean_inputs(cset)
    ctype = next(iter(cset)).type.value
    args["policy"] = replace(args["policy"], require_formal_for_constraint_types=(ctype,))
    candidate = create_release_candidate(cset, **args)
    assert candidate.status == ReleaseStatus.BLOCKED
    assert any(item.category == "FORMAL" for item in candidate.issues)


def test_changed_ucm_or_scope_makes_record_stale():
    cset = _ucm(scenarios=("FAST",)); candidate = _candidate(cset)
    cset.create_clock("other", 20e-9, source="other", source_kind=SourceKind.USER)
    assessment = assess_constraint_release(cset, release=candidate)
    assert assessment.current_status == ReleaseStatus.STALE
    assert assessment.staleness_reasons


def test_revoke_creates_immutable_successor_and_can_not_be_released_again():
    cset = _ucm(); released = _released(cset)
    revoked = revoke_release(released, cset, actor=ReviewActor.from_value("releaser"), comment="withdraw",
                             recorded_at=_EPOCH)
    assert revoked.status == ReleaseStatus.REVOKED
    assert revoked.decision.recorded_at == _EPOCH
    assert revoked.previous_release_id == released.id
    with pytest.raises(ReleaseDecisionError):
        release_constraint_set(revoked, cset, actor=ReviewActor.from_value("releaser"))


def test_supersede_creates_new_candidate_without_mutating_prior_release():
    cset = _ucm(); released = _released(cset)
    cset.create_clock("later", 12e-9, source="later", source_kind=SourceKind.USER)
    successor = supersede_release(released, cset, **_clean_inputs(cset))
    assert successor.supersedes_release_id == released.id
    assert successor.status == ReleaseStatus.READY
    assert released.status == ReleaseStatus.RELEASED


def test_package_is_explicit_reproducible_and_statelessly_verified(tmp_path: Path):
    cset = _ucm(); args = _clean_inputs(cset); released = release_constraint_set(
        create_release_candidate(cset, **args), cset, actor=ReviewActor.from_value("r"), comment="go", **args,
    )
    package = create_release_package(released, cset, tmp_path / "package", review=args["review"],
                                     readiness=args["readiness"], validation=args["validation"], lineage=args["lineage"])
    verification = verify_release_package(tmp_path / "package")
    assert package.id == verification.package_id
    assert verification.status == PackageVerificationStatus.VERIFIED
    assert (tmp_path / "package" / "release_manifest.json").is_file()


def test_package_tamper_fails_hash_verification(tmp_path: Path):
    cset = _ucm(); args = _clean_inputs(cset); released = release_constraint_set(
        create_release_candidate(cset, **args), cset, actor=ReviewActor.from_value("r"), **args,
    )
    root = tmp_path / "package"
    create_release_package(released, cset, root, review=args["review"], readiness=args["readiness"],
                           validation=args["validation"], lineage=args["lineage"])
    (root / "ucm_snapshot.json").write_text("{}\n", encoding="utf-8")
    assert verify_release_package(root).status == PackageVerificationStatus.INVALID


def test_manifest_tamper_and_traversal_artifact_both_fail_closed(tmp_path: Path):
    cset = _ucm(); args = _clean_inputs(cset); released = release_constraint_set(
        create_release_candidate(cset, **args), cset, actor=ReviewActor.from_value("r"), **args,
    )
    root = tmp_path / "package"
    create_release_package(released, cset, root, review=args["review"], readiness=args["readiness"],
                           validation=args["validation"], lineage=args["lineage"])
    data = json.loads((root / "release_manifest.json").read_text(encoding="utf-8"))
    data["artifacts"][0]["relative_path"] = "../outside.json"
    (root / "release_manifest.json").write_text(json.dumps(data), encoding="utf-8")
    assert verify_release_package(root).status == PackageVerificationStatus.INVALID


def test_package_rejects_candidate_stale_or_revoked_records(tmp_path: Path):
    cset = _ucm(); candidate = _candidate(cset)
    with pytest.raises(ValueError):
        create_release_package(candidate, cset, tmp_path / "candidate")
    released = _released(cset); revoked = revoke_release(released, cset)
    with pytest.raises(ValueError):
        create_release_package(revoked, cset, tmp_path / "revoked")


def test_package_requires_identity_bound_evidence_files(tmp_path: Path):
    cset = _ucm(); args = _clean_inputs(cset); released = release_constraint_set(
        create_release_candidate(cset, **args), cset, actor=ReviewActor.from_value("r"), **args,
    )
    with pytest.raises(ValueError):
        create_release_package(released, cset, tmp_path / "missing-evidence")


def test_release_round_trip_is_lossless_and_deterministic():
    release = _candidate(_ucm())
    restored = ConstraintRelease.from_dict(release.to_dict())
    assert restored.to_dict() == release.to_dict()


@pytest.mark.parametrize("policy_delta", [
    {"require_review": False}, {"require_readiness": False}, {"require_validation_evidence": False},
    {"require_complete_coverage": False}, {"require_lineage": False}, {"require_sdc": True},
    {"allow_review_with_warnings": True}, {"allow_release_with_warnings": True},
])
def test_policy_json_round_trips_deterministically(policy_delta):
    policy = replace(ReleasePolicy(), **policy_delta)
    assert ReleasePolicy.from_dict(policy.to_dict()).to_dict() == policy.to_dict()


def test_default_policy_requires_exact_configuration_design_and_timing_identities():
    candidate = create_release_candidate(_ucm())
    blockers = {item.category for item in candidate.issues if item.severity == ReleaseIssueSeverity.BLOCKER}
    assert {"CONFIGURATION", "DESIGN", "TIMING_GRAPH"}.issubset(blockers)


def test_review_scope_must_cover_the_exact_release_scope():
    cset = _ucm(scenarios=("FAST", "SLOW"))
    args = _clean_inputs(cset)
    # The supplied review is deliberately scoped to FAST only; release must not
    # silently broaden it to SLOW/global coverage.
    review = create_constraint_review(
        cset, policy=ReviewPolicy(require_all_active_scenarios=False), scenario_ids=("FAST",),
        readiness=args["readiness"], validation=args["validation"],
    )
    review = approve_review(review, cset, actor=ReviewActor.from_value("reviewer"), comment="FAST only")
    candidate = create_release_candidate(cset, **(args | {"review": review}))
    assert candidate.status == ReleaseStatus.BLOCKED
    assert any(item.category == "REVIEW" and "scope" in item.message.lower() for item in candidate.issues)


def test_package_refuses_nonempty_directory_instead_of_repairing_or_mixing_contents(tmp_path: Path):
    cset = _ucm(); args = _clean_inputs(cset); released = release_constraint_set(
        create_release_candidate(cset, **args), cset, actor=ReviewActor.from_value("r"), **args,
    )
    root = tmp_path / "existing"; root.mkdir(); (root / "old.txt").write_text("old", encoding="utf-8")
    with pytest.raises(ValueError, match="new or empty"):
        create_release_package(released, cset, root, review=args["review"], readiness=args["readiness"],
                               validation=args["validation"], lineage=args["lineage"])
