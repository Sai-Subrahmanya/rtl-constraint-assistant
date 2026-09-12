"""Opt-in real-EDA integration boundary; never substitutes a fake result."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from rca.config.model import FlowConfig, ProjectConfig, ProjectInfo, SourceConfig
from rca.constraint_model import ConstraintSet
from rca.eda import run_flow
from rca.exceptions import SymbiYosysFormalBackend
from rca.utils.enums import RunStatus

pytestmark = pytest.mark.optional_real_eda


def _require_real_eda() -> None:
    if os.environ.get("RCA_RUN_REAL_EDA") != "1":
        pytest.skip("optional real EDA disabled; set RCA_RUN_REAL_EDA=1 to opt in")


def test_real_yosys_opensta_flow_when_tools_and_liberty_are_explicitly_available(tmp_path):
    _require_real_eda()
    yosys = shutil.which(os.environ.get("RCA_YOSYS", "yosys"))
    opensta = shutil.which(os.environ.get("RCA_OPENSTA", "sta"))
    liberty_text = os.environ.get("RCA_REAL_EDA_LIBERTY")
    if not yosys or not opensta:
        pytest.skip("real Yosys/OpenSTA pair is unavailable")
    if not liberty_text or not Path(liberty_text).is_file():
        pytest.skip("RCA_REAL_EDA_LIBERTY must name a readable Liberty file")

    source = tmp_path / "top.v"
    source.write_text("module top(input clk, output reg q); always @(posedge clk) q <= 1'b0; endmodule\n")
    cfg = ProjectConfig(
        project=ProjectInfo(name="optional-real-eda", top="top"),
        sources=SourceConfig(files=[str(source)]),
        flow=FlowConfig(output_dir=str(tmp_path / "output"), liberty=[liberty_text]),
    )
    cset = ConstraintSet(name="top")
    cset.create_clock(name="clk", period_seconds=10e-9, source="clk")
    result = run_flow(
        cfg, cset, "create_clock -name clk -period 10 [get_ports clk]\n", "COMPLETE", [source],
        output_dir=Path(cfg.flow.output_dir), backend="yosys_opensta", run_id="real-optional",
        yosys_bin=yosys, sta_bin=opensta,
    )
    assert result["status"] in {RunStatus.SUCCESS.value, RunStatus.TIMING_FAIL.value}, result
    assert result["qor_result"] is not None
    assert result["qor_result"].is_mock is False


def test_real_symbiyosys_version_probe_when_opted_in(tmp_path):
    _require_real_eda()
    executable = shutil.which(os.environ.get("RCA_SYMBIYOSYS", "sby"))
    if not executable:
        pytest.skip("SymbiYosys executable is unavailable")
    backend = SymbiYosysFormalBackend(executable=executable, work_dir=tmp_path)
    version = backend.get_version()
    assert version, "available real SymbiYosys must return a version string"
