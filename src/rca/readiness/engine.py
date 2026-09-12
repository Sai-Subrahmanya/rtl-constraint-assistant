"""Step-29 deterministic constraint readiness and closure orchestration.

This module deliberately *consumes* the canonical UCM plus the existing
validation, coverage, MCMM, provenance, inference/application, and Step-25
preflight outputs.  It contains no SDC/semantic/selector/conflict/coverage
algorithm and never mutates a supplied object, runs an EDA flow, accepts advice,
or creates timing intent.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..constraint_model import ConstraintSet, stable_hash_cset
from ..design_model import Design
from ..eda import EDAPreflight
from ..inference import InferenceReport
from ..mcmm import ScenarioMatrix, build_scenario_matrix
from ..timing_model import TimingGraph
from ..utils.enums import ErrorCode, Severity, SourceKind, ValidationCategory, ValidationStatus
from ..utils.hashing import stable_hash
from ..validation import ValidationResult
from ..validation import validate as run_validation
from .models import (
    ConstraintReadinessReport,
    EvidenceState,
    ReadinessBlocker,
    ReadinessEvidence,
    ReadinessFinding,
    ReadinessRequirement,
    ReadinessSeverity,
    ReadinessStatus,
    ScenarioReadinessResult,
)

_REAL_EDA_BACKENDS = {"yosys_opensta"}
_COVERAGE_KEYS = (
    "clock_source_coverage_pct",
    "input_timing_path_coverage_pct",
    "output_timing_path_coverage_pct",
    "reg_to_reg_coverage_pct",
    "cdc_path_coverage_pct",
    "clock_relationship_coverage_pct",
)
_UNRESOLVED = {"UNKNOWN", "UNRESOLVED", "REQUIRES_USER_INPUT"}


@dataclass
class _RequirementDraft:
    id: str
    category: str
    title: str
    status: ReadinessStatus
    required: bool
    rationale: str
    evidence: list[ReadinessEvidence] = field(default_factory=list)
    scenario_id: str | None = None
    finding_ids: list[str] = field(default_factory=list)


class _Collector:
    """Internal deterministic assembly helper; no authoritative state lives here."""

    def __init__(self) -> None:
        self.requirements: dict[str, _RequirementDraft] = {}
        self.findings: list[ReadinessFinding] = []

    def requirement(self, *, requirement_id: str, category: str, title: str,
                    status: ReadinessStatus, required: bool, rationale: str,
                    evidence: Iterable[ReadinessEvidence] = (),
                    scenario_id: str | None = None) -> _RequirementDraft:
        draft = _RequirementDraft(
            id=requirement_id,
            category=category,
            title=title,
            status=status,
            required=required,
            rationale=rationale,
            evidence=list(evidence),
            scenario_id=scenario_id,
        )
        self.requirements[requirement_id] = draft
        return draft

    def finding(self, *, requirement_id: str, category: str, severity: ReadinessSeverity,
                status: ReadinessStatus, message: str, affected_object: str | None = None,
                scenario_id: str | None = None, evidence: Iterable[ReadinessEvidence] = (),
                missing_information: Iterable[str] = (), suggested_action: str | None = None,
                provenance_references: Iterable[str] = ()) -> ReadinessFinding:
        fingerprint = {
            "requirement": requirement_id,
            "category": category,
            "severity": severity.value,
            "status": status.value,
            "message": message,
            "object": affected_object,
            "scenario": scenario_id,
            "evidence": [item.to_dict() for item in evidence],
            "missing": sorted(missing_information),
            "suggestion": suggested_action,
            "provenance": sorted(provenance_references),
        }
        finding = ReadinessFinding(
            id="RDF-" + stable_hash(fingerprint)[:20],
            requirement_id=requirement_id,
            category=category,
            severity=severity,
            status=status,
            message=message,
            affected_object=affected_object,
            scenario_id=scenario_id,
            evidence=tuple(evidence),
            missing_information=tuple(sorted(set(missing_information))),
            suggested_action=suggested_action,
            provenance_references=tuple(sorted(set(provenance_references))),
        )
        self.findings.append(finding)
        if requirement_id in self.requirements:
            self.requirements[requirement_id].finding_ids.append(finding.id)
        return finding

    def build_requirements(self) -> tuple[ReadinessRequirement, ...]:
        return tuple(ReadinessRequirement(
            id=draft.id,
            category=draft.category,
            title=draft.title,
            status=draft.status,
            required=draft.required,
            rationale=draft.rationale,
            evidence=tuple(draft.evidence),
            finding_ids=tuple(sorted(set(draft.finding_ids))),
            scenario_id=draft.scenario_id,
        ) for draft in sorted(self.requirements.values(), key=lambda item: (item.id, item.scenario_id or "")))


@dataclass(frozen=True)
class ConstraintReadinessEngine:
    """Stateless public façade for the single report-only readiness engine."""

    def assess(
        self,
        config: Any,
        cset: ConstraintSet | None,
        design: Design | None = None,
        timing_graph: TimingGraph | None = None,
        *,
        validation: ValidationResult | None = None,
        inference_report: InferenceReport | None = None,
        application_receipts: Iterable[Any] = (),
        eda_preflight: EDAPreflight | None = None,
        scenario_ids: Iterable[str] = (),
    ) -> ConstraintReadinessReport:
        """Assess supplied evidence; delegates to the sole implementation."""
        return assess_constraint_readiness(
            config, cset, design, timing_graph,
            validation=validation, inference_report=inference_report,
            application_receipts=application_receipts, eda_preflight=eda_preflight,
            scenario_ids=scenario_ids,
        )


def assess_constraint_readiness(
    config: Any,
    cset: ConstraintSet | None,
    design: Design | None = None,
    timing_graph: TimingGraph | None = None,
    *,
    validation: ValidationResult | None = None,
    inference_report: InferenceReport | None = None,
    application_receipts: Iterable[Any] = (),
    eda_preflight: EDAPreflight | None = None,
    scenario_ids: Iterable[str] = (),
) -> ConstraintReadinessReport:
    """Return a deterministic, report-only readiness assessment.

    When no ``validation`` is supplied, the existing validation pipeline runs
    against the supplied in-memory UCM.  It is invoked with no formal backend,
    so this convenience path performs structural/conservative validation only
    and never launches a proof, synthesis, STA, SDC emission, cache action, or
    SQLite/history write.  Callers with an existing authoritative validation
    result should pass it directly.
    """
    canonical_ucm_available = cset is not None
    active_ucm = cset if cset is not None else ConstraintSet(name=_project_name(config))
    matrix = build_scenario_matrix(config, active_ucm)
    source_identity = _source_identity(config, active_ucm, design, timing_graph, matrix)
    active_scenarios = set(matrix.active_ids) if matrix.is_enabled else None
    validation = validation or run_validation(
        design=design,
        tg=timing_graph,
        cset=active_ucm,
        # Keep the no-input convenience path deliberately generic and
        # conservative. Configured real backends are represented only by the
        # injected Step-25 preflight requirement below, never invoked here.
        backend="generic",
        active_scenarios=active_scenarios,
    )
    coverage = validation.coverage
    coverage_summary = coverage.as_dict() if coverage is not None else {}
    issues = tuple(sorted(validation.report.issues, key=_issue_key))
    collector = _Collector()

    stale_findings, application_refs, inference_refs = _collect_stale_and_references(
        active_ucm, design, timing_graph, config, source_identity, inference_report, application_receipts,
    )
    stale_evidence = tuple(stale_findings)

    # A. source/design. These reflect already-produced structural model inputs,
    # rather than parsing/elaborating a second time.
    design_evidence = _source_evidence(design, "design_model", source_identity["design"])
    design_status = ReadinessStatus.READY if design is not None else ReadinessStatus.BLOCKED
    collector.requirement(
        requirement_id="DESIGN_ELABORATED", category="SOURCE_DESIGN", title="Design elaborated",
        status=design_status, required=True,
        rationale=("An existing elaborated Design model is available."
                   if design is not None else "No elaborated Design model was supplied."),
        evidence=design_evidence,
    )
    if design is None:
        collector.finding(
            requirement_id="DESIGN_ELABORATED", category="SOURCE_DESIGN", severity=ReadinessSeverity.BLOCKER,
            status=design_status, message="Design elaboration evidence is unavailable.",
            evidence=design_evidence, missing_information=("elaborated design",),
            suggested_action="Run the existing analyze workflow and provide its elaborated design model.",
        )

    graph_evidence = _source_evidence(timing_graph, "timing_graph", source_identity["timing_graph"])
    graph_status = ReadinessStatus.READY if timing_graph is not None else ReadinessStatus.BLOCKED
    collector.requirement(
        requirement_id="TIMING_GRAPH_AVAILABLE", category="SOURCE_DESIGN", title="Timing graph available",
        status=graph_status, required=True,
        rationale=("An existing timing graph is available." if timing_graph is not None
                   else "No timing graph was supplied; timing intent and coverage cannot be assessed."),
        evidence=graph_evidence,
    )
    if timing_graph is None:
        collector.finding(
            requirement_id="TIMING_GRAPH_AVAILABLE", category="SOURCE_DESIGN", severity=ReadinessSeverity.BLOCKER,
            status=graph_status, message="Timing-graph evidence is unavailable.",
            evidence=graph_evidence, missing_information=("timing graph",),
            suggested_action="Run the existing timing analysis workflow; readiness does not build a replacement graph.",
        )

    ucm_status = ReadinessStatus.READY if canonical_ucm_available else ReadinessStatus.INCOMPLETE
    ucm_evidence = (ReadinessEvidence(
        state=EvidenceState.AUTHORITATIVE if canonical_ucm_available else EvidenceState.MISSING,
        source="canonical_ucm", detail=("Current ConstraintSet supplied." if canonical_ucm_available
                                        else "No canonical ConstraintSet supplied; an empty read-only projection was used."),
        snapshot_identity=source_identity["constraint_set"],
    ),)
    collector.requirement(
        requirement_id="CANONICAL_UCM_AVAILABLE", category="SOURCE_DESIGN", title="Canonical UCM available",
        status=ucm_status, required=True,
        rationale=("Readiness consumed the caller-provided canonical ConstraintSet."
                   if canonical_ucm_available else "Readiness cannot treat configuration or advisory data as UCM intent."),
        evidence=ucm_evidence,
    )
    if not canonical_ucm_available:
        collector.finding(
            requirement_id="CANONICAL_UCM_AVAILABLE", category="SOURCE_DESIGN",
            severity=ReadinessSeverity.WARNING, status=ucm_status,
            message="No canonical UCM was supplied; advisory/configuration data was not materialized.",
            evidence=ucm_evidence, missing_information=("canonical UCM snapshot",),
            suggested_action="Supply the current canonical ConstraintSet or a canonical UCM snapshot.",
        )

    # B. clocks. All missing data is read from validation/coverage evidence.
    _assess_primary_clock_intent(collector, coverage_summary, source_identity)
    _assess_validation_requirement(
        collector, "CLOCK_PERIODS_RESOLVED", "CLOCK_INTENT", "Clock periods resolved", issues,
        codes={ErrorCode.CLOCK_PERIOD_MISSING.value, ErrorCode.CLOCK_PERIOD_INVALID.value},
        incomplete_status=ReadinessStatus.BLOCKED,
        ready_rationale="The existing validator reports no missing or invalid primary-clock period.",
        problem_rationale="The existing validator reported a missing or invalid primary-clock period.",
        action="Provide an explicit primary-clock period; readiness never guesses one.",
    )
    relationship_needed = bool(getattr(timing_graph, "domain_edges", []) if timing_graph is not None else [])
    if relationship_needed:
        _assess_validation_requirement(
            collector, "CLOCK_RELATIONSHIPS_RESOLVED", "CLOCK_INTENT", "Clock relationships resolved", issues,
            codes={ErrorCode.COMPLETENESS_CLOCK_RELATIONSHIP.value},
            incomplete_status=ReadinessStatus.INCOMPLETE,
            ready_rationale="The existing completeness validator reports no unresolved clock relationship.",
            problem_rationale="Clock relationship evidence remains unresolved.",
            action="Use the existing clock-relationship workflow to declare or confirm the relationship.",
        )
    generated_needed = bool(active_ucm.generated_clocks()) or bool(
        getattr(timing_graph, "generated_clock_candidates", []) if timing_graph is not None else [],
    )
    if generated_needed:
        _assess_generated_clock_intent(collector, issues, timing_graph, source_identity)

    # C. I/O. Existing completeness and coverage remain the authority.
    _assess_io_timing(collector, issues, coverage_summary, source_identity)

    # D. exception safety/formal evidence.
    _assess_exceptions(collector, active_ucm, issues, config, source_identity)

    # E. MCMM and scenario identity.
    selected_scenarios = _selected_scenarios(collector, matrix, scenario_ids, source_identity)
    _assess_scenarios(collector, matrix, issues, source_identity)

    # F/G. validation and coverage summaries are consumed, not recreated.
    _assess_validation_clean(collector, validation, issues, config, source_identity)
    _assess_coverage(collector, coverage_summary, source_identity)

    # H. provenance, assumption and stale-evidence aggregation.
    _assess_provenance(collector, active_ucm, application_refs, source_identity)
    _assess_stale_evidence(collector, stale_evidence, source_identity)
    _add_inference_context(collector, inference_report, inference_refs)

    # I. EDA preflight is configuration-aware and injected/produced by a caller.
    _assess_eda_preflight(collector, config, eda_preflight, source_identity)

    requirements = collector.build_requirements()
    findings = tuple(sorted(collector.findings, key=_finding_key))
    blockers = tuple(ReadinessBlocker.from_finding(item) for item in findings
                     if item.severity == ReadinessSeverity.BLOCKER)
    scenario_results = _scenario_results(matrix, selected_scenarios, requirements, findings)
    overall = _overall_status(requirements, selected_scenarios, findings)
    references = _provenance_references(active_ucm, application_refs, inference_refs)
    evidence_summary = _evidence_summary(active_ucm, requirements, application_refs, inference_refs)
    next_actions = tuple(sorted({item.suggested_action for item in findings if item.suggested_action}))
    return ConstraintReadinessReport(
        status=overall,
        requirements=requirements,
        blockers=blockers,
        findings=findings,
        scenario_results=scenario_results,
        validation_summary=validation.as_dict(),
        coverage_summary=coverage_summary,
        evidence_summary=evidence_summary,
        stale_evidence=stale_evidence,
        next_actions=next_actions,
        provenance_references=references,
        source_snapshot_identity=source_identity,
    )


def _assess_primary_clock_intent(collector: _Collector, coverage: dict[str, Any],
                                 source_identity: dict[str, str]) -> None:
    value = coverage.get("clock_source_coverage_pct")
    evidence = _coverage_evidence("clock_source_coverage_pct", value, source_identity)
    if value is None or value == "UNKNOWN":
        status = ReadinessStatus.UNKNOWN
        rationale = "Authoritative coverage is unavailable or UNKNOWN for clock sources."
    elif isinstance(value, (int, float)) and value < 100.0:
        status = ReadinessStatus.BLOCKED
        rationale = "Authoritative coverage reports an uncovered clock source."
    else:
        status = ReadinessStatus.READY
        rationale = "Authoritative coverage reports all applicable clock sources covered."
    collector.requirement(
        requirement_id="PRIMARY_CLOCK_INTENT_RESOLVED", category="CLOCK_INTENT",
        title="Primary clock intent resolved", status=status,
        required=value != "NOT_APPLICABLE", rationale=rationale, evidence=evidence,
    )
    if status != ReadinessStatus.READY:
        collector.finding(
            requirement_id="PRIMARY_CLOCK_INTENT_RESOLVED", category="CLOCK_INTENT",
            severity=_severity(status), status=status,
            message=("Primary-clock coverage is UNKNOWN; it was not interpreted as zero or complete."
                     if status == ReadinessStatus.UNKNOWN else "One or more structural clock sources lack canonical UCM intent."),
            evidence=evidence,
            missing_information=("primary clock intent",) if status == ReadinessStatus.BLOCKED else (),
            suggested_action=("Provide or explicitly apply reviewed primary-clock intent."
                              if status == ReadinessStatus.BLOCKED else
                              "Provide an elaborated timing graph so existing coverage can be computed."),
        )


def _assess_validation_requirement(collector: _Collector, requirement_id: str, category: str,
                                   title: str, issues: tuple[Any, ...], *, codes: set[str],
                                   incomplete_status: ReadinessStatus, ready_rationale: str,
                                   problem_rationale: str, action: str) -> None:
    matching = [item for item in issues if _issue_code(item) in codes]
    status = incomplete_status if matching else ReadinessStatus.READY
    evidence = tuple(_issue_evidence(item) for item in matching)
    collector.requirement(
        requirement_id=requirement_id, category=category, title=title, status=status, required=True,
        rationale=problem_rationale if matching else ready_rationale, evidence=evidence,
    )
    for issue in matching:
        _add_issue_finding(collector, requirement_id, category, status, issue, action)


def _assess_generated_clock_intent(collector: _Collector, issues: tuple[Any, ...],
                                  timing_graph: TimingGraph | None, source_identity: dict[str, str]) -> None:
    codes = {
        ErrorCode.COMPLETENESS_GENERATED_CLOCK.value,
        ErrorCode.GCLK_SOURCE_MISSING.value,
        ErrorCode.GCLK_MASTER_MISSING.value,
        ErrorCode.GCLK_TARGET_MISSING.value,
        ErrorCode.GCLK_INVALID_DIV.value,
        ErrorCode.GCLK_INVALID_MUL.value,
        ErrorCode.GCLK_EDGES_INVALID.value,
    }
    matching = [item for item in issues if _issue_code(item) in codes]
    candidates = list(getattr(timing_graph, "generated_clock_candidates", []) or [])
    status = ReadinessStatus.INCOMPLETE if matching or candidates else ReadinessStatus.READY
    evidence = [_issue_evidence(item) for item in matching]
    for candidate in sorted(candidates, key=lambda item: stable_hash(item)):
        evidence.append(ReadinessEvidence(
            state=EvidenceState.STRUCTURAL, source="timing_graph.generated_clock_candidate",
            reference_id=str(candidate.get("output", "")), detail="Existing structural candidate requires explicit intent.",
            snapshot_identity=source_identity["timing_graph"],
        ))
    collector.requirement(
        requirement_id="GENERATED_CLOCK_INTENT_RESOLVED", category="CLOCK_INTENT",
        title="Generated-clock intent resolved", status=status, required=True,
        rationale=("Existing validation/structural analysis still needs generated-clock information."
                   if status != ReadinessStatus.READY else "No generated-clock completeness issue is present."),
        evidence=evidence,
    )
    for issue in matching:
        _add_issue_finding(
            collector, "GENERATED_CLOCK_INTENT_RESOLVED", "CLOCK_INTENT", status, issue,
            "Provide explicit generated-clock source and transformation information; do not infer a ratio or phase.",
        )
    for candidate in candidates:
        collector.finding(
            requirement_id="GENERATED_CLOCK_INTENT_RESOLVED", category="CLOCK_INTENT",
            severity=ReadinessSeverity.WARNING, status=ReadinessStatus.INCOMPLETE,
            message="Structural generated-clock observation remains advisory and has not become canonical UCM intent.",
            affected_object=str(candidate.get("output", "")) or None,
            evidence=(ReadinessEvidence(
                state=EvidenceState.STRUCTURAL, source="timing_graph.generated_clock_candidate",
                reference_id=str(candidate.get("output", "")), detail=str(candidate.get("detail", "")),
                snapshot_identity=source_identity["timing_graph"],
            ),),
            missing_information=("generated-clock source/ratio/phase",),
            suggested_action="Confirm and explicitly provide generated-clock intent through the established workflow.",
        )


def _assess_io_timing(collector: _Collector, issues: tuple[Any, ...], coverage: dict[str, Any],
                      source_identity: dict[str, str]) -> None:
    relevant = [item for item in issues if _issue_code(item) in {
        ErrorCode.COMPLETENESS_IO_TIMING.value, ErrorCode.COMPLETENESS_ENVIRONMENT.value,
        ErrorCode.IO_CLOCK_UNKNOWN.value, ErrorCode.IO_DELAY_INVALID.value,
    }]
    io_values = {key: coverage.get(key) for key in (
        "input_timing_path_coverage_pct", "output_timing_path_coverage_pct",
    )}
    unknown = any(value == "UNKNOWN" for value in io_values.values())
    gaps = any(isinstance(value, (int, float)) and value < 100.0 for value in io_values.values())
    if unknown:
        status = ReadinessStatus.UNKNOWN
    elif relevant or gaps:
        status = ReadinessStatus.INCOMPLETE
    else:
        status = ReadinessStatus.READY
    evidence = [_issue_evidence(item) for item in relevant]
    for key, value in sorted(io_values.items()):
        evidence.extend(_coverage_evidence(key, value, source_identity))
    collector.requirement(
        requirement_id="IO_TIMING_RESOLVED", category="IO_TIMING", title="I/O timing resolved",
        status=status, required=any(value != "NOT_APPLICABLE" for value in io_values.values()),
        rationale=("Existing completeness/coverage evidence contains unresolved I/O timing."
                   if status != ReadinessStatus.READY else "All applicable I/O timing coverage is complete."),
        evidence=evidence,
    )
    for issue in relevant:
        _add_issue_finding(
            collector, "IO_TIMING_RESOLVED", "IO_TIMING", status, issue,
            "Provide an explicit I/O timing budget and clock association; readiness never invents one.",
        )
    if gaps and not relevant:
        collector.finding(
            requirement_id="IO_TIMING_RESOLVED", category="IO_TIMING", severity=ReadinessSeverity.WARNING,
            status=status, message="Authoritative I/O coverage reports an uncovered applicable path.",
            evidence=tuple(evidence), missing_information=("I/O timing budget",),
            suggested_action="Provide explicit I/O timing intent for uncovered ports.",
        )
    if unknown:
        collector.finding(
            requirement_id="IO_TIMING_RESOLVED", category="IO_TIMING", severity=ReadinessSeverity.WARNING,
            status=status, message="I/O coverage is UNKNOWN and was not treated as complete.",
            evidence=tuple(evidence), suggested_action="Provide design/timing-graph evidence so existing coverage can be computed.",
        )


def _assess_exceptions(collector: _Collector, cset: ConstraintSet, issues: tuple[Any, ...],
                       config: Any, source_identity: dict[str, str]) -> None:
    exceptions = sorted(cset.exceptions(), key=lambda item: item.id)
    if not exceptions:
        return
    matching = [item for item in issues if _issue_category(item) == ValidationCategory.EXCEPTION.value]
    formal_required = _formal_required(config)
    invalid = any(_issue_code(item) in {
        ErrorCode.EXCEPTION_FORMAL_INVALID.value, ErrorCode.EXCEPTION_VERIFICATION_ERROR.value,
        ErrorCode.EXCEPTION_BAD_CYCLES.value, ErrorCode.EXCEPTION_SETUP_HOLD_INCOHERENT.value,
    } or bool(getattr(item, "blocking", False)) for item in matching)
    unverified = [item for item in matching if _issue_code(item) == ErrorCode.EXCEPTION_UNVERIFIED.value]
    unresolved = [item for item in matching if _resolution(item) in _UNRESOLVED]
    if invalid or unverified and formal_required:
        status = ReadinessStatus.BLOCKED
    elif unresolved:
        status = ReadinessStatus.READY_WITH_WARNINGS if not formal_required else ReadinessStatus.UNKNOWN
    elif matching:
        status = ReadinessStatus.READY_WITH_WARNINGS
    else:
        status = ReadinessStatus.READY
    evidence = tuple(_issue_evidence(item) for item in matching)
    collector.requirement(
        requirement_id="EXCEPTIONS_VALIDATED", category="EXCEPTIONS", title="Timing exceptions validated",
        status=status, required=True,
        rationale=("Configured formal verification is required for exception safety."
                   if formal_required else "Exception verification follows existing configured/optional policy."),
        evidence=evidence,
    )
    for issue in matching:
        issue_status = status if _issue_code(issue) == ErrorCode.EXCEPTION_UNVERIFIED.value else _status_for_issue(issue)
        action = ("Configure/complete the explicit formal proof mapped to this exception."
                  if _issue_code(issue) == ErrorCode.EXCEPTION_UNVERIFIED.value and formal_required else
                  "Review the existing exception validation/formal evidence; readiness does not fabricate proof.")
        _add_issue_finding(collector, "EXCEPTIONS_VALIDATED", "EXCEPTIONS", issue_status, issue, action)
    if not matching and exceptions:
        collector.finding(
            requirement_id="EXCEPTIONS_VALIDATED", category="EXCEPTIONS", severity=ReadinessSeverity.INFORMATION,
            status=ReadinessStatus.READY, message="Existing exception validation reports no exception-specific finding.",
            evidence=(ReadinessEvidence(EvidenceState.VALIDATED, "validation", detail="Exception validation completed.",
                                        snapshot_identity=source_identity["constraint_set"]),),
            provenance_references=[item.id for item in exceptions],
        )


def _selected_scenarios(collector: _Collector, matrix: ScenarioMatrix, requested: Iterable[str],
                        source_identity: dict[str, str]) -> tuple[str, ...]:
    requested_ids = tuple(sorted(set(requested)))
    if not matrix.is_enabled:
        return ()
    active = tuple(matrix.active_ids)
    if not requested_ids:
        return active
    invalid = tuple(sorted(set(requested_ids) - set(active)))
    if invalid:
        evidence = (ReadinessEvidence(
            EvidenceState.AUTHORITATIVE, "mcmm.scenario_matrix", detail="Requested scope does not match active matrix.",
            snapshot_identity=source_identity["scenario_matrix"],
        ),)
        collector.requirement(
            requirement_id="SCENARIO_SELECTION_VALID", category="SCENARIOS", title="Requested scenario scope valid",
            status=ReadinessStatus.BLOCKED, required=True,
            rationale="A requested readiness scenario is unknown or inactive in the current matrix.", evidence=evidence,
        )
        collector.finding(
            requirement_id="SCENARIO_SELECTION_VALID", category="SCENARIOS", severity=ReadinessSeverity.BLOCKER,
            status=ReadinessStatus.BLOCKED,
            message="Requested readiness scenario IDs are not active: " + ", ".join(invalid), evidence=evidence,
            missing_information=invalid, suggested_action="Select active scenario IDs from the configured MCMM matrix.",
        )
    return tuple(sid for sid in requested_ids if sid in active)


def _assess_scenarios(collector: _Collector, matrix: ScenarioMatrix, issues: tuple[Any, ...],
                      source_identity: dict[str, str]) -> None:
    if not matrix.is_enabled:
        return
    scenario_issues = [item for item in issues if _issue_category(item) == ValidationCategory.SCENARIO.value]
    if not matrix.active_ids or any(_issue_code(item) == ErrorCode.SCENARIO_UNKNOWN_ID.value for item in scenario_issues):
        status = ReadinessStatus.BLOCKED
    elif scenario_issues:
        status = ReadinessStatus.INCOMPLETE
    else:
        status = ReadinessStatus.READY
    evidence = [ReadinessEvidence(
        EvidenceState.AUTHORITATIVE, "mcmm.scenario_matrix", detail="Active scenario matrix evaluated.",
        snapshot_identity=source_identity["scenario_matrix"],
    ), *(_issue_evidence(item) for item in scenario_issues)]
    collector.requirement(
        requirement_id="SCENARIOS_VALID", category="SCENARIOS", title="MCMM scenarios valid",
        status=status, required=True,
        rationale=("Configured MCMM requires active, valid scenario definitions and constraint scope."
                   if status != ReadinessStatus.READY else "Configured MCMM scenario matrix is valid."),
        evidence=evidence,
    )
    if not matrix.active_ids:
        collector.finding(
            requirement_id="SCENARIOS_VALID", category="SCENARIOS", severity=ReadinessSeverity.BLOCKER,
            status=status, message="MCMM is enabled but has no active scenarios.", evidence=evidence,
            missing_information=("active scenario definition",),
            suggested_action="Configure at least one active MCMM scenario.",
        )
    # A finding for an unknown/inactive scope has no active per-scenario home;
    # retain it on the aggregate MCMM requirement rather than inventing one.
    for issue in scenario_issues:
        if getattr(issue, "scenario_id", None) not in matrix.active_ids:
            _add_issue_finding(
                collector, "SCENARIOS_VALID", "SCENARIOS",
                ReadinessStatus.BLOCKED if _issue_code(issue) == ErrorCode.SCENARIO_UNKNOWN_ID.value else status,
                issue, "Resolve the scenario definition/scope without broadening a scenario-specific constraint.",
            )
    for sid in matrix.active_ids:
        specific = [item for item in scenario_issues if getattr(item, "scenario_id", None) == sid]
        scenario_status = (ReadinessStatus.BLOCKED if any(
            _issue_code(item) == ErrorCode.SCENARIO_UNKNOWN_ID.value
            or bool(getattr(item, "blocking", False)) for item in specific
        ) else ReadinessStatus.INCOMPLETE if specific else ReadinessStatus.READY)
        collector.requirement(
            requirement_id=f"SCENARIO_{sid}_INTENT_VALID", category="SCENARIOS",
            title=f"Scenario {sid} intent valid", status=scenario_status, required=True,
            rationale=(f"Scenario {sid} has no scenario-scoped validation finding."
                       if not specific else f"Scenario {sid} retains scenario-scoped validation findings."),
            evidence=tuple(_issue_evidence(item) for item in specific) or (ReadinessEvidence(
                EvidenceState.AUTHORITATIVE, "mcmm.scenario_matrix", reference_id=sid,
                detail="Active scenario definition.", scenario_id=sid,
                snapshot_identity=source_identity["scenario_matrix"],
            ),),
            scenario_id=sid,
        )
        for issue in specific:
            _add_issue_finding(
                collector, f"SCENARIO_{sid}_INTENT_VALID", "SCENARIOS", scenario_status, issue,
                "Resolve this scenario's definition/scope without broadening a scenario-specific constraint.",
            )


def _assess_validation_clean(collector: _Collector, validation: ValidationResult,
                             issues: tuple[Any, ...], config: Any,
                             source_identity: dict[str, str]) -> None:
    status_value = _enum_value(validation.status)
    formal_optional = not _formal_required(config)
    unresolved = [item for item in issues if _resolution(item) in _UNRESOLVED
                  and not (formal_optional and _issue_code(item) == ErrorCode.EXCEPTION_UNVERIFIED.value)]
    if status_value in {ValidationStatus.BLOCKED.value, ValidationStatus.ERROR.value}:
        status = ReadinessStatus.BLOCKED
    elif unresolved:
        status = ReadinessStatus.UNKNOWN
    elif status_value == ValidationStatus.PASS_WITH_WARNINGS.value or validation.warnings:
        status = ReadinessStatus.READY_WITH_WARNINGS
    else:
        status = ReadinessStatus.READY
    evidence = (ReadinessEvidence(
        EvidenceState.VALIDATED, "validation", reference_id=status_value,
        detail=f"Existing validation status {status_value}.", snapshot_identity=source_identity["constraint_set"],
    ),)
    collector.requirement(
        requirement_id="VALIDATION_CLEAN", category="VALIDATION", title="Validation clean",
        status=status, required=True,
        rationale="Readiness uses the existing validation report without reproducing validation logic.",
        evidence=evidence,
    )
    if status != ReadinessStatus.READY:
        driving = validation.blocking if status == ReadinessStatus.BLOCKED else unresolved
        scopes = {getattr(item, "scenario_id", None) for item in driving}
        # Scope this summary only when all findings that drive it belong to one
        # active scenario. Otherwise it remains an honest global condition.
        summary_scope = next(iter(scopes)) if len(scopes) == 1 and None not in scopes else None
        collector.finding(
            requirement_id="VALIDATION_CLEAN", category="VALIDATION", severity=_severity(status), status=status,
            message=(f"Existing validation status is {status_value}." if status != ReadinessStatus.UNKNOWN
                     else "Existing validation contains unresolved findings; readiness preserves UNKNOWN."),
            scenario_id=summary_scope, evidence=evidence,
            suggested_action="Resolve the existing validator findings; readiness does not repair constraints.",
        )


def _assess_coverage(collector: _Collector, coverage: dict[str, Any],
                     source_identity: dict[str, str]) -> None:
    values = {key: coverage.get(key) for key in _COVERAGE_KEYS}
    if not coverage or any(value is None or value == "UNKNOWN" for value in values.values()):
        status = ReadinessStatus.UNKNOWN
    elif any(isinstance(value, (int, float)) and value < 100.0 for value in values.values()):
        status = ReadinessStatus.INCOMPLETE
    else:
        status = ReadinessStatus.READY
    evidence = tuple(item for key, value in sorted(values.items())
                     for item in _coverage_evidence(key, value, source_identity))
    collector.requirement(
        requirement_id="COVERAGE_KNOWN", category="COVERAGE", title="Coverage known",
        status=status, required=True,
        rationale=("Existing coverage is UNKNOWN and was retained as UNKNOWN." if status == ReadinessStatus.UNKNOWN
                   else "Existing coverage reports uncovered applicable objects." if status == ReadinessStatus.INCOMPLETE
                   else "Existing authoritative coverage is known for all applicable categories."),
        evidence=evidence,
    )
    if status != ReadinessStatus.READY:
        collector.finding(
            requirement_id="COVERAGE_KNOWN", category="COVERAGE", severity=ReadinessSeverity.WARNING,
            status=status,
            message=("Coverage is UNKNOWN; readiness did not reinterpret it as zero or complete."
                     if status == ReadinessStatus.UNKNOWN else "Coverage has one or more uncovered applicable categories/objects."),
            evidence=evidence,
            suggested_action=("Provide an elaborated design/timing graph so the existing coverage engine can report coverage."
                              if status == ReadinessStatus.UNKNOWN else
                              "Resolve the uncovered objects reported by the existing coverage engine."),
        )


def _assess_provenance(collector: _Collector, cset: ConstraintSet,
                       application_refs: tuple[str, ...], source_identity: dict[str, str]) -> None:
    missing: list[str] = []
    evidence: list[ReadinessEvidence] = []
    for constraint in sorted(cset, key=lambda item: item.id):
        source_kind = constraint.source_kind
        provenance = constraint.provenance
        evidence_count = len(provenance.evidence) if provenance is not None else 0
        state = _source_kind_evidence_state(source_kind)
        evidence.append(ReadinessEvidence(
            state=state, source="ucm.constraint", reference_id=constraint.id,
            detail=f"source_kind={source_kind.value}; provenance_evidence={evidence_count}",
            snapshot_identity=source_identity["constraint_set"],
        ))
        if source_kind in {SourceKind.INFERENCE, SourceKind.DERIVED} and evidence_count == 0:
            missing.append(constraint.id)
    for assumption in cset.ledger:
        if str(assumption.severity).upper() == "REQUIRED" and not assumption.user_confirmed:
            missing.append(assumption.id)
            evidence.append(ReadinessEvidence(
                EvidenceState.MISSING, "assumption_ledger", reference_id=assumption.id,
                detail="Required assumption is not user-confirmed.", snapshot_identity=source_identity["constraint_set"],
            ))
    evidence.extend(ReadinessEvidence(
        EvidenceState.VALIDATED, "constraint_application", reference_id=app_id,
        detail="Application receipt is recognized only because its applied UCM constraints are present.",
        snapshot_identity=source_identity["constraint_set"],
    ) for app_id in application_refs)
    status = ReadinessStatus.INCOMPLETE if missing else ReadinessStatus.READY
    collector.requirement(
        requirement_id="REQUIRED_EVIDENCE_PRESENT", category="EVIDENCE_PROVENANCE",
        title="Required provenance/evidence present", status=status, required=True,
        rationale=("Some inference/derived constraints or required assumptions lack retained evidence."
                   if missing else "Canonical UCM provenance and required assumption evidence are present."),
        evidence=evidence,
    )
    if missing:
        collector.finding(
            requirement_id="REQUIRED_EVIDENCE_PRESENT", category="EVIDENCE_PROVENANCE",
            severity=ReadinessSeverity.WARNING, status=status,
            message="Canonical UCM contains constraints/assumptions without required retained evidence.",
            affected_object=", ".join(sorted(missing)), evidence=evidence,
            missing_information=("provenance/evidence",),
            suggested_action="Supply evidence through the established UCM/application workflow; do not rewrite existing intent.",
            provenance_references=missing,
        )


def _assess_stale_evidence(collector: _Collector, stale: tuple[ReadinessFinding, ...],
                           source_identity: dict[str, str]) -> None:
    evidence = (ReadinessEvidence(
        EvidenceState.AUTHORITATIVE if not stale else EvidenceState.MISSING,
        "readiness.snapshot_comparison",
        detail=("No supplied evidence snapshot is stale." if not stale else "One or more supplied evidence snapshots differ."),
        snapshot_identity=source_identity["constraint_set"], stale=bool(stale),
    ),)
    status = ReadinessStatus.BLOCKED if stale else ReadinessStatus.READY
    collector.requirement(
        requirement_id="STALE_EVIDENCE_CLEAR", category="EVIDENCE_PROVENANCE", title="Evidence snapshots current",
        status=status, required=True,
        rationale=("Existing source/application/advisory evidence matches the supplied current snapshots."
                   if not stale else "Stale evidence cannot be silently refreshed or treated as current."),
        evidence=evidence,
    )
    for finding in stale:
        collector.findings.append(finding)
        collector.requirements["STALE_EVIDENCE_CLEAR"].finding_ids.append(finding.id)


def _add_inference_context(collector: _Collector, report: InferenceReport | None,
                           references: tuple[str, ...]) -> None:
    if report is None:
        return
    candidates = tuple(sorted(report.candidates, key=lambda item: item.id))
    if not candidates:
        return
    incomplete = [item for item in candidates if item.constraint_template is None or item.missing_information]
    collector.requirement(
        requirement_id="ADVISORY_INFERENCE_REVIEW", category="INFERENCE_APPLICATION",
        title="Advisory inference review", status=ReadinessStatus.INCOMPLETE if incomplete else ReadinessStatus.READY,
        required=False,
        rationale="Advisory candidates are contextual only and cannot alter readiness/UCM intent until explicitly applied.",
        evidence=tuple(ReadinessEvidence(
            EvidenceState.INFERRED, "inference.candidate", reference_id=item.id,
            detail=f"{item.status.value}/{item.decision.value}",
        ) for item in candidates),
    )
    for candidate in incomplete:
        collector.finding(
            requirement_id="ADVISORY_INFERENCE_REVIEW", category="INFERENCE_APPLICATION",
            severity=ReadinessSeverity.INFORMATION, status=ReadinessStatus.INCOMPLETE,
            message="Advisory inference identifies unresolved information but has not changed canonical UCM readiness.",
            affected_object=(candidate.source_objects[0] if candidate.source_objects else None),
            evidence=(ReadinessEvidence(EvidenceState.INFERRED, "inference.candidate", candidate.id,
                                        f"{candidate.status.value}/{candidate.decision.value}"),),
            missing_information=[str(item.get("message", "")) for item in candidate.missing_information],
            suggested_action="Review the candidate; explicitly apply only an eligible, validated candidate.",
            provenance_references=(candidate.id,),
        )


def _assess_eda_preflight(collector: _Collector, config: Any, preflight: EDAPreflight | None,
                          source_identity: dict[str, str]) -> None:
    backend = _flow_backend(config)
    if backend not in _REAL_EDA_BACKENDS:
        collector.requirement(
            requirement_id="EDA_PREFLIGHT_READY", category="EDA_READINESS", title="EDA preflight ready",
            status=ReadinessStatus.READY, required=False,
            rationale=(f"Configured backend '{backend}' does not request a real EDA execution boundary."),
            evidence=(ReadinessEvidence(EvidenceState.OBSERVED, "eda.preflight", reference_id=backend,
                                        detail="Real EDA preflight is not required for this configured path."),),
        )
        return
    if preflight is None:
        collector.requirement(
            requirement_id="EDA_PREFLIGHT_READY", category="EDA_READINESS", title="EDA preflight ready",
            status=ReadinessStatus.UNKNOWN, required=True,
            rationale="Configured real EDA execution requires existing Step-25 preflight evidence.",
            evidence=(ReadinessEvidence(EvidenceState.MISSING, "eda.preflight", reference_id=backend,
                                        detail="No Step-25 preflight evidence was supplied."),),
        )
        collector.finding(
            requirement_id="EDA_PREFLIGHT_READY", category="EDA_READINESS", severity=ReadinessSeverity.WARNING,
            status=ReadinessStatus.UNKNOWN,
            message="Real EDA is configured but Step-25 preflight evidence is unavailable; no EDA run was started.",
            suggested_action="Run the existing doctor/preflight workflow before requesting real EDA execution.",
        )
        return
    checks = tuple(sorted(preflight.checks, key=lambda item: item.component))
    backend_matches = preflight.backend == backend
    failed = [item for item in checks if item.required and not item.ready]
    unsupported = any(_enum_value(item.status) == "unsupported" for item in failed)
    status = (ReadinessStatus.READY if preflight.ready and backend_matches else
              ReadinessStatus.UNSUPPORTED if unsupported else ReadinessStatus.BLOCKED)
    evidence = tuple(ReadinessEvidence(
        EvidenceState.OBSERVED, "eda.preflight", reference_id=item.component,
        detail=f"{getattr(item.status, 'value', item.status)}: {item.detail}",
        snapshot_identity=preflight.environment_fingerprint,
    ) for item in checks)
    collector.requirement(
        requirement_id="EDA_PREFLIGHT_READY", category="EDA_READINESS", title="EDA preflight ready",
        status=status, required=True,
        rationale="Step-25 preflight evidence was consumed; readiness did not execute synthesis or STA.", evidence=evidence,
    )
    if not backend_matches:
        collector.finding(
            requirement_id="EDA_PREFLIGHT_READY", category="EDA_READINESS", severity=ReadinessSeverity.BLOCKER,
            status=ReadinessStatus.BLOCKED,
            message=(f"Supplied preflight backend '{preflight.backend}' does not match configured backend '{backend}'."),
            evidence=evidence,
            suggested_action="Supply current Step-25 preflight evidence for the configured real backend.",
        )
    for check in failed:
        collector.finding(
            requirement_id="EDA_PREFLIGHT_READY", category="EDA_READINESS", severity=ReadinessSeverity.BLOCKER,
            status=status, message=f"EDA preflight check '{check.component}' is not ready: {check.detail}",
            affected_object=check.component,
            evidence=(ReadinessEvidence(
                EvidenceState.OBSERVED, "eda.preflight", check.component,
                f"{getattr(check.status, 'value', check.status)}: {check.detail}",
                snapshot_identity=preflight.environment_fingerprint,
            ),),
            missing_information=(check.component,),
            suggested_action="Provide the required executable/collateral or select an explicitly supported execution path.",
        )


def _collect_stale_and_references(cset: ConstraintSet, design: Design | None,
                                  tg: TimingGraph | None, config: Any,
                                  current: dict[str, str], report: InferenceReport | None,
                                  receipts: Iterable[Any]) -> tuple[list[ReadinessFinding], tuple[str, ...], tuple[str, ...]]:
    stale: list[ReadinessFinding] = []
    application_refs: list[str] = []
    inference_refs: list[str] = []

    def add_stale(source: str, reference_id: str, message: str, *, scenario_id: str | None = None) -> None:
        evidence = (ReadinessEvidence(
            EvidenceState.MISSING, source, reference_id=reference_id, detail=message,
            scenario_id=scenario_id, stale=True,
        ),)
        identifier = "RDF-" + stable_hash(("STALE_EVIDENCE_CLEAR", source, reference_id, message, scenario_id))[:20]
        stale.append(ReadinessFinding(
            id=identifier, requirement_id="STALE_EVIDENCE_CLEAR", category="EVIDENCE_PROVENANCE",
            severity=ReadinessSeverity.BLOCKER, status=ReadinessStatus.BLOCKED,
            message=message, scenario_id=scenario_id, evidence=evidence,
            suggested_action="Re-run the existing analysis/inference/application review; readiness never refreshes evidence implicitly.",
            provenance_references=(reference_id,) if reference_id else (),
        ))

    stored = cset.metadata.get("constraint_applications", {}) if isinstance(cset.metadata, dict) else {}
    if isinstance(stored, dict):
        for app_id, raw in sorted(stored.items()):
            if not isinstance(raw, dict):
                add_stale("constraint_application", str(app_id), "Stored application metadata is malformed.")
                continue
            applied = tuple(str(item) for item in raw.get("applied_constraint_ids", []) if item)
            if applied and all(cset.get(item) is not None for item in applied):
                application_refs.append(str(app_id))
            else:
                add_stale("constraint_application", str(app_id),
                          "Application receipt is not recognized because its applied UCM constraint is absent.")
            source = raw.get("source_snapshot_identity", {})
            if isinstance(source, Mapping):
                for key in ("design", "timing_graph", "config"):
                    if source.get(key) and current.get(key) and source[key] != current[key]:
                        add_stale("constraint_application", str(app_id),
                                  f"Application evidence is stale: {key} snapshot differs from current input.")

    for raw in receipts:
        payload = raw.to_dict() if hasattr(raw, "to_dict") else raw
        if not isinstance(payload, Mapping):
            continue
        ref = str(payload.get("application_id", "external_application_receipt"))
        after = payload.get("ucm_after_snapshot_identity")
        applied = tuple(str(item) for item in payload.get("applied_constraint_ids", []) if item)
        if after and after != current["constraint_set"]:
            add_stale("constraint_application_receipt", ref,
                      "External application receipt UCM-after identity differs from current canonical UCM.")
        source = payload.get("source_snapshot_identity", {})
        if isinstance(source, Mapping):
            for key in ("design", "timing_graph", "config"):
                if source.get(key) and current.get(key) and source[key] != current[key]:
                    add_stale("constraint_application_receipt", ref,
                              f"External application receipt is stale: {key} snapshot differs from current input.")
        if applied and not all(cset.get(item) is not None for item in applied):
            add_stale("constraint_application_receipt", ref,
                      "External application receipt names constraints absent from canonical UCM; it is not trusted as applied.")

    if report is not None:
        for candidate in sorted(report.candidates, key=lambda item: item.id):
            inference_refs.append(candidate.id)
            source = candidate.source_snapshot_identity
            for key in ("design", "timing_graph", "constraint_set", "config"):
                if source.get(key) and current.get(key) and source[key] != current[key]:
                    add_stale("inference.candidate", candidate.id,
                              f"Advisory candidate is stale: {key} snapshot differs from current input.")
                    break
    return stale, tuple(sorted(set(application_refs))), tuple(sorted(set(inference_refs)))


def _scenario_results(matrix: ScenarioMatrix, selected: tuple[str, ...],
                      requirements: tuple[ReadinessRequirement, ...],
                      findings: tuple[ReadinessFinding, ...]) -> tuple[ScenarioReadinessResult, ...]:
    if not matrix.is_enabled:
        return ()
    results: list[ScenarioReadinessResult] = []
    selected_set = set(selected)
    findings_by_requirement: dict[str, list[ReadinessFinding]] = {}
    for finding in findings:
        findings_by_requirement.setdefault(finding.requirement_id, []).append(finding)
    # A global requirement whose only supporting findings are scoped to one
    # scenario must not make its unaffected peers look blocked. The aggregate
    # still retains that global requirement/status, so no issue is hidden.
    global_requirements = [
        item for item in requirements
        if item.required and item.scenario_id is None and item.id != "SCENARIOS_VALID"
        and not (
            findings_by_requirement.get(item.id)
            and all(finding.scenario_id is not None
                    for finding in findings_by_requirement[item.id])
        )
    ]
    for sid in matrix.active_ids:
        if selected_set and sid not in selected_set:
            continue
        scenario_requirements = [item for item in requirements if item.scenario_id == sid]
        relevant = [*global_requirements, *scenario_requirements]
        scoped_findings = [item for item in findings if item.scenario_id in {None, sid}]
        # Scenario-specific blockers must not be hidden by the aggregate. Global
        # blockers are intentionally inherited because they affect every scenario.
        status = _aggregate_status([item.status for item in relevant])
        scenario = matrix.scenario(sid)
        results.append(ScenarioReadinessResult(
            scenario_id=sid,
            mode=getattr(scenario, "mode", ""),
            corner=getattr(scenario, "corner", ""),
            status=status,
            requirement_ids=tuple(item.id for item in relevant),
            blocker_ids=tuple(item.id for item in scoped_findings if item.severity == ReadinessSeverity.BLOCKER),
            finding_ids=tuple(item.id for item in scoped_findings),
        ))
    return tuple(sorted(results, key=lambda item: item.scenario_id))


def _overall_status(requirements: tuple[ReadinessRequirement, ...], selected_scenarios: tuple[str, ...],
                    findings: tuple[ReadinessFinding, ...]) -> ReadinessStatus:
    selected = set(selected_scenarios)
    findings_by_requirement: dict[str, list[ReadinessFinding]] = {}
    for finding in findings:
        findings_by_requirement.setdefault(finding.requirement_id, []).append(finding)
    relevant: list[ReadinessStatus] = []
    for requirement in requirements:
        if not requirement.required:
            continue
        if requirement.scenario_id is not None:
            if not selected or requirement.scenario_id in selected:
                relevant.append(requirement.status)
            continue
        scoped = findings_by_requirement.get(requirement.id, [])
        # A caller asking for a selected active scenario receives a scoped
        # verdict. Do not let a different scenario's localized aggregate
        # finding recast that selected scope. An unscoped finding remains
        # globally applicable and is always retained.
        if selected and requirement.id == "SCENARIOS_VALID":
            scenario_states = [item for item in requirements if item.scenario_id is not None]
            localized = [item for item in scenario_states if item.status != ReadinessStatus.READY]
            if localized and not any(item.scenario_id in selected for item in localized):
                continue
        if (selected and scoped and all(item.scenario_id is not None for item in scoped)
                and not any(item.scenario_id in selected for item in scoped)):
            continue
        relevant.append(requirement.status)
    return _aggregate_status(relevant)


def _aggregate_status(statuses: Iterable[ReadinessStatus]) -> ReadinessStatus:
    states = set(statuses)
    for status in (
        ReadinessStatus.BLOCKED,
        ReadinessStatus.UNSUPPORTED,
        ReadinessStatus.INCOMPLETE,
        ReadinessStatus.UNKNOWN,
        ReadinessStatus.READY_WITH_WARNINGS,
    ):
        if status in states:
            return status
    return ReadinessStatus.READY


def _issue_key(issue: Any) -> tuple[str, str, str, str, str]:
    return (
        str(getattr(issue, "issue_id", "")), str(getattr(issue, "scenario_id", "") or ""),
        str(getattr(issue, "constraint_id", "") or ""), _issue_code(issue), str(getattr(issue, "message", "")),
    )


def _enum_value(value: Any) -> str:
    return str(value.value) if hasattr(value, "value") else str(value)


def _issue_code(issue: Any) -> str:
    return _enum_value(getattr(issue, "code", ""))


def _issue_category(issue: Any) -> str:
    return _enum_value(getattr(issue, "category", ""))


def _resolution(issue: Any) -> str:
    return str(getattr(issue, "resolution_status", "RESOLVED")).upper()


def _status_for_issue(issue: Any) -> ReadinessStatus:
    if bool(getattr(issue, "blocking", False)) or getattr(issue, "severity", None) in {
        Severity.CRITICAL, Severity.HIGH, Severity.ERROR,
    }:
        return ReadinessStatus.BLOCKED
    if _resolution(issue) in _UNRESOLVED:
        return ReadinessStatus.UNKNOWN
    return ReadinessStatus.READY_WITH_WARNINGS


def _severity(status: ReadinessStatus) -> ReadinessSeverity:
    if status in {ReadinessStatus.BLOCKED, ReadinessStatus.UNSUPPORTED}:
        return ReadinessSeverity.BLOCKER
    if status in {ReadinessStatus.INCOMPLETE, ReadinessStatus.UNKNOWN, ReadinessStatus.READY_WITH_WARNINGS}:
        return ReadinessSeverity.WARNING
    return ReadinessSeverity.INFORMATION


def _add_issue_finding(collector: _Collector, requirement_id: str, category: str,
                       status: ReadinessStatus, issue: Any, action: str) -> None:
    issue_status = status if status in {ReadinessStatus.BLOCKED, ReadinessStatus.INCOMPLETE} else _status_for_issue(issue)
    collector.finding(
        requirement_id=requirement_id, category=category, severity=_severity(issue_status), status=issue_status,
        message=str(getattr(issue, "message", "Existing validator finding.")),
        affected_object=(getattr(issue, "constraint_id", None)
                         or (getattr(issue, "object_names", []) or [None])[0]),
        scenario_id=getattr(issue, "scenario_id", None), evidence=(_issue_evidence(issue),),
        missing_information=(str(getattr(issue, "code", "")),), suggested_action=(getattr(issue, "suggestion", None) or action),
        provenance_references=tuple(filter(None, [getattr(issue, "constraint_id", None),
                                                   *getattr(issue, "related_constraint_ids", [])])),
    )


def _issue_evidence(issue: Any) -> ReadinessEvidence:
    return ReadinessEvidence(
        state=EvidenceState.VALIDATED, source="validation", reference_id=str(getattr(issue, "issue_id", "")),
        detail=f"{_issue_category(issue)}/{_issue_code(issue)}: {_resolution(issue)}",
        scenario_id=getattr(issue, "scenario_id", None),
    )


def _coverage_evidence(key: str, value: Any, source_identity: dict[str, str]) -> tuple[ReadinessEvidence, ...]:
    return (ReadinessEvidence(
        state=EvidenceState.VALIDATED if value not in {None, "UNKNOWN"} else EvidenceState.UNKNOWN,
        source="coverage", reference_id=key, detail=f"{key}={value}",
        snapshot_identity=source_identity["constraint_set"],
    ),)


def _source_evidence(value: Any, source: str, snapshot_identity: str) -> tuple[ReadinessEvidence, ...]:
    return (ReadinessEvidence(
        state=EvidenceState.STRUCTURAL if value is not None else EvidenceState.MISSING,
        source=source, detail=("Available." if value is not None else "Unavailable."),
        snapshot_identity=snapshot_identity or None,
    ),)


def _source_identity(config: Any, cset: ConstraintSet, design: Design | None,
                     timing_graph: TimingGraph | None, matrix: ScenarioMatrix) -> dict[str, str]:
    return {
        "config": stable_hash(config.identity_dict() if hasattr(config, "identity_dict") else config.model_dump()) if hasattr(config, "model_dump") else stable_hash(config),
        "constraint_set": stable_hash_cset(cset),
        "design": stable_hash(design.snapshot()) if design is not None else "",
        "timing_graph": stable_hash(timing_graph.model_dump()) if timing_graph is not None else "",
        "scenario_matrix": matrix.hash_key(),
    }


def _source_kind_evidence_state(source_kind: SourceKind) -> EvidenceState:
    if source_kind == SourceKind.USER:
        return EvidenceState.USER_PROVIDED
    if source_kind == SourceKind.INFERENCE:
        return EvidenceState.INFERRED
    if source_kind in {SourceKind.TOOL, SourceKind.PHYSICAL_DATA, SourceKind.LIBRARY}:
        return EvidenceState.OBSERVED
    return EvidenceState.AUTHORITATIVE


def _flow_backend(config: Any) -> str:
    return str(getattr(getattr(config, "flow", None), "backend", "generic"))


def _formal_required(config: Any) -> bool:
    formal = getattr(config, "formal", None)
    return bool(formal is not None and getattr(formal, "backend", "conservative") == "symbiyosys")


def _project_name(config: Any) -> str:
    return str(getattr(getattr(config, "project", None), "name", "readiness"))


def _finding_key(item: ReadinessFinding) -> tuple[int, str, str, str, str]:
    severity = {ReadinessSeverity.BLOCKER: 0, ReadinessSeverity.WARNING: 1, ReadinessSeverity.INFORMATION: 2}
    return (severity[item.severity], item.requirement_id, item.scenario_id or "", item.affected_object or "", item.id)


def _provenance_references(cset: ConstraintSet, application_refs: tuple[str, ...],
                           inference_refs: tuple[str, ...]) -> tuple[str, ...]:
    refs = {constraint.id for constraint in cset}
    refs.update(application_refs)
    refs.update(inference_refs)
    return tuple(sorted(refs))


def _evidence_summary(cset: ConstraintSet, requirements: tuple[ReadinessRequirement, ...],
                      application_refs: tuple[str, ...], inference_refs: tuple[str, ...]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for requirement in requirements:
        for evidence in requirement.evidence:
            counts[evidence.state.value] += 1
    for constraint in cset:
        for evidence in constraint.provenance.evidence:
            counts[_evidence_state_for_kind(evidence.kind).value] += 1
    return {
        "by_state": dict(sorted(counts.items())),
        "constraint_count": len(cset),
        "recognized_application_count": len(application_refs),
        "advisory_candidate_count": len(inference_refs),
    }


def _evidence_state_for_kind(kind: str) -> EvidenceState:
    normalized = str(kind).lower()
    if normalized == "formal":
        return EvidenceState.VALIDATED
    if normalized == "user":
        return EvidenceState.USER_PROVIDED
    if normalized == "structural":
        return EvidenceState.STRUCTURAL
    if normalized in {"rule", "heuristic", "naming_hint"}:
        return EvidenceState.INFERRED
    if normalized in {"tool", "library", "physical", "simulation"}:
        return EvidenceState.OBSERVED
    return EvidenceState.UNKNOWN


__all__ = ["ConstraintReadinessEngine", "assess_constraint_readiness"]
