"""
Clock inference rules.

CLK-001 — sequential_clock_candidate
    Structural detection. A net used as an edge-control signal
    (posedge/negedge) driving registers establishes that the signal
    plays a *clock role*. This produces a CANDIDATE but does NOT emit
    create_clock unless a period is known from USER or EXISTING_SDC.
    Without a period, a REQUIRED missing-information item is
    produced and emission is blocked.

CLK-002 — clock_name_hints
    Corroborating only. Names like clk/clock/sys_clk produce LOW
    heuristic evidence. They never alone create a clock, never raise
    confidence to HIGH, never set a period.

CLK-003 — user_clock_specification
    User-supplied clocks override inference. Period, uncertainty,
    fixed/tunable come from config. If the user names a clock with
    no structural match it is retained as USER (FIXED) and a warning
    records that structural confirmation was absent.
"""

from __future__ import annotations

from ..design_model import Design
from ..provenance import Evidence
from ..timing_model import Clock, TimingGraph
from ..utils.enums import (
    Confidence,
    InferenceResultStatus,
    RequirementLevel,
    SourceKind,
)
from ._evidence import make_evidence
from .rules import InferenceResult, MissingInformation, ProposedConstraint


def _mk(rule_id: str, kind: str, description: str,
        confidence: Confidence = Confidence.MEDIUM,
        source_objects: list[str] | None = None,
        created_at: str | None = None) -> Evidence:
    return make_evidence(rule_id, kind, description,
                         source_objects=source_objects,
                         confidence=confidence,
                         created_at=created_at)


def rule_clk_001_sequential(design: Design, tg: TimingGraph, *,
                            _run_ts: str | None = None, **kw) -> InferenceResult:
    rule_id = "CLK-001"
    res = InferenceResult(rule_id=rule_id, rule_name="sequential_clock_candidate",
                          confidence=Confidence.HIGH,
                          result_status=InferenceResultStatus.NO_FINDING)
    for name in sorted(tg.clocks.keys()):
        c = tg.clocks[name]
        n_regs = len(c.registers_driven)
        if c.source_of_value == "INFERENCE" and n_regs == 0:
            continue
        ev = _mk(rule_id, "structural",
                 f"Clock '{name}' drives {n_regs} register(s) via {c.edge.value} sensitivity.",
                 confidence=Confidence.HIGH, source_objects=[name], created_at=_run_ts)
        res.add_evidence(ev)

        if c.period_seconds is not None:
            src_kind = SourceKind.USER if c.source_of_value == "USER" else SourceKind.INFERENCE
            conf = Confidence.HIGH if c.source_of_value in ("USER", "EXISTING_SDC") else Confidence.MEDIUM
            status = "FIXED" if c.status == "FIXED" else ("CONFIRMED" if conf >= Confidence.HIGH else "PROPOSED")
            values: dict = {"name": name, "period": c.period_seconds}
            if c.waveform:
                values["waveform"] = list(c.waveform)
            all_ev: list[Evidence] = [ev]
            seen_keys = {(ev.kind, ev.description, tuple(sorted(ev.source_objects)))}
            for cev in c.evidence:
                kind = (cev.kind.value if hasattr(cev.kind, "value") else str(cev.kind))
                desc = cev.detail if hasattr(cev, "detail") and cev.detail else f"clock evidence: {kind}"
                econf = Confidence.HIGH if kind in ("user_declared",) else Confidence.MEDIUM
                nev = _mk(rule_id, kind, desc, confidence=econf,
                          source_objects=[name], created_at=_run_ts)
                key = (nev.kind, nev.description, tuple(sorted(nev.source_objects)))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                all_ev.append(nev)
            res.propose(ProposedConstraint(
                kind="create_clock", object=name, clock=name,
                period_seconds=c.period_seconds, values=values,
                confidence=conf, status=status,
                source_kind=src_kind.value, evidence=all_ev,
                rationale=f"Structural clock role with known period ({c.source_of_value}).",
                target_objects=[c.source_object or name],
                merge_key=("create_clock", name),
            ))
            if c.uncertainty_seconds is not None:
                res.propose(ProposedConstraint(
                    kind="set_clock_uncertainty", object=name, clock=name,
                    values={"uncertainty": c.uncertainty_seconds},
                    confidence=Confidence.HIGH if c.source_of_value == "USER" else Confidence.MEDIUM,
                    status=status, source_kind=src_kind.value, evidence=all_ev,
                    merge_key=("set_clock_uncertainty", name),
                ))
        else:
            res.add_missing(MissingInformation(
                id=f"REQ-CLK-PERIOD-{name}", category="clock_period", object=name,
                severity="ERROR", requirement_level=RequirementLevel.REQUIRED,
                message=f"Clock period required for '{name}'",
                rationale=(f"'{name}' is structurally a clock (drives {n_regs} register(s)) "
                           "but no period was provided. create_clock cannot be emitted."),
                evidence=[{"kind": "structural",
                           "description": f"{n_regs} register(s) clocked by {name}",
                           "source_objects": list(c.registers_driven)[:5]}],
                suggested_inputs=[{"field": "period", "format": "time string (e.g. 10ns)"}],
                blocking=True, rule_id=rule_id,
            ))
    if res.proposed_constraints:
        res.result_status = InferenceResultStatus.APPLIED
    elif res.missing_information:
        res.result_status = InferenceResultStatus.BLOCKED
    return res


