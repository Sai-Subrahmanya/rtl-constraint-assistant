# ADR-006: Coordinator-Owned Optimizer Execution Ledger

- **Status:** Accepted
- **Date:** 2026-09-12
- **Decision scope:** Optimization observability and execution-state hardening

## Context

The optimizer already has canonical candidate, QoR, MCMM, filesystem-cache,
artifact, and advisory SQLite-history structures. Step 22 added bounded complete
candidate evaluation but a concise durable account of planning, admission,
submission, worker outcome, skipped work, and coordinator application was not
available. An operational failure or deadline must remain auditable without
making task completion timing semantic.

## Decision

Extend `OptimizationResult` with a typed `OptimizationExecutionLedger` and
`OptimizationExecutionStopReason` from `rca.optimizer.execution`.

- The ledger has one fresh invocation ID and deterministic task ordinal order.
  It captures baseline and generated candidate lineage, task/worker context,
  lifecycle status history, admissions/submissions, result/failure/cache/EDA/
  MCMM/run/artifact evidence, skips/cancellations, coordinator application order,
  totals, and final/Pareto/stop facts.
- It is a coordinator-owned projection of the existing result. It neither
  replaces nor contributes to candidate identity, QoR, cache identity, MCMM
  aggregation, scheduler policy, or selection. Task IDs and invocation tokens
  are explicitly excluded from cache keys.
- `workers: 1` preserves the pre-existing direct serial loop; `workers > 1`
  preserves the standard-library bounded `ThreadPoolExecutor` candidate tasks,
  serial-within-candidate MCMM behavior, and ordered coordinator application.
  Worker threads do not mutate the ledger or write normal SQLite rows.
- A normal optimize command writes `optimizer_execution_ledger.json` with a
  same-directory atomic replacement, alongside existing root optimizer
  artifacts. `optimizer_execution_manifest.json` is a normal manifest for the
  new execution artifact and its companion state/candidate/Pareto/final-SDC
  records. These files are authoritative before advisory SQLite indexing starts.
- `rca history --optimization-ledger [--json]` reads that artifact directly and
  deliberately bypasses SQLite. SQLite remains query/index advice; index
  failure is nonfatal and does not mutate the authoritative ledger.

## Consequences

### Positive

- A partial execution is inspectable with typed facts rather than inferred from
  mutable logs or database rows.
- Ordered application is explicit, so concurrent completion cannot be confused
  with optimizer semantic order.
- Budget/deadline decisions and executor/coordinator failures distinguish work
  never dispatched from an evaluator that genuinely ran and failed.
- Operators can inspect a ledger even if no SQLite sidecar exists or it is
  unavailable.

### Limits

- The ledger is not a distributed workflow engine, retry queue, dashboard,
  cache, replay engine, or signoff record.
- A task context is a logical identity/locator, not a process-isolation claim.
  Thread callbacks remain responsible for treating their inputs/configuration as
  read-only.
- Recorded EDA evidence cannot guarantee real-tool reproducibility. Existing
  manifests and retained environments remain the relevant conservative evidence.

## Alternatives rejected

1. **A separate optimizer state model or SQLite-led ledger.** This would create
   a second authority and make a database availability problem obscure existing
   artifacts.
2. **Worker-written rows or ledgers.** This would compromise the coordinator
   ordering and the existing SQLite boundary.
3. **Task IDs in cache keys.** Task allocation is not experiment identity and
   would reduce valid filesystem-cache reuse.
4. **Completion-order timeline as semantic order.** It would make timing appear
   to govern candidate application and undermine deterministic interpretation.
5. **Process/distributed/async orchestration.** The bounded local executor is
   sufficient for this milestone and preserves Step 22's scope.
