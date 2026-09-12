"""Step 27 advisory inference API tests.

These tests intentionally exercise the existing rule engine through the
non-mutating candidate boundary. They do not replace legacy materialization
coverage in ``test_inference.py``.
"""

from __future__ import annotations

import json
import os
import tempfile

from rca.config.model import IOPortSpec, ProjectConfig, ProjectInfo, UserClockSpec
from rca.constraint_model import Constraint, ConstraintSet
from rca.explanation import explain_inference_candidate
from rca.inference import (
    InferenceDecision,
    InferenceEngine,
    InferenceStatus,
    accept_inference_candidate,
)
from rca.parser import SlangAdapter
from rca.provenance import AssumptionLedger
from rca.search import KnowledgeEngine, KnowledgeOrigin, TrustLevel
from rca.timing_model import TimingGraph
from rca.utils.enums import Confidence, ConstraintStatus, ConstraintType, SourceKind
from rca.validation import validate as run_validation


def _parse(source: str):
    with tempfile.NamedTemporaryFile("w", suffix=".sv", delete=False) as handle:
        handle.write(source)
        path = handle.name
    try:
        return SlangAdapter().parse([path], top="m")
    finally:
        os.unlink(path)


def _context(source: str, *, clocks: list[dict[str, object]] | None = None,
             inputs: dict[str, dict[str, object]] | None = None):
    design = _parse(source)
    clock_data = []
    config = ProjectConfig(project=ProjectInfo(name="m", top="m", rtl_files=["m.sv"]))
    for item in clocks or []:
        clock_data.append({
            "name": item["name"],
            "period_seconds": item.get("period_seconds"),
            "fixed": item.get("fixed", True),
        })
        config.constraints.user.clocks.append(UserClockSpec(
            name=str(item["name"]), period=item.get("period"), fixed=bool(item.get("fixed", True)),
        ))
    for name, item in (inputs or {}).items():
        config.constraints.user.io.inputs[name] = IOPortSpec(**item)
    timing = TimingGraph.build(design, user_clocks=clock_data)
    return design, timing, config


def _infer(source: str, **kwargs):
    design, timing, config = _context(source, **kwargs)
    baseline = ConstraintSet(name="m")
    report = InferenceEngine().infer_candidates(
        design, timing, config, baseline, AssumptionLedger(), run_ts="2000-01-01T00:00:00+00:00",
    )
    return design, timing, config, baseline, report


def test_candidate_inference_is_non_mutating_and_never_guesses_clock_period():
    source = "module m(input clk, d, output reg q); always_ff @(posedge clk) q<=d; endmodule"
    design, timing, config = _context(source)
    baseline = ConstraintSet(name="m")
    design_snapshot = design.snapshot()
    timing_summary = timing.summary()
    report = InferenceEngine().infer_candidates(
        design, timing, config, baseline, AssumptionLedger(), run_ts="2000-01-01T00:00:00+00:00",
    )

    assert len(baseline) == 0
    assert baseline.ledger.to_dict()["assumptions"] == []
    assert timing.clocks["clk"].period_seconds is None
    assert design.snapshot() == design_snapshot
    assert timing.summary() == timing_summary
    assert report.constraints_added == 0
    assert any(fact["category"] == "structural_clock_role" for fact in report.structural_facts)
    missing = [item for item in report.candidates if item.kind == "create_clock"]
    assert missing and missing[0].status == InferenceStatus.INSUFFICIENT_EVIDENCE
    assert missing[0].constraint_template is None
    assert missing[0].decision == InferenceDecision.REJECTED
    assert "Numeric or semantic timing intent was not invented." in missing[0].warnings
    explanation = explain_inference_candidate(missing[0])
    assert "UCM state: NOT_ACCEPTED" in explanation
    assert "Clock period required" in explanation
    # Re-run with the same caller state: deterministic report and no mutation.
    again = InferenceEngine().infer_candidates(design, timing, config, baseline, AssumptionLedger(),
                                               run_ts="2000-01-01T00:00:00+00:00")
    assert report.to_dict() == again.to_dict()
    assert len(baseline) == 0


