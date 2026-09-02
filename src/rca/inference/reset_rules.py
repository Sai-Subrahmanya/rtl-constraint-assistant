"""
Reset inference rules (Manual §14, §121).

RST-001 async_reset_from_sensitivity
    Structural detection only: when a register's sensitivity list
    includes the reset signal AND it branches to a constant load
    BEFORE checking the clock edge, it's asynchronous. Recorded; no
    false_path/clock_groups emitted.

RST-002 sync_reset_detection
    Reset-like signal that is NOT in the sensitivity list but gates a
    constant-0/1 assignment inside a clocked always_ff is a candidate
    sync reset. Flagged as REQUIRES_CONFIRMATION, not emitted.

RST-003 adversarial_reset_name_usage
    Reset-named signal feeding data inputs rather than just reset
    pins is flagged to prevent false reset inference.
"""

from __future__ import annotations

from ..design_model import Design
from ..timing_model import TimingGraph
from ..utils.enums import Confidence, InferenceResultStatus, RequirementLevel
from ._evidence import make_evidence
from .rules import InferenceResult, ProposedConstraint


def _ev(rid: str, kind: str, desc: str,
        confidence: Confidence = Confidence.MEDIUM,
        objs: list[str] | None = None,
        created_at: str | None = None):
    return make_evidence(rid, kind, desc, source_objects=objs,
                         confidence=confidence, created_at=created_at)


def rule_rst_001_async_sensitivity(design: Design, tg: TimingGraph, *,
                                   _run_ts: str | None = None, **kw) -> InferenceResult:
    rid = "RST-001"
    res = InferenceResult(rule_id=rid, rule_name="async_reset_from_sensitivity",
                          confidence=Confidence.HIGH,
                          result_status=InferenceResultStatus.NO_FINDING)
    for name in sorted(tg.resets.keys()):
        rst = tg.resets[name]
        if rst.reset_type.value != "asynchronous":
            continue
        ev = _ev(rid, "structural",
                 f"Reset '{name}' appears in sensitivity list with "
                 f"{rst.edge.value if rst.edge else 'unknown'} edge.",
                 confidence=Confidence.HIGH, objs=[name], created_at=_run_ts)
        res.add_evidence(ev)
        res.propose(ProposedConstraint(
            kind="reset_detection", object=name,
            values={"reset_type": "asynchronous",
                    "polarity": rst.polarity.value,
                    "associated_clock": rst.associated_clock},
            confidence=Confidence.HIGH, status="CONFIRMED",
            source_kind="INFERENCE", evidence=[ev],
            rationale=("Async reset structurally identified (sensitivity list); "
                       "no timing exception is implied by reset detection alone."),
            merge_key=("reset_detection", name),
        ))
    if res.proposed_constraints:
        res.result_status = InferenceResultStatus.APPLIED
    return res


def rule_rst_002_sync_reset(design: Design, tg: TimingGraph, *,
                            _run_ts: str | None = None, **kw) -> InferenceResult:
    rid = "RST-002"
    res = InferenceResult(rule_id=rid, rule_name="synchronous_reset_detection",
                          confidence=Confidence.MEDIUM,
                          result_status=InferenceResultStatus.NO_FINDING)
    candidates: dict[str, int] = {}
    for reg in sorted(design.registers.values(), key=lambda r: r.hierarchical_name):
        if not reg.reset_signal:
            continue
        rleaf = reg.reset_signal.split(".")[-1]
        if reg.reset_type.value == "unknown":
            candidates[rleaf] = candidates.get(rleaf, 0) + 1
    for rname in sorted(candidates):
        if rname in tg.resets and tg.resets[rname].reset_type.value == "asynchronous":
            continue
        ev = _ev(rid, "structural",
                 f"Signal '{rname}' appears to act as a synchronous reset "
                 f"for {candidates[rname]} register(s).",
                 confidence=Confidence.MEDIUM, objs=[rname], created_at=_run_ts)
        res.add_evidence(ev)
        res.add_ambiguity(
            f"Signal '{rname}' may be a synchronous reset; manual review recommended.",
            object=rname, severity="WARNING",
        )
    if res.ambiguities:
        res.result_status = InferenceResultStatus.REQUIRES_CONFIRMATION
    return res


def rule_rst_003_adversarial(design: Design, tg: TimingGraph, *,
                             _run_ts: str | None = None, **kw) -> InferenceResult:
    rid = "RST-003"
    res = InferenceResult(rule_id=rid, rule_name="adversarial_reset_name_usage",
                          confidence=Confidence.MEDIUM,
                          result_status=InferenceResultStatus.NO_FINDING)
    rst_like = ("rst", "reset", "rst_n", "reset_n", "areset", "sreset")
    reset_signals = set(tg.resets.keys())
    for net in sorted(design.nets.values(), key=lambda n: n.local_name):
        lname = net.local_name.lower()
        if not any(lname.startswith(h) for h in rst_like):
            continue
        if net.local_name in reset_signals:
            continue
        ev = _ev(rid, "structural",
                 f"Signal '{net.local_name}' has a reset-like name but is not used "
                 f"as a register reset pin; it may be functional data.",
                 confidence=Confidence.MEDIUM, objs=[net.local_name], created_at=_run_ts)
        res.add_evidence(ev)
        res.add_warning(
            f"Signal '{net.local_name}' named like a reset but used as data.",
            object=net.local_name,
        )
    return res
