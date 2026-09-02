"""Backend capability validation (Step 7 §16).

Before code generation for a target backend, cross-check the constraint
set against the backend's capability matrix so that validation does not
PASS while the generator is forced to drop constraints.
"""

from __future__ import annotations

from ..constraint_model import ConstraintSet
from ..utils.enums import (
    ConstraintType, ErrorCode, Severity, ValidationCategory,
)
from ..sdc import get_backend
from ..sdc.generation.preflight import preflight_constraint
from .base import ValidationIssue, ValidationReport, _issue


def validate_backend(cset: ConstraintSet, backend_name: str,
                     report: ValidationReport, mode: str = "balanced") -> None:
    """Validate all emittable constraints against the backend's preflight
    using the same code path the generator uses. Any fatal preflight
    issue is recorded as a BACKEND_BLOCKED error.
    """
    report.checks_run.append(f"backend:{backend_name}")
    try:
        backend = get_backend(backend_name)
    except Exception:
        _issue(report, Severity.ERROR, ValidationCategory.BACKEND,
               ErrorCode.BACKEND_UNSUPPORTED,
               f"Unknown backend '{backend_name}'.",
               suggestion="Use one of: generic, opensta, synopsys, cadence.")
        return
    caps = backend.capabilities()
    blocked = 0
    for c in cset.emittable(mode):
        issues = preflight_constraint(c, caps)
        fatal = [i for i in issues if i.fatal]
        for i in fatal:
            blocked += 1
            _issue(report, Severity.ERROR, ValidationCategory.BACKEND,
                   ErrorCode.BACKEND_BLOCKED,
                   f"[{backend_name}] {c.id} ({c.type.value}): {i.message}",
                   constraint_id=c.id,
                   suggestion="Either select a backend that supports this constraint/option, or remove the constraint.")
    if blocked:
        _issue(report, Severity.ERROR, ValidationCategory.BACKEND,
               ErrorCode.BACKEND_BLOCKED,
               f"Backend '{backend_name}' cannot safely emit {blocked} constraint(s).",
               suggestion="Review the diagnostics above and either switch backends or adjust constraints.")
