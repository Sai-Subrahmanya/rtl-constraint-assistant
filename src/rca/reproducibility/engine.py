"""Compose existing UCM, manifest, package and formal evidence without new authority."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..constraint_model import ConstraintSet, stable_hash_cset
from ..exceptions.formal_backend import VerificationResult, formal_result_is_current
from ..utils.hashing import hash_file, stable_hash
from .models import (
    ReplayEvidenceComponent,
    ReplayEvidenceReport,
    ReplayEvidenceStatus,
    make_replay_evidence_id,
)


def assess_replay_evidence(
    cset: ConstraintSet,
    *,
    config: Any | None = None,
    manifest: Any | None = None,
    formal_results: Iterable[Any] = (),
    package_verification: Any | None = None,
) -> ReplayEvidenceReport:
    """Assess retained evidence from authoritative existing systems.

    This function does no tool discovery, does not read/write history, and
    never treats a mock/available locator as an execution result. Absolute
    artifact locations are intentionally excluded from its deterministic
    identity; content hashes and existing component identifiers are retained.
    """
    ucm_identity = stable_hash_cset(cset)
    scenario_ids = tuple(sorted({sid for constraint in cset for sid in constraint.scenario_ids} | set(cset.scenarios)))
    configuration = _configuration_component(config)
    manifest_component = _manifest_component(manifest)
    formal_component = _formal_component(cset, formal_results)
    package_component = _package_component(package_verification, ucm_identity)
    components = (ReplayEvidenceComponent("canonical_ucm", ReplayEvidenceStatus.CURRENT, ucm_identity,
                                          (("constraint_count", str(len(cset))),)),
                  configuration, manifest_component, formal_component, package_component)
    configuration_identity = configuration.identity
    if not configuration_identity:
        readiness = "IDENTITY_INCOMPLETE"
    elif formal_component.status == ReplayEvidenceStatus.INVALID:
        readiness = "FORMAL_EVIDENCE_STALE"
    elif manifest_component.status == ReplayEvidenceStatus.INVALID:
        readiness = "ARTIFACT_INTEGRITY_INVALID"
    elif manifest_component.status == ReplayEvidenceStatus.NOT_SUPPLIED:
        readiness = "IDENTITY_COMPLETE_REPLAY_UNPLANNED"
    elif manifest_component.status != ReplayEvidenceStatus.CURRENT:
        readiness = "IDENTITY_INCOMPLETE"
    else:
        readiness = "REPLAY_INVESTIGATION_READY"
    return ReplayEvidenceReport(
        id=make_replay_evidence_id(ucm_content_identity=ucm_identity,
                                   engineering_configuration_identity=configuration_identity,
                                   scenario_ids=scenario_ids, components=components),
        ucm_content_identity=ucm_identity,
        engineering_configuration_identity=configuration_identity,
        scenario_ids=scenario_ids,
        components=components,
        replay_readiness=readiness,
    )


def _configuration_component(config: Any | None) -> ReplayEvidenceComponent:
    if config is None:
        return ReplayEvidenceComponent("engineering_configuration", ReplayEvidenceStatus.NOT_SUPPLIED)
    data = config.engineering_dict() if hasattr(config, "engineering_dict") else _as_dict(config)
    if not isinstance(data, dict):
        return ReplayEvidenceComponent("engineering_configuration", ReplayEvidenceStatus.INVALID,
                                       detail=(("reason", "configuration does not serialize to a mapping"),))
    return ReplayEvidenceComponent("engineering_configuration", ReplayEvidenceStatus.CURRENT,
                                   stable_hash(_portable(data)))


def _manifest_component(manifest: Any | None) -> ReplayEvidenceComponent:
    if manifest is None:
        return ReplayEvidenceComponent("eda_manifest", ReplayEvidenceStatus.NOT_SUPPLIED)
    data = _as_dict(manifest)
    if not isinstance(data, dict):
        return ReplayEvidenceComponent("eda_manifest", ReplayEvidenceStatus.INVALID,
                                       detail=(("reason", "manifest does not serialize to a mapping"),))
    execution_mode = str(data.get("execution_mode") or "UNKNOWN")
    execution_status = str(data.get("execution_status") or "UNKNOWN")
    artifact_hashes = data.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict) or not artifact_hashes:
        return ReplayEvidenceComponent("eda_manifest", ReplayEvidenceStatus.INCOMPLETE,
                                       stable_hash(_portable(data)),
                                       (("execution_mode", execution_mode),
                                        ("execution_status", execution_status),
                                        ("reason", "no retained artifact hashes")))
    invalid = _manifest_artifact_invalid(data)
    status = ReplayEvidenceStatus.INVALID if invalid else ReplayEvidenceStatus.CURRENT
    detail = (("execution_mode", execution_mode), ("execution_status", execution_status),
              ("artifact_integrity", "invalid" if invalid else "recorded"))
    return ReplayEvidenceComponent("eda_manifest", status, stable_hash(_portable(data)), detail)


def _manifest_artifact_invalid(data: dict[str, Any]) -> bool:
    artifacts = data.get("artifacts")
    hashes = data.get("artifact_hashes")
    if not isinstance(artifacts, dict) or not isinstance(hashes, dict):
        return False
    for kind, expected in hashes.items():
        locator = artifacts.get(kind)
        if not isinstance(locator, str) or not locator or not isinstance(expected, str) or not expected:
            return True
        path = Path(locator)
        # A portable/moved manifest can still be complete identity evidence;
        # only inspect locations that are directly available to this process.
        if path.is_file():
            try:
                if hash_file(path) != expected:
                    return True
            except OSError:
                return True
    return False


def _formal_component(cset: ConstraintSet, formal_results: Iterable[Any]) -> ReplayEvidenceComponent:
    results = tuple(formal_results)
    if not results:
        return ReplayEvidenceComponent("formal", ReplayEvidenceStatus.NOT_SUPPLIED)
    stale: list[str] = []
    records: list[dict[str, Any]] = []
    for raw in results:
        result = getattr(raw, "verification", raw)
        if isinstance(result, VerificationResult):
            constraint = cset.get(result.constraint_id)
            if constraint is None or not formal_result_is_current(result, constraint):
                stale.append(result.constraint_id or "UNKNOWN")
            records.append(result.to_dict())
        else:
            records.append(_as_dict(result))
            stale.append(str(_as_dict(result).get("constraint_id", "UNKNOWN")))
    if stale:
        return ReplayEvidenceComponent("formal", ReplayEvidenceStatus.INVALID, stable_hash(_portable(records)),
                                       (("stale_or_unbound_constraint_ids", ",".join(sorted(set(stale)))),))
    return ReplayEvidenceComponent("formal", ReplayEvidenceStatus.CURRENT, stable_hash(_portable(records)),
                                   (("result_count", str(len(records))),))


def _package_component(verification: Any | None, ucm_identity: str) -> ReplayEvidenceComponent:
    if verification is None:
        return ReplayEvidenceComponent("release_package", ReplayEvidenceStatus.NOT_SUPPLIED)
    data = _as_dict(verification)
    status = str(data.get("status") or "UNKNOWN")
    snapshot = str(data.get("snapshot_identity") or "")
    if status != "VERIFIED":
        return ReplayEvidenceComponent("release_package", ReplayEvidenceStatus.INVALID, stable_hash(_portable(data)),
                                       (("verification_status", status),))
    if snapshot != ucm_identity:
        return ReplayEvidenceComponent("release_package", ReplayEvidenceStatus.INVALID, stable_hash(_portable(data)),
                                       (("reason", "package snapshot identity differs from canonical UCM"),))
    return ReplayEvidenceComponent("release_package", ReplayEvidenceStatus.CURRENT, stable_hash(_portable(data)),
                                   (("verification_status", status),))


def _as_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return dict(value) if isinstance(value, dict) else {}


def _portable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _portable(item) for key, item in sorted(value.items())
                if str(key) not in {"timestamp", "config_path", "project_root", "run_dir", "cwd"}}
    if isinstance(value, (list, tuple)):
        return [_portable(item) for item in value]
    if isinstance(value, str):
        path = Path(value)
        return path.name if path.is_absolute() else value
    return value
