"""Step 25 unit contracts for typed real-EDA preflight evidence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import rca.eda.preflight as preflight_module
from rca.artifacts import RunManifest
from rca.cli.main import app
from rca.eda import (
    CapabilityClassification,
    CapabilityStatus,
    ToolInfo,
    preflight_symbiyosys,
    preflight_yosys_opensta,
)


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _info(name: str, executable: Path, version: str = "test-1.0") -> ToolInfo:
    return ToolInfo(vendor="test", tool=name, version=version, executable=str(executable), available=True)


def _preflight(tmp_path: Path, *, yosys: ToolInfo | None = None,
               opensta: ToolInfo | None = None, backend: str = "yosys_opensta",
               expected_outputs: list[Path] | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "top.v"
    liberty = tmp_path / "cells.lib"
    sdc = tmp_path / "generated.sdc"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    liberty.write_text("library(test) {}\n", encoding="utf-8")
    sdc.write_text("create_clock -name clk -period 10 clk\n", encoding="utf-8")
    yosys_executable = _executable(tmp_path / "yosys")
    opensta_executable = _executable(tmp_path / "sta")
    return preflight_yosys_opensta(
        backend=backend,
        top="top",
        sources=[source],
        liberty=[liberty],
        sdc_path=sdc,
        output_dir=tmp_path,
        expected_outputs=expected_outputs or [tmp_path / "top_synth.v", tmp_path / "top.setup.rpt"],
        yosys_info=yosys or _info("yosys", yosys_executable),
        opensta_info=opensta or _info("opensta", opensta_executable),
        liberty_hashes={str(liberty): "lib-hash"},
        config_hash="config-hash",
    )


def _status(preflight, component: str) -> CapabilityStatus:
    return next(check.status for check in preflight.checks if check.component == component)


def _classification(preflight, component: str) -> CapabilityClassification:
    return next(check.classification for check in preflight.checks if check.component == component)


def test_preflight_success_is_component_granular_and_fingerprint_deterministic(tmp_path, monkeypatch):
    monkeypatch.setenv("RCA_TEST_SECRET", "must-not-appear")
    first = _preflight(tmp_path)
    second = _preflight(tmp_path)
    assert first.ready
    assert first.overall_status == CapabilityStatus.ENVIRONMENT_READY
    assert _status(first, "yosys") == CapabilityStatus.VERSION_DISCOVERED
    assert _status(first, "opensta") == CapabilityStatus.VERSION_DISCOVERED
    assert _status(first, "liberty") == CapabilityStatus.ENVIRONMENT_READY
    assert _status(first, "generated_sdc") == CapabilityStatus.ENVIRONMENT_READY
    assert _classification(first, "yosys") == CapabilityClassification.AVAILABLE
    assert first.environment_fingerprint == second.environment_fingerprint
    assert "must-not-appear" not in str(first.to_dict())


@pytest.mark.parametrize(
    ("component", "missing_name", "expected"),
    [
        ("yosys", "missing-yosys", CapabilityStatus.EXECUTABLE_MISSING),
        ("opensta", "missing-opensta", CapabilityStatus.EXECUTABLE_MISSING),
    ],
)
def test_preflight_distinguishes_each_missing_tool(tmp_path, component, missing_name, expected):
    missing = ToolInfo(vendor="test", tool=component, version="unknown",
                       executable=str(tmp_path / missing_name), available=False,
                       error="controlled missing executable")
    kwargs = {component: missing}
    result = _preflight(tmp_path, **kwargs)
    assert not result.ready
    assert _status(result, component) == expected
    assert _classification(result, component) == CapabilityClassification.TOOL_MISSING
    assert result.failure_classification == expected.value


def test_preflight_distinguishes_missing_rtl_and_sdc_inputs(tmp_path):
    _preflight(tmp_path)
    result = preflight_yosys_opensta(
        backend="yosys_opensta", top="top", sources=[tmp_path / "missing.v"],
        liberty=[tmp_path / "cells.lib"], sdc_path=tmp_path / "missing.sdc", output_dir=tmp_path,
        expected_outputs=[tmp_path / "top_synth.v"],
        yosys_info=_info("yosys", tmp_path / "yosys"),
        opensta_info=_info("opensta", tmp_path / "sta"),
        liberty_hashes={}, config_hash="config-hash",
    )
    assert not result.ready
    assert _status(result, "rtl_sources") == CapabilityStatus.COLLATERAL_MISSING
    assert _status(result, "generated_sdc") == CapabilityStatus.COLLATERAL_MISSING
    assert _classification(result, "rtl_sources") == CapabilityClassification.INPUT_MISSING
    assert _classification(result, "generated_sdc") == CapabilityClassification.INPUT_MISSING


def test_preflight_rejects_a_non_executable_tool_file(tmp_path):
    non_executable = tmp_path / "not-executable-yosys"
    non_executable.write_text("not executable\n", encoding="utf-8")
    info = ToolInfo(vendor="test", tool="yosys", version="unknown",
                    executable=str(non_executable), available=False)
    result = _preflight(tmp_path, yosys=info)
    assert not result.ready
    assert _status(result, "yosys") == CapabilityStatus.EXECUTABLE_MISSING
    assert _classification(result, "yosys") == CapabilityClassification.TOOL_MISSING


def test_preflight_fails_closed_when_a_found_executable_has_no_safe_version_probe(tmp_path):
    executable = _executable(tmp_path / "unprobeable-yosys")
    failed_probe = ToolInfo(
        vendor="test", tool="yosys", version="unknown", executable=str(executable),
        available=False, error="controlled version probe timeout",
    )
    result = _preflight(tmp_path, yosys=failed_probe)
    assert not result.ready
    assert _status(result, "yosys") == CapabilityStatus.EXECUTABLE_FOUND
    assert _classification(result, "yosys") == CapabilityClassification.TOOL_UNAVAILABLE
    assert result.failure_classification == CapabilityStatus.EXECUTABLE_FOUND.value


def test_preflight_rejects_missing_collateral_and_unsupported_execution_backend(tmp_path):
    _preflight(tmp_path)
    liberty = tmp_path / "cells.lib"
    liberty.unlink()
    missing_lib = preflight_yosys_opensta(
        backend="yosys_opensta", top="top", sources=[tmp_path / "top.v"], liberty=[liberty],
        sdc_path=tmp_path / "generated.sdc", output_dir=tmp_path,
        expected_outputs=[tmp_path / "top_synth.v"],
        yosys_info=_info("yosys", _executable(tmp_path / "yosys-again")),
        opensta_info=_info("opensta", _executable(tmp_path / "sta-again")),
        liberty_hashes={}, config_hash="config-hash",
    )
    assert not missing_lib.ready
    assert _status(missing_lib, "liberty") == CapabilityStatus.COLLATERAL_MISSING
    assert _classification(missing_lib, "liberty") == CapabilityClassification.LIBERTY_MISSING

    _preflight(tmp_path / "include")
    missing_include = preflight_yosys_opensta(
        backend="yosys_opensta", top="top", sources=[tmp_path / "include" / "top.v"],
        liberty=[tmp_path / "include" / "cells.lib"], sdc_path=tmp_path / "include" / "generated.sdc",
        output_dir=tmp_path / "include", expected_outputs=[tmp_path / "include" / "top_synth.v"],
        yosys_info=_info("yosys", _executable(tmp_path / "include" / "yosys")),
        opensta_info=_info("opensta", _executable(tmp_path / "include" / "sta")),
        liberty_hashes={}, config_hash="config-hash", include_dirs=[tmp_path / "does-not-exist"],
    )
    assert not missing_include.ready
    assert _status(missing_include, "include_directories") == CapabilityStatus.COLLATERAL_MISSING

    unsupported = _preflight(tmp_path / "unsupported", backend="synopsys")
    assert not unsupported.ready
    assert _status(unsupported, "backend") == CapabilityStatus.UNSUPPORTED


def test_preflight_distinguishes_unreadable_collateral_as_permission_error(tmp_path, monkeypatch):
    _preflight(tmp_path)
    source = tmp_path / "top.v"
    original_access = preflight_module.os.access

    def deny_source(path, mode):
        if Path(path) == source and mode == os.R_OK:
            return False
        return original_access(path, mode)

    monkeypatch.setattr(preflight_module.os, "access", deny_source)
    result = _preflight(tmp_path)
    assert not result.ready
    assert _status(result, "rtl_sources") == CapabilityStatus.PERMISSION_ERROR
    assert _classification(result, "rtl_sources") == CapabilityClassification.PERMISSION_ERROR


def test_preflight_rejects_an_expected_output_path_that_is_a_directory(tmp_path):
    output_path = tmp_path / "not-a-file"
    output_path.mkdir()
    result = _preflight(tmp_path, expected_outputs=[output_path])
    assert not result.ready
    assert _status(result, "expected_outputs") == CapabilityStatus.CONFIGURATION_INVALID
    assert _classification(result, "expected_outputs") == CapabilityClassification.ENVIRONMENT_INVALID


def test_formal_preflight_requires_explicit_sby_collateral_only_when_selected(tmp_path):
    source = tmp_path / "top.v"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    disabled = preflight_symbiyosys(
        executable=str(tmp_path / "missing-sby"), version=None, proofs=[], sources=[source], required=False,
    )
    assert disabled.ready
    selected = preflight_symbiyosys(
        executable=str(tmp_path / "missing-sby"), version=None, proofs=[], sources=[source], required=True,
    )
    assert not selected.ready
    assert _status(selected, "symbiyosys") == CapabilityStatus.EXECUTABLE_MISSING
    assert _status(selected, "sby_collateral") == CapabilityStatus.COLLATERAL_MISSING


def test_manifest_execution_and_environment_fields_are_backward_compatible():
    manifest = RunManifest(
        execution_mode="REAL",
        execution_status="STA_FAILED",
        failure_classification="output_missing",
        environment_fingerprint="environment-hash",
    )
    restored = RunManifest.from_dict(manifest.to_dict())
    assert restored.execution_mode == "REAL"
    assert restored.execution_status == "STA_FAILED"
    assert restored.failure_classification == "output_missing"
    assert restored.environment_fingerprint == "environment-hash"


def test_doctor_json_is_machine_readable_and_reports_invalid_config(tmp_path):
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("project: [invalid]\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["doctor", str(invalid), "--json"])
    assert result.exit_code == 0, result.output
    import json

    data = json.loads(result.output)
    assert data["kind"] == "rca_doctor"
    assert data["configuration"]["error"]
    assert data["mock_policy"] == "mock is available only when explicitly selected"
