"""
Explanation engine (Manual §70, §100).

Provides machine- and human-readable explanations for constraints,
warnings, and optimization decisions.
"""

from __future__ import annotations

from typing import Any

from ..constraint_model import Constraint, ConstraintSet
from ..inference.application import ConstraintApplicationResult
from ..inference.rules import InferenceCandidate
from ..lineage.models import ConstraintLineageReport, LineageChangeKind
from ..optimizer import Candidate
from ..readiness.models import ConstraintReadinessReport, ReadinessSeverity


def explain_constraint(c: Constraint) -> str:
    lines = [f"Constraint {c.id}: {c.type.value}"]
    if c.values:
        lines.append(f"  Values: {c.values}")
    if c.target_objects:
        lines.append(f"  Targets: {c.target_objects}")
    lines.append(f"  Source: {c.source_kind.value}")
    lines.append(f"  Confidence: {c.confidence.value}")
    lines.append(f"  Status: {c.status.value}")
    if c.provenance:
        if c.provenance.explanation:
            lines.append(f"  Why: {c.provenance.explanation}")
        for ev in c.provenance.evidence:
            lines.append(f"    evidence[{ev.kind}]: {ev.description}")
            # Step-26 reuse remains ordinary provenance evidence. Surface its
            # stable knowledge identity here instead of inventing a parallel
            # explanation model.
            if ev.rule_id == "KNOWLEDGE-REUSE":
                item_id = ev.detail.get("knowledge_item_id")
                if item_id:
                    lines.append(f"    knowledge item: {item_id} (explicit acceptance; requires confirmation)")
        for aid in c.provenance.assumption_ids:
            lines.append(f"    assumption: {aid}")
    if c.assumption_ids:
        lines.append(f"  Assumptions: {c.assumption_ids}")
    return "\n".join(lines)


def explain_inference_candidate(candidate: InferenceCandidate) -> str:
    """Explain an advisory candidate without representing it as accepted UCM."""
    lines = [
        f"Inference candidate {candidate.id}: {candidate.kind}",
        f"  Advisory status: {candidate.status.value}",
        f"  Acceptance decision: {candidate.decision.value}",
        f"  Analysis: {candidate.analysis}",
        f"  Why: {candidate.rationale}",
    ]
    if candidate.source_objects:
        lines.append(f"  Source objects: {list(candidate.source_objects)}")
    if candidate.rule_ids:
        lines.append(f"  Rules: {list(candidate.rule_ids)}")
    for evidence in candidate.evidence:
        lines.append(f"    evidence[{evidence.kind}]: {evidence.description}")
    for missing in candidate.missing_information:
        lines.append(f"  Missing: [{missing.get('id', '?')}] {missing.get('message', '')}")
    for warning in candidate.warnings:
        lines.append(f"  Warning: {warning}")
    for reference in candidate.knowledge_references:
        lines.append("  Knowledge: " + str(reference.get("knowledge_item_id", "?"))
                     + " (advisory only)")
    lines.append("  UCM state: NOT_ACCEPTED")
    return "\n".join(lines)


def explain_constraint_application(receipt: ConstraintApplicationResult) -> str:
    """Explain a Step-28 application receipt without recasting advice as user intent.

    The receipt remains the authoritative typed outcome. This rendering only
    exposes its decision, validation, snapshots, and inherited evidence for a
    reviewer; it does not write UCM, artifacts, or history.
    """
    lines = [
        f"Constraint application {receipt.application_id}: {receipt.status.value}",
        f"  Candidate: {receipt.candidate_id}",
        f"  Explicit decision: {receipt.decision.value}",
        f"  UCM mutated: {receipt.ucm_mutated}",
        (
            f"  UCM snapshot: {receipt.ucm_before_snapshot_identity} -> "
            f"{receipt.ucm_after_snapshot_identity}"
        ),
    ]
    if receipt.candidate_semantic_identity:
        lines.append(f"  Candidate semantic identity: {receipt.candidate_semantic_identity}")
    if receipt.scenario_ids:
        lines.append(f"  Scenario scope: {list(receipt.scenario_ids)}")
    if receipt.applied_constraint_ids:
        lines.append(f"  Applied UCM constraints: {list(receipt.applied_constraint_ids)}")
    if receipt.already_present_ids:
        lines.append(f"  Already present UCM constraints: {list(receipt.already_present_ids)}")
    if receipt.conflict_ids:
        lines.append(f"  Conflicting UCM constraints: {list(receipt.conflict_ids)}")
    if receipt.rejected_constraint_ids:
        lines.append(f"  Rejected candidate constraints: {list(receipt.rejected_constraint_ids)}")
    if receipt.validation_status:
        lines.append(f"  Validation: {receipt.validation_status}")
    if receipt.validation_summary:
        lines.append("  Validation summary: " + str(receipt.validation_summary))
    for issue in receipt.validation_issues:
        lines.append("    validation: " + str(issue.get("message", issue)))
    for reason in receipt.blocking_reasons:
        lines.append(f"  Blocked: {reason}")
    for warning in receipt.warnings:
        lines.append(f"  Warning: {warning}")
    for evidence in receipt.application_evidence:
        lines.append(f"    evidence[{evidence.kind}/{evidence.rule_id}]: {evidence.description}")
    lines.append("  Origin: advisory inference; application does not relabel this as user-authored intent.")
    return "\n".join(lines)


