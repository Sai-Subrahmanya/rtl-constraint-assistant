"""
Timing report parser for OpenSTA-style reports (Step 10 §10, §30).

Extracts:
- WNS/TNS/violations for setup (max) and hold (min)
- worst setup/hold slack
- worst critical-path details (startpoint, endpoint, launch/capture
  clocks, path group) when present in the text
- cell-count / area lines from Yosys stat output embedded in logs.

Values are stored in SI seconds unless otherwise noted. The parser is
tolerant of missing fields — absent metrics remain None rather than
being fabricated.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..qor.model import CriticalPath, QoRResult
from ..utils.logging import get_logger

log = get_logger("reports.timing")


_PATH_START_RE = re.compile(r"^\s*Startpoint:\s*(\S+)")
_PATH_END_RE = re.compile(r"^\s*Endpoint:\s*(\S+)")
_PATH_GROUP_RE = re.compile(r"^\s*Path Group:\s*(\S+)")
_PATH_TYPE_RE = re.compile(r"^\s*Path Type:\s*(setup|hold|max|min|max_rise|max_fall|min_rise|min_fall)\s*$", re.IGNORECASE)
_SLACK_RE = re.compile(r"slack\s*\(\s*(MET|VIOLATED)\s*\)\s*(-?\d+\.?\d*(?:[eE][+-]?\d+)?)")
# report_wns outputs e.g. "wns -0.0123"
_WNS_RE = re.compile(r"^\s*wns\s*(-?\d+\.?\d*(?:[eE][+-]?\d+)?)\s*$", re.IGNORECASE | re.MULTILINE)
_TNS_RE = re.compile(r"^\s*tns\s*(-?\d+\.?\d*(?:[eE][+-]?\d+)?)\s*$", re.IGNORECASE | re.MULTILINE)
_WORST_SUMMARY_RE = re.compile(r"worst\s+negative\s+slack", re.IGNORECASE)
# "Startpoint: foo (rising edge-triggered flip-flop ...)" — capture clock group by launch/capture lines
_LAUNCH_CLOCK_RE = re.compile(r"^\s*Clock (?:Path )?(?:Start|Launch|Source):\s*(\S+)|^\s*(?:Launch|Source)\s+Clock:\s*(\S+)|^\s*clock\s+(\S+)\s*\(rise edge\)", re.IGNORECASE)
_CAPTURE_CLOCK_RE = re.compile(r"^\s*Clock (?:End|Capture|Destination):\s*(\S+)|^\s*(?:Capture|Destination)\s+Clock:\s*(\S+)", re.IGNORECASE)
_CELL_RE = re.compile(r"Number of cells:\s*(\d+)")
_AREA_RE = re.compile(r"Chip area[^:]*:\s*(-?\d+\.?\d*(?:[eE][+-]?\d+)?)")


def parse_synth_report(path: str | Path) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return _parse_stat_block(text)


def _parse_stat_block(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {"cell_count": None}
    m = _CELL_RE.search(text)
    if m:
        out["cell_count"] = int(m.group(1))
    m = _AREA_RE.search(text)
    if m:
        try:
            out["area"] = float(m.group(1))
        except ValueError:
            pass
    return out


def parse_sta_report(path: str | Path) -> QoRResult:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return parse_sta_text(text)


def _ns_to_seconds(v: float) -> float:
    """OpenSTA defaults to reporting in nanoseconds. The values in
    slack lines, report_wns, and report_tns are treated as ns."""
    return v * 1e-9


def parse_sta_text(text: str) -> QoRResult:
    qor = QoRResult(raw_report_text=text, tool="opensta")

    # report_wns / report_tns outputs are authoritative when present.
    # There can be max (setup) then min (hold) sections. We pick the
    # first block for setup and the second for hold, but also look at
    # inline report_checks blocks.
    wns_matches = list(_WNS_RE.finditer(text))
    tns_matches = list(_TNS_RE.finditer(text))

    # Separate setup/hold by path-type markers in the text. We walk
    # through the text line-by-line tracking current path type and
    # accumulating per-path slacks, start/end/clocks.
    lines = text.splitlines()
    setup_slacks: list[float] = []
    hold_slacks: list[float] = []
    current_is_setup = True
    cur_start = cur_end = cur_group = cur_launch = cur_capture = None
    cur_slack: float | None = None
    cur_path_type: str = "max"
    worst_setup: list | None = None  # [slack, CriticalPath]
    worst_hold: list | None = None

    def _flush_path() -> None:
        nonlocal cur_start, cur_end, cur_group, cur_launch, cur_capture, cur_slack
        nonlocal worst_setup, worst_hold
        if cur_slack is None:
            return
        cp = CriticalPath(
            startpoint=cur_start, endpoint=cur_end, path_group=cur_group,
            launch_clock=cur_launch, capture_clock=cur_capture,
            slack=cur_slack, path_type=cur_path_type,
        )
        if current_is_setup:
            setup_slacks.append(cur_slack)
            if worst_setup is None or cur_slack < worst_setup[0]:
                worst_setup = [cur_slack, cp]
        else:
            hold_slacks.append(cur_slack)
            if worst_hold is None or cur_slack < worst_hold[0]:
                worst_hold = [cur_slack, cp]
        cur_start = cur_end = cur_group = cur_launch = cur_capture = None
        cur_slack = None

    for line in lines:
        # Detect path type section header
        m = _PATH_TYPE_RE.search(line)
        if m:
            _flush_path()
            pt = m.group(1).lower()
            cur_path_type = pt
            current_is_setup = ("hold" not in pt and "min" not in pt)
            continue
        m = _PATH_START_RE.search(line)
        if m:
            if cur_slack is not None:
                _flush_path()
            cur_start = m.group(1)
            continue
        m = _PATH_END_RE.search(line)
        if m:
            cur_end = m.group(1)
            continue
        m = _PATH_GROUP_RE.search(line)
        if m:
            cur_group = m.group(1)
            continue
        m = _LAUNCH_CLOCK_RE.search(line)
        if m:
            cur_launch = next((g for g in m.groups() if g), cur_launch)
            continue
        m = _CAPTURE_CLOCK_RE.search(line)
        if m:
            cur_capture = next((g for g in m.groups() if g), cur_capture)
            continue
        m = _SLACK_RE.search(line)
        if m:
            try:
                cur_slack = _ns_to_seconds(float(m.group(2)))
                met = m.group(1) == "MET"
                if current_is_setup:
                    if not met or cur_slack < 0:
                        qor.setup_violations += 1
                else:
                    if not met or cur_slack < 0:
                        qor.hold_violations += 1
            except ValueError:
                pass
            _flush_path()
            continue

    # Use report_wns / report_tns outputs when present (preferred).
    # OpenSTA outputs these in ns.
    if wns_matches:
        try:
            qor.setup_wns = _ns_to_seconds(float(wns_matches[0].group(1)))
        except ValueError:
            pass
        if len(wns_matches) > 1:
            try:
                qor.hold_wns = _ns_to_seconds(float(wns_matches[1].group(1)))
            except ValueError:
                pass
    if tns_matches:
        try:
            qor.setup_tns = _ns_to_seconds(float(tns_matches[0].group(1)))
        except ValueError:
            pass
        if len(tns_matches) > 1:
            try:
                qor.hold_tns = _ns_to_seconds(float(tns_matches[1].group(1)))
            except ValueError:
                pass

    # Fall back to worst path slacks if report_wns didn't supply them
    if worst_setup is not None:
        qor.critical_setup = worst_setup[1]
        if qor.setup_wns is None:
            qor.setup_wns = worst_setup[0]
    if worst_hold is not None:
        qor.critical_hold = worst_hold[1]
        if qor.hold_wns is None:
            qor.hold_wns = worst_hold[0]
    qor.whs = qor.hold_wns
    qor.ths = qor.hold_tns
    qor.near_critical_count = sum(
        1 for s in setup_slacks if 0 <= s <= 200e-12
    )
    qor.path_count = len(setup_slacks) + len(hold_slacks)

    # Stat/area lines may appear if a yosys stat was included
    stat = _parse_stat_block(text)
    if stat.get("cell_count") is not None and qor.cell_count is None:
        qor.cell_count = stat["cell_count"]
    if stat.get("area") is not None and qor.area is None:
        qor.area = stat["area"]
        qor.area_proxy = None
    if qor.area is None and qor.cell_count is not None:
        qor.area_proxy = qor.cell_count

    return qor
