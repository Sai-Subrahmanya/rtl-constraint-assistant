"""Integration boundary tests: cache, semantic uncertainty, and artifacts."""

from __future__ import annotations

import json
import time
from pathlib import Path

import yaml
from typer.testing import CliRunner

from rca.cli.main import app
from rca.config.model import load_config
from rca.constraint_model import ConstraintSet
from rca.equivalence import compare_sdc_text
from rca.exceptions import formal_backend_from_config, verify_exceptions
from rca.reports.power import PowerParseStatus, parse_openroad_power_report
from rca.utils.enums import VerificationStatus
from rca.utils.hashing import hash_file

from .conftest import make_project

runner = CliRunner()


def _ok(*args: str):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result


def test_fake_real_toolchain_cache_reuse_invalidates_after_rtl_change_and_rejects_corruption(tmp_path, monkeypatch):
    """Use deterministic executable fixtures, never fake a real-tool result."""
    from tests.unit.test_eda import _write_fake_sta, _write_fake_yosys

    config_path = make_project(tmp_path)
    output = config_path.parent / "output"
    fake_yosys = tmp_path / "fake-yosys"
    fake_opensta = tmp_path / "fake-opensta"
    fake_lib = tmp_path / "fake.lib"
    _write_fake_yosys(fake_yosys)
    _write_fake_sta(fake_opensta)
    fake_lib.write_text("library(fake) { cell(DFF) { area : 1.0; } }\n", encoding="utf-8")
    config_data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config_data["flow"]["liberty"] = [str(fake_lib)]
    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("RCA_YOSYS", str(fake_yosys))
    monkeypatch.setenv("RCA_OPENSTA", str(fake_opensta))

    first = _ok("run-sta", str(config_path), "--backend", "yosys_opensta")
    assert "Status: SUCCESS" in first.output
    second = _ok("run-sta", str(config_path), "--backend", "yosys_opensta")
    assert "Status: CACHE_HIT" in second.output

    first_run = next((output / "runs").iterdir())
    manifest = json.loads((first_run / "run_manifest.json").read_text(encoding="utf-8"))
    netlist_path = first_run / manifest["artifacts"]["netlist"]
    netlist_path.write_text("// corrupt output artifact\n", encoding="utf-8")
    time.sleep(1.05)  # physical run IDs are second-granularity, not test identity
    after_corruption = _ok("run-sta", str(config_path), "--backend", "yosys_opensta")
    assert "Status: SUCCESS" in after_corruption.output
    assert "Status: CACHE_HIT" not in after_corruption.output

    rtl_path = next((config_path.parent / "rtl").glob("*.sv"))
    rtl_path.write_text(rtl_path.read_text(encoding="utf-8") + "\n// meaningful source change\n",
                        encoding="utf-8")
    time.sleep(1.05)
    after_rtl_change = _ok("run-sta", str(config_path), "--backend", "yosys_opensta")
    assert "Status: SUCCESS" in after_rtl_change.output
    manifests = list((output / "runs").glob("*/run_manifest.json"))
    assert len(manifests) >= 3


def test_semantic_unknown_different_and_power_reference_evidence(tmp_path):
    make_project(tmp_path)
    same = tmp_path / "same.sdc"
    different = tmp_path / "different.sdc"
    unsupported = tmp_path / "unsupported.sdc"
    same.write_text("create_clock -name clk -period 10 [get_ports clk]\n", encoding="utf-8")
    different.write_text("create_clock -name clk -period 11 [get_ports clk]\n", encoding="utf-8")
    unsupported.write_text("set_load 0.5 [get_ports q]\n", encoding="utf-8")
    equivalent = compare_sdc_text(same.read_text(), same.read_text(), source_a=str(same), source_b=str(same))
    assert equivalent.overall_status.value.startswith("EQUIVALENT")
    changed = compare_sdc_text(same.read_text(), different.read_text(), source_a=str(same), source_b=str(different))
    assert changed.overall_status.value in {"DIFFERENT", "NON_EQUIVALENT"}
    unknown = compare_sdc_text(unsupported.read_text(), unsupported.read_text(),
                               source_a=str(unsupported), source_b=str(unsupported))
    assert unknown.overall_status.value == "UNKNOWN"

    power_fixture = Path(__file__).parents[1] / "golden" / "reports" / "openroad_report_power_representative.rpt"
    parsed = parse_openroad_power_report(power_fixture, scenario_id="SLOW", mode="functional", corner="slow")
    assert parsed.status == PowerParseStatus.AVAILABLE.value
    assert parsed.source_sha256 == hash_file(power_fixture)
    assert parsed.total == 1.33e-3


def test_malformed_config_missing_source_and_formal_unavailable_are_explicit(tmp_path):
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("project: [not-an-object]\n", encoding="utf-8")
    try:
        load_config(malformed)
    except ValueError as exc:
        assert "Invalid project config" in str(exc)
    else:  # pragma: no cover - safety assertion for the failure matrix
        raise AssertionError("malformed configuration must not load")

    config = make_project(tmp_path, name="missing")
    source = next((config.parent / "rtl").glob("*.sv"))
    source.unlink()
    failed = runner.invoke(app, ["analyze", str(config)])
    assert failed.exit_code != 0

    # An explicitly configured but absent SBY executable remains unresolved;
    # RCA does not simulate a proof or promote structural evidence to VERIFIED.
    formal_project = make_project(tmp_path, name="formal")
    proof = formal_project.parent / "exception.sby"
    proof.write_text("[options]\nmode prove\n", encoding="utf-8")
    text = formal_project.read_text(encoding="utf-8")
    formal_project.write_text(
        text + "\nformal:\n  backend: symbiyosys\n  symbiyosys_executable: missing-sby\n"
        f"  proofs:\n    - constraint_id: FP0001\n      exception_kind: false_path\n      sby_file: {proof}\n",
        encoding="utf-8",
    )
    formal_sdc = formal_project.parent / "exception.sdc"
    formal_sdc.write_text("set_false_path -from [get_ports clk] -to [get_pins q]\n", encoding="utf-8")
    validation = _ok("validate", str(formal_project), "--sdc", str(formal_sdc))
    assert "Validation Report" in validation.output
    cli_report = json.loads((formal_project.parent / "output" / "validation_report.json").read_text(encoding="utf-8"))
    assert cli_report["infos"] >= 1  # unresolved formal exception is reported, not fabricated.
    formal_cset = ConstraintSet(name="formal")
    formal_cset.create_false_path(from_set=["counter/q"], to_set=["counter/q"])
    verification = verify_exceptions(
        formal_cset, backend=formal_backend_from_config(load_config(formal_project).formal),
    )
    outcome = next(iter(verification))
    assert outcome.verification_status == VerificationStatus.UNRESOLVED
    assert outcome.verification.evidence["reason"] == "tool_unavailable"
