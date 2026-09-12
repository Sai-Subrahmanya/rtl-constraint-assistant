# Step 21 — QoR Database Implementation Report

## Status

Implemented a local historical QoR repository using Python standard-library
SQLite. The repository is a query/index sidecar, not an EDA cache, optimizer,
provenance replacement, artifact store, or second QoR model.

**Database path:** `<flow.output_dir>/qor.sqlite3`.

## Authority boundaries

| Authority | Responsibility | Step-21 behavior |
|---|---|---|
| `QoRResult` | Canonical QoR semantics | `repository.py` reads/projections its fields only. |
| `RunManifest` / `ArtifactManager` | Artifact/provenance identity | SQLite indexes existing locators and hashes only. |
| Filesystem cache | Safe experiment reuse | SQLite stores observed cache key/status but is never consulted for cache hits. |
| Existing optimizer/MCMM models | Candidate lineage and scenario aggregation | Repository stores session-scoped relations and all per-scenario evidence. |

## Storage and version contract

`SQLiteQoRRepository` in `src/rca/qor/repository.py` uses `sqlite3` only.
It sets a dedicated `application_id` (`RCAQ`), verifies it on every connection,
and configures:

- `PRAGMA foreign_keys=ON`;
- WAL journal mode;
- `synchronous=FULL`;
- bounded 5-second busy timeout;
- `PRAGMA user_version` plus an append-only `schema_migrations` ledger.

Current schema version is **2**. New sidecars initialize transactionally at that
version. Version 1 creates the core repository; version 2 appends the physical
artifact fingerprint used to reconcile an explicit legacy scan of an already
indexed run. Older supported versions migrate forward in numbered transactions.
A database newer than RCA supports, an unknown application ID, or disagreement
between the pragma and ledger fails clearly; no silent downgrade occurs.

## Schema

The version-1 schema contains these normalized entities:

1. `schema_migrations`
2. `optimization_sessions`
3. `constraint_sets`
4. `candidates`
5. `candidate_mutations`
6. `evaluations`
7. `qor_measurements`
8. `power_evidence`
9. `evaluation_artifacts`
10. `mcmm_aggregates`
11. `mcmm_objectives`
12. `mcmm_members`

Important query dimensions are columns: physical run/evaluation IDs,
record kind, candidate/session keys, status, scenario/mode/corner, mock flag,
constraint/SDC/config/cache identity, tool/backend identity, timing, real and
proxy area, power status, quality, validation/unsafe counts, and margins.
JSON is used only for structured sparse evidence such as diagnostics, notes,
critical paths, timing distributions, tool/input identity, commands, and
manifest-extra provenance.

Timing is retained in canonical seconds (`*_s` fields); power is watts
(`*_w`). No metric has a `DEFAULT 0`; SQL `NULL` remains unknown/unrecorded and
numeric zero remains a valid value. Real and proxy area use an explicit
`area_source` and are never mixed in an area best-query comparison.

## Write and conflict semantics

`record_flow_evaluation`, `record_optimizer_session`, and
`record_mcmm_aggregate` write an entire related graph under `BEGIN IMMEDIATE`.
Failure rolls back all rows in that graph.

Physical flow records have both:

- a canonical source fingerprint for complete in-memory evidence; and
- an artifact fingerprint for the already-written manifest/`qor.json` evidence.

Exact same source is idempotent. A changed source for an existing stable run ID
is a `RecordConflictError`; it never overwrites a historical record. An explicit
legacy scan of the exact same physical artifacts recognizes the artifact
fingerprint and is a no-op even when old summaries lack newer in-memory fields.

A database failure is advisory: flow integration returns a deterministic
`QOR_DATABASE_PERSISTENCE_WARNING`; it does not change QoR, run status, cache
identity, or existing files. Later `rca history --import-legacy` can index the
same intact evidence.

## Repository API

Core lifecycle and writes:

```text
initialize()
migrate()
record_flow_evaluation(...)
record_optimizer_session(...)
record_mcmm_aggregate(...)
import_legacy_artifacts(...)
```

Deterministic parameterized reads:

```text
get_run(run_id)
list_runs(...)
find_by_constraint_set(hash)
get_candidate(session_id, candidate_id)
candidate_lineage(session_id, candidate_id)
get_artifacts(run_id)
get_provenance(run_id)
get_replay_identity(run_id)
get_mcmm(...)
list_mcmm_scenarios(...)
best_qor(...)
```

