"""Step-28 controlled application and intent-resolution tests."""

from __future__ import annotations

import copy
from dataclasses import replace

from rca.config.model import MCMMConfig, ScenarioSpec
from rca.constraint_model import Constraint, ConstraintSet
from rca.constraint_model.scenarios import Scenario
from rca.explanation import explain_constraint_application
from rca.inference import (
    ApplicationStatus,
    ConstraintApplication,
    InferenceDecision,
    InferenceEngine,
    IntentDecision,
    IntentDecisionKind,
    apply_intent_decision,
    candidate_semantic_identity,
)
from rca.provenance import AssumptionLedger
from rca.search import KnowledgeEngine, KnowledgeOrigin, TrustLevel
from rca.utils.enums import Confidence, ConstraintStatus, ConstraintType, SourceKind
from rca.validation.base import ValidationIssue, ValidationReport
from rca.validation.engine import ValidationResult

from .test_inference_candidates import _context

_SOURCE = "module m(input clk, d, output reg q); always_ff @(posedge clk) q <= d; endmodule"


def _candidate(*, cset: ConstraintSet | None = None):
    design, timing, config = _context(_SOURCE)
    # A controlled fake timing-graph observation stands in for a trusted prior
    # tool input. Inference does not invent this value.
    timing.clocks["clk"].period_seconds = 10e-9
    timing.clocks["clk"].source_of_value = "TOOL"
    baseline = cset or ConstraintSet(name="m")
    report = InferenceEngine().infer_candidates(
        design, timing, config, baseline, AssumptionLedger(),
        run_ts="2000-01-01T00:00:00+00:00",
    )
    candidate = next(item for item in report.candidates if item.decision == InferenceDecision.ACCEPTABLE)
    return design, timing, config, baseline, candidate


def _request(candidate, kind: IntentDecisionKind = IntentDecisionKind.ACCEPT, *, dry_run: bool = False,
             scenarios: tuple[str, ...] = ()) -> ConstraintApplication:
    return ConstraintApplication(
        candidate=candidate,
        decision=IntentDecision(candidate_id=candidate.id, kind=kind, rationale="reviewed by test"),
        scenario_ids=scenarios,
        dry_run=dry_run,
    )


def _with_templates(candidate, templates: list[dict]):
    altered = replace(candidate, constraint_template=None, constraint_templates=tuple(copy.deepcopy(templates)),
                      candidate_semantic_identity=None)
    return replace(altered, candidate_semantic_identity=candidate_semantic_identity(altered))


def test_typed_decision_and_application_status_axes_are_distinct_and_complete():
    assert {item.value for item in IntentDecisionKind} == {
        "ACCEPT", "REJECT", "DEFER", "CONFIRM", "ALREADY_SATISFIED", "CONFLICT", "INVALID",
    }
    assert {item.value for item in ApplicationStatus} == {
        "NOT_APPLIED", "APPLIED", "REJECTED", "DEFERRED", "BLOCKED", "ALREADY_PRESENT",
        "FAILED_VALIDATION", "STALE",
    }


def test_explicit_accept_is_the_only_mutating_application_path_and_preserves_provenance():
    design, timing, config, cset, candidate = _candidate()
    before = cset.to_canonical_json()

    outcome = apply_intent_decision(_request(candidate), cset, design, timing, config)

    assert outcome.status == ApplicationStatus.APPLIED
    assert outcome.ucm_mutated is True
    assert outcome.ucm_before_snapshot_identity != outcome.ucm_after_snapshot_identity
    assert outcome.validation_summary == {
        "status": "PASS_WITH_WARNINGS",
        "issue_count": len(outcome.validation_issues),
        "error_count": 0,
        "unresolved_count": 0,
        "checks_run": sorted(outcome.validation_summary["checks_run"]),
    }
    assert outcome.candidate_provenance["source_kind"] == SourceKind.INFERENCE.value
    assert cset.to_canonical_json() != before
    applied = cset.get(outcome.applied_constraint_ids[0])
    assert applied is not None and applied.source_kind == SourceKind.INFERENCE
    evidence = {item.rule_id for item in applied.provenance.evidence}
    assert {"INTENT-APPLICATION", "INTENT-APPLICATION-VALIDATION", "INFERENCE-ACCEPT"} <= evidence
    receipt = cset.metadata["constraint_applications"][outcome.application_id]
    assert receipt["candidate_id"] == candidate.id
    assert receipt["candidate_semantic_identity"] == candidate.candidate_semantic_identity
    explanation = explain_constraint_application(outcome)
    assert "Explicit decision: ACCEPT" in explanation
    assert "Origin: advisory inference" in explanation
    assert "INTENT-APPLICATION" in explanation


