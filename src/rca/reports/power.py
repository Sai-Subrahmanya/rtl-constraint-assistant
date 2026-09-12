"""Conservative parser for OpenROAD/OpenSTA ``report_power`` text reports.

Only the OpenROAD/OpenSTA group-summary format is supported.  A usable report
must identify its group table, declare a supported power unit explicitly, and
contain exactly one usable ``Total`` row.  The parser reports evidence; it does
not estimate power or execute any EDA tool.

All returned numerical values are normalized to watts.  ``0.0`` is a valid
reported value and is never used as an unavailable-value sentinel.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..utils.hashing import hash_file, hash_text

POWER_REPORT_FORMAT = "openroad_report_power"
POWER_REPORT_PARSER_VERSION = "1"
POWER_REPORT_PRODUCER = "openroad_opensta"


class PowerParseStatus(str, Enum):
    """Detailed parser classification, separate from canonical QoR status.

    ``QoRResult.power_status`` intentionally remains the historical
    :class:`rca.utils.enums.PowerStatus` vocabulary.  The flow records this
    classification in ``raw_reports[\"power\"][\"parsing_status\"]`` while mapping
    every non-available parse result to canonical ``UNAVAILABLE`` power.
    """

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    MALFORMED = "MALFORMED"
    INVALID = "INVALID"
    UNSUPPORTED = "UNSUPPORTED"


# The reported unit must be declared in the table header.  The parser does not
# assume OpenSTA's default command units, because a redirected report alone may
# otherwise be dimensionally ambiguous.
_UNIT_FACTORS: dict[str, float] = {
    "w": 1.0,
    "watts": 1.0,
    "mw": 1e-3,
    "uw": 1e-6,
    "µw": 1e-6,
    "nw": 1e-9,
    "pw": 1e-12,
}

_HEADER_RE = re.compile(
    r"^\s*Group\b.*\bInternal\b.*\bSwitching\b.*\bLeakage\b.*\bTotal\b",
    re.IGNORECASE,
)
_UNIT_RE = re.compile(r"\bPower\s*\(\s*([^()\s]+)\s*\)", re.IGNORECASE)
_TOTAL_RE = re.compile(r"^\s*Total\b\s*(.*)$", re.IGNORECASE)
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_MISSING_TOKENS = {"-", "n/a", "na", "unknown", "none", ""}


@dataclass(frozen=True)
class PowerReportParseResult:
    """Parser-boundary result, not a second QoR model.

    It preserves raw report identity and diagnostics until the existing
    :class:`~rca.qor.model.QoRResult` fields are populated by the EDA flow.
    """

    status: str
    total: float | None = None
    dynamic: float | None = None
    leakage: float | None = None
    internal: float | None = None
    switching: float | None = None
    original_unit: str | None = None
    source_path: str = ""
    source_sha256: str = ""
    scenario_id: str | None = None
    mode: str | None = None
    corner: str | None = None
    producer_version: str | None = None
    diagnostics: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.status == PowerParseStatus.AVAILABLE.value and self.total is not None

    def provenance(self) -> dict[str, Any]:
        """Return the structured metadata stored in ``QoRResult.raw_reports``."""
        return {
            "format": POWER_REPORT_FORMAT,
            "parser_format_version": POWER_REPORT_PARSER_VERSION,
            "producer": POWER_REPORT_PRODUCER,
            "producer_version": self.producer_version,
            "original_unit": self.original_unit,
            # Only claim a normalized unit when a usable numeric report value
            # was actually accepted. Unsupported/malformed evidence must not
            # masquerade as normalized power data.
            "normalized_unit": "W" if self.available else None,
            "scenario_id": self.scenario_id,
            "mode": self.mode,
            "corner": self.corner,
            "report_path": self.source_path,
            "sha256": self.source_sha256,
            "parsing_status": self.status,
            "diagnostics": list(self.diagnostics),
            # Preserve individual source cells for audit; canonical QoR exposes
            # only total, dynamic (= internal + switching), and leakage.
            "reported_internal_w": self.internal,
            "reported_switching_w": self.switching,
        }


def unavailable_power_result(*, source_path: str = "", scenario_id: str | None = None,
                             mode: str | None = None, corner: str | None = None,
                             producer_version: str | None = None,
                             diagnostic: str = "No configured power report is available.",
                             ) -> PowerReportParseResult:
    """Build an explicit unavailable result without fabricating a value."""
    return PowerReportParseResult(
        status=PowerParseStatus.UNAVAILABLE.value,
        source_path=source_path,
        scenario_id=scenario_id,
        mode=mode,
        corner=corner,
        producer_version=producer_version,
        diagnostics=[diagnostic],
    )


def parse_openroad_power_report(path: str | Path, *, scenario_id: str | None = None,
                                 mode: str | None = None, corner: str | None = None,
                                 producer_version: str | None = None,
                                 ) -> PowerReportParseResult:
    """Parse one configured OpenROAD/OpenSTA power-report file.

    A missing or unreadable file is ``UNAVAILABLE`` rather than zero.  Content
    classification is delegated to :func:`parse_openroad_power_text`.
    """
    report_path = Path(path)
    path_text = str(report_path)
    if not report_path.is_file():
        return unavailable_power_result(
            source_path=path_text,
            scenario_id=scenario_id,
            mode=mode,
            corner=corner,
            producer_version=producer_version,
            diagnostic=f"Configured power report does not exist: {path_text}",
        )
    try:
        text = report_path.read_text(encoding="utf-8", errors="replace")
        digest = hash_file(report_path)
    except OSError as exc:
        return unavailable_power_result(
            source_path=path_text,
            scenario_id=scenario_id,
            mode=mode,
            corner=corner,
            producer_version=producer_version,
            diagnostic=f"Configured power report is unreadable: {path_text}: {exc}",
        )
    return parse_openroad_power_text(
        text,
        source_path=path_text,
        source_sha256=digest,
        scenario_id=scenario_id,
        mode=mode,
        corner=corner,
        producer_version=producer_version,
    )


def parse_openroad_power_text(text: str, *, source_path: str = "",
                               source_sha256: str | None = None,
                               scenario_id: str | None = None,
                               mode: str | None = None, corner: str | None = None,
                               producer_version: str | None = None,
                               ) -> PowerReportParseResult:
    """Parse deterministic OpenROAD/OpenSTA group-summary report text.

    The table signature has all four component columns in OpenROAD's documented
    order.  More than one candidate table or more than one ``Total`` row is
    intentionally ambiguous; RCA never chooses one silently.
    """
    digest = source_sha256 if source_sha256 is not None else hash_text(text)
    common = {
        "source_path": source_path,
        "source_sha256": digest,
        "scenario_id": scenario_id,
        "mode": mode,
        "corner": corner,
        "producer_version": producer_version,
    }
    lines = text.splitlines()
    header_indices = [i for i, line in enumerate(lines) if _HEADER_RE.search(line)]

    # A file mentioning report_power but lacking the required group-table
    # signature is an intended but malformed report.  A wholly unrelated file
    # is simply unsupported by this focused parser.
    if not header_indices:
        status = (PowerParseStatus.MALFORMED.value if re.search(r"\breport_power\b", text, re.IGNORECASE)
                  else PowerParseStatus.UNSUPPORTED.value)
        message = ("OpenROAD/OpenSTA report_power marker was found but the required "
                   "group-summary header is malformed."
                   if status == PowerParseStatus.MALFORMED.value else
                   "File does not identify the supported OpenROAD/OpenSTA report_power "
                   "group-summary format.")
        return PowerReportParseResult(status=status, diagnostics=[message], **common)

    if len(header_indices) != 1:
        return PowerReportParseResult(
            status=PowerParseStatus.UNKNOWN.value,
            diagnostics=[("Ambiguous report: multiple OpenROAD/OpenSTA power tables found; "
                          "no table was selected.")],
            **common,
        )

    header_index = header_indices[0]
    # The table's unit is conventionally on the next header line.  Limit the
    # search to this table (until the first Total row) so an unrelated unit
    # declaration elsewhere cannot lend a dimension to this table.
    total_indices = [i for i in range(header_index + 1, len(lines))
                     if _TOTAL_RE.search(lines[i])]
    if not total_indices:
        return PowerReportParseResult(
            status=PowerParseStatus.UNKNOWN.value,
            diagnostics=["Recognized OpenROAD/OpenSTA power table has no Total row."],
            **common,
        )
    first_total = total_indices[0]
    unit_matches = [m for line in lines[header_index:first_total + 1]
                    if (m := _UNIT_RE.search(line))]
    if not unit_matches:
        return PowerReportParseResult(
            status=PowerParseStatus.UNKNOWN.value,
            diagnostics=["Recognized OpenROAD/OpenSTA power table has no explicit power unit."],
            **common,
        )
    if len(unit_matches) != 1:
        return PowerReportParseResult(
            status=PowerParseStatus.UNKNOWN.value,
            diagnostics=["Ambiguous power unit declarations in the report table."],
            **common,
        )
    original_unit = unit_matches[0].group(1)
    factor = _UNIT_FACTORS.get(original_unit.lower())
    if factor is None:
        return PowerReportParseResult(
            status=PowerParseStatus.UNSUPPORTED.value,
            original_unit=original_unit,
            diagnostics=[f"Unsupported explicit power unit '{original_unit}'."],
            **common,
        )

    if len(total_indices) != 1:
        return PowerReportParseResult(
            status=PowerParseStatus.UNKNOWN.value,
            original_unit=original_unit,
            diagnostics=["Ambiguous report: multiple Total rows found; no row was selected."],
            **common,
        )

    cells = _TOTAL_RE.search(lines[first_total]).group(1).split()
    if len(cells) < 4:
        return PowerReportParseResult(
            status=PowerParseStatus.MALFORMED.value,
            original_unit=original_unit,
            diagnostics=[("Malformed Total row: expected Internal, Switching, Leakage, and "
                          "Total columns.")],
            **common,
        )

    values: list[float | None] = []
    labels = ("Internal", "Switching", "Leakage", "Total")
    for label, token in zip(labels, cells[:4]):
        parsed, error = _parse_cell(token)
        if error:
            return PowerReportParseResult(
                status=PowerParseStatus.MALFORMED.value,
                original_unit=original_unit,
                diagnostics=[f"Malformed numeric {label} power cell '{token}' in Total row."],
                **common,
            )
        values.append(parsed * factor if parsed is not None else None)

    internal, switching, leakage, total = values
    if total is None:
        return PowerReportParseResult(
            status=PowerParseStatus.UNKNOWN.value,
            original_unit=original_unit,
            diagnostics=["Recognized Total row has no usable total-power value."],
            **common,
        )
    present = {label: value for label, value in zip(labels, values) if value is not None}
    if any(value < 0 for value in present.values()):
        return PowerReportParseResult(
            status=PowerParseStatus.INVALID.value,
            original_unit=original_unit,
            diagnostics=["Negative power is invalid in the supported report summary."],
            **common,
        )

    # The group table is printed with finite precision.  Validate an available
    # full breakdown with a small documented rounding tolerance rather than
    # demanding bit-for-bit equality; a missing component is deliberately not
    # inferred and therefore bypasses this check.
    if internal is not None and switching is not None and leakage is not None:
        component_total = internal + switching + leakage
        if not math.isclose(total, component_total, rel_tol=0.02, abs_tol=1e-15):
            return PowerReportParseResult(
                status=PowerParseStatus.INVALID.value,
                original_unit=original_unit,
                diagnostics=[("Total power is inconsistent with Internal + Switching + Leakage "
                              "beyond report-rounding tolerance.")],
                **common,
            )

    diagnostics: list[str] = []
    dynamic = internal + switching if internal is not None and switching is not None else None
    if dynamic is None:
        diagnostics.append("Dynamic power unavailable: both Internal and Switching are required.")
    if leakage is None:
        diagnostics.append("Leakage power unavailable: Total-row leakage component is missing.")
    return PowerReportParseResult(
        status=PowerParseStatus.AVAILABLE.value,
        total=total,
        dynamic=dynamic,
        leakage=leakage,
        internal=internal,
        switching=switching,
        original_unit=original_unit,
        diagnostics=diagnostics,
        **common,
    )


def _parse_cell(token: str) -> tuple[float | None, bool]:
    """Return ``(value, malformed)`` for one Total-row cell."""
    if token.lower() in _MISSING_TOKENS:
        return None, False
    if not _NUMBER_RE.fullmatch(token):
        return None, True
    try:
        value = float(token)
    except ValueError:
        return None, True
    if not math.isfinite(value):
        return None, True
    return value, False


__all__ = [
    "POWER_REPORT_FORMAT",
    "POWER_REPORT_PARSER_VERSION",
    "POWER_REPORT_PRODUCER",
    "PowerParseStatus",
    "PowerReportParseResult",
    "parse_openroad_power_report",
    "parse_openroad_power_text",
    "unavailable_power_result",
]