def rule_clk_002_name_hints(design: Design, tg: TimingGraph, *,
                            _run_ts: str | None = None, **kw) -> InferenceResult:
    rule_id = "CLK-002"
    res = InferenceResult(rule_id=rule_id, rule_name="clock_name_hints",
                          confidence=Confidence.LOW,
                          result_status=InferenceResultStatus.NO_FINDING)
    clock_like = ("clk", "clock", "gclk", "sys_clk", "clk_", "mclk", "sclk", "pclk", "aclk")
    for port in sorted(design.top_ports(), key=lambda p: p.local_name):
        lname = port.local_name.lower()
        is_name_match = lname in clock_like or any(lname.startswith(h) for h in clock_like)
        if not is_name_match:
            continue
        if port.local_name in tg.clocks:
            ev = _mk(rule_id, "naming_hint",
                     f"Port name '{port.local_name}' matches clock naming convention.",
                     confidence=Confidence.LOW, source_objects=[port.local_name],
                     created_at=_run_ts)
            res.add_evidence(ev)
            continue
        ev = _mk(rule_id, "naming_hint",
                 f"Port '{port.local_name}' looks clock-like by name but has no structural support.",
                 confidence=Confidence.LOW, source_objects=[port.local_name],
                 created_at=_run_ts)
        res.add_evidence(ev)
        res.add_ambiguity(
            f"Port '{port.local_name}' resembles a clock by name only; no structural evidence. "
            "No create_clock will be emitted on name evidence alone.",
            object=port.local_name, severity="WARNING",
        )
    if res.ambiguities and res.result_status == InferenceResultStatus.NO_FINDING:
        res.result_status = InferenceResultStatus.REQUIRES_CONFIRMATION
    return res


