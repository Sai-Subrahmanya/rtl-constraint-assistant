# ADR-005: Bounded Concurrent Complete-Candidate Evaluation

- **Status:** Accepted
- **Date:** 2026-09-12
- **Decision scope:** Simultaneous candidate-evaluation milestone

## Context

The optimizer had a deterministic serial candidate loop. Independent candidate
EDA evaluations can spend most of their time waiting for external tools, but
parallelizing individual flow stages or MCMM scenarios would change ordering,
budgets, artifacts, failure visibility, and safety boundaries. The existing
filesystem manifest/hash cache is authoritative for safe reuse, while the
SQLite sidecar is advisory historical indexing only.

## Decision

Add one strict project configuration setting:

```yaml
optimization:
  workers: 1 # integer from 1 through 8; default is 1
```

There is intentionally no CLI override, automatic sizing, process pool, nested
executor, or distributed scheduler.

- With `workers: 1`, the direct pre-existing serial candidate path is used and
  no executor is constructed.
- With `workers > 1`, only complete generated candidates are executor tasks,
  using `concurrent.futures.ThreadPoolExecutor`. An MCMM candidate remains one
  task and evaluates its active scenarios serially in existing scenario order.
- The coordinator generates/deduplicates candidates and allocates candidate
  IDs, parent/mutation lineage, and task ordinals before submitting work.
  It collects and applies task outcomes in that task-list order, never
  completion order. Workers return a value or an isolated candidate error and
  do not mutate optimizer collections, budgets, ranks, Pareto flags, or
  history state.
- Task work contexts include an invocation token, ordinal, and candidate ID.
  Parallel real-flow callbacks derive physical run IDs from that context plus a
  sanitized scenario component. Those values prevent concurrent output
  collisions but never enter a cache key; the established filesystem cache is
  not supplemented by an in-memory cache or stampede protocol.
- Admission accounts for the complete known MCMM scenario cost before
  submission and does not dispatch work that would exceed `max_eda_runs`.
  An elapsed deadline prevents later waves from being admitted. Work already
  started is collected safely, so a binding wall-clock deadline can still
  leave safely completed in-flight work to be recorded.
- Executor creation or submission failure is a deterministic blocked
  optimization/concurrency error, never a serial fallback. A worker's own
  exception remains an isolated failed candidate.
- Workers complete normal artifacts and manifests but defer normal flow history
  indexing. The coordinator consumes that immutable evidence in task order,
  then performs the existing advisory SQLite indexing before its established
  optimizer-session persistence. SQLite failures remain warnings and do not
  alter QoR, artifact, EDA, or cache results; explicit legacy import can later
  reconcile manifests.

## Consequences

### Positive

- Candidate-level concurrency is bounded, deterministic in its coordinator
  semantics, and preserves the existing flow order inside a task: synthesis,
  STA, report-derived power parsing, QoR, artifacts, then manifest.
- MCMM keeps complete aggregation, conservative missing/failed behavior,
  limiting-scenario semantics, and no partial candidate result exposure.
- Parallel physical outputs cannot collide merely because candidate callbacks
  start in the same second.

### Limits

- This does not guarantee a speedup. Tool licenses, host resources, cache hit
  rate, I/O, and the configured EDA backend determine observed performance.
- Thread execution is not a process-isolation boundary; callbacks must continue
  to treat configuration and candidate input as read-only.
- The strict serial path retains its historical admission behavior. Parallel
  admission is additionally conservative where complete MCMM task cost would
  exceed the remaining EDA-run budget.

## Alternatives rejected

1. **Process pools or external schedulers.** They add serialization,
   heavyweight deployment, and separate failure semantics without a need for
   this local bounded milestone.
2. **Candidate-times-scenario fan-out.** It would create nested concurrency,
   expose partial MCMM state, and complicate aggregation/budget semantics.
3. **Completion-order application.** It makes candidate ranks, Pareto state,
   history ordering, and potentially future mutation choices timing-dependent.
4. **Worker SQLite writes.** Multiple worker writers would blur the existing
   advisory indexing boundary and make history ordering less auditable.
5. **Task identity in the cache key.** Invocation tokens and ordinals describe
   output allocation, not experiment identity; putting them in cache identity
   would defeat safe existing cache reuse.
