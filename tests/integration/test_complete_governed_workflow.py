"""Deterministic complete non-tool lifecycle composition (Steps 34/36/42/43)."""

from __future__ import annotations

import json
from pathlib import Path

from rca.handoff import HandoffPolicy, assess_constraint_handoff
from rca.release import assess_constraint_release, create_release_package, verify_release_package
from rca.review import assess_constraint_review
from rca.workflow import (
    WorkflowInputs,
    WorkflowStageStatus,
    build_complete_workflow,
    explain_complete_workflow,
)
from tests.unit.test_constraint_release import _clean_inputs, _released, _ucm


def test_complete_governed_workflow_preserves_authorities_and_determinism(tmp_path: Path):
    cset = _ucm(scenarios=("FAST", "SLOW"))
    before = cset.to_canonical_json()
    args = _clean_inputs(cset)
    released = _released(cset, **args)
    package_dir = tmp_path / "release"
    create_release_package(released, cset, package_dir, review=args["review"], readiness=args["readiness"],
                           validation=args["validation"], lineage=args["lineage"])
    package_verification = verify_release_package(package_dir)
    handoff = assess_constraint_handoff(package_dir, policy=HandoffPolicy(require_configuration_identity=False))
    review = assess_constraint_review(cset, review=args["review"], readiness=args["readiness"],
                                      validation=args["validation"], lineage=args["lineage"])
    release = assess_constraint_release(cset, release=released, **args)
    inputs = WorkflowInputs(
        design={"kind": "existing_design"}, knowledge={"kind": "offline"}, inference={"candidates": []},
        application={"status": "ACCEPTED"}, validation=args["validation"], readiness=args["readiness"],
        lineage=args["lineage"], review=review, release=release, package_verification=package_verification,
        handoff=handoff,
    )
    first = build_complete_workflow(cset, inputs=inputs)
    second = build_complete_workflow(cset, inputs=inputs)
    assert first.to_dict() == second.to_dict()
    assert first.ucm_content_identity == released.snapshot.ucm_content_identity
    assert dict(first.summary)["scenario_scope_preserved"] == "true"
    assert any(stage.name == "release" and stage.status == WorkflowStageStatus.COMPLETE for stage in first.stages)
    assert any(stage.name == "handoff" and stage.status == WorkflowStageStatus.COMPLETE for stage in first.stages)
    assert any(stage.name == "eda" and stage.status == WorkflowStageStatus.EDA_UNAVAILABLE for stage in first.stages)
    assert "does not apply, approve, release" in explain_complete_workflow(first)
    assert cset.to_canonical_json() == before
    assert json.loads(json.dumps(first.to_dict()))["kind"] == "rca_constraint_workflow"
