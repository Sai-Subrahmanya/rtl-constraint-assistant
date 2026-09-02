"""
Inference engine — runs all registered rules over the design/timing graph
and produces a populated ConstraintSet + reports (Manual §119).

Architectural boundary:

* Rules produce :class:`InferenceResult` objects (see ``rules.py``).
  They do not write to the ConstraintSet or AssumptionLedger.
* The engine is responsible for executing rules, validating results,
  merging evidence from multiple rules onto the same semantic
  constraint, attaching full provenance (rule_id, evidence,
  confidence), updating the AssumptionLedger with *actual*
  assumptions (not user values), and reporting missing information.

The engine NEVER fabricates high-risk timing values:
no default clock selection, no guessed period, no guessed I/O delay,
no guessed clock relationship.  Missing information is surfaced
structurally via :class:`MissingInformation`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from ..config.model import ProjectConfig
from ..constraint_model import Constraint, ConstraintSet, PathSelector
from ..constraint_model.constraint_set import ValidationIssue
from ..design_model import Design
from ..provenance import AssumptionLedger, Evidence, ImportMetadata, ProvenanceRecord
from ..timing_model import TimingGraph
from ..utils.enums import (
    Confidence,
    ConstraintStatus,
    ConstraintType,
    GenerationConfidence,
    InferenceResultStatus,
    OptimizationStatus,
    RequirementLevel,
    SourceKind,
)
from ..utils.logging import get_logger
from ..utils.units import parse_frequency_string, parse_time_string
from . import clock_rules, generated_clock_rules, io_rules, relationship_rules, reset_rules
from .rules import InferenceResult, MissingInformation, ProposedConstraint, Rule

log = get_logger("inference")


@dataclass
class InferenceReport:
    results: list[InferenceResult] = field(default_factory=list)
    ambiguities: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    missing_information: list[dict[str, Any]] = field(default_factory=list)
    assumptions_added: list[dict[str, Any]] = field(default_factory=list)
    constraints_added: int = 0
    evidence_count: int = 0

    # Convenience --------------------------------------------------

    def required_information(self) -> list[dict[str, Any]]:
        return [mi for mi in self.missing_information
                if mi.get("requirement_level") in ("REQUIRED", "UNSAFE_TO_INFER")]

    def format_required(self) -> str:
        lines = ["REQUIRED INFORMATION", "-" * 20]
        for i, mi in enumerate(self.required_information(), 1):
            rid = mi.get("id", f"REQ-{i:03d}")
            lines.append(f"[{rid}] {mi.get('message','')}")
            rat = mi.get("rationale")
            if rat:
                lines.append(f"        Reason: {rat}")
            ev = mi.get("evidence") or []
            if ev:
                first = ev[0]
                if isinstance(first, dict) and first.get("description"):
                    lines.append(f"        Evidence: {first['description']}")
            if mi.get("blocking"):
                lines.append("        (blocking)")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules_run": len(self.results),
            "constraints_added": self.constraints_added,
            "result_statuses": {
                s.value: sum(1 for r in self.results if r.result_status == s)
                for s in InferenceResultStatus
            },
            "ambiguities": self.ambiguities,
            "warnings": self.warnings,
            "conflicts": self.conflicts,
            "missing_information": self.missing_information,
            "required_information": self.required_information(),
            "assumptions_added": len(self.assumptions_added),
            "evidence_count": self.evidence_count,
            "per_rule": [
                {
                    "rule_id": r.rule_id,
                    "rule_name": r.rule_name,
                    "result_status": r.result_status.value,
                    "confidence": r.confidence.value if isinstance(r.confidence, Confidence) else r.confidence,
                    "constraints": [pc.to_dict() for pc in r.proposed_constraints],
                    "warnings": r.warnings,
                    "ambiguities": r.ambiguities,
                    "conflicts": r.conflicts,
                    "missing_information": [mi.to_dict() for mi in r.missing_information],
                    "evidence_count": len(r.evidence),
                }
                for r in sorted(self.results, key=lambda x: x.rule_id)
            ],
        }


class InferenceEngine:
    def __init__(self) -> None:
        self.rules: list[Rule] = []
        self._register_default_rules()

    def _register_default_rules(self) -> None:
        defs = [
            # User-first rules run first so USER evidence is present
            # before structural rules merge evidence onto it.
            ("CLK-003", "user_clock_specification", lambda *a, **k: True,
             clock_rules.rule_clk_003_user_period, "HIGH",
             "Apply user-provided clock periods (highest precedence)."),
            ("CLK-001", "sequential_clock_candidate", lambda *a, **k: True,
             clock_rules.rule_clk_001_sequential, "HIGH",
             "Detect clocks from posedge/negedge in sequential processes."),
            ("CLK-002", "clock_name_hints", lambda *a, **k: True,
             clock_rules.rule_clk_002_name_hints, "LOW",
             "Name-based clock hints (corroborating only, never sole evidence)."),
            ("RST-001", "async_reset_from_sensitivity", lambda *a, **k: True,
             reset_rules.rule_rst_001_async_sensitivity, "HIGH",
             "Detect asynchronous resets from sensitivity lists."),
            ("RST-002", "sync_reset_detection", lambda *a, **k: True,
             reset_rules.rule_rst_002_sync_reset, "MEDIUM",
             "Identify potential synchronous resets (candidates only)."),
            ("RST-003", "adversarial_reset_name_usage", lambda *a, **k: True,
             reset_rules.rule_rst_003_adversarial, "MEDIUM",
             "Flag reset-named signals used as data."),
            ("REL-001", "clock_relationship_status", lambda *a, **k: True,
             relationship_rules.rule_rel_001_relationships, "HIGH",
             "Track clock-domain relationships; user overrides win."),
            ("IO-001", "io_port_classification", lambda *a, **k: True,
             io_rules.rule_io_001_classify, "HIGH",
             "Classify top IO for delay constraints."),
            ("IO-002", "missing_input_delay", lambda *a, **k: True,
             io_rules.rule_io_002_missing_input_delay, "HIGH",
             "Detect inputs without set_input_delay; no default clock."),
            ("IO-003", "missing_output_delay", lambda *a, **k: True,
             io_rules.rule_io_003_missing_output_delay, "HIGH",
             "Detect outputs without set_output_delay; no default clock."),
            ("GCLK-001", "clock_divider_candidate", lambda *a, **k: True,
             generated_clock_rules.rule_gclk_001_divider_candidate, "LOW",
             "Detect possible clock dividers (candidates only)."),
            ("GCLK-002", "gated_clock_candidate", lambda *a, **k: True,
             generated_clock_rules.rule_gclk_002_gated_clock_candidate, "LOW",
             "Detect possible gated clocks (candidates only)."),
            ("GCLK-003", "clock_mux_candidate", lambda *a, **k: True,
             generated_clock_rules.rule_gclk_003_mux_candidate, "LOW",
             "Detect possible clock muxes (candidates only)."),
        ]
        for rid, name, applies, infer, conf, desc in defs:
            self.rules.append(Rule(id=rid, name=name, applies=applies, infer=infer,
                                   confidence=conf, description=desc))

    def add_rule(self, rule: Rule) -> None:
        self.rules.append(rule)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(
        self,
        design: Design,
        tg: TimingGraph,
        config: ProjectConfig,
        cset: ConstraintSet,
        ledger: AssumptionLedger,
        *,
        run_ts: str | None = None,
    ) -> InferenceReport:
        """Run all applicable rules; apply their outputs into the ConstraintSet.

        Parameters
        ----------
        run_ts: optional ISO8601 UTC timestamp used for evidence and
            provenance ``created_at``.  Providing a fixed value makes
            the resulting ConstraintSet fully reproducible across
            processes; when omitted the current UTC wall-clock time is
            used.  Timestamps are intentionally NOT part of evidence
            identity (see :func:`rca.inference._evidence.evidence_id`).
        """
        report = InferenceReport()
        self._ev_seen: set[tuple[str, str, tuple[str, ...]]] = set()
        if run_ts is None:
            from datetime import datetime, timezone
            run_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._run_ts = run_ts
        user_clocks = self._collect_user_clocks(config)
        user_io = {
            "inputs": {k: v.model_dump() for k, v in config.constraints.user.io.inputs.items()},
            "outputs": {k: v.model_dump() for k, v in config.constraints.user.io.outputs.items()},
        }
        user_rels = [r.model_dump() for r in config.constraints.user.relationships]

        # Stamp the ConstraintSet's own created_at with run_ts for
        # canonical-snapshot reproducibility.
        cset.created_at = self._run_ts

        ctx = dict(design=design, tg=tg, user_clocks=user_clocks,
                   user_io=user_io, user_rels=user_rels, config=config,
                   cset=cset, ledger=ledger, _run_ts=self._run_ts)

        for rule in self.rules:
            try:
                result = rule.infer(**ctx)
            except Exception as e:  # pragma: no cover - defensive
                log.error("Rule %s failed: %s", rule.id, e)
                result = InferenceResult(rule_id=rule.id, rule_name=rule.name,
                                         result_status=InferenceResultStatus.ERROR,
                                         confidence=Confidence.UNKNOWN)
                result.add_warning(f"Rule {rule.id} failed: {e}", rule=rule.id)
            # Normalize confidence enum.
            if not isinstance(result.confidence, Confidence):
                try:
                    result.confidence = Confidence(str(result.confidence).upper())
                except Exception:
                    result.confidence = Confidence.UNKNOWN
            report.results.append(result)
            report.warnings.extend(result.warnings)
            report.ambiguities.extend(result.ambiguities)
            report.conflicts.extend(result.conflicts)
            for mi in result.missing_information:
                report.missing_information.append(mi.to_dict())
            report.assumptions_added.extend(result.assumptions_added)
            for ev in result.evidence:
                key = (ev.kind, ev.description, tuple(sorted(ev.source_objects)))
                if key in self._ev_seen:
                    continue
                self._ev_seen.add(key)
                report.evidence_count += 1
        self._ev_seen = set()

        # Also include TimingGraph missing_information (clock periods,
        # structural candidates) as MISSING_INFORMATION entries, but
        # de-duplicate against entries already produced by rules.
        rule_mi_keys = {(m.get("category"), m.get("object")) for m in report.missing_information}
        for raw in tg.missing_information():
            sev = raw.get("severity", "")
            cat = raw.get("category", "")
            obj = raw.get("object", "")
            if (cat, obj) in rule_mi_keys:
                continue
            lvl = {
                "required": RequirementLevel.REQUIRED.value,
                "recommended": RequirementLevel.RECOMMENDED.value,
                "confirmation_required": RequirementLevel.UNSAFE_TO_INFER.value,
            }.get(sev, RequirementLevel.RECOMMENDED.value)
            blocking = sev in ("required",)
            report.missing_information.append({
                "id": f"TG-{cat}-{obj}".replace(" ", "_"),
                "category": cat,
                "object": obj,
                "severity": "ERROR" if sev == "required" else "WARNING",
                "requirement_level": lvl,
                "message": raw.get("message", ""),
                "rationale": "Structural finding from timing graph.",
                "evidence": [{"kind": "structural"}],
                "suggested_inputs": [],
                "blocking": blocking,
                "rule_id": None,
                "possible_values": [],
            })

        # ------------------------------------------------------------------
        # Materialize: merge proposals across rules by merge_key -> UCM.
        # ------------------------------------------------------------------
        # Group proposals by merge_key (or by (kind, object) when unset).
        merged: dict[tuple, dict[str, Any]] = {}
        for result in report.results:
            for pc in result.proposed_constraints:
                key = pc.merge_key or (pc.kind, pc.object)
                slot = merged.setdefault(key, {
                    "kind": pc.kind, "object": pc.object,
                    "evidence": [], "confidences": [],
                    "clock": pc.clock, "period": pc.period_seconds,
                    "delay": pc.delay_seconds, "values": dict(pc.values),
                    "source_kinds": [], "statuses": [],
                    "rationale": [], "targets": set(pc.target_objects),
                    "sources": set(pc.source_objects),
                    "assumption_ids": set(pc.assumption_ids),
                    "scenario_ids": set(pc.scenario_ids),
                    "path_selector": pc.path_selector,
                    "origin_rule_ids": set(),
                    "user_present": False,
                })
                for ev in pc.evidence:
                    slot["evidence"].append(ev)
                if pc.confidence:
                    slot["confidences"].append(pc.confidence if isinstance(pc.confidence, Confidence)
                                               else Confidence(str(pc.confidence).upper()))
                if pc.source_kind:
                    slot["source_kinds"].append(pc.source_kind)
                if pc.status:
                    slot["statuses"].append(pc.status)
                slot["rationale"].append(pc.rationale or "")
                slot["targets"].update(pc.target_objects)
                slot["sources"].update(pc.source_objects)
                slot["assumption_ids"].update(pc.assumption_ids)
                slot["scenario_ids"].update(pc.scenario_ids)
                slot["origin_rule_ids"].add(result.rule_id)
                if pc.source_kind == SourceKind.USER.value:
                    slot["user_present"] = True
                # Merge values
                for k, v in pc.values.items():
                    slot["values"].setdefault(k, v)
                # User clock/delay overrides any structural value.
                if pc.source_kind == SourceKind.USER.value:
                    if pc.clock:
                        slot["clock"] = pc.clock
                    if pc.period_seconds is not None:
                        slot["period"] = pc.period_seconds
                    if pc.delay_seconds is not None:
                        slot["delay"] = pc.delay_seconds

        for key, slot in merged.items():
            con = self._build_constraint(slot, ledger, report)
            if con is None:
                continue
            # Conflict detection: user vs inference disagreement is
            # recorded but the user value is preserved.
            existing = self._find_semantic_match(cset, con)
            if existing is None:
                cset.add(con)
                report.constraints_added += 1
            else:
                # Merge evidence onto the existing constraint instead
                # of creating a duplicate.
                for ev in con.provenance.evidence:
                    if ev.id not in {e.id for e in existing.provenance.evidence}:
                        existing.provenance.add_evidence(ev)
                # If new is USER and existing is INFERENCE, note conflict
                # but keep existing (user) values intact.
                if (con.source_kind == SourceKind.USER
                        and existing.source_kind == SourceKind.INFERENCE):
                    report.conflicts.append({
                        "message": (f"User-supplied constraint for {con.id} matches an inferred "
                                    "constraint; user value retained."),
                        "subject": con.id,
                    })
                # If existing is USER and new is INFERENCE, do not override.
        # Add relationship constraints that need explicit clock groups.
        # (Already proposed via REL-001.)
        report.missing_information.sort(key=lambda m: (m.get("category", ""), m.get("object", "")))
        return report

    # ------------------------------------------------------------------
    # Materialization helpers
    # ------------------------------------------------------------------

    def _build_constraint(self, slot: dict[str, Any], ledger: AssumptionLedger,
                          report: InferenceReport):
        kind = slot["kind"]
        # Deduplicate evidence by stable semantic id (rule_id, kind,
        # description, sorted source_objects). This is independent of
        # insertion order, so merging evidence across rules is stable.
        seen_ids: set[str] = set()
        norm_ev: list[Evidence] = []
        # Sort deterministically before assigning so output ordering is
        # reproducible across processes.
        for ev in sorted(slot["evidence"], key=lambda e: (e.id, e.kind, e.description)):
            if ev.id in seen_ids:
                continue
            seen_ids.add(ev.id)
            evid = ev.model_copy(update={"created_at": self._run_ts})
            norm_ev.append(evid)

        # Choose provenance/source_kind: USER > EXISTING_SDC > INFERENCE
        if slot["user_present"]:
            source_kind = SourceKind.USER
        elif any(sk == SourceKind.EXISTING_SDC.value for sk in slot["source_kinds"]):
            source_kind = SourceKind.EXISTING_SDC
        else:
            source_kind = SourceKind.INFERENCE

        # Choose confidence: highest among sources
        rank = {Confidence.UNKNOWN: 0, Confidence.LOW: 1, Confidence.MEDIUM: 2, Confidence.HIGH: 3}
        conf = Confidence.MEDIUM
        for c in slot["confidences"]:
            if rank.get(c, 0) > rank.get(conf, 0):
                conf = c
        if source_kind == SourceKind.USER:
            conf = Confidence.HIGH

        # Status: FIXED if USER.fixed, CONFIRMED HIGH, else PROPOSED.
        status = ConstraintStatus.PROPOSED
        if source_kind == SourceKind.USER:
            status = ConstraintStatus.FIXED if "FIXED" in slot["statuses"] else ConstraintStatus.CONFIRMED
        elif conf == Confidence.HIGH:
            status = ConstraintStatus.CONFIRMED

        opt = OptimizationStatus.FIXED if status == ConstraintStatus.FIXED else OptimizationStatus.TUNABLE

        prov = ProvenanceRecord(
            created_by=",".join(sorted(slot["origin_rule_ids"])) or "rca",
            created_at=self._run_ts,
            source_kind=source_kind.value,
            confidence=conf.value,
            explanation="; ".join(x for x in slot["rationale"] if x) or f"{kind} on {slot['object']}",
            rule_id=",".join(sorted(slot["origin_rule_ids"])) or None,
        )
        for ev in norm_ev:
            prov.add_evidence(ev)

        if kind == "create_clock":
            if slot["period"] is None:
                return None  # blocked by missing period
            c = Constraint(
                id=f"CLK__{slot['object']}",
                type=ConstraintType.CREATE_CLOCK,
                target_objects=list(slot["targets"]) or [slot["object"]],
                clock_refs=[slot["object"]],
                values=copy.deepcopy(slot["values"]),
                source_kind=source_kind,
                provenance=prov,
                confidence=conf,
                status=status,
                opt_status=opt,
                generation_confidence=(GenerationConfidence.USER_SPECIFIED
                                       if source_kind == SourceKind.USER
                                       else GenerationConfidence.INFERRED_HIGH_CONFIDENCE),
                scenario_ids=sorted(slot["scenario_ids"]),
                assumption_ids=sorted(slot["assumption_ids"]),
            )
            return c
        if kind == "set_clock_uncertainty":
            if "uncertainty" not in slot["values"]:
                return None
            c = Constraint(
                id=f"UNC__{slot['object']}",
                type=ConstraintType.SET_CLOCK_UNCERTAINTY,
                target_objects=[slot["object"]],
                clock_refs=[slot["object"]],
                values=copy.deepcopy(slot["values"]),
                source_kind=source_kind,
                provenance=prov,
                confidence=conf, status=status, opt_status=opt,
            )
            return c
        if kind == "set_input_delay":
            clk = slot.get("clock")
            d = slot.get("delay")
            if not clk or d is None:
                return None
            values = {"clock": clk, "delay": d,
                      "min_max": slot["values"].get("min_max", "max")}
            c = Constraint(
                id=f"INP__{slot['object']}",
                type=ConstraintType.SET_INPUT_DELAY,
                target_objects=[slot["object"]],
                clock_refs=[clk],
                values=values,
                source_kind=source_kind,
                provenance=prov,
                confidence=conf, status=status, opt_status=opt,
                scenario_ids=sorted(slot["scenario_ids"]),
            )
            return c
        if kind == "set_output_delay":
            clk = slot.get("clock")
            d = slot.get("delay")
            if not clk or d is None:
                return None
            values = {"clock": clk, "delay": d,
                      "min_max": slot["values"].get("min_max", "max")}
            c = Constraint(
                id=f"OUT__{slot['object']}",
                type=ConstraintType.SET_OUTPUT_DELAY,
                target_objects=[slot["object"]],
                clock_refs=[clk],
                values=values,
                source_kind=source_kind,
                provenance=prov,
                confidence=conf, status=status, opt_status=opt,
            )
            return c
        if kind == "set_clock_groups":
            groups = slot["values"].get("groups")
            if not groups:
                return None
            c = Constraint(
                id=f"CG__{'_'.join(sorted(g for grp in groups for g in grp))}",
                type=ConstraintType.SET_CLOCK_GROUPS,
                values=dict(groups=groups, relationship=slot["values"].get("relationship", "asynchronous")),
                clock_refs=sorted(g for grp in groups for g in grp),
                source_kind=source_kind, provenance=prov,
                confidence=conf, status=status, opt_status=opt,
            )
            return c
        # Unknown kind — skip (do not silently emit)
        return None

    def _find_semantic_match(self, cset: ConstraintSet, new: Constraint):
        for existing in cset:
            if existing.semantically_equivalent(new):
                return existing
        return None

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _collect_user_clocks(self, config: ProjectConfig) -> list[dict[str, Any]]:
        out = []
        for c in config.constraints.user.clocks:
            info: dict[str, Any] = {"name": c.name, "fixed": c.fixed, "port": c.port}
            ps = c.period_seconds()
            if ps is not None:
                info["period_seconds"] = ps
            if c.uncertainty is not None:
                try:
                    info["uncertainty_seconds"] = parse_time_string(c.uncertainty)
                except Exception:
                    pass
            out.append(info)
        return out