def test_ambiguous_io_association_preserves_hypotheses_without_selecting_clock():
    source = ("module m(input clk_a, clk_b, d, output reg qa, qb); "
              "always_ff @(posedge clk_a) qa<=d; always_ff @(posedge clk_b) qb<=d; endmodule")
    _, _, _, baseline, report = _infer(
        source,
        clocks=[
            {"name": "clk_a", "period": "10ns", "period_seconds": 10e-9},
            {"name": "clk_b", "period": "8ns", "period_seconds": 8e-9},
        ],
        inputs={"d": {"delay": "2ns"}},
    )
    candidates = [item for item in report.candidates if item.kind == ConstraintType.SET_INPUT_DELAY.value]
    assert candidates
    candidate = candidates[0]
    assert candidate.status == InferenceStatus.AMBIGUOUS
    assert candidate.decision == InferenceDecision.REJECTED
    assert candidate.constraint_template is None
    assert set(candidate.missing_information[0]["possible_values"]) == {"clk_a", "clk_b"}
    assert len(baseline) == 0


def test_generated_clock_structure_requires_confirmation_not_generated_clock_intent():
    source = "module m(input clk, output reg q); always_ff @(posedge clk) q <= ~q; endmodule"
    design, timing, config = _context(source)
    timing.generated_clock_candidates = [{"output": "clk_div", "master_clock": "clk", "detail": "counter"}]
    report = InferenceEngine().infer_candidates(design, timing, config, ConstraintSet(name="m"),
                                                 AssumptionLedger(), run_ts="2000-01-01T00:00:00+00:00")
    candidates = [item for item in report.candidates
                  if item.kind == ConstraintType.CREATE_GENERATED_CLOCK.value]
    assert candidates
    candidate = candidates[0]
    assert candidate.status == InferenceStatus.CONFIRMATION_REQUIRED
    assert candidate.decision == InferenceDecision.REQUIRES_CONFIRMATION
    assert candidate.constraint_template is None


def test_existing_ucm_disagreement_is_reported_and_never_overwritten():
    source = "module m(input clk, d, output reg q); always_ff @(posedge clk) q<=d; endmodule"
    design, timing, config = _context(source, clocks=[{
        "name": "clk", "period": "10ns", "period_seconds": 10e-9,
    }])
    baseline = ConstraintSet(name="m")
    baseline.add(Constraint(
        id="USER_CLK", type=ConstraintType.CREATE_CLOCK, target_objects=["clk", "m.clk"], clock_refs=["clk"],
        values={"name": "clk", "period": 8e-9}, source_kind=SourceKind.USER,
        confidence=Confidence.HIGH, status=ConstraintStatus.FIXED,
    ))
    snapshot = baseline.to_canonical_json()
    report = InferenceEngine().infer_candidates(design, timing, config, baseline, AssumptionLedger(),
                                                 run_ts="2000-01-01T00:00:00+00:00")
    candidate = next(item for item in report.candidates if item.kind == ConstraintType.CREATE_CLOCK.value)
    assert candidate.status == InferenceStatus.CONFLICTING
    assert candidate.decision == InferenceDecision.REJECTED
    assert candidate.existing_constraint_ids == ("USER_CLK",)
    assert baseline.to_canonical_json() == snapshot


def test_explicit_acceptance_preserves_provenance_and_validation_is_read_before_write():
    source = "module m(input clk, d, output reg q); always_ff @(posedge clk) q<=d; endmodule"
    design, timing, config = _context(source)
    # Simulate a timing graph whose period originated in trusted prior analysis;
    # this test exercises acceptance, not a rule that manufactures a period.
    timing.clocks["clk"].period_seconds = 10e-9
    timing.clocks["clk"].source_of_value = "TOOL"
    baseline = ConstraintSet(name="m")
    engine = InferenceEngine()
    report = engine.infer_candidates(design, timing, config, baseline, AssumptionLedger(),
                                     run_ts="2000-01-01T00:00:00+00:00")
    candidate = next(item for item in report.candidates
                     if item.kind == ConstraintType.CREATE_CLOCK.value
                     and item.decision == InferenceDecision.ACCEPTABLE)
    # Reporting a candidate does not affect established coverage. Only the
    # explicit UCM acceptance below can make the clock source constrained.
    before = run_validation(design=design, tg=timing, cset=baseline).coverage.as_dict()
    assert before["clock_source_coverage_pct"] == 0.0
    assert accept_inference_candidate(candidate, baseline, design, timing).status == "ACCEPTED"
    after = run_validation(design=design, tg=timing, cset=baseline).coverage.as_dict()
    assert after["clock_source_coverage_pct"] == 100.0
    assert len(baseline) == 1
    accepted = next(iter(baseline))
    assert accepted.status == ConstraintStatus.CONFIRMED
    assert any(ev.rule_id == "INFERENCE-ACCEPT" for ev in accepted.provenance.evidence)
    assert baseline.metadata["accepted_inference"][accepted.id]["candidate_id"] == candidate.id
    duplicate = engine.accept_candidate(candidate, baseline, design, timing)
    assert duplicate.status == "REJECTED"  # stale source snapshot, no duplicate write
    assert len(baseline) == 1


