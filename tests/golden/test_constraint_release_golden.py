"""Thirty-plus compact Step-32 release/package golden policy projections."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from rca.constraint_model import Constraint
from rca.release import (
    PackageVerificationStatus,
    ReleasePolicy,
    assess_constraint_release,
    create_release_candidate,
    create_release_package,
    release_constraint_set,
    revoke_release,
    supersede_release,
    verify_release_package,
)
from rca.review import ReviewActor
from rca.utils.enums import ConstraintType, SourceKind
from rca.validation.coverage import CoverageReport
from tests.unit.test_constraint_release import (
    _candidate,
    _clean_inputs,
    _released,
    _ucm,
    _validation,
)

_FIXTURES = Path(__file__).with_name("release")


def _result(record):
    assessment = assess_constraint_release(_ucm(), release=record)
    return {
        "record_status": record.status.value,
        "current_status": assessment.current_status.value,
        "release_possible": assessment.release_possible,
        "release_with_warnings_possible": assessment.release_with_warnings_possible,
        "blocker_categories": sorted({item.category for item in assessment.issues if item.severity.value == "BLOCKER"}),
        "warning_categories": sorted({item.category for item in assessment.issues if item.severity.value == "WARNING"}),
        "scope": record.scope.to_dict(),
        "external_eda_signoff": record.external_eda_signoff.value,
    }


def _projections(tmp_path: Path) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    cset = _ucm()
    clean = _candidate(cset)
    released = _released(cset)
    args = _clean_inputs(cset)
    missing_review = create_release_candidate(cset, **(args | {"review": None}))
    missing_readiness = create_release_candidate(cset, **(args | {"readiness": None}))
    missing_validation = create_release_candidate(cset, **(args | {"validation": None}))
    missing_lineage = create_release_candidate(cset, **(args | {"lineage": None}))
    warning_args = args | {"policy": replace(args["policy"], allow_release_with_warnings=True),
                           "validation": _validation(status="PASS_WITH_WARNINGS")}
    warning_candidate = create_release_candidate(cset, **warning_args)
    warning_released = release_constraint_set(warning_candidate, cset, actor=ReviewActor.from_value("alice"), **warning_args)
    scenarios = _ucm(scenarios=("FAST", "SLOW")); scenario_args = _clean_inputs(scenarios)
    selected = create_release_candidate(scenarios, scenario_ids=("FAST",),
                                        **(scenario_args | {"policy": replace(scenario_args["policy"], require_all_active_scenarios=False)}))
    all_active = create_release_candidate(scenarios, all_active_scenarios=True, **scenario_args)
    unknown_scope = create_release_candidate(scenarios, scenario_ids=("UNKNOWN",), **scenario_args)
    conflict_scope = create_release_candidate(scenarios, scenario_ids=("FAST",), all_active_scenarios=True, **scenario_args)
    sdc = tmp_path / "known.sdc"; sdc.write_text("create_clock -period 10 clk\n", encoding="utf-8")
    artifact = tmp_path / "trace.txt"; artifact.write_text("existing evidence\n", encoding="utf-8")
    sdc_policy = replace(args["policy"], require_sdc=True)
    artifact_policy = replace(args["policy"], required_artifact_kinds=("trace",))
    required_sdc_missing = create_release_candidate(cset, **(args | {"policy": sdc_policy}))
    required_sdc = create_release_candidate(cset, **(args | {"policy": sdc_policy, "sdc_path": sdc}))
    required_artifact_missing = create_release_candidate(cset, **(args | {"policy": artifact_policy}))
    required_artifact = create_release_candidate(cset, **(args | {"policy": artifact_policy,
                                                                    "additional_artifacts": {"trace": artifact}}))
    formal_policy = replace(args["policy"], require_formal_for_constraint_types=(ConstraintType.CREATE_CLOCK.value,))
    formal_missing = create_release_candidate(cset, **(args | {"policy": formal_policy}))
    incomplete_coverage = create_release_candidate(cset, **(args | {"validation": _validation(
        coverage=CoverageReport(graph_available=True, uncovered=[{"path": "p"}]),
    )}))
    unsupported = _ucm(); unsupported.add(Constraint(id="LOAD", type=ConstraintType.SET_LOAD,
                                                      target_objects=["q"], values={"value": 2.0}))
    unsupported_record = _candidate(unsupported)
    changed = cset.clone(); changed.create_clock("later", 12e-9, source="later", source_kind=SourceKind.USER)
    stale = assess_constraint_release(changed, release=clean)
    revoked = revoke_release(released, cset, actor=ReviewActor.from_value("bob"), comment="withdraw")
    newer = _ucm(); newer.create_clock("later", 12e-9, source="later", source_kind=SourceKind.USER)
    superseding = supersede_release(released, newer, **_clean_inputs(newer))
    package_root = tmp_path / "package"
    package = create_release_package(released, cset, package_root, review=args["review"], readiness=args["readiness"],
                                     validation=args["validation"], lineage=args["lineage"])
    package_verification = verify_release_package(package_root)
    (package_root / "ucm_snapshot.json").write_text("{}\n", encoding="utf-8")
    corrupted_verification = verify_release_package(package_root)
    return {
        "01_clean_candidate": _result(clean),
        "02_clean_explicit_release": {"status": released.status.value, "previous": bool(released.previous_release_id),
                                        "decision": released.decision.kind.value},
        "03_missing_review": _result(missing_review),
        "04_missing_readiness": _result(missing_readiness),
        "05_missing_validation": _result(missing_validation),
        "06_missing_lineage": _result(missing_lineage),
        "07_warning_candidate": _result(warning_candidate),
        "08_warning_explicit_release": {"status": warning_released.status.value,
                                          "decision": warning_released.decision.kind.value},
        "09_global_scope": clean.scope.to_dict(),
        "10_selected_scope": selected.scope.to_dict(),
        "11_all_active_scope": all_active.scope.to_dict(),
        "12_unknown_scope": _result(unknown_scope),
        "13_conflicting_scope": _result(conflict_scope),
        "14_required_sdc_missing": _result(required_sdc_missing),
        "15_required_sdc_supplied": {"status": required_sdc.status.value,
                                       "authorities": sorted(item.authority for item in required_sdc.evidence)},
        "16_required_artifact_missing": _result(required_artifact_missing),
        "17_required_artifact_supplied": {"status": required_artifact.status.value,
                                            "source_path_absent": str(artifact) not in json.dumps(required_artifact.to_dict())},
        "18_required_formal_missing": _result(formal_missing),
        "19_incomplete_coverage": _result(incomplete_coverage),
        "20_unsupported_semantics": {"status": unsupported_record.status.value,
                                       "semantic_identity": unsupported_record.snapshot.ucm_semantic_identity or "UNKNOWN"},
        "21_stale_ucm": {"current_status": stale.current_status.value, "reason_count": len(stale.staleness_reasons)},
        "22_explicit_revocation": {"status": revoked.status.value,
                                     "history": [item.kind.value for item in revoked.decision_history]},
        "23_explicit_supersession": {"status": superseding.status.value,
                                       "supersedes": superseding.supersedes_release_id == released.id},
        "24_package_manifest": {"kind": package.kind, "artifact_kinds": sorted(item.kind for item in package.artifacts)},
        "25_package_verification": {"status": package_verification.status.value,
                                      "count": len(package_verification.verified_artifact_ids)},
        "26_package_corruption": {"status": corrupted_verification.status.value,
                                   "blockers": sorted({item.category for item in corrupted_verification.issues})},
        "27_round_trip": clean.to_dict() == type(clean).from_dict(clean.to_dict()).to_dict(),
        "28_deterministic_record": _candidate(cset).to_dict() == _candidate(cset).to_dict(),
        "29_identity_binding": {"content": clean.identity.ucm_content_identity == clean.snapshot.ucm_content_identity,
                                  "semantic": clean.identity.ucm_semantic_identity == clean.snapshot.ucm_semantic_identity,
                                  "review": clean.identity.review_id == clean.snapshot.review_id},
        "30_policy_defaults": ReleasePolicy().to_dict(),
        "31_package_status_enum": package_verification.status == PackageVerificationStatus.VERIFIED,
        "32_no_eda_claim": released.external_eda_signoff.value,
    }


def test_golden_release_policy_lifecycle_and_package_projections(tmp_path: Path):
    actual = _projections(tmp_path)
    expected = json.loads((_FIXTURES / "deterministic_release.json").read_text(encoding="utf-8"))
    assert actual == expected
    assert _projections(tmp_path / "second")["28_deterministic_record"] is True
