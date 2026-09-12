# ADR-004: Local SQLite QoR History Sidecar

- **Status:** Accepted
- **Date:** 2026-09-02
- **Decision scope:** Step 21 QoR database implementation

## Context

RCA already has authoritative per-run artifacts and manifests, a canonical
`QoRResult`, optimizer candidate/MCMM models, and a filesystem manifest/hash
cache. These are serializable but do not support small, indexed, deterministic
historical queries across run, candidate, session, scenario, artifact, tool,
cache, and constraint-set identity. Root optimizer JSON/JSONL files are current
snapshots and may be overwritten by later optimizer invocations.

The missing capability is a local historical query repository. It must not turn
storage into a second QoR model, provenance system, artifact store, or cache.

## Decision

Use Python standard-library `sqlite3` to create a per-output-directory sidecar:

```text
<flow.output_dir>/qor.sqlite3
```

SQLite stores normalized historical references and metric projections for:

- schema migrations;
- optimization sessions;
- session-scoped candidates and ordered mutations;
- existing stable constraint-set identities;
- physical and explicitly labelled logical evaluations;
- canonical QoR metric columns with SQL `NULL` for unknown/unrecorded values;
- report-derived power evidence and parser provenance;
- manifest artifact locators and integrity hashes;
- MCMM aggregate objectives and separate per-scenario members.

The sidecar uses an RCA application identifier, `PRAGMA user_version`, an
append-only `schema_migrations` ledger, transactional numbered migrations,
foreign keys, WAL, bounded busy timeout, and `synchronous=FULL`. A newer or
inconsistent database fails clearly; no downgrade is attempted.

Flow indexing occurs only after normal artifacts and manifests have been
written. Evaluation/session/MCMM graphs use `BEGIN IMMEDIATE` and commit all
related rows or none. Matching evidence fingerprints are idempotent; conflicting
stable IDs are rejected rather than overwritten.

## Authority boundaries

| Concern | Authority | SQLite role |
|---|---|---|
| Canonical QoR semantics | `QoRResult` | Read/project existing fields only. |
| Artifacts and provenance | `RunManifest` / `ArtifactManager` | Index paths/hashes; never copy artifact content. |
| Reuse safety | Existing filesystem cache/integrity scan | Store observed cache key/status only; never choose a hit. |
| MCMM semantics | Existing `ScenarioQoR` / `MCMMResult` | Preserve every scenario row plus derived aggregate rows. |
| Candidate semantics | Existing `Candidate` / optimizer | Store session-scoped identity/lineage, never infer across sessions. |

A database write failure must not alter `QoRResult`, `RunStatus`, cache identity,
or artifacts. It is emitted as a deterministic persistence warning and can be
reconciled later with explicit legacy import.

## Consequences

### Positive

- No service, credentials, network port, ORM, or additional package is needed.
- Local SQL supports deterministic indexed history and joins without replacing
  current interoperable artifact files.
- WAL permits a reader during a short writer transaction on one machine.
- Explicit import retains older artifacts without silent rewrites.

### Limits

- This is not a distributed, networked, or cross-machine database.
- SQLite does not make an EDA experiment replayable. `get_replay_identity`
  returns retained identities/locators and current hash checks; missing tools,
  inputs, libraries, or files remain explicit.
- Legacy `qor.json` summaries cannot recreate fields they never serialized;
  those database columns remain `NULL` with import evidence/diagnostics.
- Root `optimizer_state.json` and `candidates.jsonl` snapshots overwritten
  before import cannot be recovered as historical sessions.

## Alternatives rejected

1. **Use the filesystem cache as history.** It answers safe reuse by scanning
   matching manifests, not arbitrary history queries, and must remain isolated.
2. **Append more JSON/JSONL indexes.** This would recreate query, migration,
   transaction, conflict, ordering, and reader/writer semantics poorly.
3. **Remote SQL service.** It adds operational, credential, and portability
   costs unjustified for RCA's local CLI workflow.
4. **ORM or a parallel QoR data model.** Direct parameterized SQL and
   projections from existing models keep authority clear and scope small.
