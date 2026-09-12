"""Golden deterministic projections for Step-30 lineage and semantic audit."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from rca.constraint_model import Constraint, ConstraintSet
from rca.constraint_model.scenarios import Scenario
from rca.exceptions.formal_backend import VerificationResult
from rca.lineage import build_constraint_lineage, compare_constraint_lineage
from rca.parser import SlangAdapter
from rca.provenance import Evidence, ImportMetadata, ProvenanceRecord
from rca.utils.enums import Confidence, ConstraintType, SourceKind, VerificationStatus
from rca.validation.base import ValidationReport
from rca.validation.coverage import CoverageReport
from rca.validation.engine import ValidationResult
from tests.unit.test_constraint_lineage import _accepted_context

_FIXTURES = Path(__file__).with_name("lineage")
_RTL_FIXTURE = _FIXTURES / "lineage_fixture_m.sv"


@contextmanager
def _fixed_rtl_parse():
    """Give the pre-existing inference fixture a stable source-file identity."""
    def parse(_source: str):
        return SlangAdapter().parse([str(_RTL_FIXTURE)], top="m")

    with patch("tests.unit.test_inference_candidates._parse", parse):
        yield


def _accepted_projection() -> dict:
    with _fixed_rtl_parse():
        design, timing, config, cset, advisory, _candidate, receipt = _accepted_context(knowledge=True)
    report = build_constraint_lineage(
        cset, config=config, design=design, timing_graph=timing,
        inference_report=advisory, application_receipts=(receipt,),
    )
    return report.to_dict()


def _semantic_diff_projection() -> dict:
    before = ConstraintSet(name="golden-diff", created_at="2000-01-01T00:00:00+00:00")
    clock = before.create_clock("clk", 10e-9, source="clk", source_kind=SourceKind.USER)
    before.add_scenario(Scenario(id="FAST", mode="functional", corner="fast"))
    after = before.clone()
    after.get(clock.id).values["period"] = "8ns"
    after.add_scenario(Scenario(id="SLOW", mode="functional", corner="slow"))
    after.create_clock("added", 4e-9, source="added", source_kind=SourceKind.USER)
    return compare_constraint_lineage(before, after).to_dict()


def _stale_projection() -> dict:
    with _fixed_rtl_parse():
        design, timing, config, cset, advisory, candidate, receipt = _accepted_context()
    stale_candidate = replace(candidate, source_snapshot_identity={
        **candidate.source_snapshot_identity, "config": "stale",
    })
    report = build_constraint_lineage(
        cset, config=config, design=design, timing_graph=timing,
        inference_report=replace(advisory, candidates=[stale_candidate]),
        application_receipts=(replace(receipt, ucm_after_snapshot_identity="stale"),),
    )
    return report.to_dict()


def _source_classification_projection() -> dict:
    epoch = "2000-01-01T00:00:00+00:00"
    cset = ConstraintSet(name="golden-sources", created_at=epoch)
    user = cset.create_clock("user", 10e-9, source="user", source_kind=SourceKind.USER)
    user.provenance = ProvenanceRecord(source_kind=SourceKind.USER, created_by="user", created_at=epoch)
    imported = Constraint(
        id="IMP-1", type=ConstraintType.CREATE_CLOCK, target_objects=["imp"],
        values={"name": "imp", "period": 10e-9, "source": "imp"}, source_kind=SourceKind.EXISTING_SDC,
        provenance=ProvenanceRecord(
            source_kind=SourceKind.EXISTING_SDC, created_by="sdc_parser", created_at=epoch,
            import_meta=ImportMetadata(source_file="prior.sdc", source_line=7,
                                       original_command="create_clock -period 10 [get_ports imp]",
                                       import_timestamp=epoch),
        ),
    )
    inferred = Constraint(
        id="INF-1", type=ConstraintType.CREATE_CLOCK, target_objects=["inf"],
        values={"name": "inf", "period": 8e-9, "source": "inf"}, source_kind=SourceKind.INFERENCE,
        provenance=ProvenanceRecord(
            source_kind=SourceKind.INFERENCE, rule_id="CLK-001", created_at=epoch,
            evidence=[Evidence(id="E-INF", kind="structural", description="clock observation",
                               source_objects=["inf"], confidence=Confidence.HIGH, rule_id="CLK-001",
                               created_at=epoch)],
        ),
    )
    knowledge = Constraint(
        id="KNOW-1", type=ConstraintType.CREATE_CLOCK, target_objects=["know"],
        values={"name": "know", "period": 8e-9, "source": "know"}, source_kind=SourceKind.INFERENCE,
        provenance=ProvenanceRecord(
            source_kind=SourceKind.INFERENCE, created_at=epoch,
            evidence=[Evidence(id="E-KNOW", kind="knowledge", description="advisory knowledge reference",
                               source_objects=["know"], confidence=Confidence.MEDIUM, rule_id="KNOWLEDGE-REUSE",
                               detail={"knowledge_item_id": "K-1"}, created_at=epoch)],
        ),
    )
    system = Constraint(
        id="SYS-1", type=ConstraintType.CREATE_CLOCK, target_objects=["rtl"],
        values={"name": "rtl", "period": 12e-9, "source": "rtl"}, source_kind=SourceKind.RTL,
        provenance=ProvenanceRecord(source_kind=SourceKind.RTL, created_by="parser", created_at=epoch),
    )
    unknown = Constraint(
        id="UNK-1", type=ConstraintType.CREATE_CLOCK, target_objects=["legacy"],
        values={"name": "legacy", "period": 12e-9, "source": "legacy"}, source_kind=SourceKind.INFERENCE,
        provenance=ProvenanceRecord(source_kind=SourceKind.INFERENCE, created_at=epoch),
    )
    cset.add(imported); cset.add(inferred); cset.add(knowledge); cset.add(system); cset.add(unknown)
    report = build_constraint_lineage(cset)
    return {item.constraint_id: {
        "source": item.source.value,
        "origin_source": item.origin_source.value,
        "rule_id": item.provenance.rule_id if item.provenance else None,
        "import_file": (item.provenance.import_meta.source_file
                        if item.provenance and item.provenance.import_meta else None),
        "linkage_gaps": list(item.linkage_gaps),
    } for item in report.constraints}


def _validation_formal_projection() -> dict:
    epoch = "2000-01-01T00:00:00+00:00"
    cset = ConstraintSet(name="golden-evidence", created_at=epoch)
    constraint = cset.create_false_path(from_set=["clk"], to_set=["q"], source_kind=SourceKind.USER)
    validation = ValidationResult(
        status="PASS", report=ValidationReport(), coverage=CoverageReport(graph_available=True),
    )
    formal = VerificationResult(constraint_id=constraint.id, status=VerificationStatus.VERIFIED,
                                tool="mock", property_checked="p")
    entry = build_constraint_lineage(cset, validation=validation, formal_results=(formal,)).constraints[0]
    return {
        "source": entry.source.value,
        "validation_event_kinds": [event.kind.value for event in entry.validation_events],
        "formal_statuses": [event.details["verification_status"] for event in entry.formal_events],
    }


def _mcmm_and_unknown_semantics_projection() -> dict:
    epoch = "2000-01-01T00:00:00+00:00"
    before = ConstraintSet(name="golden-unknown", created_at=epoch)
    before.add_scenario(Scenario(id="FAST", mode="functional", corner="fast"))
    scoped = before.create_clock("scoped", 5e-9, source="scoped", source_kind=SourceKind.USER)
    scoped.scenario_ids = ["FAST"]
    after = before.clone()
    unsupported = Constraint(id="LOAD", type=ConstraintType.SET_LOAD, target_objects=["q"], values={"value": 2.0})
    before.add(unsupported)
    after.add(unsupported.clone(new_id="LOAD"))
    changes = compare_constraint_lineage(before, after).changes
    return {
        "scenario_scope": build_constraint_lineage(before).constraints[0].scenario_scope.to_dict(),
        "unknown_constraint_ids": [item.before_constraint_id for item in changes if item.kind.value == "UNKNOWN"],
    }


def test_golden_lineage_json_projections():
    projections = {
        "inferred_accepted_constraint": _accepted_projection(),
        "source_classifications": _source_classification_projection(),
        "validation_and_formal_evidence": _validation_formal_projection(),
        "semantic_ucm_diff_mixed_added_modified_scenario": _semantic_diff_projection(),
        "mcmm_scope_and_unknown_semantics": _mcmm_and_unknown_semantics_projection(),
        "stale_evidence": _stale_projection(),
    }
    expected = json.loads((_FIXTURES / "deterministic_lineage.json").read_text(encoding="utf-8"))
    assert projections == expected
    assert _accepted_projection() == projections["inferred_accepted_constraint"]
