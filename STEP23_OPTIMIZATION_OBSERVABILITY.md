# Step 23 — Optimization Observability and Execution-State Hardening

**Status:** implemented

## Scope and authority

This milestone adds a typed, per-invocation execution ledger to the existing
optimizer result. It is an **execution projection**, not a second optimizer,
cache, QoR, MCMM, artifact authority, or history database. Candidate generation,
feasibility, Pareto selection, cache keys, QoR semantics, and the Step 22
scheduler are unchanged.

`OptimizationResult` now carries:

- `execution_ledger`: `OptimizationExecutionLedger`; and
- `execution_stop_reason`: an operational stop classification in addition to
  the existing backward-compatible `StopReason`.

The ledger is coordinator-owned. Workers return raw outcomes only; they do not
write the ledger, SQLite, candidate collections, budgets, QoR, cache state, or
Pareto state. Completion timing consequently cannot change ledger application
order or optimization semantics.

## Ledger schema and lifecycle

`src/rca/optimizer/execution.py` defines explicit enums for task execution,
admission, submission, result, failure, cache observation, and optimization stop
classification. Each task record includes a deterministic ordinal, candidate and
parent ID, generation, constraint/mutation lineage, task context, logical worker
identity, status history, observed cache key/status/accounting, MCMM scenario
evidence, physical run IDs, artifact locators, and coordinator application order.
The observed cache key is evidence only; it is never a cache-key input.

Task execution states are:

```text
PLANNED → ADMITTED → SUBMITTED → RUNNING → COMPLETED
                                      └→ FAILED
                       └→ CANCELLED
PLANNED/ADMITTED → SKIPPED
PLANNED/ADMITTED/SUBMITTED/RUNNING → BLOCKED
```

The baseline has ordinal zero and is recorded too. Serial operation (`workers:
1`) stays direct and uses `coordinator-direct`; no executor is created. Parallel
entries get a fresh invocation-scoped task context. The identity is an output
allocation/observability locator only: it is never placed in a filesystem cache
key.

A ledger finalization records ordered application, status/admission/submission
counts, EDA runs, final and Pareto candidate IDs, the existing legacy stop value,
and one typed operational stop reason. The classifications include iteration and
EDA limits, elapsed time, no admissible work, executor creation/submission
failure, fatal coordinator error, explicit stop, completed search, and blocked
configuration. Programmatic callers may request a cooperative explicit stop with
`Optimizer.request_stop()`; the established legacy result remains `USER_STOP`.

Failures remain honest: executor construction/submission failures are blocked or
cancelled without a serial fallback; an evaluator failure is failed; elapsed or
budget work not dispatched is skipped; and coordinator failure blocks unfinished
entries rather than labelling them complete. A completed callback which reports
an unusable EDA outcome remains `COMPLETED` with a `BLOCKED` result
classification and EDA failure evidence—the callback finished, but it did not
provide a usable candidate.

## Persistence and reporting

At the normal optimization output root, `rca optimize` now writes, before any
advisory SQLite indexing:

```text
optimizer_execution_ledger.json
optimizer_state.json
optimizer_execution_manifest.json
candidates.jsonl
pareto_frontier.json
[design.final.sdc]
```

`ArtifactManager.write_json_atomic()` serializes into a same-directory temporary
file, flushes/syncs it, then atomically replaces the target. The execution ledger
therefore cannot be mistaken for a complete JSON document when a write is
interrupted. `optimizer_execution_manifest.json` is a normal `RunManifest` that
records artifact names/hashes, the invocation ID, worker count, ledger schema,
and stop facts. It does not use the physical flow cache namespace.

Use the established reporting command to inspect the authoritative artifact
without touching SQLite:

```bash
rca history --optimization-ledger --output-dir results
rca history --optimization-ledger --output-dir results --json
```

The human form reports the invocation, ledger location, typed stop, planned /
admitted / submitted / completed / failed / skipped / cancelled / blocked totals,
EDA accounting, final candidate, and Pareto IDs. JSON returns the complete
ordered ledger. This path creates no SQLite database and performs no EDA,
optimization, cache lookup, or cache reuse.

SQLite remains an advisory post-artifact history index. Deferred worker flow
evidence is consumed by the coordinator in task order after the authoritative
files exist. A SQLite error does not rewrite or invalidate the ledger, artifacts,
QoR, candidate decision, EDA accounting, or cache state; `rca history
--import-legacy` remains the explicit reconciliation route.

## MCMM and reproducibility limits

One complete MCMM candidate remains one Step 22 task. Its active scenarios still
run serially in the existing matrix order. The ledger records the aggregated
candidate's scenario IDs/count and physical run references after normal MCMM
aggregation; it does not expose partial scenario work as a completed candidate.
Admission continues to reserve complete known scenario cost conservatively.

The ledger records evidence and locators, not a replay or signoff promise.
Real-EDA reproducibility still depends on retained inputs, libraries, tools,
licenses, host conditions, tool versions, configuration, and existing manifest
verification. Capturing execution evidence does not manufacture power, timing,
or deterministic real-tool reruns.

## Focused verification

`tests/unit/test_optimizer_execution_ledger.py` covers deterministic task and
coordinator order despite reversed completions; direct serial behavior; worker,
executor-construction, and executor-submission failures; EDA budget and elapsed
deadline skips; MCMM accounting and cache evidence; normalized repeatability;
atomic artifact/manifest persistence; artifact-only human/JSON history; a
persistable coordinator-failure ledger; and SQLite advisory isolation.

See [ADR-006](docs/decisions/ADR-006-optimization-execution-ledger.md) for the
persisted-authority decision.
