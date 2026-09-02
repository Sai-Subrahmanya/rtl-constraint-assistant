"""
QoR data model (Step 10 — WP-N).

QoRResult captures timing/area/power/cell metrics from an EDA run
along with metadata (run id, backend, version, flow stage, scenario).
Feasibility is a deterministic assessment separating "cannot run"
(BLOCKED) from "design timing is bad" (setup/hold violation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..utils.enums import PowerStatus, RunStatus


@dataclass
class CriticalPath:
    startpoint: str | None = None
    endpoint: str | None = None
    launch_clock: str | None = None
    capture_clock: str | None = None
    path_group: str | None = None
    slack: float | None = None
    path_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "startpoint": self.startpoint, "endpoint": self.endpoint,
            "launch_clock": self.launch_clock, "capture_clock": self.capture_clock,
            "path_group": self.path_group, "slack": self.slack,
            "path_type": self.path_type,
        }


@dataclass
class Feasibility:
    setup_pass: bool = False
    hold_pass: bool = False
    feasible: bool = False
    blocked: bool = False
    reason: str = ""
    status: str = RunStatus.BLOCKED.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_pass": self.setup_pass, "hold_pass": self.hold_pass,
            "feasible": self.feasible, "blocked": self.blocked,
            "reason": self.reason, "status": self.status,
        }

    @classmethod
    def from_qor(cls, qor: "QoRResult", *, setup_margin_ns: float = 0.0,
                 hold_margin_ns: float = 0.0) -> "Feasibility":
        if qor is None:
            return cls(blocked=True, reason="no QoR result", status=RunStatus.BLOCKED.value)
        if qor.setup_wns is None and qor.hold_wns is None and qor.cell_count is None:
            return cls(blocked=True, reason="no metrics collected", status=RunStatus.BLOCKED.value)
        setup_pass = qor.setup_wns is not None and qor.setup_wns >= (setup_margin_ns * 1e-9)
        hold_pass = qor.hold_wns is not None and qor.hold_wns >= (hold_margin_ns * 1e-9)
        feasible = setup_pass and hold_pass
        if not setup_pass and not hold_pass:
            reason = "setup and hold violations"
        elif not setup_pass:
            reason = "setup violation"
        elif not hold_pass:
            reason = "hold violation"
        else:
            reason = ""
        return cls(
            setup_pass=setup_pass, hold_pass=hold_pass, feasible=feasible,
            blocked=False, reason=reason,
            status=RunStatus.TIMING_FAIL.value if not feasible else RunStatus.SUCCESS.value,
        )

    @classmethod
    def blocked(cls, reason: str) -> "Feasibility":
        return cls(blocked=True, reason=reason, status=RunStatus.BLOCKED.value)


@dataclass
class QoRResult:
    # --- run metadata (Step 10 §14) ---
    run_id: str = ""
    candidate_id: str = ""
    backend: str = ""
    backend_version: str = ""
    flow_stage: str = ""
    scenario: str = "default"
    is_mock: bool = False

    # --- timing ---
    setup_wns: float | None = None
    setup_tns: float | None = None
    setup_violations: int = 0
    hold_wns: float | None = None
    hold_tns: float | None = None
    hold_violations: int = 0
    whs: float | None = None           # worst hold slack synonym
    ths: float | None = None
    near_critical_count: int = 0
    path_count: int | None = None
    wns_percentiles: dict[str, float] = field(default_factory=dict)
    timing_distribution: dict[str, int] = field(default_factory=dict)
    critical_setup: CriticalPath | None = None
    critical_hold: CriticalPath | None = None

    # --- area / cells ---
    area: float | None = None          # real mapped area (if Liberty used)
    area_total: float | None = None    # back-compat alias
    area_proxy: float | None = None    # cell-count or other proxy when no Liberty
    area_comb: float | None = None
    area_seq: float | None = None
    area_buffer: float | None = None
    cell_count: int | None = None
    ff_count: int | None = None
    comb_cell_count: int | None = None
    buf_count: int | None = None
    buffer_count: int | None = None

    # --- power (Step 10 §13: do NOT fabricate) ---
    power: float | None = None
    power_total: float | None = None   # back-compat alias
    power_dynamic: float | None = None
    power_leakage: float | None = None
    power_status: str = PowerStatus.UNAVAILABLE.value

    # --- constraint quality / validation (0.0 worst, 1.0 perfect) ---
    # Preserved across optimization: a candidate must not improve timing by
    # silently introducing validation errors or unsafe exceptions.
    constraint_quality: float | None = None  # None = not measured
    validation_errors: int = 0
    unsafe_exceptions: int = 0

    # --- timing-margin utilization (Step 11 §8/§9) ---
    # margin_headroom_ns   = WNS - required_margin (positive = usable slack)
    # margin_utilization   = fraction of usable headroom currently consumed
    #                        by candidate's tightening (0..1);
    #                        0 = baseline untouched, 1 = at required margin.
    margin_headroom_ns: float | None = None
    margin_utilization: float | None = None

    # --- experiment provenance (Step 10 cache / Step 11 candidate record) ---
    corner: str = "default"
    mode: str = "default"
    cache_key: str = ""
    cache_status: str = ""  # CACHE_HIT / MISS / N/A

    def __post_init__(self):
        # Keep area_total / area aliased.
        if self.area_total is None and self.area is not None:
            self.area_total = self.area
        elif self.area_total is not None and self.area is None:
            self.area = self.area_total
        if self.power_total is None and self.power is not None:
            self.power_total = self.power
        elif self.power_total is not None and self.power is None:
            self.power = self.power_total
        if self.buffer_count is None and self.buf_count is not None:
            self.buffer_count = self.buf_count
        elif self.buf_count is None and self.buffer_count is not None:
            self.buf_count = self.buffer_count

    # --- bookkeeping ---
    congestion: float | None = None
    runtime_seconds: float | None = None
    raw_reports: dict[str, Any] = field(default_factory=dict)
    tool: str = ""
    tool_version: str = ""
    notes: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    raw_report_text: str = ""
    feasibility: dict[str, Any] = field(default_factory=dict)

    def setup_feasible(self, required_margin_ns: float = 0.0) -> bool:
        return self.setup_wns is not None and self.setup_wns >= required_margin_ns * 1e-9

    def hold_feasible(self, required_margin_ns: float = 0.0) -> bool:
        return self.hold_wns is not None and self.hold_wns >= required_margin_ns * 1e-9

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "candidate_id": self.candidate_id,
            "backend": self.backend or self.tool, "is_mock": self.is_mock,
            "flow_stage": self.flow_stage, "scenario": self.scenario,
            "setup_wns_ns": (self.setup_wns * 1e9) if self.setup_wns is not None else None,
            "setup_tns_ns": (self.setup_tns * 1e9) if self.setup_tns is not None else None,
            "setup_violations": self.setup_violations,
            "hold_wns_ns": (self.hold_wns * 1e9) if self.hold_wns is not None else None,
            "hold_tns_ns": (self.hold_tns * 1e9) if self.hold_tns is not None else None,
            "hold_violations": self.hold_violations,
            "near_critical_count": self.near_critical_count,
            "path_count": self.path_count,
            "area": self.area, "area_proxy": self.area_proxy,
            "cell_count": self.cell_count, "ff_count": self.ff_count,
            "power": self.power, "power_status": self.power_status,
            "runtime_s": self.runtime_seconds, "tool": self.tool,
            "critical_setup": self.critical_setup.to_dict() if self.critical_setup else None,
            "critical_hold": self.critical_hold.to_dict() if self.critical_hold else None,
            "feasibility": self.feasibility,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.summary()
