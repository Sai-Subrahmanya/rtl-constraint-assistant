"""Step-28 end-to-end controlled-application workflow."""

from __future__ import annotations

import copy

from rca.cli.main import _do_parse, _do_timing
from rca.config import load_config
from rca.constraint_model import ConstraintSet
from rca.inference import (
    ApplicationStatus,
    ConstraintApplication,
    InferenceDecision,
    InferenceEngine,
    IntentDecision,
    IntentDecisionKind,
    apply_intent_decision,
)
from rca.provenance import AssumptionLedger
from rca.sdc import get_backend
from rca.search import KnowledgeEngine
from rca.validation import validate as run_validation

from .conftest import make_project


def test_rtl_to_advice_to_explicit_application_to_ucm_validation_coverage_and_sdc(tmp_path):
    """A candidate remains advisory until the one explicit application call."""
    path = make_project(tmp_path)
    raw = path.read_text(encoding="utf-8")
    # Keep the fixture deterministic but remove config-owned clock intent: the
    # period below stands for a controlled external timing-graph observation.
    raw = raw.replace("    clocks:\n    - name: clk\n      period: 10ns\n      fixed: true\n", "    clocks: []\n")
    path.write_text(raw, encoding="utf-8")
    config = load_config(path)
    design, _ = _do_parse(config)
    timing = _do_timing(config, design)
    timing.clocks["clk"].period_seconds = 10e-9
    timing.clocks["clk"].source_of_value = "TOOL"
    canonical_ucm = ConstraintSet(name=config.project.name)

    # Knowledge is explicitly advisory: attaching it to inference does not
    # materialize a UCM constraint or establish a timing value.
    report = InferenceEngine().infer_candidates(
        design, timing, config, canonical_ucm, AssumptionLedger(), knowledge=KnowledgeEngine(),
        run_ts="2000-01-01T00:00:00+00:00",
    )
    candidate = next(item for item in report.candidates if item.decision == InferenceDecision.ACCEPTABLE)
    before = run_validation(design, timing, canonical_ucm)
    assert len(canonical_ucm) == 0
    assert before.coverage.as_dict()["clock_source_coverage_pct"] == 0.0

    outcome = apply_intent_decision(
        ConstraintApplication(candidate, IntentDecision(candidate.id, IntentDecisionKind.ACCEPT)),
        canonical_ucm, design, timing, config,
    )
    assert outcome.status == ApplicationStatus.APPLIED
    assert outcome.ucm_mutated is True
    assert len(canonical_ucm) == 1
    applied = canonical_ucm.get(outcome.applied_constraint_ids[0])
    assert applied is not None and applied.source_kind.value == "INFERENCE"
    assert any(item.rule_id == "INTENT-APPLICATION" for item in applied.provenance.evidence)

    after = run_validation(design, timing, canonical_ucm)
    assert after.coverage.as_dict()["clock_source_coverage_pct"] == 100.0
    generated = get_backend("generic").generate(canonical_ucm, design_name=config.project.name)
    assert outcome.applied_constraint_ids[0] in generated.emitted_constraint_ids
    assert "create_clock" in generated.text

    # Failure/staleness after advice cannot leave a partial canonical UCM.
    stale_ucm = ConstraintSet(name="stale")
    stale_timing = timing.model_copy(deep=True)
    stale_timing.clocks["clk"].period_seconds = 8e-9
    stale = apply_intent_decision(
        ConstraintApplication(candidate, IntentDecision(candidate.id, IntentDecisionKind.ACCEPT)),
        stale_ucm, design, stale_timing, config,
    )
    assert stale.status == ApplicationStatus.STALE
    assert stale.ucm_mutated is False
    assert stale_ucm.constraints == {}

    # The report itself is immutable advisory data rather than a hidden UCM.
    assert candidate.constraint_template == copy.deepcopy(candidate.constraint_template)