def test_reject_defer_and_dry_run_never_mutate_ucm():
    for decision in (IntentDecisionKind.REJECT, IntentDecisionKind.DEFER):
        design, timing, config, cset, candidate = _candidate()
        outcome = apply_intent_decision(_request(candidate, decision), cset, design, timing, config)
        assert outcome.status in {ApplicationStatus.REJECTED, ApplicationStatus.DEFERRED}
        assert outcome.ucm_mutated is False
        if decision == IntentDecisionKind.REJECT:
            assert outcome.rejected_constraint_ids == (candidate.constraint_template["id"],)
        assert len(cset) == 0

    design, timing, config, cset, candidate = _candidate()
    preview = apply_intent_decision(_request(candidate, dry_run=True), cset, design, timing, config)
    assert preview.status == ApplicationStatus.NOT_APPLIED
    assert preview.ucm_mutated is False
    assert preview.validation_status == "PASS_WITH_WARNINGS"
    assert len(cset) == 0 and not cset.metadata


def test_stale_design_timing_and_ucm_snapshots_are_rejected_without_mutation():
    design, timing, config, cset, candidate = _candidate()
    design.top_module = "different"
    stale_design = apply_intent_decision(_request(candidate), cset, design, timing, config)
    assert stale_design.status == ApplicationStatus.STALE and not stale_design.ucm_mutated

    design, timing, config, cset, candidate = _candidate()
    timing.clocks["clk"].period_seconds = 8e-9
    stale_timing = apply_intent_decision(_request(candidate), cset, design, timing, config)
    assert stale_timing.status == ApplicationStatus.STALE and not stale_timing.ucm_mutated

    design, timing, config, cset, candidate = _candidate()
    cset.create_clock("other", 12e-9, source="other", source_kind=SourceKind.USER,
                      status=ConstraintStatus.FIXED, fixed=True)
    stale_ucm = apply_intent_decision(_request(candidate), cset, design, timing, config)
    assert stale_ucm.status == ApplicationStatus.STALE and not stale_ucm.ucm_mutated


def test_semantic_duplicate_is_idempotent_and_conflicting_fixed_intent_is_blocked():
    design, timing, config, cset, candidate = _candidate()
    first = apply_intent_decision(_request(candidate), cset, design, timing, config)
    second = apply_intent_decision(_request(candidate), cset, design, timing, config)
    assert first.status == ApplicationStatus.APPLIED
    assert second.status == ApplicationStatus.ALREADY_PRESENT
    assert second.already_present_ids == first.applied_constraint_ids
    assert len(cset) == 1

    design, timing, config, cset, candidate = _candidate()
    template = candidate.constraint_template
    assert template is not None
    conflict = Constraint.from_canonical_dict(copy.deepcopy(template), unknown_field_policy="error")
    conflict.id = "USER_DIFFERENT_PERIOD"
    conflict.values["period"] = 8e-9
    conflict.source_kind = SourceKind.USER
    conflict.status = ConstraintStatus.FIXED
    cset.add(conflict)
    # Generate advice against the current fixed UCM, then prove the Step-28
    # request cannot override it. This avoids hiding a real conflict behind a
    # stale-snapshot result.
    report = InferenceEngine().infer_candidates(design, timing, config, cset, AssumptionLedger(),
                                                run_ts="2000-01-01T00:00:00+00:00")
    conflicting = next(item for item in report.candidates if item.kind == ConstraintType.CREATE_CLOCK.value)
    outcome = apply_intent_decision(_request(conflicting), cset, design, timing, config)
    assert outcome.status == ApplicationStatus.BLOCKED
    assert outcome.conflict_ids == ("USER_DIFFERENT_PERIOD",)
    assert cset.get("USER_DIFFERENT_PERIOD").values["period"] == 8e-9
    assert len(cset) == 1


