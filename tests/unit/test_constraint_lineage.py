"""Step-30 lineage/audit contracts over existing authoritative evidence."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

from rca.constraint_model import Constraint, ConstraintSet
from rca.constraint_model.scenarios import Scenario
from rca.exceptions.formal_backend import VerificationResult
from rca.explanation import explain_constraint_lineage
from rca.inference import (
    ApplicationStatus,
    ConstraintApplication,
    ConstraintApplicationResult,
    InferenceDecision,
    InferenceEngine,
    IntentDecision,
    IntentDecisionKind,
    apply_intent_decision,
)
from rca.lineage import (
    ConstraintLineageEngine,
    LineageChangeKind,
    LineageEventKind,
    LineageSource,
    build_constraint_lineage,
    compare_constraint_lineage,
)
from rca.provenance import Evidence, ImportMetadata, ProvenanceRecord
from rca.readiness import (
    ConstraintReadinessReport,
    ReadinessRequirement,
    ReadinessStatus,
    assess_constraint_readiness,
)
from rca.utils.enums import (
    Confidence,
    ConstraintType,
    ErrorCode,
    Severity,
    SourceKind,
    ValidationCategory,
    VerificationStatus,
)
from rca.validation.base import ValidationIssue, ValidationReport
from rca.validation.coverage import CoverageReport
from rca.validation.engine import ValidationResult

from .test_inference_candidates import _context

_EPOCH = "2000-01-01T00:00:00+00:00"
_SOURCE = "module m(input clk, d, output reg q); always_ff @(posedge clk) q <= d; endmodule"


def _evidence(identifier: str, *, rule: str | None = None) -> Evidence:
    return Evidence(
        id=identifier, kind="structural", description=f"evidence {identifier}",
        source_objects=["clk"], confidence=Confidence.HIGH, rule_id=rule, created_at=_EPOCH,
    )


def _validation(*issues: ValidationIssue, status: str = "PASS") -> ValidationResult:
    return ValidationResult(
        status=status, report=ValidationReport(issues=list(issues)), coverage=CoverageReport(graph_available=True),
    )


def _accepted_context(*, knowledge: bool = False):
    design, timing, config = _context(_SOURCE)
    timing.clocks["clk"].period_seconds = 10e-9
    timing.clocks["clk"].source_of_value = "TOOL"
    cset = ConstraintSet(name="m", created_at=_EPOCH)
    advisory = InferenceEngine().infer_candidates(
        design, timing, config, cset, cset.ledger, run_ts=_EPOCH,
    )
    candidate = next(item for item in advisory.candidates if item.decision == InferenceDecision.ACCEPTABLE)
    if knowledge:
        candidate = replace(candidate, knowledge_references=({
            "knowledge_item_id": "K-TEST", "suggestion_id": "KS-TEST", "trust_level": "VALIDATED",
            "applicability": "APPLICABLE", "match_kind": "semantic_exact",
        },))
        advisory = replace(advisory, candidates=[candidate], conflicts=[{
            "subject": candidate.id, "knowledge_item_id": "K-CONFLICT",
            "message": "Knowledge disagreement retained.",
        }])
    receipt = apply_intent_decision(
        ConstraintApplication(candidate, IntentDecision(candidate.id, IntentDecisionKind.ACCEPT)),
        cset, design, timing, config,
    )
    return design, timing, config, cset, advisory, candidate, receipt


def test_user_imported_inferred_and_unknown_sources_are_not_relabelled():
    cset = ConstraintSet(name="sources", created_at=_EPOCH)
    user = cset.create_clock("user_clk", 10e-9, source="user_clk", source_kind=SourceKind.USER)
    user.provenance = ProvenanceRecord(
        source_kind=SourceKind.USER, created_by="user", evidence=[_evidence("E-USER")], created_at=_EPOCH,
    )
    imported = Constraint(
        id="IMP-1", type=ConstraintType.CREATE_CLOCK, target_objects=["imp_clk"],
        values={"name": "imp_clk", "period": 10e-9, "source": "imp_clk"},
        source_kind=SourceKind.EXISTING_SDC,
        provenance=ProvenanceRecord(
            source_kind=SourceKind.EXISTING_SDC, created_by="sdc_parser", created_at=_EPOCH,
            import_meta=ImportMetadata(source_file="prior.sdc", source_line=7,
                                       original_command="create_clock -period 10 [get_ports imp_clk]",
                                       import_timestamp=_EPOCH),
        ),
    )
    inferred = Constraint(
        id="INF-1", type=ConstraintType.CREATE_CLOCK, target_objects=["inf_clk"],
        values={"name": "inf_clk", "period": 8e-9, "source": "inf_clk"},
        source_kind=SourceKind.INFERENCE,
        provenance=ProvenanceRecord(source_kind=SourceKind.INFERENCE, rule_id="CLK-001", created_at=_EPOCH,
                                    evidence=[_evidence("E-INF", rule="CLK-001")]),
    )
    knowledge = Constraint(
        id="KNOW-1", type=ConstraintType.CREATE_CLOCK, target_objects=["know_clk"],
        values={"name": "know_clk", "period": 8e-9, "source": "know_clk"},
        source_kind=SourceKind.INFERENCE,
        provenance=ProvenanceRecord(
            source_kind=SourceKind.INFERENCE, created_at=_EPOCH,
            evidence=[Evidence(
                id="E-KNOW", kind="knowledge", description="advisory knowledge reference",
                source_objects=["know_clk"], confidence=Confidence.MEDIUM, rule_id="KNOWLEDGE-REUSE",
                detail={"knowledge_item_id": "K-1"}, created_at=_EPOCH,
            )],
        ),
    )
    system = Constraint(
        id="SYS-1", type=ConstraintType.CREATE_CLOCK, target_objects=["rtl_clk"],
        values={"name": "rtl_clk", "period": 12e-9, "source": "rtl_clk"},
        source_kind=SourceKind.RTL,
        provenance=ProvenanceRecord(source_kind=SourceKind.RTL, created_by="parser", created_at=_EPOCH),
    )
    unknown = Constraint(
        id="UNK-1", type=ConstraintType.CREATE_CLOCK, target_objects=["legacy_clk"],
        values={"name": "legacy_clk", "period": 12e-9, "source": "legacy_clk"},
        source_kind=SourceKind.INFERENCE,
        provenance=ProvenanceRecord(source_kind=SourceKind.INFERENCE, created_at=_EPOCH),
    )
    cset.add(imported); cset.add(inferred); cset.add(knowledge); cset.add(system); cset.add(unknown)

    report = build_constraint_lineage(cset)
    entries = {item.constraint_id: item for item in report.constraints}

    assert entries[user.id].source == LineageSource.USER_PROVIDED
    assert entries[imported.id].source == LineageSource.IMPORTED
    assert entries[inferred.id].source == LineageSource.INFERRED
    assert entries[inferred.id].provenance.rule_id == "CLK-001"
    assert entries[knowledge.id].source == LineageSource.KNOWLEDGE_REFERENCED
    assert entries[knowledge.id].origin_source == LineageSource.INFERRED
    assert entries[system.id].source == LineageSource.SYSTEM_OBSERVED
    assert entries[unknown.id].source == LineageSource.UNKNOWN
    assert entries[unknown.id].linkage_gaps
    assert entries[imported.id].provenance.import_meta.source_file == "prior.sdc"


def test_candidate_application_ucm_knowledge_and_evidence_chain_is_preserved_read_only():
    design, timing, config, cset, advisory, candidate, receipt = _accepted_context(knowledge=True)
    before = cset.to_canonical_json()
    report = build_constraint_lineage(
        cset, config=config, design=design, timing_graph=timing,
        inference_report=advisory, application_receipts=(receipt,),
    )
    entry = report.constraints[0]

    assert entry.source == LineageSource.EXPLICITLY_ACCEPTED
    assert entry.origin_source == LineageSource.INFERRED
    assert entry.candidate_id == candidate.id
    assert entry.application_id == receipt.application_id
    assert {item["knowledge_item_id"] for item in entry.knowledge_references} == {"K-TEST"}
    assert entry.knowledge_references[0]["advisory"] is True
    assert entry.knowledge_conflicts[0]["knowledge_item_id"] == "K-CONFLICT"
    assert {item.id for item in entry.evidence} >= {item.id for item in candidate.evidence}
    assert {event.kind for event in entry.events} >= {
        LineageEventKind.APPLICATION_ATTEMPTED, LineageEventKind.ACCEPTED, LineageEventKind.CREATED,
    }
    assert cset.to_canonical_json() == before
    explanation = explain_constraint_lineage(report)
    assert "EXPLICITLY_ACCEPTED" in explanation
    assert "Knowledge references (advisory): K-TEST" in explanation
    assert "read-only traceability projection" in explanation


def test_validation_and_formal_evidence_are_separate_from_constraint_source():
    cset = ConstraintSet(name="formal", created_at=_EPOCH)
    exception = cset.create_false_path(from_set=["clk"], to_set=["q"], source_kind=SourceKind.USER)
    exception.provenance = ProvenanceRecord(source_kind=SourceKind.USER, created_by="user", created_at=_EPOCH)
    issue = ValidationIssue(
        severity=Severity.INFO, category=ValidationCategory.EXCEPTION,
        code=ErrorCode.EXCEPTION_UNVERIFIED, message="Exception has no proof.", constraint_id=exception.id,
        evidence={"verification": {
            "constraint_id": exception.id, "status": "UNRESOLVED", "tool": "conservative",
            "message": "Formal proof unavailable.",
        }}, resolution_status="UNRESOLVED",
    )
    formal = VerificationResult(
        constraint_id=exception.id, status=VerificationStatus.VERIFIED, tool="mock", property_checked="p",
    )
    validation = _validation(issue, status="PASS_WITH_WARNINGS")
    validation_before = copy.deepcopy(validation.as_dict())
    provenance_before = exception.provenance.to_dict()
    report = build_constraint_lineage(cset, validation=validation, formal_results=(formal,))
    entry = report.constraints[0]

    assert entry.source == LineageSource.USER_PROVIDED
    assert entry.validation_events and entry.validation_events[0].kind == LineageEventKind.VALIDATED
    statuses = {event.details["verification_status"] for event in entry.formal_events}
    assert statuses == {"UNRESOLVED", VerificationStatus.VERIFIED.value}
    assert entry.source != LineageSource.SYSTEM_OBSERVED
    assert validation.as_dict() == validation_before
    assert exception.provenance.to_dict() == provenance_before


def test_stale_candidate_and_receipt_are_preserved_without_removal_or_refresh():
    design, timing, config, cset, advisory, candidate, receipt = _accepted_context()
    stale_candidate = replace(candidate, source_snapshot_identity={
        **candidate.source_snapshot_identity, "design": "not-current",
    })
    stale_advisory = replace(advisory, candidates=[stale_candidate])
    stale_receipt = replace(receipt, ucm_after_snapshot_identity="not-current")
    report = build_constraint_lineage(
        cset, config=config, design=design, timing_graph=timing,
        inference_report=stale_advisory, application_receipts=(stale_receipt,),
    )
    entry = report.constraints[0]

    assert any(event.kind == LineageEventKind.BECAME_STALE and event.stale for event in entry.events)
    assert any(event.kind == LineageEventKind.BECAME_STALE and event.stale
               for event in report.application_attempts)
    assert not any(event.kind == LineageEventKind.CREATED for event in entry.events)
    assert entry.current_canonical is True
    assert cset.get(entry.constraint_id) is not None


def test_rejected_deferred_and_already_present_attempts_do_not_create_constraint_events():
    cset = ConstraintSet(name="attempts", created_at=_EPOCH)
    user = cset.create_clock("clk", 10e-9, source="clk", source_kind=SourceKind.USER)
    current = cset.to_canonical_json()
    rejected = ConstraintApplicationResult(
        application_id="APP-REJECT", candidate_id="IC-REJECT", candidate_semantic_identity=None,
        decision=IntentDecisionKind.REJECT, status=ApplicationStatus.REJECTED, ucm_mutated=False,
        rejected_constraint_ids=("PROPOSAL",),
    )
    deferred = ConstraintApplicationResult(
        application_id="APP-DEFER", candidate_id="IC-DEFER", candidate_semantic_identity=None,
        decision=IntentDecisionKind.DEFER, status=ApplicationStatus.DEFERRED, ucm_mutated=False,
    )
    already = ConstraintApplicationResult(
        application_id="APP-PRESENT", candidate_id="IC-PRESENT", candidate_semantic_identity=None,
        decision=IntentDecisionKind.ALREADY_SATISFIED, status=ApplicationStatus.ALREADY_PRESENT,
        ucm_mutated=False, already_present_ids=(user.id,),
        ucm_after_snapshot_identity=build_constraint_lineage(cset).snapshot.snapshot_identity,
    )
    report = build_constraint_lineage(cset, application_receipts=(rejected, deferred, already))
    entry = report.constraints[0]

    assert cset.to_canonical_json() == current
    assert entry.source == LineageSource.USER_PROVIDED
    assert not any(event.kind == LineageEventKind.CREATED for event in entry.events)
    attempts = {event.application_id: event.kind for event in report.application_attempts
                if event.kind != LineageEventKind.APPLICATION_ATTEMPTED}
    assert attempts["APP-REJECT"] == LineageEventKind.REJECTED
    assert attempts["APP-DEFER"] == LineageEventKind.DEFERRED
    assert attempts["APP-PRESENT"] == LineageEventKind.ACCEPTED


def test_semantic_snapshot_diff_reuses_equivalence_for_unit_equivalence_changes_unknown_and_mcmm():
    before = ConstraintSet(name="diff", created_at=_EPOCH)
    clock = before.create_clock("clk", 10e-9, source="clk", source_kind=SourceKind.USER)
    before.add_scenario(Scenario(id="FAST", mode="functional", corner="fast"))
    before.add_scenario(Scenario(id="SLOW", mode="functional", corner="slow"))
    scoped = before.create_clock("scoped", 5e-9, source="scoped", source_kind=SourceKind.USER)
    scoped.scenario_ids = ["FAST"]

    equivalent = before.clone(name="diff-equivalent")
    equivalent.get(clock.id).values["period"] = "10000ps"
    equivalent.get(scoped.id).scenario_ids = ["FAST"]
    eq_changes = compare_constraint_lineage(before, equivalent).changes
    assert any(item.kind == LineageChangeKind.UNCHANGED and item.before_constraint_id == clock.id
               for item in eq_changes)

    after = before.clone(name="diff-after")
    after.get(clock.id).values["period"] = 8e-9
    after.get(scoped.id).scenario_ids = ["SLOW"]
    removed = after.get(scoped.id)
    after.remove(removed.id)
    added = after.create_clock("new_clk", 4e-9, source="new_clk", source_kind=SourceKind.USER)
    unsupported = Constraint(id="LOAD", type=ConstraintType.SET_LOAD, target_objects=["q"], values={"value": 2.0})
    before.add(unsupported)
    after.add(unsupported.clone(new_id="LOAD"))
    changes = compare_constraint_lineage(before, after).changes

    assert any(item.kind == LineageChangeKind.MODIFIED and item.before_constraint_id == clock.id
               and any(field["field"] == "period" for field in item.semantic_field_changes) for item in changes)
    assert any(item.kind == LineageChangeKind.REMOVED and item.before_constraint_id == scoped.id for item in changes)
    assert any(item.kind == LineageChangeKind.ADDED and item.after_constraint_id == added.id for item in changes)
    assert any(item.kind == LineageChangeKind.UNKNOWN and item.before_constraint_id == "LOAD" for item in changes)


def test_mcmm_scope_readiness_change_and_engine_json_are_deterministic_without_mutation():
    design, timing, config = _context(_SOURCE, clocks=[{
        "name": "clk", "period": "10ns", "period_seconds": 10e-9,
    }])
    before = ConstraintSet(name="m", created_at=_EPOCH)
    before.add_scenario(Scenario(id="FAST", mode="functional", corner="fast"))
    before.create_clock("clk", 10e-9, source="clk", source_kind=SourceKind.USER)
    after = before.clone()
    after.add_scenario(Scenario(id="SLOW", mode="functional", corner="slow"))
    after.get(before.clocks()[0].id).scenario_ids = ["FAST"]
    before_validation = _validation()
    after_validation = _validation()
    before_readiness = assess_constraint_readiness(config, before, design, timing, validation=before_validation)
    after_readiness = assess_constraint_readiness(config, after, design, timing, validation=after_validation)
    before_json = before.to_canonical_json(); after_json = after.to_canonical_json()

    first = ConstraintLineageEngine().build(
        after, config=config, design=design, timing_graph=timing, validation=after_validation,
        readiness=after_readiness, before=before, before_readiness=before_readiness,
    )
    second = build_constraint_lineage(
        after, config=config, design=design, timing_graph=timing, validation=after_validation,
        readiness=after_readiness, before=before, before_readiness=before_readiness,
    )

    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(second.to_dict(), sort_keys=True)
    assert before.to_canonical_json() == before_json and after.to_canonical_json() == after_json
    assert any(item.subject_kind == "scenario" for item in first.change_set.changes)
    # Readiness may be correlated with snapshot comparison but no causal edge is manufactured.
    for event in first.change_set.readiness_events:
        assert event.details["causality"] == "not_inferred"


def test_readiness_differences_are_explicitly_temporal_not_constraint_causality():
    before = ConstraintSet(name="readiness-diff", created_at=_EPOCH)
    before.create_clock("clk", 10e-9, source="clk", source_kind=SourceKind.USER)
    after = before.clone()
    before_readiness = ConstraintReadinessReport(
        status=ReadinessStatus.BLOCKED,
        requirements=(ReadinessRequirement(
            id="VALIDATION", category="validation", title="Validation", status=ReadinessStatus.BLOCKED,
            required=True, rationale="Existing validation evidence is blocking.",
        ),),
    )
    after_readiness = ConstraintReadinessReport(
        status=ReadinessStatus.READY,
        requirements=(ReadinessRequirement(
            id="VALIDATION", category="validation", title="Validation", status=ReadinessStatus.READY,
            required=True, rationale="Existing validation evidence is no longer blocking.",
        ),),
    )

    changes = compare_constraint_lineage(
        before, after, before_readiness=before_readiness, after_readiness=after_readiness,
    )

    assert len(changes.readiness_events) == 1
    event = changes.readiness_events[0]
    assert event.kind == LineageEventKind.READINESS_CHANGED
    assert event.details["temporal_correlation_only"] is True
    assert event.details["causality"] == "not_inferred"
