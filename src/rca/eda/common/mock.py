"""
Mock EDA backend for testing (Step 10 §20).

Clearly labeled MOCK results; never confused with real STA. Used for
unit tests, offline development, and missing-tool tests.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...qor.model import Feasibility, PowerStatus, QoRResult
from ...utils.enums import RunStatus
from ..base import ToolBackend, ToolInfo

if TYPE_CHECKING:
    from ...optimizer import Candidate


@dataclass
class MockEDA(ToolBackend):
    name = "mock"
    seed: int = 42
    base_area: float = 100.0
    base_power: float = 100.0

    def discover(self) -> ToolInfo:
        return ToolInfo(vendor="RCA", tool="mock", version="0.1",
                        executable="mock", available=True,
                        capabilities={"sta": True, "synthesis": True, "mock": True})

    def evaluate_candidate(self, cand: "Candidate", work_dir: Path) -> QoRResult:
        """Return synthetic QoR for optimizer testing (clearly labeled)."""
        rng = random.Random(f"{cand.id}-{self.seed}")
        n = len(cand.constraint_set) if cand.constraint_set else 0
        setup_wns = (0.50 - 0.05 * n) + rng.uniform(-0.05, 0.05)
        hold_wns = 0.20 + rng.uniform(-0.05, 0.05)
        if cand.generated_changes and any("false_path" in c for c in cand.generated_changes):
            setup_wns += 0.80
        area = self.base_area + rng.uniform(-2, 5) + 0.3 * n
        setup = setup_wns * 1e-9
        hold = hold_wns * 1e-9
        qor = QoRResult(
            backend="mock", is_mock=True, tool="mock", tool_version="0.1",
            flow_stage="synthesis_sta",
            setup_wns=setup, setup_tns=min(0.0, setup), setup_violations=0 if setup >= 0 else 1,
            hold_wns=hold, hold_tns=min(0.0, hold), hold_violations=0 if hold >= 0 else 1,
            area_proxy=area, cell_count=50 + n, ff_count=25,
            power=None, power_status=PowerStatus.UNAVAILABLE.value,
            notes=["MOCK result — not from real EDA tools."],
        )
        qor.feasibility = Feasibility.from_qor(qor).to_dict()
        return qor

    def synthesize(self, sources, top, liberty, work_dir, sdc_out=None, extra_args=None):
        netlist = work_dir / f"{top}_synth.v"
        netlist.write_text(f"// mock netlist for {top}\n", encoding="utf-8")
        return netlist

    def run_sta(self, netlist, sdc, liberty, work_dir, top, corner="default", extra_args=None):
        qor = QoRResult(
            backend="mock", is_mock=True, tool="mock", tool_version="0.1",
            flow_stage="synthesis_sta", scenario=corner,
            setup_wns=0.0, setup_tns=0.0, hold_wns=0.0, hold_tns=0.0,
            area_proxy=100.0, cell_count=50, ff_count=25,
            power=None, power_status=PowerStatus.UNAVAILABLE.value,
            notes=["MOCK result — no real OpenSTA ran."],
        )
        qor.feasibility = Feasibility(qor.setup_wns >= 0, qor.hold_wns >= 0, True,
                                       False, "", RunStatus.MOCK.value).to_dict()
        return qor
