"""Deterministic Tcl quoting/escaping and unit formatting for SDC emission."""

from __future__ import annotations

import math
import re

from ...utils.units import from_seconds, parse_time_string
from ...utils.enums import TimeUnit


# Names that are Tcl metacharacter-free and do not need quoting.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_/\[\].:-]*$")


def tcl_quote(name: str) -> str:
    """Return a deterministic Tcl-quoted form of ``name``.

    * Safe alphanumeric/identifier-like names are returned unquoted.
    * Names with Tcl metacharacters or whitespace are wrapped in ``{...}``.
    * Names that themselves contain ``{`` or ``}`` fall back to a quoted
      string with backslash escapes (braces are not safe for those).
    * Wildcards ``*?[]`` are preserved literally (braced so they are not
      interpreted by Tcl).
    """
    if name is None:
        return "{}"
    s = str(name)
    if s == "":
        return "{}"
    if _SAFE_NAME_RE.match(s):
        return s
    # If the name contains braces, we can't use { } quoting; use "..." with
    # the small set of escapes Tcl requires inside double quotes.
    if "{" in s or "}" in s or "\\" in s or "$" in s or "[" in s or "]" in s:
        return _quoted_string(s)
    # Otherwise brace-quote — preserves contents verbatim.
    return "{" + s + "}"


def _quoted_string(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch in ("\\", '"', "$", "[", "]"):
            out.append("\\")
            out.append(ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def tcl_quote_list(names: list[str]) -> str:
    """Render a list of names using deterministic brace-group syntax
    when more than one, otherwise a single quoted name."""
    if not names:
        return ""
    if len(names) == 1:
        return tcl_quote(names[0])
    return "{ " + " ".join(tcl_quote(n) for n in names) + " }"


# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------


# Deterministic formatting: no scientific notation, strip trailing zeros.
def format_ns(seconds: float, *, prec: int = 3) -> str:
    """Render SI seconds as deterministic nanoseconds.

    - Integer values render without a decimal point if they are exact
      (e.g. ``10``).
    - Fractional values render with up to ``prec`` fractional digits,
      trailing zeros stripped (e.g. ``2.5`` not ``2.500``).
    - ps/fs values are scaled so the output is always in nanoseconds
      (consistent with our generic SDC header declaring nanosecond units).
    """
    if seconds is None:
        raise ValueError("cannot format None time value")
    ns = float(from_seconds(float(seconds), TimeUnit.NANOSECOND))
    if not math.isfinite(ns):
        raise ValueError(f"non-finite time value: {seconds}")
    # Round to prec fractional digits to avoid binary-fp artifacts.
    ns = round(ns, prec)
    if ns == int(ns):
        return str(int(ns))
    txt = f"{ns:.{prec}f}".rstrip("0").rstrip(".")
    return txt or "0"


def format_time(seconds: float, unit: TimeUnit = TimeUnit.NANOSECOND, prec: int = 3) -> str:
    v = float(from_seconds(float(seconds), unit))
    v = round(v, prec)
    if v == int(v):
        return str(int(v))
    return f"{v:.{prec}f}".rstrip("0").rstrip(".")
