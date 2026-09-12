"""Explicit, deterministic release governance and reproducible package handling.

Step 32 consumes existing UCM, review, readiness, validation/coverage, formal,
lineage, MCMM, and supplied-artifact evidence. It never creates a review,
changes UCM, runs validation/formal/EDA, generates SDC, or creates a cache or
SQLite authority. Package writing is separately explicit and uses the existing
ArtifactManager for release-package files.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..artifacts import ArtifactManager
from ..constraint_model import ConstraintSet, SnapshotFormatError, stable_hash_cset
from ..equivalence import has_unsupported_options, normalize_constraint
from ..lineage import ConstraintLineageReport
from ..mcmm import build_scenario_matrix
from ..readiness import ConstraintReadinessReport
from ..review import (
    ConstraintReview,
    ConstraintReviewAssessment,
    ConstraintReviewStatus,
    ReviewActor,
)
from ..utils.hashing import hash_file, stable_hash
from ..validation import ValidationResult
from .models import (
    ConstraintRelease,
    ExternalEDASignoffStatus,
    PackageVerificationStatus,
    ReleaseArtifact,
    ReleaseAssessment,
    ReleaseBaseline,
    ReleaseDecision,
    ReleaseDecisionKind,
    ReleaseDependency,
    ReleaseEvidence,
    ReleaseIdentity,
    ReleaseIssue,
    ReleaseIssueSeverity,
    ReleasePackage,
    ReleasePolicy,
    ReleaseScope,
    ReleaseSnapshot,
    ReleaseStatus,
    ReleaseVerification,
)

_VOLATILE_ID_KEYS = {
    "created_at", "recorded_at", "timestamp", "import_timestamp", "runtime_seconds",
    "duration_seconds", "enabled_at", "started_at", "finished_at",
}


class ReleaseDecisionError(ValueError):
    """Raised when an explicit release action must fail closed."""


class ReleasePackageError(ValueError):
    """Raised for an invalid explicit package-writing request."""


def create_release_candidate(
    cset: ConstraintSet,
    *,
    policy: ReleasePolicy | None = None,
    scenario_ids: Iterable[str] = (),
    all_active_scenarios: bool = False,
    config: Any | None = None,
    design: Any | None = None,
    timing_graph: Any | None = None,
    review: ConstraintReview | ConstraintReviewAssessment | None = None,
    readiness: ConstraintReadinessReport | None = None,
    validation: ValidationResult | None = None,
    lineage: ConstraintLineageReport | None = None,
    formal_results: Iterable[Any] = (),
    sdc_path: str | Path | None = None,
    additional_artifacts: Mapping[str, str | Path] | None = None,
    external_eda_evidence_refs: Iterable[str] = (),
    supersedes_release_id: str | None = None,
) -> ConstraintRelease:
    """Create an immutable, un-released candidate over the exact supplied UCM.

    This is a read-only assessment boundary. A candidate with no blockers has
    status ``READY`` but still requires :func:`release_constraint_set`; READY
    never silently becomes RELEASED.
    """
    formal_results = tuple(formal_results)
    external_refs = _strings(external_eda_evidence_refs)
    policy = policy or ReleasePolicy()
    scope = _scope(cset, config, scenario_ids, all_active_scenarios)
    snapshot = _snapshot(cset, config, design, timing_graph, review, readiness, validation, lineage, formal_results)
    evidence, dependencies, issues = _evidence_and_gates(
        cset, snapshot, scope, policy, review, readiness, validation, lineage, formal_results,
        sdc_path, additional_artifacts,
    )
    root_id = _release_root_id(snapshot, scope, policy, evidence, dependencies, supersedes_release_id)
    identity = ReleaseIdentity(
        id=_release_identity(snapshot, scope, policy, evidence, dependencies),
        ucm_content_identity=snapshot.ucm_content_identity,
        ucm_semantic_identity=snapshot.ucm_semantic_identity,
        review_id=snapshot.review_id,
        scope_identity=_identity(scope.to_dict()),
    )
    baseline = ReleaseBaseline(
        id="RBL-" + stable_hash({"release_root_id": root_id, "snapshot": snapshot.to_dict(),
                                  "scope": scope.to_dict()})[:20],
        snapshot=snapshot, scope=scope, review_id=snapshot.review_id or None,
    )
    status = _candidate_status(scope, snapshot, issues)
    return ConstraintRelease(
        id=root_id,
        release_root_id=root_id,
        identity=identity,
        baseline=baseline,
        snapshot=snapshot,
        scope=scope,
        policy=policy,
        status=status,
        evidence=evidence,
        dependencies=dependencies,
        issues=issues,
        supersedes_release_id=supersedes_release_id,
        external_eda_signoff=(ExternalEDASignoffStatus.EVIDENCE_SUPPLIED if external_refs
                              else ExternalEDASignoffStatus.UNKNOWN),
        external_eda_evidence_refs=external_refs,
    )


def assess_constraint_release(
    cset: ConstraintSet,
    *,
    release: ConstraintRelease | None = None,
    policy: ReleasePolicy | None = None,
    scenario_ids: Iterable[str] = (),
    all_active_scenarios: bool = False,
    config: Any | None = None,
    design: Any | None = None,
    timing_graph: Any | None = None,
    review: ConstraintReview | ConstraintReviewAssessment | None = None,
    readiness: ConstraintReadinessReport | None = None,
    validation: ValidationResult | None = None,
    lineage: ConstraintLineageReport | None = None,
    formal_results: Iterable[Any] = (),
    sdc_path: str | Path | None = None,
    additional_artifacts: Mapping[str, str | Path] | None = None,
    external_eda_evidence_refs: Iterable[str] = (),
) -> ReleaseAssessment:
    """Read-only release eligibility/currentness assessment; never releases."""
    formal_results = tuple(formal_results)
    if release is None:
        release = create_release_candidate(
            cset, policy=policy, scenario_ids=scenario_ids, all_active_scenarios=all_active_scenarios,
            config=config, design=design, timing_graph=timing_graph, review=review, readiness=readiness,
            validation=validation, lineage=lineage, formal_results=formal_results, sdc_path=sdc_path,
            additional_artifacts=additional_artifacts, external_eda_evidence_refs=external_eda_evidence_refs,
        )
    elif policy is not None and policy != release.policy:
        raise ReleaseDecisionError("A stored release must be assessed under its recorded ReleasePolicy.")
    if not release.id or not release.snapshot.ucm_content_identity:
        raise ReleaseDecisionError("Release record is missing its exact reviewed UCM identity.")

    scope = _scope(cset, config, release.scope.requested_scenario_ids,
                   release.scope.scope_kind == "ALL_ACTIVE_SCENARIOS")
    snapshot = _snapshot(cset, config, design, timing_graph, review, readiness, validation, lineage, formal_results)
    # With no new evidence supplied, a decision may use the immutable evidence
    # captured in an existing candidate. Fresh optional evidence is assessed
    # only when callers explicitly supply it; nothing is silently recreated.
    use_recorded = (review is None and readiness is None and validation is None and lineage is None
                    and not formal_results and sdc_path is None and not additional_artifacts)
    if use_recorded:
        evidence, dependencies, issues = release.evidence, release.dependencies, release.issues
    else:
        evidence, dependencies, issues = _evidence_and_gates(
            cset, snapshot, scope, release.policy, review, readiness, validation, lineage, formal_results,
            sdc_path, additional_artifacts,
        )
    stale_reasons = _staleness_reasons(release, snapshot, scope, review, readiness, lineage)
    if stale_reasons:
        current_status = ReleaseStatus.STALE
    elif release.status == ReleaseStatus.REVOKED:
        current_status = ReleaseStatus.REVOKED
    elif release.status in {ReleaseStatus.RELEASED, ReleaseStatus.RELEASED_WITH_WARNINGS}:
        current_status = release.status
    else:
        current_status = _candidate_status(scope, snapshot, issues)
    blockers = any(item.severity == ReleaseIssueSeverity.BLOCKER for item in issues)
    warnings = any(item.severity == ReleaseIssueSeverity.WARNING for item in issues)
    eligible = current_status in {ReleaseStatus.CANDIDATE, ReleaseStatus.READY, ReleaseStatus.BLOCKED,
                                  ReleaseStatus.UNKNOWN, ReleaseStatus.INVALID}
    release_possible = eligible and not stale_reasons and not blockers and not warnings
    release_with_warnings_possible = (
        eligible and not stale_reasons and not blockers and release.policy.allow_release_with_warnings
    )
    return ReleaseAssessment(
        release=release,
        current_snapshot=snapshot,
        current_status=current_status,
        release_possible=release_possible,
        release_with_warnings_possible=release_with_warnings_possible,
        snapshot_current=not stale_reasons,
        staleness_reasons=stale_reasons,
        evidence=evidence,
        dependencies=dependencies,
        issues=issues,
    )


def release_constraint_set(
    release: ConstraintRelease,
    cset: ConstraintSet,
    *,
    policy: ReleasePolicy | None = None,
    actor: ReviewActor | None = None,
    comment: str = "",
    config: Any | None = None,
    design: Any | None = None,
    timing_graph: Any | None = None,
    review: ConstraintReview | ConstraintReviewAssessment | None = None,
    readiness: ConstraintReadinessReport | None = None,
    validation: ValidationResult | None = None,
    lineage: ConstraintLineageReport | None = None,
    formal_results: Iterable[Any] = (),
    sdc_path: str | Path | None = None,
    additional_artifacts: Mapping[str, str | Path] | None = None,
    external_eda_evidence_refs: Iterable[str] = (),
    recorded_at: str | None = None,
) -> ConstraintRelease:
    """Explicitly mark a current eligible candidate RELEASED; UCM stays untouched."""
    assessment = assess_constraint_release(
        cset, release=release, policy=policy, config=config, design=design, timing_graph=timing_graph, review=review,
        readiness=readiness, validation=validation, lineage=lineage, formal_results=tuple(formal_results),
        sdc_path=sdc_path, additional_artifacts=additional_artifacts,
        external_eda_evidence_refs=external_eda_evidence_refs,
    )
    if assessment.current_status == ReleaseStatus.STALE:
        raise ReleaseDecisionError("Cannot release a stale release candidate.")
    if assessment.current_status == ReleaseStatus.REVOKED:
        raise ReleaseDecisionError("Cannot release a revoked release record.")
    if assessment.release_possible:
        status = ReleaseStatus.RELEASED
    elif assessment.release_with_warnings_possible:
        status = ReleaseStatus.RELEASED_WITH_WARNINGS
    else:
        raise ReleaseDecisionError(_release_failure(assessment))
    actor = actor or ReviewActor()
    decision = _decision(release.release_root_id, ReleaseDecisionKind.RELEASE, actor, comment,
                         _evidence_ids(assessment.evidence), recorded_at)
    return replace(
        release,
        id=_decision_release_id(release, decision),
        previous_release_id=release.id,
        status=status,
        decision=decision,
        decision_history=(*release.decision_history, decision),
        evidence=assessment.evidence,
        dependencies=assessment.dependencies,
        issues=assessment.issues,
    )


def revoke_release(
    release: ConstraintRelease,
    cset: ConstraintSet,
    *,
    actor: ReviewActor | None = None,
    comment: str = "",
    **kwargs: Any,
) -> ConstraintRelease:
    """Explicitly revoke a released record by returning an immutable successor."""
    if release.status not in {ReleaseStatus.RELEASED, ReleaseStatus.RELEASED_WITH_WARNINGS}:
        raise ReleaseDecisionError("Only a released record can be explicitly revoked.")
    recorded_at = kwargs.pop("recorded_at", None)
    assessment = assess_constraint_release(cset, release=release, **kwargs)
    actor = actor or ReviewActor()
    decision = _decision(release.release_root_id, ReleaseDecisionKind.REVOKE, actor, comment,
                         _evidence_ids(assessment.evidence), recorded_at)
    return replace(
        release,
        id=_decision_release_id(release, decision),
        previous_release_id=release.id,
        status=ReleaseStatus.REVOKED,
        decision=decision,
        decision_history=(*release.decision_history, decision),
        evidence=assessment.evidence,
        dependencies=assessment.dependencies,
        issues=assessment.issues,
    )


def supersede_release(release: ConstraintRelease, cset: ConstraintSet, **kwargs: Any) -> ConstraintRelease:
    """Create a separate candidate for a newer snapshot without rewriting history."""
    return create_release_candidate(cset, supersedes_release_id=release.id, **kwargs)


def create_release_package(
    release: ConstraintRelease,
    cset: ConstraintSet,
    output_dir: str | Path,
    *,
    config: Any | None = None,
    design: Any | None = None,
    timing_graph: Any | None = None,
    review: ConstraintReview | ConstraintReviewAssessment | None = None,
    readiness: ConstraintReadinessReport | None = None,
    validation: ValidationResult | None = None,
    lineage: ConstraintLineageReport | None = None,
    formal_results: Iterable[Any] = (),
    sdc_path: str | Path | None = None,
    additional_artifacts: Mapping[str, str | Path] | None = None,
) -> ReleasePackage:
    """Explicitly write a reproducible package using existing ArtifactManager I/O.

    The release must already be RELEASED/RELEASED_WITH_WARNINGS and all supplied
    current inputs must match its recorded identities. No SDC is generated; a
    provided SDC is merely copied into the explicit package directory.
    """
    if release.status not in {ReleaseStatus.RELEASED, ReleaseStatus.RELEASED_WITH_WARNINGS}:
        raise ReleasePackageError("Only an explicit released record can be packaged.")
    formal_results = tuple(formal_results)
    assessment = assess_constraint_release(
        cset, release=release, config=config, design=design, timing_graph=timing_graph, review=review,
        readiness=readiness, validation=validation, lineage=lineage, formal_results=formal_results, sdc_path=sdc_path,
        additional_artifacts=additional_artifacts,
    )
    if assessment.current_status in {ReleaseStatus.STALE, ReleaseStatus.REVOKED} or not assessment.snapshot_current:
        raise ReleasePackageError("Cannot package a stale or revoked release record.")
    if assessment.current_snapshot.to_dict() != release.snapshot.to_dict():
        raise ReleasePackageError("Supplied package inputs do not match the released snapshot identities.")

    root = Path(output_dir)
    if root.exists() and any(root.iterdir()):
        raise ReleasePackageError("Release package directory must be new or empty; stale contents are not repaired.")
    manager = ArtifactManager(root)
    written: list[ReleaseArtifact] = []

    def write_json(kind: str, relative: str, payload: Any, *, required: bool = True) -> None:
        path = manager.write_json_atomic(relative, payload)
        written.append(_artifact(kind, relative, path, required=required))

    write_json("ucm_snapshot", "ucm_snapshot.json", cset.to_snapshot_dict())
    write_json("release_record", "release.json", release.to_dict())
    if review is not None:
        write_json("review_record", "review.json", _review_record(review).to_dict())
    if readiness is not None:
        write_json("readiness", "readiness.json", readiness.to_dict())
    if validation is not None:
        write_json("validation", "validation.json", validation.as_dict())
        coverage = getattr(validation, "coverage", None)
        if coverage is not None:
            write_json("coverage", "coverage.json", coverage.as_dict())
    if lineage is not None:
        write_json("lineage", "lineage.json", lineage.to_dict())
    if formal_results:
        write_json("formal", "formal.json", [_as_dict(item) for item in formal_results], required=False)
    if sdc_path is not None:
        source = Path(sdc_path)
        if not source.is_file():
            raise ReleasePackageError("Explicit supplied SDC path is not a readable file.")
        relative = str(Path("sdc") / _safe_filename(source.name))
        _copy_file_atomic(source, manager.path(relative))
        written.append(_artifact("sdc", relative, manager.path(relative), required=release.policy.require_sdc))
    for kind, raw_path in sorted((additional_artifacts or {}).items(), key=lambda item: str(item[0])):
        source = Path(raw_path)
        if not source.is_file():
            raise ReleasePackageError(f"Explicit supplied artifact {kind!r} is not a readable file.")
        relative = str(Path("artifacts") / _safe_filename(f"{kind}-{source.name}"))
        _copy_file_atomic(source, manager.path(relative))
        written.append(_artifact(str(kind), relative, manager.path(relative),
                                 required=str(kind) in set(release.policy.required_artifact_kinds)))

    required_kinds = {"ucm_snapshot", "release_record", *release.policy.required_artifact_kinds}
    if release.snapshot.review_id:
        required_kinds.add("review_record")
    if release.snapshot.readiness_identity:
        required_kinds.add("readiness")
    if release.snapshot.validation_identity:
        required_kinds.add("validation")
    if release.snapshot.coverage_identity:
        required_kinds.add("coverage")
    if release.snapshot.lineage_identity:
        required_kinds.add("lineage")
    if release.snapshot.formal_identity:
        required_kinds.add("formal")
    if release.policy.require_sdc:
        required_kinds.add("sdc")
    actual_kinds = {artifact.kind for artifact in written}
    missing = sorted(required_kinds - actual_kinds)
    if missing:
        raise ReleasePackageError("Package misses required artifacts: " + ", ".join(missing))
    package = ReleasePackage(
        id=_package_id(release, tuple(written)),
        release_id=release.id,
        release_identity=release.identity.id,
        snapshot=release.snapshot,
        scope=release.scope,
        dependencies=release.dependencies,
        artifacts=tuple(sorted(written, key=lambda item: item.relative_path)),
        root_path=str(root),
    )
    # This descriptor is the package's own content list, not a replacement for
    # existing run manifests or artifact/cache authority.
    manager.write_json_atomic("release_manifest.json", package.to_dict())
    return package


def verify_release_package(package_dir: str | Path) -> ReleaseVerification:
    """Statelessly verify an existing package without repairs or tool execution."""
    root = Path(package_dir)
    issues: list[ReleaseIssue] = []
    verified: list[str] = []
    manifest_path = root / "release_manifest.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data, Mapping):
            raise TypeError("release manifest must be a JSON object")
        package = ReleasePackage.from_dict(data, root_path=str(root))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        issue = _issue(ReleaseIssueSeverity.BLOCKER, "PACKAGE", f"Cannot read package manifest: {exc}")
        return ReleaseVerification(package_id="", status=PackageVerificationStatus.INVALID, issues=(issue,))
    if package.kind != "rca_constraint_release_package" or not package.id:
        issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "PACKAGE", "Package manifest kind or identity is invalid."))
    expected_package_id = _package_id_values(package.release_id, package.release_identity, package.snapshot,
                                             package.scope, package.dependencies, package.artifacts)
    if package.id != expected_package_id:
        issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "PACKAGE", "Package identity does not match manifest contents."))

    for artifact in package.artifacts:
        path = _safe_package_path(root, artifact.relative_path)
        if path is None or not path.is_file():
            if artifact.required:
                issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "ARTIFACT",
                                     f"Required package artifact is missing: {artifact.relative_path}", (artifact.id,)))
            continue
        actual_hash = hash_file(path)
        actual_size = path.stat().st_size
        if actual_hash != artifact.sha256 or actual_size != artifact.size_bytes:
            issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "ARTIFACT",
                                 f"Artifact hash or size mismatch: {artifact.relative_path}", (artifact.id,)))
            continue
        verified.append(artifact.id)

    release = _read_release_record(root, package, issues)
    cset = _read_package_ucm(root, package, issues)
    if release is not None:
        if release.id != package.release_id or release.identity.id != package.release_identity:
            issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "RELEASE", "Package release identity does not match release record."))
        expected_root_id = _release_root_id(release.snapshot, release.scope, release.policy,
                                            release.evidence, release.dependencies, release.supersedes_release_id)
        expected_identity = _release_identity(release.snapshot, release.scope, release.policy,
                                              release.evidence, release.dependencies)
        if release.release_root_id != expected_root_id or release.identity.id != expected_identity:
            issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "RELEASE", "Release record identity is not bound to its evidence baseline."))
        if (release.identity.ucm_content_identity != release.snapshot.ucm_content_identity
                or release.identity.ucm_semantic_identity != release.snapshot.ucm_semantic_identity
                or release.identity.review_id != release.snapshot.review_id
                or release.identity.scope_identity != _identity(release.scope.to_dict())):
            issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "RELEASE", "Release identity summary differs from its snapshot or exact scope."))
        if release.snapshot.to_dict() != package.snapshot.to_dict() or release.scope.to_dict() != package.scope.to_dict():
            issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "RELEASE", "Release record snapshot or scope differs from package manifest."))
        if tuple(sorted(release.dependencies, key=lambda item: (item.kind, item.identity))) != package.dependencies:
            issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "RELEASE", "Release dependencies differ from package manifest."))
        if release.status == ReleaseStatus.REVOKED:
            issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "RELEASE", "Package contains an explicitly revoked release record."))
        elif release.status not in {ReleaseStatus.RELEASED, ReleaseStatus.RELEASED_WITH_WARNINGS}:
            issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "RELEASE", "Package release record is not explicitly released."))
    if cset is not None:
        if stable_hash_cset(cset) != package.snapshot.ucm_content_identity:
            issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "UCM", "Packaged UCM content identity differs from manifest."))
        if _semantic_ucm_identity(cset) != package.snapshot.ucm_semantic_identity:
            issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "UCM", "Packaged UCM semantic identity differs from manifest."))
        if tuple(sorted(cset.constraints)) != package.snapshot.constraint_ids:
            issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "UCM", "Packaged UCM constraint IDs differ from manifest."))
        if tuple(sorted(cset.scenarios)) != package.snapshot.scenario_ids:
            issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "MCMM_SCOPE", "Packaged UCM scenario IDs differ from manifest."))
    _verify_optional_evidence(root, package, issues)
    status = PackageVerificationStatus.VERIFIED
    if any(item.severity == ReleaseIssueSeverity.BLOCKER for item in issues):
        status = PackageVerificationStatus.INVALID
    return ReleaseVerification(
        package_id=package.id,
        release_id=package.release_id,
        snapshot_identity=package.snapshot.ucm_content_identity,
        status=status,
        verified_artifact_ids=tuple(sorted(verified)),
        issues=tuple(_sorted_issues(issues)),
    )


@dataclass(frozen=True)
class ConstraintReleaseEngine:
    """Stateless façade; it retains no release history or package authority."""

    def assess(self, cset: ConstraintSet, **kwargs: Any) -> ReleaseAssessment:
        return assess_constraint_release(cset, **kwargs)

    def create(self, cset: ConstraintSet, **kwargs: Any) -> ConstraintRelease:
        return create_release_candidate(cset, **kwargs)

    def release(self, release: ConstraintRelease, cset: ConstraintSet, **kwargs: Any) -> ConstraintRelease:
        return release_constraint_set(release, cset, **kwargs)

    def verify(self, package_dir: str | Path) -> ReleaseVerification:
        return verify_release_package(package_dir)


def _scope(cset: ConstraintSet, config: Any | None, scenario_ids: Iterable[str], all_active: bool) -> ReleaseScope:
    if config is not None:
        matrix = build_scenario_matrix(config, cset)
        active = tuple(sorted(matrix.active_ids))
        definitions = matrix.summary().get("active_scenarios", [])
    else:
        active = tuple(sorted(sid for sid, scenario in cset.scenarios.items()
                              if scenario is not None and getattr(scenario, "active", True)))
        definitions = [_as_dict(item) for _, item in sorted(cset.scenarios.items())]
    requested = _strings(scenario_ids)
    known = set(cset.scenarios) | set(active)
    unknown = tuple(sorted(set(requested) - known))
    if all_active and requested:
        unknown = tuple(sorted(set(unknown) | {"<all-active-with-selected-scenarios>"}))
    if all_active:
        kind, released = "ALL_ACTIVE_SCENARIOS", active
    elif requested:
        kind, released = "SELECTED_SCENARIOS", tuple(sorted(set(requested) & set(active)))
    else:
        kind, released = "GLOBAL", active
    return ReleaseScope(
        scope_kind=kind, requested_scenario_ids=requested, active_scenario_ids=active,
        released_scenario_ids=released, unknown_scenario_ids=unknown,
        scenario_definition_identity=_identity(definitions),
    )


def _snapshot(cset: ConstraintSet, config: Any | None, design: Any | None, timing_graph: Any | None,
              review: ConstraintReview | ConstraintReviewAssessment | None,
              readiness: ConstraintReadinessReport | None, validation: ValidationResult | None,
              lineage: ConstraintLineageReport | None, formal_results: Iterable[Any]) -> ReleaseSnapshot:
    review_record = _review_record(review) if review is not None else None
    formal_results = tuple(formal_results)
    coverage = getattr(validation, "coverage", None) if validation is not None else None
    return ReleaseSnapshot(
        ucm_content_identity=stable_hash_cset(cset),
        ucm_semantic_identity=_semantic_ucm_identity(cset),
        review_id=review_record.id if review_record is not None else "",
        review_snapshot_identity=(review_record.reviewed_snapshot.ucm_content_identity if review_record is not None else ""),
        readiness_identity=_identity(readiness.to_dict()) if readiness is not None else "",
        validation_identity=_identity(validation.as_dict()) if validation is not None else "",
        coverage_identity=_identity(coverage.as_dict()) if coverage is not None else "",
        lineage_snapshot_identity=lineage.snapshot.snapshot_identity if lineage is not None else "",
        lineage_identity=_identity(lineage.to_dict()) if lineage is not None else "",
        formal_identity=_identity([_as_dict(item) for item in formal_results]) if formal_results else "",
        configuration_identity=_identity(_as_dict(config)) if config is not None else "",
        design_identity=_identity(_as_dict(design)) if design is not None else "",
        timing_graph_identity=_identity(_as_dict(timing_graph)) if timing_graph is not None else "",
        constraint_ids=tuple(sorted(cset.constraints)), scenario_ids=tuple(sorted(cset.scenarios)),
    )


def _evidence_and_gates(cset: ConstraintSet, snapshot: ReleaseSnapshot, scope: ReleaseScope, policy: ReleasePolicy,
                        review: ConstraintReview | ConstraintReviewAssessment | None,
                        readiness: ConstraintReadinessReport | None, validation: ValidationResult | None,
                        lineage: ConstraintLineageReport | None, formal_results: Iterable[Any],
                        sdc_path: str | Path | None, additional_artifacts: Mapping[str, str | Path] | None
                        ) -> tuple[tuple[ReleaseEvidence, ...], tuple[ReleaseDependency, ...], tuple[ReleaseIssue, ...]]:
    evidence: list[ReleaseEvidence] = []
    dependencies: list[ReleaseDependency] = []
    issues: list[ReleaseIssue] = []

    def add_evidence(authority: str, reference: str, status: str, *, required: bool = False,
                     stale: bool = False, details: Mapping[str, Any] | None = None) -> ReleaseEvidence:
        item = _evidence(authority, reference, status, required=required, stale=stale, details=details)
        evidence.append(item)
        return item

    def issue(severity: ReleaseIssueSeverity, category: str, message: str,
              refs: Iterable[str] = (), scenario_id: str | None = None) -> None:
        issues.append(_issue(severity, category, message, refs, scenario_id))

    ucm_ref = add_evidence("CANONICAL_UCM", snapshot.ucm_content_identity, "CURRENT", required=True,
                           details={"constraint_ids": list(snapshot.constraint_ids), "scenario_ids": list(snapshot.scenario_ids),
                                    "semantic_identity": snapshot.ucm_semantic_identity or "UNKNOWN"})
    dependencies.append(ReleaseDependency("canonical_ucm", snapshot.ucm_content_identity, True,
                                          "Existing canonical UCM content identity."))
    if not snapshot.ucm_semantic_identity:
        issue(ReleaseIssueSeverity.WARNING, "UCM_SEMANTICS",
              "UCM has unsupported semantics; exact content is bound but semantic equivalence is unknown.", (ucm_ref.id,))

    scope_ref = add_evidence("MCMM_SCOPE", scope.scenario_definition_identity, "CURRENT", required=True,
                             details=scope.to_dict())
    dependencies.append(ReleaseDependency("mcmm_scope", scope.scenario_definition_identity, True,
                                          "Existing canonical/configured active scenario definition."))
    if scope.unknown_scenario_ids:
        issue(ReleaseIssueSeverity.BLOCKER, "MCMM_SCOPE",
              "Requested release scope contains unknown or conflicting scenario selection.", (scope_ref.id,))
    elif policy.require_all_active_scenarios and not set(scope.active_scenario_ids).issubset(scope.released_scenario_ids):
        issue(ReleaseIssueSeverity.BLOCKER, "MCMM_SCOPE",
              "ReleasePolicy requires all active MCMM scenarios to be released.", (scope_ref.id,))
    elif scope.scope_kind == "SELECTED_SCENARIOS":
        issue(ReleaseIssueSeverity.INFORMATION, "MCMM_SCOPE",
              "Release is explicitly limited to selected active scenarios.", (scope_ref.id,))

    _context_gate(snapshot, policy, add_evidence, dependencies, issue)
    _review_gate(cset, snapshot, scope, policy, review, add_evidence, dependencies, issue)
    _readiness_gate(cset, snapshot, policy, readiness, add_evidence, dependencies, issue)
    _validation_gate(policy, validation, add_evidence, dependencies, issue)
    _lineage_gate(cset, snapshot, policy, lineage, add_evidence, dependencies, issue)
    _formal_gate(cset, policy, formal_results, add_evidence, dependencies, issue)
    _artifact_gate(policy, sdc_path, additional_artifacts, add_evidence, dependencies, issue)
    return (tuple(sorted(evidence, key=lambda item: item.id)),
            tuple(sorted(dependencies, key=lambda item: (item.kind, item.identity))),
            tuple(_sorted_issues(issues)))


def _context_gate(snapshot, policy, add, dependencies, issue) -> None:
    """Fail closed when a policy-required project/design/timing identity is absent."""
    for category, identity, required, detail in (
        ("CONFIGURATION", snapshot.configuration_identity, policy.require_configuration,
         "Supplied project/configuration identity."),
        ("DESIGN", snapshot.design_identity, policy.require_design,
         "Supplied design-model identity."),
        ("TIMING_GRAPH", snapshot.timing_graph_identity, policy.require_timing_graph,
         "Supplied timing-graph identity."),
    ):
        ref = add(category, identity or "UNSUPPLIED", "CURRENT" if identity else "MISSING", required=required)
        if identity:
            dependencies.append(ReleaseDependency(category.lower(), identity, required, detail))
        if required and not identity:
            issue(ReleaseIssueSeverity.BLOCKER, category,
                  f"ReleasePolicy requires an explicit current {category.lower().replace('_', ' ')} identity.",
                  (ref.id,))


def _review_gate(cset, snapshot, scope, policy, review, add, dependencies, issue) -> None:
    if review is None:
        ref = add("REVIEW", "UNSUPPLIED", "MISSING", required=policy.require_review)
        if policy.require_review:
            issue(ReleaseIssueSeverity.BLOCKER, "REVIEW", "No explicit approved Step-31 review was supplied.", (ref.id,))
        return
    record = _review_record(review)
    status = _review_status(review)
    ref = add("REVIEW", record.id, status, required=policy.require_review,
              stale=status == ConstraintReviewStatus.STALE.value,
              details={"reviewed_ucm_identity": record.reviewed_snapshot.ucm_content_identity,
                       "review_status_at_record": record.status.value})
    dependencies.append(ReleaseDependency("review", record.id, policy.require_review, "Explicit Step-31 review record."))
    if record.reviewed_snapshot.ucm_content_identity != snapshot.ucm_content_identity:
        issue(ReleaseIssueSeverity.BLOCKER, "REVIEW", "Review is bound to a different canonical UCM snapshot.", (ref.id,))
    elif not set(scope.released_scenario_ids).issubset(set(record.scope.reviewed_scenario_ids)):
        issue(ReleaseIssueSeverity.BLOCKER, "REVIEW", "Review MCMM scope does not cover the exact release scenario scope.", (ref.id,))
    elif (record.scope.scenario_definition_identity and scope.scenario_definition_identity
          and record.scope.scenario_definition_identity != scope.scenario_definition_identity):
        issue(ReleaseIssueSeverity.BLOCKER, "REVIEW", "Review MCMM scenario definition differs from release scope.", (ref.id,))
    elif status == ConstraintReviewStatus.STALE.value:
        issue(ReleaseIssueSeverity.BLOCKER, "REVIEW", "Review is stale against current supplied evidence.", (ref.id,))
    elif status == ConstraintReviewStatus.REVOKED.value:
        issue(ReleaseIssueSeverity.BLOCKER, "REVIEW", "Review was explicitly revoked.", (ref.id,))
    elif status == ConstraintReviewStatus.APPROVED.value:
        return
    elif status == ConstraintReviewStatus.APPROVED_WITH_WARNINGS.value:
        if policy.allow_review_with_warnings:
            issue(ReleaseIssueSeverity.WARNING, "REVIEW", "Review was approved with warnings.", (ref.id,))
        else:
            issue(ReleaseIssueSeverity.BLOCKER, "REVIEW",
                  "ReleasePolicy does not permit a review approved with warnings.", (ref.id,))
    else:
        issue(ReleaseIssueSeverity.BLOCKER, "REVIEW", f"Review status {status} is not an explicit approval.", (ref.id,))


def _readiness_gate(cset, snapshot, policy, readiness, add, dependencies, issue) -> None:
    if readiness is None:
        ref = add("READINESS", "UNSUPPLIED", "MISSING", required=policy.require_readiness)
        if policy.require_readiness:
            issue(ReleaseIssueSeverity.BLOCKER, "READINESS", "No existing Step-29 readiness report was supplied.", (ref.id,))
        return
    status = readiness.status.value
    readiness_ucm = getattr(readiness, "source_snapshot_identity", {}).get("constraint_set")
    stale = bool(readiness.stale_evidence) or bool(readiness_ucm and readiness_ucm != snapshot.ucm_content_identity)
    ref = add("READINESS", _identity(readiness.to_dict()), status, required=policy.require_readiness, stale=stale,
              details={"blocker_ids": sorted(item.id for item in readiness.blockers),
                       "warning_ids": sorted(item.id for item in readiness.warnings),
                       "source_snapshot_identity": readiness_ucm or None})
    dependencies.append(ReleaseDependency("readiness", ref.reference_id, policy.require_readiness,
                                          "Existing Step-29 readiness evidence."))
    if stale:
        issue(ReleaseIssueSeverity.BLOCKER, "READINESS", "Readiness evidence is stale or references another UCM.", (ref.id,))
    elif policy.require_readiness and status not in set(policy.required_readiness_statuses):
        _unresolved(policy, "READINESS", f"Readiness status {status} is not allowed by ReleasePolicy.", ref.id, issue)
    if readiness.blockers:
        issue(ReleaseIssueSeverity.BLOCKER, "READINESS", "Readiness evidence contains unresolved blockers.", (ref.id,))
    if readiness.warnings:
        issue(ReleaseIssueSeverity.WARNING, "READINESS", "Readiness evidence contains warnings.", (ref.id,))


def _validation_gate(policy, validation, add, dependencies, issue) -> None:
    if validation is None:
        ref = add("VALIDATION", "UNSUPPLIED", "MISSING", required=policy.require_validation_evidence)
        if policy.require_validation_evidence:
            issue(ReleaseIssueSeverity.BLOCKER, "VALIDATION", "No existing validation result was supplied.", (ref.id,))
        if policy.require_complete_coverage:
            cref = add("COVERAGE", "UNSUPPLIED", "MISSING", required=True)
            issue(ReleaseIssueSeverity.BLOCKER, "COVERAGE", "No existing coverage result was supplied.", (cref.id,))
        return
    status = str(validation.status or "UNKNOWN")
    ref = add("VALIDATION", _identity(validation.as_dict()), status, required=policy.require_validation_evidence,
              details={"issue_ids": _validation_issue_ids(validation)})
    dependencies.append(ReleaseDependency("validation", ref.reference_id, policy.require_validation_evidence,
                                          "Existing validation evidence."))
    normalized = status.upper()
    if normalized in {"PASS", "READY", "VALID", "COMPLETE"}:
        pass
    elif normalized in {"PASS_WITH_WARNINGS", "READY_WITH_WARNINGS", "WARNING", "WARNINGS"}:
        issue(ReleaseIssueSeverity.WARNING, "VALIDATION", "Validation result contains warnings.", (ref.id,))
    elif policy.require_validation_evidence:
        _unresolved(policy, "VALIDATION", f"Validation status {status} is not release-complete.", ref.id, issue)
    for raw in getattr(validation.report, "issues", ()):
        issue_id = str(getattr(raw, "issue_id", "") or _identity(_as_dict(raw)))
        severity = _value(getattr(raw, "severity", ""))
        blocking = bool(getattr(raw, "blocking", False)) or severity in {"CRITICAL", "ERROR", "HIGH"}
        issue_ref = add("VALIDATION_ISSUE", issue_id, severity or "UNKNOWN",
                        details={"issue": _as_dict(raw)})
        if blocking:
            issue(ReleaseIssueSeverity.BLOCKER, "VALIDATION", f"Validation reports blocking issue {issue_id}.",
                  (ref.id, issue_ref.id))
        elif severity in {"WARNING", "MEDIUM"}:
            issue(ReleaseIssueSeverity.WARNING, "VALIDATION", f"Validation reports warning issue {issue_id}.",
                  (ref.id, issue_ref.id))
    coverage = getattr(validation, "coverage", None)
    if coverage is None:
        cref = add("COVERAGE", "UNSUPPLIED", "MISSING", required=policy.require_complete_coverage)
        if policy.require_complete_coverage:
            issue(ReleaseIssueSeverity.BLOCKER, "COVERAGE", "Validation result has no coverage evidence.", (cref.id,))
        return
    data = coverage.as_dict() if hasattr(coverage, "as_dict") else _as_dict(coverage)
    complete, reason = _coverage_complete(data)
    cref = add("COVERAGE", _identity(data), "COMPLETE" if complete else "INCOMPLETE",
               required=policy.require_complete_coverage, details={"coverage": data})
    dependencies.append(ReleaseDependency("coverage", cref.reference_id, policy.require_complete_coverage,
                                          "Existing validation coverage evidence."))
    if not complete:
        severity = ReleaseIssueSeverity.BLOCKER if policy.require_complete_coverage else ReleaseIssueSeverity.WARNING
        issue(severity, "COVERAGE", f"Coverage is incomplete or unknown: {reason}", (cref.id,))


def _lineage_gate(cset, snapshot, policy, lineage, add, dependencies, issue) -> None:
    if lineage is None:
        ref = add("LINEAGE", "UNSUPPLIED", "MISSING", required=policy.require_lineage)
        if policy.require_lineage:
            issue(ReleaseIssueSeverity.BLOCKER, "LINEAGE", "No Step-30 lineage report was supplied.", (ref.id,))
        return
    matches = lineage.snapshot.snapshot_identity == snapshot.ucm_content_identity
    stale = not matches or any(event.stale for entry in lineage.constraints
                               for event in (*entry.events, *entry.validation_events, *entry.formal_events))
    ref = add("LINEAGE", _identity(lineage.to_dict()), "CURRENT" if matches else "STALE",
              required=policy.require_lineage, stale=stale,
              details={"lineage_snapshot_identity": lineage.snapshot.snapshot_identity,
                       "constraint_ids": sorted(item.constraint_id for item in lineage.constraints)})
    dependencies.append(ReleaseDependency("lineage", ref.reference_id, policy.require_lineage,
                                          "Existing Step-30 lineage report."))
    if not matches:
        issue(ReleaseIssueSeverity.BLOCKER, "LINEAGE", "Lineage report is bound to another UCM snapshot.", (ref.id,))
    elif stale:
        issue(ReleaseIssueSeverity.WARNING, "LINEAGE", "Lineage retains stale source evidence.", (ref.id,))


def _formal_gate(cset, policy, formal_results, add, dependencies, issue) -> None:
    results: dict[str, str] = {}
    for raw in formal_results:
        data = _as_dict(getattr(raw, "verification", raw))
        constraint_id = str(data.get("constraint_id") or getattr(raw, "constraint_id", "") or "")
        status = _value(data.get("status")) or "UNKNOWN"
        reference = constraint_id or _identity(data)
        ref = add("FORMAL", reference, status, required=False, details={"formal_result": data})
        dependencies.append(ReleaseDependency("formal", _identity(data), False, "Supplied existing formal evidence."))
        if constraint_id:
            results[constraint_id] = status
        if status.upper() in {"FAILED", "INVALID", "ERROR"}:
            issue(ReleaseIssueSeverity.BLOCKER, "FORMAL", f"Formal evidence for {reference} reports {status}.", (ref.id,))
        elif status.upper() in {"UNRESOLVED", "UNVERIFIED", "UNKNOWN", "UNSUPPORTED"}:
            issue(ReleaseIssueSeverity.WARNING, "FORMAL", f"Formal evidence for {reference} remains {status}.", (ref.id,))
    for constraint in cset:
        ctype = _value(getattr(constraint, "type", ""))
        if ctype not in set(policy.require_formal_for_constraint_types):
            continue
        status = results.get(constraint.id, "MISSING")
        if status.upper() not in {"VERIFIED", "PASS", "PROVEN"}:
            ref = add("FORMAL", constraint.id, status, required=True,
                      details={"required_constraint_type": ctype})
            _unresolved(policy, "FORMAL",
                        f"ReleasePolicy requires verified formal evidence for {constraint.id} ({ctype}); status is {status}.",
                        ref.id, issue)


def _artifact_gate(policy, sdc_path, additional, add, dependencies, issue) -> None:
    seen: set[str] = set()
    if sdc_path is None:
        ref = add("SDC", "UNSUPPLIED", "MISSING", required=policy.require_sdc)
        if policy.require_sdc:
            issue(ReleaseIssueSeverity.BLOCKER, "SDC", "ReleasePolicy requires an explicitly supplied SDC artifact.", (ref.id,))
    else:
        path = Path(sdc_path)
        if not path.is_file():
            ref = add("SDC", "UNREADABLE", "INVALID", required=policy.require_sdc)
            issue(ReleaseIssueSeverity.BLOCKER, "SDC", "Explicit supplied SDC artifact is not readable.", (ref.id,))
        else:
            digest = hash_file(path)
            ref = add("SDC", digest, "SUPPLIED", required=policy.require_sdc,
                      details={"sha256": digest, "size_bytes": path.stat().st_size})
            dependencies.append(ReleaseDependency("sdc", digest, policy.require_sdc,
                                                  "Explicit supplied SDC artifact; never generated by release."))
            seen.add("sdc")
    for kind, raw in sorted((additional or {}).items(), key=lambda item: str(item[0])):
        path = Path(raw)
        label = str(kind)
        if not path.is_file():
            ref = add(f"ARTIFACT:{label}", "UNREADABLE", "INVALID", required=label in policy.required_artifact_kinds)
            if label in policy.required_artifact_kinds:
                issue(ReleaseIssueSeverity.BLOCKER, "ARTIFACT", f"Required supplied artifact {label!r} is not readable.", (ref.id,))
            continue
        digest = hash_file(path)
        ref = add(f"ARTIFACT:{label}", digest, "SUPPLIED", required=label in policy.required_artifact_kinds,
                  details={"sha256": digest, "size_bytes": path.stat().st_size})
        dependencies.append(ReleaseDependency(f"artifact:{label}", digest, label in policy.required_artifact_kinds,
                                              "Explicit supplied release artifact."))
        seen.add(label)
    for required in set(policy.required_artifact_kinds) - seen:
        ref = add(f"ARTIFACT:{required}", "UNSUPPLIED", "MISSING", required=True)
        issue(ReleaseIssueSeverity.BLOCKER, "ARTIFACT", f"ReleasePolicy requires artifact kind {required!r}.", (ref.id,))


def _candidate_status(scope: ReleaseScope, snapshot: ReleaseSnapshot, issues: Iterable[ReleaseIssue]) -> ReleaseStatus:
    if scope.unknown_scenario_ids:
        return ReleaseStatus.INVALID
    if any(item.severity == ReleaseIssueSeverity.BLOCKER for item in issues):
        return ReleaseStatus.BLOCKED
    if not snapshot.ucm_semantic_identity:
        return ReleaseStatus.UNKNOWN
    if any(item.severity == ReleaseIssueSeverity.WARNING for item in issues):
        return ReleaseStatus.CANDIDATE
    return ReleaseStatus.READY


def _staleness_reasons(release: ConstraintRelease, current: ReleaseSnapshot, scope: ReleaseScope,
                       review, readiness, lineage) -> tuple[str, ...]:
    prior = release.snapshot
    reasons: list[str] = []
    if prior.ucm_content_identity != current.ucm_content_identity:
        reasons.append("Canonical UCM content identity differs from release baseline.")
    if prior.ucm_semantic_identity != current.ucm_semantic_identity:
        reasons.append("Canonical UCM semantic identity differs from release baseline or is unsupported.")
    if prior.scenario_ids != current.scenario_ids:
        reasons.append("Canonical UCM scenario IDs differ from release baseline.")
    if release.scope.scenario_definition_identity != scope.scenario_definition_identity:
        reasons.append("MCMM scenario definition differs from release baseline.")
    for name, old, new in (
        ("configuration", prior.configuration_identity, current.configuration_identity),
        ("design", prior.design_identity, current.design_identity),
        ("timing graph", prior.timing_graph_identity, current.timing_graph_identity),
        ("review", prior.review_id, current.review_id),
        ("readiness", prior.readiness_identity, current.readiness_identity),
        ("validation", prior.validation_identity, current.validation_identity),
        ("coverage", prior.coverage_identity, current.coverage_identity),
        ("lineage", prior.lineage_identity, current.lineage_identity),
        ("formal", prior.formal_identity, current.formal_identity),
    ):
        if old and new and old != new:
            reasons.append(f"Supplied {name} identity differs from release baseline.")
    return tuple(sorted(set(reasons)))


def _read_release_record(root: Path, package: ReleasePackage, issues: list[ReleaseIssue]) -> ConstraintRelease | None:
    path = root / "release.json"
    try:
        return ConstraintRelease.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "RELEASE", f"Cannot read packaged release record: {exc}"))
        return None


def _read_package_ucm(root: Path, package: ReleasePackage, issues: list[ReleaseIssue]) -> ConstraintSet | None:
    path = root / "ucm_snapshot.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise TypeError("UCM snapshot must be a JSON object")
        return ConstraintSet.from_snapshot_dict(dict(payload), unknown_field_policy="error")
    except (OSError, ValueError, TypeError, json.JSONDecodeError, SnapshotFormatError) as exc:
        issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "UCM", f"Cannot read packaged canonical UCM: {exc}"))
        return None


def _verify_optional_evidence(root: Path, package: ReleasePackage, issues: list[ReleaseIssue]) -> None:
    expected = package.snapshot
    names = {
        "review.json": ("review", expected.review_id),
        "readiness.json": ("readiness", expected.readiness_identity),
        "validation.json": ("validation", expected.validation_identity),
        "coverage.json": ("coverage", expected.coverage_identity),
        "lineage.json": ("lineage", expected.lineage_identity),
        "formal.json": ("formal", expected.formal_identity),
    }
    for filename, (category, identity) in names.items():
        path = root / filename
        if not identity:
            continue
        if not path.is_file():
            issues.append(_issue(ReleaseIssueSeverity.BLOCKER, category.upper(),
                                 f"Package lacks identity-bound {filename}."))
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(_issue(ReleaseIssueSeverity.BLOCKER, category.upper(),
                                 f"Cannot parse packaged {filename}: {exc}"))
            continue
        if category == "review":
            try:
                record = ConstraintReview.from_dict(payload)
                actual = record.id
                if record.status in {ConstraintReviewStatus.REVOKED, ConstraintReviewStatus.STALE}:
                    issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "REVIEW",
                                         "Packaged review is revoked or stale."))
            except (TypeError, ValueError) as exc:
                issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "REVIEW",
                                     f"Cannot restore packaged review: {exc}"))
                continue
        else:
            actual = _identity(payload)
        if actual != identity:
            issues.append(_issue(ReleaseIssueSeverity.BLOCKER, category.upper(),
                                 f"Packaged {filename} identity differs from release snapshot."))
    if expected.lineage_snapshot_identity:
        path = root / "lineage.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            actual_snapshot = str(payload.get("snapshot", {}).get("snapshot_identity", ""))
            if actual_snapshot != expected.lineage_snapshot_identity:
                issues.append(_issue(ReleaseIssueSeverity.BLOCKER, "LINEAGE",
                                     "Packaged lineage snapshot identity differs from release snapshot."))
        except (OSError, TypeError, json.JSONDecodeError):
            pass


def _artifact(kind: str, relative: str, path: Path, *, required: bool) -> ReleaseArtifact:
    digest = hash_file(path)
    size = path.stat().st_size
    return ReleaseArtifact(
        id="RPA-" + stable_hash({"kind": kind, "relative_path": relative, "sha256": digest,
                                  "size_bytes": size, "required": required})[:20],
        kind=kind, relative_path=relative, sha256=digest, size_bytes=size, required=required,
    )


def _package_id(release: ConstraintRelease, artifacts: tuple[ReleaseArtifact, ...]) -> str:
    return _package_id_values(release.id, release.identity.id, release.snapshot, release.scope,
                              release.dependencies, artifacts)


def _package_id_values(release_id: str, release_identity: str, snapshot: ReleaseSnapshot,
                       scope: ReleaseScope, dependencies: Iterable[ReleaseDependency],
                       artifacts: Iterable[ReleaseArtifact]) -> str:
    return "RPK-" + stable_hash({
        "release_id": release_id, "release_identity": release_identity,
        "snapshot": snapshot.to_dict(), "scope": scope.to_dict(),
        "dependencies": [item.to_dict() for item in sorted(dependencies, key=lambda item: (item.kind, item.identity))],
        "artifacts": [item.to_dict() for item in sorted(artifacts, key=lambda item: item.relative_path)],
    })[:20]


def _release_root_id(snapshot: ReleaseSnapshot, scope: ReleaseScope, policy: ReleasePolicy,
                     evidence: Iterable[ReleaseEvidence], dependencies: Iterable[ReleaseDependency],
                     supersedes_release_id: str | None) -> str:
    return "REL-" + stable_hash({
        "snapshot": snapshot.to_dict(), "scope": scope.to_dict(), "policy": policy.to_dict(),
        "evidence": [item.to_dict() for item in sorted(evidence, key=lambda item: item.id)],
        "dependencies": [item.to_dict() for item in sorted(dependencies, key=lambda item: (item.kind, item.identity))],
        "supersedes_release_id": supersedes_release_id,
    })[:20]


def _release_identity(snapshot: ReleaseSnapshot, scope: ReleaseScope, policy: ReleasePolicy,
                      evidence: Iterable[ReleaseEvidence], dependencies: Iterable[ReleaseDependency]) -> str:
    """Stable baseline identity over all consumed evidence, never action time/host data."""
    return "RID-" + stable_hash({
        "snapshot": snapshot.to_dict(), "scope": scope.to_dict(), "policy": policy.to_dict(),
        "evidence": [item.to_dict() for item in sorted(evidence, key=lambda item: item.id)],
        "dependencies": [item.to_dict() for item in sorted(dependencies, key=lambda item: (item.kind, item.identity))],
    })[:20]


def _decision(root_id: str, kind: ReleaseDecisionKind, actor: ReviewActor, comment: str,
              evidence_ids: tuple[str, ...], recorded_at: str | None) -> ReleaseDecision:
    identity = {"release_root_id": root_id, "kind": kind.value, "actor": actor.to_dict(),
                "comment": comment, "evidence_ids": list(evidence_ids)}
    return ReleaseDecision(id="RDC-" + stable_hash(identity)[:20], kind=kind, actor=actor,
                           comment=comment, release_root_id=root_id, evidence_ids=evidence_ids,
                           recorded_at=recorded_at)


def _decision_release_id(release: ConstraintRelease, decision: ReleaseDecision) -> str:
    return "REL-" + stable_hash({"release_root_id": release.release_root_id,
                                  "previous_release_id": release.id, "decision_id": decision.id,
                                  "history": [item.id for item in release.decision_history] + [decision.id]})[:20]


def _evidence(authority: str, reference: str, status: str, *, required: bool, stale: bool,
              details: Mapping[str, Any] | None) -> ReleaseEvidence:
    payload = {"authority": authority, "reference_id": reference, "status": status,
               "required": required, "stale": stale, "details": _identity_value(details or {})}
    return ReleaseEvidence(id="RLE-" + stable_hash(payload)[:20], authority=authority, reference_id=reference,
                           status=status, required=required, stale=stale, details=dict(details or {}))


def _issue(severity: ReleaseIssueSeverity, category: str, message: str,
           evidence_ids: Iterable[str] = (), scenario_id: str | None = None) -> ReleaseIssue:
    ids = tuple(sorted({str(item) for item in evidence_ids if item}))
    payload = {"severity": severity.value, "category": category, "message": message,
               "evidence_ids": list(ids), "scenario_id": scenario_id}
    return ReleaseIssue(id="RLI-" + stable_hash(payload)[:20], severity=severity, category=category,
                        message=message, evidence_ids=ids, scenario_id=scenario_id)


def _unresolved(policy: ReleasePolicy, category: str, message: str, evidence_id: str, issue) -> None:
    severity = (ReleaseIssueSeverity.WARNING if category in set(policy.allowed_unresolved_evidence_categories)
                else ReleaseIssueSeverity.BLOCKER)
    issue(severity, category, message, (evidence_id,))


def _coverage_complete(data: Mapping[str, Any]) -> tuple[bool, str]:
    if not data.get("graph_available", False):
        return False, "coverage graph is unavailable"
    if data.get("uncovered"):
        return False, "uncovered paths or objects are retained"
    metrics = [value for key, value in data.items() if str(key).endswith("_coverage_pct")]
    if any(value == "UNKNOWN" or value is None for value in metrics):
        return False, "one or more coverage metrics are unknown"
    if any(isinstance(value, (int, float)) and value < 100 for value in metrics):
        return False, "one or more coverage metrics are below 100 percent"
    return True, "complete"


def _semantic_ucm_identity(cset: ConstraintSet) -> str:
    constraints = list(cset)
    if any(has_unsupported_options(item) for item in constraints):
        return ""
    return stable_hash([{"id": item.id, "semantic": normalize_constraint(item)}
                        for item in sorted(constraints, key=lambda item: item.id)])


def _review_record(value: ConstraintReview | ConstraintReviewAssessment) -> ConstraintReview:
    return value.review if isinstance(value, ConstraintReviewAssessment) else value


def _review_status(value: ConstraintReview | ConstraintReviewAssessment) -> str:
    status = value.current_status if isinstance(value, ConstraintReviewAssessment) else value.status
    return status.value


def _validation_issue_ids(validation: ValidationResult) -> list[str]:
    return sorted(str(getattr(item, "issue_id", "") or _identity(_as_dict(item)))
                  for item in getattr(validation.report, "issues", ()))


def _evidence_ids(evidence: Iterable[ReleaseEvidence]) -> tuple[str, ...]:
    return tuple(sorted(item.id for item in evidence))


def _release_failure(assessment: ReleaseAssessment) -> str:
    messages = [item.message for item in assessment.issues if item.severity == ReleaseIssueSeverity.BLOCKER]
    if not messages:
        messages = ["Warnings require ReleasePolicy.allow_release_with_warnings."]
    return "Cannot release: " + " ".join(messages)


def _copy_file_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush(); os.fsync(output.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _safe_filename(name: str) -> str:
    value = Path(name).name.replace("..", "_")
    return value or "artifact"


def _safe_package_path(root: Path, relative: str) -> Path | None:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _strings(values: Iterable[Any] | Any) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return ()
    return tuple(sorted({str(item) for item in values if item}))


def _sorted_issues(values: Iterable[ReleaseIssue]) -> list[ReleaseIssue]:
    order = {ReleaseIssueSeverity.BLOCKER: 0, ReleaseIssueSeverity.WARNING: 1,
             ReleaseIssueSeverity.INFORMATION: 2}
    return sorted(values, key=lambda item: (order[item.severity], item.category, item.scenario_id or "", item.id))


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


__all__ = [
    "ConstraintReleaseEngine", "ReleaseDecisionError", "ReleasePackageError", "assess_constraint_release",
    "create_release_candidate", "create_release_package", "release_constraint_set", "revoke_release",
    "supersede_release", "verify_release_package",
]
