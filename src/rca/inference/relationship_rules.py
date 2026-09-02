"""
Clock-relationship inference (Manual §17, §18).

REL-001 clock_relationship_status
    For every active clock pair, start UNKNOWN unless evidence exists.
    User-declared relationships become set_clock_groups only when the
    user specifies "asynchronous" as FIXED; CDC path observation is
    evidence, NEVER proof of asynchrony.
"""

from __future__ import annotations

from ..design_model import Design
from ..timing_model import TimingGraph
from ..utils.enums import (
    ClockDomainRelationship,
    Confidence,
    InferenceResultStatus,
    RequirementLevel,
    SourceKind,
)
from ._evidence import make_evidence
from .rules import InferenceResult, MissingInformation, ProposedConstraint


def _ev(rid, kind, desc, confidence=Confidence.MEDIUM, objs=None, created_at=None):
    return make_evidence(rid, kind, desc, source_objects=objs,
                         confidence=confidence, created_at=created_at)


def rule_rel_001_relationships(design: Design, tg: TimingGraph,
                                user_rels=None, *,
                                _run_ts: str | None = None, **kw) -> InferenceResult:
    rid = "REL-001"
    res = InferenceResult(rule_id=rid, rule_name="clock_relationship_status",
                          confidence=Confidence.HIGH,
                          result_status=InferenceResultStatus.NO_FINDING)
    # User-specified async relationships -> set_clock_groups proposals
    for rel in user_rels or []:
        clocks = rel.get("clocks", [])
        if rel.get("relationship", "").lower() != "asynchronous":
            continue
        if len(clocks) < 2:
            continue
        fixed = bool(rel.get("fixed", True))
        for i in range(len(clocks)):
            for j in range(i + 1, len(clocks)):
                a, b = clocks[i], clocks[j]
                ev = _ev(rid, "user",
                         f"Clock pair '{a}','{b}' declared asynchronous by user.",
                         confidence=Confidence.HIGH, objs=[a, b], created_at=_run_ts)
                res.add_evidence(ev)
                res.propose(ProposedConstraint(
                    kind="set_clock_groups",
                    object=f"{a}:{b}",
                    values={"groups": [[a], [b]], "relationship": "asynchronous"},
                    confidence=Confidence.HIGH,
                    status="FIXED" if fixed else "PROPOSED",
                    source_kind=SourceKind.USER.value, evidence=[ev],
                    rationale="User-declared asynchronous relationship.",
                    merge_key=("set_clock_groups", tuple(sorted([a, b]))),
                ))
    # Unknown relationships with observed CDC paths -> missing info
    for e in tg.domain_edges:
        if e.relationship != ClockDomainRelationship.UNKNOWN:
            continue
        if e.cdc_paths_observed > 0:
            res.add_missing(MissingInformation(
                id=f"REQ-REL-{e.clock_a}-{e.clock_b}",
                category="clock_relationship",
                object=f"{e.clock_a} <-> {e.clock_b}",
                severity="ERROR",
                requirement_level=RequirementLevel.UNSAFE_TO_INFER,
                message=(f"Clock relationship required between '{e.clock_a}' and '{e.clock_b}'"),
                rationale=(f"{e.cdc_paths_observed} CDC path(s) observed but relationship is unknown; "
                           "automatically inferring asynchrony is unsafe."),
                evidence=[{"kind": "structural", "cdc_paths": e.cdc_paths_observed,
                           "possible_relationships": ["asynchronous", "synchronous", "related"]}],
                suggested_inputs=[{"field": "relationship",
                                   "options": ["asynchronous", "synchronous", "related"]}],
                blocking=True, rule_id=rid,
                possible_values=["asynchronous", "synchronous", "related"],
            ))
    if res.proposed_constraints:
        res.result_status = InferenceResultStatus.APPLIED
    elif res.missing_information:
        res.result_status = InferenceResultStatus.BLOCKED
    return res
