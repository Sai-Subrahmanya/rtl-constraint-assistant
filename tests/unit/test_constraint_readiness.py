"""Step-29 deterministic report-only readiness contracts."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from rca.config.model import FormalConfig, MCMMConfig, ProjectConfig
from rca.constraint_model import ConstraintSet
from rca.constraint_model.scenarios import Scenario
from rca.eda.preflight import CapabilityCheck, CapabilityStatus, EDAPreflight
from rca.explanation import explain_constraint_readiness
from rca.inference import InferenceEngine
from rca.provenance import AssumptionLedger
from rca.readiness import ConstraintReadinessEngine, ReadinessStatus, assess_constraint_readiness
from rca.utils.enums import ErrorCode, Severity, SourceKind, ValidationCategory, ValidationStatus
from rca.validation.base import ValidationIssue, ValidationReport
from rca.validation.coverage import CoverageReport
from rca.validation.engine import ValidationResult

from .test_inference_candidates import _context

_SOURCE = "module m(input clk, d, output reg q); always_ff @(posedge clk) q <= d; endmodule"


def _complete_coverage() -> CoverageReport:
    return CoverageReport(graph_available=True)


def _validation(*issues: ValidationIssue, status: str = ValidationStatus.PASS.value) -> ValidationResult:
    return ValidationResult(
        status=status,
        report=ValidationReport(issues=list(issues)),
        coverage=_complete_coverage(),
    )


def test_readiness_status_axis_and_json_are_deterministic_and_report_only():
    assert {item.value for item in ReadinessStatus} == {
        "READY", "READY_WITH_WARNINGS", "BLOCKED", "INCOMPLETE", "UNKNOWN", "UNSUPPORTED",
    }
    design, timing, config = _context(_SOURCE, clocks=[{
        "name": "clk", "period": "10ns", "period_seconds": 10e-9,
    }])
    cset = ConstraintSet(name="m")
    cset.create_clock("clk", 10e-9, source="clk", source_kind=SourceKind.USER)
    before = cset.to_canonical_json()

    first = assess_constraint_readiness(config, cset, design, timing)
    second = assess_constraint_readiness(config, cset, design, timing)

    assert first.to_dict() == second.to_dict()
    assert ConstraintReadinessEngine().assess(config, cset, design, timing).to_dict() == first.to_dict()
    assert cset.to_canonical_json() == before
    assert first.kind == "rca_constraint_readiness"
    assert first.schema_version == 1
    assert "report-only" in explain_constraint_readiness(first)
    assert first.coverage_summary == first.validation_summary["coverage"]

    # Advisory incompleteness is contextual only; it cannot lower a readiness
    # verdict unless authoritative UCM/validation evidence says so.
    advisory = InferenceEngine().infer_candidates(
        design, timing, config, cset, AssumptionLedger(), run_ts="2000-01-01T00:00:00+00:00",
    )
    advisory.candidates = [replace(advisory.candidates[0], missing_information=({"id": "NEEDS_REVIEW"},))]
    advisory_only = assess_constraint_readiness(
        config, cset, design, timing, validation=_validation(), inference_report=advisory,
    )
    assert advisory_only.status == ReadinessStatus.READY


def test_golden_json_projection_is_byte_stable():
    config = ProjectConfig(project={"name": "golden"})
    report = assess_constraint_readiness(config, ConstraintSet(name="golden"))
    actual = json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n"
    expected = (Path(__file__).parents[1] / "golden" / "readiness" / "basic_unknown.json").read_text(
        encoding="utf-8",
    )
    assert actual == expected


def test_unknown_coverage_is_not_promoted_to_complete():
    config = _context(_SOURCE)[2]
    report = assess_constraint_readiness(config, ConstraintSet(name="m"))

    assert report.status == ReadinessStatus.BLOCKED  # absent design/graph remains a hard prerequisite
    coverage = next(item for item in report.requirements if item.id == "COVERAGE_KNOWN")
    assert coverage.status == ReadinessStatus.UNKNOWN
    assert any(item.status == ReadinessStatus.UNKNOWN for item in report.findings)


def test_mixed_mcmm_results_preserve_scenario_identity_without_hiding_blocker():
    design, timing, config = _context(_SOURCE, clocks=[{
        "name": "clk", "period": "10ns", "period_seconds": 10e-9,
    }])
    config.mcmm = MCMMConfig(enabled=True, active_scenario_ids=["FAST", "SLOW"])
    cset = ConstraintSet(name="m")
    cset.add_scenario(Scenario(id="FAST", mode="functional", corner="fast"))
    cset.add_scenario(Scenario(id="SLOW", mode="functional", corner="slow"))
    issue = ValidationIssue(
        severity=Severity.ERROR,
        category=ValidationCategory.SCENARIO,
        code=ErrorCode.SCENARIO_MISMATCH,
        message="SLOW scope has a conflicting constraint.",
        scenario_id="SLOW",
        blocking=True,
    )
    report = assess_constraint_readiness(
        config, cset, design, timing, validation=_validation(issue, status=ValidationStatus.BLOCKED.value),
    )
    scenarios = {item.scenario_id: item for item in report.scenario_results}

    assert report.status == ReadinessStatus.BLOCKED
    assert scenarios["SLOW"].status == ReadinessStatus.BLOCKED
    assert scenarios["FAST"].status == ReadinessStatus.READY
    assert any(item.scenario_id == "SLOW" for item in report.blockers)
    fast_only = assess_constraint_readiness(
        config, cset, design, timing, validation=_validation(issue, status=ValidationStatus.BLOCKED.value),
        scenario_ids=("FAST",),
    )
    assert fast_only.status == ReadinessStatus.READY
    assert [item.scenario_id for item in fast_only.scenario_results] == ["FAST"]


def test_stale_advisory_and_external_after_receipt_are_blockers_but_stored_receipt_is_recognized():
    design, timing, config = _context(_SOURCE, clocks=[{
        "name": "clk", "period": "10ns", "period_seconds": 10e-9,
    }])
    cset = ConstraintSet(name="m")
    advisory = InferenceEngine().infer_candidates(
        design, timing, config, cset, AssumptionLedger(), run_ts="2000-01-01T00:00:00+00:00",
    )
    candidate = advisory.candidates[0]
    stale_candidate = replace(
        candidate,
        source_snapshot_identity={**candidate.source_snapshot_identity, "config": "different"},
    )
    stale_report = replace(advisory, candidates=[stale_candidate])
    stale = assess_constraint_readiness(config, cset, design, timing, inference_report=stale_report)

    assert stale.status == ReadinessStatus.BLOCKED
    assert stale.stale_evidence and "Advisory candidate is stale" in stale.stale_evidence[0].message

    # A valid stored Step-28 record has a pre-application UCM identity and
    # therefore must not be flagged merely because the UCM changed after apply.
    cset.create_clock("clk", 10e-9, source="clk")
    cset.metadata["constraint_applications"] = {
        "APP-1": {
            "applied_constraint_ids": [cset.clocks()[0].id],
            "source_snapshot_identity": {"constraint_set": "intentionally-old"},
        }
    }
    recognized = assess_constraint_readiness(config, cset, design, timing)
    assert "APP-1" in recognized.provenance_references
    assert not any("APP-1" in item.message for item in recognized.stale_evidence)

    external = assess_constraint_readiness(
        config, cset, design, timing,
        application_receipts=({
            "application_id": "APP-EXTERNAL",
            "applied_constraint_ids": [cset.clocks()[0].id],
            "ucm_after_snapshot_identity": "not-current",
        },),
    )
    assert any("UCM-after identity differs" in item.message for item in external.stale_evidence)

    cset.metadata["constraint_applications"]["APP-GONE"] = {
        "applied_constraint_ids": ["C-ABSENT"],
        "source_snapshot_identity": {"constraint_set": "also-intentionally-old"},
    }
    absent = assess_constraint_readiness(config, cset, design, timing)
    assert "APP-GONE" not in absent.provenance_references
    assert any(item.evidence[0].reference_id == "APP-GONE" for item in absent.stale_evidence)


def test_eda_and_formal_requirements_are_only_configured_when_applicable():
    design, timing, config = _context(_SOURCE, clocks=[{
        "name": "clk", "period": "10ns", "period_seconds": 10e-9,
    }])
    cset = ConstraintSet(name="m")
    generic = assess_constraint_readiness(config, cset, design, timing, validation=_validation())
    generic_preflight = next(item for item in generic.requirements if item.id == "EDA_PREFLIGHT_READY")
    assert generic_preflight.required is False and generic_preflight.status == ReadinessStatus.READY

    config.flow.backend = "yosys_opensta"
    configured = assess_constraint_readiness(config, cset, design, timing, validation=_validation())
    required_preflight = next(item for item in configured.requirements if item.id == "EDA_PREFLIGHT_READY")
    assert required_preflight.required is True and required_preflight.status == ReadinessStatus.UNKNOWN

    unavailable = EDAPreflight(
        backend="yosys_opensta",
        checks=(CapabilityCheck("yosys", CapabilityStatus.EXECUTABLE_MISSING, detail="not found"),),
        environment_fingerprint="test-env",
    )
    blocked = assess_constraint_readiness(
        config, cset, design, timing, validation=_validation(), eda_preflight=unavailable,
    )
    assert next(item for item in blocked.requirements if item.id == "EDA_PREFLIGHT_READY").status == ReadinessStatus.BLOCKED

    # The default formal backend creates no universal proof requirement. The
    # explicit SymbiYosys setting is checked only if an exception exists.
    no_exceptions = {item.id for item in generic.requirements}
    assert "EXCEPTIONS_VALIDATED" not in no_exceptions
    config.formal = FormalConfig(backend="symbiyosys")
    still_no_exceptions = assess_constraint_readiness(config, cset, design, timing, validation=_validation())
    assert "EXCEPTIONS_VALIDATED" not in {item.id for item in still_no_exceptions.requirements}


def test_exception_unresolved_remains_warning_by_default_but_blocks_configured_formal():
    design, timing, config = _context(_SOURCE, clocks=[{
        "name": "clk", "period": "10ns", "period_seconds": 10e-9,
    }])
    cset = ConstraintSet(name="m")
    exception = cset.create_false_path(from_set=["clk"], to_set=["q"])
    unverified = ValidationIssue(
        severity=Severity.INFO,
        category=ValidationCategory.EXCEPTION,
        code=ErrorCode.EXCEPTION_UNVERIFIED,
        message="Exception is structurally analyzed but no formal proof is available.",
        constraint_id=exception.id,
        resolution_status="UNRESOLVED",
    )
    conservative = assess_constraint_readiness(
        config, cset, design, timing,
        validation=_validation(unverified, status=ValidationStatus.PASS_WITH_WARNINGS.value),
    )
    requirement = next(item for item in conservative.requirements if item.id == "EXCEPTIONS_VALIDATED")
    assert requirement.status == ReadinessStatus.READY_WITH_WARNINGS
    assert conservative.status != ReadinessStatus.BLOCKED

    config.formal = FormalConfig(backend="symbiyosys")
    formal = assess_constraint_readiness(
        config, cset, design, timing,
        validation=_validation(unverified, status=ValidationStatus.PASS_WITH_WARNINGS.value),
    )
    formal_requirement = next(item for item in formal.requirements if item.id == "EXCEPTIONS_VALIDATED")
    assert formal_requirement.status == ReadinessStatus.BLOCKED
    assert formal.status == ReadinessStatus.BLOCKED
