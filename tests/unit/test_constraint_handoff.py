"""Step-33 handoff contracts over already released Step-32 packages."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from rca.handoff import (
    ConstraintHandoff,
    HandoffError,
    HandoffPolicy,
    HandoffStatus,
    HandoffTarget,
    assess_constraint_handoff,
    execute_constraint_handoff,
    prepare_constraint_handoff,
    verify_constraint_handoff,
)
from rca.release import create_release_package

from .test_constraint_release import _clean_inputs, _released, _ucm


def _package(tmp_path: Path, *, sdc: bool = False) -> tuple[Path, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    cset = _ucm(); args = _clean_inputs(cset)
    if sdc:
        path = tmp_path / "existing.sdc"; path.write_text("create_clock -period 10 clk\n", encoding="utf-8")
        args["sdc_path"] = path
    release = _released(cset, **args)
    root = tmp_path / "package"
    create_release_package(release, cset, root, review=args["review"], readiness=args["readiness"],
                           validation=args["validation"], lineage=args["lineage"], sdc_path=args.get("sdc_path"))
    return root, release


def _policy(**kwargs):
    return HandoffPolicy(require_configuration_identity=False, **kwargs)


def test_generic_assessment_consumes_verified_package_without_mutating_it(tmp_path: Path):
    root, _ = _package(tmp_path)
    before = {item.relative_to(root): item.read_bytes() for item in root.rglob("*") if item.is_file()}
    assessment = assess_constraint_handoff(root, policy=_policy())
    assert assessment.handoff.status == HandoffStatus.HANDOFF_READY
    assert assessment.handoff_possible
    assert {item.relative_to(root): item.read_bytes() for item in root.rglob("*") if item.is_file()} == before


def test_assessment_and_identity_are_deterministic(tmp_path: Path):
    root, _ = _package(tmp_path)
    first = assess_constraint_handoff(root, policy=_policy())
    second = assess_constraint_handoff(root, policy=_policy())
    assert first.handoff.id == second.handoff.id
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(second.to_dict(), sort_keys=True)


def test_preparation_is_explicit_immutable_successor_projection(tmp_path: Path):
    root, _ = _package(tmp_path)
    ready = assess_constraint_handoff(root, policy=_policy()).handoff
    prepared = prepare_constraint_handoff(root, policy=_policy())
    assert ready.status == HandoffStatus.HANDOFF_READY
    assert prepared.status == HandoffStatus.HANDOFF_PREPARED
    with pytest.raises(FrozenInstanceError):
        prepared.status = HandoffStatus.HANDOFF_EXECUTED


def test_execution_boundary_never_fabricates_a_tool_run_or_signoff(tmp_path: Path):
    root, _ = _package(tmp_path)
    prepared = prepare_constraint_handoff(root, policy=_policy())
    deferred = execute_constraint_handoff(prepared)
    unavailable = execute_constraint_handoff(prepared, execute=True)
    assert deferred.status == HandoffStatus.HANDOFF_PREPARED
    assert not deferred.actual_external_tool_executed
    assert unavailable.status == HandoffStatus.UNAVAILABLE
    assert not unavailable.executed and not unavailable.actual_external_tool_executed


def test_configuration_identity_is_a_conservative_default_gate(tmp_path: Path):
    root, _ = _package(tmp_path)
    assessment = assess_constraint_handoff(root)
    assert assessment.handoff.status == HandoffStatus.FAILED
    assert any(item.category == "CONFIGURATION" for item in assessment.issues)


def test_required_sdc_is_only_satisfied_by_preexisting_packaged_sdc(tmp_path: Path):
    root, _ = _package(tmp_path)
    missing = assess_constraint_handoff(root, policy=_policy(require_sdc=True))
    assert missing.handoff.status == HandoffStatus.FAILED
    root, _ = _package(tmp_path / "with-sdc", sdc=True)
    supplied = assess_constraint_handoff(root, policy=_policy(require_sdc=True, sdc_dialect="GENERIC"))
    assert supplied.handoff.status == HandoffStatus.HANDOFF_READY


@pytest.mark.parametrize("target", [HandoffTarget.OPENSTA_OPENROAD, HandoffTarget.SYNOPSYS, HandoffTarget.CADENCE])
def test_vendor_targets_require_explicit_compatible_sdc_dialect(tmp_path: Path, target: HandoffTarget):
    root, _ = _package(tmp_path, sdc=True)
    unknown = assess_constraint_handoff(root, target=target, policy=_policy())
    assert unknown.handoff.status == HandoffStatus.FAILED
    dialect = {HandoffTarget.OPENSTA_OPENROAD: "OPENSTA", HandoffTarget.SYNOPSYS: "SYNOPSYS",
               HandoffTarget.CADENCE: "CADENCE"}[target]
    compatible = assess_constraint_handoff(root, target=target, policy=_policy(sdc_dialect=dialect))
    assert compatible.handoff.status == HandoffStatus.HANDOFF_READY


def test_incompatible_vendor_dialect_and_future_vendor_fail_closed(tmp_path: Path):
    root, _ = _package(tmp_path, sdc=True)
    incompatible = assess_constraint_handoff(root, target="SYNOPSYS", policy=_policy(sdc_dialect="CADENCE"))
    future = assess_constraint_handoff(root, target="FUTURE_VENDOR", policy=_policy())
    assert incompatible.handoff.status == HandoffStatus.FAILED
    assert future.handoff.status == HandoffStatus.FAILED


def test_corrupt_or_missing_package_cannot_be_prepared(tmp_path: Path):
    root, _ = _package(tmp_path)
    (root / "ucm_snapshot.json").write_text("{}\n", encoding="utf-8")
    assessment = assess_constraint_handoff(root, policy=_policy())
    assert assessment.handoff.status == HandoffStatus.FAILED
    with pytest.raises(HandoffError):
        prepare_constraint_handoff(root, policy=_policy())


def test_handoff_verification_detects_tampered_identity_scope_and_artifacts(tmp_path: Path):
    root, _ = _package(tmp_path)
    prepared = prepare_constraint_handoff(root, policy=_policy())
    restored = ConstraintHandoff.from_dict(prepared.to_dict())
    assert verify_constraint_handoff(restored, root).handoff.status == HandoffStatus.HANDOFF_READY
    bad = replace(restored, id="HOF-tampered")
    assert verify_constraint_handoff(bad, root).handoff.status == HandoffStatus.FAILED


def test_required_artifact_kind_is_checked_against_package_members(tmp_path: Path):
    root, _ = _package(tmp_path)
    assessment = assess_constraint_handoff(root, policy=_policy(required_artifact_kinds=("timing_report",)))
    assert assessment.handoff.status == HandoffStatus.FAILED
    assert any(item.category == "ARTIFACT" for item in assessment.issues)


def test_unknown_target_is_an_explicit_error(tmp_path: Path):
    root, _ = _package(tmp_path)
    with pytest.raises(HandoffError):
        assess_constraint_handoff(root, target="MAGIC", policy=_policy())
