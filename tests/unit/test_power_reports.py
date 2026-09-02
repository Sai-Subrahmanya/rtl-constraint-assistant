"""Step 20 — conservative OpenROAD/OpenSTA power-report ingestion tests.

The one tracked fixture is representative test data only.  Malformed and
ambiguous cases are derived in-memory or in pytest temporary directories so
this suite does not grow a duplicate fixture corpus or claim a tool run.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rca.config.model import (
    FlowConfig,
    MCMMConfig,
    PowerReportConfig,
    ProjectConfig,
    ProjectInfo,
    ScenarioSpec,
    SourceConfig,
    load_config,
)
from rca.constraint_model import ConstraintSet
from rca.eda.flow import run_flow
from rca.mcmm import MCMMEvaluator, build_scenario_matrix
from rca.optimizer import Candidate
from rca.qor.model import QoRResult
from rca.qor.objectives import is_dominating, objective_vector
from rca.reports.power import (
    POWER_REPORT_FORMAT,
    POWER_REPORT_PARSER_VERSION,
    PowerParseStatus,
    parse_openroad_power_report,
    parse_openroad_power_text,
)
from rca.utils.enums import PowerStatus, RunStatus
from rca.utils.hashing import hash_file

_FIXTURE = Path(__file__).parents[1] / "golden" / "reports" / "openroad_report_power_representative.rpt"


def _fixture_text() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


def _report_with_unit(unit: str, *, internal: str = "1", switching: str = "2",
                      leakage: str = "3", total: str = "6") -> str:
    """Construct a temporary real-format representative table in one unit."""
    return "\n".join([
        "# Generated test variant; not a live tool result.",
        "report_power",
        "Group                  Internal  Switching    Leakage      Total",
        f"                          Power      Power      Power ({unit})",
        "---------------------------------------------------------------",
        f"Total                  {internal}          {switching}          {leakage}          {total} 100.0%",
        "",
    ])


# ---------------------------------------------------------------------------
# Focused parser semantics
# ---------------------------------------------------------------------------

def test_power_fixture_parses_total_dynamic_and_leakage_with_provenance():
    parsed = parse_openroad_power_report(_FIXTURE, scenario_id="FUNC_SLOW",
                                         mode="functional", corner="slow")
    assert parsed.status == PowerParseStatus.AVAILABLE.value
    assert parsed.total == pytest.approx(1.33e-3)
    assert parsed.dynamic == pytest.approx(8.57e-4 + 4.56e-4)
    assert parsed.leakage == pytest.approx(1.30e-5)
    provenance = parsed.provenance()
    assert provenance["format"] == POWER_REPORT_FORMAT
    assert provenance["parser_format_version"] == POWER_REPORT_PARSER_VERSION
    assert provenance["original_unit"] == "Watts"
    assert provenance["normalized_unit"] == "W"
    assert provenance["scenario_id"] == "FUNC_SLOW"
    assert provenance["mode"] == "functional"
    assert provenance["corner"] == "slow"
    assert provenance["sha256"] == hash_file(_FIXTURE)


@pytest.mark.parametrize(
    ("unit", "factor"),
    [("W", 1.0), ("Watts", 1.0), ("mW", 1e-3), ("uW", 1e-6),
     ("µW", 1e-6), ("nW", 1e-9), ("pW", 1e-12)],
)
def test_explicit_units_normalize_to_watts(unit, factor):
    parsed = parse_openroad_power_text(_report_with_unit(unit))
    assert parsed.status == PowerParseStatus.AVAILABLE.value
    assert parsed.total == pytest.approx(6 * factor)
    assert parsed.dynamic == pytest.approx(3 * factor)
    assert parsed.leakage == pytest.approx(3 * factor)


def test_valid_zero_is_available_not_unavailable():
    parsed = parse_openroad_power_text(
        _report_with_unit("Watts", internal="0", switching="0", leakage="0", total="0")
    )
    assert parsed.status == PowerParseStatus.AVAILABLE.value
    assert parsed.total == 0.0
    assert parsed.dynamic == 0.0
    assert parsed.leakage == 0.0


def test_missing_report_is_unavailable_not_zero(tmp_path):
    parsed = parse_openroad_power_report(tmp_path / "does-not-exist.rpt")
    assert parsed.status == PowerParseStatus.UNAVAILABLE.value
    assert parsed.total is None
    assert parsed.dynamic is None
    assert parsed.leakage is None


def test_parser_classification_does_not_expand_canonical_power_status():
    """Detailed report failures remain parser provenance, not a QoR API break."""
    assert [status.value for status in PowerStatus] == [
        "AVAILABLE", "UNAVAILABLE", "ESTIMATED",
    ]
    parsed = parse_openroad_power_text("report_power\nnot a group summary\n")
    assert parsed.status == PowerParseStatus.MALFORMED.value
    assert parsed.status not in {status.value for status in PowerStatus}


def test_missing_total_is_unknown():
    text = _fixture_text().replace(
        "Total                  8.57e-04   4.56e-04   1.30e-05   1.33e-03 100.0%\n", ""
    )
    parsed = parse_openroad_power_text(text)
    assert parsed.status == PowerParseStatus.UNKNOWN.value
    assert parsed.total is None


def test_missing_dynamic_component_keeps_total_available():
    text = _report_with_unit("Watts", internal="-", switching="2", leakage="3", total="6")
    parsed = parse_openroad_power_text(text)
    assert parsed.status == PowerParseStatus.AVAILABLE.value
    assert parsed.total == 6.0
    assert parsed.dynamic is None
    assert parsed.leakage == 3.0


def test_missing_leakage_keeps_total_available():
    text = _report_with_unit("Watts", internal="1", switching="2", leakage="-", total="6")
    parsed = parse_openroad_power_text(text)
    assert parsed.status == PowerParseStatus.AVAILABLE.value
    assert parsed.total == 6.0
    assert parsed.dynamic == 3.0
    assert parsed.leakage is None


def test_malformed_numeric_field_is_not_a_power_value():
    parsed = parse_openroad_power_text(_report_with_unit("Watts", leakage="not-a-number"))
    assert parsed.status == PowerParseStatus.MALFORMED.value
    assert parsed.total is None


@pytest.mark.parametrize("field,value", [("internal", "38.6%"), ("total", "100.0%")])
def test_percentage_tokens_cannot_be_parsed_as_power_cells(field, value):
    kwargs = {field: value}
    parsed = parse_openroad_power_text(_report_with_unit("Watts", **kwargs))
    assert parsed.status == PowerParseStatus.MALFORMED.value
    assert parsed.total is None


def test_two_report_tables_are_ambiguous_not_silently_selected():
    parsed = parse_openroad_power_text(_fixture_text() + "\n" + _fixture_text())
    assert parsed.status == PowerParseStatus.UNKNOWN.value
    assert parsed.total is None


def test_unsupported_file_is_not_a_power_value():
    parsed = parse_openroad_power_text("timing report\nwns -0.02\n")
    assert parsed.status == PowerParseStatus.UNSUPPORTED.value
    assert parsed.total is None


def test_unsupported_explicit_unit_is_not_a_power_value():
    parsed = parse_openroad_power_text(_report_with_unit("kW"))
    assert parsed.status == PowerParseStatus.UNSUPPORTED.value
    assert parsed.total is None


def test_absent_unit_is_unknown_not_assumed_watts():
    text = _report_with_unit("Watts").replace("Power (Watts)", "Power")
    parsed = parse_openroad_power_text(text)
    assert parsed.status == PowerParseStatus.UNKNOWN.value
    assert parsed.total is None


def test_negative_power_is_invalid_not_a_power_value():
    parsed = parse_openroad_power_text(_report_with_unit("Watts", internal="-1", total="4"))
    assert parsed.status == PowerParseStatus.INVALID.value
    assert parsed.total is None


def test_inconsistent_component_sum_is_invalid_not_a_power_value():
    parsed = parse_openroad_power_text(_report_with_unit("Watts", total="60"))
    assert parsed.status == PowerParseStatus.INVALID.value
    assert parsed.total is None


def test_parser_is_deterministic_for_same_text():
    a = parse_openroad_power_text(_fixture_text())
    b = parse_openroad_power_text(_fixture_text())
    assert a == b


def test_qor_summary_retains_power_fields_and_existing_provenance_container():
    parsed = parse_openroad_power_report(_FIXTURE, scenario_id="S1")
    q = QoRResult(
        power=parsed.total, power_total=parsed.total, power_dynamic=parsed.dynamic,
        power_leakage=parsed.leakage, power_status=PowerStatus.AVAILABLE.value,
        raw_reports={"power": parsed.provenance()},
    )
    summary = q.summary()
    assert summary["power"] == pytest.approx(1.33e-3)
    assert summary["power_total"] == pytest.approx(1.33e-3)
    assert summary["power_dynamic"] == pytest.approx(1.313e-3)
    assert summary["power_leakage"] == pytest.approx(1.3e-5)
    assert summary["power_provenance"]["scenario_id"] == "S1"
    restored = QoRResult.from_summary(summary)
    assert restored.power == q.power
    assert restored.raw_reports["power"]["sha256"] == parsed.source_sha256


# ---------------------------------------------------------------------------
# Configuration validation / resolution
# ---------------------------------------------------------------------------

def _project(*, reports=None, scenarios=None, mcmm=None, output_dir="output") -> ProjectConfig:
    return ProjectConfig(
        project=ProjectInfo(name="top", top="top"),
        sources=SourceConfig(),
        flow=FlowConfig(output_dir=output_dir, power_reports=list(reports or [])),
        scenarios=list(scenarios or []),
        mcmm=mcmm or MCMMConfig(),
    )


def test_single_scenario_config_allows_omitted_scenario_id_and_resolves_path(tmp_path):
    cfg_path = tmp_path / "project.yaml"
    cfg_path.write_text("""