def explain_constraint_readiness(report: ConstraintReadinessReport) -> str:
    """Render a Step-29 readiness report without reinterpreting its evidence.

    This is a human-facing projection of the typed report only. It creates no
    UCM, validation, coverage, proof, artifact, or execution state.
    """
    lines = [
        f"Constraint readiness: {report.status.value}",
        f"  Requirements: {len(report.requirements)}",
        f"  Blockers: {len(report.blockers)}",
        f"  Warnings: {len(report.warnings)}",
    ]
    if report.scenario_results:
        lines.append("  Active scenarios:")
        for scenario in report.scenario_results:
            lines.append(
                f"    {scenario.scenario_id} ({scenario.mode}/{scenario.corner}): "
                f"{scenario.status.value}"
            )
    if report.blockers:
        lines.append("  Blockers:")
        for blocker in report.blockers:
            scope = f" [{blocker.scenario_id}]" if blocker.scenario_id else ""
            lines.append(f"    {blocker.requirement_id}{scope}: {blocker.message}")
    warnings = [item for item in report.findings if item.severity == ReadinessSeverity.WARNING]
    if warnings:
        lines.append("  Warnings:")
        for finding in warnings:
            scope = f" [{finding.scenario_id}]" if finding.scenario_id else ""
            lines.append(f"    {finding.requirement_id}{scope}: {finding.message}")
    if report.stale_evidence:
        lines.append("  Stale evidence:")
        for finding in report.stale_evidence:
            lines.append(f"    {finding.message}")
    if report.next_actions:
        lines.append("  Next actions:")
        lines.extend(f"    - {action}" for action in report.next_actions)
    lines.append("  Boundary: report-only; no UCM, SDC, coverage, history, proof, or EDA state changed.")
    return "\n".join(lines)


def explain_constraint_lineage(report: ConstraintLineageReport) -> str:
    """Render Step-30 traceability without asserting new lifecycle facts.

    The typed lineage report remains a read-only projection of UCM and its
    existing evidence. This renderer neither changes canonical intent nor
    turns temporal snapshot correlation into causality.
    """
    lines = [
        f"Constraint lineage: {report.snapshot.name}",
        f"  Snapshot: {report.snapshot.snapshot_identity}",
        f"  Current canonical constraints: {len(report.constraints)}",
        f"  Application attempts: {len(report.application_attempts)}",
        f"  Advisory candidates: {len(report.advisory_candidates)}",
    ]
    for entry in sorted(report.constraints, key=lambda item: item.constraint_id):
        lines.append(f"  [{entry.constraint_id}] {entry.constraint_type}")
        lines.append(f"    Source: {entry.source.value} (origin: {entry.origin_source.value})")
        lines.append(f"    Scenario scope: {entry.scenario_scope.scope_kind} "
                     f"{list(entry.scenario_scope.scenario_ids)}")
        if entry.candidate_id:
            lines.append(f"    Candidate: {entry.candidate_id}")
        if entry.application_id:
            lines.append(f"    Explicit application: {entry.application_id}")
        if entry.knowledge_references:
            knowledge_ids = [str(item.get("knowledge_item_id", "?")) for item in entry.knowledge_references]
            lines.append("    Knowledge references (advisory): " + ", ".join(sorted(knowledge_ids)))
        if entry.validation_events:
            lines.append("    Validation evidence: " + ", ".join(
                sorted(event.kind.value for event in entry.validation_events)
            ))
        if entry.formal_events:
            lines.append("    Formal verification evidence: " + ", ".join(
                sorted(str(event.details.get("verification_status", "UNVERIFIED"))
                       for event in entry.formal_events)
            ))
        if entry.events:
            stale = [event for event in entry.events if event.stale]
            if stale:
                lines.append("    Stale evidence: " + "; ".join(event.message for event in stale))
        for gap in entry.linkage_gaps:
            lines.append(f"    Linkage gap: {gap}")
    if report.change_set is not None:
        changes = report.change_set.changes
        counts = {kind.value: sum(1 for item in changes if item.kind == kind) for kind in LineageChangeKind}
        lines.append("  Semantic snapshot changes: " + ", ".join(
            f"{kind}={counts[kind]}" for kind in sorted(counts)
        ))
        for event in report.change_set.readiness_events:
            lines.append(f"  Readiness: {event.message}")
    lines.append("  Boundary: lineage is a read-only traceability projection, not a second canonical history or provenance authority.")
    return "\n".join(lines)


