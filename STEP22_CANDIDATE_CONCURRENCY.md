# Step 22 — Bounded Simultaneous Candidate Evaluation

**Status:** implemented

## Scope

This milestone adds bounded local concurrency to the existing optimizer. It
does not introduce a second optimizer, MCMM engine, flow, cache, QoR model,
artifact system, or history database. Candidate-level scheduling uses only
Python's standard-library `ThreadPoolExecutor`.

## Configuration

```yaml
optimization:
  workers: 1 # default; strict integer, allowed range 1..8
```

`workers` is the sole concurrency authority. There is no `rca optimize
--workers` option and no automatic worker count. `workers: 1` deliberately
keeps the direct serial evaluation code path and never creates an executor.

## Scheduler contract

For `workers > 1`, the coordinator deterministically generates and globally
deduplicates candidates, assigns IDs/lineage/mutation records, assigns a task
ordinal, and creates a task-local context before submitting anything. A worker
executes one complete candidate evaluation and returns a raw result or error.
The coordinator waits and applies results in task-list order—not future
completion order—and it alone updates budgets, candidates, feasibility,
Pareto/rank state, cache counters, and result collections.

An MCMM candidate remains exactly one task. Its scenarios run serially in the
existing active-scenario order, and normal complete MCMM aggregation occurs
before the task outcome becomes visible. No candidate-times-scenario fan-out
or nested executor is used.

A worker exception only invalidates that candidate. By contrast, executor
construction or task submission failure produces a deterministic
`OPTIMIZATION_CONCURRENCY_ERROR`/blocked result and stops the optimization;
there is no fallback to serial execution.

## Budgets, timeout, artifacts, cache, and history

Before dispatch, the scheduler reserves the known complete-candidate cost (one
per active MCMM scenario) and does not submit a task that would exceed
`max_eda_runs`. It launches at most `workers` tasks per wave. After the
deadline, it admits no further wave but safely collects already-started work;
therefore a tight elapsed-time limit can permit previously admitted in-flight
work to finish.

Task contexts have a fresh invocation token, deterministic ordinal, and
candidate ID. Parallel real-flow run IDs additionally carry a sanitized
scenario ID. These locator-only values keep concurrent physical output
locations distinct but are *not* cache inputs. Existing filesystem cache lookup
and manifest integrity validation remain the sole reuse authority; no in-memory
cache or stampede mechanism is added.

Workers finish the ordinary flow sequence—Yosys, OpenSTA, report-derived power
parsing, QoR, artifacts, manifest—then return transient existing
manifest/QoR/constraint evidence. They do not write normal optimizer-history
SQLite rows. The coordinator indexes that evidence in task order after existing
optimizer JSON/JSONL artifacts are present and before existing session
persistence. Index failures are advisory warnings; they leave EDA outcome,
QoR, artifacts, and cache state intact and can be reconciled via
`rca history --import-legacy`.

## Verification and benchmark harness

`tests/unit/test_optimizer_concurrency.py` uses controlled in-process fake
evaluators to cover:

- strict configuration bounds and default;
- direct no-executor serial behavior;
- bounded in-flight candidates, deterministic application despite deliberately
  reversed completion, preassigned task contexts, lineage, and EDA budget;
- serial/parallel candidate semantic equivalence;
- isolated task failure and deterministic executor-construction failure with no
  fallback;
- serial-within-candidate MCMM scenario evaluation and conservative MCMM budget
  admission;
- deferred worker history indexing followed by coordinator indexing; and
- task/scenario run-ID formation without cache-key participation.

Run the small observational fake-work benchmark with:

```bash
python scripts/benchmark_candidate_concurrency.py --workers 1,2,4 --delay-ms 80
```

It reports elapsed time and observed simultaneous complete candidates. It does
not invoke EDA and is not a claim of guaranteed EDA performance improvement.

See [ADR-005](docs/decisions/ADR-005-bounded-candidate-evaluation.md) for the
architecture decision and rejected alternatives.

## Recorded verification

On 2026-09-12, using Python 3.11.2 and controlled fake evaluators where
applicable:

- `python -m compileall -q src tests scripts` completed successfully.
- `pytest -q` completed with **902 passed** in **8.56s**.
- The focused test module completed with **11 passed**.
- `python scripts/benchmark_candidate_concurrency.py --workers 1,2,4
  --delay-ms 25 --repeats 1` observed maximum in-flight complete candidates of
  1, 2, and 4 respectively. Measured synthetic elapsed times were 0.129303 s,
  0.082129 s, and 0.057115 s. These are local fake-delay observations only,
  not an EDA speedup guarantee.
