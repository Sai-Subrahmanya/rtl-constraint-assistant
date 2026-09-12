# Step 29 — Constraint Readiness & Closure Engine

## Purpose and boundary

`rca.readiness` is a deterministic **report-only orchestration layer**. It answers
whether the supplied canonical Universal Constraint Model (UCM) has the existing
evidence required by the selected configuration before a downstream timing-flow
request. It does not claim signoff, timing closure, QoR, formal proof, or a real
EDA success.

It consumes existing authorities rather than replacing them:

| Evidence | Existing authority consumed by readiness |
|---|---|
| Canonical constraints, provenance, assumptions, application metadata | `ConstraintSet` (UCM) |
| References, semantics, conflicts, exceptions, completeness and validation outcome | `rca.validation` |
| Graph-aware coverage and `UNKNOWN` / `NOT_APPLICABLE` categories | `rca.validation.coverage` |
| Active scenario definitions and applicability | `rca.mcmm.ScenarioMatrix` |
| Formal exception lifecycle | existing exception validator/formal backend result |
| Advice only | `InferenceReport` / `InferenceCandidate` |
| Explicit candidate-to-UCM decisions | Step-28 application receipts plus current UCM contents |
| Tool/collateral prerequisite evidence | Step-25 `EDAPreflight` |

The engine never constructs a second UCM, validation result, coverage calculation,
provenance store, inference engine, knowledge base, cache, QoR record, execution
manifest, artifact authority, or history entry. It never fixes a requirement,
infers a period/budget/relationship, refreshes evidence, accepts advice, rewrites
a scenario, writes a file, or runs synthesis, STA, or a proof.

## Public API

```python
from rca.readiness import ConstraintReadinessEngine, assess_constraint_readiness

# Functional and stateless-facade forms use the same sole engine.
report = assess_constraint_readiness(
    config,
    canonical_ucm,
    design,
    timing_graph,
    validation=existing_validation,        # optional; existing validator otherwise used conservatively
    inference_report=advisory_report,      # optional and non-mutating
    application_receipts=(receipt,),       # optional, externally supplied
    eda_preflight=existing_preflight,      # optional Step-25 observation
    scenario_ids=("FUNC_SLOW",),          # optional active MCMM report scope
)
print(report.status.value)
print(report.to_dict())
```

The immutable report models are:

- `ReadinessStatus`: `READY`, `READY_WITH_WARNINGS`, `BLOCKED`,
  `INCOMPLETE`, `UNKNOWN`, and `UNSUPPORTED`.
- `ReadinessRequirement`, `ReadinessFinding`, `ReadinessBlocker`, and
  `ReadinessEvidence` for requirement-level, actionable evidence traces.
- `ScenarioReadinessResult` for each selected active MCMM scenario.
- `ConstraintReadinessReport`, including stable requirements/findings/blockers,
  source snapshot identities, summaries projected from validation/coverage, stale
  evidence, and next actions.

`ReadinessStatus` is deliberately separate from validation status, coverage,
inference, application, formal verification, MCMM/QoR, and EDA execution status.

## Assessment categories

The engine projects the following existing evidence categories when applicable:

1. **Source/design:** supplied canonical UCM, elaborated design, and timing graph.
2. **Clock intent:** primary sources/periods, relationships, and observed
   generated-clock candidates requiring explicit canonical intent.
3. **I/O timing:** existing completeness findings and input/output coverage.
4. **Exceptions:** existing exception validation and verification lifecycle.
5. **MCMM:** active matrix validity and scenario-specific readiness results.
6. **Validation/coverage:** validation report state and categorical coverage gaps;
   it does not recalculate either.
7. **Evidence/provenance:** retained UCM evidence, unconfirmed required
   assumptions, and recognized Step-28 application links.
8. **Staleness:** supplied snapshot identity comparisons only.
9. **EDA prerequisites:** Step-25 preflight only for a configured real backend.

Requirements are configuration-aware. Generic/mock paths do not require real-tool
preflight. A `yosys_opensta` configuration requires supplied/current Step-25
preflight evidence before readiness can be fully ready. Formal proof is required
for relevant exception evidence only when `formal.backend: symbiyosys` is
configured. With the default conservative formal backend, an unverified exception
stays an explicit warning/uncertainty; readiness never fabricates a proof.

Power and other flow-specific collateral are not universal readiness requirements.
They remain conditional on the existing configured flow/preflight evidence.

## Status and blocker policy

Aggregate precedence is deterministic:

1. `BLOCKED` — hard missing/invalid design, UCM, clock intent, formal/preflight,
   stale, or other blocking evidence;
2. `UNSUPPORTED` — selected configured capability is explicitly unsupported;
3. `INCOMPLETE` — known missing required intent/evidence;
4. `UNKNOWN` — evidence is absent or explicitly unknown/unresolved;
5. `READY_WITH_WARNINGS` — configured requirements are met subject to retained
   non-blocking uncertainty/warnings;
6. `READY` — all applicable configured requirements are ready.

`UNKNOWN`, `UNRESOLVED`, missing evidence, and unsupported evidence are never
upgraded to a pass. `ReadinessBlocker` is a typed filtered view of blocker
findings. Known incomplete items are shown as warnings/next actions rather than
silently treated as closure.

MCMM results remain scenario-scoped. A scenario-specific issue appears on that
scenario requirement/result; it is not rewritten as a different global intent.
Global prerequisites still correctly affect every active scenario.

## Staleness and advisory/application boundaries

Readiness compares snapshots only where identities are meaningfully comparable:

- An advisory `InferenceCandidate.source_snapshot_identity` is compared to the
  current design, timing graph, UCM, and configuration snapshots. A mismatch is a
  stale evidence blocker, not a candidate refresh or implicit re-inference.
- Stored application metadata is recognized as application provenance only when
  every referenced applied constraint still exists in the current canonical UCM.
  Its original source-UCM snapshot is intentionally **not** compared to current
  UCM: a successful application necessarily changed the UCM.
- A separately supplied Step-28 receipt can be compared through its
  `ucm_after_snapshot_identity`; if it differs from current UCM it is stale.
  A receipt naming absent UCM constraints is not accepted as intent.

Advice remains advice. The report may mention candidates as a suggested review
workflow, but it does not materialize or apply them. Only Step 28's explicit
controlled application API changes the UCM.

## CLI

```bash
rca readiness project.yaml --ucm reviewed-ucm.json
rca readiness project.yaml --ucm reviewed-ucm.json --scenario FUNC_SLOW --json
```

`--ucm` is required: the command refuses to substitute a newly inferred or
configuration-derived model for canonical UCM intent. It may produce advisory
candidates in memory solely to offer context, but it does not write inference
state. `--scenario` is repeatable and only accepts active MCMM scenario IDs.

The command creates no UCM, SDC, coverage, history, SQLite, artifact, application,
or EDA state. It does not run synthesis, STA, or formal proof execution. For a
real configured flow it reports missing Step-25 preflight evidence rather than
running it; use `rca doctor` separately to inspect prerequisites.

## Determinism

Model records are frozen and use stable IDs derived from their content. JSON is a
sorted projection: requirements, evidence, findings, blockers, scenario results,
references, actions, and summary mappings have deterministic ordering. The report
has no timestamps, filesystem writes, cache use, or execution side effects.
