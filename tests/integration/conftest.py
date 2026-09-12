"""End-to-end fixture helpers with real RCA parsing and explicit mock EDA."""

from __future__ import annotations

from pathlib import Path

import yaml

_COUNTER_RTL = """module counter(input logic clk, input logic rst_n, input logic en, output logic [7:0] q);
always_ff @(posedge clk or negedge rst_n) begin
  if (!rst_n) q <= '0;
  else if (en) q <= q + 1'b1;
end
endmodule
"""

_TWO_CLOCK_RTL = """module two_clock(input logic clk_a, input logic clk_b, input logic rst_n,
                        output logic qa, output logic qb);
always_ff @(posedge clk_a or negedge rst_n) if (!rst_n) qa <= 1'b0; else qa <= ~qa;
always_ff @(posedge clk_b or negedge rst_n) if (!rst_n) qb <= 1'b0; else qb <= qa;
endmodule
"""


def make_project(tmp_path: Path, *, name: str = "counter", mcmm: bool = False,
                 two_clock: bool = False, workers: int = 1) -> Path:
    """Write a fully local fixture project; all EDA execution is mock-only."""
    root = tmp_path / name
    rtl = root / "rtl"
    rtl.mkdir(parents=True)
    rtl_path = rtl / f"{name}.sv"
    rtl_path.write_text(_TWO_CLOCK_RTL if two_clock else _COUNTER_RTL, encoding="utf-8")
    top = "two_clock" if two_clock else "counter"
    clocks = ([
        {"name": "clk_a", "period": "10ns", "fixed": True},
        {"name": "clk_b", "period": "12ns", "fixed": True},
    ] if two_clock else [{"name": "clk", "period": "10ns", "fixed": True}])
    config = {
        "schema_version": "1.0",
        "project": {"name": name, "top": top},
        "sources": {"files": [str(rtl_path)], "include_dirs": [], "defines": []},
        "constraints": {
            "user": {
                "clocks": clocks,
                "io": {"inputs": {}, "outputs": {}},
            },
            "existing_sdc": [],
        },
        "analysis": {"language": "systemverilog", "safe_mode": "balanced"},
        "flow": {"backend": "generic", "stage": "pre_layout_sta", "output_dir": str(root / "output")},
        "optimization": {
            "enabled": True,
            "workers": workers,
            "max_iterations": 2,
            "max_eda_runs": 6,
            "max_runtime_minutes": 1,
            "convergence_patience": 2,
            "priorities": {
                "timing": "high", "area": "medium", "power": "medium",
                "timing_margin_utilization": "medium", "runtime": "low",
            },
        },
    }
    if mcmm:
        config["scenarios"] = [
            {"id": "FAST", "mode": "functional", "corner": "fast", "active": True},
            {"id": "SLOW", "mode": "functional", "corner": "slow", "active": True},
        ]
        config["mcmm"] = {"enabled": True, "active_scenario_ids": ["FAST", "SLOW"]}
    config_path = root / "project.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return config_path