def test_malformed_unsupported_missing_links_and_missing_evidence_are_blocked():
    design, timing, config, cset, candidate = _candidate()
    no_template = replace(candidate, constraint_template=None, candidate_semantic_identity=None)
    assert apply_intent_decision(_request(no_template), cset, design, timing, config).status == ApplicationStatus.BLOCKED

    unsupported_template = copy.deepcopy(candidate.constraint_template)
    assert unsupported_template is not None
    unsupported_template["target_refs"][0]["collection_kind"] = "unresolved"
    unsupported = replace(candidate, constraint_template=unsupported_template)
    assert apply_intent_decision(_request(unsupported), cset, design, timing, config).status == ApplicationStatus.BLOCKED

    user_origin_template = copy.deepcopy(candidate.constraint_template)
    assert user_origin_template is not None
    user_origin_template["source_kind"] = SourceKind.USER.value
    user_origin = replace(candidate, constraint_template=user_origin_template, candidate_semantic_identity=None)
    user_origin = replace(user_origin, candidate_semantic_identity=candidate_semantic_identity(user_origin))
    assert apply_intent_decision(_request(user_origin), cset, design, timing, config).status == ApplicationStatus.BLOCKED

    stripped_provenance = copy.deepcopy(candidate.constraint_template)
    assert stripped_provenance is not None
    stripped_provenance["dependency_ids"] = ["C_MISSING"]
    # An in-place alteration after review cannot reuse the engine-issued
    # application identity, even where Step-9 semantic equality ignores links.
    tampered = replace(candidate, constraint_template=stripped_provenance)
    assert apply_intent_decision(_request(tampered), cset, design, timing, config).status == ApplicationStatus.STALE

    missing_assumption_template = copy.deepcopy(candidate.constraint_template)
    assert missing_assumption_template is not None
    missing_assumption_template["assumption_ids"] = ["A_MISSING"]
    missing_assumption = replace(candidate, constraint_template=missing_assumption_template,
                                 candidate_semantic_identity=None)
    missing_assumption = replace(missing_assumption,
                                 candidate_semantic_identity=candidate_semantic_identity(missing_assumption))
    blocked_assumption = apply_intent_decision(_request(missing_assumption), cset, design, timing, config)
    assert blocked_assumption.status == ApplicationStatus.BLOCKED
    assert "Missing assumption IDs: A_MISSING" in blocked_assumption.blocking_reasons

    missing_dependency_template = copy.deepcopy(candidate.constraint_template)
    assert missing_dependency_template is not None
    missing_dependency_template["dependency_ids"] = ["C_MISSING"]
    missing_dependency = replace(candidate, constraint_template=missing_dependency_template,
                                 candidate_semantic_identity=None)
    missing_dependency = replace(missing_dependency,
                                 candidate_semantic_identity=candidate_semantic_identity(missing_dependency))
    blocked_dependency = apply_intent_decision(_request(missing_dependency), cset, design, timing, config)
    assert blocked_dependency.status == ApplicationStatus.BLOCKED
    assert "Missing dependency IDs: C_MISSING" in blocked_dependency.blocking_reasons

    no_evidence = replace(candidate, evidence=(), provenance=candidate.provenance.model_copy(update={"evidence": []}))
    assert apply_intent_decision(_request(no_evidence), cset, design, timing, config).status == ApplicationStatus.BLOCKED
    assert len(cset) == 0


