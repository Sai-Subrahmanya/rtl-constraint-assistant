"""SDC import / parse validation (Step 13, Req 10).

Integrates with the existing SDC importer/parser rather than re-parsing.
After a constraint set has been imported and normalized, this layer
classifies the import as one of:

* ``SYNTAX_INVALID``   — the parser failed to parse one or more commands
  (syntactic errors).
* ``SEMANTIC_INVALID`` — parsed successfully but one or more commands are
  semantically unsupported / not fully modeled.
* ``INCOMPLETE``       — parsed but some options were dropped (partial
  support) or required fields are missing.
* ``COMPLETE``         — imported with no warnings/errors.
* ``UNRESOLVED``       — the parser could not resolve a reference but
  retained the original text for later resolution.

It never re-runs the lexer/parser itself; it consumes the diagnostics the
importer already produced.
"""

from __future__ import annotations

from typing import Any

from ..constraint_model import ConstraintSet
from ..utils.enums import (
    ErrorCode,
    Severity,
    ValidationCategory,
)
from .base import ValidationIssue, ValidationReport, _issue


def validate_sdc_import(cset: ConstraintSet,
                        parser: Any | None = None,
                        report: ValidationReport | None = None,
                        ) -> dict[str, Any]:
    """Validate an imported constraint set using importer diagnostics.

    ``parser`` is any object with ``warnings`` / ``errors`` lists (e.g. an
    ``rca.sdc.SDCParser`` or ``rca.sdc_importer.SdcImporter`` result).  If
    omitted, no external diagnostics are available and we only perform a
    structural completeness check (e.g. an empty import is incomplete).

    Returns a summary dict; when ``report`` is supplied the findings are
    also added to it.
    """
    rep = report or ValidationReport()
    rep.checks_run.append("sdc_import")

    diagnostics: list[str] = []
    errors: list[str] = []
    if parser is not None:
        diagnostics = list(getattr(parser, "warnings", []) or [])
        errors = list(getattr(parser, "errors", []) or [])

    # --- Syntactic errors from the parser ---
    for e in errors:
        _issue(rep, Severity.ERROR, ValidationCategory.SYNTAX,
               ErrorCode.SYNTAX_ERROR, f"SDC syntax error: {e}",
               suggestion="Correct the offending SDC command.")

    # --- Parser warnings (unknown command / failed to parse) ---
    syntax_warnings = 0
    semantic_warnings = 0
    for w in diagnostics:
        text = str(w)
        low = text.lower()
        if "failed to parse" in low or "unknown sdc command" in low \
                or "syntax" in low:
            syntax_warnings += 1
            _issue(rep, Severity.WARNING, ValidationCategory.SYNTAX,
                   ErrorCode.SYNTAX_ERROR, f"SDC parse warning: {w}",
                   suggestion="Verify the SDC command syntax and replay the import.")
        elif "minimal support" in low or "not fully" in low \
                or "unsupported" in low:
            semantic_warnings += 1
            _issue(rep, Severity.WARNING, ValidationCategory.SYNTAX,
                   ErrorCode.SDC_IMPORT_SEMANTIC,
                   f"SDC semantic limitation: {w}",
                   suggestion="Confirm the dropped option does not affect the intended timing model.")

    # --- Empty import → incomplete ---
    if len(cset) == 0:
        _issue(rep, Severity.WARNING, ValidationCategory.SYNTAX,
               ErrorCode.SDC_IMPORT_INCOMPLETE,
               "Imported SDC produced an empty constraint set.",
               suggestion="Verify the SDC file contains supported commands "
                          "or that the path is correct.",
               resolution_status="INCOMPLETE")

    # --- Classification ---
    if errors or syntax_warnings:
        status = "SYNTAX_INVALID"
    elif semantic_warnings:
        status = "SEMANTIC_INVALID"
    elif len(cset) == 0:
        status = "INCOMPLETE"
    elif diagnostics:
        status = "INCOMPLETE"
    else:
        status = "COMPLETE"

    summary = {
        "status": status,
        "constraint_count": len(cset),
        "syntax_warnings": syntax_warnings,
        "semantic_warnings": semantic_warnings,
        "parser_errors": len(errors),
        "parser_warnings": len(diagnostics),
    }
    rep.completeness_summary["sdc_import"] = summary
    if report is not None:
        # Only surface the aggregate as an info issue when there is no
        # blocking finding, to avoid noisy warnings with no value.
        if status in ("INCOMPLETE", "SEMANTIC_INVALID") and not errors:
            _issue(rep, Severity.INFO, ValidationCategory.SYNTAX,
                   ErrorCode.SDC_IMPORT_INCOMPLETE,
                   f"SDC import classified {status}: {summary['constraint_count']} "
                   f"constraint(s) imported with {summary['parser_warnings']} "
                   f"parser warning(s).",
                   blocking=False, resolution_status=status)
    return summary
