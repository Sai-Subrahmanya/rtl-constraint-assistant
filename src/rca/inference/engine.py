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
from dataclasses import dataclass, field, replace
from typing import Any

from ..config.model import ProjectConfig
from ..constraint_model import Constraint, ConstraintSet, stable_hash_cset
from ..design_model import Design
from ..equivalence.normalize import (
    has_unsupported_options,
    normalize_constraint,
    semantic_match_key,
)
from ..provenance import AssumptionLedger, Evidence, ProvenanceRecord
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
from ..utils.hashing import stable_hash
from ..utils.logging import get_logger
from ..utils.units import parse_time_string
from . import clock_rules, generated_clock_rules, io_rules, relationship_rules, reset_rules
from .rules import (
    InferenceAcceptanceResult,
    InferenceCandidate,
    InferenceDecision,
    InferenceResult,
    InferenceStatus,
    Rule,
)

log = get_logger("inference")

# Advisory inference deliberately uses a stable timestamp so candidate JSON,
# evidence, provenance, and IDs do not vary merely because it was rerun.
_CANDIDATE_EPOCH = "1970-01-01T00:00:00+00:00"


@dataclass
class InferenceReport:
    results: list[InferenceResult] = field(default_factory=list)
    # Step-27 advisory layer. Facts and candidates are distinct from the
    # legacy materialized-UCM result used by generate/validate workflows.
    structural_facts: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[InferenceCandidate] = field(default_factory=list)
    ambiguities: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    missing_information: list[dict[str, Any]] = field(default_factory=list)
    assumptions_added: list[dict[str, Any]] = field(default_factory=list)
    # Optional Step-26 advisory references. Knowledge lookup may populate this
    # list after inference, but it never changes rule results or UCM state.
    knowledge_references: list[dict[str, Any]] = field(default_factory=list)
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
        candidates = [candidate.to_dict() for candidate in sorted(self.candidates, key=lambda item: item.id)]
        advisory_by_status = {
            status.value: sum(1 for candidate in self.candidates if candidate.status == status)
            for status in InferenceStatus
        }
        ambiguous = [candidate for candidate in candidates if candidate["status"] == InferenceStatus.AMBIGUOUS.value]
        unsupported_or_rejected = [candidate for candidate in candidates if candidate["status"] in {
            InferenceStatus.UNSUPPORTED.value, InferenceStatus.REJECTED.value, InferenceStatus.CONFLICTING.value,
        }]
        return {
            "rules_run": len(self.results),
            "structural_facts": sorted(self.structural_facts, key=lambda item: item.get("id", "")),
            "candidates": candidates,
            "candidate_statuses": advisory_by_status,
            "ambiguous_candidates": ambiguous,
            "unsupported_or_rejected_candidates": unsupported_or_rejected,
            "constraints_added": self.constraints_added,
            "result_statuses": {
                status.value: sum(1 for result in self.results if result.result_status == status)
                for status in InferenceResultStatus
            },
            "ambiguities": sorted(self.ambiguities, key=lambda item: stable_hash(item)),
            "warnings": sorted(self.warnings, key=lambda item: stable_hash(item)),
            "conflicts": sorted(self.conflicts, key=lambda item: stable_hash(item)),
            "missing_information": sorted(self.missing_information, key=lambda item: stable_hash(item)),
            "required_information": sorted(self.required_information(), key=lambda item: stable_hash(item)),
            "assumptions_added": len(self.assumptions_added),
            "knowledge_references": sorted(self.knowledge_references, key=lambda item: stable_hash(item)),
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

        ctx = {"design": design, "tg": tg, "user_clocks": user_clocks,
               "user_io": user_io, "user_rels": user_rels, "config": config,
               "cset": cset, "ledger": ledger, "_run_ts": self._run_ts}

        for rule in self.rules:
            try:
                result = rule.infer(**ctx)
            except Exception as e:  # noqa: BLE001  # pragma: no cover - defensive
                log.error("Rule %s failed: %s", rule.id, e)
                result = InferenceResult(rule_id=rule.id, rule_name=rule.name,
                                         result_status=InferenceResultStatus.ERROR,
                                         confidence=Confidence.UNKNOWN)
                result.add_warning(f"Rule {rule.id} failed: {e}", rule=rule.id)
            # Normalize confidence enum.
            if not isinstance(result.confidence, Confidence):
                try:
                    result.confidence = Confidence(str(result.confidence).upper())
                except (TypeError, ValueError):
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
    # Step-27 advisory candidate API
    # ------------------------------------------------------------------

    def infer_candidates(
        self,
        design: Design,
        tg: TimingGraph,
        config: ProjectConfig,
        cset: ConstraintSet | None = None,
        ledger: AssumptionLedger | None = None,
        *,
        knowledge: Any | None = None,
        run_ts: str | None = None,
    ) -> InferenceReport:
        """Return deterministic advisory candidates without changing caller UCM.

        The established :meth:`run` materializing path remains for existing
        generate/validate/optimizer callers. This method uses a private cloned
        timing graph and scratch UCM only to reuse the existing rule ordering,
        evidence merge, and UCM-template construction logic. Neither the
        supplied ``cset`` nor its ledger is modified, and the returned
        candidates are not accepted UCM constraints.
        """
        baseline = cset if cset is not None else ConstraintSet(name=config.project.name)
        source_identity = {
            "design": stable_hash(design.snapshot()),
            "timing_graph": stable_hash(tg.model_dump()),
            "constraint_set": stable_hash_cset(baseline),
            "config": stable_hash(config.model_dump()),
            "engine": "step27-candidate-v1",
        }
        # Legacy rules may enrich a timing graph with user values. Isolate all
        # rule inputs that could carry mutable state, so advisory inference
        # cannot change caller design/config/timing/UCM/ledger objects.
        working_design = design.model_copy(deep=True)
        working_tg = tg.model_copy(deep=True)
        working_config = config.model_copy(deep=True)
        scratch_cset = ConstraintSet(name=baseline.name)
        scratch_ledger = AssumptionLedger()
        report = self.run(
            working_design, working_tg, working_config, scratch_cset, scratch_ledger,
            run_ts=run_ts or _CANDIDATE_EPOCH,
        )
        # ``run`` materialized only private scratch state. The public advisory
        # report must never imply it added constraints to the caller's UCM.
        report.constraints_added = 0
        report.structural_facts = self._structural_facts(design, working_tg)

        candidates: list[InferenceCandidate] = []
        for constraint in scratch_cset:
            candidates.append(self._candidate_from_constraint(
                constraint, baseline, design, source_identity,
            ))
        candidates.extend(self._candidates_from_missing(report, source_identity))
        candidates.extend(self._candidates_from_ambiguities(report, source_identity))
        candidates.extend(self._clock_port_ambiguity_candidates(design, working_tg, report, source_identity))
        # Merge duplicate advisory findings while retaining every independent
        # missing-information record on the surviving deterministic candidate.
        unique: dict[str, InferenceCandidate] = {}
        for candidate in sorted(candidates, key=lambda item: item.id):
            unique.setdefault(candidate.id, candidate)
        candidates = list(unique.values())
        if knowledge is not None:
            candidates = self._attach_knowledge(candidates, knowledge, report)
        report.candidates = sorted(candidates, key=lambda item: item.id)
        report.structural_facts = sorted(report.structural_facts, key=lambda item: item["id"])
        report.missing_information.sort(key=lambda item: (item.get("category", ""), item.get("object", ""),
                                                           item.get("id", "")))
        report.ambiguities.sort(key=lambda item: (str(item.get("object", "")), str(item.get("message", ""))))
        report.conflicts.sort(key=lambda item: (str(item.get("subject", "")), str(item.get("message", ""))))
        return report

    @staticmethod
    def _structural_facts(design: Design, tg: TimingGraph) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        for name, clock in sorted(tg.clocks.items()):
            registers = sorted(clock.registers_driven)
            facts.append({
                "id": "FACT-CLK-" + stable_hash((name, tuple(registers), clock.edge.value))[:16],
                "category": "structural_clock_role",
                "analysis": "timing_graph",
                "object": name,
                "source_objects": [name],
                "clock_refs": [name],
                "register_refs": registers,
                "statement": f"'{name}' drives {len(registers)} register(s) via {clock.edge.value} sensitivity.",
                "evidence_strength": "STRONG" if registers else "WEAK",
            })
        for port in sorted(design.top_ports(), key=lambda item: item.local_name):
            if port.direction.value not in {"input", "output"}:
                continue
            facts.append({
                "id": "FACT-PORT-" + stable_hash((port.local_name, port.direction.value, port.width))[:16],
                "category": "top_level_interface",
                "analysis": "design_model",
                "object": port.local_name,
                "source_objects": [port.local_name],
                "port_refs": [port.local_name],
                "statement": f"'{port.local_name}' is a top-level {port.direction.value} port (width {port.width}).",
                "evidence_strength": "STRONG",
            })
        for edge in sorted(tg.domain_edges, key=lambda item: (item.clock_a, item.clock_b)):
            if not edge.cdc_paths_observed:
                continue
            facts.append({
                "id": "FACT-CDC-" + stable_hash((edge.clock_a, edge.clock_b, edge.cdc_paths_observed))[:16],
                "category": "structural_cdc_observation",
                "analysis": "timing_graph",
                "object": f"{edge.clock_a}<->{edge.clock_b}",
                "source_objects": [edge.clock_a, edge.clock_b],
                "clock_refs": [edge.clock_a, edge.clock_b],
                "statement": (f"{edge.cdc_paths_observed} structural CDC path(s) observed between "
                              f"'{edge.clock_a}' and '{edge.clock_b}'; relationship remains unproven."),
                "evidence_strength": "MODERATE",
            })
        return facts

    @staticmethod
    def _candidate_id(kind: str, constraint_type: str | None, rule_ids: tuple[str, ...],
                      source_objects: tuple[str, ...], source_identity: dict[str, str],
                      template: Constraint | None = None, missing_id: str | None = None) -> str:
        payload: dict[str, Any] = {
            "kind": kind,
            "constraint_type": constraint_type,
            "rule_ids": rule_ids,
            "source_objects": source_objects,
            "design": source_identity["design"],
            "timing_graph": source_identity["timing_graph"],
            "constraint_set": source_identity["constraint_set"],
            "missing_id": missing_id,
        }
        if template is not None:
            payload["semantic"] = normalize_constraint(template)
        return "IC-" + stable_hash(payload)[:20]

    def _candidate_from_constraint(self, constraint: Constraint, baseline: ConstraintSet, design: Design,
                                   source_identity: dict[str, str]) -> InferenceCandidate:
        unsupported = has_unsupported_options(constraint)
        rule_ids = tuple(sorted(filter(None, (constraint.provenance.rule_id or "").split(","))))
        source_objects = set(constraint.target_objects) | set(constraint.source_objects) | set(constraint.through_objects)
        for evidence in constraint.provenance.evidence:
            source_objects.update(evidence.source_objects)
        if constraint.path_selector:
            source_objects.update(constraint.path_selector.all_objects())
        ports = {port.local_name for port in design.top_ports()}
        register_names = set(design.registers)
        exact: list[str] = []
        conflicts: list[str] = []
        if not unsupported:
            normalized = normalize_constraint(constraint)
            match = semantic_match_key(constraint)
            for existing in baseline:
                if has_unsupported_options(existing):
                    continue
                if normalize_constraint(existing) == normalized:
                    exact.append(existing.id)
                elif semantic_match_key(existing) == match:
                    conflicts.append(existing.id)
        warnings: list[str] = []
        status = InferenceStatus.INFERRED
        decision = InferenceDecision.ACCEPTABLE
        if unsupported:
            status = InferenceStatus.UNSUPPORTED
            decision = InferenceDecision.REJECTED
            warnings.append("Unsupported or unresolved UCM semantics: " + "; ".join(sorted(unsupported)))
        elif conflicts:
            status = InferenceStatus.CONFLICTING
            decision = InferenceDecision.REJECTED
            warnings.append("Existing UCM has the same semantic scope with different intent; nothing was overwritten.")
        elif constraint.source_kind in {SourceKind.USER, SourceKind.EXISTING_SDC}:
            decision = InferenceDecision.ALREADY_PRESENT
            warnings.append("This reflects explicit existing user/imported intent, not new inferred timing intent.")
        elif exact:
            decision = InferenceDecision.ALREADY_PRESENT
            warnings.append("Semantically equivalent UCM intent is already present; no acceptance is needed.")
        ordered_sources = tuple(sorted(source_objects))
        candidate_id = self._candidate_id(
            constraint.type.value, constraint.type.value, rule_ids, ordered_sources, source_identity, constraint,
        )
        return InferenceCandidate(
            id=candidate_id,
            kind=constraint.type.value,
            constraint_type=constraint.type.value,
            status=status,
            decision=decision,
            rule_ids=rule_ids,
            analysis="existing_inference_rules",
            source_objects=ordered_sources,
            clock_refs=tuple(sorted(set(constraint.clock_refs))),
            port_refs=tuple(sorted(source_objects & ports)),
            register_refs=tuple(sorted(source_objects & register_names)),
            evidence=tuple(sorted(constraint.provenance.evidence, key=lambda item: item.id)),
            provenance=copy.deepcopy(constraint.provenance),
            rationale=constraint.provenance.explanation or f"Existing inference proposed {constraint.type.value}.",
            source_snapshot_identity=dict(source_identity),
            candidate_semantic_identity=stable_hash((
                (normalize_constraint(constraint), stable_hash(constraint.to_canonical_dict())),
            )),
            constraint_template=constraint.to_canonical_dict(),
            assumptions=tuple(sorted(set(constraint.assumption_ids) | set(constraint.provenance.assumption_ids))),
            warnings=tuple(sorted(warnings)),
            existing_constraint_ids=tuple(sorted(exact or conflicts)),
        )

    def _candidates_from_missing(self, report: InferenceReport,
                                 source_identity: dict[str, str]) -> list[InferenceCandidate]:
        output: list[InferenceCandidate] = []
        for missing in sorted(report.missing_information, key=lambda item: item.get("id", "")):
            category = str(missing.get("category", "unknown"))
            object_name = str(missing.get("object", ""))
            possible = tuple(sorted(str(item) for item in missing.get("possible_values", []) if item is not None))
            level = str(missing.get("requirement_level", ""))
            text = str(missing.get("message", ""))
            if possible and len(possible) > 1:
                status = InferenceStatus.AMBIGUOUS
            elif level == RequirementLevel.UNSAFE_TO_INFER.value:
                status = InferenceStatus.CONFIRMATION_REQUIRED
            else:
                status = InferenceStatus.INSUFFICIENT_EVIDENCE
            decision = (InferenceDecision.REQUIRES_CONFIRMATION
                        if status == InferenceStatus.CONFIRMATION_REQUIRED else InferenceDecision.REJECTED)
            constraint_type = _constraint_type_for_missing(category)
            kind = constraint_type or category
            rule_id = str(missing.get("rule_id") or "TIMING-GRAPH")
            evidence = _missing_evidence(missing, rule_id)
            source_objects = tuple(sorted({object_name, *possible} - {""}))
            provenance = ProvenanceRecord(
                created_by=rule_id,
                created_at=_CANDIDATE_EPOCH,
                source_kind=SourceKind.INFERENCE,
                rule_id=rule_id,
                evidence=list(evidence),
                explanation=str(missing.get("rationale") or text),
                confidence=Confidence.LOW if status != InferenceStatus.INSUFFICIENT_EVIDENCE else Confidence.MEDIUM,
            )
            candidate_id = self._candidate_id(kind, constraint_type, (rule_id,), source_objects,
                                              source_identity, missing_id=str(missing.get("id", "")))
            output.append(InferenceCandidate(
                id=candidate_id,
                kind=kind,
                constraint_type=constraint_type,
                status=status,
                decision=decision,
                rule_ids=(rule_id,),
                analysis="timing_graph_missing_information",
                source_objects=source_objects,
                clock_refs=possible if category in {"clock_period", "clock_relationship"} else (),
                port_refs=(object_name,) if category.startswith(("io_", "input_", "output_")) else (),
                register_refs=(),
                evidence=evidence,
                provenance=provenance,
                rationale=str(missing.get("rationale") or text),
                source_snapshot_identity=dict(source_identity),
                assumptions=(),
                warnings=("Numeric or semantic timing intent was not invented.",),
                missing_information=(dict(missing),),
            ))
        return output

    def _candidates_from_ambiguities(self, report: InferenceReport,
                                     source_identity: dict[str, str]) -> list[InferenceCandidate]:
        output: list[InferenceCandidate] = []
        for ambiguity in sorted(report.ambiguities, key=lambda item: (str(item.get("object", "")),
                                                                       str(item.get("message", "")))):
            object_name = str(ambiguity.get("object", ""))
            message = str(ambiguity.get("message", "Ambiguous inference."))
            rule_id = str(ambiguity.get("rule_id") or "INFERENCE-AMBIGUITY")
            evidence = (Evidence(
                id="ev_" + stable_hash((rule_id, object_name, message))[:12],
                kind="naming_hint",
                description=message,
                source_objects=[object_name] if object_name else [],
                confidence=Confidence.LOW,
                rule_id=rule_id,
                created_by="rca.inference",
                created_at=_CANDIDATE_EPOCH,
            ),)
            source_objects = tuple(sorted({object_name} - {""}))
            candidate_id = self._candidate_id("create_clock", ConstraintType.CREATE_CLOCK.value, (rule_id,),
                                              source_objects, source_identity, missing_id=message)
            output.append(InferenceCandidate(
                id=candidate_id,
                kind="create_clock",
                constraint_type=ConstraintType.CREATE_CLOCK.value,
                status=InferenceStatus.AMBIGUOUS,
                decision=InferenceDecision.REJECTED,
                rule_ids=(rule_id,),
                analysis="clock_name_heuristic",
                source_objects=source_objects,
                clock_refs=(),
                port_refs=source_objects,
                register_refs=(),
                evidence=evidence,
                provenance=ProvenanceRecord(
                    created_by=rule_id, created_at=_CANDIDATE_EPOCH, source_kind=SourceKind.INFERENCE,
                    rule_id=rule_id, evidence=list(evidence), explanation=message, confidence=Confidence.LOW,
                ),
                rationale=message,
                source_snapshot_identity=dict(source_identity),
                warnings=("Name-only evidence is never enough to infer a clock or period.",),
            ))
        return output

    def _clock_port_ambiguity_candidates(self, design: Design, tg: TimingGraph, report: InferenceReport,
                                         source_identity: dict[str, str]) -> list[InferenceCandidate]:
        top_ports = {port.local_name for port in design.top_ports()}
        clock_ports = tuple(sorted(name for name, clock in tg.clocks.items()
                                   if (clock.source_object or name).split(".")[-1] in top_ports))
        if len(clock_ports) < 2:
            return []
        missing = {
            "id": "REQ-PRIMARY-CLOCK-PORTS-" + stable_hash(clock_ports)[:16],
            "category": "primary_clock_ports",
            "object": ", ".join(clock_ports),
            "severity": "WARNING",
            "requirement_level": RequirementLevel.RECOMMENDED.value,
            "message": "Multiple top-level ports have structural clock roles; confirm each clock intent and period.",
            "rationale": "Structural evidence identifies multiple clock roles but does not select, relate, or time them.",
            "evidence": [{"kind": "structural", "possible_clock_ports": list(clock_ports)}],
            "suggested_inputs": [{"field": "clock definitions", "options": list(clock_ports)}],
            "blocking": False,
            "rule_id": "CLK-001",
            "possible_values": list(clock_ports),
        }
        if not any(item.get("id") == missing["id"] for item in report.missing_information):
            report.missing_information.append(missing)
        evidence = _missing_evidence(missing, "CLK-001")
        candidate_id = self._candidate_id("create_clock", ConstraintType.CREATE_CLOCK.value, ("CLK-001",),
                                          clock_ports, source_identity, missing_id=missing["id"])
        return [InferenceCandidate(
            id=candidate_id,
            kind="create_clock",
            constraint_type=ConstraintType.CREATE_CLOCK.value,
            status=InferenceStatus.AMBIGUOUS,
            decision=InferenceDecision.REJECTED,
            rule_ids=("CLK-001",),
            analysis="timing_graph_clock_port_roles",
            source_objects=clock_ports,
            clock_refs=clock_ports,
            port_refs=clock_ports,
            register_refs=(),
            evidence=evidence,
            provenance=ProvenanceRecord(
                created_by="CLK-001", created_at=_CANDIDATE_EPOCH, source_kind=SourceKind.INFERENCE,
                rule_id="CLK-001", evidence=list(evidence), explanation=missing["rationale"],
                confidence=Confidence.MEDIUM,
            ),
            rationale=missing["rationale"],
            source_snapshot_identity=dict(source_identity),
            warnings=("Display ordering does not choose a primary clock port.",),
            missing_information=(missing,),
        )]

    def _attach_knowledge(self, candidates: list[InferenceCandidate], knowledge: Any,
                          report: InferenceReport) -> list[InferenceCandidate]:
        """Attach Step-26 advisory references without letting knowledge infer intent."""
        output: list[InferenceCandidate] = []
        try:
            from ..search import KnowledgeOrigin, TrustLevel
        except ImportError:  # pragma: no cover - package is part of this distribution
            return candidates
        allowed = {KnowledgeOrigin.BUILTIN, KnowledgeOrigin.HISTORY, KnowledgeOrigin.USER_FILE}
        for candidate in candidates:
            suggestions = []
            try:
                if candidate.constraint_template is not None:
                    template = Constraint.from_canonical_dict(copy.deepcopy(candidate.constraint_template),
                                                              unknown_field_policy="error")
                    suggestions = knowledge.search(constraint=template, include_origins=allowed, limit=5).suggestions
                else:
                    query = " ".join((candidate.kind, candidate.constraint_type or "", candidate.rationale))
                    suggestions = knowledge.search(text=query, include_origins=allowed, limit=5).suggestions
            except (AttributeError, TypeError, ValueError):
                output.append(candidate)
                continue
            references: list[dict[str, Any]] = []
            disagreement = False
            for suggestion in suggestions:
                ref = {
                    "knowledge_item_id": suggestion.knowledge_item_id,
                    "suggestion_id": suggestion.id,
                    "relevance": suggestion.relevance,
                    "match_kind": suggestion.match_kind,
                    "applicability": suggestion.applicability.value,
                    "trust_level": suggestion.trust_level.value,
                    "rationale": list(suggestion.rationale),
                    "evidence": [item.to_dict() for item in suggestion.evidence],
                }
                references.append(ref)
                if suggestion.match_kind == "same_scope_different_values":
                    disagreement = True
                    report.conflicts.append({
                        "message": "Knowledge and structural inference have same scope but different values; both retained.",
                        "subject": candidate.id,
                        "knowledge_item_id": suggestion.knowledge_item_id,
                    })
                if (suggestion.match_kind == "semantic_exact"
                        and suggestion.trust_level in {TrustLevel.VALIDATED, TrustLevel.VERIFIED}
                        and suggestion.applicability.value == "APPLICABLE"):
                    report.knowledge_references.append({
                        "constraint_id": candidate.id,
                        "knowledge_item_id": suggestion.knowledge_item_id,
                        "suggestion_id": suggestion.id,
                        "match_kind": suggestion.match_kind,
                        "trust_level": suggestion.trust_level.value,
                    })
            if disagreement:
                output.append(replace(
                    candidate,
                    status=InferenceStatus.CONFLICTING,
                    decision=InferenceDecision.REJECTED,
                    knowledge_references=tuple(references),
                    warnings=tuple(sorted(set(candidate.warnings) | {
                        "Knowledge disagreement retained; it did not establish or overwrite timing intent."
                    })),
                ))
            else:
                output.append(replace(candidate, knowledge_references=tuple(references)))
        report.knowledge_references.sort(key=lambda item: (
            item["constraint_id"], item["knowledge_item_id"], item["suggestion_id"]
        ))
        return output

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
                values={"groups": groups, "relationship": slot["values"].get("relationship", "asynchronous")},
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
                except (AttributeError, TypeError, ValueError):
                    log.warning("Ignoring unparsable user clock uncertainty for %s", c.name)
            out.append(info)
        return out

    def accept_candidate(
        self,
        candidate: InferenceCandidate,
        cset: ConstraintSet,
        design: Design | None = None,
        tg: TimingGraph | None = None,
    ) -> InferenceAcceptanceResult:
        """Backward-compatible Step-27 acceptance adapter.

        Step 28 centralizes every candidate-to-UCM transition in
        :func:`apply_intent_decision`. This legacy return shape is retained for
        callers that have not yet adopted the richer application receipt.
        """
        from .application import (
            ApplicationStatus,
            ConstraintApplication,
            IntentDecision,
            IntentDecisionKind,
            apply_intent_decision,
        )

        decision = IntentDecision(candidate_id=candidate.id, kind=IntentDecisionKind.ACCEPT)
        application = ConstraintApplication(candidate=candidate, decision=decision)
        outcome = apply_intent_decision(
            application, cset, design, tg, _retry_duplicates_before_stale=False,
        )
        status_map = {
            ApplicationStatus.APPLIED: "ACCEPTED",
            ApplicationStatus.ALREADY_PRESENT: "DUPLICATE",
        }
        return InferenceAcceptanceResult(
            status_map.get(outcome.status, "REJECTED"),
            candidate.id,
            constraint_id=outcome.applied_constraint_ids[0] if outcome.applied_constraint_ids else None,
            duplicate_of=outcome.already_present_ids[0] if outcome.already_present_ids else None,
            conflict_ids=outcome.conflict_ids,
            warnings=(*outcome.blocking_reasons, *outcome.warnings),
            validation_issues=outcome.validation_issues,
        )


def accept_inference_candidate(
    candidate: InferenceCandidate,
    cset: ConstraintSet,
    design: Design | None = None,
    tg: TimingGraph | None = None,
) -> InferenceAcceptanceResult:
    """Explicit acceptance convenience API using the existing engine checks."""
    return InferenceEngine().accept_candidate(candidate, cset, design=design, tg=tg)


def _constraint_type_for_missing(category: str) -> str | None:
    """Map missing-data categories to a possible UCM kind, never a value."""
    categories = {
        "clock_period": ConstraintType.CREATE_CLOCK.value,
        "clock_source": ConstraintType.CREATE_CLOCK.value,
        "generated_clock": ConstraintType.CREATE_GENERATED_CLOCK.value,
        "generated_clock_source": ConstraintType.CREATE_GENERATED_CLOCK.value,
        "generated_clock_ratio": ConstraintType.CREATE_GENERATED_CLOCK.value,
        "generated_clock_master": ConstraintType.CREATE_GENERATED_CLOCK.value,
        "io_input_delay": ConstraintType.SET_INPUT_DELAY.value,
        "io_output_delay": ConstraintType.SET_OUTPUT_DELAY.value,
        "input_delay": ConstraintType.SET_INPUT_DELAY.value,
        "output_delay": ConstraintType.SET_OUTPUT_DELAY.value,
        "input_clock_association": ConstraintType.SET_INPUT_DELAY.value,
        "output_clock_association": ConstraintType.SET_OUTPUT_DELAY.value,
        "generated_clock_intent": ConstraintType.CREATE_GENERATED_CLOCK.value,
        "gated_clock_intent": ConstraintType.CREATE_GENERATED_CLOCK.value,
        "clock_mux_intent": ConstraintType.CREATE_GENERATED_CLOCK.value,
        "clock_relationship": ConstraintType.SET_CLOCK_GROUPS.value,
    }
    return categories.get(category)


def _missing_evidence(missing: dict[str, Any], rule_id: str) -> tuple[Evidence, ...]:
    """Convert existing missing-information provenance into canonical evidence."""
    object_name = str(missing.get("object", ""))
    message = str(missing.get("message", "Missing information."))
    detail = {
        "missing_information_id": missing.get("id"),
        "category": missing.get("category"),
        "requirement_level": missing.get("requirement_level"),
        "possible_values": list(missing.get("possible_values", [])),
    }
    return (Evidence(
        id="ev_" + stable_hash((rule_id, object_name, message, detail))[:12],
        kind="rule",
        description=message,
        detail=detail,
        source_objects=[object_name] if object_name else [],
        confidence=Confidence.MEDIUM,
        rule_id=rule_id,
        created_by="rca.inference",
        created_at=_CANDIDATE_EPOCH,
    ),)
