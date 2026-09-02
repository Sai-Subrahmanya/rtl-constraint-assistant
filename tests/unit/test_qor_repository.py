"""QoR historical SQLite repository tests (Step 21).

All tests use temporary sidecars and canonical in-memory RCA objects.  No test
requires a live EDA or power tool.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rca.artifacts import RunManifest
from rca.cli.main import app
from rca.constraint_model import ConstraintSet, stable_hash_cset
from rca.eda.flow import run_flow
from rca.mcmm import (
    MCMMResult,
    ObjectiveAggregate,
    ScenarioQoR,
    aggregate_objectives,
    global_feasibility,
)
from rca.optimizer import Candidate, OptimizationResult
from rca.qor.model import CriticalPath, QoRResult
from rca.qor.repository import (
    SCHEMA_VERSION,
    SQLITE_APPLICATION_ID,
    RecordConflictError,
    SchemaVersionError,
    SQLiteQoRRepository,
)
from rca.reports.power import PowerParseStatus
from rca.utils.enums import CandidateDecision, PowerStatus, RunStatus
from rca.utils.hashing import hash_file


def _repo(tmp_path: Path) -> SQLiteQoRRepository:
    return SQLiteQoRRepository.for_output_dir(tmp_path / "output")


def _qor(
    *, run_id: str = "run", candidate_id: str = "C000", scenario: str = "SLOW",
    setup_ns: float | None = 1.0, hold_ns: float | None = 0.2, area: float | None = 100.0,
    area_proxy: float | None = None, power: float | None = None, is_mock: bool = False,
    power_status: str | None = None,
) -> QoRResult:
    return QoRResult(
        run_id=run_id, candidate_id=candidate_id, backend="yosys_opensta", backend_version="tool-v1",
        tool="opensta", tool_version="2.5", flow_stage="synthesis_sta", scenario=scenario,
        mode="functional", corner="slow", is_mock=is_mock,
        setup_wns=setup_ns * 1e-9 if setup_ns is not None else None,
        setup_tns=-0.1e-9 if setup_ns is not None else None,
        setup_violations=0, hold_wns=hold_ns * 1e-9 if hold_ns is not None else None,
        hold_tns=0.0 if hold_ns is not None else None, hold_violations=0,
        whs=hold_ns * 1e-9 if hold_ns is not None else None, ths=0.0,
        near_critical_count=2, path_count=4, area=area, area_proxy=area_proxy,
        area_comb=55.0, area_seq=40.0, area_buffer=5.0, cell_count=100, ff_count=30,
        comb_cell_count=70, buf_count=5, buffer_count=5, power=power,
        power_status=power_status or (PowerStatus.AVAILABLE.value if power is not None else PowerStatus.UNAVAILABLE.value),
        constraint_quality=0.9, validation_errors=0, unsafe_exceptions=0,
        margin_headroom_ns=0.8, margin_utilization=0.2, congestion=0.1, runtime_seconds=3.0,
        critical_setup=CriticalPath(startpoint="a", endpoint="b", slack=setup_ns * 1e-9 if setup_ns is not None else None),
        wns_percentiles={"p50": 0.5e-9}, timing_distribution={"met": 4},
        feasibility={"feasible": True, "formal": "VERIFIED", "validation": "PASS"},
        notes=["measured"], diagnostics=["ok"], cache_key="cache-key", cache_status="MISS",
    )


def _manifest(tmp_path: Path, run_id: str, *, candidate_id: str = "C000") -> tuple[RunManifest, Path]:
    run_dir = tmp_path / "output" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "qor.json").write_text("{}\n", encoding="utf-8")
    (run_dir / "generated.sdc").write_text("create_clock clk\n", encoding="utf-8")
    manifest = RunManifest(
        candidate_id=candidate_id, rtl_hash={"rtl/top.v": "rtl-hash"}, sdc_hash="sdc-hash",
        config_hash="config-hash", tool="yosys_opensta", tool_version="tool-v1",
        flow_stage="synthesis_sta", mode="functional", corner="slow", library="cells.lib",
        artifacts={"qor": "qor.json", "sdc": "generated.sdc"},
        artifact_hashes={"qor": hash_file(run_dir / "qor.json"), "sdc": hash_file(run_dir / "generated.sdc")},
        tool_identity={"opensta": {"version": "2.5"}},
        input_hashes={"rtl": {"rtl/top.v": "rtl-hash"}, "includes": {"inc": "inc-hash"}, "libs": {"cells.lib": "lib-hash"}},
        extra={"cache_key": "cache-key", "commands": {"opensta": ["sta", "script.tcl"]}},
    )
    return manifest, run_dir


def _record(repo: SQLiteQoRRepository, tmp_path: Path, *, run_id: str = "run", **kwargs):
    q = _qor(run_id=run_id, **kwargs)
    manifest, run_dir = _manifest(tmp_path, run_id, candidate_id=q.candidate_id)
    repo.record_flow_evaluation(q, manifest, run_dir=run_dir, run_status=RunStatus.SUCCESS.value,
                                constraint_set=ConstraintSet(name="design"))
    return q, manifest, run_dir


def test_01_database_creation_schema_version_and_application_identifier(tmp_path):
    repo = _repo(tmp_path)
    assert repo.initialize() == SCHEMA_VERSION
    conn = sqlite3.connect(repo.db_path)
    assert conn.execute("PRAGMA application_id").fetchone()[0] == SQLITE_APPLICATION_ID
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"schema_migrations", "optimization_sessions", "constraint_sets", "candidates", "candidate_mutations",
            "evaluations", "qor_measurements", "power_evidence", "evaluation_artifacts", "mcmm_aggregates",
            "mcmm_objectives", "mcmm_members"} <= tables
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION


def test_02_new_database_migrates_and_migration_failure_rolls_back(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    original = repo._apply_migration
    def fail_migration(conn, version):
        original(conn, version)
        raise RuntimeError("simulated migration failure")
    monkeypatch.setattr(repo, "_apply_migration", fail_migration)
    with pytest.raises(RuntimeError, match="simulated"):
        repo.migrate()
    conn = sqlite3.connect(repo.db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='evaluations'").fetchone() is None
    monkeypatch.setattr(repo, "_apply_migration", original)
    assert repo.migrate() == SCHEMA_VERSION
    # Simulate a supported, already-published v1 sidecar and verify its
    # append-only v2 migration is transactional and preserves the ledger.
    older = SQLiteQoRRepository.for_output_dir(tmp_path / "older")
    conn = older._connect()
    conn.execute("BEGIN IMMEDIATE")
    older._apply_migration(conn, 1)
    conn.execute("INSERT INTO schema_migrations VALUES (1, 'then', 'initial')")
    conn.execute("PRAGMA user_version=1")
    conn.commit(); conn.close()
    assert older.migrate() == SCHEMA_VERSION
    conn = sqlite3.connect(older.db_path)
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == SCHEMA_VERSION
    assert "artifact_fingerprint" in {row[1] for row in conn.execute("PRAGMA table_info(evaluations)")}


def test_03_unsupported_newer_schema_and_mismatched_ledger_fail_clearly(tmp_path):
    repo = _repo(tmp_path)
    repo.initialize()
    conn = sqlite3.connect(repo.db_path)
    conn.execute("PRAGMA user_version=3")
    conn.execute("INSERT INTO schema_migrations VALUES (3, 'now', 'future')")
    conn.commit()
    with pytest.raises(SchemaVersionError, match="newer"):
        repo.migrate()
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    with pytest.raises(SchemaVersionError, match="inconsistent"):
        repo.migrate()


def test_04_run_insertion_idempotency_and_conflicting_fingerprint(tmp_path):
    repo = _repo(tmp_path)
    q, manifest, run_dir = _record(repo, tmp_path, run_id="same")
    initial = repo.get_run("same")
    again = repo.record_flow_evaluation(q, manifest, run_dir=run_dir, run_status=RunStatus.SUCCESS.value,
                                        constraint_set=ConstraintSet(name="design"))
    assert again["evaluation"]["source_fingerprint"] == initial["evaluation"]["source_fingerprint"]
    assert len(repo.list_runs()) == 1
    changed = _qor(run_id="same", setup_ns=2.0)
    with pytest.raises(RecordConflictError, match="different source fingerprint"):
        repo.record_flow_evaluation(changed, manifest, run_dir=run_dir, run_status=RunStatus.SUCCESS.value)
    # area_comb is canonical source evidence but absent from legacy qor.json;
    # a normal flow write must still conflict rather than use legacy matching.
    changed_unserialized = _qor(run_id="same")
    changed_unserialized.area_comb = 999.0
    with pytest.raises(RecordConflictError, match="different source fingerprint"):
        repo.record_flow_evaluation(changed_unserialized, manifest, run_dir=run_dir, run_status=RunStatus.SUCCESS.value)


def test_05_full_canonical_qor_and_nullable_unknown_fields_are_preserved(tmp_path):
    repo = _repo(tmp_path)
    _record(repo, tmp_path, run_id="full", power=0.0)
    qor = repo.get_run("full")["qor"]
    assert qor["setup_wns_s"] == pytest.approx(1e-9)
    assert qor["area_comb"] == 55.0 and qor["buffer_count"] == 5
    assert qor["power_total_w"] == 0.0
    assert qor["critical_setup"]["startpoint"] == "a"
    assert qor["feasibility"]["formal"] == "VERIFIED"
    unknown = _qor(run_id="unknown", setup_ns=None, hold_ns=None, area=None, area_proxy=None, power=None)
    manifest, run_dir = _manifest(tmp_path, "unknown")
    repo.record_flow_evaluation(unknown, manifest, run_dir=run_dir, run_status=RunStatus.BLOCKED.value)
    stored = repo.get_run("unknown")["qor"]
    assert stored["setup_wns_s"] is None and stored["power_total_w"] is None
    assert stored["area_source"] == "unknown"


def test_06_power_status_parser_provenance_and_unknown_are_separate(tmp_path):
    repo = _repo(tmp_path)
    q = _qor(run_id="power", power=0.0)
    q.raw_reports["power"] = {
        "format": "openroad_report_power", "parser_format_version": "1", "configured_producer": "openroad_opensta",
        "producer_version": "2026.1", "tool_version": "OpenSTA 2.5", "original_unit": "mW", "normalized_unit": "W",
        "scenario_id": "SLOW", "mode": "functional", "corner": "slow", "report_path": "configured_power_report.rpt",
        "sha256": "a" * 64, "parsing_status": PowerParseStatus.AVAILABLE.value,
        "reported_internal_w": 0.0, "reported_switching_w": 0.0, "diagnostics": ["accepted"],
    }
    manifest, run_dir = _manifest(tmp_path, "power")
    repo.record_flow_evaluation(q, manifest, run_dir=run_dir, run_status="SUCCESS")
    evidence = repo.get_run("power")["power_evidence"]
    assert evidence["canonical_power_status"] == PowerStatus.AVAILABLE.value
    assert evidence["parser_status"] == PowerParseStatus.AVAILABLE.value
    assert evidence["report_sha256"] == "a" * 64 and evidence["reported_internal_w"] == 0.0
    unavailable = _qor(run_id="power_unknown", area=1.0, power=None)
    unavailable.raw_reports["power"] = {"parsing_status": PowerParseStatus.MALFORMED.value, "diagnostics": ["bad"]}
    manifest, run_dir = _manifest(tmp_path, "power_unknown")
    repo.record_flow_evaluation(unavailable, manifest, run_dir=run_dir, run_status="SUCCESS")
    result = repo.get_run("power_unknown")
    assert result["qor"]["power_total_w"] is None
    assert result["power_evidence"]["parser_status"] == PowerParseStatus.MALFORMED.value


def test_07_artifacts_provenance_replay_identity_and_hash_validation(tmp_path):
    repo = _repo(tmp_path)
    _, _, run_dir = _record(repo, tmp_path, run_id="replay")
    artifacts = repo.get_artifacts("replay")
    assert [a["artifact_name"] for a in artifacts] == ["qor", "sdc"]
    replay = repo.get_replay_identity("replay")
    assert replay["automatic_replay_supported"] is False
    assert replay["commands"]["opensta"] == ["sta", "script.tcl"]
    assert all(item["integrity_valid"] is True for item in replay["artifacts"])
    (run_dir / "generated.sdc").write_text("tampered\n", encoding="utf-8")
    replay = repo.get_replay_identity("replay")
    assert any(item["integrity_reason"] == "sha256_mismatch" for item in replay["artifacts"])


def test_08_candidate_session_constraint_identity_and_lineage(tmp_path):
    repo = _repo(tmp_path)
    cset = ConstraintSet(name="constraints")
    baseline = Candidate(id="C000", constraint_set=cset, qor=_qor(run_id="", candidate_id="C000"),
                         decision=CandidateDecision.FINAL, hard_feasible=True)
    child = Candidate(id="C001", parent_id="C000", generation=1, constraint_set=cset,
                      generated_changes=["uncertainty+0.1"], mutated_constraint_ids=["U1"],
                      qor=_qor(run_id="", candidate_id="C001"), hard_feasible=True)
    session = OptimizationResult(baseline=baseline, final=child, all_candidates=[baseline, child])
    assert repo.record_optimizer_session(session, session_id="session-a", project_name="proj") == "session-a"
    candidate = repo.get_candidate("session-a", "C001")
    assert candidate["parent_candidate_key"] == "session-a:C000"
    assert candidate["mutations"] == [{"ordinal": 0, "change_label": "uncertainty+0.1", "mutated_constraint_id": "U1"}]
    assert [item["candidate_id"] for item in repo.candidate_lineage("session-a", "C001")] == ["C000", "C001"]
    assert repo.find_by_constraint_set(stable_hash_cset(cset))


def test_09_mcmm_scenarios_remain_distinct_and_aggregate_is_conservative(tmp_path):
    repo = _repo(tmp_path)
    cset = ConstraintSet(name="mcmm")
    candidate = Candidate(id="C000", constraint_set=cset)
    repo.record_optimizer_session(OptimizationResult(baseline=candidate, final=candidate, all_candidates=[candidate]), session_id="mcmm-session")
    slow = _qor(run_id="", candidate_id="C000", scenario="SLOW", setup_ns=1.0, area=100.0, power=1e-3)
    fast = _qor(run_id="", candidate_id="C000", scenario="FAST", setup_ns=0.5, area=101.0, power=None)
    result = MCMMResult(candidate_id="C000", active_scenario_ids=["SLOW", "FAST"])
    result.scenario_results["SLOW"] = ScenarioQoR(candidate_id="C000", scenario_id="SLOW", mode="functional", corner="slow", qor=slow)
    result.scenario_results["FAST"] = ScenarioQoR(candidate_id="C000", scenario_id="FAST", mode="test", corner="fast", qor=fast)
    global_feasibility(result)
    aggregate_objectives(result)
    mcmm_id = repo.record_mcmm_aggregate(result, session_id="mcmm-session", candidate_id="C000", candidate=candidate)
    aggregate = repo.get_mcmm(mcmm_id)
    assert [scenario["scenario_id"] for scenario in aggregate["scenarios"]] == ["SLOW", "FAST"]
    assert aggregate["scenarios"][0]["evaluation_id"] != aggregate["scenarios"][1]["evaluation_id"]
    power = next(item for item in aggregate["objectives"] if item["name"] == "power")
    assert power["value"] is None and power["unknown_value"] == 1


def test_10_mcmm_explicit_unknown_aggregate_and_scenario_filter(tmp_path):
    repo = _repo(tmp_path)
    _record(repo, tmp_path, run_id="slow", scenario="SLOW")
    _record(repo, tmp_path, run_id="fast", scenario="FAST")
    assert [item["evaluation"]["evaluation_id"] for item in repo.list_runs(scenario_id="FAST")] == ["fast"]
    c = Candidate(id="C", constraint_set=ConstraintSet(name="x"))
    repo.record_optimizer_session(OptimizationResult(baseline=c, final=c, all_candidates=[c]), session_id="unknown-mcmm")
    m = MCMMResult(candidate_id="C", active_scenario_ids=[])
    m.objectives["area"] = ObjectiveAggregate(name="area", value=None, unknown=True, incomparable=True, limiting=["S"], area_source="mixed")
    mid = repo.record_mcmm_aggregate(m, session_id="unknown-mcmm", candidate_id="C")
    objective = repo.get_mcmm(mid)["objectives"][0]
    assert objective["value"] is None and objective["unknown_value"] == 1 and objective["incomparable"] == 1


def test_11_best_queries_are_deterministic_and_preserve_real_proxy_distinction(tmp_path):
    repo = _repo(tmp_path)
    _record(repo, tmp_path, run_id="a", setup_ns=1.0, area=100.0, power=2.0)
    _record(repo, tmp_path, run_id="b", setup_ns=1.0, area=50.0, power=1.0)
    _record(repo, tmp_path, run_id="proxy", setup_ns=3.0, area=None, area_proxy=1.0, power=None)
    _record(repo, tmp_path, run_id="mock", setup_ns=5.0, area=1.0, power=0.1, is_mock=True)
    assert repo.best_qor("setup_wns")[0]["evaluation"]["evaluation_id"] == "proxy"
    assert repo.best_qor("power")[0]["evaluation"]["evaluation_id"] == "b"
    assert repo.best_qor("area")[0]["evaluation"]["evaluation_id"] == "b"
    assert repo.best_qor("area", area_source="proxy")[0]["evaluation"]["evaluation_id"] == "proxy"
    assert repo.best_qor("power", include_mock=True)[0]["evaluation"]["evaluation_id"] == "mock"
    assert repo.list_runs() == repo.list_runs()
    with pytest.raises(Exception, match="real/proxy"):
        repo.best_qor("area", area_source="both")


def test_12_transaction_rollback_leaves_no_partial_graph(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    q = _qor(run_id="rollback", power=1.0)
    q.raw_reports["power"] = {"parsing_status": "AVAILABLE"}
    manifest, run_dir = _manifest(tmp_path, "rollback")
    monkeypatch.setattr(repo, "_insert_power_evidence", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated insert failure")))
    with pytest.raises(RuntimeError, match="simulated"):
        repo.record_flow_evaluation(q, manifest, run_dir=run_dir, run_status="SUCCESS")
    conn = sqlite3.connect(repo.db_path)
    assert conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0] == 0


def test_13_legacy_import_is_explicit_idempotent_and_detects_conflicts(tmp_path):
    output = tmp_path / "legacy-output"; run_dir = output / "runs" / "legacy"; run_dir.mkdir(parents=True)
    summary = _qor(run_id="legacy", candidate_id="OLD", setup_ns=None, area=None, power=None).summary(); summary.pop("cell_count")
    (run_dir / "qor.json").write_text(json.dumps(summary), encoding="utf-8")
    (run_dir / "generated.sdc").write_text("x\n", encoding="utf-8")
    manifest = RunManifest(candidate_id="OLD", artifacts={"qor": "qor.json", "sdc": "generated.sdc"}, artifact_hashes={"qor": hash_file(run_dir / "qor.json"), "sdc": hash_file(run_dir / "generated.sdc")})
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    repo = SQLiteQoRRepository.for_output_dir(output)
    assert repo.import_legacy_artifacts()["imported"] == ["legacy"]
    assert repo.import_legacy_artifacts()["skipped"] == ["legacy"]
    assert repo.get_run("legacy")["qor"]["cell_count"] is None
    changed = json.loads((run_dir / "qor.json").read_text(encoding="utf-8")); changed["setup_wns_ns"] = 9.0
    (run_dir / "qor.json").write_text(json.dumps(changed), encoding="utf-8")
    assert repo.import_legacy_artifacts()["conflicts"]


def test_14_legacy_import_preserves_power_provenance_and_missing_fields(tmp_path):
    output = tmp_path / "legacy-power"; run_dir = output / "runs" / "legacy-power"; run_dir.mkdir(parents=True)
    summary = _qor(run_id="legacy-power", power=None).summary()
    summary["power_provenance"] = {"parsing_status": "MALFORMED", "sha256": "deadbeef", "diagnostics": ["bad"]}
    (run_dir / "qor.json").write_text(json.dumps(summary), encoding="utf-8")
    (run_dir / "run_manifest.json").write_text(json.dumps(RunManifest().to_dict()), encoding="utf-8")
    repo = SQLiteQoRRepository.for_output_dir(output); repo.import_legacy_artifacts()
    record = repo.get_run("legacy-power")
    assert record["qor"]["power_total_w"] is None and record["power_evidence"]["parser_status"] == "MALFORMED"


def test_15_two_connection_wal_read_safety_and_cache_key_is_passive(tmp_path):
    repo = _repo(tmp_path); _record(repo, tmp_path, run_id="concurrent")
    writer = repo._connect(); writer.execute("BEGIN IMMEDIATE")
    try:
        assert repo.get_run("concurrent")["evaluation"]["cache_key"] == "cache-key"
    finally:
        writer.rollback(); writer.close()


def test_16_database_failure_preserves_mock_qor_and_artifacts_when_explicitly_supplied(tmp_path):
    class FailingRepository:
        def record_flow_evaluation(self, *args, **kwargs): raise RuntimeError("sidecar unavailable")
    class Flow:
        output_dir = str(tmp_path / "out"); backend = "mock"; stage = "synthesis_sta"
        def liberty_files(self): return []
    class Analysis: safe_mode = "balanced"
    class Sources:
        def __init__(self):
            self.defines = []
            self.include_dirs = []
    class Config:
        def __init__(self):
            self._config_path = str(tmp_path / "project.yaml")
            self.flow = Flow()
            self.analysis = Analysis()
            self.sources = Sources()
            self.parameters = {}
        def top_module(self): return "top"
    result = run_flow(Config(), ConstraintSet(name="top"), "# sdc\n", "COMPLETE", [], backend="mock", run_id="failure-safe", qor_repository=FailingRepository())
    assert result["status"] == RunStatus.MOCK.value and result["qor_result"].power is None
    assert result["persistence_warning"].startswith("QOR_DATABASE_PERSISTENCE_WARNING")
    assert (Path(result["run_dir"]) / "qor.json").is_file() and (Path(result["run_dir"]) / "run_manifest.json").is_file()


def test_17_cli_history_queries_and_json_output_are_deterministic(tmp_path):
    repo = _repo(tmp_path); _record(repo, tmp_path, run_id="cli", power=1.0)
    runner = CliRunner(); output_dir = str(tmp_path / "output")
    text = runner.invoke(app, ["history", "--output-dir", output_dir, "--run-id", "cli"])
    assert text.exit_code == 0 and "automatic_replay_supported" in text.output
    structured = runner.invoke(app, ["history", "--output-dir", output_dir, "--best", "power", "--json"])
    repeated = runner.invoke(app, ["history", "--output-dir", output_dir, "--best", "power", "--json"])
    assert structured.exit_code == 0 and json.loads(structured.output)[0]["evaluation"]["evaluation_id"] == "cli"
    assert structured.output == repeated.output


def test_18_cli_explicit_legacy_import_never_executes_eda_or_cache(tmp_path):
    output = tmp_path / "cli-output"; output.mkdir()
    # Root optimizer snapshots are imported only when the explicit history
    # command is requested; this does not execute any flow/cache operation.
    (output / "candidates.jsonl").write_text(json.dumps({
        "id": "OLD", "generation": 0, "decision": "FINAL", "validity": "VALIDATED",
        "hard_feasible": True, "blocked": False, "changes": ["baseline"],
        "mutated_constraints": [], "qor": {"setup_wns_ns": 1.0, "power_status": "UNAVAILABLE"},
        "mcmm": {"candidate_id": "OLD", "active_scenarios": ["slow"], "feasible": True,
                 "infeasible": False, "blocked": False, "invalid": False, "global_status": "feasible",
                 "global_reason": "all pass", "limiting_scenarios": ["slow"], "objectives": {},
                 "scenario_results": {"slow": {"scenario_id": "slow", "mode": "functional",
                     "corner": "slow", "feasible": True, "blocked": False, "invalid": False,
                     "status": "feasible", "qor": {"setup_wns_ns": 1.0, "power_status": "UNAVAILABLE"}}}},
    }) + "\n", encoding="utf-8")
    # JSONL wins over the overwrite-prone fallback snapshot.
    (output / "optimizer_state.json").write_text(json.dumps({"candidates": [{"id": "IGNORED"}]}), encoding="utf-8")
    result = CliRunner().invoke(app, ["history", "--output-dir", str(output), "--import-legacy", "--json"])
    payload = json.loads(result.output)
    assert result.exit_code == 0 and payload["imported"] == [] and "No run directory" in payload["diagnostics"][0]
    assert payload["optimizer_sessions"][0]["status"] == "imported"
    legacy_session = payload["optimizer_sessions"][0]["session_id"]
    imported = SQLiteQoRRepository.for_output_dir(output)
    assert imported.get_candidate(legacy_session, "OLD") is not None
    assert imported.get_candidate(legacy_session, "IGNORED") is None
    assert imported.list_mcmm_scenarios(session_id=legacy_session, candidate_id="OLD")[0]["scenario_id"] == "slow"
    # Without JSONL the state file is a deterministic fallback.
    fallback = tmp_path / "state-fallback"; fallback.mkdir()
    (fallback / "optimizer_state.json").write_text(json.dumps({"candidates": [{
        "id": "STATE", "hard_feasible": False, "blocked": True,
    }]}), encoding="utf-8")
    imported_state = SQLiteQoRRepository.for_output_dir(fallback)
    state_result = imported_state.import_legacy_artifacts()
    assert state_result["optimizer_sessions"][0]["source"].endswith("optimizer_state.json")
    assert imported_state.get_candidate(state_result["optimizer_sessions"][0]["session_id"], "STATE") is not None


# Additional focused cases map one-for-one to the Step-21 repository contract.

def test_19_flow_integration_indexes_a_blocked_real_manifest_after_write(tmp_path):
    class Flow:
        output_dir = str(tmp_path / "real-output")
        backend = "yosys_opensta"
        stage = "synthesis_sta"
        def liberty_files(self): return []
    class Analysis: safe_mode = "balanced"
    class Sources:
        def __init__(self):
            self.defines = []
            self.include_dirs = []
    class Config:
        def __init__(self):
            self._config_path = str(tmp_path / "project.yaml")
            self.flow = Flow()
            self.analysis = Analysis()
            self.sources = Sources()
            self.parameters = {}
        def top_module(self): return "top"
    source = tmp_path / "top.v"
    source.write_text("module top; endmodule\n", encoding="utf-8")
    out = run_flow(Config(), ConstraintSet(name="top"), "# sdc\n", "COMPLETE", [source],
                   backend="yosys_opensta", run_id="blocked-real", yosys_bin=str(tmp_path / "none-y"),
                   sta_bin=str(tmp_path / "none-s"))
    assert out["status"] == RunStatus.BLOCKED.value
    stored = SQLiteQoRRepository.for_output_dir(Flow.output_dir).get_run("blocked-real")
    assert stored["qor"] is None
    assert stored["evaluation"]["run_status"] == RunStatus.BLOCKED.value
    assert (Path(out["run_dir"]) / "run_manifest.json").is_file()


def test_20_flow_record_keeps_existing_constraint_set_hash(tmp_path):
    repo = _repo(tmp_path)
    cset = ConstraintSet(name="identity")
    q = _qor(run_id="identity")
    manifest, run_dir = _manifest(tmp_path, "identity")
    repo.record_flow_evaluation(q, manifest, run_dir=run_dir, run_status="SUCCESS", constraint_set=cset)
    assert repo.get_run("identity")["evaluation"]["constraint_set_hash"] == stable_hash_cset(cset)


def test_21_session_candidate_ids_are_scoped_not_globally_inferred(tmp_path):
    repo = _repo(tmp_path)
    cset = ConstraintSet(name="scope")
    for sid in ("one", "two"):
        c = Candidate(id="C000", constraint_set=cset)
        repo.record_optimizer_session(OptimizationResult(baseline=c, final=c, all_candidates=[c]), session_id=sid)
    assert repo.get_candidate("one", "C000")["candidate_key"] == "one:C000"
    assert repo.get_candidate("two", "C000")["candidate_key"] == "two:C000"


def test_22_optimizer_session_conflict_is_explicit(tmp_path):
    repo = _repo(tmp_path)
    c = Candidate(id="C000", constraint_set=ConstraintSet(name="a"))
    session = OptimizationResult(baseline=c, final=c, all_candidates=[c])
    repo.record_optimizer_session(session, session_id="same-session")
    changed = Candidate(id="C000", constraint_set=ConstraintSet(name="b"), generated_changes=["changed evidence"])
    with pytest.raises(RecordConflictError, match="session"):
        repo.record_optimizer_session(OptimizationResult(baseline=changed, final=changed, all_candidates=[changed]),
                                      session_id="same-session")


def test_23_physical_flow_evaluation_can_link_to_session_candidate(tmp_path):
    repo = _repo(tmp_path)
    q, _, _ = _record(repo, tmp_path, run_id="physical", candidate_id="C000")
    c = Candidate(id="C000", constraint_set=ConstraintSet(name="physical"), qor=q)
    repo.record_optimizer_session(OptimizationResult(baseline=c, final=c, all_candidates=[c]), session_id="link")
    assert repo.get_run("physical")["evaluation"]["candidate_key"] == "link:C000"
    # A cache-reused physical execution may legitimately support a candidate
    # in a new session; preserve its locator in a logical projection instead
    # of conflating or rejecting session-scoped identities.
    second = Candidate(id="C000", constraint_set=ConstraintSet(name="physical"), qor=q)
    repo.record_optimizer_session(OptimizationResult(baseline=second, final=second, all_candidates=[second]), session_id="link-two")
    linked = repo.list_runs(candidate_id="C000", session_id="link-two")
    assert linked[0]["evaluation"]["record_kind"] == "optimizer_logical"
    assert linked[0]["evaluation"]["manifest_extra"]["physical_evaluation_id"] == "physical"


def test_24_list_runs_filters_status_candidate_scenario_and_constraint_hash(tmp_path):
    repo = _repo(tmp_path)
    cset = ConstraintSet(name="filters")
    q = _qor(run_id="filter-a", candidate_id="A", scenario="S1")
    m, d = _manifest(tmp_path, "filter-a", candidate_id="A")
    repo.record_flow_evaluation(q, m, run_dir=d, run_status="SUCCESS", constraint_set=cset)
    assert [item["evaluation"]["run_id"] for item in repo.list_runs(candidate_id="A")] == ["filter-a"]
    assert [item["evaluation"]["run_id"] for item in repo.list_runs(scenario_id="S1")] == ["filter-a"]
    assert [item["evaluation"]["run_id"] for item in repo.list_runs(run_status="SUCCESS")] == ["filter-a"]
    assert repo.find_by_constraint_set(stable_hash_cset(cset))[0]["evaluation"]["run_id"] == "filter-a"


def test_25_provenance_query_retains_validation_formal_and_input_identity(tmp_path):
    repo = _repo(tmp_path)
    _record(repo, tmp_path, run_id="provenance")
    provenance = repo.get_provenance("provenance")
    assert provenance["input_hashes"]["includes"] == {"inc": "inc-hash"}
    assert provenance["tool_identity"]["opensta"]["version"] == "2.5"
    assert repo.get_run("provenance")["qor"]["feasibility"]["validation"] == "PASS"
    assert repo.get_run("provenance")["qor"]["feasibility"]["formal"] == "VERIFIED"


def test_26_best_power_requires_canonical_available_not_estimated(tmp_path):
    repo = _repo(tmp_path)
    _record(repo, tmp_path, run_id="available", power=2.0)
    _record(repo, tmp_path, run_id="estimated", power=0.1, power_status=PowerStatus.ESTIMATED.value)
    assert repo.best_qor("power")[0]["evaluation"]["run_id"] == "available"


def test_27_sql_null_is_not_numeric_zero_for_unknown_metrics(tmp_path):
    repo = _repo(tmp_path)
    _record(repo, tmp_path, run_id="nullable", setup_ns=None, power=None)
    conn = sqlite3.connect(repo.db_path)
    values = conn.execute("SELECT setup_wns_s, power_total_w FROM qor_measurements WHERE evaluation_id='nullable'").fetchone()
    assert values == (None, None)


def test_28_mock_is_excluded_by_default_from_best_setup(tmp_path):
    repo = _repo(tmp_path)
    _record(repo, tmp_path, run_id="real", setup_ns=1.0)
    _record(repo, tmp_path, run_id="mock-best", setup_ns=9.0, is_mock=True)
    assert repo.best_qor("setup_wns")[0]["evaluation"]["run_id"] == "real"
    assert repo.best_qor("setup_wns", include_mock=True)[0]["evaluation"]["run_id"] == "mock-best"


def test_29_constraint_snapshot_is_a_locator_not_a_database_copy(tmp_path):
    repo = _repo(tmp_path)
    snapshot = tmp_path / "ucm.json"
    snapshot.write_text('{"constraints": []}\n', encoding="utf-8")
    cset = ConstraintSet(name="snapshot")
    cset.metadata["snapshot_path"] = str(snapshot)
    q = _qor(run_id="snapshot")
    m, d = _manifest(tmp_path, "snapshot")
    repo.record_flow_evaluation(q, m, run_dir=d, run_status="SUCCESS", constraint_set=cset)
    conn = sqlite3.connect(repo.db_path)
    locator, digest = conn.execute("SELECT snapshot_artifact_ref, snapshot_sha256 FROM constraint_sets").fetchone()
    assert locator == str(snapshot) and digest == hash_file(snapshot)
    assert "constraints" not in conn.execute("SELECT sql FROM sqlite_master WHERE name='constraint_sets'").fetchone()[0]


def test_30_mcmm_graph_transaction_rolls_back_on_member_failure(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    c = Candidate(id="C", constraint_set=ConstraintSet(name="m"))
    repo.record_optimizer_session(OptimizationResult(baseline=c, final=c, all_candidates=[c]), session_id="rollback-mcmm")
    m = MCMMResult(candidate_id="C", active_scenario_ids=["S"])
    m.scenario_results["S"] = ScenarioQoR(candidate_id="C", scenario_id="S", qor=_qor(run_id="", candidate_id="C"))
    global_feasibility(m)
    aggregate_objectives(m)
    original = repo._insert_mcmm_member
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("member failure")
    monkeypatch.setattr(repo, "_insert_mcmm_member", fail)
    with pytest.raises(RuntimeError, match="member failure"):
        repo.record_mcmm_aggregate(m, session_id="rollback-mcmm", candidate_id="C")
    conn = sqlite3.connect(repo.db_path)
    assert conn.execute("SELECT COUNT(*) FROM mcmm_aggregates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM mcmm_members").fetchone()[0] == 0


def test_31_artifact_order_is_deterministic(tmp_path):
    repo = _repo(tmp_path)
    _record(repo, tmp_path, run_id="artifact-order")
    assert [a["artifact_name"] for a in repo.get_artifacts("artifact-order")] == ["qor", "sdc"]


def test_32_legacy_import_skips_missing_manifest_deterministically(tmp_path):
    output = tmp_path / "no-manifest"
    (output / "runs" / "orphan").mkdir(parents=True)
    repo = SQLiteQoRRepository.for_output_dir(output)
    imported = repo.import_legacy_artifacts()
    assert imported["skipped"] == [{"run_id": "orphan", "reason": "missing manifest"}]


def test_33_legacy_import_recognizes_existing_flow_record_as_idempotent(tmp_path):
    repo = _repo(tmp_path)
    q, manifest, run_dir = _record(repo, tmp_path, run_id="already-indexed")
    (run_dir / "qor.json").write_text(json.dumps(q.summary()), encoding="utf-8")
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    outcome = repo.import_legacy_artifacts()
    assert outcome["skipped"] == ["already-indexed"] and not outcome["conflicts"]


def test_34_mock_flow_without_explicit_repository_does_not_create_sidecar(tmp_path):
    class Flow:
        output_dir = str(tmp_path / "out")
        backend = "mock"
        stage = "synthesis_sta"
        def liberty_files(self): return []
    class Analysis: safe_mode = "balanced"
    class Sources:
        def __init__(self):
            self.defines = []
            self.include_dirs = []
    class Config:
        def __init__(self):
            self._config_path = str(tmp_path / "project.yaml")
            self.flow = Flow()
            self.analysis = Analysis()
            self.sources = Sources()
            self.parameters = {}
        def top_module(self): return "top"
    result = run_flow(Config(), ConstraintSet(name="top"), "# sdc\n", "COMPLETE", [], backend="mock", run_id="no-sidecar")
    assert result["status"] == RunStatus.MOCK.value
    assert not (Path(Flow.output_dir) / "qor.sqlite3").exists()


def test_35_history_cli_candidate_session_and_selector_validation(tmp_path):
    repo = _repo(tmp_path)
    c = Candidate(id="C000", constraint_set=ConstraintSet(name="cli"), qor=_qor(run_id="", candidate_id="C000"))
    repo.record_optimizer_session(OptimizationResult(baseline=c, final=c, all_candidates=[c]), session_id="cli-session")
    runner = CliRunner()
    out = str(tmp_path / "output")
    result = runner.invoke(app, ["history", "--output-dir", out, "--candidate", "C000", "--session", "cli-session", "--json"])
    assert result.exit_code == 0 and json.loads(result.output)["candidate"]["candidate_key"] == "cli-session:C000"
    bad = runner.invoke(app, ["history", "--output-dir", out, "--run-id", "x", "--best", "power"])
    assert bad.exit_code == 2 and "Choose one" in bad.output


def test_36_cache_is_stored_as_evidence_and_query_never_calls_flow_cache(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _record(repo, tmp_path, run_id="cache-passive")
    from rca.eda import flow
    monkeypatch.setattr(flow, "_find_cached_run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cache used")))
    assert repo.get_run("cache-passive")["evaluation"]["cache_key"] == "cache-key"
    assert repo.get_replay_identity("cache-passive")["evaluation"]["cache_key"] == "cache-key"


def test_37_repeated_identical_candidate_and_mcmm_queries_are_deterministic(tmp_path):
    repo = _repo(tmp_path)
    c = Candidate(id="C", constraint_set=ConstraintSet(name="repeat"))
    repo.record_optimizer_session(OptimizationResult(baseline=c, final=c, all_candidates=[c]), session_id="repeat")
    m = MCMMResult(candidate_id="C", active_scenario_ids=[])
    mid = repo.record_mcmm_aggregate(m, session_id="repeat", candidate_id="C")
    assert repo.get_candidate("repeat", "C") == repo.get_candidate("repeat", "C")
    assert repo.list_mcmm_scenarios(mid) == repo.list_mcmm_scenarios(mid) == []