def rule_clk_003_user_period(design: Design, tg: TimingGraph, user_clocks=None, *,
                             _run_ts: str | None = None, **kw) -> InferenceResult:
    rule_id = "CLK-003"
    res = InferenceResult(rule_id=rule_id, rule_name="user_clock_specification",
                          confidence=Confidence.HIGH,
                          result_status=InferenceResultStatus.NO_FINDING)
    for uc in user_clocks or []:
        name = uc.get("name")
        if not name:
            continue
        c = tg.clocks.get(name)
        ev_user = _mk(rule_id, "user",
                      f"User declared clock '{name}'"
                      + (f" with period {uc.get('period_seconds')}s"
                         if uc.get("period_seconds") is not None else ""),
                      confidence=Confidence.HIGH, source_objects=[name], created_at=_run_ts)
        res.add_evidence(ev_user)
        fixed = bool(uc.get("fixed", True))
        if uc.get("period_seconds") is not None:
            if c is not None:
                c.period_seconds = uc["period_seconds"]
                c.source_of_value = "USER"
                c.confidence = "HIGH"
                c.status = "FIXED" if fixed else "TUNABLE"
                if uc.get("uncertainty_seconds") is not None:
                    c.uncertainty_seconds = float(uc["uncertainty_seconds"])
                all_ev: list[Evidence] = [ev_user]
                seen = {(e.kind, e.description, tuple(sorted(e.source_objects))) for e in all_ev}
                for cev in c.evidence:
                    kind = cev.kind.value if hasattr(cev.kind, "value") else str(cev.kind)
                    desc = cev.detail if hasattr(cev, "detail") and cev.detail else f"clock evidence: {kind}"
                    econf = Confidence.HIGH if kind == "user_declared" else Confidence.MEDIUM
                    nev = _mk(rule_id, kind, desc, confidence=econf,
                              source_objects=[name], created_at=_run_ts)
                    key = (nev.kind, nev.description, tuple(sorted(nev.source_objects)))
                    if key in seen:
                        continue
                    seen.add(key)
                    all_ev.append(nev)
                values = {"name": name, "period": uc["period_seconds"]}
                if c.waveform:
                    values["waveform"] = list(c.waveform)
                if uc.get("uncertainty_seconds") is not None:
                    values["uncertainty"] = uc["uncertainty_seconds"]
                res.propose(ProposedConstraint(
                    kind="create_clock", object=name, clock=name,
                    period_seconds=uc["period_seconds"], values=values,
                    confidence=Confidence.HIGH,
                    status="FIXED" if fixed else "CONFIRMED",
                    source_kind=SourceKind.USER.value,
                    evidence=all_ev,
                    rationale="User-specified clock overrides inference.",
                    target_objects=[uc.get("port") or name],
                    merge_key=("create_clock", name),
                ))
                if uc.get("uncertainty_seconds") is not None:
                    res.propose(ProposedConstraint(
                        kind="set_clock_uncertainty", object=name, clock=name,
                        values={"uncertainty": uc["uncertainty_seconds"]},
                        confidence=Confidence.HIGH,
                        status="FIXED" if fixed else "CONFIRMED",
                        source_kind=SourceKind.USER.value,
                        evidence=[ev_user],
                        merge_key=("set_clock_uncertainty", name),
                    ))
            else:
                res.add_warning(
                    f"User-specified clock '{name}' not detected structurally; "
                    "retained as USER constraint without structural corroboration.",
                    object=name,
                )
                values = {"name": name, "period": uc["period_seconds"]}
                if uc.get("port"):
                    values["port"] = uc["port"]
                res.propose(ProposedConstraint(
                    kind="create_clock", object=name, clock=name,
                    period_seconds=uc["period_seconds"], values=values,
                    confidence=Confidence.HIGH,
                    status="FIXED" if fixed else "CONFIRMED",
                    source_kind=SourceKind.USER.value, evidence=[ev_user],
                    rationale=("User declared a clock with no structural match; "
                               "preserved as explicit user data."),
                    target_objects=[uc.get("port") or name],
                    merge_key=("create_clock", name),
                ))
        else:
            res.add_missing(MissingInformation(
                id=f"REQ-USER-CLK-PERIOD-{name}", category="clock_period",
                object=name, severity="ERROR",
                requirement_level=RequirementLevel.REQUIRED,
                message=f"Clock period required for user clock '{name}'",
                rationale="User specified the clock but did not provide a period.",
                evidence=[{"kind": "user", "description": "user clock declaration"}],
                suggested_inputs=[{"field": "period", "format": "time string (e.g. 10ns)"}],
                blocking=True, rule_id=rule_id,
            ))
    if res.proposed_constraints:
        res.result_status = InferenceResultStatus.APPLIED
    elif res.missing_information:
        res.result_status = InferenceResultStatus.BLOCKED
    return res
