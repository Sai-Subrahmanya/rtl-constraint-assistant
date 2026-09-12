"""Step-30 end-to-end RTL → application → UCM → readiness → lineage path."""

from __future__ import annotations

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
from rca.lineage import LineageChangeKind, LineageSource, build_constraint_lineage
from rca.provenance import AssumptionLedger
from rca.readiness import assess_constraint_readiness
from rca.search import KnowledgeEngine
from rca.validation import validate as run_validation

from .conftest import make_project


def test_rtl_to_advice_to_explicit_application_to_validation_readiness_and_lineage(tmp_path):
    path = make_project(tmp_path, name="lineage-e2e")
    # A controlled timing-graph observation stands in for supplied timing
    # intent. Inference remains advisory until the existing Step-28 action.
    raw = path.read_text(encoding="utf-8")
    raw = raw.replace("    clocks:\n    - name: clk\n      period: 10ns\n      fixed: true\n", "    clocks: []\n")
    path.write_text(raw, encoding="utf-8")
    config = load_config(path)
    design, _ = _do_parse(config)
    timing = _do_timing(config, design)
    timing.clocks["clk"].period_seconds = 10e-9
    timing.clocks["clk"].source_of_value = "TOOL"
    canonical = ConstraintSet(name=config.project.name)
    before = canonical.clone()

    advisory = InferenceEngine().infer_candidates(
        design, timing, config, canonical, AssumptionLedger(), knowledge=KnowledgeEngine(),
        run_ts="2000-01-01T00:00:00+00:00",
    )
    candidate = next(item for item in advisory.candidates if item.decision == InferenceDecision.ACCEPTABLE)
    receipt = apply_intent_decision(
        ConstraintApplication(candidate, IntentDecision(candidate.id, IntentDecisionKind.ACCEPT)),
        canonical, design, timing, config,
    )
    assert receipt.status == ApplicationStatus.APPLIED
    validation = run_validation(design, timing, canonical, backend="generic")
    readiness = assess_constraint_readiness(
        config, canonical, design, timing, validation=validation, inference_report=advisory,
    )
    before_validation = run_validation(design, timing, before, backend="generic")
    before_readiness = assess_constraint_readiness(
        config, before, design, timing, validation=before_validation,
    )
    canonical_before = canonical.to_canonical_json()

    report = build_constraint_lineage(
        canonical, config=config, design=design, timing_graph=timing,
        inference_report=advisory, application_receipts=(receipt,), validation=validation,
        readiness=readiness, before=before, before_readiness=before_readiness,
    )
    entry = next(item for item in report.constraints if item.constraint_id == receipt.applied_constraint_ids[0])

    assert entry.source == LineageSource.EXPLICITLY_ACCEPTED
    assert entry.candidate_id == candidate.id and entry.application_id == receipt.application_id
    assert entry.validation_events
    assert report.readiness is readiness
    added = next(change for change in report.change_set.changes
                 if change.kind == LineageChangeKind.ADDED and change.after_constraint_id == entry.constraint_id)
    assert added.source_after == LineageSource.EXPLICITLY_ACCEPTED
    assert added.evidence
    assert canonical.to_canonical_json() == canonical_before