Filter and objective choices are whitelisted in Python; values are SQL
parameters. Lists have explicit ordering. Best setup slack maximizes non-null
setup WNS. Best power minimizes only non-null canonical `AVAILABLE` power and
excludes mocks unless requested. Best area defaults to real mapped area; proxy
requires explicit selection and is incomparable to real area.

## MCMM

`mcmm_aggregates` holds global feasibility/cache/margin/provenance;
`mcmm_objectives` retains each objective's nullable value, unknown,
incomparable, direction, source, and limiting scenarios; and `mcmm_members`
holds one record per scenario. Each member links an individual physical or
explicitly labelled logical evaluation and retains mode/corner, cache identity,
per-scenario status, QoR/power evidence, diagnostics, margins, and provenance.
When a cached physical evaluation is already associated with a candidate in a
prior session, the later session receives a labelled logical projection with a
`physical_evaluation_id` locator; candidates are never conflated across
sessions. Global rows are derived evidence and never replace scenario records.

## Power and artifacts

`qor_measurements` stores canonical `PowerStatus`; `power_evidence` separately
stores `PowerParseStatus`, report format/parser version, configured producer,
producer/tool version, original/normalized unit, scenario/mode/corner, source
path/SHA-256, raw Internal/Switching fields, and diagnostics. Unusable power is
nullable, never zeroed.

`evaluation_artifacts` indexes existing manifest artifact name, run-relative
path, content hash, and hash kind (`sha256`, `size`, or unavailable). Artifact
content is not copied to SQLite. `get_replay_identity` rechecks artifact
existence and recorded hash/size where available.

## Cache separation and replay meaning

SQLite neither calls nor changes `_find_cached_run`; it has no authority over
reuse. The cache remains filesystem-manifest/hash based.

Replay identity returns run/candidate/session/constraint-set identity,
input/include/Liberty/SDC/config hashes available in the manifest, tool/backend
identity, scenario, cache metadata, commands, artifact verification, QoR, and
power evidence. Its response explicitly says automatic replay is unsupported
and reports missing retained evidence. It does not run an EDA command or claim
that a tool/input/library can be reconstructed.

## Legacy import and compatibility

`rca history --import-legacy` is explicit. It deterministically scans
`<output>/runs/*`, recognizes `run_manifest.json` then legacy `manifest.json`,
and imports `qor.json` only when it is a valid object. It separately indexes the retained
root `candidates.jsonl` snapshot as a legacy session (including serialized
candidate QoR and MCMM scenario evidence); when JSONL is absent it falls back
to `optimizer_state.json`. JSONL is preferred so a mutable state snapshot
cannot change an otherwise identical import. Existing JSON/JSONL, manifests,
root optimizer state, Pareto files, cache layout, and artifacts are never
rewritten or deleted.

Importer limitations are intentional:

- old QoR summaries do not recreate fields that `QoRResult.summary()` omitted;
  those remain `NULL`;
- root optimizer snapshots represent only the currently retained invocation;
  overwritten historical invocations cannot be reconstructed;
- missing/invalid manifests or QoR files are reported and skipped;
- changed artifact evidence with an already-imported run ID is reported as a
  conflict rather than overwritten.

## CLI and flow integration

`rca history` supports:

```text
--run-id ID
--candidate ID [--session ID]
--scenario ID
--constraint-set HASH
--best setup_wns|area|power
--area-source real|proxy
--include-mock
--import-legacy
--json
--output-dir DIR / --config PROJECT.yaml
```

It never invokes EDA, optimizer search, or cache reuse. `run-sta` real flow
indexes only after normal artifacts/manifests are complete. The ordinary mock
`run_flow` path remains filesystem-only unless a repository is supplied
explicitly, preserving lightweight mock unit behavior. `optimize` records the
established optimizer artifact result after JSON/JSONL/final-SDC output.
MCMM `run-sta` retains its existing report layout and separately creates a
one-candidate historical grouping for relational MCMM evidence.

## Tests

`tests/unit/test_qor_repository.py` contains **37** named temporary-database
cases covering schema/version/migration, all core graph types, idempotency and
conflicts, QoR nullable semantics, power/provenance/artifacts, MCMM, session
identity/lineage, queries/order, replay, explicit legacy import, transactional
rollback, WAL reads, cache separation, mock/real distinction, flow failure
safety, and CLI output.

## Known limitations

- SQLite concurrency is local-process/file-system behavior, not distributed
  database coordination.
- The database indexes evidence and cannot supply input/tool artifacts not
  retained by manifests.
- First release does not replace `rca report`'s existing latest-artifact view
  or alter dashboard data sources.
- Existing root-level optimizer JSON/JSONL snapshot semantics remain unchanged.
