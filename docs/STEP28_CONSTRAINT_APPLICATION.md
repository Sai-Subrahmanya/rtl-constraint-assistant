# Step 28 — Controlled Constraint Application & Intent Resolution

## Purpose and boundary

Step 28 introduces the only new route by which a Step-27 advisory inference
candidate may become canonical UCM intent.  It is deliberately an orchestration
layer over the existing `ConstraintSet`, canonical snapshot format, Step-9
semantic comparison, provenance/evidence, assumption ledger, MCMM scenario
matrix, validation pipeline, coverage, and SDC generators.  It does **not** add
another UCM, inference engine, parser, validation engine, history store, or
artifact authority.

The boundary is strict:

- `rca infer`, knowledge search/suggest, ranking, reports, serialization,
  explanations, coverage, and conflict analysis are advisory/read-only with
  respect to UCM.
- A caller must identify **one** freshly reviewed candidate and submit one
  explicit decision. There is no apply-all operation.
- Application never creates numerical timing intent, clock details,
  relationships, exceptions, dependencies, or assumptions that were absent
  from the candidate and its evidence.
- Existing user/fixed UCM intent is never overwritten, repaired, deleted, or
  relabelled as inference output.
- SQLite remains a historical/query sidecar; application neither writes it nor
  changes its authority. Run artifacts/manifests likewise remain unchanged
  unless another explicit command creates them.

## Typed interface

Public types live in `rca.inference`:

```python
from rca.inference import (
    ConstraintApplication, IntentDecision, IntentDecisionKind,
    ApplicationStatus, apply_intent_decision,
)

request = ConstraintApplication(
    candidate=reviewed_candidate,
    decision=IntentDecision(
        candidate_id=reviewed_candidate.id,
        kind=IntentDecisionKind.ACCEPT,
        rationale="Reviewed against the implementation timing input.",
    ),
    scenario_ids=("FUNC_SS",),  # only when the candidate is scoped there
    dry_run=False,
)
receipt = apply_intent_decision(request, canonical_ucm, design, timing_graph, config)
```

`IntentDecisionKind` is an explicit reviewer action:

| Decision | Meaning |
|---|---|
| `ACCEPT` | Apply only an `INFERRED` / `ACCEPTABLE` candidate after all safety checks. |
| `CONFIRM` | Apply an eligible inferred or confirmation-required candidate only after the same checks. |
| `REJECT` | Record a non-mutating rejection receipt. |
| `DEFER` | Record a non-mutating deferral receipt. |
| `ALREADY_SATISFIED` | Verify that the semantic UCM equivalent is already present; it never adds a duplicate. |
| `CONFLICT`, `INVALID` | Explicit non-mutating refusal states. |

`ApplicationStatus` is intentionally separate from that decision:
`NOT_APPLIED`, `APPLIED`, `REJECTED`, `DEFERRED`, `BLOCKED`,
`ALREADY_PRESENT`, `FAILED_VALIDATION`, and `STALE`.

Every result is a deterministic `ConstraintApplicationResult`. `to_dict()`
contains the candidate and semantic identities, source provenance/snapshot,
decision and status, applied / already-present / conflicting / explicitly
rejected IDs, validation status/summary/issues, application evidence, blockers
and warnings, scenario scope, before/after UCM snapshot identities, and the
exact `ucm_mutated` flag. Receipt IDs are deterministic and
application evidence uses a fixed provenance epoch; no wall-clock timestamp is
introduced by this path.

Human-readable rendering is available through
`rca.explanation.explain_constraint_application(receipt)`. It explains the
receipt without claiming an inference-origin constraint is user-authored.

## Safety and resolution sequence

An application evaluates, in deterministic order:

1. validates that the decision names the supplied candidate and that it is an
   allowed explicit decision;
2. computes and verifies the candidate's canonical application identity from
   all template constraints (Step-9 semantic identity plus a lossless canonical
   template hash, including source/provenance and dependency/assumption links);
3. verifies candidate evidence, unresolved-information state, candidate
   eligibility, and design/timing/UCM source snapshot identities (and the
   configuration snapshot whenever `config` is supplied);
4. parses all templates through the canonical constraint parser and rejects
   unsupported/uncomparable semantics or duplicate templates;
