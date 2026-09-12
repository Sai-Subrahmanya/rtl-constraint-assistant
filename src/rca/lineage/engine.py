"""Read-only deterministic constraint lineage and semantic snapshot audit.

Step 30 joins identifiers already retained by UCM provenance, Step-27 advice,
Step-28 applications, validation/formal evidence, Step-29 readiness, and the
Step-9 semantic comparison. It deliberately owns no history and performs no
mutation, acceptance, validation, EDA, artifact, or database operation.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..constraint_model import Constraint, ConstraintSet, stable_hash_cset
from ..design_model import Design
from ..equivalence import compare, has_unsupported_options, normalize_constraint
from ..inference import InferenceReport
from ..readiness import ConstraintReadinessReport
from ..timing_model import TimingGraph
from ..utils.enums import Severity, SourceKind
from ..utils.hashing import stable_hash
from ..validation import ValidationResult
from .models import (
    ConstraintLineage,
    ConstraintLineageReport,
    LineageChange,
    LineageChangeKind,
    LineageEvent,
    LineageEventKind,
    LineageScenarioScope,
    LineageSnapshot,
    LineageSource,
    UCMChangeSet,
)

_VOLATILE_ID_KEYS = {
    "created_at", "import_timestamp", "runtime_seconds", "duration_seconds", "enabled_at",
}


@dataclass(frozen=True)
class _ApplicationLink:
    """Internal projection of existing Step-28 metadata/receipt; not persisted."""

    application_id: str
    candidate_id: str | None
    decision: str | None
    status: str
    applied_constraint_ids: tuple[str, ...]
    already_present_ids: tuple[str, ...]
    ucm_mutated: bool
    source_snapshot_identity: dict[str, str]
    ucm_after_snapshot_identity: str
    knowledge_references: tuple[dict[str, Any], ...]
    assumptions: tuple[str, ...]
    validation_status: str | None
    validation_issue_ids: tuple[str, ...]
    application_evidence: tuple[Any, ...]
    external: bool
    stale: bool


def build_constraint_lineage(
    cset: ConstraintSet,
    *,
    config: Any | None = None,
    design: Design | None = None,
    timing_graph: TimingGraph | None = None,
    inference_report: InferenceReport | None = None,
    application_receipts: Iterable[Any] = (),
    validation: ValidationResult | None = None,
    formal_results: Iterable[Any] = (),
    readiness: ConstraintReadinessReport | None = None,
    before: ConstraintSet | None = None,
    before_readiness: ConstraintReadinessReport | None = None,
) -> ConstraintLineageReport:
    """Build a deterministic traceability projection for current ``cset``.

    All optional inputs are pre-existing evidence. In particular, this function
    never calls an inference/application/validation/readiness/formal/EDA API:
    callers that want those reports must produce them through their established
    owner and explicitly supply them here. ``before`` enables a Step-9 semantic
    UCM comparison; it is never selected from history implicitly.
    """
    snapshot = _snapshot(cset)
    identities = _input_identities(cset, config, design, timing_graph)
    links, attempts = _application_links(cset, application_receipts, identities)
    links_by_constraint: dict[str, list[_ApplicationLink]] = defaultdict(list)
    for link in links:
        for cid in (*link.applied_constraint_ids, *link.already_present_ids):
            if cset.get(cid) is not None:
                links_by_constraint[cid].append(link)
    candidates = {
        candidate.id: candidate
        for candidate in (inference_report.candidates if inference_report is not None else [])
    }
    candidate_events = _candidate_events(candidates, links_by_constraint, identities, snapshot.snapshot_identity)
    validation_by_constraint, global_validation_events = _validation_events(
        validation, snapshot.snapshot_identity, tuple(sorted(cset.constraints)),
    )
    formal_by_constraint = _formal_events(
        validation, formal_results, snapshot.snapshot_identity,
    )

    entries: list[ConstraintLineage] = []
    for constraint in sorted(cset, key=lambda item: item.id):
        attached_links = tuple(sorted(links_by_constraint.get(constraint.id, ()), key=lambda item: item.application_id))
        primary_link = _select_primary_link(attached_links)
        candidate = candidates.get(primary_link.candidate_id) if primary_link and primary_link.candidate_id else None
        source, origin_source = _source_for_constraint(constraint, primary_link)
        scope = _scenario_scope(cset, constraint)
        knowledge_references = _knowledge_references(constraint, primary_link, candidate, cset)
        knowledge_conflicts = _knowledge_conflicts(inference_report, candidate.id if candidate is not None else None)
        evidence = _combined_evidence(constraint, primary_link, candidate)
        events = _constraint_application_events(
            constraint, attached_links, snapshot.snapshot_identity,
        )
        stale_events = _constraint_stale_events(
            constraint, attached_links, candidate, identities, snapshot.snapshot_identity,
        )
        gaps = _linkage_gaps(constraint, primary_link, candidate, source, evidence)
        assumptions = _dedupe_strings(
            [*constraint.assumption_ids, *getattr(constraint.provenance, "assumption_ids", []),
             *(primary_link.assumptions if primary_link is not None else ()),
             *(candidate.assumptions if candidate is not None else ())]
        )
        entries.append(ConstraintLineage(
            constraint_id=constraint.id,
            constraint_type=constraint.type.value,
            semantic_identity=_semantic_identity(constraint),
            source=source,
            origin_source=origin_source,
            source_kind=_enum_text(getattr(constraint, "source_kind", None)) or None,
            current_canonical=True,
            snapshot_identity=snapshot.snapshot_identity,
            scenario_scope=scope,
            provenance=getattr(constraint, "provenance", None),
            evidence=evidence,
            candidate_id=(candidate.id if candidate is not None
                          else primary_link.candidate_id if primary_link is not None else None),
            application_id=primary_link.application_id if primary_link is not None else None,
            knowledge_references=knowledge_references,
            knowledge_conflicts=knowledge_conflicts,
            assumption_ids=assumptions,
            dependency_ids=tuple(sorted(set(constraint.dependency_ids))),
            validation_events=tuple(validation_by_constraint.get(constraint.id, ())),
            formal_events=tuple(formal_by_constraint.get(constraint.id, ())),
            events=(*events, *stale_events),
            linkage_gaps=gaps,
        ))

    change_set = (compare_constraint_lineage(
        before, cset,
        before_readiness=before_readiness,
        after_readiness=readiness,
    ) if before is not None else None)
    return ConstraintLineageReport(
        snapshot=snapshot,
        constraints=tuple(entries),
        application_attempts=tuple(attempts),
        advisory_candidates=tuple(candidate_events),
        global_validation_events=tuple(global_validation_events),
        readiness=readiness,
        change_set=change_set,
    )


@dataclass(frozen=True)
class ConstraintLineageEngine:
    """Stateless façade for the one read-only Step-30 lineage projection."""

    def build(self, cset: ConstraintSet, **kwargs: Any) -> ConstraintLineageReport:
        return build_constraint_lineage(cset, **kwargs)

    def compare(self, before: ConstraintSet, after: ConstraintSet, **kwargs: Any) -> UCMChangeSet:
        return compare_constraint_lineage(before, after, **kwargs)


def compare_constraint_lineage(
    before: ConstraintSet,
    after: ConstraintSet,
    *,
    before_readiness: ConstraintReadinessReport | None = None,
    after_readiness: ConstraintReadinessReport | None = None,
) -> UCMChangeSet:
    """Project existing Step-9 semantic UCM comparison into stable changes."""
    compared = compare(before, after)
    before_snapshot = _snapshot(before)
    after_snapshot = _snapshot(after)
    changes: list[LineageChange] = []

    for pair in compared.equivalent_constraints:
        changes.append(_pair_change(
            LineageChangeKind.UNCHANGED, before, after, pair, before_snapshot, after_snapshot,
        ))
    for pair in compared.different_constraints:
        changes.append(_pair_change(
            LineageChangeKind.MODIFIED, before, after, pair, before_snapshot, after_snapshot,
        ))
    for pair in compared.unknown_constraints:
        changes.append(_pair_change(
            LineageChangeKind.UNKNOWN, before, after, pair, before_snapshot, after_snapshot,
        ))
    for pair in compared.only_in_left:
        changes.append(_pair_change(
            LineageChangeKind.REMOVED, before, after, pair, before_snapshot, after_snapshot,
        ))
    for pair in compared.only_in_right:
        changes.append(_pair_change(
            LineageChangeKind.ADDED, before, after, pair, before_snapshot, after_snapshot,
        ))
    for scenario_difference in compared.scenario_differences:
        kind = {
            "ONLY_IN_LEFT": LineageChangeKind.REMOVED,
            "ONLY_IN_RIGHT": LineageChangeKind.ADDED,
            "DIFFERENT": LineageChangeKind.MODIFIED,
            "UNKNOWN": LineageChangeKind.UNKNOWN,
        }.get(scenario_difference.status, LineageChangeKind.UNKNOWN)
        details = scenario_difference.to_dict()
        changes.append(LineageChange(
            id=_change_id(kind, "scenario", None, None, None, None, details),
            kind=kind,
            subject_kind="scenario",
            before_constraint_id=None,
            after_constraint_id=None,
            before_semantic_identity=None,
            after_semantic_identity=None,
            constraint_type=None,
            details=details,
        ))
    readiness_events = _readiness_changes(
        before_readiness, after_readiness, before_snapshot, after_snapshot,
    )
    return UCMChangeSet(
        before=before_snapshot,
        after=after_snapshot,
        changes=tuple(changes),
        comparison=compared.to_dict(),
        readiness_events=tuple(readiness_events),
    )


def _snapshot(cset: ConstraintSet) -> LineageSnapshot:
    raw = cset.to_snapshot_dict(include_assumptions=False)
    return LineageSnapshot(
        snapshot_identity=stable_hash_cset(cset),
        name=cset.name,
        constraint_ids=tuple(sorted(cset.constraints)),
        scenario_ids=tuple(sorted(cset.scenarios)),
        schema_version=raw.get("schema_version"),
    )


def _input_identities(cset: ConstraintSet, config: Any | None, design: Design | None,
                      timing_graph: TimingGraph | None) -> dict[str, str]:
    return {
        "constraint_set": stable_hash_cset(cset),
        "config": (stable_hash(config.model_dump()) if config is not None and hasattr(config, "model_dump")
                   else stable_hash(config) if config is not None else ""),
        "design": stable_hash(design.snapshot()) if design is not None else "",
        "timing_graph": (stable_hash(timing_graph.model_dump()) if timing_graph is not None else ""),
    }


def _application_links(cset: ConstraintSet, receipts: Iterable[Any], current: dict[str, str]
                      ) -> tuple[tuple[_ApplicationLink, ...], tuple[LineageEvent, ...]]:
    raw_by_id: dict[str, dict[str, Any]] = {}
    stored = cset.metadata.get("constraint_applications", {}) if isinstance(cset.metadata, dict) else {}
    if isinstance(stored, Mapping):
        for app_id, record in sorted(stored.items(), key=lambda item: str(item[0])):
            if isinstance(record, Mapping):
                raw_by_id[str(app_id)] = {**record, "application_id": str(app_id), "_stored": True}
    for receipt in receipts:
        payload = receipt.to_dict() if hasattr(receipt, "to_dict") else receipt
        if not isinstance(payload, Mapping):
            continue
        app_id = str(payload.get("application_id") or "external-" + stable_hash(_id_value(payload))[:20])
        combined = dict(raw_by_id.get(app_id, {}))
        combined.update(dict(payload))
        combined["application_id"] = app_id
        combined["_stored"] = app_id in raw_by_id
        raw_by_id[app_id] = combined

    links: list[_ApplicationLink] = []
    attempts: list[LineageEvent] = []
    present = set(cset.constraints)
    for app_id, raw in sorted(raw_by_id.items()):
        applied = tuple(sorted({str(item) for item in raw.get("applied_constraint_ids", []) if item}))
        already = tuple(sorted({str(item) for item in raw.get("already_present_ids", []) if item}))
        status = _enum_text(raw.get("status")) or ("APPLIED" if applied else "UNKNOWN")
        decision = _enum_text(raw.get("decision")) or None
        mutated = bool(raw.get("ucm_mutated", bool(applied)))
        source = dict(raw.get("source_snapshot_identity", {}) or {})
        after = str(raw.get("ucm_after_snapshot_identity") or "")
        stale = bool(raw.get("stale", False))
        if after and after != current["constraint_set"]:
            stale = True
        for key in ("design", "timing_graph", "config"):
            if source.get(key) and current.get(key) and source[key] != current[key]:
                stale = True
        canonical_ids = set(applied) | set(already)
        current_ids = canonical_ids & present
        # A receipt is trace evidence, but never proof that a current UCM
        # constraint exists. current_canonical reflects the UCM lookup only.
        current_canonical = bool(canonical_ids) and current_ids == canonical_ids
        link = _ApplicationLink(
            application_id=app_id,
            candidate_id=str(raw.get("candidate_id")) if raw.get("candidate_id") else None,
            decision=decision,
            status=status,
            applied_constraint_ids=applied,
            already_present_ids=already,
            ucm_mutated=mutated,
            source_snapshot_identity=source,
            ucm_after_snapshot_identity=after,
            knowledge_references=_mapping_tuple(raw.get("knowledge_references", [])),
            assumptions=tuple(sorted({str(item) for item in raw.get("assumptions", []) if item})),
            validation_status=str(raw.get("validation_status")) if raw.get("validation_status") else None,
            validation_issue_ids=tuple(sorted({str(item) for item in raw.get("validation_issue_ids", []) if item})),
            application_evidence=tuple(raw.get("application_evidence", ()) or ()),
            external=not bool(raw.get("_stored", False)),
            stale=stale,
        )
        links.append(link)
        attempts.extend(_application_attempt_events(link, current["constraint_set"], current_canonical))
    return tuple(links), tuple(attempts)


def _application_attempt_events(link: _ApplicationLink, snapshot_identity: str,
                                current_canonical: bool) -> tuple[LineageEvent, ...]:
    evidence_ids = _evidence_ids(link.application_evidence)
    base_details = {
        "application_status": link.status,
        "decision": link.decision,
        "ucm_mutated": link.ucm_mutated,
        "applied_constraint_ids": list(link.applied_constraint_ids),
        "already_present_ids": list(link.already_present_ids),
        "validation_status": link.validation_status,
        "validation_issue_ids": list(link.validation_issue_ids),
        "external_receipt": link.external,
        "ucm_after_snapshot_identity": link.ucm_after_snapshot_identity or None,
        "application_evidence_ids": list(evidence_ids),
    }
    events = [_event(
        LineageEventKind.APPLICATION_ATTEMPTED, subject_kind="application", subject_id=link.application_id,
        message=f"Observed explicit application attempt with status {link.status}.",
        snapshot_identity=snapshot_identity, candidate_id=link.candidate_id, application_id=link.application_id,
        current_canonical=current_canonical, stale=link.stale, evidence_ids=evidence_ids, details=base_details,
    )]
    status = link.status.upper()
    if status in {"REJECTED", "FAILED_VALIDATION", "BLOCKED"}:
        kind = LineageEventKind.REJECTED
    elif status in {"DEFERRED", "NOT_APPLIED"}:
        kind = LineageEventKind.DEFERRED
    elif status in {"APPLIED", "ALREADY_PRESENT"} or (link.decision or "") in {"ACCEPT", "CONFIRM"}:
        kind = LineageEventKind.ACCEPTED
    else:
        kind = None
    if kind is not None:
        events.append(_event(
            kind, subject_kind="application", subject_id=link.application_id,
            message=f"Application outcome retained as {link.status}; no lifecycle state was inferred.",
            snapshot_identity=snapshot_identity, candidate_id=link.candidate_id, application_id=link.application_id,
            current_canonical=current_canonical, stale=link.stale, evidence_ids=evidence_ids, details=base_details,
        ))
    if link.stale:
        events.append(_event(
            LineageEventKind.BECAME_STALE, subject_kind="application", subject_id=link.application_id,
            message="Application evidence is stale against the supplied current snapshot/input identity.",
            snapshot_identity=snapshot_identity, candidate_id=link.candidate_id, application_id=link.application_id,
            current_canonical=current_canonical, stale=True, evidence_ids=evidence_ids, details=base_details,
        ))
    return tuple(events)


def _candidate_events(candidates: Mapping[str, Any], links_by_constraint: Mapping[str, list[_ApplicationLink]],
                      current: dict[str, str], snapshot_identity: str) -> tuple[LineageEvent, ...]:
    accepted_candidate_ids = {
        link.candidate_id
        for links in links_by_constraint.values()
        for link in links
        if link.candidate_id and link.status.upper() == "APPLIED" and link.applied_constraint_ids
    }
    events: list[LineageEvent] = []
    for candidate_id, candidate in sorted(candidates.items()):
        linked = candidate_id in accepted_candidate_ids
        stale = _candidate_stale(candidate, current, ignore_constraint_set=linked)
        details = {
            "inference_status": _enum_text(candidate.status),
            "decision": _enum_text(candidate.decision),
            "rule_ids": sorted(candidate.rule_ids),
            "source_objects": sorted(candidate.source_objects),
            "knowledge_references": [_id_value(item) for item in candidate.knowledge_references],
            "missing_information": [_id_value(item) for item in candidate.missing_information],
            "canonical_state": "LINKED_TO_CURRENT_UCM" if linked else "NOT_ACCEPTED",
        }
        events.append(_event(
            LineageEventKind.CREATED, subject_kind="inference_candidate", subject_id=candidate_id,
            message="Observed advisory inference candidate; it is not canonical UCM intent by itself.",
            snapshot_identity=snapshot_identity, candidate_id=candidate_id,
            current_canonical=False, stale=stale, details=details,
        ))
        if stale:
            events.append(_event(
                LineageEventKind.BECAME_STALE, subject_kind="inference_candidate", subject_id=candidate_id,
                message="Advisory candidate source snapshot differs from supplied current evidence.",
                snapshot_identity=snapshot_identity, candidate_id=candidate_id,
                current_canonical=False, stale=True, details=details,
            ))
    return tuple(events)


def _validation_events(validation: ValidationResult | None, snapshot_identity: str,
                       constraint_ids: tuple[str, ...]
                      ) -> tuple[dict[str, list[LineageEvent]], tuple[LineageEvent, ...]]:
    by_constraint: dict[str, list[LineageEvent]] = defaultdict(list)
    global_events: list[LineageEvent] = []
    if validation is None:
        return by_constraint, ()
    for issue in sorted(validation.report.issues, key=lambda item: str(getattr(item, "issue_id", ""))):
        issue_data = issue.to_dict() if hasattr(issue, "to_dict") else _id_value(issue)
        invalid = bool(getattr(issue, "blocking", False)) or _enum_text(getattr(issue, "severity", None)) in {
            Severity.CRITICAL.value, Severity.ERROR.value, Severity.HIGH.value,
        }
        kind = LineageEventKind.INVALIDATED if invalid else LineageEventKind.VALIDATED
        cid = getattr(issue, "constraint_id", None)
        event = _event(
            kind, subject_kind="validation_issue", subject_id=str(getattr(issue, "issue_id", "")) or stable_hash(issue_data),
            message=str(getattr(issue, "message", "Existing validation evidence.")),
            snapshot_identity=snapshot_identity, constraint_id=cid,
            scenario_id=getattr(issue, "scenario_id", None), current_canonical=bool(cid),
            details={"validation_status": validation.status, "issue": issue_data},
        )
        if cid:
            by_constraint[cid].append(event)
        else:
            global_events.append(event)
    summary = validation.as_dict()
    global_events.append(_event(
        LineageEventKind.VALIDATED, subject_kind="validation_report", subject_id="validation_report",
        message=f"Observed existing validation report status {_enum_text(validation.status)}.",
        snapshot_identity=snapshot_identity, details={"validation_summary": summary},
    ))
    # A clean validation report has no per-constraint issue to attach, but it
    # is still existing evidence about the UCM snapshot. Project the report
    # scope onto each current constraint without claiming it authored or
    # changed any constraint.
    for cid in constraint_ids:
        if cid in by_constraint:
            continue
        by_constraint[cid].append(_event(
            LineageEventKind.VALIDATED, subject_kind="validation_report_scope",
            subject_id=f"validation_report:{cid}",
            message="Existing validation report provides aggregate evidence for this current UCM snapshot.",
            snapshot_identity=snapshot_identity, constraint_id=cid, current_canonical=True,
            details={"validation_status": validation.status, "validation_scope": "aggregate_report"},
        ))
    return by_constraint, tuple(global_events)


def _formal_events(validation: ValidationResult | None, formal_results: Iterable[Any], snapshot_identity: str
                  ) -> dict[str, list[LineageEvent]]:
    by_constraint: dict[str, list[LineageEvent]] = defaultdict(list)

    def add(raw: Any, constraint_id: str | None = None, scenario_id: str | None = None) -> None:
        data = raw.to_dict() if hasattr(raw, "to_dict") else _id_value(raw)
        if not isinstance(data, Mapping):
            return
        cid = constraint_id or str(data.get("constraint_id") or data.get("source_constraint_id") or "")
        if not cid:
            return
        status = _enum_text(data.get("status")) or "UNVERIFIED"
        invalid = status in {"INVALID", "ERROR", "FAILED"}
        kind = LineageEventKind.INVALIDATED if invalid else LineageEventKind.VALIDATED
        by_constraint[cid].append(_event(
            kind, subject_kind="formal_verification", subject_id=cid,
            message=f"Observed formal verification evidence with status {status}.",
            snapshot_identity=snapshot_identity, constraint_id=cid, scenario_id=scenario_id,
            current_canonical=True,
            details={"verification_status": status, "formal_verification": data},
        ))

    for result in formal_results:
        verification = getattr(result, "verification", result)
        add(verification, getattr(result, "constraint_id", None), getattr(result, "scenario_id", None))
    if validation is not None:
        for issue in validation.report.issues:
            if _enum_text(getattr(issue, "category", None)) != "EXCEPTION":
                continue
            evidence = getattr(issue, "evidence", {}) or {}
            verification = evidence.get("verification") if isinstance(evidence, Mapping) else None
            if verification:
                add(verification, getattr(issue, "constraint_id", None), getattr(issue, "scenario_id", None))
    for events in by_constraint.values():
        events.sort(key=lambda event: event.id)
    return by_constraint


def _source_for_constraint(constraint: Constraint, link: _ApplicationLink | None) -> tuple[LineageSource, LineageSource]:
    provenance = getattr(constraint, "provenance", None)
    source_kind = _enum_text(getattr(constraint, "source_kind", None))
    # An ALREADY_PRESENT retry is audit evidence for an attempt, not proof
    # that it authored the pre-existing canonical constraint. Only a receipt
    # that actually lists this constraint as applied may classify its source
    # as explicitly accepted.
    if (link is not None and constraint.id in link.applied_constraint_ids
            and link.status.upper() == "APPLIED"):
        return LineageSource.EXPLICITLY_ACCEPTED, LineageSource.INFERRED
    if any(
        getattr(evidence, "rule_id", None) in {"INTENT-APPLICATION", "INFERENCE-ACCEPT"}
        and str((getattr(evidence, "detail", {}) or {}).get("acceptance_state", "")).startswith("EXPLICIT_ACCEPTED")
        for evidence in getattr(provenance, "evidence", []) or []
    ):
        # Retained canonical provenance can establish explicit acceptance even
        # if a legacy snapshot no longer contains the application metadata.
        return LineageSource.EXPLICITLY_ACCEPTED, LineageSource.INFERRED
    if getattr(provenance, "import_meta", None) is not None or source_kind == SourceKind.EXISTING_SDC.value:
        return LineageSource.IMPORTED, LineageSource.IMPORTED
    if source_kind == SourceKind.USER.value:
        return LineageSource.USER_PROVIDED, LineageSource.USER_PROVIDED
    if source_kind == SourceKind.INFERENCE.value:
        knowledge_referenced = any(
            getattr(evidence, "rule_id", None) == "KNOWLEDGE-REUSE"
            or bool((getattr(evidence, "detail", {}) or {}).get("knowledge_item_id"))
            or bool((getattr(evidence, "detail", {}) or {}).get("knowledge_references"))
            for evidence in getattr(provenance, "evidence", []) or []
        )
        if knowledge_referenced:
            # Knowledge remains advisory: this classification says a retained
            # knowledge reference informed an inferred constraint, not that
            # a knowledge item is canonical UCM authority.
            return LineageSource.KNOWLEDGE_REFERENCED, LineageSource.INFERRED
        # Some legacy snapshots carry only the model default (INFERENCE) and
        # no retained rule/evidence/provenance locator. Reporting that as a
        # known inference origin would manufacture a lineage claim.
        if (provenance is None or (
                not getattr(provenance, "rule_id", None)
                and not getattr(provenance, "evidence", None)
                and not getattr(provenance, "explanation", "")
                and getattr(provenance, "created_by", "rca") in {"", "rca", None}
        )):
            return LineageSource.UNKNOWN, LineageSource.UNKNOWN
        return LineageSource.INFERRED, LineageSource.INFERRED
    if source_kind in {
        SourceKind.RTL.value, SourceKind.TOOL.value, SourceKind.LIBRARY.value,
        SourceKind.PHYSICAL_DATA.value, SourceKind.DERIVED.value,
    }:
        return LineageSource.SYSTEM_OBSERVED, LineageSource.SYSTEM_OBSERVED
    return LineageSource.UNKNOWN, LineageSource.UNKNOWN


def _scenario_scope(cset: ConstraintSet, constraint: Constraint) -> LineageScenarioScope:
    requested = tuple(sorted(set(constraint.scenario_ids or [])))
    known = set(cset.scenarios)
    active = tuple(sorted(sid for sid, value in cset.scenarios.items()
                          if value is not None and getattr(value, "active", True)))
    if not requested:
        return LineageScenarioScope(
            scope_kind="GLOBAL", scenario_ids=(), applicable_scenario_ids=active,
        )
    return LineageScenarioScope(
        scope_kind="SCENARIO_SPECIFIC", scenario_ids=requested,
        applicable_scenario_ids=tuple(sorted(set(requested) & set(active))),
        unknown_scenario_ids=tuple(sorted(set(requested) - known)),
    )


def _combined_evidence(constraint: Constraint, link: _ApplicationLink | None,
                       candidate: Any | None) -> tuple[Any, ...]:
    found: dict[str, Any] = {}
    for item in getattr(getattr(constraint, "provenance", None), "evidence", []) or []:
        found[getattr(item, "id", stable_hash(_id_value(item)))] = item
    if candidate is not None:
        for item in candidate.evidence:
            found.setdefault(getattr(item, "id", stable_hash(_id_value(item))), item)
        for item in getattr(candidate.provenance, "evidence", []) or []:
            found.setdefault(getattr(item, "id", stable_hash(_id_value(item))), item)
    # External receipts serialise evidence dictionaries. Keep their evidence
    # identifiers in events, but do not reconstruct a parallel Evidence model.
    return tuple(found[key] for key in sorted(found))


def _knowledge_references(constraint: Constraint, link: _ApplicationLink | None,
                         candidate: Any | None, cset: ConstraintSet) -> tuple[dict[str, Any], ...]:
    refs: list[dict[str, Any]] = []
    if candidate is not None:
        refs.extend(_id_value(item) for item in candidate.knowledge_references)
    if link is not None:
        refs.extend(_id_value(item) for item in link.knowledge_references)
    accepted = cset.metadata.get("accepted_inference", {}) if isinstance(cset.metadata, dict) else {}
    if isinstance(accepted, Mapping) and isinstance(accepted.get(constraint.id), Mapping):
        refs.extend(_id_value(item) for item in accepted[constraint.id].get("knowledge_references", []))
    for evidence in getattr(getattr(constraint, "provenance", None), "evidence", []) or []:
        detail = getattr(evidence, "detail", {}) or {}
        if isinstance(detail, Mapping):
            if detail.get("knowledge_item_id"):
                refs.append({"knowledge_item_id": detail["knowledge_item_id"], "advisory": True,
                             "evidence_id": getattr(evidence, "id", "")})
            refs.extend(_id_value(item) for item in detail.get("knowledge_references", [])
                        if isinstance(item, Mapping))
    normalized: dict[str, dict[str, Any]] = {}
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        item = dict(ref)
        item["advisory"] = True
        normalized[stable_hash(_id_value(item))] = item
    return tuple(normalized[key] for key in sorted(normalized))


def _knowledge_conflicts(report: InferenceReport | None, candidate_id: str | None) -> tuple[dict[str, Any], ...]:
    if report is None or not candidate_id:
        return ()
    return tuple(_id_value(item) for item in sorted(
        (item for item in report.conflicts if str(item.get("subject", "")) == candidate_id),
        key=lambda item: stable_hash(_id_value(item)),
    ))


def _constraint_application_events(constraint: Constraint, links: tuple[_ApplicationLink, ...],
                                  snapshot_identity: str) -> tuple[LineageEvent, ...]:
    events: list[LineageEvent] = []
    for link in links:
        evidence_ids = _evidence_ids(link.application_evidence)
        details = {
            "application_status": link.status, "decision": link.decision,
            "ucm_mutated": link.ucm_mutated, "applied_constraint_ids": list(link.applied_constraint_ids),
            "already_present_ids": list(link.already_present_ids), "external_receipt": link.external,
        }
        events.append(_event(
            LineageEventKind.APPLICATION_ATTEMPTED, subject_kind="constraint_application",
            subject_id=f"{link.application_id}:{constraint.id}",
            message=f"Application {link.application_id} references this current canonical constraint.",
            snapshot_identity=snapshot_identity, constraint_id=constraint.id, candidate_id=link.candidate_id,
            application_id=link.application_id, current_canonical=True, stale=link.stale,
            evidence_ids=evidence_ids, details=details,
        ))
        if link.status.upper() in {"APPLIED", "ALREADY_PRESENT"}:
            events.append(_event(
                LineageEventKind.ACCEPTED, subject_kind="constraint_application",
                subject_id=f"{link.application_id}:{constraint.id}",
                message="Explicit controlled application linkage is retained; UCM presence was verified.",
                snapshot_identity=snapshot_identity, constraint_id=constraint.id, candidate_id=link.candidate_id,
                application_id=link.application_id, current_canonical=True, stale=link.stale,
                evidence_ids=evidence_ids, details=details,
            ))
        if (link.status.upper() == "APPLIED" and constraint.id in link.applied_constraint_ids
                and not link.stale):
            events.append(_event(
                LineageEventKind.CREATED, subject_kind="constraint_application",
                subject_id=f"{link.application_id}:{constraint.id}",
                message="Current canonical constraint is linked to an explicit applied receipt.",
                snapshot_identity=snapshot_identity, constraint_id=constraint.id, candidate_id=link.candidate_id,
                application_id=link.application_id, current_canonical=True, stale=link.stale,
                evidence_ids=evidence_ids, details=details,
            ))
    return tuple(events)


def _constraint_stale_events(constraint: Constraint, links: tuple[_ApplicationLink, ...], candidate: Any | None,
                             current: dict[str, str], snapshot_identity: str) -> tuple[LineageEvent, ...]:
    events: list[LineageEvent] = []
    for link in links:
        if link.stale:
            events.append(_event(
                LineageEventKind.BECAME_STALE, subject_kind="constraint_application",
                subject_id=f"{link.application_id}:{constraint.id}",
                message="Application evidence is stale; the current UCM constraint remains separately verified.",
                snapshot_identity=snapshot_identity, constraint_id=constraint.id, candidate_id=link.candidate_id,
                application_id=link.application_id, current_canonical=True, stale=True,
                details={"application_status": link.status, "source_snapshot_identity": link.source_snapshot_identity,
                         "ucm_after_snapshot_identity": link.ucm_after_snapshot_identity or None},
            ))
    if candidate is not None and _candidate_stale(candidate, current, ignore_constraint_set=True):
        events.append(_event(
            LineageEventKind.BECAME_STALE, subject_kind="inference_candidate", subject_id=candidate.id,
            message="Linked advisory candidate evidence is stale against current supplied input evidence.",
            snapshot_identity=snapshot_identity, constraint_id=constraint.id, candidate_id=candidate.id,
            current_canonical=True, stale=True,
            details={"source_snapshot_identity": _id_value(candidate.source_snapshot_identity)},
        ))
    return tuple(events)


def _candidate_stale(candidate: Any, current: dict[str, str], *, ignore_constraint_set: bool) -> bool:
    source = getattr(candidate, "source_snapshot_identity", {}) or {}
    for key in ("design", "timing_graph", "config", "constraint_set"):
        if ignore_constraint_set and key == "constraint_set":
            continue
        if source.get(key) and current.get(key) and source[key] != current[key]:
            return True
    return False


def _linkage_gaps(constraint: Constraint, link: _ApplicationLink | None, candidate: Any | None,
                 source: LineageSource, evidence: tuple[Any, ...]) -> tuple[str, ...]:
    gaps: list[str] = []
    if source == LineageSource.UNKNOWN:
        gaps.append("Canonical source kind/provenance is unavailable or legacy-unknown.")
    if source in {LineageSource.INFERRED, LineageSource.KNOWLEDGE_REFERENCED} and link is None:
        gaps.append("No explicit controlled application record links this inferred canonical constraint.")
    if source == LineageSource.EXPLICITLY_ACCEPTED and candidate is None:
        gaps.append("Explicit acceptance is retained, but the originating candidate report was not supplied.")
    if not evidence:
        gaps.append("No retained canonical/candidate Evidence record is available.")
    if constraint.scenario_ids and any(not value for value in constraint.scenario_ids):
        gaps.append("Constraint scenario scope contains an empty identifier.")
    return tuple(sorted(set(gaps)))


def _pair_change(kind: LineageChangeKind, before: ConstraintSet, after: ConstraintSet, pair: Any,
                before_snapshot: LineageSnapshot, after_snapshot: LineageSnapshot) -> LineageChange:
    left = before.get(pair.a_id) if getattr(pair, "a_id", None) else None
    right = after.get(pair.b_id) if getattr(pair, "b_id", None) else None
    before_source = _source_for_constraint(left, None)[0] if left is not None else None
    after_source = _source_for_constraint(right, None)[0] if right is not None else None
    field_changes = tuple(item.to_dict() if hasattr(item, "to_dict") else _id_value(item)
                          for item in getattr(pair, "fields", []))
    evidence = _dedupe_evidence(
        [*(getattr(getattr(left, "provenance", None), "evidence", []) or []),
         *(getattr(getattr(right, "provenance", None), "evidence", []) or [])]
    )
    details = {
        "semantic_pair_status": _enum_text(getattr(pair, "status", None)),
        "comparison_level": _enum_text(getattr(pair, "level", None)),
        "notes": list(getattr(pair, "notes", []) or []),
        "semantic_key_digest": getattr(pair, "semantic_key_digest", ""),
        "before_snapshot_identity": before_snapshot.snapshot_identity,
        "after_snapshot_identity": after_snapshot.snapshot_identity,
    }
    return LineageChange(
        id=_change_id(kind, "constraint", getattr(pair, "a_id", None), getattr(pair, "b_id", None),
                      _semantic_identity(left), _semantic_identity(right), details),
        kind=kind,
        subject_kind="constraint",
        before_constraint_id=getattr(pair, "a_id", None),
        after_constraint_id=getattr(pair, "b_id", None),
        before_semantic_identity=_semantic_identity(left),
        after_semantic_identity=_semantic_identity(right),
        constraint_type=getattr(pair, "constraint_type", None),
        before_scenario_scope=_scenario_scope(before, left) if left is not None else None,
        after_scenario_scope=_scenario_scope(after, right) if right is not None else None,
        semantic_field_changes=field_changes,
        source_before=before_source,
        source_after=after_source,
        evidence=evidence,
        details=details,
    )


def _readiness_changes(before: ConstraintReadinessReport | None, after: ConstraintReadinessReport | None,
                      before_snapshot: LineageSnapshot, after_snapshot: LineageSnapshot) -> tuple[LineageEvent, ...]:
    if before is None or after is None:
        return ()
    before_requirements = {item.id: item.status.value for item in before.requirements}
    after_requirements = {item.id: item.status.value for item in after.requirements}
    changed_requirements = [
        {"requirement_id": key, "before": before_requirements.get(key), "after": after_requirements.get(key)}
        for key in sorted(set(before_requirements) | set(after_requirements))
        if before_requirements.get(key) != after_requirements.get(key)
    ]
    before_findings = sorted(item.id for item in before.findings)
    after_findings = sorted(item.id for item in after.findings)
    if (before.status == after.status and not changed_requirements and before_findings == after_findings):
        return ()
    details = {
        "before_status": before.status.value,
        "after_status": after.status.value,
        "changed_requirements": changed_requirements,
        "before_finding_ids": before_findings,
        "after_finding_ids": after_findings,
        "temporal_correlation_only": True,
        "causality": "not_inferred",
    }
    return (_event(
        LineageEventKind.READINESS_CHANGED, subject_kind="readiness", subject_id="readiness_comparison",
        message="Readiness reports differ across supplied snapshots; no causal attribution was inferred.",
        snapshot_identity=after_snapshot.snapshot_identity, current_canonical=True, details=details,
    ),)


def _semantic_identity(constraint: Constraint | None) -> str | None:
    if constraint is None or has_unsupported_options(constraint):
        return None
    return stable_hash(normalize_constraint(constraint))


def _event(kind: LineageEventKind, *, subject_kind: str, subject_id: str, message: str,
           snapshot_identity: str, constraint_id: str | None = None, candidate_id: str | None = None,
           application_id: str | None = None, scenario_id: str | None = None,
           current_canonical: bool = False, stale: bool = False,
           evidence_ids: Iterable[str] = (), details: Mapping[str, Any] | None = None) -> LineageEvent:
    normalized_details = _id_value(dict(details or {}))
    identity = {
        "kind": kind.value,
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "snapshot_identity": snapshot_identity,
        "constraint_id": constraint_id,
        "candidate_id": candidate_id,
        "application_id": application_id,
        "scenario_id": scenario_id,
        "current_canonical": current_canonical,
        "stale": stale,
        "evidence_ids": sorted(set(evidence_ids)),
        "details": normalized_details,
    }
    return LineageEvent(
        id="LNE-" + stable_hash(identity)[:20], kind=kind, subject_kind=subject_kind, subject_id=subject_id,
        message=message, snapshot_identity=snapshot_identity, constraint_id=constraint_id,
        candidate_id=candidate_id, application_id=application_id, scenario_id=scenario_id,
        current_canonical=current_canonical, stale=stale, evidence_ids=tuple(sorted(set(evidence_ids))),
        details=dict(details or {}),
    )


def _change_id(kind: LineageChangeKind, subject_kind: str, before_id: str | None, after_id: str | None,
              before_semantic: str | None, after_semantic: str | None, details: Mapping[str, Any]) -> str:
    return "LNC-" + stable_hash({
        "kind": kind.value, "subject_kind": subject_kind, "before_id": before_id, "after_id": after_id,
        "before_semantic": before_semantic, "after_semantic": after_semantic, "details": _id_value(details),
    })[:20]


def _mapping_tuple(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _dedupe_strings(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in values if value}))


def _evidence_ids(values: Iterable[Any]) -> tuple[str, ...]:
    """Reference retained evidence IDs without manufacturing Evidence objects."""
    identifiers: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            identifier = value.get("id")
        else:
            identifier = getattr(value, "id", None)
        if identifier:
            identifiers.add(str(identifier))
    return tuple(sorted(identifiers))


def _dedupe_evidence(values: Iterable[Any]) -> tuple[Any, ...]:
    output: dict[str, Any] = {}
    for value in values:
        output.setdefault(str(getattr(value, "id", stable_hash(_id_value(value)))), value)
    return tuple(output[key] for key in sorted(output))


def _select_primary_link(links: tuple[_ApplicationLink, ...]) -> _ApplicationLink | None:
    if not links:
        return None
    # Applied creation is the strongest retained link. An already-present
    # receipt is traceable, but never recast as a creation.
    return min(links, key=lambda item: (
        item.status.upper() != "APPLIED", item.stale, item.application_id,
    ))


def _enum_text(value: Any) -> str:
    return str(value.value) if hasattr(value, "value") else str(value or "")


def _id_value(value: Any) -> Any:
    """Stable identity input excluding display-only execution timestamps."""
    if isinstance(value, Mapping):
        return {str(key): _id_value(value[key]) for key in sorted(value, key=str)
                if str(key) not in _VOLATILE_ID_KEYS}
    if isinstance(value, (list, tuple)):
        return [_id_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_id_value(item) for item in value), key=repr)
    enum_value = getattr(value, "value", None)
    if enum_value is not None:
        return _id_value(enum_value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "to_dict"):
        return _id_value(value.to_dict())
    return str(value)


__all__ = [
    "ConstraintLineageEngine",
    "build_constraint_lineage",
    "compare_constraint_lineage",
]
