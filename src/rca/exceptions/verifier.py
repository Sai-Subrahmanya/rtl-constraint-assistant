"""
Exception verification harness (Manual §30, Step 8).

Combines structural analysis (``analyze_exceptions``) with a
``FormalBackend`` to produce ``ExceptionAnalysisResult`` records whose
``final_status`` reflects the verified state.

When no formal backend is attached, ``ConservativeFormalBackend`` is
used: structural analysis runs, but every exception remains
``UNRESOLVED`` unless the user explicitly confirmed it.  VERIFIED is
never returned without a real proof result.
"""

from __future__ import annotations

from ..constraint_model import ConstraintSet
from ..design_model import Design
from ..timing_model import TimingGraph
from ..utils.enums import (
    ConstraintType,
    VerificationStatus,
)
from .analyzer import (
    ExceptionAnalysisReport,
    ExceptionAnalysisResult,
    analyze_exceptions,
)
from .formal_backend import (
    ConservativeFormalBackend,
    FormalBackend,
    VerificationResult,
)


def verify_exceptions(cset: ConstraintSet,
                      design: Design | None = None,
                      tg: TimingGraph | None = None,
                      backend: FormalBackend | None = None,
                      user_confirmed_ids: set[str] | None = None,
                      user_rejected_ids: set[str] | None = None,
                      ) -> ExceptionAnalysisReport:
    """Run structural analysis and (if available) formal verification.

    Approval (USER_CONFIRMED/USER_REJECTED) is recorded separately from
    verification and never flips verification_status to VERIFIED.
    Observational: does not mutate inputs.
    """
    backend = backend or ConservativeFormalBackend()
    report = analyze_exceptions(design, cset, tg=tg,
                                user_confirmed_ids=user_confirmed_ids,
                                user_rejected_ids=user_rejected_ids)
    for r in report:
        c = cset.get(r.constraint_id)
        if c is None:
            r.verification = VerificationResult(
                constraint_id=r.constraint_id,
                status=VerificationStatus.ERROR,
                tool=backend.name,
                message="Constraint not found in set.",
            )
            continue
        # INVALID / ERROR / NOT_APPLICABLE from structural analysis: we
        # still populate a verification result reflecting the structural
        # verdict rather than invoking the backend (a backend proof
        # cannot salvage an invalid constraint such as -cycles -1).
        if r.verification_status in (VerificationStatus.INVALID,
                                     VerificationStatus.ERROR,
                                     VerificationStatus.NOT_APPLICABLE):
            r.verification = VerificationResult(
                constraint_id=c.id,
                status=r.verification_status,
                tool="structural",
                message=f"Structural analysis status: {r.verification_status.value}.",
                evidence={"findings": list(r.structural_findings),
                          "risk": r.risk.value},
            )
            continue
        ps = c.path_selector
        spec = ps.summary() if ps else {}
        # Scenario applicability is UCM-owned and must survive into formal
        # provenance.  The backend does not invent scenario assumptions; a
        # user-authored .sby job/task is responsible for proving them.
        spec["scenario_ids"] = sorted(c.scenario_ids or [])
        try:
            if c.type == ConstraintType.SET_FALSE_PATH:
                vr = backend.prove_false_path(c.id, spec)
            elif c.type == ConstraintType.SET_MULTICYCLE_PATH:
                cycles = int(c.values.get("cycles", 1))
                vr = backend.prove_multicycle(c.id, spec, cycles)
            else:
                vr = VerificationResult(
                    constraint_id=c.id,
                    status=VerificationStatus.UNRESOLVED,
                    tool=backend.name,
                    message="Verification not implemented for this exception type.",
                )
        except Exception as exc:  # noqa: BLE001 - isolate external formal backend failures
            vr = VerificationResult(
                constraint_id=c.id,
                status=VerificationStatus.ERROR,
                tool=backend.name,
                message=f"Formal backend raised {type(exc).__name__}: {exc}",
            )
        r.verification = vr

    return report


def emittable_exceptions(report: ExceptionAnalysisReport,
                         mode: str = "strict") -> list[ExceptionAnalysisResult]:
    return [r for r in report if r.is_emittable(mode)]
