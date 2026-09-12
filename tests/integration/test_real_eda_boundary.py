"""Step 25 fake-executable flows for the real EDA execution boundary.

These programs are deliberately test fixtures.  They exercise RCA's real-tool
subprocess contract but are never represented as actual Yosys/OpenSTA results.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rca.config.model import FlowConfig, ProjectConfig, ProjectInfo, SourceConfig
from rca.constraint_model import ConstraintSet
from rca.eda import run_flow
from rca.utils.enums import RunStatus


def _write_fake_yosys(path: Path, mode: str = "success", version: str = "Yosys fake 1.0") -> Path:
    path.write_text(
        f"""#!/usr/bin/env python3
import pathlib
import re
import sys
import time
MODE = {mode!r}
if '-V' in sys.argv:
    print({version!r})
    raise SystemExit(0)
if MODE == 'timeout':
    time.sleep(3)
if MODE == 'failure':
    print('controlled Yosys failure', file=sys.stderr)
    raise SystemExit(7)
script = pathlib.Path(sys.argv[sys.argv.index('-s') + 1])
match = re.search(r'write_verilog(?:\\s+\\S+)*\\s+(\\S+)', script.read_text())
if MODE != 'missing_output' and match:
    output = pathlib.Path(match.group(1))
    if not output.is_absolute():
        output = pathlib.Path.cwd() / output
    output.write_text('module top(input clk, output q); assign q = clk; endmodule\\n')