project:
  name: top
  top: top
sources:
  files: []
flow:
  power_reports:
    - format: openroad_report_power
      path: reports/power.rpt
""", encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg.flow.power_reports[0].scenario_id is None
    assert cfg.flow.power_reports[0].path == str((tmp_path / "reports/power.rpt").resolve())


@pytest.mark.parametrize(
    "reports,scenarios,mcmm,match",
    [
        ([PowerReportConfig(format="openroad_report_power", path="a.rpt")],
         [ScenarioSpec(id="S1")], MCMMConfig(enabled=True), "scenario_id is required"),
        ([PowerReportConfig(format="openroad_report_power", path="a.rpt", scenario_id="NOPE")],
         [ScenarioSpec(id="S1")], MCMMConfig(enabled=True), "not a configured scenario"),
        ([PowerReportConfig(format="openroad_report_power", path="a.rpt", scenario_id="OFF")],
         [ScenarioSpec(id="S1"), ScenarioSpec(id="OFF", active=False)], MCMMConfig(enabled=True), "inactive"),
        ([PowerReportConfig(format="openroad_report_power", path="a.rpt", scenario_id="S1"),
          PowerReportConfig(format="openroad_report_power", path="b.rpt", scenario_id="S1")],
         [ScenarioSpec(id="S1")], MCMMConfig(enabled=True), "Duplicate"),
    ],
)
def test_mcmm_report_configuration_rejects_unsafe_mapping(reports, scenarios, mcmm, match):
    with pytest.raises(ValueError, match=match):
        _project(reports=reports, scenarios=scenarios, mcmm=mcmm)


# ---------------------------------------------------------------------------
# Fake real-flow integration.  These executables only exercise RCA's parsing
# plumbing; they are not OpenROAD/OpenSTA power runs.
# ---------------------------------------------------------------------------

def _write_fake_yosys(path: Path) -> None:
    path.write_text(r'''#!/usr/bin/env python3
import pathlib, re, sys
if "-V" in sys.argv:
    print("Yosys 0.35 (test fake)")
    raise SystemExit(0)
script = pathlib.Path(sys.argv[sys.argv.index("-s") + 1])
m = re.search(r"write_verilog(?:\s+\S+)*\s+(\S+)", script.read_text())
out = pathlib.Path(m.group(1))
if not out.is_absolute(): out = script.parent / out
out.write_text("module top(input clk, output q); assign q=clk; endmodule\n")
print("Number of cells: 1\nChip area for module: 1.0", file=sys.stderr)
''', encoding="utf-8")
    path.chmod(0o755)


def _write_fake_sta(path: Path) -> None:
    path.write_text(r'''#!/usr/bin/env python3
import pathlib, re, sys
if "-version" in sys.argv or "--version" in sys.argv:
    print("OpenSTA 2.5.0 (test fake)")
    raise SystemExit(0)
tcl_path = pathlib.Path([a for a in sys.argv[1:] if not a.startswith("-")][0])
for target in re.findall(r">\s*(\S+)", tcl_path.read_text()):
    output = tcl_path.parent / target
    if target.endswith("setup.rpt"):
        output.write_text("Path Type: max\nStartpoint: clk\nEndpoint: q\nPath Group: clk\nslack (MET) 1.5\n")
    elif target.endswith("hold.rpt"):
        output.write_text("Path Type: min\nStartpoint: clk\nEndpoint: q\nPath Group: clk\nslack (MET) 0.2\n")
    elif target.endswith("setup.wns"):
        output.write_text("wns 1.5\n")
    elif target.endswith("setup.tns") or target.endswith("hold.tns"):
        output.write_text("tns 0.0\n")
    elif target.endswith("hold.wns"):
        output.write_text("wns 0.2\n")
    else:
        output.write_text("No setup violations.\n")
''', encoding="utf-8")
    path.chmod(0o755)


def _real_flow_config(tmp_path: Path, reports: list[PowerReportConfig], *,
                      scenarios: list[ScenarioSpec] | None = None,
                      mcmm: MCMMConfig | None = None) -> tuple[ProjectConfig, list[Path], Path, Path]:
    rtl = tmp_path / "top.v"
    rtl.write_text("module top(input clk, output q); assign q = clk; endmodule\n", encoding="utf-8")
    lib = tmp_path / "test.lib"
    lib.write_text("library(test) { cell(X) { area : 1.0; } }\n", encoding="utf-8")
    ybin, obin = tmp_path / "yosys", tmp_path / "sta"
    _write_fake_yosys(ybin)
    _write_fake_sta(obin)
    cfg = ProjectConfig(
        project=ProjectInfo(name="top", top="top"), sources=SourceConfig(),
        flow=FlowConfig(liberty=[str(lib)], output_dir=str(tmp_path / "output"),
                        power_reports=reports),
        scenarios=scenarios or [], mcmm=mcmm or MCMMConfig(),
    )
    return cfg, [rtl], ybin, obin


def _real_run(cfg, sources, ybin, obin, *, run_id, scenario="default", corner="default", mode="default"):
    return run_flow(
        cfg, ConstraintSet(name="top"), "create_clock -name clk -period 10 clk\n",
        sdc_generation_status="COMPLETE", sources=sources,
        backend="yosys_opensta", run_id=run_id, scenario=scenario, corner=corner, mode=mode,
        yosys_bin=str(ybin), sta_bin=str(obin),
    )


def test_real_flow_ingests_power_artifact_and_report_hash_and_invalidates_cache(tmp_path):
    report = tmp_path / "power.rpt"
    shutil.copyfile(_FIXTURE, report)
    cfg, sources, ybin, obin = _real_flow_config(
        tmp_path, [PowerReportConfig(format="openroad_report_power", path=str(report),
                                     producer_version="OpenROAD documented example")]
    )
    first = _real_run(cfg, sources, ybin, obin, run_id="first")
    assert first["status"] == RunStatus.SUCCESS.value
    assert first["qor"]["power_status"] == PowerStatus.AVAILABLE.value
    assert first["qor"]["power"] == pytest.approx(1.33e-3)
    assert first["qor"]["power_provenance"]["sha256"] == hash_file(report)
    assert first["qor"]["power_provenance"]["producer_version"] == "OpenROAD documented example"
    assert first["qor"]["power_provenance"]["tool_version"] == "OpenSTA 2.5.0 (test fake)"
    manifest = first["manifest"]
    assert manifest["artifacts"]["power_report"] == "configured_power_report.rpt"
    assert manifest["artifact_hashes"]["power_report"] == hash_file(report)
    assert manifest["input_hashes"]["power_report"]["sha256"] == hash_file(report)

    same = _real_run(cfg, sources, ybin, obin, run_id="same")
    assert same["status"] == RunStatus.CACHE_HIT.value
    assert same["cache_key"] == first["cache_key"]
    assert same["qor_result"].power == pytest.approx(1.33e-3)

    report.write_text(_fixture_text().replace("1.33e-03", "1.32e-03"), encoding="utf-8")
    changed = _real_run(cfg, sources, ybin, obin, run_id="changed")
    assert changed["status"] != RunStatus.CACHE_HIT.value
    assert changed["cache_key"] != first["cache_key"]
    assert changed["qor"]["power"] == pytest.approx(1.32e-3)


def test_real_flow_missing_configured_report_stays_unavailable(tmp_path):
    missing = tmp_path / "missing.rpt"
    cfg, sources, ybin, obin = _real_flow_config(
        tmp_path, [PowerReportConfig(format="openroad_report_power", path=str(missing))]
    )
    out = _real_run(cfg, sources, ybin, obin, run_id="missing")
    assert out["status"] == RunStatus.SUCCESS.value
    assert out["qor"]["power_status"] == PowerStatus.UNAVAILABLE.value
    assert out["qor"]["power"] is None
    assert "power_report" not in out["manifest"]["artifacts"]


def test_real_flow_keeps_detailed_parse_failure_in_provenance_and_canonical_power_unavailable(tmp_path):
    report = tmp_path / "malformed_power.rpt"
    report.write_text("report_power\nthis is not a group table\n", encoding="utf-8")
    cfg, sources, ybin, obin = _real_flow_config(
        tmp_path, [PowerReportConfig(format="openroad_report_power", path=str(report))]
    )
    out = _real_run(cfg, sources, ybin, obin, run_id="malformed")
    assert out["status"] == RunStatus.SUCCESS.value
    assert out["qor"]["power_status"] == PowerStatus.UNAVAILABLE.value
    assert out["qor"]["power"] is None
    assert out["qor"]["power_total"] is None
    assert out["qor"]["power_dynamic"] is None
    assert out["qor"]["power_leakage"] is None
    assert out["qor"]["power_provenance"]["parsing_status"] == PowerParseStatus.MALFORMED.value
    assert out["qor"]["power_provenance"]["normalized_unit"] is None


def test_mock_flow_ignores_configured_report_and_remains_unavailable(tmp_path):
    report = tmp_path / "power.rpt"
    shutil.copyfile(_FIXTURE, report)
    cfg = _project(
        reports=[PowerReportConfig(format="openroad_report_power", path=str(report))],
        output_dir=str(tmp_path / "output"),
    )
    out = run_flow(cfg, ConstraintSet(name="top"), "# mock\n", "COMPLETE", [],
                   backend="mock", run_id="mock")
    assert out["status"] == RunStatus.MOCK.value
    assert out["qor"]["power_status"] == PowerStatus.UNAVAILABLE.value
    assert out["qor"]["power"] is None
    assert "power_report" not in out["manifest"]["artifacts"]
    assert any("ignored for mock" in note for note in out["qor_result"].notes)


def test_mcmm_selects_only_scenario_specific_reports_and_unknown_when_one_missing(tmp_path):
    slow = tmp_path / "slow.rpt"
    fast = tmp_path / "fast.rpt"
    slow.write_text(_fixture_text(), encoding="utf-8")
    fast.write_text(_fixture_text().replace("1.33e-03", "1.32e-03"), encoding="utf-8")
    scenarios = [ScenarioSpec(id="SLOW", mode="functional", corner="slow"),
                 ScenarioSpec(id="FAST", mode="functional", corner="fast")]
    reports = [
        PowerReportConfig(format="openroad_report_power", path=str(slow), scenario_id="SLOW"),
        PowerReportConfig(format="openroad_report_power", path=str(fast), scenario_id="FAST"),
    ]
    cfg, sources, ybin, obin = _real_flow_config(
        tmp_path, reports, scenarios=scenarios, mcmm=MCMMConfig(enabled=True)
    )
    matrix = build_scenario_matrix(cfg, ConstraintSet(name="top"))
    candidate = Candidate(id="C000", constraint_set=ConstraintSet(name="top"))

    def evaluate(scenario, cand, work):
        out = _real_run(cfg, sources, ybin, obin, run_id=f"{cand.id}-{scenario.id}",
                        scenario=scenario.id, corner=scenario.corner, mode=scenario.mode)
        return {"qor": out["qor_result"], "cache_key": out.get("cache_key", ""),
                "cache_status": out["status"], "run_id": out["run_id"]}

    result = MCMMEvaluator(matrix, evaluate, base_cset=candidate.constraint_set,
                           name="yosys_opensta")(candidate, tmp_path / "work")
    assert result.scenario_results["SLOW"].qor.power == pytest.approx(1.33e-3)
    assert result.scenario_results["FAST"].qor.power == pytest.approx(1.32e-3)
    assert result.scenario_results["SLOW"].qor.raw_reports["power"]["scenario_id"] == "SLOW"
    assert result.objectives["power"].value == pytest.approx(1.33e-3)
    assert not result.objectives["power"].unknown

    # A second config deliberately omits FAST.  It remains unavailable; the
    # global MCMM objective is UNKNOWN rather than averaging SLOW alone.
    cfg.flow.power_reports = [reports[0]]
    missing = MCMMEvaluator(matrix, evaluate, base_cset=candidate.constraint_set,
                            name="yosys_opensta")(candidate, tmp_path / "work-missing")
    assert missing.scenario_results["FAST"].qor.power_status == PowerStatus.UNAVAILABLE.value
    assert missing.objectives["power"].value is None
    assert missing.objectives["power"].unknown
    assert missing.objectives["power"].limiting == ["FAST"]


def test_report_derived_available_power_participates_in_existing_pareto_logic():
    low = QoRResult(setup_wns=1e-9, hold_wns=1e-9, area=10.0, power=1e-3,
                    power_status=PowerStatus.AVAILABLE.value)
    high = QoRResult(setup_wns=1e-9, hold_wns=1e-9, area=10.0, power=2e-3,
                     power_status=PowerStatus.AVAILABLE.value)
    low_candidate = Candidate(id="LOW", qor=low, hard_feasible=True, constraint_set=ConstraintSet())
    high_candidate = Candidate(id="HIGH", qor=high, hard_feasible=True, constraint_set=ConstraintSet())
    assert objective_vector(low)["power"][0] == pytest.approx(1e-3)
    assert is_dominating(low_candidate, high_candidate)
    assert not is_dominating(high_candidate, low_candidate)


def test_cache_identity_changes_for_rebound_or_missing_scenario_evidence(tmp_path):
    report = tmp_path / "power.rpt"
    missing = tmp_path / "missing.rpt"
    shutil.copyfile(_FIXTURE, report)
    scenarios = [ScenarioSpec(id="A"), ScenarioSpec(id="B")]
    cfg_a, sources, ybin, obin = _real_flow_config(
        tmp_path,
        [PowerReportConfig(format="openroad_report_power", path=str(report), scenario_id="A")],
        scenarios=scenarios,
        mcmm=MCMMConfig(enabled=True),
    )
    bound = _real_run(cfg_a, sources, ybin, obin, run_id="bound", scenario="A")
    assert bound["qor"]["power_status"] == PowerStatus.AVAILABLE.value

    # Moving the same file mapping from A to B changes selected evidence for A;
    # it cannot be reused as A's report and therefore has a distinct cache key.
    cfg_b, _, _, _ = _real_flow_config(
        tmp_path,
        [PowerReportConfig(format="openroad_report_power", path=str(report), scenario_id="B")],
        scenarios=scenarios,
        mcmm=MCMMConfig(enabled=True),
    )
    rebound = _real_run(cfg_b, sources, ybin, obin, run_id="rebound", scenario="A")
    assert rebound["qor"]["power_status"] == PowerStatus.UNAVAILABLE.value
    assert rebound["cache_key"] != bound["cache_key"]

    # A present mapped report and an otherwise identical missing mapped report
    # also have distinct experiment identities.
    cfg_missing, _, _, _ = _real_flow_config(
        tmp_path,
        [PowerReportConfig(format="openroad_report_power", path=str(missing), scenario_id="A")],
        scenarios=scenarios,
        mcmm=MCMMConfig(enabled=True),
    )
    absent = _real_run(cfg_missing, sources, ybin, obin, run_id="absent", scenario="A")
    assert absent["qor"]["power_status"] == PowerStatus.UNAVAILABLE.value
    assert absent["cache_key"] != bound["cache_key"]


def test_cli_and_human_report_use_tool_reported_wording_and_provenance():
    from rich.console import Console

    from rca.cli.main import _print_power_summary
    from rca.explanation.generator import design_report

    parsed = parse_openroad_power_report(_FIXTURE, scenario_id="S1")
    summary = QoRResult(
        power=parsed.total, power_status=PowerStatus.AVAILABLE.value,
        power_dynamic=parsed.dynamic, power_leakage=parsed.leakage,
        raw_reports={"power": parsed.provenance()},
    ).summary()
    console = Console(record=True, force_terminal=False, width=200)
    _print_power_summary(console, summary)
    rendered = console.export_text()
    assert "Tool-reported power" in rendered
    assert "Power provenance" in rendered
    report = design_report({}, {}, None, None, ConstraintSet(), [], summary)
    assert "POWER (MOST RECENT RECORDED RUN)" in report
    assert "Tool-reported power" in report
    assert str(_FIXTURE) in report

    # Detailed parser outcomes remain visible in CLI/report presentation even
    # though canonical QoR deliberately stays UNAVAILABLE.
    unavailable = QoRResult(
        power=None,
        power_status=PowerStatus.UNAVAILABLE.value,
        raw_reports={"power": {"format": POWER_REPORT_FORMAT,
                                "parsing_status": PowerParseStatus.MALFORMED.value}},
    ).summary()
    _print_power_summary(console, unavailable)
    assert "Power: UNAVAILABLE (report parser: MALFORMED)" in console.export_text()
    unavailable_report = design_report({}, {}, None, None, ConstraintSet(), [], unavailable)
    assert "Power: UNAVAILABLE (report parser: MALFORMED)" in unavailable_report
