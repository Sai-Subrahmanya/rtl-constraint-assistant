"""SQLite-backed historical QoR repository.

This module indexes the existing canonical :class:`QoRResult`, run manifests,
optimizer candidates, and MCMM results.  It is deliberately not an EDA cache,
an artifact store, or a second QoR model.  Existing JSON/JSONL artifacts remain
the interoperable evidence written by the flow; this sidecar database supplies
parameterized, deterministic historical queries.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from ..artifacts import RunManifest
from ..constraint_model import stable_hash_cset
from ..utils.enums import PowerStatus
from ..utils.hashing import hash_file, stable_hash
from .model import QoRResult

# "RCAQ" in hexadecimal.  This identifies RCA QoR sidecars without claiming
# compatibility with arbitrary SQLite files.
SQLITE_APPLICATION_ID = 0x52434151
SCHEMA_VERSION = 2
DEFAULT_BUSY_TIMEOUT_MS = 5_000


class QoRRepositoryError(RuntimeError):
    """Base exception for repository configuration and persistence failures."""


class SchemaVersionError(QoRRepositoryError):
    """Raised when a database is older/newer/internally inconsistent."""


class RecordConflictError(QoRRepositoryError):
    """Raised when an existing stable identifier has different source evidence."""


def _json(value: Any) -> str:
    """Stable JSON for sparse data and fingerprints."""
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _bool(value: Any) -> int:
    return 1 if bool(value) else 0


def _candidate_key(session_id: str, candidate_id: str) -> str:
    """Return repository-safe candidate identity; candidate IDs are session scoped."""
    return f"{session_id}:{candidate_id}"


def _area_source(qor: QoRResult | None) -> str:
    if qor is None:
        return "unknown"
    if qor.area is not None:
        return "real"
    if qor.area_proxy is not None:
        return "proxy"
    return "unknown"


def _qor_values(qor: QoRResult | None) -> dict[str, Any]:
    """Project the existing QoR model into normalized storage values.

    This is a persistence projection, not a model replacement.  Internal
    timing remains seconds, power remains watts, and missing values remain
    ``None`` for SQLite ``NULL``.
    """
    if qor is None:
        return {}
    return {
        "setup_wns_s": qor.setup_wns,
        "setup_tns_s": qor.setup_tns,
        "setup_violations": qor.setup_violations,
        "hold_wns_s": qor.hold_wns,
        "hold_tns_s": qor.hold_tns,
        "hold_violations": qor.hold_violations,
        "whs_s": qor.whs,
        "ths_s": qor.ths,
        "near_critical_count": qor.near_critical_count,
        "path_count": qor.path_count,
        "area": qor.area,
        "area_total": qor.area_total,
        "area_proxy": qor.area_proxy,
        "area_source": _area_source(qor),
        "area_comb": qor.area_comb,
        "area_seq": qor.area_seq,
        "area_buffer": qor.area_buffer,
        "cell_count": qor.cell_count,
        "ff_count": qor.ff_count,
        "comb_cell_count": qor.comb_cell_count,
        "buf_count": qor.buf_count,
        "buffer_count": qor.buffer_count,
        "power_w": qor.power,
        "power_total_w": qor.power_total,
        "power_dynamic_w": qor.power_dynamic,
        "power_leakage_w": qor.power_leakage,
        "power_status": qor.power_status,
        "constraint_quality": qor.constraint_quality,
        "validation_errors": qor.validation_errors,
        "unsafe_exceptions": qor.unsafe_exceptions,
        "margin_headroom_ns": qor.margin_headroom_ns,
        "margin_utilization": qor.margin_utilization,
        "congestion": qor.congestion,
        "runtime_seconds": qor.runtime_seconds,
        "critical_setup_json": _json(qor.critical_setup.to_dict()) if qor.critical_setup else None,
        "critical_hold_json": _json(qor.critical_hold.to_dict()) if qor.critical_hold else None,
        "wns_percentiles_json": _json(qor.wns_percentiles),
        "timing_distribution_json": _json(qor.timing_distribution),
        "feasibility_json": _json(qor.feasibility),
        "notes_json": _json(qor.notes),
        "diagnostics_json": _json(qor.diagnostics),
        # Raw report text can be large.  Existing artifacts remain authoritative;
        # retain only structured raw-report metadata here.
        "raw_reports_json": _json(qor.raw_reports),
    }


_QOR_COLUMNS = (
    "setup_wns_s", "setup_tns_s", "setup_violations", "hold_wns_s", "hold_tns_s",
    "hold_violations", "whs_s", "ths_s", "near_critical_count", "path_count", "area",
    "area_total", "area_proxy", "area_source", "area_comb", "area_seq", "area_buffer",
    "cell_count", "ff_count", "comb_cell_count", "buf_count", "buffer_count", "power_w",
    "power_total_w", "power_dynamic_w", "power_leakage_w", "power_status",
    "constraint_quality", "validation_errors", "unsafe_exceptions", "margin_headroom_ns",
    "margin_utilization", "congestion", "runtime_seconds", "critical_setup_json",
    "critical_hold_json", "wns_percentiles_json", "timing_distribution_json",
    "feasibility_json", "notes_json", "diagnostics_json", "raw_reports_json",
)


class SQLiteQoRRepository:
    """Queryable local QoR history sidecar.

    ``db_path`` is normally ``<flow.output_dir>/qor.sqlite3``.  Each method
    opens its own configured SQLite connection, making it safe for short-lived
    CLI calls and avoiding cross-command connection state.  WAL supports a
    reader while a separate writer transaction is active; it is not a
    distributed or cross-machine database.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    @classmethod
    def for_output_dir(cls, output_dir: str | Path) -> SQLiteQoRRepository:
        return cls(Path(output_dir) / "qor.sqlite3")

    # ---- lifecycle / migration ------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=DEFAULT_BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {DEFAULT_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        app_id = int(conn.execute("PRAGMA application_id").fetchone()[0])
        if app_id not in (0, SQLITE_APPLICATION_ID):
            conn.close()
            raise QoRRepositoryError(
                f"{self.db_path} is not an RCA QoR database (application_id={app_id})."
            )
        if app_id == 0:
            existing_tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
            if existing_tables:
                conn.close()
                raise QoRRepositoryError(
                    f"{self.db_path} has application_id=0 but already contains tables; refusing to claim it."
                )
            conn.execute(f"PRAGMA application_id = {SQLITE_APPLICATION_ID}")
        return conn

    def initialize(self) -> int:
        """Create or migrate the sidecar and return its supported version."""
        return self.migrate()

    def migrate(self) -> int:
        conn = self._connect()
        try:
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            has_ledger = bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone())
            ledger_version = 0
            if has_ledger:
                row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
                ledger_version = int(row["v"] or 0)
            if user_version != ledger_version:
                raise SchemaVersionError(
                    "QoR database schema is inconsistent: PRAGMA user_version "
                    f"is {user_version}, migration ledger is {ledger_version}."
                )
            if user_version > SCHEMA_VERSION:
                raise SchemaVersionError(
                    f"QoR database schema {user_version} is newer than supported {SCHEMA_VERSION}; "
                    "refusing to downgrade."
                )
            for version in range(user_version + 1, SCHEMA_VERSION + 1):
                conn.execute("BEGIN IMMEDIATE")
                try:
                    self._apply_migration(conn, version)
                    conn.execute(
                        "INSERT INTO schema_migrations(version, applied_at, description) "
                        "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)",
                        (version, _MIGRATION_DESCRIPTIONS[version]),
                    )
                    conn.execute(f"PRAGMA user_version = {version}")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return SCHEMA_VERSION
        finally:
            conn.close()

    def _apply_migration(self, conn: sqlite3.Connection, version: int) -> None:
        if version == 1:
            for statement in _MIGRATION_1:
                conn.execute(statement)
            return
        if version == 2:
            for statement in _MIGRATION_2:
                conn.execute(statement)
            return
        raise SchemaVersionError(f"No migration implementation for version {version}.")

    # ---- recording --------------------------------------------------------
    def record_flow_evaluation(
        self,
        qor: QoRResult | None,
        manifest: RunManifest | dict[str, Any],
        *,
        run_id: str | None = None,
        run_dir: str | Path | None = None,
        run_status: str | None = None,
        candidate_key: str | None = None,
        constraint_set: Any | None = None,
        record_kind: str = "flow",
    ) -> dict[str, Any]:
        """Index one already-written flow manifest and optional canonical QoR.

        The caller must write authoritative artifacts/manifests before invoking
        this method.  A repeat with the same stable evidence fingerprint is a
        no-op.  Reusing a run ID with changed evidence raises
        :class:`RecordConflictError`.
        """
        self.initialize()
        m = manifest.to_dict() if isinstance(manifest, RunManifest) else dict(manifest)
        resolved_run_id = str(run_id or getattr(qor, "run_id", "") or "")
        if not resolved_run_id and run_dir:
            resolved_run_id = Path(run_dir).name
        if not resolved_run_id:
            raise QoRRepositoryError("record_flow_evaluation requires a physical run_id.")
        qvalues = _qor_values(qor)
        constraint_set_hash = stable_hash_cset(constraint_set) if constraint_set is not None else None
        # Source fingerprint includes every canonical in-memory field that is
        # being indexed.  Keep a second artifact fingerprint so an explicit
        # legacy scan can recognize that the same physical manifest/qor.json
        # was already indexed, even though older summaries lack fields.
        artifact_fingerprint = _artifact_fingerprint(resolved_run_id, m, qor.summary() if qor else None)
        fingerprint = stable_hash({
            "run_id": resolved_run_id, "canonical_qor": _jsonable(qor), "manifest": m,
            "run_status": run_status, "constraint_set_hash": constraint_set_hash,
        })
        evaluation = self._evaluation_values(
            evaluation_id=resolved_run_id,
            run_id=resolved_run_id,
            candidate_id=str(m.get("candidate_id") or getattr(qor, "candidate_id", "") or ""),
            candidate_key=candidate_key,
            manifest=m,
            run_dir=run_dir,
            run_status=run_status,
            record_kind=record_kind,
            source_fingerprint=fingerprint,
            qor=qor,
            constraint_set_hash=constraint_set_hash,
            artifact_fingerprint=artifact_fingerprint,
        )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if constraint_set_hash:
                self._insert_constraint_set(conn, constraint_set_hash, constraint_set)
            self._insert_evaluation_graph(conn, evaluation, qvalues, m, qor)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_run(resolved_run_id) or {"evaluation_id": resolved_run_id}

    def record_optimizer_session(
        self,
        result: Any,
        *,
        session_id: str | None = None,
        project_name: str = "",
        output_dir: str | Path | None = None,
    ) -> str:
        """Record one optimizer invocation and all its session-scoped candidates.

        Candidates carrying a physical ``QoRResult.run_id`` are linked to an
        already-indexed flow evaluation when present.  Candidates without a
        physical run directory receive a clearly-labelled ``optimizer_logical``
        evaluation so mock/legacy optimizer history remains queryable without
        pretending it is a flow artifact.
        """
        self.initialize()
        session_id = session_id or f"opt_{uuid.uuid4().hex}"
        candidates = list(getattr(result, "all_candidates", []) or [])
        snapshot = _jsonable(getattr(result, "summary", dict)())
        # Candidate JSON intentionally omits the full UCM.  Add the existing
        # stable constraint-set identity to session evidence so a reused
        # session ID cannot silently accept a different candidate model.
        candidate_identities = [
            {
                "id": getattr(candidate, "id", ""),
                "candidate": _jsonable(getattr(candidate, "to_dict", dict)()),
                "constraint_set_hash": (
                    getattr(candidate, "constraint_model_hash", "")
                    or (stable_hash_cset(candidate.constraint_set)
                        if getattr(candidate, "constraint_set", None) is not None else "")
                ),
            }
            for candidate in candidates
        ]
        session_fingerprint = stable_hash({
            "session_id": session_id, "result": snapshot, "candidates": candidate_identities,
        })
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT source_fingerprint FROM optimization_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if prior:
                if prior["source_fingerprint"] != session_fingerprint:
                    raise RecordConflictError(
                        f"Optimization session '{session_id}' already exists with different evidence."
                    )
                conn.commit()
                return session_id
            baseline = getattr(result, "baseline", None)
            final = getattr(result, "final", None)
            conn.execute(
                """INSERT INTO optimization_sessions(
                    session_id, started_at, completed_at, project_name, output_dir,
                    baseline_candidate_key, final_candidate_key, stop_reason, iterations,
                    eda_runs, cache_hits, cache_misses, optimizer_summary_json, source_fingerprint
                ) VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                    strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, project_name, str(output_dir or self.db_path.parent),
                    _candidate_key(session_id, baseline.id) if baseline else None,
                    _candidate_key(session_id, final.id) if final else None,
                    _enum_value(getattr(result, "stop_reason", "")),
                    getattr(result, "iterations", 0), getattr(result, "eda_runs", 0),
                    getattr(result, "cache_hits", 0), getattr(result, "cache_misses", 0),
                    _json(snapshot), session_fingerprint,
                ),
            )
            # First insert all candidates so parent references can be deferred safely.
            for candidate in candidates:
                self._insert_candidate(conn, session_id, candidate)
            for candidate in candidates:
                candidate_key = _candidate_key(session_id, str(getattr(candidate, "id", "")))
                mcmm = getattr(candidate, "mcmm", None)
                if mcmm is not None:
                    self._record_mcmm_in_transaction(conn, mcmm, candidate_key, session_id, candidate)
                elif getattr(candidate, "qor", None) is not None:
                    self._record_candidate_qor(conn, candidate, candidate_key, session_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return session_id

    def record_mcmm_aggregate(
        self,
        result: Any,
        *,
        session_id: str,
        candidate_id: str,
        candidate: Any | None = None,
    ) -> str:
        """Persist an MCMM aggregate plus linked per-scenario evidence.

        The session/candidate must already exist; this prevents implicit lineage
        or cross-session candidate-ID inference.
        """
        self.initialize()
        candidate_key = _candidate_key(session_id, candidate_id)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM candidates WHERE candidate_key=?", (candidate_key,)).fetchone():
                raise QoRRepositoryError(
                    f"Candidate '{candidate_id}' is not recorded in session '{session_id}'."
                )
            mcmm_id = self._record_mcmm_in_transaction(conn, result, candidate_key, session_id, candidate)
            conn.commit()
            return mcmm_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _insert_candidate(self, conn: sqlite3.Connection, session_id: str, candidate: Any) -> str:
        cid = str(getattr(candidate, "id", ""))
        if not cid:
            raise QoRRepositoryError("Cannot persist an optimizer candidate without an id.")
        ckey = _candidate_key(session_id, cid)
        cset = getattr(candidate, "constraint_set", None)
        cset_hash = str(getattr(candidate, "constraint_model_hash", "") or "")
        if not cset_hash and cset is not None:
            cset_hash = stable_hash_cset(cset)
        if cset_hash:
            self._insert_constraint_set(conn, cset_hash, cset)
        parent_id = getattr(candidate, "parent_id", None)
        payload = _jsonable(getattr(candidate, "to_dict", dict)())
        fingerprint = stable_hash({"session_id": session_id, "candidate": payload, "cset_hash": cset_hash})
        conn.execute(
            """INSERT INTO candidates(
                candidate_key, session_id, candidate_id, parent_candidate_key, generation,
                constraint_set_hash, sdc_hash, decision, validity_status, decision_reason,
                hard_feasible, blocked, infeasible_reason, scenario, mode, corner,
                cache_key, cache_status, run_id, pareto_member, rank, priority_score,
                margin_headroom_ns, margin_utilization, global_status,
                limiting_scenarios_json, margin_limiting_scenarios_json, warnings_json,
                diagnostics_json, explanation_json, coverage_json, source_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ckey, session_id, cid,
                _candidate_key(session_id, str(parent_id)) if parent_id else None,
                getattr(candidate, "generation", 0), cset_hash or None,
                getattr(candidate, "sdc_hash", "") or None,
                _enum_value(getattr(candidate, "decision", "")),
                getattr(candidate, "validity_status", ""), getattr(candidate, "decision_reason", ""),
                _bool(getattr(candidate, "hard_feasible", False)), _bool(getattr(candidate, "blocked", False)),
                getattr(candidate, "infeasible_reason", ""), getattr(candidate, "scenario", ""),
                getattr(candidate, "mode", ""), getattr(candidate, "corner", ""),
                getattr(candidate, "cache_key", "") or None, getattr(candidate, "cache_status", "") or None,
                getattr(candidate, "run_id", "") or None, _bool(getattr(candidate, "pareto_member", False)),
                getattr(candidate, "rank", -1), getattr(candidate, "priority_score", None),
                getattr(candidate, "margin_headroom_ns", None), getattr(candidate, "margin_utilization", None),
                getattr(candidate, "global_status", ""), _json(getattr(candidate, "limiting_scenarios", [])),
                _json(getattr(candidate, "margin_limiting_scenarios", [])), _json(getattr(candidate, "warnings", [])),
                _json(getattr(candidate, "diagnostics", [])), _json(getattr(candidate, "explanation", {})),
                _json(getattr(candidate, "coverage", None)), fingerprint,
            ),
        )
        for ordinal, change in enumerate(getattr(candidate, "generated_changes", []) or []):
            mutated = (getattr(candidate, "mutated_constraint_ids", []) or [])
            conn.execute(
                "INSERT INTO candidate_mutations(candidate_key, ordinal, change_label, mutated_constraint_id) "
                "VALUES (?, ?, ?, ?)",
                (ckey, ordinal, str(change), str(mutated[ordinal]) if ordinal < len(mutated) else None),
            )
        return ckey

    def _insert_constraint_set(self, conn: sqlite3.Connection, cset_hash: str, cset: Any | None) -> None:
        snapshot_path, snapshot_hash = _constraint_snapshot(cset)
        conn.execute(
            """INSERT INTO constraint_sets(constraint_set_hash, name, snapshot_artifact_ref, snapshot_sha256)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(constraint_set_hash) DO NOTHING""",
            (cset_hash, getattr(cset, "name", "") if cset is not None else "", snapshot_path, snapshot_hash),
        )

    def _record_candidate_qor(
        self, conn: sqlite3.Connection, candidate: Any, candidate_key: str, session_id: str
    ) -> None:
        qor = candidate.qor
        physical_run_id = str(getattr(qor, "run_id", "") or "")
        logical_physical_ref: str | None = None
        real_run_id = physical_run_id
        if real_run_id:
            existing = conn.execute(
                "SELECT candidate_key FROM evaluations WHERE evaluation_id=?", (real_run_id,)
            ).fetchone()
            if existing:
                if existing["candidate_key"] in (None, candidate_key):
                    conn.execute("UPDATE evaluations SET candidate_key=? WHERE evaluation_id=?",
                                 (candidate_key, real_run_id))
                    return
                # A cache-reused physical execution can support candidates in
                # multiple optimization sessions. Keep its original candidate
                # association intact and store a separately labelled logical
                # projection for this session, retaining the physical locator
                # as evidence rather than guessing a global candidate identity.
                logical_physical_ref = real_run_id
                real_run_id = ""
        evaluation_id = real_run_id or f"logical:{session_id}:{getattr(candidate, 'id', '')}"
        candidate_cset = getattr(candidate, "constraint_set", None)
        candidate_cset_hash = str(getattr(candidate, "constraint_model_hash", "") or "")
        if not candidate_cset_hash and candidate_cset is not None:
            candidate_cset_hash = stable_hash_cset(candidate_cset)
        logical_manifest = (
            {"extra": {"physical_evaluation_id": logical_physical_ref}}
            if logical_physical_ref else {}
        )
        values = self._evaluation_values(
            evaluation_id=evaluation_id,
            run_id=real_run_id or None,
            candidate_id=str(getattr(candidate, "id", "")),
            candidate_key=candidate_key,
            manifest=logical_manifest,
            run_dir=None,
            run_status="MOCK" if bool(getattr(qor, "is_mock", False)) else "OPTIMIZER_LOGICAL",
            record_kind="optimizer_logical",
            source_fingerprint=stable_hash({"candidate_key": candidate_key, "qor": _jsonable(qor)}),
            qor=qor,
            constraint_set_hash=candidate_cset_hash or None,
            artifact_fingerprint=None,
        )
        if candidate_cset_hash:
            self._insert_constraint_set(conn, candidate_cset_hash, candidate_cset)
        self._insert_evaluation_graph(conn, values, _qor_values(qor), logical_manifest, qor)

    def _record_mcmm_in_transaction(
        self, conn: sqlite3.Connection, result: Any, candidate_key: str, session_id: str, candidate: Any | None
    ) -> str:
        payload = _jsonable(getattr(result, "to_dict", dict)())
        mcmm_id = stable_hash({"candidate_key": candidate_key, "mcmm": payload})
        fingerprint = stable_hash({"candidate_key": candidate_key, "mcmm": payload})
        existing = conn.execute("SELECT source_fingerprint FROM mcmm_aggregates WHERE mcmm_id=?", (mcmm_id,)).fetchone()
        if existing:
            if existing["source_fingerprint"] != fingerprint:
                raise RecordConflictError(f"MCMM aggregate '{mcmm_id}' conflicts with existing evidence.")
            return mcmm_id
        conn.execute(
            """INSERT INTO mcmm_aggregates(
                mcmm_id, candidate_key, session_id, candidate_id, feasible, infeasible, blocked, invalid,
                global_status, global_reason, limiting_scenarios_json, margin_headroom_ns,
                margin_utilization, margin_limiting_scenarios_json, cache_key, cache_status,
                run_ids_json, diagnostics_json, eda_runs, cache_hits, cache_misses, provenance_json,
                active_scenarios_json, source_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mcmm_id, candidate_key, session_id, getattr(result, "candidate_id", "") or getattr(candidate, "id", ""),
                _bool(getattr(result, "feasible", False)), _bool(getattr(result, "infeasible", False)),
                _bool(getattr(result, "blocked", False)), _bool(getattr(result, "invalid", False)),
                getattr(result, "global_status", ""), getattr(result, "global_reason", ""),
                _json(getattr(result, "limiting_scenarios", [])), getattr(result, "margin_headroom_ns", None),
                getattr(result, "margin_utilization", None), _json(getattr(result, "margin_limiting_scenarios", [])),
                getattr(result, "cache_key", "") or None, getattr(result, "cache_status", "") or None,
                _json(getattr(result, "run_ids", [])), _json(getattr(result, "diagnostics", [])),
                getattr(result, "eda_runs", 0), getattr(result, "cache_hits", 0),
                getattr(result, "cache_misses", 0), _json(getattr(result, "provenance", {})),
                _json(getattr(result, "active_scenario_ids", [])), fingerprint,
            ),
        )
        for name, objective in sorted((getattr(result, "objectives", {}) or {}).items()):
            conn.execute(
                """INSERT INTO mcmm_objectives(
                    mcmm_id, name, value, unknown_value, incomparable, limiting_scenarios_json,
                    direction, area_source, scenarios_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mcmm_id, name, getattr(objective, "value", None), _bool(getattr(objective, "unknown", False)),
                    _bool(getattr(objective, "incomparable", False)), _json(getattr(objective, "limiting", [])),
                    getattr(objective, "direction", ""), getattr(objective, "area_source", None),
                    _json(getattr(objective, "scenarios", [])),
                ),
            )
        scenario_results = getattr(result, "scenario_results", {}) or {}
        active_ids = list(getattr(result, "active_scenario_ids", []) or [])
        for ordinal, sid in enumerate(active_ids):
            sqor = scenario_results.get(sid)
            self._insert_mcmm_member(conn, mcmm_id, candidate_key, session_id, sid, ordinal, sqor)
        # Preserve an unexpected (but present) scenario record rather than dropping it.
        for sid in sorted(set(scenario_results) - set(active_ids)):
            self._insert_mcmm_member(conn, mcmm_id, candidate_key, session_id, sid, len(active_ids), scenario_results[sid])
        return mcmm_id

    def _insert_mcmm_member(
        self, conn: sqlite3.Connection, mcmm_id: str, candidate_key: str, session_id: str,
        sid: str, ordinal: int, sqor: Any | None,
    ) -> None:
        evaluation_id = None
        if sqor is not None and getattr(sqor, "qor", None) is not None:
            qor = sqor.qor
            physical_id = str(getattr(sqor, "run_id", "") or getattr(qor, "run_id", "") or "")
            logical_physical_ref: str | None = None
            if physical_id:
                existing = conn.execute("SELECT candidate_key FROM evaluations WHERE evaluation_id=?", (physical_id,)).fetchone()
                if existing:
                    if existing["candidate_key"] in (None, candidate_key):
                        conn.execute("UPDATE evaluations SET candidate_key=? WHERE evaluation_id=?",
                                     (candidate_key, physical_id))
                        evaluation_id = physical_id
                    else:
                        # A historical physical scenario may be cache-reused by
                        # a candidate in another session. Preserve both scoped
                        # candidate records using a logical projection that
                        # points back to the original physical evaluation.
                        logical_physical_ref = physical_id
                        physical_id = ""
                else:
                    evaluation_id = physical_id
            if not evaluation_id:
                evaluation_id = f"logical:{session_id}:{candidate_key.rsplit(':', 1)[-1]}:{sid}"
            if not conn.execute("SELECT 1 FROM evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone():
                manifest = {"candidate_id": getattr(sqor, "candidate_id", ""), "mode": getattr(sqor, "mode", ""),
                            "corner": getattr(sqor, "corner", ""), "tool": getattr(sqor, "tool", ""),
                            "tool_version": getattr(sqor, "tool_version", ""),
                            "flow_stage": getattr(qor, "flow_stage", ""),
                            "extra": ({"physical_evaluation_id": logical_physical_ref}
                                      if logical_physical_ref else {})}
                candidate_row = conn.execute(
                    "SELECT constraint_set_hash FROM candidates WHERE candidate_key=?", (candidate_key,)
                ).fetchone()
                values = self._evaluation_values(
                    evaluation_id=evaluation_id, run_id=physical_id or None,
                    candidate_id=str(getattr(sqor, "candidate_id", "")), candidate_key=candidate_key,
                    manifest=manifest, run_dir=None,
                    run_status=getattr(sqor, "status", ""), record_kind="mcmm_logical",
                    source_fingerprint=stable_hash({"mcmm_id": mcmm_id, "scenario": sid, "qor": _jsonable(qor)}),
                    qor=qor,
                    constraint_set_hash=candidate_row["constraint_set_hash"] if candidate_row else None,
                    artifact_fingerprint=None,
                )
                self._insert_evaluation_graph(conn, values, _qor_values(qor), manifest, qor)
        conn.execute(
            """INSERT INTO mcmm_members(
                mcmm_id, scenario_id, ordinal, evaluation_id, candidate_key, mode, corner, name,
                feasible, blocked, invalid, status, infeasible_reason, cache_key, cache_status,
                run_id, backend, tool, tool_version, diagnostics_json, margin_headroom_ns,
                margin_utilization, limiting, is_global_binding, provenance_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mcmm_id, sid, ordinal, evaluation_id, candidate_key,
                getattr(sqor, "mode", "") if sqor else "", getattr(sqor, "corner", "") if sqor else "",
                getattr(sqor, "name", "") if sqor else "", _bool(getattr(sqor, "feasible", False)),
                _bool(getattr(sqor, "blocked", False)), _bool(getattr(sqor, "invalid", False)),
                getattr(sqor, "status", "blocked") if sqor else "blocked",
                getattr(sqor, "infeasible_reason", "missing_scenario_result") if sqor else "missing_scenario_result",
                getattr(sqor, "cache_key", "") if sqor else None, getattr(sqor, "cache_status", "") if sqor else None,
                getattr(sqor, "run_id", "") if sqor else None, getattr(sqor, "backend", "") if sqor else "",
                getattr(sqor, "tool", "") if sqor else "", getattr(sqor, "tool_version", "") if sqor else "",
                _json(getattr(sqor, "diagnostics", []) if sqor else [f"missing scenario result: {sid}"]),
                getattr(sqor, "margin_headroom_ns", None) if sqor else None,
                getattr(sqor, "margin_utilization", None) if sqor else None,
                _bool(getattr(sqor, "limiting", False)) if sqor else 1,
                _bool(getattr(sqor, "is_global_binding", False)) if sqor else 1,
                _json(getattr(sqor, "provenance", {}) if sqor else {}),
            ),
        )

    def _evaluation_values(
        self, *, evaluation_id: str, run_id: str | None, candidate_id: str, candidate_key: str | None,
        manifest: dict[str, Any], run_dir: str | Path | None, run_status: str | None,
        record_kind: str, source_fingerprint: str, qor: QoRResult | None,
        constraint_set_hash: str | None = None,
        artifact_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        extra = manifest.get("extra") or {}
        return {
            "evaluation_id": evaluation_id,
            "run_id": run_id,
            "record_kind": record_kind,
            "candidate_key": candidate_key,
            "candidate_id": candidate_id,
            "manifest_timestamp": manifest.get("timestamp"),
            "run_status": run_status or (qor.feasibility.get("status") if qor else None),
            "backend": getattr(qor, "backend", "") or _tool_backend(manifest),
            "backend_version": getattr(qor, "backend_version", "") or "",
            "tool": getattr(qor, "tool", "") or manifest.get("tool", ""),
            "tool_version": getattr(qor, "tool_version", "") or manifest.get("tool_version", ""),
            "flow_stage": getattr(qor, "flow_stage", "") or manifest.get("flow_stage", ""),
            "scenario": getattr(qor, "scenario", "") or extra.get("scenario") or "default",
            "mode": getattr(qor, "mode", "") or manifest.get("mode", ""),
            "corner": getattr(qor, "corner", "") or manifest.get("corner", ""),
            "is_mock": _bool(getattr(qor, "is_mock", False)),
            "constraint_set_hash": constraint_set_hash,
            "sdc_hash": manifest.get("sdc_hash") or None,
            "config_hash": manifest.get("config_hash") or None,
            "cache_key": getattr(qor, "cache_key", "") or extra.get("cache_key") or None,
            # Flow manifests represent physical misses; cache hits retain the
            # original physical run rather than fabricating a second execution.
            "cache_status": (getattr(qor, "cache_status", "") or
                             ("MISS" if (getattr(qor, "cache_key", "") or extra.get("cache_key")) else None)),
            "library_locator": manifest.get("library") or None,
            "run_dir": str(run_dir) if run_dir else None,
            "tool_identity_json": _json(manifest.get("tool_identity") or {}),
            "input_hashes_json": _json(manifest.get("input_hashes") or {}),
            "manifest_extra_json": _json(extra),
            "artifact_fingerprint": artifact_fingerprint,
            "source_fingerprint": source_fingerprint,
        }

    def _insert_evaluation_graph(
        self, conn: sqlite3.Connection, evaluation: dict[str, Any], qvalues: dict[str, Any],
        manifest: dict[str, Any], qor: QoRResult | None,
    ) -> None:
        existing = conn.execute(
            "SELECT source_fingerprint, artifact_fingerprint FROM evaluations WHERE evaluation_id=?",
            (evaluation["evaluation_id"],),
        ).fetchone()
        if existing:
            # A legacy import of exactly the same on-disk evidence is a no-op
            # even when its old summary cannot reproduce modern in-memory-only
            # QoR fields.  Any other source mismatch is an explicit conflict.
            same_artifact = (
                # Only an explicit legacy reconciliation may use the retained
                # on-disk artifact identity. A normal flow write with a reused
                # run ID and changed canonical source is always a conflict.
                evaluation["record_kind"] == "legacy_import"
                and evaluation["artifact_fingerprint"] is not None
                and existing["artifact_fingerprint"] == evaluation["artifact_fingerprint"]
            )
            if existing["source_fingerprint"] != evaluation["source_fingerprint"] and not same_artifact:
                raise RecordConflictError(
                    f"Evaluation '{evaluation['evaluation_id']}' already exists with a different source fingerprint."
                )
            # A physical flow is allowed to gain its session-safe candidate link later.
            if evaluation["candidate_key"]:
                current = conn.execute(
                    "SELECT candidate_key FROM evaluations WHERE evaluation_id=?", (evaluation["evaluation_id"],)
                ).fetchone()["candidate_key"]
                if current not in (None, evaluation["candidate_key"]):
                    raise RecordConflictError(
                        f"Evaluation '{evaluation['evaluation_id']}' is already linked to a different candidate."
                    )
                if current is None:
                    conn.execute("UPDATE evaluations SET candidate_key=? WHERE evaluation_id=?",
                                 (evaluation["candidate_key"], evaluation["evaluation_id"]))
            return
        columns = tuple(evaluation)
        conn.execute(
            f"INSERT INTO evaluations ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            tuple(evaluation[column] for column in columns),
        )
        if qvalues:
            values = {column: qvalues.get(column) for column in _QOR_COLUMNS}
            conn.execute(
                f"INSERT INTO qor_measurements (evaluation_id, {', '.join(_QOR_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in range(len(_QOR_COLUMNS) + 1))})",
                (evaluation["evaluation_id"], *(values[column] for column in _QOR_COLUMNS)),
            )
            self._insert_power_evidence(conn, evaluation["evaluation_id"], qor)
        for name, rel_path in sorted((manifest.get("artifacts") or {}).items()):
            digest = (manifest.get("artifact_hashes") or {}).get(name)
            kind = "sha256" if digest and not str(digest).startswith("size=") else (
                "size" if digest else "unavailable"
            )
            conn.execute(
                "INSERT INTO evaluation_artifacts(evaluation_id, artifact_name, relative_path, content_hash, hash_kind) "
                "VALUES (?, ?, ?, ?, ?)",
                (evaluation["evaluation_id"], name, str(rel_path), digest, kind),
            )

    def _insert_power_evidence(self, conn: sqlite3.Connection, evaluation_id: str, qor: QoRResult | None) -> None:
        if qor is None:
            return
        provenance = dict((qor.raw_reports or {}).get("power") or {})
        if not provenance and qor.power_status == PowerStatus.UNAVAILABLE.value:
            # Do not invent parser evidence where none existed.
            return
        conn.execute(
            """INSERT INTO power_evidence(
                evaluation_id, canonical_power_status, parser_status, report_format, parser_version,
                configured_producer, producer_version, tool_version, original_unit, normalized_unit,
                scenario_id, mode, corner, report_path, report_sha256, reported_internal_w,
                reported_switching_w, diagnostics_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                evaluation_id, qor.power_status, provenance.get("parsing_status"),
                provenance.get("format"), provenance.get("parser_format_version"),
                provenance.get("configured_producer") or provenance.get("producer"),
                provenance.get("producer_version"), provenance.get("tool_version") or qor.tool_version,
                provenance.get("original_unit"), provenance.get("normalized_unit"),
                provenance.get("scenario_id") or qor.scenario, provenance.get("mode") or qor.mode,
                provenance.get("corner") or qor.corner, provenance.get("report_path"), provenance.get("sha256"),
                provenance.get("reported_internal_w"), provenance.get("reported_switching_w"),
                _json(provenance.get("diagnostics") or []),
            ),
        )

    # ---- legacy import ----------------------------------------------------
    def import_legacy_artifacts(self, output_dir: str | Path | None = None) -> dict[str, Any]:
        """Explicitly and deterministically index existing run-directory artifacts.

        Legacy summaries are mapped directly so a missing historical field stays
        SQL ``NULL`` rather than acquiring a modern dataclass default.  Existing
        JSON files are read only and never rewritten.
        """
        self.initialize()
        root = Path(output_dir) if output_dir is not None else self.db_path.parent
        runs_dir = root / "runs"
        result: dict[str, Any] = {
            "imported": [], "skipped": [], "conflicts": [], "diagnostics": [],
            "optimizer_sessions": [], "optimizer_skipped": [],
        }
        if not runs_dir.is_dir():
            result["diagnostics"].append(f"No run directory exists at {runs_dir}.")
            self._import_legacy_optimizer_snapshot(root, result)
            return result
        for run_dir in sorted(path for path in runs_dir.iterdir() if path.is_dir()):
            manifest_path = run_dir / "run_manifest.json"
            if not manifest_path.is_file():
                manifest_path = run_dir / "manifest.json"
            if not manifest_path.is_file():
                result["skipped"].append({"run_id": run_dir.name, "reason": "missing manifest"})
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(manifest, dict):
                    raise TypeError("manifest is not an object")
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                result["skipped"].append({"run_id": run_dir.name, "reason": f"invalid manifest: {exc}"})
                continue
            qor_path = run_dir / "qor.json"
            summary: dict[str, Any] | None = None
            if qor_path.is_file():
                try:
                    data = json.loads(qor_path.read_text(encoding="utf-8"))
                    summary = data if isinstance(data, dict) else None
                    if summary is None:
                        raise ValueError("QoR summary is not an object")
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    result["skipped"].append({"run_id": run_dir.name, "reason": f"invalid qor.json: {exc}"})
                    continue
            try:
                outcome = self._import_legacy_run(run_dir.name, run_dir, manifest, summary)
                result[outcome].append(run_dir.name)
            except RecordConflictError as exc:
                result["conflicts"].append({"run_id": run_dir.name, "reason": str(exc)})
        self._import_legacy_optimizer_snapshot(root, result)
        return result

    def _import_legacy_optimizer_snapshot(self, root: Path, result: dict[str, Any]) -> None:
        """Import the currently retained candidate JSONL/state snapshot explicitly.

        Root optimizer files are overwrite-prone snapshots, not complete past
        history.  They can still supply a queryable session-scoped candidate
        record when present; absent UCM detail remains absent rather than being
        reconstructed or guessed.
        """
        jsonl = root / "candidates.jsonl"
        state = root / "optimizer_state.json"
        records: list[dict[str, Any]] = []
        source_path: Path | None = None
        summary: dict[str, Any] = {}
        if jsonl.is_file():
            source_path = jsonl
            try:
                for line_no, line in enumerate(jsonl.read_text(encoding="utf-8").splitlines(), 1):
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise TypeError(f"line {line_no} is not an object")
                    records.append(item)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                result["optimizer_skipped"].append({"path": str(jsonl), "reason": f"invalid candidates.jsonl: {exc}"})
                return
            # JSONL is the preferred retained candidate evidence.  Do not fold
            # a separately mutable state snapshot into its identity or metadata:
            # doing so would make an unchanged JSONL import conflict merely
            # because the non-authoritative fallback file changed.
        elif state.is_file():
            source_path = state
            try:
                summary_data = json.loads(state.read_text(encoding="utf-8"))
                if not isinstance(summary_data, dict):
                    raise TypeError("optimizer state is not an object")
                summary = summary_data
                records = [item for item in summary.get("candidates", []) if isinstance(item, dict)]
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                result["optimizer_skipped"].append({"path": str(state), "reason": f"invalid optimizer_state.json: {exc}"})
                return
        else:
            return
        if not records:
            result["optimizer_skipped"].append({"path": str(source_path), "reason": "no candidate records"})
            return
        assert source_path is not None
        snapshot_hash = hash_file(source_path)
        session_id = f"legacy_optimizer_{snapshot_hash[:16]}"
        fingerprint = stable_hash({"source_path": str(source_path), "sha256": snapshot_hash, "summary": summary})
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT source_fingerprint FROM optimization_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if existing:
                if existing["source_fingerprint"] != fingerprint:
                    raise RecordConflictError(
                        f"Legacy optimizer session '{session_id}' has conflicting snapshot evidence."
                    )
                conn.commit()
                result["optimizer_sessions"].append({"session_id": session_id, "status": "skipped"})
                return
            baseline_id = summary.get("baseline_id")
            final_id = summary.get("final_id")
            conn.execute(
                """INSERT INTO optimization_sessions(
                    session_id, started_at, completed_at, project_name, output_dir,
                    baseline_candidate_key, final_candidate_key, stop_reason, iterations,
                    eda_runs, cache_hits, cache_misses, optimizer_summary_json, source_fingerprint
                ) VALUES (?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, str(root), _candidate_key(session_id, str(baseline_id)) if baseline_id else None,
                    _candidate_key(session_id, str(final_id)) if final_id else None, summary.get("stop_reason"),
                    summary.get("iterations"), summary.get("eda_runs"), summary.get("cache_hits"),
                    summary.get("cache_misses"), _json(summary), fingerprint,
                ),
            )
            for item in records:
                self._insert_legacy_candidate(conn, session_id, item)
            conn.commit()
            result["optimizer_sessions"].append({"session_id": session_id, "status": "imported", "source": str(source_path)})
        except RecordConflictError as exc:
            conn.rollback()
            result["optimizer_skipped"].append({"path": str(source_path), "reason": str(exc)})
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _insert_legacy_candidate(self, conn: sqlite3.Connection, session_id: str, item: dict[str, Any]) -> None:
        candidate_id = str(item.get("id") or "")
        if not candidate_id:
            raise QoRRepositoryError("Legacy candidate record is missing id.")
        ckey = _candidate_key(session_id, candidate_id)
        conn.execute(
            """INSERT INTO candidates(
                candidate_key, session_id, candidate_id, parent_candidate_key, generation,
                constraint_set_hash, sdc_hash, decision, validity_status, decision_reason,
                hard_feasible, blocked, infeasible_reason, scenario, mode, corner,
                cache_key, cache_status, run_id, pareto_member, rank, priority_score,
                margin_headroom_ns, margin_utilization, global_status,
                limiting_scenarios_json, margin_limiting_scenarios_json, warnings_json,
                diagnostics_json, explanation_json, coverage_json, source_fingerprint
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            (
                ckey, session_id, candidate_id,
                _candidate_key(session_id, str(item["parent"])) if item.get("parent") else None,
                item.get("generation"), item.get("decision"), item.get("validity"), item.get("reason"),
                _bool(item.get("hard_feasible", item.get("feasible", False))), _bool(item.get("blocked", False)),
                item.get("infeasible_reason"), item.get("scenario"), item.get("mode"), item.get("corner"),
                item.get("cache_key"), item.get("cache_status"), item.get("run_id"), _bool(item.get("pareto", False)),
                item.get("rank"), item.get("margin_headroom_ns"), item.get("margin_utilization"),
                item.get("global_status"), _json(item.get("limiting_scenarios", [])),
                _json(item.get("margin_limiting_scenarios", [])), _json(item.get("warnings", [])),
                _json(item.get("diagnostics", [])), _json(item.get("explanation", {})),
                stable_hash({"legacy_session": session_id, "candidate": item}),
            ),
        )
        for ordinal, change in enumerate(item.get("changes") or []):
            mutations = item.get("mutated_constraints") or []
            conn.execute(
                "INSERT INTO candidate_mutations(candidate_key, ordinal, change_label, mutated_constraint_id) VALUES (?, ?, ?, ?)",
                (ckey, ordinal, str(change), str(mutations[ordinal]) if ordinal < len(mutations) else None),
            )
        summary = item.get("qor")
        if isinstance(summary, dict):
            self._insert_legacy_candidate_qor(conn, session_id, ckey, candidate_id, item, summary)
        mcmm = item.get("mcmm")
        if isinstance(mcmm, dict):
            self._insert_legacy_mcmm(conn, session_id, ckey, candidate_id, mcmm)

    def _insert_legacy_mcmm(
        self, conn: sqlite3.Connection, session_id: str, candidate_key: str, candidate_id: str,
        data: dict[str, Any],
    ) -> None:
        """Index the scenario-preserving MCMM portion of a legacy candidate JSON row."""
        fingerprint = stable_hash({"legacy_session": session_id, "candidate": candidate_id, "mcmm": data})
        mcmm_id = stable_hash({"legacy_mcmm": fingerprint})
        conn.execute(
            """INSERT INTO mcmm_aggregates(
                mcmm_id, candidate_key, session_id, candidate_id, feasible, infeasible, blocked, invalid,
                global_status, global_reason, limiting_scenarios_json, margin_headroom_ns,
                margin_utilization, margin_limiting_scenarios_json, cache_key, cache_status,
                run_ids_json, diagnostics_json, eda_runs, cache_hits, cache_misses, provenance_json,
                active_scenarios_json, source_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mcmm_id, candidate_key, session_id, candidate_id, _bool(data.get("feasible", False)),
                _bool(data.get("infeasible", False)), _bool(data.get("blocked", False)),
                _bool(data.get("invalid", False)), data.get("global_status"), data.get("global_reason"),
                _json(data.get("limiting_scenarios", [])), data.get("margin_headroom_ns"),
                data.get("margin_utilization"), _json(data.get("margin_limiting_scenarios", [])),
                data.get("cache_key"), data.get("cache_status"), _json(data.get("run_ids", [])),
                _json(data.get("diagnostics", [])), data.get("eda_runs"), data.get("cache_hits"),
                data.get("cache_misses"), _json(data.get("provenance", {})),
                _json(data.get("active_scenarios", [])), fingerprint,
            ),
        )
        for name, objective in sorted((data.get("objectives") or {}).items()):
            if not isinstance(objective, dict):
                continue
            conn.execute(
                """INSERT INTO mcmm_objectives(
                    mcmm_id, name, value, unknown_value, incomparable, limiting_scenarios_json,
                    direction, area_source, scenarios_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (mcmm_id, name, objective.get("value"), _bool(objective.get("unknown", False)),
                 _bool(objective.get("incomparable", False)), _json(objective.get("limiting", [])),
                 objective.get("direction"), objective.get("area_source"), _json(objective.get("scenarios", []))),
            )
        active = list(data.get("active_scenarios") or [])
        scenarios = data.get("scenario_results") or {}
        ids = active + sorted(set(scenarios) - set(active))
        for ordinal, sid in enumerate(ids):
            scenario = scenarios.get(sid) if isinstance(scenarios, dict) else None
            self._insert_legacy_mcmm_member(conn, mcmm_id, candidate_key, session_id, candidate_id,
                                            str(sid), ordinal, scenario if isinstance(scenario, dict) else None)

    def _insert_legacy_mcmm_member(
        self, conn: sqlite3.Connection, mcmm_id: str, candidate_key: str, session_id: str,
        candidate_id: str, scenario_id: str, ordinal: int, data: dict[str, Any] | None,
    ) -> None:
        data = data or {}
        summary = data.get("qor") if isinstance(data.get("qor"), dict) else None
        physical_id = str(data.get("run_id") or (summary or {}).get("run_id") or "")
        evaluation_id: str | None = None
        if summary is not None:
            if physical_id:
                existing = conn.execute("SELECT candidate_key FROM evaluations WHERE evaluation_id=?", (physical_id,)).fetchone()
                if existing:
                    if existing["candidate_key"] not in (None, candidate_key):
                        raise RecordConflictError(f"Legacy MCMM run '{physical_id}' is linked to another candidate.")
                    conn.execute("UPDATE evaluations SET candidate_key=? WHERE evaluation_id=?", (candidate_key, physical_id))
                    evaluation_id = physical_id
            evaluation_id = evaluation_id or physical_id or f"legacy:{session_id}:{candidate_id}:{scenario_id}"
            if not conn.execute("SELECT 1 FROM evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone():
                manifest = {
                    "candidate_id": candidate_id, "mode": data.get("mode"), "corner": data.get("corner"),
                    "tool": data.get("tool") or summary.get("tool"), "tool_version": data.get("tool_version") or summary.get("tool_version"),
                    "flow_stage": summary.get("flow_stage"), "extra": {"legacy_optimizer_snapshot": True},
                }
                artifact_fingerprint = stable_hash({"legacy_mcmm": mcmm_id, "scenario": scenario_id, "qor": summary})
                values = self._evaluation_values(
                    evaluation_id=evaluation_id, run_id=physical_id or None, candidate_id=candidate_id,
                    candidate_key=candidate_key, manifest=manifest, run_dir=None,
                    run_status=data.get("status") or "LEGACY_OPTIMIZER_IMPORTED", record_kind="legacy_import",
                    source_fingerprint=artifact_fingerprint, qor=None, artifact_fingerprint=artifact_fingerprint,
                )
                values.update({
                    "backend": str(summary.get("backend") or values["backend"]),
                    "backend_version": str(summary.get("backend_version") or values["backend_version"]),
                    "scenario": str(summary.get("scenario") or scenario_id),
                    "mode": str(summary.get("mode") or values["mode"]),
                    "corner": str(summary.get("corner") or values["corner"]),
                    "is_mock": _bool(summary.get("is_mock", False)),
                })
                self._insert_evaluation_graph(conn, values, _legacy_qor_values(summary), manifest, None)
                self._insert_legacy_power_evidence(conn, evaluation_id, summary)
        conn.execute(
            """INSERT INTO mcmm_members(
                mcmm_id, scenario_id, ordinal, evaluation_id, candidate_key, mode, corner, name,
                feasible, blocked, invalid, status, infeasible_reason, cache_key, cache_status,
                run_id, backend, tool, tool_version, diagnostics_json, margin_headroom_ns,
                margin_utilization, limiting, is_global_binding, provenance_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mcmm_id, scenario_id, ordinal, evaluation_id, candidate_key, data.get("mode"), data.get("corner"),
                data.get("name"), _bool(data.get("feasible", False)), _bool(data.get("blocked", False)),
                _bool(data.get("invalid", False)), data.get("status") or "blocked", data.get("infeasible_reason"),
                data.get("cache_key"), data.get("cache_status"), data.get("run_id"), data.get("backend"),
                data.get("tool"), data.get("tool_version"), _json(data.get("diagnostics", [])),
                data.get("margin_headroom_ns"), data.get("margin_utilization"), _bool(data.get("limiting", False)),
                _bool(data.get("is_global_binding", False)), _json(data.get("provenance", {})),
            ),
        )

    def _insert_legacy_candidate_qor(
        self, conn: sqlite3.Connection, session_id: str, candidate_key: str, candidate_id: str,
        item: dict[str, Any], summary: dict[str, Any],
    ) -> None:
        physical_id = str(summary.get("run_id") or item.get("run_id") or "")
        if physical_id:
            existing = conn.execute("SELECT candidate_key FROM evaluations WHERE evaluation_id=?", (physical_id,)).fetchone()
            if existing:
                if existing["candidate_key"] not in (None, candidate_key):
                    raise RecordConflictError(f"Legacy candidate run '{physical_id}' is linked to another candidate.")
                conn.execute("UPDATE evaluations SET candidate_key=? WHERE evaluation_id=?", (candidate_key, physical_id))
                return
        evaluation_id = physical_id or f"legacy:{session_id}:{candidate_id}"
        manifest = {
            "candidate_id": candidate_id, "mode": summary.get("mode") or item.get("mode"),
            "corner": summary.get("corner") or item.get("corner"), "tool": summary.get("tool"),
            "tool_version": summary.get("tool_version"), "flow_stage": summary.get("flow_stage"),
            "extra": {"legacy_optimizer_snapshot": True},
        }
        artifact_fingerprint = stable_hash({"session": session_id, "candidate": candidate_id, "qor": summary})
        values = self._evaluation_values(
            evaluation_id=evaluation_id, run_id=physical_id or None, candidate_id=candidate_id,
            candidate_key=candidate_key, manifest=manifest, run_dir=None,
            run_status="LEGACY_OPTIMIZER_IMPORTED", record_kind="legacy_import",
            source_fingerprint=artifact_fingerprint, qor=None, artifact_fingerprint=artifact_fingerprint,
        )
        values.update({
            "backend": str(summary.get("backend") or values["backend"]),
            "backend_version": str(summary.get("backend_version") or values["backend_version"]),
            "scenario": str(summary.get("scenario") or values["scenario"]),
            "mode": str(summary.get("mode") or values["mode"]),
            "corner": str(summary.get("corner") or values["corner"]),
            "is_mock": _bool(summary.get("is_mock", False)),
        })
        self._insert_evaluation_graph(conn, values, _legacy_qor_values(summary), manifest, None)
        self._insert_legacy_power_evidence(conn, evaluation_id, summary)

    def _import_legacy_run(
        self, run_id: str, run_dir: Path, manifest: dict[str, Any], summary: dict[str, Any] | None
    ) -> str:
        qvalues = _legacy_qor_values(summary) if summary is not None else {}
        artifact_fingerprint = _artifact_fingerprint(run_id, manifest, summary)
        # Legacy source contains exactly its retained artifact evidence.
        fingerprint = artifact_fingerprint
        evaluation = self._evaluation_values(
            evaluation_id=run_id, run_id=run_id,
            candidate_id=str((summary or {}).get("candidate_id") or manifest.get("candidate_id") or ""),
            candidate_key=None, manifest=manifest, run_dir=run_dir,
            run_status=((summary or {}).get("feasibility") or {}).get("status") or "LEGACY_IMPORTED",
            record_kind="legacy_import", source_fingerprint=fingerprint, qor=None,
            artifact_fingerprint=artifact_fingerprint,
        )
        # Legacy summary supplies run context directly without constructing a
        # QoRResult, because constructor defaults would falsify absent fields.
        if summary:
            evaluation.update({
                "backend": str(summary.get("backend") or evaluation["backend"]),
                "backend_version": str(summary.get("backend_version") or evaluation["backend_version"]),
                "tool": str(summary.get("tool") or evaluation["tool"]),
                "tool_version": str(summary.get("tool_version") or evaluation["tool_version"]),
                "flow_stage": str(summary.get("flow_stage") or evaluation["flow_stage"]),
                "scenario": str(summary.get("scenario") or evaluation["scenario"]),
                "mode": str(summary.get("mode") or evaluation["mode"]),
                "corner": str(summary.get("corner") or evaluation["corner"]),
                "is_mock": _bool(summary.get("is_mock", False)),
            })
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            before = conn.execute("SELECT 1 FROM evaluations WHERE evaluation_id=?", (run_id,)).fetchone()
            self._insert_evaluation_graph(conn, evaluation, qvalues, manifest, None)
            if summary is not None and not before:
                self._insert_legacy_power_evidence(conn, run_id, summary)
            conn.commit()
            return "skipped" if before else "imported"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _insert_legacy_power_evidence(
        self, conn: sqlite3.Connection, evaluation_id: str, summary: dict[str, Any]
    ) -> None:
        """Persist only legacy power evidence actually serialized in QoR JSON."""
        provenance = dict(summary.get("power_provenance") or {})
        status = summary.get("power_status")
        if status is None and not provenance:
            return
        conn.execute(
            """INSERT INTO power_evidence(
                evaluation_id, canonical_power_status, parser_status, report_format, parser_version,
                configured_producer, producer_version, tool_version, original_unit, normalized_unit,
                scenario_id, mode, corner, report_path, report_sha256, reported_internal_w,
                reported_switching_w, diagnostics_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                evaluation_id, status or PowerStatus.UNAVAILABLE.value,
                provenance.get("parsing_status"), provenance.get("format"),
                provenance.get("parser_format_version"),
                provenance.get("configured_producer") or provenance.get("producer"),
                provenance.get("producer_version"), provenance.get("tool_version") or summary.get("tool_version"),
                provenance.get("original_unit"), provenance.get("normalized_unit"),
                provenance.get("scenario_id") or summary.get("scenario"),
                provenance.get("mode") or summary.get("mode"), provenance.get("corner") or summary.get("corner"),
                provenance.get("report_path"), provenance.get("sha256"),
                provenance.get("reported_internal_w"), provenance.get("reported_switching_w"),
                _json(provenance.get("diagnostics") or []),
            ),
        )

    # ---- query API --------------------------------------------------------
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        self.initialize()
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM evaluations WHERE evaluation_id=? OR run_id=? "
                               "ORDER BY evaluation_id ASC LIMIT 1", (run_id, run_id)).fetchone()
            return self._hydrate_run(conn, row) if row else None
        finally:
            conn.close()

    def list_runs(
        self, *, candidate_id: str | None = None, session_id: str | None = None,
        scenario_id: str | None = None, constraint_set_hash: str | None = None,
        run_status: str | None = None, is_mock: bool | None = None, limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self.initialize()
        clauses: list[str] = []
        values: list[Any] = []
        joins = " LEFT JOIN candidates c ON c.candidate_key=e.candidate_key"
        for column, value in (
            ("e.candidate_id", candidate_id), ("c.session_id", session_id),
            ("e.scenario", scenario_id), ("e.constraint_set_hash", constraint_set_hash),
            ("e.run_status", run_status),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        if is_mock is not None:
            clauses.append("e.is_mock=?")
            values.append(_bool(is_mock))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = "SELECT e.* FROM evaluations e" + joins + where + \
              " ORDER BY COALESCE(e.manifest_timestamp, ''), e.evaluation_id"
        if limit is not None:
            sql += " LIMIT ?"
            values.append(max(0, int(limit)))
        conn = self._connect()
        try:
            rows = conn.execute(sql, values).fetchall()
            return [self._hydrate_run(conn, row) for row in rows]
        finally:
            conn.close()

    def find_by_constraint_set(self, constraint_set_hash: str) -> list[dict[str, Any]]:
        return self.list_runs(constraint_set_hash=constraint_set_hash)

    def get_candidate(self, session_id: str, candidate_id: str) -> dict[str, Any] | None:
        self.initialize()
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM candidates WHERE candidate_key=?",
                               (_candidate_key(session_id, candidate_id),)).fetchone()
            return self._hydrate_candidate(conn, row) if row else None
        finally:
            conn.close()

    def candidate_lineage(self, session_id: str, candidate_id: str) -> list[dict[str, Any]]:
        """Return root-to-selected candidate lineage within one explicit session."""
        self.initialize()
        conn = self._connect()
        try:
            key = _candidate_key(session_id, candidate_id)
            lineage: list[sqlite3.Row] = []
            seen: set[str] = set()
            while key:
                if key in seen:
                    raise QoRRepositoryError(f"Cycle detected in candidate lineage for '{key}'.")
                seen.add(key)
                row = conn.execute("SELECT * FROM candidates WHERE candidate_key=?", (key,)).fetchone()
                if row is None:
                    break
                lineage.append(row)
                key = row["parent_candidate_key"]
            return [self._hydrate_candidate(conn, row) for row in reversed(lineage)]
        finally:
            conn.close()

    def get_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        self.initialize()
        conn = self._connect()
        try:
            row = conn.execute("SELECT evaluation_id FROM evaluations WHERE evaluation_id=? OR run_id=? "
                               "ORDER BY evaluation_id LIMIT 1", (run_id, run_id)).fetchone()
            if row is None:
                return []
            rows = conn.execute(
                "SELECT artifact_name, relative_path, content_hash, hash_kind FROM evaluation_artifacts "
                "WHERE evaluation_id=? ORDER BY artifact_name", (row["evaluation_id"],)
            ).fetchall()
            return [dict(item) for item in rows]
        finally:
            conn.close()

    def get_provenance(self, run_id: str) -> dict[str, Any] | None:
        record = self.get_run(run_id)
        if record is None:
            return None
        evaluation = record["evaluation"]
        return {
            "evaluation_id": evaluation["evaluation_id"], "run_id": evaluation["run_id"],
            "candidate_id": evaluation["candidate_id"], "candidate_key": evaluation["candidate_key"],
            "constraint_set_hash": evaluation["constraint_set_hash"], "sdc_hash": evaluation["sdc_hash"],
            "config_hash": evaluation["config_hash"], "tool_identity": evaluation["tool_identity"],
            "input_hashes": evaluation["input_hashes"], "manifest_extra": evaluation["manifest_extra"],
            "artifacts": record["artifacts"], "power_evidence": record["power_evidence"],
        }

    def get_replay_identity(self, run_id: str) -> dict[str, Any] | None:
        """Return retained replay-investigation identity, never an execution claim."""
        record = self.get_run(run_id)
        if record is None:
            return None
        evaluation = record["evaluation"]
        root = Path(evaluation["run_dir"]) if evaluation["run_dir"] else None
        checked: list[dict[str, Any]] = []
        for artifact in record["artifacts"]:
            path = Path(artifact["relative_path"])
            resolved = path if path.is_absolute() else (root / path if root else path)
            exists = resolved.is_file()
            valid: bool | None = None
            reason = ""
            if not exists:
                valid = False
                reason = "missing"
            elif artifact["hash_kind"] == "sha256" and artifact["content_hash"]:
                try:
                    valid = hash_file(resolved) == artifact["content_hash"]
                    reason = "sha256_match" if valid else "sha256_mismatch"
                except OSError as exc:
                    valid = False
                    reason = f"unreadable: {exc}"
            elif artifact["hash_kind"] == "size" and artifact["content_hash"]:
                try:
                    valid = f"size={resolved.stat().st_size}" == artifact["content_hash"]
                    reason = "size_match" if valid else "size_mismatch"
                except OSError as exc:
                    valid = False
                    reason = f"unreadable: {exc}"
            else:
                reason = "no_integrity_hash"
            checked.append({**artifact, "resolved_path": str(resolved), "exists": exists,
                            "integrity_valid": valid, "integrity_reason": reason})
        missing: list[str] = []
        if not evaluation["sdc_hash"]:
            missing.append("SDC identity")
        if not evaluation["config_hash"]:
            missing.append("configuration identity")
        if not evaluation["input_hashes"].get("rtl"):
            missing.append("RTL input hashes")
        if not evaluation["tool"]:
            missing.append("tool identity")
        if not root:
            missing.append("run-directory locator")
        if any(not artifact["exists"] or artifact["integrity_valid"] is False for artifact in checked):
            missing.append("one or more retained artifacts")
        return {
            "meaning": "Retained identity for investigation/reproduction planning; this API does not execute or guarantee an EDA replay.",
            "evaluation": evaluation, "qor": record["qor"], "power_evidence": record["power_evidence"],
            "artifacts": checked, "commands": evaluation["manifest_extra"].get("commands"),
            "replay_information_missing": sorted(set(missing)),
            "automatic_replay_supported": False,
        }

    def get_mcmm(
        self, mcmm_id: str | None = None, *, session_id: str | None = None, candidate_id: str | None = None
    ) -> dict[str, Any] | None:
        self.initialize()
        if not mcmm_id and not (session_id and candidate_id):
            raise QoRRepositoryError("get_mcmm requires mcmm_id or session_id plus candidate_id.")
        conn = self._connect()
        try:
            if mcmm_id:
                row = conn.execute("SELECT * FROM mcmm_aggregates WHERE mcmm_id=?", (mcmm_id,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM mcmm_aggregates WHERE candidate_key=? ORDER BY mcmm_id LIMIT 1",
                    (_candidate_key(str(session_id), str(candidate_id)),),
                ).fetchone()
            return self._hydrate_mcmm(conn, row) if row else None
        finally:
            conn.close()

    def list_mcmm_scenarios(
        self, mcmm_id: str | None = None, *, session_id: str | None = None, candidate_id: str | None = None
    ) -> list[dict[str, Any]]:
        aggregate = self.get_mcmm(mcmm_id, session_id=session_id, candidate_id=candidate_id)
        return list(aggregate["scenarios"]) if aggregate else []

    def best_qor(
        self, objective: str, *, include_mock: bool = False, area_source: str | None = None,
        scenario_id: str | None = None, candidate_id: str | None = None,
        constraint_set_hash: str | None = None, limit: int = 1,
    ) -> list[dict[str, Any]]:
        """Return best comparable measurements under explicit availability rules.

        ``setup_wns`` maximizes non-null setup slack.  ``power`` minimizes only
        canonical AVAILABLE non-null report power and excludes mock by default.
        ``area`` requires either all-real (default) or explicit proxy selection;
        real and proxy rows are never mixed in one comparison.
        """
        self.initialize()
        normalized = {"setup_wns": "setup_wns", "setup_wns_s": "setup_wns", "power": "power", "area": "area"}.get(objective)
        if normalized is None:
            raise QoRRepositoryError("Unsupported best-QoR objective. Allowed: setup_wns, area, power.")
        clauses: list[str] = []
        args: list[Any] = []
        if not include_mock:
            clauses.append("e.is_mock=0")
        for column, value in (("e.scenario", scenario_id), ("e.candidate_id", candidate_id),
                              ("e.constraint_set_hash", constraint_set_hash)):
            if value is not None:
                clauses.append(f"{column}=?")
                args.append(value)
        if normalized == "setup_wns":
            clauses.append("q.setup_wns_s IS NOT NULL")
            order = "q.setup_wns_s DESC, e.evaluation_id ASC"
        elif normalized == "power":
            clauses.extend(["q.power_status=?", "q.power_total_w IS NOT NULL"])
            args.append(PowerStatus.AVAILABLE.value)
            order = "q.power_total_w ASC, e.evaluation_id ASC"
        else:
            source = area_source or "real"
            if source not in {"real", "proxy"}:
                raise QoRRepositoryError("area_source must be 'real' or 'proxy'; real/proxy cannot be mixed.")
            clauses.append("q.area_source=?")
            args.append(source)
            clauses.append("q.area IS NOT NULL" if source == "real" else "q.area_proxy IS NOT NULL")
            order = ("q.area ASC, e.evaluation_id ASC" if source == "real"
                     else "q.area_proxy ASC, e.evaluation_id ASC")
        sql = "SELECT e.* FROM evaluations e JOIN qor_measurements q ON q.evaluation_id=e.evaluation_id"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY " + order + " LIMIT ?"
        args.append(max(0, int(limit)))
        conn = self._connect()
        try:
            rows = conn.execute(sql, args).fetchall()
            return [self._hydrate_run(conn, row) for row in rows]
        finally:
            conn.close()

    # ---- hydrate helpers --------------------------------------------------
    def _hydrate_run(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        evaluation = dict(row)
        for key in ("tool_identity_json", "input_hashes_json", "manifest_extra_json"):
            evaluation[key.removesuffix("_json")] = _loads(evaluation.pop(key), {})
        measurement = conn.execute("SELECT * FROM qor_measurements WHERE evaluation_id=?", (row["evaluation_id"],)).fetchone()
        power = conn.execute("SELECT * FROM power_evidence WHERE evaluation_id=?", (row["evaluation_id"],)).fetchone()
        return {
            "evaluation": evaluation,
            "qor": self._hydrate_qor_row(measurement),
            "power_evidence": self._hydrate_json_row(power, ("diagnostics_json",)) if power else None,
            "artifacts": [dict(item) for item in conn.execute(
                "SELECT artifact_name, relative_path, content_hash, hash_kind FROM evaluation_artifacts "
                "WHERE evaluation_id=? ORDER BY artifact_name", (row["evaluation_id"],)
            ).fetchall()],
        }

    def _hydrate_qor_row(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        for key in ("critical_setup_json", "critical_hold_json", "wns_percentiles_json",
                    "timing_distribution_json", "feasibility_json", "notes_json", "diagnostics_json",
                    "raw_reports_json"):
            default = [] if key in {"notes_json", "diagnostics_json"} else {}
            value[key.removesuffix("_json")] = _loads(value.pop(key), default)
        return value

    def _hydrate_candidate(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("limiting_scenarios_json", "margin_limiting_scenarios_json", "warnings_json",
                    "diagnostics_json", "explanation_json", "coverage_json"):
            result[key.removesuffix("_json")] = _loads(result.pop(key), [] if key.endswith("scenarios_json") or key in {"warnings_json", "diagnostics_json"} else {})
        result["mutations"] = [dict(item) for item in conn.execute(
            "SELECT ordinal, change_label, mutated_constraint_id FROM candidate_mutations "
            "WHERE candidate_key=? ORDER BY ordinal", (row["candidate_key"],)
        ).fetchall()]
        return result

    def _hydrate_mcmm(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("limiting_scenarios_json", "margin_limiting_scenarios_json", "run_ids_json",
                    "diagnostics_json", "provenance_json", "active_scenarios_json"):
            default = [] if key != "provenance_json" else {}
            result[key.removesuffix("_json")] = _loads(result.pop(key), default)
        result["objectives"] = [self._hydrate_json_row(item, ("limiting_scenarios_json", "scenarios_json"))
                                for item in conn.execute(
                "SELECT * FROM mcmm_objectives WHERE mcmm_id=? ORDER BY name", (row["mcmm_id"],)
            ).fetchall()]
        result["scenarios"] = [self._hydrate_json_row(item, ("diagnostics_json", "provenance_json"))
                               for item in conn.execute(
                "SELECT * FROM mcmm_members WHERE mcmm_id=? ORDER BY ordinal, scenario_id", (row["mcmm_id"],)
            ).fetchall()]
        return result

    @staticmethod
    def _hydrate_json_row(row: sqlite3.Row, keys: Iterable[str]) -> dict[str, Any]:
        result = dict(row)
        for key in keys:
            result[key.removesuffix("_json")] = _loads(result.pop(key), [] if key != "provenance_json" else {})
        return result


# ---- migration SQL ---------------------------------------------------------
# Values intentionally have no DEFAULT 0.  SQLite NULL retains the existing
# model's unknown/not-recorded semantics and zero remains a real numeric value.
_MIGRATION_DESCRIPTIONS = {
    1: "Initial local historical QoR repository",
    2: "Add physical artifact fingerprint for legacy import reconciliation",
}
_MIGRATION_1 = (
    """CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL,
        description TEXT NOT NULL
    )""",
    """CREATE TABLE optimization_sessions (
        session_id TEXT PRIMARY KEY,
        started_at TEXT,
        completed_at TEXT,
        project_name TEXT,
        output_dir TEXT,
        baseline_candidate_key TEXT,
        final_candidate_key TEXT,
        stop_reason TEXT,
        iterations INTEGER,
        eda_runs INTEGER,
        cache_hits INTEGER,
        cache_misses INTEGER,
        optimizer_summary_json TEXT,
        source_fingerprint TEXT NOT NULL UNIQUE
    )""",
    """CREATE TABLE constraint_sets (
        constraint_set_hash TEXT PRIMARY KEY,
        name TEXT,
        snapshot_artifact_ref TEXT,
        snapshot_sha256 TEXT
    )""",
    """CREATE TABLE candidates (
        candidate_key TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES optimization_sessions(session_id) ON DELETE RESTRICT,
        candidate_id TEXT NOT NULL,
        parent_candidate_key TEXT REFERENCES candidates(candidate_key) DEFERRABLE INITIALLY DEFERRED,
        generation INTEGER,
        constraint_set_hash TEXT REFERENCES constraint_sets(constraint_set_hash) ON DELETE RESTRICT,
        sdc_hash TEXT,
        decision TEXT,
        validity_status TEXT,
        decision_reason TEXT,
        hard_feasible INTEGER NOT NULL CHECK(hard_feasible IN (0, 1)),
        blocked INTEGER NOT NULL CHECK(blocked IN (0, 1)),
        infeasible_reason TEXT,
        scenario TEXT,
        mode TEXT,
        corner TEXT,
        cache_key TEXT,
        cache_status TEXT,
        run_id TEXT,
        pareto_member INTEGER NOT NULL CHECK(pareto_member IN (0, 1)),
        rank INTEGER,
        priority_score REAL,
        margin_headroom_ns REAL,
        margin_utilization REAL,
        global_status TEXT,
        limiting_scenarios_json TEXT,
        margin_limiting_scenarios_json TEXT,
        warnings_json TEXT,
        diagnostics_json TEXT,
        explanation_json TEXT,
        coverage_json TEXT,
        source_fingerprint TEXT NOT NULL,
        UNIQUE(session_id, candidate_id)
    )""",
    """CREATE TABLE candidate_mutations (
        candidate_key TEXT NOT NULL REFERENCES candidates(candidate_key) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        change_label TEXT NOT NULL,
        mutated_constraint_id TEXT,
        PRIMARY KEY(candidate_key, ordinal)
    )""",
    """CREATE TABLE evaluations (
        evaluation_id TEXT PRIMARY KEY,
        run_id TEXT UNIQUE,
        record_kind TEXT NOT NULL CHECK(record_kind IN ('flow', 'optimizer_logical', 'mcmm_logical', 'legacy_import')),
        candidate_key TEXT REFERENCES candidates(candidate_key) DEFERRABLE INITIALLY DEFERRED,
        candidate_id TEXT,
        manifest_timestamp TEXT,
        run_status TEXT,
        backend TEXT,
        backend_version TEXT,
        tool TEXT,
        tool_version TEXT,
        flow_stage TEXT,
        scenario TEXT,
        mode TEXT,
        corner TEXT,
        is_mock INTEGER NOT NULL CHECK(is_mock IN (0, 1)),
        constraint_set_hash TEXT REFERENCES constraint_sets(constraint_set_hash) ON DELETE RESTRICT,
        sdc_hash TEXT,
        config_hash TEXT,
        cache_key TEXT,
        cache_status TEXT,
        library_locator TEXT,
        run_dir TEXT,
        tool_identity_json TEXT,
        input_hashes_json TEXT,
        manifest_extra_json TEXT,
        source_fingerprint TEXT NOT NULL
    )""",
    """CREATE TABLE qor_measurements (
        evaluation_id TEXT PRIMARY KEY REFERENCES evaluations(evaluation_id) ON DELETE CASCADE,
        setup_wns_s REAL, setup_tns_s REAL, setup_violations INTEGER,
        hold_wns_s REAL, hold_tns_s REAL, hold_violations INTEGER,
        whs_s REAL, ths_s REAL, near_critical_count INTEGER, path_count INTEGER,
        area REAL, area_total REAL, area_proxy REAL,
        area_source TEXT NOT NULL CHECK(area_source IN ('real', 'proxy', 'unknown')),
        area_comb REAL, area_seq REAL, area_buffer REAL,
        cell_count INTEGER, ff_count INTEGER, comb_cell_count INTEGER, buf_count INTEGER, buffer_count INTEGER,
        power_w REAL, power_total_w REAL, power_dynamic_w REAL, power_leakage_w REAL, power_status TEXT,
        constraint_quality REAL, validation_errors INTEGER, unsafe_exceptions INTEGER,
        margin_headroom_ns REAL, margin_utilization REAL, congestion REAL, runtime_seconds REAL,
        critical_setup_json TEXT, critical_hold_json TEXT, wns_percentiles_json TEXT,
        timing_distribution_json TEXT, feasibility_json TEXT, notes_json TEXT, diagnostics_json TEXT,
        raw_reports_json TEXT
    )""",
    """CREATE TABLE power_evidence (
        evaluation_id TEXT PRIMARY KEY REFERENCES qor_measurements(evaluation_id) ON DELETE CASCADE,
        canonical_power_status TEXT NOT NULL,
        parser_status TEXT,
        report_format TEXT,
        parser_version TEXT,
        configured_producer TEXT,
        producer_version TEXT,
        tool_version TEXT,
        original_unit TEXT,
        normalized_unit TEXT,
        scenario_id TEXT,
        mode TEXT,
        corner TEXT,
        report_path TEXT,
        report_sha256 TEXT,
        reported_internal_w REAL,
        reported_switching_w REAL,
        diagnostics_json TEXT
    )""",
    """CREATE TABLE evaluation_artifacts (
        evaluation_id TEXT NOT NULL REFERENCES evaluations(evaluation_id) ON DELETE CASCADE,
        artifact_name TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        content_hash TEXT,
        hash_kind TEXT NOT NULL CHECK(hash_kind IN ('sha256', 'size', 'unavailable')),
        PRIMARY KEY(evaluation_id, artifact_name)
    )""",
    """CREATE TABLE mcmm_aggregates (
        mcmm_id TEXT PRIMARY KEY,
        candidate_key TEXT NOT NULL REFERENCES candidates(candidate_key) ON DELETE RESTRICT,
        session_id TEXT NOT NULL REFERENCES optimization_sessions(session_id) ON DELETE RESTRICT,
        candidate_id TEXT,
        feasible INTEGER NOT NULL CHECK(feasible IN (0, 1)),
        infeasible INTEGER NOT NULL CHECK(infeasible IN (0, 1)),
        blocked INTEGER NOT NULL CHECK(blocked IN (0, 1)),
        invalid INTEGER NOT NULL CHECK(invalid IN (0, 1)),
        global_status TEXT,
        global_reason TEXT,
        limiting_scenarios_json TEXT,
        margin_headroom_ns REAL,
        margin_utilization REAL,
        margin_limiting_scenarios_json TEXT,
        cache_key TEXT,
        cache_status TEXT,
        run_ids_json TEXT,
        diagnostics_json TEXT,
        eda_runs INTEGER,
        cache_hits INTEGER,
        cache_misses INTEGER,
        provenance_json TEXT,
        active_scenarios_json TEXT,
        source_fingerprint TEXT NOT NULL UNIQUE
    )""",
    """CREATE TABLE mcmm_objectives (
        mcmm_id TEXT NOT NULL REFERENCES mcmm_aggregates(mcmm_id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        value REAL,
        unknown_value INTEGER NOT NULL CHECK(unknown_value IN (0, 1)),
        incomparable INTEGER NOT NULL CHECK(incomparable IN (0, 1)),
        limiting_scenarios_json TEXT,
        direction TEXT,
        area_source TEXT,
        scenarios_json TEXT,
        PRIMARY KEY(mcmm_id, name)
    )""",
    """CREATE TABLE mcmm_members (
        mcmm_id TEXT NOT NULL REFERENCES mcmm_aggregates(mcmm_id) ON DELETE CASCADE,
        scenario_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        evaluation_id TEXT REFERENCES evaluations(evaluation_id) ON DELETE RESTRICT,
        candidate_key TEXT NOT NULL REFERENCES candidates(candidate_key) ON DELETE RESTRICT,
        mode TEXT,
        corner TEXT,
        name TEXT,
        feasible INTEGER NOT NULL CHECK(feasible IN (0, 1)),
        blocked INTEGER NOT NULL CHECK(blocked IN (0, 1)),
        invalid INTEGER NOT NULL CHECK(invalid IN (0, 1)),
        status TEXT,
        infeasible_reason TEXT,
        cache_key TEXT,
        cache_status TEXT,
        run_id TEXT,
        backend TEXT,
        tool TEXT,
        tool_version TEXT,
        diagnostics_json TEXT,
        margin_headroom_ns REAL,
        margin_utilization REAL,
        limiting INTEGER NOT NULL CHECK(limiting IN (0, 1)),
        is_global_binding INTEGER NOT NULL CHECK(is_global_binding IN (0, 1)),
        provenance_json TEXT,
        PRIMARY KEY(mcmm_id, scenario_id)
    )""",
    "CREATE INDEX evaluations_candidate_idx ON evaluations(candidate_id, evaluation_id)",
    "CREATE INDEX evaluations_scenario_idx ON evaluations(scenario, evaluation_id)",
    "CREATE INDEX evaluations_constraint_idx ON evaluations(constraint_set_hash, evaluation_id)",
    "CREATE INDEX evaluations_cache_idx ON evaluations(cache_key, evaluation_id)",
    "CREATE INDEX candidates_session_idx ON candidates(session_id, candidate_id)",
    "CREATE INDEX mcmm_members_evaluation_idx ON mcmm_members(evaluation_id)",
)


# Version 2 is deliberately a small append-only evolution.  It allows modern
# full canonical source fingerprints while recognizing an old summary/manifest
# scan of the same physical artifact evidence as idempotent.
_MIGRATION_2 = (
    "ALTER TABLE evaluations ADD COLUMN artifact_fingerprint TEXT",
)


def _artifact_fingerprint(run_id: str, manifest: dict[str, Any], summary: dict[str, Any] | None) -> str:
    """Stable identity of already-written artifact evidence for one physical run."""
    return stable_hash({"run_id": run_id, "manifest": manifest, "qor_summary": summary})


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _tool_backend(manifest: dict[str, Any]) -> str:
    identity = manifest.get("tool_identity") or {}
    return str(identity.get("backend") or manifest.get("tool") or "")


def _constraint_snapshot(cset: Any | None) -> tuple[str | None, str | None]:
    """Return optional existing snapshot locator/hash without serializing UCM into SQLite."""
    if cset is None:
        return None, None
    metadata = getattr(cset, "metadata", {}) or {}
    locator = metadata.get("snapshot_artifact") or metadata.get("snapshot_path")
    if not locator:
        return None, None
    path = Path(str(locator))
    try:
        return str(path), hash_file(path) if path.is_file() else None
    except OSError:
        return str(path), None


def _legacy_qor_values(summary: dict[str, Any] | None) -> dict[str, Any]:
    """Map historical summary fields without applying QoRResult defaults."""
    if summary is None:
        return {}
    def seconds(key: str) -> float | None:
        value = summary.get(key)
        return float(value) * 1e-9 if value is not None else None
    power_provenance = summary.get("power_provenance")
    return {
        "setup_wns_s": seconds("setup_wns_ns"), "setup_tns_s": seconds("setup_tns_ns"),
        "setup_violations": summary.get("setup_violations"), "hold_wns_s": seconds("hold_wns_ns"),
        "hold_tns_s": seconds("hold_tns_ns"), "hold_violations": summary.get("hold_violations"),
        "whs_s": None, "ths_s": None, "near_critical_count": summary.get("near_critical_count"),
        "path_count": summary.get("path_count"), "area": summary.get("area"),
        "area_total": summary.get("area_total"), "area_proxy": summary.get("area_proxy"),
        "area_source": "real" if summary.get("area") is not None else (
            "proxy" if summary.get("area_proxy") is not None else "unknown"),
        "area_comb": None, "area_seq": None, "area_buffer": None,
        "cell_count": summary.get("cell_count"), "ff_count": summary.get("ff_count"),
        "comb_cell_count": None, "buf_count": None, "buffer_count": None,
        "power_w": summary.get("power"), "power_total_w": summary.get("power_total"),
        "power_dynamic_w": summary.get("power_dynamic"), "power_leakage_w": summary.get("power_leakage"),
        "power_status": summary.get("power_status"), "constraint_quality": None,
        "validation_errors": None, "unsafe_exceptions": None, "margin_headroom_ns": None,
        "margin_utilization": None, "congestion": None, "runtime_seconds": summary.get("runtime_s"),
        "critical_setup_json": _json(summary.get("critical_setup")) if summary.get("critical_setup") is not None else None,
        "critical_hold_json": _json(summary.get("critical_hold")) if summary.get("critical_hold") is not None else None,
        "wns_percentiles_json": None, "timing_distribution_json": None,
        "feasibility_json": _json(summary.get("feasibility")) if summary.get("feasibility") is not None else None,
        "notes_json": _json(summary.get("notes")) if summary.get("notes") is not None else None,
        "diagnostics_json": _json(summary.get("diagnostics")) if summary.get("diagnostics") is not None else None,
        "raw_reports_json": _json({"power": power_provenance}) if power_provenance is not None else None,
    }


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "SCHEMA_VERSION",
    "SQLITE_APPLICATION_ID",
    "QoRRepositoryError",
    "RecordConflictError",
    "SQLiteQoRRepository",
    "SchemaVersionError",
]