def test_authoritative_validation_failure_or_unresolved_result_never_commits(monkeypatch):
    design, timing, config, cset, candidate = _candidate()
    bad_template = copy.deepcopy(candidate.constraint_template)
    assert bad_template is not None
    bad_template["values"]["period"] = None
    invalid = replace(candidate, constraint_template=bad_template, candidate_semantic_identity=None)
    invalid = replace(invalid, candidate_semantic_identity=candidate_semantic_identity(invalid))
    failed = apply_intent_decision(_request(invalid), cset, design, timing, config)
    assert failed.status == ApplicationStatus.FAILED_VALIDATION
    assert failed.ucm_mutated is False and len(cset) == 0

    from rca.inference import application as application_module
    from rca.utils.enums import ErrorCode, Severity, ValidationCategory

    def unresolved_validation(*args, **kwargs):
        trial = kwargs["cset"]
        issue = ValidationIssue(
            severity=Severity.WARNING,
            category=ValidationCategory.CLOCK,
            code=ErrorCode.CLOCK_PERIOD_MISSING,
            message="Controlled unresolved validation fixture.",
            constraint_id=next(iter(trial.constraints)),
            resolution_status="UNRESOLVED",
        )
        report = ValidationReport(issues=[issue])
        return ValidationResult(status="PASS_WITH_WARNINGS", report=report)

    monkeypatch.setattr(application_module, "run_validation", unresolved_validation)
    unresolved = apply_intent_decision(_request(candidate), cset, design, timing, config)
    assert unresolved.status == ApplicationStatus.BLOCKED
    assert unresolved.validation_status == "PASS_WITH_WARNINGS"
    assert len(cset) == 0


def test_multiconstraint_application_is_atomic_and_preserves_internal_dependencies():
    design, timing, config, cset, candidate = _candidate()
    clock = copy.deepcopy(candidate.constraint_template)
    assert clock is not None
    uncertainty = Constraint(
        id="UNC__clk", type=ConstraintType.SET_CLOCK_UNCERTAINTY, target_objects=["clk"],
        clock_refs=["clk"], values={"uncertainty": 100e-12}, source_kind=SourceKind.INFERENCE,
        confidence=Confidence.MEDIUM, dependency_ids=[clock["id"]],
    ).to_canonical_dict()
    multi = _with_templates(candidate, [clock, uncertainty])
    outcome = apply_intent_decision(_request(multi), cset, design, timing, config)
    assert outcome.status == ApplicationStatus.APPLIED
    assert set(outcome.applied_constraint_ids) == {clock["id"], "UNC__clk"}
    assert cset.get("UNC__clk").dependency_ids == [clock["id"]]
    assert "UNC__clk" in cset.get(clock["id"]).downstream_ids

    design, timing, config, cset, candidate = _candidate()
    clock = copy.deepcopy(candidate.constraint_template)
    assert clock is not None
    invalid_delay = Constraint(
        id="BAD_DELAY", type=ConstraintType.SET_INPUT_DELAY, target_objects=["d"], clock_refs=["missing"],
        values={"clock": "missing", "delay": 1e-9}, source_kind=SourceKind.INFERENCE,
    ).to_canonical_dict()
    invalid_multi = _with_templates(candidate, [clock, invalid_delay])
    blocked = apply_intent_decision(_request(invalid_multi), cset, design, timing, config)
    assert blocked.status == ApplicationStatus.FAILED_VALIDATION
    assert len(cset) == 0


def test_mcmm_scope_must_be_explicit_active_and_never_broadened():
    design, timing, config, cset, candidate = _candidate()
    config.scenarios = [
        ScenarioSpec(id="FAST", mode="functional", corner="fast", active=True),
        ScenarioSpec(id="SLOW", mode="functional", corner="slow", active=False),
    ]
    config.mcmm = MCMMConfig(enabled=True, active_scenario_ids=["FAST"])
    report = InferenceEngine().infer_candidates(design, timing, config, cset, AssumptionLedger(),
                                                run_ts="2000-01-01T00:00:00+00:00")
    candidate = next(item for item in report.candidates if item.decision == InferenceDecision.ACCEPTABLE)
    scoped = copy.deepcopy(candidate.constraint_template)
    assert scoped is not None
    scoped["scenario_ids"] = ["FAST"]
    scoped_candidate = replace(candidate, constraint_template=scoped, candidate_semantic_identity=None)
    scoped_candidate = replace(scoped_candidate, candidate_semantic_identity=candidate_semantic_identity(scoped_candidate))

    missing_scope = apply_intent_decision(_request(scoped_candidate), cset, design, timing, config)
    assert missing_scope.status == ApplicationStatus.BLOCKED
    active = apply_intent_decision(_request(scoped_candidate, scenarios=("FAST",)), cset, design, timing, config)
    assert active.status == ApplicationStatus.APPLIED
    assert cset.get(active.applied_constraint_ids[0]).scenario_ids == ["FAST"]

    design, timing, config, cset, candidate = _candidate()
    config.scenarios = [ScenarioSpec(id="SLOW", mode="functional", corner="slow", active=False)]
    config.mcmm = MCMMConfig(enabled=True, active_scenario_ids=[])
    report = InferenceEngine().infer_candidates(design, timing, config, cset, AssumptionLedger(),
                                                run_ts="2000-01-01T00:00:00+00:00")
    candidate = next(item for item in report.candidates if item.decision == InferenceDecision.ACCEPTABLE)
    inactive_template = copy.deepcopy(candidate.constraint_template)
    assert inactive_template is not None
    inactive_template["scenario_ids"] = ["SLOW"]
    inactive = replace(candidate, constraint_template=inactive_template, candidate_semantic_identity=None)
    inactive = replace(inactive, candidate_semantic_identity=candidate_semantic_identity(inactive))
    outcome = apply_intent_decision(_request(inactive, scenarios=("SLOW",)), cset, design, timing, config)
    assert outcome.status == ApplicationStatus.BLOCKED
    assert "inactive scenario IDs: SLOW" in outcome.blocking_reasons[0]


