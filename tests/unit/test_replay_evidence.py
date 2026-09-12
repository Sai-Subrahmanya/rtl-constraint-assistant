"""Step-43 cross-lifecycle identity and replay-evidence tests."""

from __future__ import annotations

from rca.artifacts import RunManifest
from rca.config.model import ProjectConfig, ProjectInfo
from rca.exceptions import VerificationResult, bind_verification_result
from rca.reproducibility import ReplayEvidenceStatus, assess_replay_evidence
from rca.utils.enums import VerificationStatus
from tests.unit.test_constraint_release import _ucm


def test_replay_evidence_is_deterministic_portable_and_never_claims_execution(tmp_path):
    cset = _ucm(scenarios=("SLOW", "FAST"))
    config = ProjectConfig(project=ProjectInfo(name="dut", top="dut", rtl_files=["rtl/dut.sv"]))
    artifact = tmp_path / "result.rpt"
    artifact.write_text("observed report\n", encoding="utf-8")
    from rca.utils.hashing import hash_file

    manifest = RunManifest(
        config_hash="legacy-hash", execution_mode="REAL", execution_status="PASS",
        artifacts={"sta_setup": str(artifact)}, artifact_hashes={"sta_setup": hash_file(artifact)},
    )
    first = assess_replay_evidence(cset, config=config, manifest=manifest)
    second = assess_replay_evidence(cset, config=config, manifest=manifest)
    assert first.to_dict() == second.to_dict()
    assert first.replay_readiness == "REPLAY_INVESTIGATION_READY"
    assert first.to_dict()["automatic_replay_supported"] is False
    assert "does not execute" in first.to_dict()["meaning"]
    components = {item.name: item for item in first.components}
    assert components["canonical_ucm"].status == ReplayEvidenceStatus.CURRENT
    assert components["eda_manifest"].status == ReplayEvidenceStatus.CURRENT


def test_replay_evidence_marks_changed_formal_or_artifact_evidence_invalid(tmp_path):
    cset = _ucm()
    constraint = next(iter(cset))
    formal = bind_verification_result(VerificationResult(
        constraint_id=constraint.id, status=VerificationStatus.VERIFIED, tool="actual-tool", tool_version="1",
        property_checked="property", evidence={"proof_sha256": "abc"},
    ), constraint)
    report = assess_replay_evidence(cset, config=ProjectConfig(project=ProjectInfo(name="dut", top="dut")),
                                    formal_results=(formal,))
    assert {item.name: item.status for item in report.components}["formal"] == ReplayEvidenceStatus.CURRENT
    constraint.values["comment"] = "changed"
    stale = assess_replay_evidence(cset, config=ProjectConfig(project=ProjectInfo(name="dut", top="dut")),
                                   formal_results=(formal,))
    assert stale.replay_readiness == "FORMAL_EVIDENCE_STALE"
    artifact = tmp_path / "result.rpt"
    artifact.write_text("one", encoding="utf-8")
    from rca.utils.hashing import hash_file

    manifest = RunManifest(artifacts={"rpt": str(artifact)}, artifact_hashes={"rpt": hash_file(artifact)})
    artifact.write_text("two", encoding="utf-8")
    bad = assess_replay_evidence(cset, config=ProjectConfig(project=ProjectInfo(name="dut", top="dut")),
                                 manifest=manifest)
    assert bad.replay_readiness == "ARTIFACT_INTEGRITY_INVALID"
