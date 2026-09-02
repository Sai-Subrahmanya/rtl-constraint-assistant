"""
I/O timing inference rules (Manual §22, §24).

Policy:
* User-specified I/O delays take precedence (FIXED/USER).
* If the user gives a delay but no clock, resolve via structural
  association (tg.input_clock_assoc / fanout BFS).
* If multiple clocks are possible: REQUIRED ambiguity, nothing emitted.
* If zero clocks: REQUIRED missing info, nothing emitted.
* No numeric default delay is invented.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..design_model import Design
from ..timing_model import TimingGraph
from ..utils.enums import Confidence, InferenceResultStatus, RequirementLevel, SourceKind
from ..utils.units import parse_time_string
from ._evidence import make_evidence
from .rules import InferenceResult, MissingInformation, ProposedConstraint


def _ev(rid: str, kind: str, desc: str,
        confidence: Confidence = Confidence.MEDIUM,
        objs: list[str] | None = None,
        created_at: str | None = None):
    return make_evidence(rid, kind, desc, source_objects=objs,
                         confidence=confidence, created_at=created_at)


def _parse_delay_seconds(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(parse_time_string(str(v)))
    except Exception:
        return None


def _clock_set_for_input(design: Design, tg: TimingGraph, leaf: str) -> list[str]:
    g = getattr(design, "_structural_graph_internal", None)
    if g is None:
        for h, a in tg.input_clock_assoc.items():
            if h.split(".")[-1] == leaf:
                return [a] if a else []
        return []
    start = None
    for p in design.top_ports():
        if p.direction.value == "input" and p.local_name == leaf:
            start = p.hierarchical_name
            break
    if start is None:
        return []
    reg_by_d: dict[str, list[str]] = defaultdict(list)
    for r in design.registers.values():
        for ds in r.data_sources:
            reg_by_d[ds].append(r.hierarchical_name)
    visited = {start}; queue = [start]
    domains: dict[str, int] = defaultdict(int)
    fanout = g.data_fanout if hasattr(g, "data_fanout") else {}
    while queue:
        sig = queue.pop(0)
        if sig in reg_by_d:
            for rh in reg_by_d[sig]:
                r = design.registers.get(rh)
                if r and r.clock_signal:
                    domains[r.clock_signal.split(".")[-1]] += 1
        for nxt in sorted(fanout.get(sig, ())):
            if nxt in visited:
                continue
            visited.add(nxt); queue.append(nxt)
    return sorted(domains.keys())


def _clock_set_for_output(design: Design, tg: TimingGraph, leaf: str) -> list[str]:
    g = getattr(design, "_structural_graph_internal", None)
    if g is None:
        for h, a in tg.output_clock_assoc.items():
            if h.split(".")[-1] == leaf:
                return [a] if a else []
        return []
    end = None
    for p in design.top_ports():
        if p.direction.value == "output" and p.local_name == leaf:
            end = p.hierarchical_name
            break
    if end is None:
        return []
    fanin = g.data_fanin if hasattr(g, "data_fanin") else {}
    reg_q_names = {r.q_name(): r for r in design.registers.values()}
    visited = {end}; queue = [end]
    domains: dict[str, int] = defaultdict(int)
    while queue:
        sig = queue.pop(0)
        if sig in reg_q_names:
            r = reg_q_names[sig]
            if r.clock_signal:
                domains[r.clock_signal.split(".")[-1]] += 1
        for prev in sorted(fanin.get(sig, ())):
            if prev in visited:
                continue
            visited.add(prev); queue.append(prev)
    return sorted(domains.keys())


def rule_io_001_classify(design: Design, tg: TimingGraph, *,
                         _run_ts: str | None = None, **kw) -> InferenceResult:
    rid = "IO-001"
    res = InferenceResult(rule_id=rid, rule_name="io_port_classification",
                          confidence=Confidence.HIGH,
                          result_status=InferenceResultStatus.NO_FINDING)
    clock_names = {c.name for c in tg.clocks.values()}
    reset_names = set(tg.resets.keys())
    for p in sorted(design.top_ports(), key=lambda x: x.local_name):
        if p.local_name in clock_names or p.local_name in reset_names:
            continue
        role = "input" if p.direction.value == "input" else "output"
        res.add_evidence(_ev(
            rid, "structural",
            f"Port '{p.local_name}' is a top-level {role} (width {p.width}) requiring timing.",
            confidence=Confidence.HIGH, objs=[p.local_name], created_at=_run_ts,
        ))
    return res


def _handle_port(res, rid, kind, obj, user_delay, user_clock, possible_clocks,
                 delay_field_mi_id, clk_field_mi_id, created_at, user_fixed):
    """Shared logic for input/output delay inference."""
    delay_s = _parse_delay_seconds(user_delay)
    if delay_s is not None and user_clock:
        ev = _ev(rid, "user",
                 f"{kind} delay for '{obj}' specified by user "
                 f"({user_delay} relative to {user_clock}).",
                 confidence=Confidence.HIGH, objs=[obj], created_at=created_at)
        res.add_evidence(ev)
        res.propose(ProposedConstraint(
            kind=kind, object=obj, clock=user_clock, delay_seconds=delay_s,
            values={"clock": user_clock},
            confidence=Confidence.HIGH,
            status="FIXED" if user_fixed else "CONFIRMED",
            source_kind=SourceKind.USER.value, evidence=[ev],
            rationale=f"User-specified {kind}.",
            merge_key=(kind, obj),
        ))
        return
    if delay_s is not None and len(possible_clocks) == 1:
        clk = possible_clocks[0]
        ev = _ev(rid, "user",
                 f"{kind} for '{obj}' specified by user ({user_delay}); "
                 f"clock structurally resolved to '{clk}'.",
                 confidence=Confidence.HIGH, objs=[obj, clk], created_at=created_at)
        res.add_evidence(ev)
        res.propose(ProposedConstraint(
            kind=kind, object=obj, clock=clk, delay_seconds=delay_s,
            values={"clock": clk},
            confidence=Confidence.HIGH, status="CONFIRMED",
            source_kind=SourceKind.USER.value, evidence=[ev],
            rationale=f"User delay; clock resolved structurally.",
            merge_key=(kind, obj),
        ))
        return
    # Missing info cases
    if delay_s is None:
        if len(possible_clocks) == 1:
            res.add_missing(MissingInformation(
                id=f"{delay_field_mi_id}-{obj}",
                category="io_input_delay" if kind == "set_input_delay" else "io_output_delay",
                object=obj, severity="WARNING",
                requirement_level=RequirementLevel.RECOMMENDED,
                message=f"{kind.split('_')[1].title()} delay required for '{obj}'",
                rationale=(f"Port '{obj}' fans into/out of registers clocked by "
                           f"{possible_clocks[0]} but no delay was supplied."),
                evidence=[{"kind": "structural", "possible_clock": possible_clocks[0]}],
                suggested_inputs=[{"field": "delay", "format": "time string"},
                                   {"field": "clock", "value": possible_clocks[0]}],
                blocking=False, rule_id=rid, possible_values=[possible_clocks[0]],
            ))
        elif len(possible_clocks) > 1:
            res.add_missing(MissingInformation(
                id=f"{delay_field_mi_id}-{obj}",
                category="io_input_delay" if kind == "set_input_delay" else "io_output_delay",
                object=obj, severity="ERROR",
                requirement_level=RequirementLevel.REQUIRED,
                message=f"{kind.split('_')[1].title()} delay and clock association required for '{obj}'",
                rationale=(f"Port '{obj}' connects to multiple clock domains "
                           f"{possible_clocks}; association is ambiguous."),
                evidence=[{"kind": "structural", "possible_clocks": possible_clocks}],
                suggested_inputs=[{"field": "clock", "options": possible_clocks},
                                   {"field": "delay", "format": "time string"}],
                blocking=True, rule_id=rid, possible_values=list(possible_clocks),
            ))
        else:
            res.add_missing(MissingInformation(
                id=f"{delay_field_mi_id}-{obj}",
                category="io_input_delay" if kind == "set_input_delay" else "io_output_delay",
                object=obj, severity="ERROR",
                requirement_level=RequirementLevel.REQUIRED,
                message=f"{kind.split('_')[1].title()} delay and clock association required for '{obj}'",
                rationale=f"No structural clock association found for port '{obj}'.",
                evidence=[{"kind": "structural"}],
                suggested_inputs=[{"field": "clock"}, {"field": "delay", "format": "time string"}],
                blocking=True, rule_id=rid,
            ))
    else:
        if len(possible_clocks) == 0:
            res.add_missing(MissingInformation(
                id=f"{clk_field_mi_id}-{obj}",
                category=("input_clock_association" if kind == "set_input_delay"
                          else "output_clock_association"),
                object=obj, severity="ERROR",
                requirement_level=RequirementLevel.REQUIRED,
                message=f"Clock association required for {kind.split('_')[1]} '{obj}'",
                rationale=f"Delay given but no clock association is known.",
                evidence=[{"kind": "user", "description": f"delay={user_delay} but clock unspecified"}],
                suggested_inputs=[{"field": "clock"}],
                blocking=True, rule_id=rid,
            ))
        elif len(possible_clocks) > 1:
            res.add_missing(MissingInformation(
                id=f"{clk_field_mi_id}-{obj}",
                category=("input_clock_association" if kind == "set_input_delay"
                          else "output_clock_association"),
                object=obj, severity="ERROR",
                requirement_level=RequirementLevel.REQUIRED,
                message=f"Ambiguous clock association for {kind.split('_')[1]} '{obj}'",
                rationale=f"Possible clocks: {possible_clocks}.",
                evidence=[{"kind": "structural", "possible_clocks": possible_clocks}],
                suggested_inputs=[{"field": "clock", "options": possible_clocks}],
                blocking=True, rule_id=rid, possible_values=list(possible_clocks),
            ))


def rule_io_002_missing_input_delay(design: Design, tg: TimingGraph,
                                    user_io: dict | None = None, *,
                                    _run_ts: str | None = None, **kw) -> InferenceResult:
    rid = "IO-002"
    res = InferenceResult(rule_id=rid, rule_name="missing_input_delay",
                          confidence=Confidence.HIGH,
                          result_status=InferenceResultStatus.NO_FINDING)
    user_inputs = (user_io or {}).get("inputs", {}) or {}
    clock_names = {c.name for c in tg.clocks.values()}
    reset_names = set(tg.resets.keys())
    for p in sorted(design.top_ports(), key=lambda x: x.local_name):
        if p.direction.value != "input":
            continue
        if p.local_name in clock_names or p.local_name in reset_names:
            continue
        spec = user_inputs.get(p.local_name) or {}
        user_clock = spec.get("clock") if isinstance(spec, dict) else None
        user_delay = spec.get("delay") if isinstance(spec, dict) else None
        possible = _clock_set_for_input(design, tg, p.local_name) if not user_clock else [user_clock]
        _handle_port(res, rid, "set_input_delay", p.local_name,
                     user_delay=user_delay, user_clock=user_clock,
                     possible_clocks=possible,
                     delay_field_mi_id="REQ-IN-DELAY",
                     clk_field_mi_id="REQ-IN-CLK",
                     created_at=_run_ts,
                     user_fixed=bool(spec.get("fixed", True)))
    if res.proposed_constraints:
        res.result_status = InferenceResultStatus.APPLIED
    elif res.missing_information:
        res.result_status = (InferenceResultStatus.BLOCKED
                              if any(mi.blocking for mi in res.missing_information)
                              else InferenceResultStatus.PROPOSED)
    return res


def rule_io_003_missing_output_delay(design: Design, tg: TimingGraph,
                                     user_io: dict | None = None, *,
                                     _run_ts: str | None = None, **kw) -> InferenceResult:
    rid = "IO-003"
    res = InferenceResult(rule_id=rid, rule_name="missing_output_delay",
                          confidence=Confidence.HIGH,
                          result_status=InferenceResultStatus.NO_FINDING)
    user_outputs = (user_io or {}).get("outputs", {}) or {}
    clock_names = {c.name for c in tg.clocks.values()}
    reset_names = set(tg.resets.keys())
    for p in sorted(design.top_ports(), key=lambda x: x.local_name):
        if p.direction.value != "output":
            continue
        if p.local_name in clock_names or p.local_name in reset_names:
            continue
        spec = user_outputs.get(p.local_name) or {}
        user_clock = spec.get("clock") if isinstance(spec, dict) else None
        user_delay = spec.get("delay") if isinstance(spec, dict) else None
        possible = _clock_set_for_output(design, tg, p.local_name) if not user_clock else [user_clock]
        _handle_port(res, rid, "set_output_delay", p.local_name,
                     user_delay=user_delay, user_clock=user_clock,
                     possible_clocks=possible,
                     delay_field_mi_id="REQ-OUT-DELAY",
                     clk_field_mi_id="REQ-OUT-CLK",
                     created_at=_run_ts,
                     user_fixed=bool(spec.get("fixed", True)))
    if res.proposed_constraints:
        res.result_status = InferenceResultStatus.APPLIED
    elif res.missing_information:
        res.result_status = (InferenceResultStatus.BLOCKED
                              if any(mi.blocking for mi in res.missing_information)
                              else InferenceResultStatus.PROPOSED)
    return res
