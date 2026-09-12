# Step 30 — Constraint Lineage & Audit Trail

> **Lineage is a read-only traceability projection, not a second canonical
> history or provenance authority.**

## Purpose and boundary

Step 30 makes an existing timing-constraint lifecycle inspectable:

```text
RTL → analysis → knowledge → inference → explicit application → canonical UCM
    → validation / coverage → readiness → downstream generation or EDA
```

The `rca.lineage` package joins references already retained by authoritative
systems. It does not create a UCM, update UCM provenance, accept advice, run
validation, build coverage, run a formal proof, execute EDA, write artifacts,
create a manifest, access/write SQLite history, or maintain a graph database.
It is safe to call repeatedly.

| Question | Existing authority projected |
|---|---|
| What canonical constraint exists now? | `ConstraintSet` / canonical UCM |
| Origin, evidence, dependencies, assumptions, import metadata | `Constraint.provenance` / canonical `Evidence` |
| Candidate and structural/knowledge advice | `InferenceReport` / `InferenceCandidate` |
| Explicit decision and current UCM application linkage | Step-28 receipts and `ConstraintSet.metadata["constraint_applications"]` |
| Validation/coverage/formal evidence | supplied existing `ValidationResult` and formal results |
| Scenario applicability | canonical `Constraint.scenario_ids` / UCM scenarios |
| Readiness | supplied existing `ConstraintReadinessReport` |
| Semantic change between snapshots | existing Step-9 `rca.equivalence.compare` |

The canonical UCM remains the only current constraint authority. SQLite remains
historical/query-only, and existing artifact/manifest ownership is unchanged.

## Public API

```python
from rca.lineage import build_constraint_lineage, compare_constraint_lineage

current = build_constraint_lineage(
    canonical_ucm,
    config=config,                    # optional current identity context
    design=design, timing_graph=tg,   # optional current identity context
    inference_report=advice,          # optional and advisory only
    application_receipts=(receipt,),  # optional existing Step-28 receipts
    validation=validation_result,     # optional existing output
    formal_results=(formal_result,),  # optional existing output; never executed here
    readiness=readiness_report,       # optional existing Step-29 output
)

changes = compare_constraint_lineage(
    before_ucm, after_ucm,
    before_readiness=before_readiness,
    after_readiness=after_readiness,
)
```

`ConstraintLineageEngine().build(...)` and `.compare(...)` are stateless façade
forms over the same sole read-only implementation.

The typed report records are `ConstraintLineage`, `ConstraintLineageReport`,
`LineageEvent`, `LineageSnapshot`, `LineageScenarioScope`, `LineageChange`, and
`UCMChangeSet`. They reference the existing `Evidence`, `ProvenanceRecord`,
validation, readiness, and semantic-comparison types instead of introducing a
second evidence/provenance model.

## Source and event classification

Observed source classifications are intentionally separate from UCM
`SourceKind`:

- `USER_PROVIDED` — canonical UCM records user source.
- `IMPORTED` — existing SDC/import source or retained `ImportMetadata`.
- `INFERRED` — retained inference source/rule/evidence without an application
  linkage.
- `EXPLICITLY_ACCEPTED` — a Step-28 applied receipt names a constraint that is
  still present in current UCM.
- `SYSTEM_OBSERVED` — canonical RTL/tool/library/physical/derived source.
- `UNKNOWN` — source/provenance is missing or only a legacy default remains.
- `KNOWLEDGE_REFERENCED` — retained advisory knowledge evidence informed an
  inferred canonical constraint. Its `origin_source` remains `INFERRED`; the
  knowledge item does not become canonical UCM authority or overwrite intent.

Events have deterministic IDs and use `CREATED`, `ACCEPTED`, `REJECTED`,
`DEFERRED`, `VALIDATED`, `INVALIDATED`, `MODIFIED`, `REMOVED`, `BECAME_STALE`,
`READINESS_CHANGED`, and `APPLICATION_ATTEMPTED`. An observed validation result
does not alter constraint origin or lifecycle. An application attempt that did
not mutate UCM is retained as an attempt and never becomes a creation event.

For an accepted inference, lineage follows available IDs:

```text
current constraint ID
  → application ID
  → candidate ID
  → inference rule IDs / candidate Evidence
  → knowledge references / assumptions
  → validation and formal evidence
  → exact MCMM scope
```