5. semantically compares the proposal against UCM. Exact equivalents return
   `ALREADY_PRESENT`; partial multi-template duplicates, same-scope semantic
   disagreements, and existing unsupported semantics block safely;
6. checks candidate and requested MCMM scenario scope against the active
   scenario matrix without broadening it; checks all dependency and assumption
   references, including internal dependencies within an atomic group;
7. clones the caller UCM, appends all proposed constraints only to that clone,
   runs the existing authoritative validation, and classifies failed,
   unresolved, and unsupported results conservatively;
8. atomically replaces the caller UCM contents only when clone validation is
   safe and the request is not a dry run.

Consequently a stale candidate is never implicitly regenerated or applied;
failed/unresolved validation and every blocked decision leave the supplied UCM
unchanged. A repeated `ACCEPT` of an already committed semantic equivalent
returns `ALREADY_PRESENT` rather than adding another constraint.

On successful application, the committed constraint stays
`SourceKind.INFERENCE`. Its original candidate provenance is retained and
explicit `INTENT-APPLICATION` and `INTENT-APPLICATION-VALIDATION` evidence is
appended. The established legacy `INFERENCE-ACCEPT` evidence remains available
for Step-27 compatibility; it does not create a second mutation path.

## Dry run

Set `dry_run=True` in the API or pass `--dry-run` to the CLI. A dry run executes
the same source, semantic, dependency, scenario, and isolated-validation checks
and returns its receipt, but it does not mutate the caller UCM, write a UCM
snapshot, emit SDC, change coverage, or write SQLite/history.

## CLI

The CLI deliberately exposes only one-candidate controlled resolution:

```bash
# Advisory report only. --ucm supplies an existing canonical snapshot to read.
rca infer project.yaml --ucm current-ucm.json --json

# Inspect an application without changing UCM or creating any SDC/history data.
rca apply project.yaml \
  --candidate IC-... --decision ACCEPT --dry-run --json

# Commit exactly that validated candidate and write a canonical UCM snapshot.
rca apply project.yaml \
  --candidate IC-... --decision ACCEPT \
  --ucm current-ucm.json --output reviewed-ucm.json --json

# Scenario-scoped candidates must name their exact scope; repeat as needed.
rca apply project.yaml \
  --candidate IC-... --decision CONFIRM \
  --scenario FUNC_SS --scenario SCAN_FF --output reviewed-ucm.json
```

`--candidate` and `--decision` are required. `apply` re-runs deterministic
advisory inference against the supplied snapshot, so an unknown candidate ID is
reported as JSON `INVALID` (with exit code 2 when `--json` is used). It writes a
canonical UCM snapshot only when `receipt.ucm_mutated` is true. It does not emit
SDC; use the established `rca generate`, `rca validate`, and `rca coverage`
commands after reviewing the saved UCM in the normal project workflow.

## Deliberate limitations

- Application cannot turn weak naming/structural evidence into false paths,
  multicycle paths, asynchronous relationships, generated-clock details, or
  numerical timing values. Such candidates remain blocked or require the
  existing stronger evidence/confirmation path.
- Candidate snapshots are intentionally strict. Any changed design, timing
  graph, configuration, or non-idempotent UCM source yields `STALE` and requires
  explicit reinference/review.
- Ambiguous, inactive, invalid, or broadened scenario scope is blocked rather
  than guessed.
- Validation warnings unrelated to the candidate are retained in the receipt;
  application success does not claim a complete/signoff-clean design.
- `rca apply` does not generate SDC, alter coverage independently, execute EDA,
  or persist a reviewer decision ledger. The receipt and committed provenance
  are the bounded UCM application evidence.

## Test coverage

Focused Step-28 tests cover explicit accept/reject/defer/duplicate behavior,
design/timing/UCM staleness, fixed-intent conflicts, malformed or unsupported
templates, missing evidence/dependencies/assumptions, validation failure and
unresolved validation, atomic multi-template groups, MCMM scope, deterministic
knowledge conflict handling, explanation rendering, CLI required arguments,
deterministic dry-run, and an RTL → inference → explicit application → UCM →
validation → coverage → SDC workflow. Real EDA remains optional and is not a
Step-28 prerequisite.