def test_knowledge_disagreement_is_retained_and_cannot_promote_candidate():
    source = "module m(input clk, d, output reg q); always_ff @(posedge clk) q<=d; endmodule"
    design, timing, config = _context(source)
    timing.clocks["clk"].period_seconds = 10e-9
    timing.clocks["clk"].source_of_value = "TOOL"
    initial = InferenceEngine().infer_candidates(design, timing, config, ConstraintSet(name="m"),
                                                  AssumptionLedger(), run_ts="2000-01-01T00:00:00+00:00")
    template = next(item.constraint_template for item in initial.candidates
                    if item.kind == ConstraintType.CREATE_CLOCK.value)
    known_constraint = Constraint.from_canonical_dict(template, unknown_field_policy="error")
    known_constraint.values["period"] = 8e-9
    known = ConstraintSet(name="known")
    known.add(known_constraint)
    knowledge = KnowledgeEngine(include_builtins=False)
    knowledge.index_constraint_set(known, origin=KnowledgeOrigin.HISTORY, trust_level=TrustLevel.VALIDATED)

    report = InferenceEngine().infer_candidates(design, timing, config, ConstraintSet(name="m"),
                                                AssumptionLedger(), knowledge=knowledge,
                                                run_ts="2000-01-01T00:00:00+00:00")
    candidate = next(item for item in report.candidates if item.kind == ConstraintType.CREATE_CLOCK.value)
    assert candidate.status == InferenceStatus.CONFLICTING
    assert candidate.decision == InferenceDecision.REJECTED
    assert any(ref["match_kind"] == "same_scope_different_values"
               for ref in candidate.knowledge_references)
    assert report.constraints_added == 0
    assert report.conflicts


def test_candidate_json_is_stably_ordered_and_marks_not_accepted():
    source = "module m(input clk, d, output reg q); always_ff @(posedge clk) q<=d; endmodule"
    _, _, _, _, report = _infer(source)
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    decoded = json.loads(encoded)
    assert all(item["acceptance_state"] == "NOT_ACCEPTED" for item in decoded["candidates"])
    assert decoded["candidates"] == sorted(decoded["candidates"], key=lambda item: item["id"])


def test_advanced_ambiguous_hypotheses_are_deterministic_and_never_selected():
    source = ("module m(input clk_a, clk_b, output reg qa, qb); "
              "always_ff @(posedge clk_a) qa<=1'b0; always_ff @(posedge clk_b) qb<=1'b0; endmodule")
    design, timing, config = _context(source)
    cset = ConstraintSet(name="m")
    report = InferenceEngine().infer_candidates(design, timing, config, cset, AssumptionLedger(),
                                                run_ts="2000-01-01T00:00:00+00:00")
    ambiguous = [item for item in report.candidates if item.status == InferenceStatus.AMBIGUOUS]
    assert ambiguous
    for candidate in ambiguous:
        assert candidate.decision == InferenceDecision.REJECTED
        assert all(item["status"] == "UNCONFIRMED" for item in candidate.hypotheses)
    repeated = InferenceEngine().infer_candidates(design, timing, config, cset, AssumptionLedger(),
                                                   run_ts="2000-01-01T00:00:00+00:00")
    assert [item.to_dict().get("hypotheses") for item in report.candidates] == [
        item.to_dict().get("hypotheses") for item in repeated.candidates
    ]