print('Number of cells: 1')
print("Chip area for module 'top': 1.0")
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _write_fake_opensta(path: Path, mode: str = "success", version: str = "OpenSTA fake 1.0") -> Path:
    path.write_text(
        f"""#!/usr/bin/env python3
import pathlib
import re
import sys
import time
MODE = {mode!r}
if '-version' in sys.argv or '--version' in sys.argv:
    print({version!r})
    raise SystemExit(0)
if MODE == 'timeout':
    time.sleep(3)
if MODE == 'failure':
    print('controlled OpenSTA failure', file=sys.stderr)
    raise SystemExit(9)
tcl = pathlib.Path(sys.argv[-1]).read_text()
outputs = [match.group(1) for match in re.finditer(r'>\\s*(\\S+)', tcl)]
for name in outputs:
    target = pathlib.Path.cwd() / name
    if MODE == 'missing_output' and 'hold.rpt' in name:
        continue
    if MODE == 'malformed':
        target.write_text('this is not an OpenSTA timing report\\n')
    elif 'setup.rpt' in name:
        target.write_text('Path Type: max\\nslack (MET) 1.0\\n')
    elif 'hold.rpt' in name:
        target.write_text('Path Type: min\\nslack (MET) 0.2\\n')
    elif 'setup.wns' in name:
        target.write_text('wns 1.0\\n')
    elif 'setup.tns' in name:
        target.write_text('tns 0.0\\n')
    elif 'hold.wns' in name:
        target.write_text('wns 0.2\\n')
    elif 'hold.tns' in name:
        target.write_text('tns 0.0\\n')
    else:
        target.write_text('No setup violations.\\n')
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _project(tmp_path: Path, *, liberty: bool = True, timeout: int = 5,
             power_report: Path | None = None) -> tuple[ProjectConfig, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "top.v"
    source.write_text("module top(input clk, output reg q); always @(posedge clk) q <= 1'b0; endmodule\n")
    lib = tmp_path / "cells.lib"
    if liberty:
        lib.write_text("library(cells) { cell(DFF) { area : 1.0; } }\n")
    flow = FlowConfig(
        backend="yosys_opensta", output_dir=str(tmp_path / "output"),
        liberty=[str(lib)] if liberty else [], tool_timeout_seconds=timeout,
        power_reports=([{"path": str(power_report), "format": "openroad_report_power"}]
                       if power_report else []),
    )
    cfg = ProjectConfig(
        project=ProjectInfo(name="fake-real-boundary", top="top"),
        sources=SourceConfig(files=[str(source)]),
        flow=flow,
    )
    return cfg, source, lib


def _run(tmp_path: Path, *, yosys_mode: str = "success", sta_mode: str = "success",
         run_id: str = "run", liberty: bool = True, timeout: int = 5,
         power_report: Path | None = None, yosys_version: str = "Yosys fake 1.0",
         sta_version: str = "OpenSTA fake 1.0", force: bool = True):
    cfg, source, _ = _project(tmp_path, liberty=liberty, timeout=timeout, power_report=power_report)
    y = _write_fake_yosys(tmp_path / "fake-yosys", yosys_mode, yosys_version)
    s = _write_fake_opensta(tmp_path / "fake-sta", sta_mode, sta_version)
    return run_flow(
        cfg, ConstraintSet(name="top"), "create_clock -name clk -period 10 [get_ports clk]\n",
        "COMPLETE", [source], output_dir=Path(cfg.flow.output_dir), backend="yosys_opensta",
        run_id=run_id, yosys_bin=str(y), sta_bin=str(s), force=force,
    ), cfg, source, y, s


def _manifest_path(result: dict) -> Path:
    return Path(result["run_dir"]) / "run_manifest.json"


def test_fake_real_success_records_preflight_commands_fingerprint_and_no_environment_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("RCA_BOUNDARY_SECRET", "not-for-manifest")
    result, _, _, _, _ = _run(tmp_path, run_id="success")
    assert result["status"] == RunStatus.SUCCESS.value
    assert result["qor_result"] is not None and result["qor_result"].is_mock is False
    manifest = json.loads(_manifest_path(result).read_text(encoding="utf-8"))
    assert not (Path(result["run_dir"]) / "manifest.json").exists()
    assert manifest["execution_mode"] == "REAL"
    assert manifest["execution_status"] == RunStatus.SUCCESS.value
    assert manifest["failure_classification"] == ""
    assert len(manifest["environment_fingerprint"]) == 64
    assert manifest["extra"]["preflight"]["ready"] is True
    assert manifest["extra"]["commands"]["yosys"]["argv"][1] == "-s"
    assert manifest["extra"]["commands"]["yosys"]["timed_out"] is False
    assert all(Path(result["run_dir"], relative).is_file() for relative in manifest["artifacts"].values())
    assert "not-for-manifest" not in json.dumps(manifest)
    assert manifest["artifact_hashes"]["sta_setup"]
    assert manifest["artifact_hashes"]["sta_hold"]


@pytest.mark.parametrize(
    ("yosys_mode", "sta_mode", "expected_status", "classification"),
    [
        ("failure", "success", RunStatus.SYNTHESIS_FAILED.value, "execution_failed"),
        ("missing_output", "success", RunStatus.SYNTHESIS_FAILED.value, "output_missing"),
        ("timeout", "success", RunStatus.SYNTHESIS_FAILED.value, "execution_timed_out"),
        ("success", "failure", RunStatus.STA_FAILED.value, "execution_failed"),
        ("success", "missing_output", RunStatus.STA_FAILED.value, "output_missing"),
        ("success", "malformed", RunStatus.STA_FAILED.value, "output_malformed"),
        ("success", "timeout", RunStatus.STA_FAILED.value, "execution_timed_out"),
    ],
)
def test_failed_fake_real_invocations_never_return_successful_qor(tmp_path, yosys_mode, sta_mode,
                                                                    expected_status, classification):
    result, _, _, _, _ = _run(
        tmp_path, yosys_mode=yosys_mode, sta_mode=sta_mode, timeout=1, run_id="failure",
    )
    assert result["status"] == expected_status
    assert result["qor_result"] is None
    manifest = result["manifest"]
    assert manifest["execution_status"] == expected_status
    assert manifest["failure_classification"] == classification
    assert "qor" not in manifest["artifacts"]
    command = ((result.get("synth") or {}).get("command") if yosys_mode == "timeout"
               else (result.get("sta") or {}).get("command"))
    if "timed_out" in classification:
        assert command["timed_out"] is True


def test_explicit_mock_execution_remains_separately_labelled(tmp_path):
    cfg, source, _ = _project(tmp_path)
    result = run_flow(
        cfg, ConstraintSet(name="top"), "create_clock -name clk -period 10 [get_ports clk]\n", "COMPLETE",
        [source], output_dir=Path(cfg.flow.output_dir), backend="mock", run_id="explicit-mock",
    )
    assert result["status"] == RunStatus.MOCK.value
    assert result["qor_result"].is_mock is True
    assert result["manifest"]["execution_mode"] == "MOCK"
    assert result["manifest"]["tool"] == "mock"
    assert result["preflight"] is None


def test_commercial_execution_is_explicitly_unsupported_not_emulated(tmp_path):
    cfg, source, _ = _project(tmp_path)
    y = _write_fake_yosys(tmp_path / "fake-yosys")
    s = _write_fake_opensta(tmp_path / "fake-sta")
    result = run_flow(
        cfg, ConstraintSet(name="top"), "create_clock -name clk -period 10 [get_ports clk]\n", "COMPLETE",
        [source], output_dir=Path(cfg.flow.output_dir), backend="synopsys", run_id="commercial",
        yosys_bin=str(y), sta_bin=str(s), force=True,
    )
    assert result["status"] == RunStatus.BLOCKED.value
    assert result["qor_result"] is None
    assert result["manifest"]["tool"] == "synopsys"
    assert result["manifest"]["failure_classification"] == "unsupported"
    assert not (Path(result["run_dir"]) / "top_synth.v").exists()
    assert next(check for check in result["preflight"]["checks"]
                if check["component"] == "backend")["status"] == "unsupported"


def test_missing_liberty_fails_preflight_closed_without_mock_fallback(tmp_path):
    result, _, _, _, _ = _run(tmp_path, liberty=False, run_id="missing-liberty")
    assert result["status"] == RunStatus.BLOCKED.value
    assert result["qor"] is None
    assert result["manifest"]["failure_classification"] == "collateral_missing"
    assert result["preflight"]["ready"] is False
    assert any("PREFLIGHT liberty: collateral_missing" in item for item in result["diagnostics"])
    assert result["manifest"]["tool"] == "yosys_opensta"


def test_valid_and_malformed_power_evidence_never_becomes_fabricated_power(tmp_path):
    reference = Path(__file__).parents[1] / "golden" / "reports" / "openroad_report_power_representative.rpt"
    valid_power = tmp_path / "valid-power.rpt"
    valid_power.write_text(reference.read_text(encoding="utf-8"), encoding="utf-8")
    valid, _, _, _, _ = _run(tmp_path / "valid", power_report=valid_power, run_id="valid-power")
    assert valid["status"] == RunStatus.SUCCESS.value
    assert valid["qor_result"].power_status == "AVAILABLE"
    assert "power_report" in valid["manifest"]["artifacts"]
    assert valid["manifest"]["artifact_hashes"]["power_report"]

    malformed_power = tmp_path / "malformed-power.rpt"
    malformed_power.write_text("not a report_power result\n", encoding="utf-8")
    malformed, _, _, _, _ = _run(tmp_path / "malformed", power_report=malformed_power,
                                  run_id="malformed-power")
    assert malformed["status"] == RunStatus.SUCCESS.value
    assert malformed["qor_result"].power_status == "UNAVAILABLE"
    assert malformed["qor_result"].power is None


def test_stale_timing_and_power_outputs_are_removed_before_failed_opensta_run(tmp_path):
    power = tmp_path / "configured-power.rpt"
    power.write_text("Power (Watts)\nTotal 1e-3 2e-3 3e-6 3.003e-3\n")
    run_dir = tmp_path / "output" / "runs" / "stale"
    run_dir.mkdir(parents=True)
    for name in ("top.default.setup.rpt", "top.default.hold.rpt", "configured_power_report.rpt"):
        (run_dir / name).write_text("stale report that must not be consumed\n")
    result, _, _, _, _ = _run(
        tmp_path, sta_mode="failure", power_report=power, run_id="stale", force=True,
    )
    assert result["status"] == RunStatus.STA_FAILED.value
    assert result["qor_result"] is None
    assert not (run_dir / "top.default.setup.rpt").exists()
    assert not (run_dir / "top.default.hold.rpt").exists()
    assert not (run_dir / "configured_power_report.rpt").exists()
    assert "sta_setup" not in result["manifest"]["artifacts"]
    assert "power_report" not in result["manifest"]["artifacts"]


def test_stale_synthesis_and_qor_outputs_are_removed_before_failed_yosys_run(tmp_path):
    run_dir = tmp_path / "output" / "runs" / "stale-yosys"
    run_dir.mkdir(parents=True)
    for name in ("top_synth.v", "synthesis_stats.json", "qor.json", "configured_power_report.rpt"):
        (run_dir / name).write_text("stale evidence that must not be consumed\n", encoding="utf-8")
    result, _, _, _, _ = _run(
        tmp_path, yosys_mode="failure", run_id="stale-yosys", force=True,
    )
    assert result["status"] == RunStatus.SYNTHESIS_FAILED.value
    assert result["qor_result"] is None
    assert all(not (run_dir / name).exists()
               for name in ("top_synth.v", "synthesis_stats.json", "qor.json", "configured_power_report.rpt"))
    assert "netlist" not in result["manifest"]["artifacts"]
    assert "qor" not in result["manifest"]["artifacts"]
    assert result["manifest"]["extra"]["commands"]["yosys"]["execution_status"] == "execution_failed"


def test_cache_reuse_input_and_tool_version_changes_and_failed_runs_never_create_false_hits(tmp_path):
    first, cfg, source, y, s = _run(tmp_path, run_id="first", force=False)
    assert first["status"] == RunStatus.SUCCESS.value
    second = run_flow(
        cfg, ConstraintSet(name="top"), "create_clock -name clk -period 10 [get_ports clk]\n", "COMPLETE",
        [source], output_dir=Path(cfg.flow.output_dir), backend="yosys_opensta", run_id="second",
        yosys_bin=str(y), sta_bin=str(s), force=False,
    )
    assert second["status"] == RunStatus.CACHE_HIT.value
    source.write_text(source.read_text(encoding="utf-8") + "// source identity change\n", encoding="utf-8")
    changed = run_flow(
        cfg, ConstraintSet(name="top"), "create_clock -name clk -period 10 [get_ports clk]\n", "COMPLETE",
        [source], output_dir=Path(cfg.flow.output_dir), backend="yosys_opensta", run_id="changed",
        yosys_bin=str(y), sta_bin=str(s), force=False,
    )
    assert changed["status"] == RunStatus.SUCCESS.value
    _write_fake_yosys(y, version="Yosys fake 2.0")
    version_changed = run_flow(
        cfg, ConstraintSet(name="top"), "create_clock -name clk -period 10 [get_ports clk]\n", "COMPLETE",
        [source], output_dir=Path(cfg.flow.output_dir), backend="yosys_opensta", run_id="version-changed",
        yosys_bin=str(y), sta_bin=str(s), force=False,
    )
    assert version_changed["status"] == RunStatus.SUCCESS.value
    failed, _, _, _, _ = _run(tmp_path / "failed-cache", yosys_mode="failure", run_id="failed", force=False)
    assert failed["status"] == RunStatus.SYNTHESIS_FAILED.value
    recovered, _, _, _, _ = _run(tmp_path / "failed-cache", run_id="recovered", force=False)
    assert recovered["status"] == RunStatus.SUCCESS.value
    assert recovered["status"] != RunStatus.CACHE_HIT.value