Absent identifiers remain explicit linkage gaps; the report never invents a
candidate, a receipt, an inference rule, or a causal link.

## Evidence, knowledge, validation, and formal results

The report exposes existing canonical `Evidence` records unchanged, including
RTL/design/clock/port/net/register structural observations, inference rules,
assumptions, receipt/application evidence, and import metadata. A supplied
`ValidationResult` is represented as validation evidence—both constraint-scoped
issues and aggregate report scope—not as the origin of the constraint.

Existing formal results and formal evidence nested in exception validation issues
retain the proof/property/tool/result/counterexample data exactly as provided.
`UNRESOLVED`/`UNVERIFIED` stays unresolved. Lineage never runs a backend or
claims a proof merely because structural analysis exists.

Knowledge references retain knowledge item ID, suggestion ID, trust level,
applicability, rationale/evidence where previously retained, and conflicts. They
are always annotated advisory and never overwrite fixed UCM intent.

## MCMM and stale evidence

`LineageScenarioScope` preserves a constraint's exact stored `scenario_ids`,
whether it is global or scenario-specific, its active applicability projection,
and unknown IDs. Snapshot comparison uses the existing Step-9 MCMM-aware
comparison, so scenario additions/removals/definition changes remain visible and
scenario-specific constraints are not collapsed into global intent.

Stale source evidence is retained as `BECAME_STALE`:

- candidate source design/timing/config identities may be compared to supplied
  current inputs;
- application receipt `ucm_after_snapshot_identity` is compared only when it
  exists;
- stored Step-28 source-UCM identity is **not** compared to the current UCM,
  because an applied candidate necessarily changed the UCM;
- current UCM membership is independently verified before any event says the
  referenced canonical constraint is current.

Stale evidence is neither silently refreshed nor deleted.

## Semantic snapshot comparison

`compare_constraint_lineage(A, B)` delegates to Step-9 semantic comparison; it
never compares raw JSON/text. Each deterministic `LineageChange` is `ADDED`,
`REMOVED`, `MODIFIED`, `UNCHANGED`, or `UNKNOWN` and preserves:

- before/after canonical constraint IDs and semantic identities;
- Step-9 field-level semantic differences;
- before/after provenance source classifications;
- exact before/after MCMM scope;
- available existing evidence references.

Thus `10ns` and `10000ps` are unchanged where Step 9 normalizes them, while a
clock period change such as `10ns → 8ns` is modified. Unsupported semantics
remain `UNKNOWN`; no equivalence is guessed. Step-9 pairing may conservatively
represent an unrelated target-set change as remove/add rather than force a
modified pair.

If both supplied Step-29 reports differ, the change set includes a
`READINESS_CHANGED` event listing changed requirements/findings and explicitly
marks it as temporal correlation only (`causality: not_inferred`). It does not
create a second readiness engine or claim that a particular constraint caused a
status change.

## CLI

```bash
# Current canonical snapshot trace
rca lineage project.yaml --ucm reviewed-ucm.json --json

# Explicit semantic comparison; no history snapshot is selected implicitly
rca lineage project.yaml --before before-ucm.json --after after-ucm.json --json
```

Exactly one mode is required: either `--ucm`, or both `--before` and `--after`.
The CLI parses the configured RTL and creates advisory context only in memory;
it uses conservative existing validation and Step-29 readiness projections
without running SymbiYosys, synthesis, STA, or another EDA flow. It writes no
UCM, SDC, coverage, application, artifact, manifest, SQLite, cache, or history
record. No `--output` option is provided, so a lineage artifact is never
silently created.

## Determinism and limitations

Event and change IDs are content-addressed stable hashes of event kind,
constraint/candidate/application IDs, snapshot identities, relevant evidence
identifiers, and non-volatile details. Display timestamps are retained only from
existing authoritative evidence and do not determine an event/change ID. Lists
and JSON mappings are rendered in stable order.

Lineage is only as complete as the evidence supplied or retained by the existing
systems. Legacy UCMs may have unknown source linkage. Stored Step-28 metadata
may preserve a candidate ID but not the complete original candidate report. The
lineage report exposes those gaps rather than reconstructing history. It makes
no signoff, QoR, coverage, timing, formal-proof, or EDA-execution claim.
