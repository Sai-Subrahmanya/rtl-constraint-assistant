"""
Explanation engine (Manual §70, §100).

Provides machine- and human-readable explanations for constraints,
warnings, and optimization decisions.
"""

from __future__ import annotations

from typing import Any

from ..constraint_model import Constraint, ConstraintSet
from ..optimizer import Candidate
from ..utils.enums import ConstraintType


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
        for aid in c.provenance.assumptions:
            lines.append(f"    assumption: {aid}")
    if c.assumption_ids:
        lines.append(f"  Assumptions: {c.assumption_ids}")
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
                  cset: ConstraintSet, missing: list[dict[str, str]]) -> str:
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
    a(f"GENERATED CONSTRAINTS ({len(cset)})")
    a("-" * 30)
    for c in cset:
        a(f"  [{c.id}] {c.type.value:30s} targets={c.target_objects} values={c.values}")
    a("")
    return "\n".join(lines)
