"""Golden semantic views of Step-28 controlled-application receipts."""

from __future__ import annotations

from rca.constraint_model import ConstraintSet
from rca.inference import (
    ConstraintApplication,
    InferenceDecision,
    InferenceEngine,
    IntentDecision,
    IntentDecisionKind,
    apply_intent_decision,
)
from rca.provenance import AssumptionLedger
from tests.unit.test_inference_candidates import _context

_SOURCE = "module m(input clk, d, output reg q); always_ff @(posedge clk) q <= d; endmodule"


def _advisory_context():
    design, timing, config = _context(_SOURCE)
    timing.clocks["clk"].period_seconds = 10e-9
    timing.clocks["clk"].source_of_value = "TOOL"
    cset = ConstraintSet(name="m")
    report = InferenceEngine().infer_candidates(
        design, timing, config, cset, AssumptionLedger(),
        run_ts="2000-01-01T00:00:00+00:00",
    )
    candidate = next(item for item in report.candidates if item.decision == InferenceDecision.ACCEPTABLE)
    return design, timing, config, cset, candidate


def _view(receipt):
    """Exclude content-addressed IDs while retaining the public semantics."""
    return {
        "decision": receipt.decision.value,
        "status": receipt.status.value,
        "mutated": receipt.ucm_mutated,
        "applied_count": len(receipt.applied_constraint_ids),
        "present_count": len(receipt.already_present_ids),
        "conflict_count": len(receipt.conflict_ids),
        "validation_status": receipt.validation_status,
        "blockers": list(receipt.blocking_reasons),
        "evidence_rules": sorted(item.rule_id for item in receipt.application_evidence),
        "snapshot_changed": receipt.ucm_before_snapshot_identity != receipt.ucm_after_snapshot_identity,
    }


def test_golden_explicit_accept_then_idempotent_retry_and_dry_run():
    design, timing, config, cset, candidate = _advisory_context()
    request = ConstraintApplication(candidate, IntentDecision(candidate.id, IntentDecisionKind.ACCEPT))
    accepted = apply_intent_decision(request, cset, design, timing, config)
    assert _view(accepted) == {
        "decision": "ACCEPT",
        "status": "APPLIED",
        "mutated": True,
        "applied_count": 1,
        "present_count": 0,
        "conflict_count": 0,
        "validation_status": "PASS_WITH_WARNINGS",
        "blockers": [],
        "evidence_rules": ["INFERENCE-ACCEPT", "INTENT-APPLICATION", "INTENT-APPLICATION-VALIDATION"],
        "snapshot_changed": True,
    }

    retry = apply_intent_decision(request, cset, design, timing, config)
    assert _view(retry) == {
        "decision": "ACCEPT",
        "status": "ALREADY_PRESENT",
        "mutated": False,
        "applied_count": 0,
        "present_count": 1,
        "conflict_count": 0,
        "validation_status": None,
        "blockers": [],
        "evidence_rules": [],
        "snapshot_changed": False,
    }

    design, timing, config, empty_ucm, candidate = _advisory_context()
    preview = apply_intent_decision(
        ConstraintApplication(candidate, IntentDecision(candidate.id, IntentDecisionKind.ACCEPT), dry_run=True),
        empty_ucm, design, timing, config,
    )
    assert _view(preview) == {
        "decision": "ACCEPT",
        "status": "NOT_APPLIED",
        "mutated": False,
        "applied_count": 0,
        "present_count": 0,
        "conflict_count": 0,
        "validation_status": "PASS_WITH_WARNINGS",
        "blockers": [],
        "evidence_rules": ["INTENT-APPLICATION", "INTENT-APPLICATION-VALIDATION"],
        "snapshot_changed": False,
    }
    assert len(empty_ucm) == 0
