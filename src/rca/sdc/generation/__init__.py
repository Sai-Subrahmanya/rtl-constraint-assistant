"""Step 6 deterministic SDC generation package.

Public surface:

* :class:`SdcRenderer` – shared deterministic rendering engine.
* :class:`SdcGenerationResult`, :class:`GenerationStatus`,
  :class:`GenerationDiagnostic` – result model.
* :func:`tcl_quote`, :func:`tcl_quote_list`, :func:`format_ns` –
  deterministic quoting / unit formatting.
* :func:`preflight_constraint` – backend-independent preflight checks.
"""

from .preflight import PreflightIssue, preflight_constraint
from .renderer import EMISSION_ORDER, SdcRenderer, render_target, render_target_list
from .result import GenerationDiagnostic, GenerationStatus, SdcGenerationResult
from .tcl_quote import format_ns, tcl_quote, tcl_quote_list

__all__ = [
    "SdcRenderer", "EMISSION_ORDER",
    "SdcGenerationResult", "GenerationStatus", "GenerationDiagnostic",
    "PreflightIssue", "preflight_constraint",
    "render_target", "render_target_list",
    "tcl_quote", "tcl_quote_list", "format_ns",
]
