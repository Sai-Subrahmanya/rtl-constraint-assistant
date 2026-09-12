"""Controlled, explicit application of advisory inference candidates.

This module is intentionally a small orchestration layer over the existing
UCM, semantic normalization, provenance, validation, and MCMM machinery. It
does not persist a second constraint model or validation result. Advisory
objects become ordinary UCM constraints only after an explicit decision has
passed every check on an isolated :class:`ConstraintSet` projection.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..constraint_model import Constraint, ConstraintSet, stable_hash_cset
from ..equivalence.normalize import (
    has_unsupported_options,
    normalize_constraint,
    semantic_match_key,
)
from ..mcmm import ScenarioMatrix, build_scenario_matrix
from ..provenance import Evidence
from ..timing_model import TimingGraph
from ..utils.enums import ConstraintStatus, SourceKind
from ..utils.hashing import stable_hash
from ..validation import validate as run_validation
from .rules import InferenceCandidate, InferenceDecision, InferenceStatus

# Application receipts need stable provenance just like advisory candidates;
# receipt identity is a separate deterministic hash, never a timestamp.
_APPLICATION_EPOCH = "1970-01-01T00:00:00+00:00"


class IntentDecisionKind(str, Enum):
    """An explicit caller decision, intentionally separate from candidate status."""

    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    DEFER = "DEFER"
    CONFIRM = "CONFIRM"
    ALREADY_SATISFIED = "ALREADY_SATISFIED"
    CONFLICT = "CONFLICT"
    INVALID = "INVALID"


class ApplicationStatus(str, Enum):
    """Outcome of an application request, never a UCM lifecycle state."""

    NOT_APPLIED = "NOT_APPLIED"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"
    BLOCKED = "BLOCKED"
    ALREADY_PRESENT = "ALREADY_PRESENT"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    STALE = "STALE"


# The canonical immutable evidence model remains the only evidence format.
ApplicationEvidence = Evidence


@dataclass(frozen=True)
class IntentDecision:
    """A deliberate decision tied to exactly one advisory candidate ID."""

    candidate_id: str
    kind: IntentDecisionKind
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind.value,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class ConstraintApplication:
    """Explicit application request for one advisory candidate.

    ``scenario_ids`` states the requested scope; it is checked against the
    candidate templates and active MCMM matrix and is never used to broaden a
    template's scope. ``dry_run`` runs the same isolated validation path but
    never commits to the caller-owned UCM.
    """

    candidate: InferenceCandidate
    decision: IntentDecision
    scenario_ids: tuple[str, ...] = ()
    dry_run: bool = False


@dataclass(frozen=True)
class ConstraintApplicationResult:
    """Deterministic receipt for a controlled UCM application attempt."""

    application_id: str
    candidate_id: str
    candidate_semantic_identity: str | None
    decision: IntentDecisionKind
    status: ApplicationStatus
    ucm_mutated: bool
    applied_constraint_ids: tuple[str, ...] = ()
    already_present_ids: tuple[str, ...] = ()
    conflict_ids: tuple[str, ...] = ()
    rejected_constraint_ids: tuple[str, ...] = ()
    validation_status: str | None = None
    validation_summary: dict[str, Any] | None = None
    validation_issues: tuple[dict[str, Any], ...] = ()
    candidate_provenance: dict[str, Any] | None = None
    source_snapshot_identity: dict[str, str] | None = None
    application_evidence: tuple[ApplicationEvidence, ...] = ()
    blocking_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    scenario_ids: tuple[str, ...] = ()
    ucm_before_snapshot_identity: str = ""
    ucm_after_snapshot_identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "candidate_id": self.candidate_id,
            "candidate_semantic_identity": self.candidate_semantic_identity,
            "decision": self.decision.value,
            "status": self.status.value,
            "ucm_mutated": self.ucm_mutated,
            "applied_constraint_ids": sorted(self.applied_constraint_ids),
            "already_present_ids": sorted(self.already_present_ids),
            "conflict_ids": sorted(self.conflict_ids),
            "rejected_constraint_ids": sorted(self.rejected_constraint_ids),
            "validation_status": self.validation_status,
            "validation_summary": (dict(self.validation_summary)
                                   if self.validation_summary is not None else None),
            "validation_issues": [dict(item) for item in self.validation_issues],
            "candidate_provenance": (dict(self.candidate_provenance)
                                     if self.candidate_provenance is not None else None),
            "source_snapshot_identity": (dict(sorted(self.source_snapshot_identity.items()))
                                         if self.source_snapshot_identity is not None else None),
            "application_evidence": [item.to_dict() for item in sorted(
                self.application_evidence, key=lambda item: item.id,
            )],
            "blocking_reasons": sorted(self.blocking_reasons),
            "warnings": sorted(self.warnings),
            "scenario_ids": sorted(self.scenario_ids),
            "ucm_before_snapshot_identity": self.ucm_before_snapshot_identity,
            "ucm_after_snapshot_identity": self.ucm_after_snapshot_identity,
        }


def candidate_semantic_identity(candidate: InferenceCandidate) -> str | None:
    """Return the canonical semantic digest of all candidate templates.

    ``None`` means the proposal is incomplete, malformed, or semantically
    unsupported. Each record combines its Step-9 semantic normalization with
    the lossless canonical template hash. That binds application-relevant
    source, dependency, assumption, and provenance links as well as timing
    semantics, so they cannot be silently stripped between review and commit.
    """
    templates = _templates_for(candidate)
    if not templates:
        return None
    try:
        constraints = [Constraint.from_canonical_dict(copy.deepcopy(template), unknown_field_policy="error")
                       for template in templates]
    except (TypeError, ValueError):
        return None
    if any(has_unsupported_options(constraint) for constraint in constraints):
        return None
    records = tuple(sorted(
        (normalize_constraint(constraint), stable_hash(constraint.to_canonical_dict()))
        for constraint in constraints
    ))
    return stable_hash(records)


def apply_intent_decision(
    application: ConstraintApplication,
    cset: ConstraintSet,
    design: Any,
    tg: TimingGraph,
    config: Any | None = None,
    *,
    _retry_duplicates_before_stale: bool = True,
) -> ConstraintApplicationResult:
    """Apply a candidate atomically only after an explicit decision and validation.

    The caller-owned ``cset`` is never touched until a fully populated clone
    has passed established validation. The private retry flag exists solely for
    the Step-27 compatibility adapter; public callers get idempotent duplicate
    reporting before a changed UCM snapshot is called stale.
    """
    candidate = application.candidate
    decision = application.decision
    before = stable_hash_cset(cset)
    semantic_identity = candidate_semantic_identity(candidate)
    application_id = _application_id(application, before, semantic_identity)
    requested_scenarios = tuple(sorted(set(application.scenario_ids)))

    def result(status: ApplicationStatus, *, mutated: bool = False,
               applied: tuple[str, ...] = (), present: tuple[str, ...] = (),
               conflicts: tuple[str, ...] = (), rejected: tuple[str, ...] = (),
               validation_status: str | None = None, validation_summary: dict[str, Any] | None = None,
               validation_issues: tuple[dict[str, Any], ...] = (),
               evidence: tuple[Evidence, ...] = (), blocking: tuple[str, ...] = (),
               warnings: tuple[str, ...] = (), after: str | None = None) -> ConstraintApplicationResult:
        return ConstraintApplicationResult(
            application_id=application_id,
            candidate_id=candidate.id,
            candidate_semantic_identity=semantic_identity,
            decision=decision.kind,
            status=status,
            ucm_mutated=mutated,
            applied_constraint_ids=tuple(sorted(applied)),
            already_present_ids=tuple(sorted(present)),
            conflict_ids=tuple(sorted(conflicts)),
            rejected_constraint_ids=tuple(sorted(rejected)),
            validation_status=validation_status,
            validation_summary=(dict(validation_summary) if validation_summary is not None else None),
            validation_issues=tuple(sorted((dict(issue) for issue in validation_issues),
                                           key=lambda issue: issue.get("issue_id", stable_hash(issue)))),
            candidate_provenance=candidate.provenance.to_dict(),
            source_snapshot_identity=dict(sorted(candidate.source_snapshot_identity.items())),
            application_evidence=tuple(sorted(evidence, key=lambda item: item.id)),
            blocking_reasons=tuple(sorted(blocking)),
            warnings=tuple(sorted(warnings)),
            scenario_ids=requested_scenarios,
            ucm_before_snapshot_identity=before,
            ucm_after_snapshot_identity=after if after is not None else before,
        )

    if decision.candidate_id != candidate.id:
        return result(ApplicationStatus.BLOCKED, blocking=(
            "Intent decision candidate ID does not match the supplied candidate.",
        ))
    if decision.kind == IntentDecisionKind.REJECT:
        return result(ApplicationStatus.REJECTED, rejected=_candidate_template_ids(candidate), warnings=(
            "Explicit rejection recorded; advisory candidate was not applied.",
        ))
    if decision.kind == IntentDecisionKind.DEFER:
        return result(ApplicationStatus.DEFERRED, warnings=(
            "Explicit deferral recorded; advisory candidate was not applied.",
        ))
    if decision.kind in {IntentDecisionKind.CONFLICT, IntentDecisionKind.INVALID}:
        return result(ApplicationStatus.REJECTED, blocking=(
            f"Explicit {decision.kind.value} decision cannot alter canonical UCM.",
        ))
    if not candidate.id:
        return result(ApplicationStatus.BLOCKED, blocking=("Candidate has no stable identity.",))
    if semantic_identity is None:
        return result(ApplicationStatus.BLOCKED, blocking=(
            "Candidate has no complete, safely comparable canonical template.",
        ))
    if candidate.candidate_semantic_identity != semantic_identity:
        return result(ApplicationStatus.STALE, blocking=(
            "Candidate semantic identity does not match its canonical template; reinfer before applying.",
        ))
    source = candidate.source_snapshot_identity
    required_snapshot_keys = {"design", "timing_graph", "constraint_set"}
    if not required_snapshot_keys <= set(source):
        return result(ApplicationStatus.STALE, blocking=(
            "Candidate lacks required design, timing-graph, or UCM source snapshot identity.",
        ))
    if design is None or tg is None:
        return result(ApplicationStatus.BLOCKED, blocking=(
            "Design and timing graph are required for controlled application validation.",
        ))
    if stable_hash(design.snapshot()) != source["design"]:
        return result(ApplicationStatus.STALE, blocking=(
            "Design snapshot differs from candidate source; explicit reinference is required.",
        ))
    if stable_hash(tg.model_dump()) != source["timing_graph"]:
        return result(ApplicationStatus.STALE, blocking=(
            "Timing-graph snapshot differs from candidate source; explicit reinference is required.",
        ))
    if config is not None and "config" in source and stable_hash(config.model_dump()) != source["config"]:
        return result(ApplicationStatus.STALE, blocking=(
            "Project configuration differs from candidate source; explicit reinference is required.",
        ))
    if not candidate.evidence and not candidate.provenance.evidence:
        return result(ApplicationStatus.BLOCKED, blocking=(
            "Candidate has no canonical evidence; numerical or semantic intent cannot be applied.",
        ))
    if candidate.missing_information:
        return result(ApplicationStatus.BLOCKED, blocking=(
            "Candidate has unresolved missing information and cannot become UCM intent.",
        ))
    if decision.kind == IntentDecisionKind.ACCEPT:
        if candidate.status != InferenceStatus.INFERRED or candidate.decision != InferenceDecision.ACCEPTABLE:
            conflicts = (tuple(sorted(candidate.existing_constraint_ids))
                         if candidate.status == InferenceStatus.CONFLICTING else ())
            return result(ApplicationStatus.BLOCKED, conflicts=conflicts, blocking=(
                f"Candidate is {candidate.status.value}/{candidate.decision.value}; ACCEPT is unsafe.",
            ))
    elif decision.kind == IntentDecisionKind.CONFIRM:
        if candidate.status not in {InferenceStatus.INFERRED, InferenceStatus.CONFIRMATION_REQUIRED}:
            return result(ApplicationStatus.BLOCKED, blocking=(
                f"Candidate status {candidate.status.value} cannot be confirmed into UCM.",
            ))
        if candidate.decision not in {InferenceDecision.ACCEPTABLE, InferenceDecision.REQUIRES_CONFIRMATION}:
            return result(ApplicationStatus.BLOCKED, blocking=(
                f"Candidate decision {candidate.decision.value} cannot be confirmed into UCM.",
            ))
    elif decision.kind != IntentDecisionKind.ALREADY_SATISFIED:
        return result(ApplicationStatus.BLOCKED, blocking=("Unknown explicit decision.",))

    templates = _templates_for(candidate)
    try:
        proposed = [Constraint.from_canonical_dict(copy.deepcopy(template), unknown_field_policy="error")
                    for template in templates]
    except (TypeError, ValueError) as exc:
        return result(ApplicationStatus.BLOCKED, blocking=(
            f"Candidate canonical template is invalid: {type(exc).__name__}: {exc}",
        ))
    non_inference_sources = sorted({item.source_kind.value for item in proposed
                                    if item.source_kind != SourceKind.INFERENCE})
    if candidate.provenance.source_kind != SourceKind.INFERENCE:
        non_inference_sources = sorted(set(non_inference_sources) | {candidate.provenance.source_kind.value})
    if non_inference_sources:
        return result(ApplicationStatus.BLOCKED, blocking=(
            "Advisory application may only commit inference-origin templates; source kinds were: "
            + ", ".join(non_inference_sources),
        ))
    unsupported = sorted({reason for item in proposed for reason in has_unsupported_options(item)})
    if unsupported:
        return result(ApplicationStatus.BLOCKED, blocking=(
            "Candidate semantic comparison is UNKNOWN: " + "; ".join(unsupported),
        ))
    if len({normalize_constraint(item) for item in proposed}) != len(proposed):
        return result(ApplicationStatus.BLOCKED, blocking=(
            "Candidate contains duplicate canonical constraints; multi-constraint application is ambiguous.",
        ))

    duplicate_ids, conflict_ids = _semantic_matches(cset, proposed)
    all_duplicate = len(duplicate_ids) == len(proposed) and not conflict_ids
    if decision.kind == IntentDecisionKind.ALREADY_SATISFIED:
        if all_duplicate:
            return result(ApplicationStatus.ALREADY_PRESENT, present=tuple(duplicate_ids), warnings=(
                "Explicit already-satisfied decision verified against canonical UCM; no mutation occurred.",
            ))
        return result(ApplicationStatus.BLOCKED, conflicts=tuple(conflict_ids), blocking=(
            "ALREADY_SATISFIED was not verified by semantic UCM identity.",
        ))
    if all_duplicate and _retry_duplicates_before_stale:
        return result(ApplicationStatus.ALREADY_PRESENT, present=tuple(duplicate_ids), warnings=(
            "Equivalent canonical UCM intent is already present; retry is idempotent.",
        ))
    if before != source["constraint_set"]:
        return result(ApplicationStatus.STALE, blocking=(
            "Canonical UCM snapshot differs from candidate source; explicit reinference is required.",
        ))
    if all_duplicate:
        return result(ApplicationStatus.ALREADY_PRESENT, present=tuple(duplicate_ids), warnings=(
            "Equivalent canonical UCM intent is already present; no duplicate was added.",
        ))
    if duplicate_ids:
        return result(ApplicationStatus.BLOCKED, present=tuple(duplicate_ids), blocking=(
            "Only part of a multi-constraint candidate is already present; application is atomic.",
        ))
    if conflict_ids:
        return result(ApplicationStatus.BLOCKED, conflicts=tuple(conflict_ids), blocking=(
            "Same-scope canonical UCM intent differs; existing user intent was not altered.",
        ))
    existing_unsupported = [item.id for item in cset if has_unsupported_options(item)]
    if existing_unsupported:
        return result(ApplicationStatus.BLOCKED, blocking=(
            "Existing UCM contains unsupported semantics; safe comparison is unavailable for application.",
        ), warnings=("Unsupported existing constraint IDs: " + ", ".join(sorted(existing_unsupported)),))

    scenario_block = _scenario_block(proposed, requested_scenarios, config, cset)
    if scenario_block is not None:
        return result(ApplicationStatus.BLOCKED, blocking=(scenario_block,))
    missing_dependencies, missing_assumptions = _missing_links(cset, proposed)
    if missing_dependencies or missing_assumptions:
        reasons: list[str] = []
        if missing_dependencies:
            reasons.append("Missing dependency IDs: " + ", ".join(missing_dependencies))
        if missing_assumptions:
            reasons.append("Missing assumption IDs: " + ", ".join(missing_assumptions))
        return result(ApplicationStatus.BLOCKED, blocking=tuple(reasons))

    # Build and validate a temporary projection. No caller-owned object changes
    # until the projection succeeds, including metadata and assumption links.
    trial = cset.clone()
    try:
        trial_constraints, template_ids = _prepare_trial_constraints(proposed, trial)
    except ValueError as exc:
        return result(ApplicationStatus.BLOCKED, blocking=(str(exc),))
    for item in trial_constraints:
        trial.add(item)
    _restore_internal_dependency_edges(trial, trial_constraints, template_ids)
    active_scenarios = _active_scenario_ids(config, cset)
    validation = run_validation(
        design=design, tg=tg, cset=trial,
        active_scenarios=set(active_scenarios) if active_scenarios else None,
    )
    planned_ids = {item.id for item in trial_constraints}
    validation_issues = tuple(
        issue.to_dict() for issue in validation.report.issues
        if issue.constraint_id in planned_ids or bool(planned_ids.intersection(issue.related_constraint_ids))
    )
    failures = [issue for issue in validation_issues
                if issue["severity"] in {"CRITICAL", "HIGH", "ERROR"}]
    unresolved = [issue for issue in validation_issues
                  if issue.get("resolution_status") in {"UNKNOWN", "UNRESOLVED", "REQUIRES_USER_INPUT"}]
    validation_summary = {
        "status": validation.status,
        "issue_count": len(validation_issues),
        "error_count": len(failures),
        "unresolved_count": len(unresolved),
        "checks_run": sorted(validation.report.checks_run),
    }
    if failures:
        return result(ApplicationStatus.FAILED_VALIDATION, validation_status=validation.status,
                      validation_summary=validation_summary, validation_issues=validation_issues, blocking=(
                          "Authoritative validation reported an error for the proposed UCM constraint(s).",
                      ))
    if unresolved:
        return result(ApplicationStatus.BLOCKED, validation_status=validation.status,
                      validation_summary=validation_summary, validation_issues=validation_issues, blocking=(
                          "Authoritative validation is unresolved for the proposed UCM constraint(s).",
                      ))

    receipt_evidence = _application_evidence(application, semantic_identity, validation.status, template_ids)
    validation_evidence = _validation_evidence(application, semantic_identity, validation, template_ids)
    legacy_evidence = _legacy_inference_acceptance_evidence(application, semantic_identity)
    for item in trial_constraints:
        item.status = ConstraintStatus.CONFIRMED
        for evidence in (*receipt_evidence, *validation_evidence, *legacy_evidence):
            item.provenance.add_evidence(evidence)
    if application.dry_run:
        return result(ApplicationStatus.NOT_APPLIED, validation_status=validation.status,
                      validation_summary=validation_summary, validation_issues=validation_issues,
                      evidence=(*receipt_evidence, *validation_evidence), warnings=(
                          "Dry run completed on an isolated UCM projection; caller UCM was not mutated.",
                      ))

    committed: list[Constraint] = []
    for item in trial_constraints:
        committed.append(cset.add(item.clone(new_id=item.id)))
    _restore_internal_dependency_edges(cset, committed, template_ids)
    after = stable_hash_cset(cset)
    receipt_store = cset.metadata.setdefault("constraint_applications", {})
    receipt_store[application_id] = {
        "candidate_id": candidate.id,
        "candidate_semantic_identity": semantic_identity,
        "decision": decision.kind.value,
        "applied_constraint_ids": sorted(item.id for item in committed),
        "scenario_ids": list(requested_scenarios),
        "source_snapshot_identity": dict(sorted(source.items())),
        "knowledge_references": [dict(item) for item in candidate.knowledge_references],
        "assumptions": sorted(candidate.assumptions),
        "validation_status": validation.status,
        "validation_issue_ids": sorted(item.get("issue_id", "") for item in validation_issues),
    }
    # Keep Step-27's metadata locator stable for callers that adopted its
    # explicit acceptance API before the richer receipt was introduced.
    legacy_store = cset.metadata.setdefault("accepted_inference", {})
    for item in committed:
        legacy_store[item.id] = {
            "candidate_id": candidate.id,
            "knowledge_references": [dict(reference) for reference in candidate.knowledge_references],
            "warnings": list(candidate.warnings),
            "assumptions": list(candidate.assumptions),
            "source_snapshot_identity": dict(candidate.source_snapshot_identity),
        }
    return result(ApplicationStatus.APPLIED, mutated=True,
                  applied=tuple(item.id for item in committed), validation_status=validation.status,
                  validation_summary=validation_summary, validation_issues=validation_issues,
                  evidence=(*receipt_evidence, *validation_evidence, *legacy_evidence), after=after,
                  warnings=("Candidate entered UCM only after explicit decision and isolated validation.",))


def apply_constraint_application(application: ConstraintApplication, cset: ConstraintSet,
                                 design: Any, tg: TimingGraph, config: Any | None = None) -> ConstraintApplicationResult:
    """Named alias for callers that prefer application-oriented terminology."""
    return apply_intent_decision(application, cset, design, tg, config)


def _templates_for(candidate: InferenceCandidate) -> tuple[dict[str, Any], ...]:
    if candidate.constraint_templates:
        return tuple(copy.deepcopy(item) for item in candidate.constraint_templates)
    if candidate.constraint_template is not None:
        return (copy.deepcopy(candidate.constraint_template),)
    return ()


def _candidate_template_ids(candidate: InferenceCandidate) -> tuple[str, ...]:
    """Return declared template IDs for a non-mutating reviewer rejection."""
    return tuple(sorted({str(template["id"]) for template in _templates_for(candidate)
                         if template.get("id")}))


def _application_id(application: ConstraintApplication, before: str, semantic_identity: str | None) -> str:
    return "APP-" + stable_hash({
        "candidate": application.candidate.id,
        "candidate_semantic": semantic_identity,
        "decision": application.decision.to_dict(),
        "scenarios": sorted(application.scenario_ids),
        "dry_run": application.dry_run,
        "ucm_before": before,
    })[:20]


def _semantic_matches(cset: ConstraintSet, proposed: list[Constraint]) -> tuple[list[str], list[str]]:
    duplicates: list[str] = []
    conflicts: list[str] = []
    for item in proposed:
        item_norm = normalize_constraint(item)
        item_match = semantic_match_key(item)
        exact = [existing.id for existing in cset if not has_unsupported_options(existing)
                 and normalize_constraint(existing) == item_norm]
        scoped_conflicts = [existing.id for existing in cset if not has_unsupported_options(existing)
                            and semantic_match_key(existing) == item_match
                            and normalize_constraint(existing) != item_norm]
        if exact:
            duplicates.extend(exact)
        if scoped_conflicts:
            conflicts.extend(scoped_conflicts)
    return sorted(set(duplicates)), sorted(set(conflicts))


def _scenario_block(proposed: list[Constraint], requested: tuple[str, ...], config: Any | None,
                    cset: ConstraintSet) -> str | None:
    matrix = _scenario_matrix(config, cset)
    explicit_scopes = [set(item.scenario_ids) for item in proposed if item.scenario_ids]
    candidate_scope = tuple(sorted(set().union(*explicit_scopes))) if explicit_scopes else ()
    if matrix.is_enabled:
        if not candidate_scope:
            return "MCMM is enabled; a globally scoped candidate requires explicit scenario-specific inference."
        if requested != candidate_scope:
            return "Application scenario scope must exactly match the candidate scenario scope."
    elif requested != candidate_scope:
        return "Application scenario scope does not exactly match canonical candidate scope."
    known = set(matrix.scenarios)
    unknown = sorted(set(candidate_scope) - known)
    inactive = sorted(set(candidate_scope) - set(matrix.active_ids)) if matrix.is_enabled else []
    if unknown:
        return "Candidate references unknown scenario IDs: " + ", ".join(unknown)
    if inactive:
        return "Candidate references inactive scenario IDs: " + ", ".join(inactive)
    # A multi-template proposal may not mix globally scoped and specific UCM
    # constraints because that would broaden part of the candidate implicitly.
    if candidate_scope and any(not item.scenario_ids for item in proposed):
        return "Multi-constraint candidate mixes global and scenario-specific scope."
    return None


def _scenario_matrix(config: Any | None, cset: ConstraintSet) -> ScenarioMatrix:
    if config is not None:
        return build_scenario_matrix(config, cset)
    scenarios = {sid: item for sid, item in cset.scenarios.items()}
    return ScenarioMatrix(scenarios=scenarios, enabled=False)


def _active_scenario_ids(config: Any | None, cset: ConstraintSet) -> list[str]:
    matrix = _scenario_matrix(config, cset)
    return list(matrix.active_ids) if matrix.is_enabled else []


def _missing_links(cset: ConstraintSet, proposed: list[Constraint]) -> tuple[list[str], list[str]]:
    template_ids = {item.id for item in proposed if item.id}
    dependencies = sorted({dependency for item in proposed for dependency in item.dependency_ids
                           if dependency not in cset.constraints and dependency not in template_ids})
    assumptions = sorted({assumption for item in proposed
                          for assumption in (*item.assumption_ids, *item.provenance.assumption_ids)
                          if assumption not in cset.ledger})
    return dependencies, assumptions


def _prepare_trial_constraints(proposed: list[Constraint], trial: ConstraintSet) -> tuple[list[Constraint], dict[str, str]]:
    """Allocate deterministic UCM IDs in a private projection before validation."""
    output: list[Constraint] = []
    template_ids: dict[str, str] = {}
    claimed = set(trial.constraints)
    for ordinal, source in enumerate(proposed, 1):
        item = source.clone(new_id=source.id)
        original_id = item.id
        if original_id:
            if original_id in claimed:
                raise ValueError(f"Candidate template ID '{original_id}' collides with existing UCM intent.")
            assigned = original_id
        else:
            assigned = f"APP_{ordinal:04d}"
            while assigned in claimed:
                assigned = f"APP_{ordinal:04d}_{len(claimed):04d}"
        item.id = assigned
        claimed.add(assigned)
        if original_id:
            template_ids[original_id] = assigned
        output.append(item)
    return output, template_ids


def _restore_internal_dependency_edges(cset: ConstraintSet, constraints: list[Constraint],
                                       template_ids: dict[str, str]) -> None:
    """Map candidate-local dependency IDs to assigned canonical UCM IDs."""
    for item in constraints:
        remapped = [template_ids.get(dependency, dependency) for dependency in item.dependency_ids]
        item.dependency_ids = sorted(set(remapped))
        for dependency in item.dependency_ids:
            cset.add_dependency_edge(dependency, item.id)


def _application_evidence(application: ConstraintApplication, semantic_identity: str,
                          validation_status: str, template_ids: dict[str, str]) -> tuple[Evidence, ...]:
    candidate = application.candidate
    return (Evidence(
        id="APP-ACCEPT-" + stable_hash({
            "candidate": candidate.id,
            "semantic": semantic_identity,
            "decision": application.decision.kind.value,
            "scenarios": sorted(application.scenario_ids),
        })[:16],
        kind="rule",
        description="Explicit controlled application of advisory inference candidate into canonical UCM.",
        detail={
            "candidate_id": candidate.id,
            "candidate_semantic_identity": semantic_identity,
            "decision": application.decision.to_dict(),
            "source_snapshot_identity": dict(sorted(candidate.source_snapshot_identity.items())),
            "rule_ids": list(candidate.rule_ids),
            "source_objects": list(candidate.source_objects),
            "assumptions": list(candidate.assumptions),
            "knowledge_references": [dict(item) for item in candidate.knowledge_references],
            "scenario_ids": sorted(application.scenario_ids),
            "template_id_map": dict(sorted(template_ids.items())),
            "validation_status": validation_status,
            "application_state": "EXPLICIT_ACCEPTED" if not application.dry_run else "DRY_RUN_NOT_APPLIED",
        },
        confidence=candidate.provenance.confidence,
        rule_id="INTENT-APPLICATION",
        created_by="rca.inference.application",
        created_at=_APPLICATION_EPOCH,
    ),)


def _legacy_inference_acceptance_evidence(application: ConstraintApplication,
                                        semantic_identity: str) -> tuple[Evidence, ...]:
    """Retain the Step-27 acceptance provenance locator without a second path."""
    candidate = application.candidate
    return (Evidence(
        id="INF-ACCEPT-" + stable_hash({
            "candidate_id": candidate.id,
            "semantic": semantic_identity,
        })[:16],
        kind="rule",
        description="Explicit advisory inference acceptance into canonical UCM.",
        detail={
            "candidate_id": candidate.id,
            "source_snapshot_identity": dict(candidate.source_snapshot_identity),
            "knowledge_references": [dict(item) for item in candidate.knowledge_references],
            "candidate_warnings": list(candidate.warnings),
            "acceptance_state": "EXPLICIT_ACCEPTED",
        },
        confidence=candidate.provenance.confidence,
        rule_id="INFERENCE-ACCEPT",
        created_by="rca.inference.application",
        created_at=_APPLICATION_EPOCH,
    ),)


def _validation_evidence(application: ConstraintApplication, semantic_identity: str,
                         validation: Any, template_ids: dict[str, str]) -> tuple[Evidence, ...]:
    return (Evidence(
        id="APP-VALIDATION-" + stable_hash({
            "candidate": application.candidate.id,
            "semantic": semantic_identity,
            "validation": validation.status,
            "templates": dict(sorted(template_ids.items())),
        })[:16],
        kind="rule",
        description="Existing RCA validation completed on isolated controlled-application UCM projection.",
        detail={
            "candidate_id": application.candidate.id,
            "candidate_semantic_identity": semantic_identity,
            "validation_status": validation.status,
            "validation_issue_count": len(validation.report.issues),
            "validation_checks": list(validation.report.checks_run),
        },
        confidence=application.candidate.provenance.confidence,
        rule_id="INTENT-APPLICATION-VALIDATION",
        created_by="rca.inference.application",
        created_at=_APPLICATION_EPOCH,
    ),)


__all__ = [
    "ApplicationEvidence",
    "ApplicationStatus",
    "ConstraintApplication",
    "ConstraintApplicationResult",
    "IntentDecision",
    "IntentDecisionKind",
    "apply_constraint_application",
    "apply_intent_decision",
    "candidate_semantic_identity",
]