def explain_candidate(c: Candidate, baseline: Candidate | None = None) -> str:
    lines = [f"Candidate {c.id}: decision={c.decision.value}",
             f"  Parent: {c.parent_id}",
             f"  Feasible: {c.feasible}",
             f"  Changes: {c.generated_changes}",
             f"  Reason: {c.decision_reason}"]
    if c.qor:
        q = c.qor.summary()
        lines.append(f"  QoR: WNS={q['setup_wns_ns']}ns hold={q['hold_wns_ns']}ns "
                     f"area={q['area_total']} power={q['power_total']}")
    if c.warnings:
        lines.append(f"  Warnings: {c.warnings}")
    return "\n".join(lines)


def design_report(design_summary: dict[str, Any], tg_summary: dict[str, Any],
                  validation: dict[str, Any] | None, coverage: dict[str, Any] | None,
                  cset: ConstraintSet, missing: list[dict[str, str]],
                  qor_summary: dict[str, Any] | None = None) -> str:
    """Produce a human-readable report (Manual §101)."""
    lines: list[str] = []
    a = lines.append
    a("RTL Constraint Assistant")
    a("========================")
    a("")
    a(f"Design: {design_summary.get('name')}")
    a(f"Top module: {design_summary.get('top')}")
    a(f"Files: {design_summary.get('source_files', 0)}")
    a("")
    a("CLOCKS")
    a("------")
    for n, c in tg_summary.get("clocks", {}).items():
        period = f"{c['period_ns']:.3f} ns" if c.get("period_ns") else "(unknown period)"
        fixed = "FIXED" if c.get("status") == "FIXED" else c.get("status", "")
        a(f"  {n:12s} detected / {c['edge']:7s} / {period:>15s} / {fixed}")
    a("")
    a("RESETS")
    a("------")
    for n, r in tg_summary.get("resets", {}).items():
        a(f"  {n:12s} {r['type']} / {r['polarity']}")
    a("")
    a("CLOCK RELATIONSHIPS")
    a("-------------------")
    for e in tg_summary.get("domain_edges", []):
        a(f"  {e['a']} <-> {e['b']} : {e['relationship'].upper()} "
          f"(confidence={e['confidence']})")
    a("")
    a("MISSING INFORMATION")
    a("-------------------")
    for m in missing:
        a(f"  [{m.get('severity','?'):10s}] {m['message']}")
    a("")
    a("CONSTRAINT QUALITY")
    a("------------------")
    if coverage:
        a(f"  Clock source coverage: {coverage.get('clock_source_coverage_pct', 'UNKNOWN')}%")
        a(f"  Input timing coverage:  {coverage.get('input_timing_path_coverage_pct', 'UNKNOWN')}%")
        a(f"  Output timing coverage: {coverage.get('output_timing_path_coverage_pct', 'UNKNOWN')}%")
    if validation:
        summary = validation.get("summary", {})
        a(f"  Validation errors: {summary.get('errors', 0)}")
        a(f"  Validation warnings: {summary.get('warnings', 0)}")
    a("")
    a("POWER (MOST RECENT RECORDED RUN)")
    a("---------------------------------")
    if qor_summary is None:
        a("  No completed QoR artifact is available. This command did not run a power tool.")
    else:
        status = qor_summary.get("power_status", "UNAVAILABLE")
        total = qor_summary.get("power_total", qor_summary.get("power"))
        if status == "AVAILABLE" and total is not None:
            a(f"  Tool-reported power: {total:.6g} W")
            dynamic = qor_summary.get("power_dynamic")
            leakage = qor_summary.get("power_leakage")
            if dynamic is not None:
                a(f"  Dynamic power: {dynamic:.6g} W")
            if leakage is not None:
                a(f"  Leakage power: {leakage:.6g} W")
        else:
            # Detailed report-parser classification is provenance, while
            # canonical QoR intentionally remains UNAVAILABLE here.
            provenance = qor_summary.get("power_provenance") or {}
            parse_status = provenance.get("parsing_status")
            suffix = (f" (report parser: {parse_status})"
                      if parse_status and parse_status != status else "")
            a(f"  Power: {status}{suffix}")
        provenance = qor_summary.get("power_provenance") or {}
        if provenance:
            a(f"  Source: {provenance.get('report_path') or '-'}")
            a(f"  Format: {provenance.get('format') or '-'}")
            a(f"  SHA-256: {provenance.get('sha256') or '-'}")
    a("")
    a(f"GENERATED CONSTRAINTS ({len(cset)})")
    a("-" * 30)
    for c in cset:
        a(f"  [{c.id}] {c.type.value:30s} targets={c.target_objects} values={c.values}")
    a("")
    return "\n".join(lines)