def test_knowledge_conflict_is_preserved_as_block_and_results_are_deterministic():
    design, timing, config, cset, candidate = _candidate()
    known = Constraint.from_canonical_dict(copy.deepcopy(candidate.constraint_template), unknown_field_policy="error")
    known.values["period"] = 8e-9
    history = ConstraintSet(name="history")
    history.add(known)
    knowledge = KnowledgeEngine(include_builtins=False)
    knowledge.index_constraint_set(history, origin=KnowledgeOrigin.HISTORY, trust_level=TrustLevel.VALIDATED)
    report = InferenceEngine().infer_candidates(design, timing, config, cset, AssumptionLedger(), knowledge=knowledge,
                                                run_ts="2000-01-01T00:00:00+00:00")
    conflicted = next(item for item in report.candidates if item.id == candidate.id)
    first = apply_intent_decision(_request(conflicted), cset, design, timing, config).to_dict()
    second = apply_intent_decision(_request(conflicted), cset, design, timing, config).to_dict()
    assert first == second
    assert first["status"] == ApplicationStatus.BLOCKED.value
    assert cset.constraints == {}


def test_explicit_already_satisfied_decision_verifies_without_mutation():
    design, timing, config, cset, candidate = _candidate()
    assert apply_intent_decision(_request(candidate), cset, design, timing, config).status == ApplicationStatus.APPLIED
    verified = apply_intent_decision(
        _request(candidate, IntentDecisionKind.ALREADY_SATISFIED), cset, design, timing, config,
    )
    assert verified.status == ApplicationStatus.ALREADY_PRESENT
    assert verified.ucm_mutated is False


def test_existing_scenario_registry_is_respected_without_second_scenario_model():
    design, timing, config, cset, candidate = _candidate()
    cset.scenarios["FAST"] = Scenario(id="FAST", mode="functional", corner="fast", active=True)
    config.scenarios = [ScenarioSpec(id="FAST", mode="functional", corner="fast", active=True)]
    config.mcmm = MCMMConfig(enabled=True, active_scenario_ids=["FAST"])
    report = InferenceEngine().infer_candidates(design, timing, config, cset, AssumptionLedger(),
                                                run_ts="2000-01-01T00:00:00+00:00")
    candidate = next(item for item in report.candidates if item.decision == InferenceDecision.ACCEPTABLE)
    template = copy.deepcopy(candidate.constraint_template)
    assert template is not None
    template["scenario_ids"] = ["FAST"]
    scoped = replace(candidate, constraint_template=template, candidate_semantic_identity=None)
    scoped = replace(scoped, candidate_semantic_identity=candidate_semantic_identity(scoped))
    result = apply_intent_decision(_request(scoped, scenarios=("FAST",)), cset, design, timing, config)
    assert result.status == ApplicationStatus.APPLIED
    assert cset.scenarios["FAST"].corner == "fast"
