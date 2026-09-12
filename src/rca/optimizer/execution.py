"""Deterministic optimizer execution-observability records.

The execution ledger is an audit projection owned by the existing optimizer;
it is not a second optimizer, cache, QoR model, artifact store, or SQLite
source of truth.  Coordinator code mutates it only at deterministic lifecycle
boundaries and serializes entries in task-ordinal order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExecutionStatus(str, Enum):
    """Lifecycle state of one planned optimizer task."""

    PLANNED = "planned"
    ADMITTED = "admitted"
    SUBMITTED = "submitted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class AdmissionStatus(str, Enum):
    """Whether the coordinator admitted a planned task for dispatch."""

    PENDING = "pending"
    ADMITTED = "admitted"
    NOT_ADMITTED = "not_admitted"


class SubmissionStatus(str, Enum):
    """Whether an admitted task reached the executor."""

    NOT_SUBMITTED = "not_submitted"
    SUBMITTED = "submitted"
    FAILED = "failed"


class TaskResultClassification(str, Enum):
    """Execution result distinct from the lifecycle state."""

    PENDING = "pending"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class FailureClassification(str, Enum):
    """Failure source, if any, without overloading ``ExecutionStatus``."""

    NONE = "none"
    WORKER_EVALUATION = "worker_evaluation"
    EDA_FAILURE = "eda_failure"
    EXECUTOR_CREATION = "executor_creation_failure"
    EXECUTOR_SUBMISSION = "executor_submission_failure"
    FUTURE_RETRIEVAL = "future_retrieval_failure"
    COORDINATOR = "coordinator_failure"
    BLOCKED_CONFIGURATION = "blocked_configuration"


class CacheObservation(str, Enum):
    """Normalized observation of the existing filesystem-cache decision."""

    UNKNOWN = "unknown"
    HIT = "hit"
    MISS = "miss"
    NOT_APPLICABLE = "not_applicable"


class OptimizationExecutionStopReason(str, Enum):
    """Explicit deterministic execution-level optimizer stop classification."""

    ITERATION_LIMIT = "iteration_limit"
    EDA_RUN_BUDGET = "eda_run_budget"
    ELAPSED_TIME_LIMIT = "elapsed_time_limit"
    NO_ADMISSIBLE_CANDIDATES = "no_admissible_candidates"
    EXECUTOR_CREATION_FAILURE = "executor_creation_failure"
    EXECUTOR_SUBMISSION_FAILURE = "executor_submission_failure"
    FATAL_COORDINATOR_ERROR = "fatal_coordinator_error"
    EXPLICIT_OPTIMIZER_STOP = "explicit_optimizer_stop"
    COMPLETED_SEARCH = "completed_search"
    BLOCKED_CONFIGURATION = "blocked_configuration"


@dataclass
class ExecutionLedgerEntry:
    """One logical candidate evaluation task in a deterministic invocation."""

    task_ordinal: int
    task_id: str
    candidate_id: str
    parent_candidate_id: str | None = None
    generation: int = 0
    constraint_set_hash: str = ""
    mutation_identity: str = ""
    mutation_ids: list[str] = field(default_factory=list)
    mutation_labels: list[str] = field(default_factory=list)
    task_work_dir: str = ""
    worker_identity: str = ""
    execution_status: ExecutionStatus = ExecutionStatus.PLANNED
    admission_status: AdmissionStatus = AdmissionStatus.PENDING
    submission_status: SubmissionStatus = SubmissionStatus.NOT_SUBMITTED
    status_history: list[ExecutionStatus] = field(
        default_factory=lambda: [ExecutionStatus.PLANNED]
    )
    result_classification: TaskResultClassification = TaskResultClassification.PENDING
    failure_classification: FailureClassification = FailureClassification.NONE
    failure_detail: str = ""
    skip_reason: str = ""
    cache_observation: CacheObservation = CacheObservation.UNKNOWN
    # Observed only: this is never an input to cache lookup or identity.
    cache_key: str = ""
    cache_status: str = ""
    eda_runs_consumed: int = 0
    mcmm_scenario_count: int = 0
    mcmm_scenario_ids: list[str] = field(default_factory=list)
    physical_run_ids: list[str] = field(default_factory=list)
    artifact_locators: list[str] = field(default_factory=list)
    application_order: int | None = None

    def _transition(self, status: ExecutionStatus) -> None:
        if self.execution_status != status:
            self.execution_status = status
            self.status_history.append(status)

    def mark_admitted(self) -> None:
        self.admission_status = AdmissionStatus.ADMITTED
        self._transition(ExecutionStatus.ADMITTED)

    def mark_submitted(self) -> None:
        self.submission_status = SubmissionStatus.SUBMITTED
        self._transition(ExecutionStatus.SUBMITTED)

    def mark_running(self) -> None:
        self._transition(ExecutionStatus.RUNNING)

    def mark_skipped(self, reason: str, *, admitted: bool = False) -> None:
        if not admitted:
            self.admission_status = AdmissionStatus.NOT_ADMITTED
        self.skip_reason = reason
        self.result_classification = TaskResultClassification.SKIPPED
        self._transition(ExecutionStatus.SKIPPED)

    def mark_cancelled(self, reason: str, failure: FailureClassification) -> None:
        self.skip_reason = reason
        self.failure_classification = failure
        self.result_classification = TaskResultClassification.CANCELLED
        self._transition(ExecutionStatus.CANCELLED)

    def mark_blocked(self, failure: FailureClassification, detail: str) -> None:
        self.failure_classification = failure
        self.failure_detail = detail
        self.result_classification = TaskResultClassification.BLOCKED
        self._transition(ExecutionStatus.BLOCKED)

    def mark_failed(self, failure: FailureClassification, detail: str) -> None:
        self.failure_classification = failure
        self.failure_detail = detail
        self.result_classification = TaskResultClassification.FAILED
        self._transition(ExecutionStatus.FAILED)

    def attach_application(self, *, cache_observation: CacheObservation,
                           cache_key: str, cache_status: str, eda_runs: int,
                           mcmm_scenario_ids: list[str], physical_run_ids: list[str],
                           artifact_locators: list[str], application_order: int) -> None:
        """Attach coordinator-applied result references without changing lifecycle."""
        self.cache_observation = cache_observation
        self.cache_key = cache_key
        self.cache_status = cache_status
        self.eda_runs_consumed = eda_runs
        self.mcmm_scenario_ids = list(mcmm_scenario_ids)
        self.mcmm_scenario_count = len(self.mcmm_scenario_ids)
        self.physical_run_ids = list(physical_run_ids)
        self.artifact_locators = list(artifact_locators)
        self.application_order = application_order

    def mark_completed(self, *, result: TaskResultClassification,
                       cache_observation: CacheObservation, cache_key: str,
                       cache_status: str, eda_runs: int, mcmm_scenario_ids: list[str],
                       physical_run_ids: list[str], artifact_locators: list[str],
                       application_order: int) -> None:
        self.result_classification = result
        self.attach_application(
            cache_observation=cache_observation, cache_key=cache_key,
            cache_status=cache_status, eda_runs=eda_runs,
            mcmm_scenario_ids=mcmm_scenario_ids,
            physical_run_ids=physical_run_ids, artifact_locators=artifact_locators,
            application_order=application_order,
        )
        self._transition(ExecutionStatus.COMPLETED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_ordinal": self.task_ordinal,
            "task_id": self.task_id,
            "candidate_id": self.candidate_id,
            "parent_candidate_id": self.parent_candidate_id,
            "generation": self.generation,
            "constraint_set_hash": self.constraint_set_hash,
            "mutation_identity": self.mutation_identity,
            "mutation_ids": list(self.mutation_ids),
            "mutation_labels": list(self.mutation_labels),
            "task_work_dir": self.task_work_dir,
            "worker_identity": self.worker_identity,
            "execution_status": self.execution_status.value,
            "admission_status": self.admission_status.value,
            "submission_status": self.submission_status.value,
            "status_history": [status.value for status in self.status_history],
            "result_classification": self.result_classification.value,
            "failure_classification": self.failure_classification.value,
            "failure_detail": self.failure_detail,
            "skip_reason": self.skip_reason,
            "cache_observation": self.cache_observation.value,
            "cache_key": self.cache_key,
            "cache_status": self.cache_status,
            "eda_runs_consumed": self.eda_runs_consumed,
            "mcmm_scenario_count": self.mcmm_scenario_count,
            "mcmm_scenario_ids": list(self.mcmm_scenario_ids),
            "physical_run_ids": list(self.physical_run_ids),
            "artifact_locators": list(self.artifact_locators),
            "application_order": self.application_order,
        }


@dataclass
class OptimizationExecutionLedger:
    """Coordinator-owned deterministic execution ledger for one invocation."""

    invocation_id: str
    workers: int
    baseline_constraint_set_hash: str
    schema_version: int = 1
    entries: list[ExecutionLedgerEntry] = field(default_factory=list)
    coordinator_application_order: list[str] = field(default_factory=list)
    stop_reason: OptimizationExecutionStopReason | None = None
    legacy_stop_reason: str = ""
    final_candidate_id: str | None = None
    pareto_candidate_ids: list[str] = field(default_factory=list)
    total_eda_runs: int = 0
    artifact_path: str = "optimizer_execution_ledger.json"
    diagnostics: list[str] = field(default_factory=list)

    def add_entry(self, entry: ExecutionLedgerEntry) -> ExecutionLedgerEntry:
        self.entries.append(entry)
        return entry

    def entry_for_ordinal(self, ordinal: int) -> ExecutionLedgerEntry:
        for entry in self.entries:
            if entry.task_ordinal == ordinal:
                return entry
        raise KeyError(f"No execution-ledger entry for task ordinal {ordinal}.")

    def final_counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in ExecutionStatus}
        for entry in self.entries:
            counts[entry.execution_status.value] += 1
        counts["planned_total"] = len(self.entries)
        counts["admitted_total"] = sum(
            entry.admission_status == AdmissionStatus.ADMITTED for entry in self.entries
        )
        counts["submitted_total"] = sum(
            entry.submission_status == SubmissionStatus.SUBMITTED for entry in self.entries
        )
        return counts

    def finalize(self, *, stop_reason: OptimizationExecutionStopReason,
                 legacy_stop_reason: str, final_candidate_id: str | None,
                 pareto_candidate_ids: list[str], total_eda_runs: int) -> None:
        self.stop_reason = stop_reason
        self.legacy_stop_reason = legacy_stop_reason
        self.final_candidate_id = final_candidate_id
        self.pareto_candidate_ids = list(pareto_candidate_ids)
        self.total_eda_runs = total_eda_runs

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "invocation_id": self.invocation_id,
            "workers": self.workers,
            "baseline_constraint_set_hash": self.baseline_constraint_set_hash,
            "artifact_path": self.artifact_path,
            "entries": [entry.to_dict() for entry in sorted(
                self.entries, key=lambda item: item.task_ordinal
            )],
            "coordinator_application_order": list(self.coordinator_application_order),
            "counts": self.final_counts(),
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "legacy_stop_reason": self.legacy_stop_reason,
            "final_candidate_id": self.final_candidate_id,
            "pareto_candidate_ids": list(self.pareto_candidate_ids),
            "total_eda_runs": self.total_eda_runs,
            "diagnostics": list(self.diagnostics),
        }
