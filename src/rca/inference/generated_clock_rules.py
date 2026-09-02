"""
Generated-clock / gating / mux rules (Manual §19, §20, §21).

These rules are candidate detectors only. They do NOT emit
``create_generated_clock``/``set_clock_groups`` automatically:
confirming generated-clock intent, gating intent, or mux exclusivity
is high-risk timing intent that requires user confirmation.
"""

from __future__ import annotations

from ..design_model import Design
from ..timing_model import TimingGraph
from ..utils.enums import Confidence, InferenceResultStatus, RequirementLevel
from ._evidence import make_evidence
from .rules import InferenceResult, MissingInformation


def _ev(rid: str, kind: str, desc: str,
        confidence: Confidence = Confidence.LOW,
        objs: list[str] | None = None,
        created_at: str | None = None):
    return make_evidence(rid, kind, desc, source_objects=objs,
                         confidence=confidence, created_at=created_at)


def rule_gclk_001_divider_candidate(design: Design, tg: TimingGraph, *,
                                    _run_ts: str | None = None, **kw) -> InferenceResult:
    rid = "GCLK-001"
    res = InferenceResult(rule_id=rid, rule_name="clock_divider_candidate",
                          confidence=Confidence.LOW,
                          result_status=InferenceResultStatus.NO_FINDING)
    for gc in tg.generated_clock_candidates:
        out = gc.get("output", "?")
        master = gc.get("master_clock")
        detail = gc.get("detail", "")
        objs = [out, master] if master else [out]
        ev = _ev(rid, "structural",
                 f"Possible generated clock at '{out}'"
                 + (f" derived from master '{master}'" if master else "")
                 + (f": {detail}" if detail else "."),
                 confidence=Confidence.LOW, objs=objs, created_at=_run_ts)
        res.add_evidence(ev)
        res.add_missing(MissingInformation(
            id=f"REQ-GCLK-{out}",
            category="generated_clock_intent", object=out,
            severity="WARNING", requirement_level=RequirementLevel.UNSAFE_TO_INFER,
            message=f"Generated-clock intent confirmation required for '{out}'",
            rationale=("A divider-like structure is present but automatically inferring "
                       "create_generated_clock is high-risk; divide factor, master clock, "
                       "and edge alignment require confirmation."),
            evidence=[{"kind": "structural", "master": master, "detail": detail}],
            suggested_inputs=[{"field": "intent"}, {"field": "master_clock", "value": master},
                               {"field": "divide_by"}],
            blocking=False, rule_id=rid,
        ))
    if res.missing_information:
        res.result_status = InferenceResultStatus.REQUIRES_CONFIRMATION
    return res


def rule_gclk_002_gated_clock_candidate(design: Design, tg: TimingGraph, *,
                                        _run_ts: str | None = None, **kw) -> InferenceResult:
    rid = "GCLK-002"
    res = InferenceResult(rule_id=rid, rule_name="gated_clock_candidate",
                          confidence=Confidence.LOW,
                          result_status=InferenceResultStatus.NO_FINDING)
    for cg in tg.clock_gating_candidates:
        out = cg.get("output", "?")
        clk = cg.get("clock"); en = cg.get("enable"); gate = cg.get("gate_type")
        ev = _ev(rid, "structural",
                 f"Possible gated clock at '{out}' (gate={gate}, clk={clk}, en={en}).",
                 confidence=Confidence.LOW, objs=[out], created_at=_run_ts)
        res.add_evidence(ev)
        res.add_missing(MissingInformation(
            id=f"REQ-GCLK-GATE-{out}",
            category="gated_clock_intent", object=out,
            severity="WARNING", requirement_level=RequirementLevel.UNSAFE_TO_INFER,
            message=f"Clock-gating intent confirmation required for '{out}'",
            rationale=("A combinational gate between clock and register clock pins was found; "
                       "automatically emitting clock gating is unsafe without glitch-safe confirmation."),
            evidence=[{"kind": "structural", "clock": clk, "enable": en, "gate": gate}],
            suggested_inputs=[{"field": "intent"}, {"field": "setup_check"}],
            blocking=False, rule_id=rid,
        ))
    if res.missing_information:
        res.result_status = InferenceResultStatus.REQUIRES_CONFIRMATION
    return res


def rule_gclk_003_mux_candidate(design: Design, tg: TimingGraph, *,
                                _run_ts: str | None = None, **kw) -> InferenceResult:
    rid = "GCLK-003"
    res = InferenceResult(rule_id=rid, rule_name="clock_mux_candidate",
                          confidence=Confidence.LOW,
                          result_status=InferenceResultStatus.NO_FINDING)
    for cm in tg.clock_mux_candidates:
        out = cm.get("output", "?")
        sources = cm.get("sources", []); sel = cm.get("select")
        ev = _ev(rid, "structural",
                 f"Possible clock mux at '{out}' between {sources} (sel={sel}).",
                 confidence=Confidence.LOW, objs=[out], created_at=_run_ts)
        res.add_evidence(ev)
        res.add_missing(MissingInformation(
            id=f"REQ-CLK-MUX-{out}",
            category="clock_mux_intent", object=out,
            severity="WARNING", requirement_level=RequirementLevel.UNSAFE_TO_INFER,
            message=f"Clock-mux exclusivity/mode confirmation required for '{out}'",
            rationale=("Multiple clock sources fan into a mux-like structure feeding a clock "
                       "pin; exclusivity is never inferred automatically."),
            evidence=[{"kind": "structural", "sources": sources, "select": sel}],
            suggested_inputs=[{"field": "mode"}, {"field": "active_scenario"}],
            blocking=False, rule_id=rid, possible_values=list(sources),
        ))
    if res.missing_information:
        res.result_status = InferenceResultStatus.REQUIRES_CONFIRMATION
    return res
